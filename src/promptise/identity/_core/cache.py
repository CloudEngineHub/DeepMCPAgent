"""Minted-token cache primitive.

This module owns two pieces of state:

* :class:`MintedToken` — the immutable record of one successful
  exchange. Every provider holds at most one instance in memory.
* The two refresh-window constants — advisory at expiry − 120 s,
  mandatory at expiry − 30 s. They are module-level so tests can
  monkey-patch them without rewriting the dataclass and so other
  modules can reuse the exact values rather than redefining them.

The token's expiry is anchored to :func:`time.monotonic`, not
:func:`time.time`. Wall-clock skew — NTP corrections, container
suspensions, virtualised time — must not affect a token's safety
window. This is an architectural rule from the build plan, not a
preference (section 4.5).
"""

from __future__ import annotations

import time
from dataclasses import dataclass

#: Number of seconds before expiry at which the framework attempts an
#: **advisory** refresh. If the refresh fails, the cached token
#: continues to be used and a warning is logged.
ADVISORY_REFRESH_BUFFER_SECONDS: int = 120

#: Number of seconds before expiry at which the framework performs a
#: **mandatory** refresh. If this refresh fails, an exception is raised
#: — the cached token is too close to expiry to be safe.
MANDATORY_REFRESH_BUFFER_SECONDS: int = 30


@dataclass(frozen=True, slots=True)
class MintedToken:
    """An access token minted by the Anthropic JWT-bearer endpoint.

    Immutable. Every successful call to the exchange endpoint produces
    a new :class:`MintedToken`; refresh logic replaces the cached
    instance rather than mutating it.

    Args:
        access_token: The bearer token returned by Anthropic. Always
            starts with ``sk-ant-oat01-``; the exchange engine
            validates this and refuses to construct a :class:`MintedToken`
            with any other prefix.
        token_type: OAuth token type, normally ``"Bearer"``. Preserved
            verbatim from the exchange response.
        expires_at_monotonic: The value of :func:`time.monotonic` at
            which this token expires. Computed at mint time as
            ``mint_monotonic + expires_in_seconds``.
        expires_in_seconds: The original ``expires_in`` value from the
            exchange response, retained for diagnostics and metrics.
    """

    access_token: str
    token_type: str
    expires_at_monotonic: float
    expires_in_seconds: int

    def time_until_expiry(self) -> float:
        """Seconds of remaining lifetime, using the monotonic clock."""
        return self.expires_at_monotonic - time.monotonic()

    def needs_advisory_refresh(self) -> bool:
        """``True`` when the token is inside the advisory window.

        Returns ``True`` once the remaining lifetime drops to or below
        :data:`ADVISORY_REFRESH_BUFFER_SECONDS`. Callers attempt a
        refresh; if it fails they continue using the cached token and
        log a warning.
        """
        return self.time_until_expiry() <= ADVISORY_REFRESH_BUFFER_SECONDS

    def needs_mandatory_refresh(self) -> bool:
        """``True`` when the token is inside the mandatory window.

        Returns ``True`` once the remaining lifetime drops to or below
        :data:`MANDATORY_REFRESH_BUFFER_SECONDS`. Callers refresh; if
        the refresh fails they raise — the cached token is too close
        to expiry to be safe.
        """
        return self.time_until_expiry() <= MANDATORY_REFRESH_BUFFER_SECONDS
