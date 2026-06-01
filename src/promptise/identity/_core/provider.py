""":class:`IdentityProvider` — caching, locking, and refresh logic.

Every concrete identity provider subclasses :class:`IdentityProvider`
and implements two members:

* the :attr:`provider_name` property — a short string used in log
  messages and error output;
* the :meth:`_acquire_upstream_jwt` method — fetches a fresh JWT from
  the cloud platform.

Everything else lives on the base class: the cached
:class:`MintedToken`, the :class:`threading.Lock` that serialises
concurrent callers, and the two-tier refresh logic defined in section
4.5 of the build plan.
"""

from __future__ import annotations

import threading
from abc import ABC, abstractmethod

from .._internal.logging import logger
from .cache import MintedToken
from .errors import TokenAcquisitionError, TokenExchangeError
from .exchange import exchange_jwt_for_anthropic_token


class IdentityProvider(ABC):
    """Abstract base for every federated identity provider.

    A single :class:`IdentityProvider` instance is safe to share
    across threads. Concurrent calls to :meth:`get_token` collapse
    into one upstream JWT acquisition and one Anthropic exchange,
    serialised by :attr:`_lock`. The cache is process-local — there
    is no shared cache between workers in a multi-process deployment.
    That trade-off is intentional (build plan section 4.4): sharing
    minted tokens across processes is a security problem the framework
    explicitly does not solve at this layer.

    Args:
        federation_rule_id: The ``fdrl_*`` identifier from the
            Anthropic Console.
        organization_id: The Anthropic organization UUID.
        service_account_id: The ``svac_*`` identifier of the workload's
            service account.
        workspace_id: Optional ``wrkspc_*`` identifier. When supplied,
            minted tokens are scoped to a single workspace.
        exchange_timeout: Seconds to wait for the Anthropic exchange.
            Default ten seconds.
    """

    def __init__(
        self,
        *,
        federation_rule_id: str,
        organization_id: str,
        service_account_id: str,
        workspace_id: str | None = None,
        exchange_timeout: float = 10.0,
    ) -> None:
        self.federation_rule_id: str = federation_rule_id
        self.organization_id: str = organization_id
        self.service_account_id: str = service_account_id
        self.workspace_id: str | None = workspace_id
        self.exchange_timeout: float = exchange_timeout
        self._cached: MintedToken | None = None
        self._lock: threading.Lock = threading.Lock()

    @property
    @abstractmethod
    def provider_name(self) -> str:
        """A short identifier used in log messages and error output."""

    @abstractmethod
    def _acquire_upstream_jwt(self) -> str:
        """Fetch a fresh upstream JWT from the cloud platform.

        Subclasses call the platform's metadata service, SDK, or read
        a projected token file. They return the JWT as a bare string;
        the Anthropic exchange happens in :meth:`_refresh` of this base
        class.

        Raises:
            TokenAcquisitionError: When the upstream IdP cannot supply
                a JWT. Subclass implementations wrap their underlying
                exception with ``raise … from exc`` so the original
                cause stays attached to ``__cause__``.
        """

    def get_token(self) -> str:
        """Return a currently-valid Anthropic access token.

        Behaviour:

        * If no token is cached, perform a mandatory refresh.
        * If the cached token is inside the mandatory window
          (≤ :data:`~promptise.identity.MANDATORY_REFRESH_BUFFER_SECONDS`
          to expiry), perform a mandatory refresh. Any failure
          propagates.
        * If the cached token is inside the advisory window
          (≤ :data:`~promptise.identity.ADVISORY_REFRESH_BUFFER_SECONDS`
          to expiry) but outside the mandatory window, attempt a
          refresh; on failure, log a warning and continue with the
          cached token.
        * Otherwise, return the cached token unchanged.

        Returns:
            A short-lived Anthropic access token (prefix
            ``sk-ant-oat01-``).

        Raises:
            TokenAcquisitionError: When the upstream IdP fails AND a
                mandatory refresh was required.
            TokenExchangeError: When Anthropic rejects the exchange
                AND a mandatory refresh was required.
        """
        with self._lock:
            token = self._cached
            if token is None or token.needs_mandatory_refresh():
                self._cached = self._refresh()
                return self._cached.access_token
            if token.needs_advisory_refresh():
                try:
                    self._cached = self._refresh()
                    return self._cached.access_token
                except (TokenAcquisitionError, TokenExchangeError) as exc:
                    logger.warning(
                        "advisory refresh failed provider=%s reason=%s; "
                        "continuing with cached token",
                        self.provider_name,
                        exc,
                    )
                    return token.access_token
            return token.access_token

    def get_auth_header(self) -> dict[str, str]:
        """Return a ready-to-use ``Authorization`` header value.

        Convenience for callers that need to attach a bearer token to
        a downstream HTTP request (an MCP tool calling a third-party
        API, an Anthropic SDK that accepts a custom header).

        Returns:
            A single-key dict ``{"Authorization": "Bearer <token>"}``.
        """
        return {"Authorization": f"Bearer {self.get_token()}"}

    def _refresh(self) -> MintedToken:
        """Acquire a fresh upstream JWT and exchange it for an access token."""
        jwt = self._acquire_upstream_jwt()
        return exchange_jwt_for_anthropic_token(
            jwt,
            federation_rule_id=self.federation_rule_id,
            organization_id=self.organization_id,
            service_account_id=self.service_account_id,
            workspace_id=self.workspace_id,
            timeout=self.exchange_timeout,
            provider_name=self.provider_name,
        )
