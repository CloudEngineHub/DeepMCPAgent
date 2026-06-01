"""The :class:`AgentIdentity` public class — the entire user surface.

Everything a developer touches goes through one class. Five
classmethod factories — one per supported IdP family — plus
:meth:`AgentIdentity.auto` for environment-based platform detection.
Each factory wraps the corresponding ``from_*`` function from the
:mod:`promptise.identity.providers` package and returns an
``AgentIdentity`` holding a single :class:`IdentityProvider`.

The class is a deliberately thin wrapper. It forwards
:meth:`get_token`, :meth:`get_auth_header`, :meth:`get_upstream_jwt`,
and the federation identifiers to the underlying provider. The wrapper
exists for forward compatibility: future cross-cutting features —
audit-log enrichment, token-claim inspection, metrics — attach to
``AgentIdentity`` without changing the provider classes or the public
import path.

Typical use is one line at the call site::

    from promptise.identity import AgentIdentity

    identity = AgentIdentity.from_aws()      # zero args, reads env
    token = identity.get_token()             # exchanged + cached

or, when the platform is not known ahead of time::

    identity = AgentIdentity.auto()          # detects the cloud
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Literal

from ._core.provider import IdentityProvider
from ._internal.detect import detect_platform
from .providers.aws import _DEFAULT_AUDIENCE as _AWS_DEFAULT_AUDIENCE
from .providers.aws import _DEFAULT_SIGNING_ALGORITHM as _AWS_DEFAULT_SIGNING_ALGORITHM
from .providers.aws import from_aws as _from_aws
from .providers.entra import _DEFAULT_RESOURCE as _ENTRA_DEFAULT_RESOURCE
from .providers.entra import from_entra as _from_entra
from .providers.gcp import _DEFAULT_AUDIENCE as _GCP_DEFAULT_AUDIENCE
from .providers.gcp import (
    _DEFAULT_SERVICE_ACCOUNT_EMAIL as _GCP_DEFAULT_SERVICE_ACCOUNT_EMAIL,
)
from .providers.gcp import from_gcp as _from_gcp
from .providers.oidc import from_oidc as _from_oidc
from .providers.spiffe import _DEFAULT_AUDIENCE as _SPIFFE_DEFAULT_AUDIENCE
from .providers.spiffe import from_spiffe as _from_spiffe

#: Default Anthropic exchange timeout in seconds, shared across every
#: factory. Mirrors :class:`IdentityProvider`'s own default.
_DEFAULT_EXCHANGE_TIMEOUT: float = 10.0

#: Default metadata-server timeout for GCP, in seconds.
_DEFAULT_GCP_REQUEST_TIMEOUT: float = 5.0


class AgentIdentity:
    """A federated workload identity for an AI agent.

    Holds one :class:`IdentityProvider` and forwards the token-minting
    surface to it. Construct one with a factory classmethod rather than
    calling ``__init__`` directly:

    * :meth:`from_entra` — Microsoft Entra ID (IMDS or projected token)
    * :meth:`from_aws` — AWS IAM (STS or EKS-projected token)
    * :meth:`from_gcp` — Google Cloud (compute metadata server)
    * :meth:`from_spiffe` — SPIFFE / SPIRE (Workload API or file)
    * :meth:`from_oidc` — any standards-compliant OIDC issuer
    * :meth:`auto` — detect the platform from the environment

    A single instance is thread-safe and caches the minted Anthropic
    token internally (see :class:`IdentityProvider`).

    Args:
        provider: The underlying identity provider. Prefer the factory
            classmethods; pass a provider directly only when wrapping a
            custom :class:`IdentityProvider` subclass.
    """

    def __init__(self, provider: IdentityProvider) -> None:
        self._provider: IdentityProvider = provider

    # -- Forwarded identity surface --------------------------------------

    @property
    def provider(self) -> IdentityProvider:
        """The wrapped :class:`IdentityProvider` (advanced use)."""
        return self._provider

    @property
    def provider_name(self) -> str:
        """The provider's short label, e.g. ``"aws-sts"`` (for logs)."""
        return self._provider.provider_name

    @property
    def federation_rule_id(self) -> str:
        """The ``fdrl_*`` federation rule identifier."""
        return self._provider.federation_rule_id

    @property
    def organization_id(self) -> str:
        """The Anthropic organization UUID."""
        return self._provider.organization_id

    @property
    def service_account_id(self) -> str:
        """The ``svac_*`` service-account identifier."""
        return self._provider.service_account_id

    @property
    def workspace_id(self) -> str | None:
        """The optional ``wrkspc_*`` workspace identifier."""
        return self._provider.workspace_id

    def get_token(self) -> str:
        """Return a currently-valid Anthropic access token.

        Delegates to :meth:`IdentityProvider.get_token`, which performs
        the two-tier refresh and caches the result.

        Returns:
            A short-lived Anthropic access token (``sk-ant-oat01-`` …).
        """
        return self._provider.get_token()

    def get_auth_header(self) -> dict[str, str]:
        """Return a ready-to-use ``Authorization`` bearer header.

        Returns:
            ``{"Authorization": "Bearer <token>"}``.
        """
        return self._provider.get_auth_header()

    def get_upstream_jwt(self) -> str:
        """Return a fresh upstream OIDC JWT from the cloud platform.

        This is the value the Anthropic SDK exchanges itself when an
        ``AgentIdentity`` is wired into ``build_agent()`` — the SDK
        calls this to obtain a JWT and runs its own exchange and
        refresh. It bypasses the framework's own token cache; callers
        that want the exchanged Anthropic token use :meth:`get_token`.

        Returns:
            A bare upstream JWT string.

        Raises:
            TokenAcquisitionError: When the platform cannot supply a JWT.
            ProviderConfigError: When a required optional SDK is missing.
        """
        return self._provider._acquire_upstream_jwt()

    def __repr__(self) -> str:
        """Return an identifier-only repr — never includes a token."""
        workspace = (
            f", workspace_id={self.workspace_id!r}" if self.workspace_id else ""
        )
        return (
            f"AgentIdentity(provider={self.provider_name!r}, "
            f"service_account_id={self.service_account_id!r}{workspace})"
        )

    # -- Factories -------------------------------------------------------

    @classmethod
    def from_entra(
        cls,
        *,
        mode: Literal["auto", "imds", "projected"] = "auto",
        client_id: str | None = None,
        token_file: str | Path | None = None,
        resource: str = _ENTRA_DEFAULT_RESOURCE,
        federation_rule_id: str | None = None,
        organization_id: str | None = None,
        service_account_id: str | None = None,
        workspace_id: str | None = None,
        exchange_timeout: float = _DEFAULT_EXCHANGE_TIMEOUT,
    ) -> AgentIdentity:
        """Build a Microsoft Entra ID identity.

        Args:
            mode: ``"auto"`` (default) picks projected-token mode when
                ``$AZURE_FEDERATED_TOKEN_FILE`` is set, otherwise IMDS.
                Force a mode with ``"imds"`` or ``"projected"``.
            client_id: Managed-identity client id (IMDS mode). Falls
                back to ``$AZURE_CLIENT_ID``.
            token_file: Projected token path. Falls back to
                ``$AZURE_FEDERATED_TOKEN_FILE``.
            resource: Resource/audience requested from IMDS.
            federation_rule_id: ``fdrl_*``; falls back to
                ``$ANTHROPIC_FEDERATION_RULE_ID``.
            organization_id: Org UUID; falls back to
                ``$ANTHROPIC_ORGANIZATION_ID``.
            service_account_id: ``svac_*``; falls back to
                ``$ANTHROPIC_SERVICE_ACCOUNT_ID``.
            workspace_id: Optional ``wrkspc_*``; falls back to
                ``$ANTHROPIC_WORKSPACE_ID``.
            exchange_timeout: Seconds to wait for the Anthropic exchange.

        Returns:
            An ``AgentIdentity`` wrapping the selected Entra provider.
        """
        return cls(
            _from_entra(
                mode=mode,
                client_id=client_id,
                token_file=token_file,
                resource=resource,
                federation_rule_id=federation_rule_id,
                organization_id=organization_id,
                service_account_id=service_account_id,
                workspace_id=workspace_id,
                exchange_timeout=exchange_timeout,
            )
        )

    @classmethod
    def from_aws(
        cls,
        *,
        mode: Literal["auto", "sts", "projected"] = "auto",
        region: str | None = None,
        token_file: str | Path | None = None,
        audience: str = _AWS_DEFAULT_AUDIENCE,
        signing_algorithm: str = _AWS_DEFAULT_SIGNING_ALGORITHM,
        federation_rule_id: str | None = None,
        organization_id: str | None = None,
        service_account_id: str | None = None,
        workspace_id: str | None = None,
        exchange_timeout: float = _DEFAULT_EXCHANGE_TIMEOUT,
    ) -> AgentIdentity:
        """Build an AWS IAM identity.

        Args:
            mode: ``"auto"`` (default) picks EKS-projected mode when
                ``$ANTHROPIC_IDENTITY_TOKEN_FILE`` is set, otherwise STS.
                Force a mode with ``"sts"`` or ``"projected"``.
            region: AWS region for STS. Falls back to ``$AWS_REGION``
                then ``$AWS_DEFAULT_REGION``. Required for STS mode.
            token_file: EKS-projected token path. Falls back to
                ``$ANTHROPIC_IDENTITY_TOKEN_FILE`` then the default mount.
            audience: Audience requested in the web-identity token.
            signing_algorithm: STS signing algorithm (e.g. ``"RS256"``).
            federation_rule_id: ``fdrl_*``; env fallback as above.
            organization_id: Org UUID; env fallback as above.
            service_account_id: ``svac_*``; env fallback as above.
            workspace_id: Optional ``wrkspc_*``; env fallback as above.
            exchange_timeout: Seconds to wait for the Anthropic exchange.

        Returns:
            An ``AgentIdentity`` wrapping the selected AWS provider.
        """
        return cls(
            _from_aws(
                mode=mode,
                region=region,
                token_file=token_file,
                audience=audience,
                signing_algorithm=signing_algorithm,
                federation_rule_id=federation_rule_id,
                organization_id=organization_id,
                service_account_id=service_account_id,
                workspace_id=workspace_id,
                exchange_timeout=exchange_timeout,
            )
        )

    @classmethod
    def from_gcp(
        cls,
        *,
        audience: str = _GCP_DEFAULT_AUDIENCE,
        service_account_email: str = _GCP_DEFAULT_SERVICE_ACCOUNT_EMAIL,
        request_timeout: float = _DEFAULT_GCP_REQUEST_TIMEOUT,
        federation_rule_id: str | None = None,
        organization_id: str | None = None,
        service_account_id: str | None = None,
        workspace_id: str | None = None,
        exchange_timeout: float = _DEFAULT_EXCHANGE_TIMEOUT,
    ) -> AgentIdentity:
        """Build a Google Cloud identity.

        Args:
            audience: Audience requested in the identity token.
            service_account_email: Attached service account whose
                identity to request. ``"default"`` selects the
                instance's primary account.
            request_timeout: Seconds to wait for the metadata response.
            federation_rule_id: ``fdrl_*``; env fallback as above.
            organization_id: Org UUID; env fallback as above.
            service_account_id: Anthropic ``svac_*`` (distinct from the
                GCP ``service_account_email``); env fallback as above.
            workspace_id: Optional ``wrkspc_*``; env fallback as above.
            exchange_timeout: Seconds to wait for the Anthropic exchange.

        Returns:
            An ``AgentIdentity`` wrapping the GCP metadata provider.
        """
        return cls(
            _from_gcp(
                audience=audience,
                service_account_email=service_account_email,
                request_timeout=request_timeout,
                federation_rule_id=federation_rule_id,
                organization_id=organization_id,
                service_account_id=service_account_id,
                workspace_id=workspace_id,
                exchange_timeout=exchange_timeout,
            )
        )

    @classmethod
    def from_spiffe(
        cls,
        *,
        mode: Literal["auto", "file", "sdk"] = "auto",
        token_file: str | Path | None = None,
        socket_path: str | None = None,
        audience: str = _SPIFFE_DEFAULT_AUDIENCE,
        federation_rule_id: str | None = None,
        organization_id: str | None = None,
        service_account_id: str | None = None,
        workspace_id: str | None = None,
        exchange_timeout: float = _DEFAULT_EXCHANGE_TIMEOUT,
    ) -> AgentIdentity:
        """Build a SPIFFE / SPIRE identity.

        Args:
            mode: ``"auto"`` (default) picks file mode when
                ``token_file`` is supplied, otherwise SDK mode. Force a
                mode with ``"file"`` or ``"sdk"``.
            token_file: JWT-SVID path written by ``spiffe-helper`` (file
                mode). Required when ``mode="file"``.
            socket_path: SPIRE Workload API socket (SDK mode). Falls back
                to ``$SPIFFE_ENDPOINT_SOCKET`` then the default socket.
            audience: Audience requested in the JWT-SVID (SDK mode).
            federation_rule_id: ``fdrl_*``; env fallback as above.
            organization_id: Org UUID; env fallback as above.
            service_account_id: ``svac_*``; env fallback as above.
            workspace_id: Optional ``wrkspc_*``; env fallback as above.
            exchange_timeout: Seconds to wait for the Anthropic exchange.

        Returns:
            An ``AgentIdentity`` wrapping the selected SPIFFE provider.
        """
        return cls(
            _from_spiffe(
                mode=mode,
                token_file=token_file,
                socket_path=socket_path,
                audience=audience,
                federation_rule_id=federation_rule_id,
                organization_id=organization_id,
                service_account_id=service_account_id,
                workspace_id=workspace_id,
                exchange_timeout=exchange_timeout,
            )
        )

    @classmethod
    def from_oidc(
        cls,
        issuer: str,
        *,
        token_file: str | Path | None = None,
        token_fn: Callable[[], str] | None = None,
        token_env_var: str | None = None,
        federation_rule_id: str | None = None,
        organization_id: str | None = None,
        service_account_id: str | None = None,
        workspace_id: str | None = None,
        exchange_timeout: float = _DEFAULT_EXCHANGE_TIMEOUT,
    ) -> AgentIdentity:
        """Build a generic OIDC identity.

        Exactly one of ``token_file``, ``token_fn``, or
        ``token_env_var`` must be supplied — they are the three ways to
        obtain the issuer's JWT.

        Args:
            issuer: The OIDC issuer URL (recorded for observability).
            token_file: Path to a file holding the JWT.
            token_fn: Zero-arg callable returning the JWT.
            token_env_var: Name of an env var holding the JWT (re-read
                on every refresh).
            federation_rule_id: ``fdrl_*``; env fallback as above.
            organization_id: Org UUID; env fallback as above.
            service_account_id: ``svac_*``; env fallback as above.
            workspace_id: Optional ``wrkspc_*``; env fallback as above.
            exchange_timeout: Seconds to wait for the Anthropic exchange.

        Returns:
            An ``AgentIdentity`` wrapping the selected OIDC provider.
        """
        return cls(
            _from_oidc(
                issuer,
                token_file=token_file,
                token_fn=token_fn,
                token_env_var=token_env_var,
                federation_rule_id=federation_rule_id,
                organization_id=organization_id,
                service_account_id=service_account_id,
                workspace_id=workspace_id,
                exchange_timeout=exchange_timeout,
            )
        )

    @classmethod
    def auto(
        cls,
        *,
        federation_rule_id: str | None = None,
        organization_id: str | None = None,
        service_account_id: str | None = None,
        workspace_id: str | None = None,
        exchange_timeout: float = _DEFAULT_EXCHANGE_TIMEOUT,
    ) -> AgentIdentity:
        """Detect the platform from the environment and build an identity.

        Uses :func:`promptise.identity._internal.detect.detect_platform`
        to inspect platform environment markers, then dispatches to the
        matching factory with platform defaults (auto sub-mode, default
        audience, SDK mode for SPIFFE). Only the federation identifiers
        and the exchange timeout are forwarded; per-platform tunables
        keep their defaults.

        Args:
            federation_rule_id: ``fdrl_*``; env fallback as above.
            organization_id: Org UUID; env fallback as above.
            service_account_id: ``svac_*``; env fallback as above.
            workspace_id: Optional ``wrkspc_*``; env fallback as above.
            exchange_timeout: Seconds to wait for the Anthropic exchange.

        Returns:
            An ``AgentIdentity`` for the detected platform.

        Raises:
            PlatformDetectionError: When no platform marker is present.
        """
        platform = detect_platform()
        if platform == "entra":
            return cls.from_entra(
                federation_rule_id=federation_rule_id,
                organization_id=organization_id,
                service_account_id=service_account_id,
                workspace_id=workspace_id,
                exchange_timeout=exchange_timeout,
            )
        if platform == "aws":
            return cls.from_aws(
                federation_rule_id=federation_rule_id,
                organization_id=organization_id,
                service_account_id=service_account_id,
                workspace_id=workspace_id,
                exchange_timeout=exchange_timeout,
            )
        if platform == "gcp":
            return cls.from_gcp(
                federation_rule_id=federation_rule_id,
                organization_id=organization_id,
                service_account_id=service_account_id,
                workspace_id=workspace_id,
                exchange_timeout=exchange_timeout,
            )
        # The only remaining identifier from detect_platform() is "spiffe";
        # detect_platform raises rather than return anything else.
        return cls.from_spiffe(
            federation_rule_id=federation_rule_id,
            organization_id=organization_id,
            service_account_id=service_account_id,
            workspace_id=workspace_id,
            exchange_timeout=exchange_timeout,
        )
