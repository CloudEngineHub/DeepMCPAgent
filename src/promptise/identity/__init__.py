"""``promptise.identity`` — Agent Identity subsystem.

Identity, tracing, and attribution for AI agents. Every agent gets a
stable, traceable identity — *who is acting* — so its tool calls, audit
entries, and outbound requests can all be attributed to it. An identity
can be **local** (just an ``agent_id``) or **verifiable** — backed by a
credential provider (Microsoft Entra, AWS IAM, Google Cloud,
SPIFFE/SPIRE, or a generic OIDC issuer) that mints a signed JWT the
agent presents to the resources it calls, such as MCP servers.

The user-facing surface is the single class :class:`AgentIdentity` plus
its ``from_*`` factories and :meth:`AgentIdentity.auto`. Lower-level
classes — :class:`IdentityProvider`, the provider bases, exception
types — are exported for advanced use and custom subclassing.
"""

from __future__ import annotations

from ._core.cache import (
    CREDENTIAL_REFRESH_BUFFER_SECONDS,
    CachedCredential,
    decode_jwt_claims,
    decode_jwt_expiry,
)
from ._core.callable_provider import CallableTokenProvider
from ._core.errors import (
    CredentialAcquisitionError,
    IdentityError,
    PlatformDetectionError,
    ProviderConfigError,
)
from ._core.file_provider import FileTokenProvider
from ._core.provider import IdentityProvider
from ._internal.logging import _configure_default_handler
from .agent_identity import AgentIdentity
from .providers.aws import AwsEksProjectedProvider, AwsStsProvider
from .providers.entra import (
    EntraManagedIdentityProvider,
    EntraProjectedTokenProvider,
)
from .providers.gcp import GcpMetadataProvider
from .providers.oidc import OidcCallableProvider, OidcFileProvider
from .providers.spiffe import SpiffeFileProvider, SpiffeSdkProvider

_configure_default_handler()

__all__ = [
    "CREDENTIAL_REFRESH_BUFFER_SECONDS",
    "AgentIdentity",
    "AwsEksProjectedProvider",
    "AwsStsProvider",
    "CachedCredential",
    "CallableTokenProvider",
    "CredentialAcquisitionError",
    "EntraManagedIdentityProvider",
    "EntraProjectedTokenProvider",
    "FileTokenProvider",
    "GcpMetadataProvider",
    "IdentityError",
    "IdentityProvider",
    "OidcCallableProvider",
    "OidcFileProvider",
    "PlatformDetectionError",
    "ProviderConfigError",
    "SpiffeFileProvider",
    "SpiffeSdkProvider",
    "decode_jwt_claims",
    "decode_jwt_expiry",
]
