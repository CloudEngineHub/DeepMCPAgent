"""Google Cloud identity provider.

A single provider that reads an OIDC identity token from the Google
Compute metadata server. Works on Compute Engine, GKE, Cloud Run,
Cloud Functions, and any other GCP runtime exposing the metadata
server at ``metadata.google.internal``.

The metadata identity endpoint returns the JWT as a **plain string**
in the response body — it is *not* a JSON wrapper (unlike Azure IMDS,
which returns JSON with an ``id_token`` field). The audience is
supplied as a query parameter. The response body must be used directly
and never parsed as JSON.

GCP needs no cloud SDK — the metadata server is a plain HTTP GET — so
there is no optional dependency for this provider.
"""

from __future__ import annotations

from typing import cast

import httpx

from .._core.callable_provider import CallableTokenProvider
from .._core.errors import TokenAcquisitionError
from .._internal.env import _resolve_anthropic_credentials

#: Template for the GCP metadata identity endpoint. The service-account
#: email segment selects which attached service account issues the token.
_METADATA_IDENTITY_URL_TEMPLATE: str = (
    "http://metadata.google.internal/computeMetadata/v1/instance/"
    "service-accounts/{service_account_email}/identity"
)

#: Default audience requested in the identity token.
_DEFAULT_AUDIENCE: str = "https://api.anthropic.com"

#: Default service account — the instance's primary attached account.
_DEFAULT_SERVICE_ACCOUNT_EMAIL: str = "default"


class GcpMetadataProvider(CallableTokenProvider):
    """GCP identity provider backed by the Compute metadata server.

    Args:
        audience: Audience to request in the identity token. Defaults to
            ``https://api.anthropic.com``.
        service_account_email: The attached service account whose
            identity to request. ``"default"`` (the default) selects the
            instance's primary service account; pass a full email to
            select a specific one.
        metadata_endpoint: Override for the metadata URL — primarily for
            tests. When ``None`` it is derived from
            ``service_account_email``.
        request_timeout: Seconds to wait for the metadata response.
            Defaults to five seconds; the metadata server is link-local.
        federation_rule_id: See :class:`IdentityProvider`. This is the
            Anthropic ``fdrl_*`` identifier — distinct from
            ``service_account_email``, which is the GCP service account.
        organization_id: See :class:`IdentityProvider`.
        service_account_id: See :class:`IdentityProvider`. The Anthropic
            ``svac_*`` identifier — again distinct from the GCP
            ``service_account_email``.
        workspace_id: See :class:`IdentityProvider`.
        exchange_timeout: See :class:`IdentityProvider`.
    """

    def __init__(
        self,
        *,
        audience: str = _DEFAULT_AUDIENCE,
        service_account_email: str = _DEFAULT_SERVICE_ACCOUNT_EMAIL,
        metadata_endpoint: str | None = None,
        request_timeout: float = 5.0,
        federation_rule_id: str,
        organization_id: str,
        service_account_id: str,
        workspace_id: str | None = None,
        exchange_timeout: float = 10.0,
    ) -> None:
        self._audience: str = audience
        self._service_account_email: str = service_account_email
        self._metadata_endpoint: str = (
            metadata_endpoint
            if metadata_endpoint is not None
            else _METADATA_IDENTITY_URL_TEMPLATE.format(
                service_account_email=service_account_email
            )
        )
        self._request_timeout: float = request_timeout
        super().__init__(
            token_fn=self._fetch_metadata_identity_token,
            provider_label="gcp-metadata",
            federation_rule_id=federation_rule_id,
            organization_id=organization_id,
            service_account_id=service_account_id,
            workspace_id=workspace_id,
            exchange_timeout=exchange_timeout,
        )

    def _fetch_metadata_identity_token(self) -> str:
        """Fetch the OIDC identity token from the GCP metadata server.

        The response body is the JWT itself — returned verbatim, never
        parsed as JSON. Raises a precise :class:`TokenAcquisitionError`
        on every failure mode; the base passes typed errors through.
        """
        try:
            response = httpx.get(
                self._metadata_endpoint,
                params={"audience": self._audience},
                headers={"Metadata-Flavor": "Google"},
                timeout=self._request_timeout,
            )
        except httpx.TimeoutException as exc:
            raise TokenAcquisitionError(
                f"[gcp-metadata] the Google Compute metadata server at "
                f"{self._metadata_endpoint} timed out after "
                f"{self._request_timeout}s. Most common cause: this workload "
                f"is not running on Google Cloud, or egress to "
                f"metadata.google.internal is blocked."
            ) from exc
        except httpx.HTTPError as exc:
            raise TokenAcquisitionError(
                f"[gcp-metadata] could not reach the Google Compute metadata "
                f"server at {self._metadata_endpoint} ({type(exc).__name__}). "
                f"Most common cause: this workload is not running on Google "
                f"Cloud compute. Underlying error: {exc}"
            ) from exc

        if response.status_code != 200:
            raise TokenAcquisitionError(
                f"[gcp-metadata] the metadata server returned HTTP "
                f"{response.status_code}. Most common cause: the service "
                f"account {self._service_account_email!r} is not attached to "
                f"this instance, or the audience {self._audience!r} is not "
                f"permitted. Response body: {response.text}"
            )

        # The identity endpoint returns the JWT directly as the body —
        # plain text, not JSON. Use it verbatim.
        token = response.text.strip()
        if not token:
            raise TokenAcquisitionError(
                "[gcp-metadata] the metadata server returned an empty body. "
                "Most common cause: a transient metadata-server issue or a "
                "malformed audience parameter."
            )
        return token


def from_gcp(
    *,
    audience: str = _DEFAULT_AUDIENCE,
    service_account_email: str = _DEFAULT_SERVICE_ACCOUNT_EMAIL,
    request_timeout: float = 5.0,
    federation_rule_id: str | None = None,
    organization_id: str | None = None,
    service_account_id: str | None = None,
    workspace_id: str | None = None,
    exchange_timeout: float = 10.0,
) -> GcpMetadataProvider:
    """Build a Google Cloud identity provider.

    Args:
        audience: Audience requested in the identity token. Defaults to
            ``https://api.anthropic.com``.
        service_account_email: The attached service account whose
            identity to request. Defaults to ``"default"``.
        request_timeout: Seconds to wait for the metadata response.
        federation_rule_id: The ``fdrl_*`` identifier. Falls back to
            ``ANTHROPIC_FEDERATION_RULE_ID``.
        organization_id: The organization UUID. Falls back to
            ``ANTHROPIC_ORGANIZATION_ID``.
        service_account_id: The Anthropic ``svac_*`` identifier. Falls
            back to ``ANTHROPIC_SERVICE_ACCOUNT_ID``. Distinct from
            ``service_account_email``.
        workspace_id: Optional ``wrkspc_*`` identifier. Falls back to
            ``ANTHROPIC_WORKSPACE_ID``.
        exchange_timeout: Seconds to wait for the Anthropic exchange.

    Returns:
        A :class:`GcpMetadataProvider`.

    Raises:
        ProviderConfigError: If a required federation identifier is unset.
    """
    creds = _resolve_anthropic_credentials(
        federation_rule_id=federation_rule_id,
        organization_id=organization_id,
        service_account_id=service_account_id,
        workspace_id=workspace_id,
    )
    return GcpMetadataProvider(
        audience=audience,
        service_account_email=service_account_email,
        request_timeout=request_timeout,
        federation_rule_id=cast(str, creds["federation_rule_id"]),
        organization_id=cast(str, creds["organization_id"]),
        service_account_id=cast(str, creds["service_account_id"]),
        workspace_id=creds["workspace_id"],
        exchange_timeout=exchange_timeout,
    )
