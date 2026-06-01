"""SPIFFE / SPIRE identity provider.

Two acquisition modes:

* **File mode** — reads a JWT-SVID written to disk by ``spiffe-helper``.
  A thin alias over :class:`FileTokenProvider`; needs no pyspiffe.
* **SDK mode** — lazily imports :mod:`pyspiffe`, connects to the SPIRE
  agent's Workload API Unix socket, and fetches a JWT-SVID for the
  configured audience.

pyspiffe is an *optional* dependency (``pip install
promptise[identity-spiffe]``). The SDK provider imports it inside the
acquisition method, never at module top, so a workload that uses
``spiffe-helper`` file output — or a different cloud — does not need
pyspiffe installed. When the import fails the provider raises
:class:`ProviderConfigError` naming the exact install command.

The pyspiffe Workload-API call is coded against the documented
``pyspiffe>=1.0`` surface (``WorkloadApiClient.fetch_jwt_svid``). The
serialized-token accessor and client teardown are handled defensively
because their exact form has varied across pyspiffe releases.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal, cast

from .._core.callable_provider import CallableTokenProvider
from .._core.errors import ProviderConfigError, TokenAcquisitionError
from .._core.file_provider import FileTokenProvider
from .._internal.env import _resolve_anthropic_credentials

#: Environment variable naming the SPIRE agent Workload API socket.
ENV_SPIFFE_ENDPOINT_SOCKET: str = "SPIFFE_ENDPOINT_SOCKET"

#: Default Workload API socket path used when the env var is unset.
DEFAULT_SPIFFE_ENDPOINT_SOCKET: str = "unix:///tmp/spire-agent/public/api.sock"

#: Default audience requested in the JWT-SVID.
_DEFAULT_AUDIENCE: str = "https://api.anthropic.com"

#: The exact install command surfaced when pyspiffe is missing.
_INSTALL_HINT: str = "pip install promptise[identity-spiffe]"


class SpiffeFileProvider(FileTokenProvider):
    """SPIFFE provider reading a JWT-SVID written by ``spiffe-helper``.

    Args:
        token_file: Path to the JWT-SVID file ``spiffe-helper`` rotates.
        federation_rule_id: See :class:`IdentityProvider`.
        organization_id: See :class:`IdentityProvider`.
        service_account_id: See :class:`IdentityProvider`.
        workspace_id: See :class:`IdentityProvider`.
        exchange_timeout: See :class:`IdentityProvider`.
    """

    def __init__(
        self,
        *,
        token_file: str | Path,
        federation_rule_id: str,
        organization_id: str,
        service_account_id: str,
        workspace_id: str | None = None,
        exchange_timeout: float = 10.0,
    ) -> None:
        super().__init__(
            token_file=token_file,
            provider_label="spiffe-file",
            federation_rule_id=federation_rule_id,
            organization_id=organization_id,
            service_account_id=service_account_id,
            workspace_id=workspace_id,
            exchange_timeout=exchange_timeout,
        )


class SpiffeSdkProvider(CallableTokenProvider):
    """SPIFFE provider using the Workload API via pyspiffe (lazy import).

    Args:
        socket_path: SPIRE agent Workload API socket. Falls back to
            ``$SPIFFE_ENDPOINT_SOCKET`` and then
            :data:`DEFAULT_SPIFFE_ENDPOINT_SOCKET`.
        audience: Audience to request in the JWT-SVID. Defaults to
            ``https://api.anthropic.com``.
        federation_rule_id: See :class:`IdentityProvider`.
        organization_id: See :class:`IdentityProvider`.
        service_account_id: See :class:`IdentityProvider`.
        workspace_id: See :class:`IdentityProvider`.
        exchange_timeout: See :class:`IdentityProvider`.
    """

    def __init__(
        self,
        *,
        socket_path: str | None = None,
        audience: str = _DEFAULT_AUDIENCE,
        federation_rule_id: str,
        organization_id: str,
        service_account_id: str,
        workspace_id: str | None = None,
        exchange_timeout: float = 10.0,
    ) -> None:
        self._socket_path: str = (
            socket_path
            or os.environ.get(ENV_SPIFFE_ENDPOINT_SOCKET)
            or DEFAULT_SPIFFE_ENDPOINT_SOCKET
        )
        self._audience: str = audience
        super().__init__(
            token_fn=self._fetch_jwt_svid,
            provider_label="spiffe-sdk",
            federation_rule_id=federation_rule_id,
            organization_id=organization_id,
            service_account_id=service_account_id,
            workspace_id=workspace_id,
            exchange_timeout=exchange_timeout,
        )

    def _fetch_jwt_svid(self) -> str:
        """Fetch a JWT-SVID from the SPIRE Workload API.

        Lazily imports pyspiffe (principle 4.2). Raises
        :class:`ProviderConfigError` when pyspiffe is missing and
        :class:`TokenAcquisitionError` when the Workload API call
        fails; both propagate through the base unchanged.
        """
        try:
            from pyspiffe.workloadapi.workload_api_client import (  # noqa: E402
                WorkloadApiClient,
            )
        except ImportError as exc:
            raise ProviderConfigError(
                f"[spiffe-sdk] SPIFFE SDK mode requires pyspiffe, which is "
                f"not installed. Install it with: {_INSTALL_HINT}. "
                f"Alternatively use file mode "
                f"(from_spiffe(token_file=...)) with spiffe-helper, which "
                f"needs no pyspiffe."
            ) from exc

        client = WorkloadApiClient(spiffe_socket_path=self._socket_path)
        try:
            jwt_svid = client.fetch_jwt_svid(audiences={self._audience})
        except Exception as exc:
            raise TokenAcquisitionError(
                f"[spiffe-sdk] fetching a JWT-SVID from the Workload API at "
                f"{self._socket_path} failed ({type(exc).__name__}: {exc}). "
                f"Most common cause: no SPIRE agent is listening on that "
                f"socket, or this workload has no registration entry."
            ) from exc
        finally:
            _close_quietly(client)

        return _extract_serialized_token(jwt_svid, socket_path=self._socket_path)


def _close_quietly(client: Any) -> None:
    """Best-effort close of a pyspiffe client.

    The teardown method has varied across pyspiffe releases; close it
    if a ``close`` callable exists and swallow any teardown error so it
    does not mask the fetch result.
    """
    close = getattr(client, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 — teardown failures must not mask success
            pass


def _extract_serialized_token(jwt_svid: Any, *, socket_path: str) -> str:
    """Extract the serialized JWT string from a pyspiffe JwtSvid.

    The serialized-token accessor differs across pyspiffe versions —
    an attribute on some, a method on others. Try the documented forms
    in order and raise a precise error if none yields a non-empty string.
    """
    for attr in ("token", "token_str", "marshal", "serialize"):
        candidate = getattr(jwt_svid, attr, None)
        if callable(candidate):
            try:
                candidate = candidate()
            except Exception:  # noqa: BLE001 — try the next accessor
                continue
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    raise TokenAcquisitionError(
        f"[spiffe-sdk] the Workload API at {socket_path} returned a "
        f"JwtSvid, but the serialized token could not be extracted from "
        f"it. Most common cause: the installed pyspiffe version exposes "
        f"the token under an unexpected accessor."
    )


def from_spiffe(
    *,
    mode: Literal["auto", "file", "sdk"] = "auto",
    token_file: str | Path | None = None,
    socket_path: str | None = None,
    audience: str = _DEFAULT_AUDIENCE,
    federation_rule_id: str | None = None,
    organization_id: str | None = None,
    service_account_id: str | None = None,
    workspace_id: str | None = None,
    exchange_timeout: float = 10.0,
) -> SpiffeFileProvider | SpiffeSdkProvider:
    """Build a SPIFFE / SPIRE identity provider.

    Args:
        mode: ``"auto"`` (default) picks file mode when ``token_file``
            is supplied, otherwise SDK mode. Force a mode with
            ``"file"`` or ``"sdk"``.
        token_file: Path to a JWT-SVID file (file mode). Required for
            file mode.
        socket_path: SPIRE Workload API socket (SDK mode). Falls back to
            ``$SPIFFE_ENDPOINT_SOCKET`` and then the default socket.
        audience: Audience for the JWT-SVID (SDK mode).
        federation_rule_id: The ``fdrl_*`` identifier. Falls back to
            ``ANTHROPIC_FEDERATION_RULE_ID``.
        organization_id: The organization UUID. Falls back to
            ``ANTHROPIC_ORGANIZATION_ID``.
        service_account_id: The ``svac_*`` identifier. Falls back to
            ``ANTHROPIC_SERVICE_ACCOUNT_ID``.
        workspace_id: Optional ``wrkspc_*`` identifier. Falls back to
            ``ANTHROPIC_WORKSPACE_ID``.
        exchange_timeout: Seconds to wait for the Anthropic exchange.

    Returns:
        A :class:`SpiffeFileProvider` for file mode or a
        :class:`SpiffeSdkProvider` for SDK mode.

    Raises:
        ProviderConfigError: If ``mode`` is not ``auto``/``file``/``sdk``,
            if file mode is selected without ``token_file``, or if a
            required federation identifier is unset.
    """
    resolved_mode = mode
    if resolved_mode == "auto":
        resolved_mode = "file" if token_file is not None else "sdk"

    creds = _resolve_anthropic_credentials(
        federation_rule_id=federation_rule_id,
        organization_id=organization_id,
        service_account_id=service_account_id,
        workspace_id=workspace_id,
    )
    fed = cast(str, creds["federation_rule_id"])
    org = cast(str, creds["organization_id"])
    svc = cast(str, creds["service_account_id"])
    ws = creds["workspace_id"]

    if resolved_mode == "file":
        if token_file is None:
            raise ProviderConfigError(
                "from_spiffe file mode requires token_file=... — the path "
                "spiffe-helper writes the JWT-SVID to. Most common cause: "
                "mode='file' was requested without supplying the path."
            )
        return SpiffeFileProvider(
            token_file=token_file,
            federation_rule_id=fed,
            organization_id=org,
            service_account_id=svc,
            workspace_id=ws,
            exchange_timeout=exchange_timeout,
        )
    if resolved_mode == "sdk":
        return SpiffeSdkProvider(
            socket_path=socket_path,
            audience=audience,
            federation_rule_id=fed,
            organization_id=org,
            service_account_id=svc,
            workspace_id=ws,
            exchange_timeout=exchange_timeout,
        )
    raise ProviderConfigError(
        f"Unknown SPIFFE mode {mode!r}. Valid modes are 'auto', 'file', and "
        f"'sdk'."
    )
