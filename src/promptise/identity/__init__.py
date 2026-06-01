"""``promptise.identity`` — Agent Identity subsystem.

The foundation layer for agentic identity. Federated authentication
for AI agents via Workload Identity Federation — zero static
credentials in agent code.

Day-one supported providers:

* Microsoft Entra ID (IMDS and projected-token modes)
* AWS IAM (STS and EKS-projected modes)
* Google Cloud (compute metadata server)
* SPIFFE / SPIRE (file and Workload API modes)
* Generic OIDC (file, callable, env-var)

The user-facing surface is the single class :class:`AgentIdentity`
plus its five ``from_*`` factories. Lower-level classes —
:class:`IdentityProvider`, :class:`MintedToken`, exception types —
are exported for advanced use, subclassing, and integration with
other parts of the framework.

The public surface is intentionally small: build an
:class:`AgentIdentity` with one of its ``from_*`` factories (or
:meth:`AgentIdentity.auto`), then call :meth:`~AgentIdentity.get_token`
or hand it to ``build_agent(identity=...)``. The lower-level provider
classes and exception types are exported for advanced use, custom
subclassing, and integration with the rest of the framework.
"""

from __future__ import annotations

from ._core.cache import (
    ADVISORY_REFRESH_BUFFER_SECONDS,
    MANDATORY_REFRESH_BUFFER_SECONDS,
    MintedToken,
)
from ._core.callable_provider import CallableTokenProvider
from ._core.errors import (
    CredentialPrecedenceError,
    IdentityError,
    PlatformDetectionError,
    ProviderConfigError,
    TokenAcquisitionError,
    TokenExchangeError,
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
    "ADVISORY_REFRESH_BUFFER_SECONDS",
    "MANDATORY_REFRESH_BUFFER_SECONDS",
    "AgentIdentity",
    "AwsEksProjectedProvider",
    "AwsStsProvider",
    "CallableTokenProvider",
    "CredentialPrecedenceError",
    "EntraManagedIdentityProvider",
    "EntraProjectedTokenProvider",
    "FileTokenProvider",
    "GcpMetadataProvider",
    "IdentityError",
    "IdentityProvider",
    "MintedToken",
    "OidcCallableProvider",
    "OidcFileProvider",
    "PlatformDetectionError",
    "ProviderConfigError",
    "SpiffeFileProvider",
    "SpiffeSdkProvider",
    "TokenAcquisitionError",
    "TokenExchangeError",
]
