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

This is the partial ``__init__`` shipped in Phase 1 of the build
plan: it exposes only the core abstractions plus the two concrete
provider bases. The provider factories (``from_entra``, ``from_aws``,
``from_gcp``, ``from_spiffe``, ``from_oidc``) and the
:class:`AgentIdentity` public class land in later phases.
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
from .providers.oidc import OidcCallableProvider, OidcFileProvider

_configure_default_handler()

__all__ = [
    "ADVISORY_REFRESH_BUFFER_SECONDS",
    "MANDATORY_REFRESH_BUFFER_SECONDS",
    "CallableTokenProvider",
    "CredentialPrecedenceError",
    "FileTokenProvider",
    "IdentityError",
    "IdentityProvider",
    "MintedToken",
    "OidcCallableProvider",
    "OidcFileProvider",
    "PlatformDetectionError",
    "ProviderConfigError",
    "TokenAcquisitionError",
    "TokenExchangeError",
]
