"""SPIFFE / SPIRE credential provider.

Mints a verifiable identity JWT-SVID for an agent, in two acquisition
modes:

* **File mode** — reads a JWT-SVID written to disk by ``spiffe-helper``.
  A thin alias over :class:`FileTokenProvider`; needs no SPIFFE library.
* **SDK mode** — lazily imports :mod:`spiffe` (the py-spiffe library,
  ``pip install spiffe``), connects to the SPIRE agent's Workload API
  Unix socket, and fetches a JWT-SVID for the configured audience.

py-spiffe is an *optional* dependency (``pip install
promptise[identity-spiffe]``). The SDK provider imports it inside the
acquisition method, never at module top, so a workload that uses
``spiffe-helper`` file output — or a different cloud — does not need
it installed. When the import fails the provider raises
:class:`ProviderConfigError` naming the exact install command.

The Workload-API call is coded against py-spiffe's public surface
(``spiffe>=0.2``): ``spiffe.WorkloadApiClient(socket_path=...)``,
``fetch_jwt_svid(audience={...})`` and the serialized token on
``JwtSvid.token``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Literal

from .._core.callable_provider import CallableTokenProvider
from .._core.errors import CredentialAcquisitionError, ProviderConfigError
from .._core.file_provider import FileTokenProvider
from .._core.retry import retry_call

#: Environment variable naming the SPIRE agent Workload API socket.
ENV_SPIFFE_ENDPOINT_SOCKET: str = "SPIFFE_ENDPOINT_SOCKET"

#: Default Workload API socket path used when the env var is unset.
DEFAULT_SPIFFE_ENDPOINT_SOCKET: str = "unix:///tmp/spire-agent/public/api.sock"

#: Default audience requested in the JWT-SVID — the resource the agent
#: authenticates to. Override with the audience your resource expects.
_DEFAULT_AUDIENCE: str = "api://promptise-agent"

#: The exact install command surfaced when py-spiffe is missing.
_INSTALL_HINT: str = "pip install promptise[identity-spiffe]"


class SpiffeFileProvider(FileTokenProvider):
    """SPIFFE credential source reading a JWT-SVID from ``spiffe-helper``.

    Args:
        token_file: Path to the JWT-SVID file ``spiffe-helper`` rotates.
    """

    def __init__(
        self,
        *,
        token_file: str | Path,
    ) -> None:
        super().__init__(
            token_file=token_file,
            provider_label="spiffe-file",
        )


class SpiffeSdkProvider(CallableTokenProvider):
    """SPIFFE credential source using the Workload API via py-spiffe.

    Args:
        socket_path: SPIRE agent Workload API socket. Falls back to
            ``$SPIFFE_ENDPOINT_SOCKET`` and then
            :data:`DEFAULT_SPIFFE_ENDPOINT_SOCKET`.
        audience: Audience to request in the JWT-SVID — the resource the
            agent authenticates to.
    """

    def __init__(
        self,
        *,
        socket_path: str | None = None,
        audience: str = _DEFAULT_AUDIENCE,
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
            default_audience=audience,
        )

    def _fetch_jwt_svid(self, audience: str | None = None) -> str:
        """Fetch a JWT-SVID from the SPIRE Workload API.

        Requests ``audience`` when given, otherwise the configured default.
        Lazily imports py-spiffe. Raises :class:`ProviderConfigError`
        when it is missing and :class:`CredentialAcquisitionError`
        when connecting to or calling the Workload API fails; both
        propagate through the base unchanged.
        """
        try:
            from spiffe import WorkloadApiClient  # noqa: E402
        except ImportError as exc:
            raise ProviderConfigError(
                f"[spiffe-sdk] SPIFFE SDK mode requires the py-spiffe "
                f"library (the 'spiffe' package), which is not installed. "
                f"Install it with: {_INSTALL_HINT}. "
                f"Alternatively use file mode "
                f"(from_spiffe(token_file=...)) with spiffe-helper, which "
                f"needs no SPIFFE library."
            ) from exc

        client: Any = None
        try:
            # The client validates the socket address and checks that the
            # socket exists, so constructing it can fail too.
            client = WorkloadApiClient(socket_path=self._socket_path)
            jwt_svid = retry_call(
                lambda: client.fetch_jwt_svid(audience={audience or self._audience}),
                is_transient=lambda _exc: True,
            )
        except Exception as exc:
            raise CredentialAcquisitionError(
                f"[spiffe-sdk] fetching a JWT-SVID from the Workload API at "
                f"{self._socket_path} failed ({type(exc).__name__}: {exc}). "
                f"Most common cause: no SPIRE agent is listening on that "
                f"socket, or this workload has no registration entry."
            ) from exc
        finally:
            if client is not None:
                _close_quietly(client)

        return _extract_serialized_token(jwt_svid, socket_path=self._socket_path)


def _close_quietly(client: Any) -> None:
    """Best-effort close of a Workload API client.

    Closes it if a ``close`` callable exists and swallows any teardown
    error so it does not mask the fetch result.
    """
    close = getattr(client, "close", None)
    if callable(close):
        try:
            close()
        except Exception:  # noqa: BLE001 — teardown failures must not mask success
            pass


def _extract_serialized_token(jwt_svid: Any, *, socket_path: str) -> str:
    """Extract the serialized JWT string from a ``spiffe.JwtSvid``.

    py-spiffe exposes it as the ``token`` property. The other accessors
    are tried as a fallback for other Workload API clients; a precise
    error is raised if none yields a non-empty string.
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
    raise CredentialAcquisitionError(
        f"[spiffe-sdk] the Workload API at {socket_path} returned a "
        f"JwtSvid, but the serialized token could not be extracted from "
        f"it. Most common cause: the installed py-spiffe version exposes "
        f"the token under an unexpected accessor."
    )


def from_spiffe(
    *,
    mode: Literal["auto", "file", "sdk"] = "auto",
    token_file: str | Path | None = None,
    socket_path: str | None = None,
    audience: str = _DEFAULT_AUDIENCE,
) -> SpiffeFileProvider | SpiffeSdkProvider:
    """Build a SPIFFE / SPIRE credential source.

    Args:
        mode: ``"auto"`` (default) picks file mode when ``token_file``
            is supplied, otherwise SDK mode. Force a mode with
            ``"file"`` or ``"sdk"``.
        token_file: Path to a JWT-SVID file (file mode). Required for
            file mode.
        socket_path: SPIRE Workload API socket (SDK mode). Falls back to
            ``$SPIFFE_ENDPOINT_SOCKET`` and then the default socket.
        audience: Audience for the JWT-SVID (SDK mode).

    Returns:
        A :class:`SpiffeFileProvider` for file mode or a
        :class:`SpiffeSdkProvider` for SDK mode.

    Raises:
        ProviderConfigError: If ``mode`` is not ``auto``/``file``/``sdk``,
            or if file mode is selected without ``token_file``.
    """
    resolved_mode = mode
    if resolved_mode == "auto":
        resolved_mode = "file" if token_file is not None else "sdk"

    if resolved_mode == "file":
        if token_file is None:
            raise ProviderConfigError(
                "from_spiffe file mode requires token_file=... — the path "
                "spiffe-helper writes the JWT-SVID to. Most common cause: "
                "mode='file' was requested without supplying the path."
            )
        return SpiffeFileProvider(token_file=token_file)
    if resolved_mode == "sdk":
        return SpiffeSdkProvider(socket_path=socket_path, audience=audience)
    raise ProviderConfigError(
        f"Unknown SPIFFE mode {mode!r}. Valid modes are 'auto', 'file', and 'sdk'."
    )
