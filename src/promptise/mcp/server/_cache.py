"""Caching layer for MCP server tool results.

Provides an in-memory cache backend, a ``@cached`` decorator for tools,
and a ``CacheMiddleware`` for server-wide caching.

Example::

    from promptise.mcp.server import MCPServer
    from promptise.mcp.server._cache import InMemoryCache, cached

    cache = InMemoryCache()

    @server.tool()
    @cached(ttl=300, backend=cache)
    async def expensive_query(query: str) -> dict:
        return await db.slow_search(query)
"""

from __future__ import annotations

import asyncio
import functools
import hashlib
import json
import logging
import time
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

logger = logging.getLogger("promptise.server")


@runtime_checkable
class CacheBackend(Protocol):
    """Protocol for cache backends."""

    async def get(self, key: str) -> Any | None:
        """Get a cached value, or ``None`` if not found / expired."""
        ...

    async def set(self, key: str, value: Any, ttl: float) -> None:
        """Store a value with a TTL in seconds."""
        ...

    async def delete(self, key: str) -> None:
        """Delete a cached value."""
        ...

    async def clear(self) -> None:
        """Clear all cached values."""
        ...


class InMemoryCache:
    """In-process cache with TTL-based expiry and background cleanup.

    Thread-safe for asyncio (single-threaded event loop).

    Args:
        max_size: Maximum number of entries. Oldest entries are
            evicted when the limit is reached.  ``0`` means unlimited.
        cleanup_interval: Seconds between background sweeps of expired
            entries.  ``0`` disables background cleanup (expired entries
            are still removed on access).
    """

    def __init__(
        self,
        *,
        max_size: int = 0,
        cleanup_interval: float = 60.0,
    ) -> None:
        self._store: dict[str, tuple[Any, float]] = {}
        self._max_size = max_size
        self._cleanup_interval = cleanup_interval
        self._cleanup_task: asyncio.Task[None] | None = None
        self._evicted_count: int = 0

    async def get(self, key: str) -> Any | None:
        entry = self._store.get(key)
        if entry is None:
            return None
        value, expires_at = entry
        if time.monotonic() > expires_at:
            del self._store[key]
            return None
        return value

    async def set(self, key: str, value: Any, ttl: float) -> None:
        if self._max_size > 0 and len(self._store) >= self._max_size:
            # Evict oldest entry
            oldest_key = next(iter(self._store))
            del self._store[oldest_key]
        self._store[key] = (value, time.monotonic() + ttl)
        # Lazily start the background cleanup on first write
        self._ensure_cleanup_running()

    async def delete(self, key: str) -> None:
        self._store.pop(key, None)

    async def clear(self) -> None:
        self._store.clear()

    @property
    def size(self) -> int:
        """Current number of entries (including expired)."""
        return len(self._store)

    @property
    def evicted_count(self) -> int:
        """Total number of entries removed by background cleanup."""
        return self._evicted_count

    # ------------------------------------------------------------------
    # Background cleanup
    # ------------------------------------------------------------------

    def _ensure_cleanup_running(self) -> None:
        """Start the background sweep task if not already running."""
        if self._cleanup_interval <= 0:
            return
        if self._cleanup_task is not None and not self._cleanup_task.done():
            return
        try:
            loop = asyncio.get_running_loop()
            self._cleanup_task = loop.create_task(self._cleanup_loop())
        except RuntimeError:
            # No running loop — skip (e.g. sync tests).
            pass

    async def _cleanup_loop(self) -> None:
        """Periodically sweep expired entries from the store."""
        try:
            while True:
                await asyncio.sleep(self._cleanup_interval)
                self._sweep_expired()
        except asyncio.CancelledError:
            pass

    def _sweep_expired(self) -> int:
        """Remove all expired entries.  Returns number removed."""
        now = time.monotonic()
        expired_keys = [k for k, (_, expires_at) in self._store.items() if now > expires_at]
        for k in expired_keys:
            del self._store[k]
        if expired_keys:
            self._evicted_count += len(expired_keys)
            logger.debug("Cache cleanup: removed %d expired entries", len(expired_keys))
        return len(expired_keys)

    async def stop_cleanup(self) -> None:
        """Cancel the background cleanup task (for graceful shutdown)."""
        if self._cleanup_task is not None and not self._cleanup_task.done():
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None


# Default shared cache instance
_default_cache = InMemoryCache()


def _make_cache_key(func_name: str, kwargs: dict[str, Any]) -> str:
    """Build a deterministic cache key from function name + arguments."""
    serialised = json.dumps(kwargs, sort_keys=True, default=str)
    # MD5 is used purely as a non-cryptographic digest to produce short,
    # deterministic cache keys. Not used for security — collision resistance
    # is unnecessary here, just fast fingerprinting.
    key_hash = hashlib.md5(serialised.encode(), usedforsecurity=False).hexdigest()[:16]
    return f"cache:{func_name}:{key_hash}"


def cached(
    ttl: float = 60.0,
    *,
    key_func: Callable[..., str] | None = None,
    backend: CacheBackend | None = None,
) -> Callable[..., Any]:
    """Decorator that caches tool handler results.

    Args:
        ttl: Time-to-live in seconds.
        key_func: Custom key function ``(func_name, kwargs) -> str``.
            Defaults to hashing the function name + JSON-serialised kwargs.
        backend: Cache backend.  Defaults to the module-level
            ``InMemoryCache`` singleton.

    Example::

        @server.tool()
        @cached(ttl=300)
        async def expensive_query(query: str) -> dict:
            return await db.search(query)
    """
    cache = backend or _default_cache

    def decorator(func: Callable[..., Any]) -> Callable[..., Any]:
        @functools.wraps(func)
        async def wrapper(**kwargs: Any) -> Any:
            if key_func is not None:
                cache_key = key_func(func.__name__, kwargs)
            else:
                cache_key = _make_cache_key(func.__name__, kwargs)

            # Try cache
            cached_value = await cache.get(cache_key)
            if cached_value is not None:
                return cached_value

            # Call handler
            result = func(**kwargs)
            if asyncio.iscoroutine(result):
                result = await result

            # Store in cache
            await cache.set(cache_key, result, ttl)
            return result

        # Attach cache reference for testing
        wrapper.cache = cache  # type: ignore[attr-defined]
        return wrapper

    return decorator


class CacheMiddleware:
    """Server-wide caching middleware.

    Caches all tool results based on tool name + arguments.
    Opt-out individual tools by setting ``tdef.cache = False`` in state.

    Args:
        backend: Cache backend.
        ttl: Default TTL in seconds.
    """

    def __init__(
        self,
        backend: CacheBackend | None = None,
        *,
        ttl: float = 60.0,
    ) -> None:
        self.cache = backend or InMemoryCache()
        self.ttl = ttl

    async def __call__(
        self,
        ctx: Any,
        call_next: Callable[..., Any],
    ) -> Any:
        cache_key = f"mw:{ctx.tool_name}:{json.dumps(ctx.state.get('arguments', {}), sort_keys=True, default=str)}"
        # MD5 is used as a non-cryptographic fingerprint (short, fast, deterministic)
        # for the middleware cache key. Not a security boundary.
        key_hash = hashlib.md5(cache_key.encode(), usedforsecurity=False).hexdigest()[:16]
        final_key = f"mw:{ctx.tool_name}:{key_hash}"

        cached_value = await self.cache.get(final_key)
        if cached_value is not None:
            return cached_value

        result = await call_next(ctx)
        await self.cache.set(final_key, result, self.ttl)
        return result
