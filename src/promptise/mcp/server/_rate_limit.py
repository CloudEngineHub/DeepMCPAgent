"""Token bucket rate limiter for MCP server tools.

Example::

    from promptise.mcp.server import MCPServer, RateLimitMiddleware, TokenBucketLimiter

    limiter = TokenBucketLimiter(rate_per_minute=100, burst=20)
    server = MCPServer(name="api")
    server.add_middleware(RateLimitMiddleware(limiter))
"""

from __future__ import annotations

import threading
import time
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from ._context import RequestContext
from ._errors import RateLimitError


@runtime_checkable
class RateLimitStrategy(Protocol):
    """Protocol for rate limiting strategies."""

    def consume(self, key: str) -> tuple[bool, float]:
        """Try to consume a token.

        Returns:
            ``(allowed, retry_after_seconds)``
        """
        ...


class TokenBucketLimiter:
    """Token bucket rate limiter.

    Args:
        rate_per_minute: Sustained rate (tokens refilled per minute).
        burst: Maximum burst size (bucket capacity).
    """

    _MAX_BUCKETS = 10_000  # Evict stale entries beyond this count

    def __init__(self, rate_per_minute: int = 60, burst: int | None = None) -> None:
        self._rate = rate_per_minute / 60.0  # tokens per second
        self._burst = float(burst or rate_per_minute)
        self._buckets: dict[str, tuple[float, float]] = {}  # key → (tokens, last_time)
        self._lock = threading.Lock()

    def consume(self, key: str) -> tuple[bool, float]:
        """Try to consume one token from the bucket for *key*."""
        now = time.monotonic()
        with self._lock:
            tokens, last_time = self._buckets.get(key, (self._burst, now))

            # Refill tokens based on elapsed time
            elapsed = now - last_time
            tokens = min(self._burst, tokens + elapsed * self._rate)

            if tokens >= 1.0:
                self._buckets[key] = (tokens - 1.0, now)
                allowed, retry = True, 0.0
            else:
                deficit = 1.0 - tokens
                retry = deficit / self._rate if self._rate > 0 else 60.0
                self._buckets[key] = (tokens, now)
                allowed, retry = False, retry

            # Evict stale buckets periodically or when the map grows too large.
            # Remove entries not accessed in the last 5 minutes.
            _last_cleanup = getattr(self, "_last_cleanup", 0.0)
            if len(self._buckets) > self._MAX_BUCKETS or (now - _last_cleanup > 60.0 and len(self._buckets) > 100):
                stale_cutoff = now - 300.0
                stale_keys = [k for k, (_, lt) in self._buckets.items() if lt < stale_cutoff]
                for k in stale_keys:
                    del self._buckets[k]
                self._last_cleanup = now  # type: ignore[attr-defined]

            return allowed, retry


class RateLimitMiddleware:
    """Middleware that enforces rate limits.

    Args:
        limiter: A ``RateLimitStrategy`` implementation. If ``None``,
            a ``TokenBucketLimiter`` is created with the given params.
        per_tool: If ``True``, rate limit per tool name (in addition
            to per client).
        key_func: Custom function to extract the rate limit key from
            context. Overrides ``per_tool`` behaviour.
        rate_per_minute: Default rate if creating a new limiter.
        burst: Default burst if creating a new limiter.
    """

    def __init__(
        self,
        limiter: Any | None = None,
        *,
        per_tool: bool = False,
        key_func: Callable[[RequestContext], str] | None = None,
        rate_per_minute: int = 60,
        burst: int | None = None,
    ) -> None:
        self._limiter = limiter or TokenBucketLimiter(rate_per_minute, burst)
        self._per_tool = per_tool
        self._key_func = key_func

    def _get_key(self, ctx: RequestContext) -> str:
        if self._key_func:
            return self._key_func(ctx)
        parts: list[str] = []
        if ctx.client_id:
            parts.append(ctx.client_id)
        else:
            parts.append("global")
        if self._per_tool:
            parts.append(ctx.tool_name)
        return ":".join(parts)

    async def __call__(self, ctx: RequestContext, call_next: Callable[..., Any]) -> Any:
        key = self._get_key(ctx)
        allowed, retry_after = self._limiter.consume(key)
        if not allowed:
            raise RateLimitError(retry_after=retry_after)
        return await call_next(ctx)
