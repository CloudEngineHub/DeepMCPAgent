"""Microsoft Entra ID identity provider.

Two acquisition modes:

* **Managed identity (IMDS)** — for Azure VMs, VM Scale Sets, and other
  compute with a system- or user-assigned managed identity. Calls the
  Azure Instance Metadata Service and extracts the OIDC ``id_token``
  (not the ``access_token`` — the ``id_token`` is the federation
  assertion).
* **Projected token (AKS Workload Identity)** — for AKS pods using the
  Workload Identity admission webhook, which projects a federated token
  to the file named by ``$AZURE_FEDERATED_TOKEN_FILE`` (default
  ``/var/run/secrets/azure/tokens/azure-identity-token``).

The :func:`from_entra` factory's ``auto`` mode selects projected when
``AZURE_FEDERATED_TOKEN_FILE`` is present (the signal that the workload
runs under AKS Workload Identity), and IMDS otherwise.

Entra needs no cloud SDK — IMDS is a plain HTTP GET — so there is no
optional dependency for this provider.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, cast

import httpx

from .._core.callable_provider import CallableTokenProvider
from .._core.errors import ProviderConfigError, TokenAcquisitionError
from .._core.file_provider import FileTokenProvider
from .._internal.env import _resolve_anthropic_credentials

#: Azure Instance Metadata Service token endpoint (link-local address).
_IMDS_TOKEN_ENDPOINT: str = "http://169.254.169.254/metadata/identity/oauth2/token"

#: Environment variable AKS Workload Identity sets to the projected
#: token file path.
ENV_AZURE_FEDERATED_TOKEN_FILE: str = "AZURE_FEDERATED_TOKEN_FILE"

#: Default projected-token path used when the env var is unset.
DEFAULT_AZURE_FEDERATED_TOKEN_FILE: str = (
    "/var/run/secrets/azure/tokens/azure-identity-token"
)

#: Default resource (audience) requested from IMDS.
_DEFAULT_RESOURCE: str = "https://api.anthropic.com"

#: Default IMDS API version.
_DEFAULT_IMDS_API_VERSION: str = "2018-02-01"


class EntraManagedIdentityProvider(CallableTokenProvider):
    """Entra managed-identity provider backed by the Azure IMDS endpoint.

    Args:
        client_id: Optional client ID of a user-assigned managed
            identity. Omit for the resource's system-assigned identity.
        resource: The resource (audience) to request. Defaults to
            ``https://api.anthropic.com``.
        api_version: IMDS API version. Defaults to ``2018-02-01``.
        imds_endpoint: Override for the IMDS URL — primarily for tests.
        request_timeout: Seconds to wait for the IMDS response.
            Defaults to five seconds; IMDS is link-local and fast.
        federation_rule_id: See :class:`IdentityProvider`.
        organization_id: See :class:`IdentityProvider`.
        service_account_id: See :class:`IdentityProvider`.
        workspace_id: See :class:`IdentityProvider`.
        exchange_timeout: See :class:`IdentityProvider`.
    """

    def __init__(
        self,
        *,
        client_id: str | None = None,
        resource: str = _DEFAULT_RESOURCE,
        api_version: str = _DEFAULT_IMDS_API_VERSION,
        imds_endpoint: str = _IMDS_TOKEN_ENDPOINT,
        request_timeout: float = 5.0,
        federation_rule_id: str,
        organization_id: str,
        service_account_id: str,
        workspace_id: str | None = None,
        exchange_timeout: float = 10.0,
    ) -> None:
        self._client_id: str | None = client_id
        self._resource: str = resource
        self._api_version: str = api_version
        self._imds_endpoint: str = imds_endpoint
        self._request_timeout: float = request_timeout
        super().__init__(
            token_fn=self._fetch_imds_id_token,
            provider_label="entra-imds",
            federation_rule_id=federation_rule_id,
            organization_id=organization_id,
            service_account_id=service_account_id,
            workspace_id=workspace_id,
            exchange_timeout=exchange_timeout,
        )

    def _fetch_imds_id_token(self) -> str:
        """Fetch the OIDC ``id_token`` from the Azure IMDS endpoint.

        Raises a precise :class:`TokenAcquisitionError` on every failure
        mode; the base :class:`CallableTokenProvider` passes typed
        errors through unchanged.
        """
        params: dict[str, str] = {
            "api-version": self._api_version,
            "resource": self._resource,
        }
        if self._client_id is not None:
            params["client_id"] = self._client_id

        try:
            response = httpx.get(
                self._imds_endpoint,
                params=params,
                headers={"Metadata": "true"},
                timeout=self._request_timeout,
            )
        except httpx.TimeoutException as exc:
            raise TokenAcquisitionError(
                f"[entra-imds] the Azure Instance Metadata Service at "
                f"{self._imds_endpoint} timed out after "
                f"{self._request_timeout}s. Most common cause: this workload "
                f"is not running on Azure compute, or egress to "
                f"169.254.169.254 is blocked."
            ) from exc
        except httpx.HTTPError as exc:
            raise TokenAcquisitionError(
                f"[entra-imds] could not reach the Azure Instance Metadata "
                f"Service at {self._imds_endpoint} ({type(exc).__name__}). "
                f"Most common cause: this workload is not running on Azure "
                f"compute with a managed identity assigned. Underlying error: "
                f"{exc}"
            ) from exc

        if response.status_code != 200:
            raise TokenAcquisitionError(
                f"[entra-imds] IMDS returned HTTP {response.status_code}. "
                f"Most common cause: no managed identity is assigned to this "
                f"resource, or the requested resource {self._resource!r} is "
                f"not permitted. Response body: {response.text}"
            )

        try:
            body = response.json()
        except ValueError as exc:
            raise TokenAcquisitionError(
                f"[entra-imds] IMDS returned a non-JSON body. Body preview: "
                f"{response.text[:200]!r}"
            ) from exc

        id_token = body.get("id_token")
        if not isinstance(id_token, str) or not id_token:
            raise TokenAcquisitionError(
                f"[entra-imds] IMDS response did not contain an 'id_token' "
                f"field. Most common cause: the managed identity returned "
                f"only an access_token; the federation flow needs the OIDC "
                f"id_token. Body keys: {list(body.keys())}"
            )
        return id_token


class EntraProjectedTokenProvider(FileTokenProvider):
    """Entra projected-token provider for AKS Workload Identity.

    Args:
        token_file: Path to the projected token. Defaults to the value
            of ``$AZURE_FEDERATED_TOKEN_FILE`` or, if that is unset,
            :data:`DEFAULT_AZURE_FEDERATED_TOKEN_FILE`.
        federation_rule_id: See :class:`IdentityProvider`.
        organization_id: See :class:`IdentityProvider`.
        service_account_id: See :class:`IdentityProvider`.
        workspace_id: See :class:`IdentityProvider`.
        exchange_timeout: See :class:`IdentityProvider`.
    """

    def __init__(
        self,
        *,
        token_file: str | Path | None = None,
        federation_rule_id: str,
        organization_id: str,
        service_account_id: str,
        workspace_id: str | None = None,
        exchange_timeout: float = 10.0,
    ) -> None:
        resolved_path: str | Path
        if token_file is not None:
            resolved_path = token_file
        else:
            resolved_path = os.environ.get(
                ENV_AZURE_FEDERATED_TOKEN_FILE,
                DEFAULT_AZURE_FEDERATED_TOKEN_FILE,
            )
        super().__init__(
            token_file=resolved_path,
            provider_label="entra-projected",
            federation_rule_id=federation_rule_id,
            organization_id=organization_id,
            service_account_id=service_account_id,
            workspace_id=workspace_id,
            exchange_timeout=exchange_timeout,
        )


def from_entra(
    *,
    mode: Literal["auto", "imds", "projected"] = "auto",
    client_id: str | None = None,
    token_file: str | Path | None = None,
    resource: str = _DEFAULT_RESOURCE,
    federation_rule_id: str | None = None,
    organization_id: str | None = None,
    service_account_id: str | None = None,
    workspace_id: str | None = None,
    exchange_timeout: float = 10.0,
) -> EntraManagedIdentityProvider | EntraProjectedTokenProvider:
    """Build a Microsoft Entra ID identity provider.

    Args:
        mode: ``"auto"`` (default) picks projected when
            ``AZURE_FEDERATED_TOKEN_FILE`` is set, otherwise IMDS.
            Force a mode with ``"imds"`` or ``"projected"``.
        client_id: Client ID of a user-assigned managed identity
            (IMDS mode only).
        token_file: Projected-token path (projected mode only).
            Defaults to ``$AZURE_FEDERATED_TOKEN_FILE``.
        resource: Resource/audience to request from IMDS (IMDS mode
            only). Defaults to ``https://api.anthropic.com``.
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
        An :class:`EntraManagedIdentityProvider` for IMDS mode or an
        :class:`EntraProjectedTokenProvider` for projected mode.

    Raises:
        ProviderConfigError: If ``mode`` is not one of ``auto``,
            ``imds``, or ``projected``, or if a required federation
            identifier is unset.
    """
    resolved_mode = mode
    if resolved_mode == "auto":
        resolved_mode = (
            "projected"
            if os.environ.get(ENV_AZURE_FEDERATED_TOKEN_FILE)
            else "imds"
        )

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

    if resolved_mode == "projected":
        return EntraProjectedTokenProvider(
            token_file=token_file,
            federation_rule_id=fed,
            organization_id=org,
            service_account_id=svc,
            workspace_id=ws,
            exchange_timeout=exchange_timeout,
        )
    if resolved_mode == "imds":
        return EntraManagedIdentityProvider(
            client_id=client_id,
            resource=resource,
            federation_rule_id=fed,
            organization_id=org,
            service_account_id=svc,
            workspace_id=ws,
            exchange_timeout=exchange_timeout,
        )
    raise ProviderConfigError(
        f"Unknown Entra mode {mode!r}. Valid modes are 'auto', 'imds', and "
        f"'projected'."
    )
