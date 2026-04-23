"""Tests for promptise.mcp.server caching."""

from __future__ import annotations

import json

from promptise.mcp.server import MCPServer, TestClient
from promptise.mcp.server._cache import (
    CacheMiddleware,
    InMemoryCache,
    _make_cache_key,
    cached,
)

# =====================================================================
# InMemoryCache
# =====================================================================


class TestInMemoryCache:
    async def test_set_and_get(self):
        cache = InMemoryCache()
        await cache.set("k", "v", ttl=60)
        assert await cache.get("k") == "v"

    async def test_get_missing(self):
        cache = InMemoryCache()
        assert await cache.get("nonexistent") is None

    async def test_expiry(self):
        cache = InMemoryCache()
        # TTL and sleep must both exceed Windows ``time.time()`` ~15.6ms
        # resolution so ``now > expires_at`` evaluates correctly.
        await cache.set("k", "v", ttl=0.05)
        import asyncio

        await asyncio.sleep(0.15)
        assert await cache.get("k") is None

    async def test_delete(self):
        cache = InMemoryCache()
        await cache.set("k", "v", ttl=60)
        await cache.delete("k")
        assert await cache.get("k") is None

    async def test_delete_nonexistent(self):
        cache = InMemoryCache()
        await cache.delete("nonexistent")  # Should not raise

    async def test_clear(self):
        cache = InMemoryCache()
        await cache.set("a", 1, ttl=60)
        await cache.set("b", 2, ttl=60)
        await cache.clear()
        assert await cache.get("a") is None
        assert await cache.get("b") is None

    async def test_max_size_eviction(self):
        cache = InMemoryCache(max_size=2)
        await cache.set("a", 1, ttl=60)
        await cache.set("b", 2, ttl=60)
        await cache.set("c", 3, ttl=60)  # Should evict "a"
        assert await cache.get("a") is None
        assert await cache.get("b") == 2
        assert await cache.get("c") == 3

    async def test_size_property(self):
        cache = InMemoryCache()
        assert cache.size == 0
        await cache.set("a", 1, ttl=60)
        assert cache.size == 1

    async def test_stores_complex_types(self):
        cache = InMemoryCache()
        data = {"items": [1, 2, 3], "nested": {"key": "value"}}
        await cache.set("k", data, ttl=60)
        result = await cache.get("k")
        assert result == data

    async def test_overwrite_existing_key(self):
        cache = InMemoryCache()
        await cache.set("k", "old", ttl=60)
        await cache.set("k", "new", ttl=60)
        assert await cache.get("k") == "new"


# =====================================================================
# Cache key generation
# =====================================================================


class TestCacheKey:
    def test_deterministic(self):
        key1 = _make_cache_key("search", {"query": "hello"})
        key2 = _make_cache_key("search", {"query": "hello"})
        assert key1 == key2

    def test_different_args(self):
        key1 = _make_cache_key("search", {"query": "hello"})
        key2 = _make_cache_key("search", {"query": "world"})
        assert key1 != key2

    def test_different_func(self):
        key1 = _make_cache_key("search", {"query": "hello"})
        key2 = _make_cache_key("query", {"query": "hello"})
        assert key1 != key2

    def test_key_order_independent(self):
        key1 = _make_cache_key("f", {"a": 1, "b": 2})
        key2 = _make_cache_key("f", {"b": 2, "a": 1})
        assert key1 == key2


# =====================================================================
# @cached decorator
# =====================================================================


class TestCachedDecorator:
    async def test_caches_result(self):
        call_count = 0
        cache = InMemoryCache()

        @cached(ttl=60, backend=cache)
        async def expensive(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        result1 = await expensive(x=5)
        result2 = await expensive(x=5)
        assert result1 == 10
        assert result2 == 10
        assert call_count == 1  # Only called once

    async def test_different_args_not_cached(self):
        call_count = 0
        cache = InMemoryCache()

        @cached(ttl=60, backend=cache)
        async def expensive(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        await expensive(x=5)
        await expensive(x=10)
        assert call_count == 2

    async def test_custom_key_func(self):
        call_count = 0
        cache = InMemoryCache()

        @cached(
            ttl=60,
            backend=cache,
            key_func=lambda name, kw: f"custom:{kw.get('x')}",
        )
        async def expensive(x: int) -> int:
            nonlocal call_count
            call_count += 1
            return x * 2

        await expensive(x=5)
        await expensive(x=5)
        assert call_count == 1

    async def test_sync_handler(self):
        cache = InMemoryCache()

        @cached(ttl=60, backend=cache)
        def sync_func(x: int) -> int:
            return x + 1

        # Cached decorator wraps in async
        result = await sync_func(x=5)
        assert result == 6

    async def test_preserves_func_name(self):
        cache = InMemoryCache()

        @cached(ttl=60, backend=cache)
        async def my_tool(x: int) -> int:
            return x

        assert my_tool.__name__ == "my_tool"

    async def test_cache_attribute(self):
        cache = InMemoryCache()

        @cached(ttl=60, backend=cache)
        async def my_tool(x: int) -> int:
            return x

        assert my_tool.cache is cache


# =====================================================================
# Integration with MCPServer + TestClient
# =====================================================================


class TestCachedWithServer:
    async def test_cached_tool(self):
        server = MCPServer(name="test")
        call_count = 0
        cache = InMemoryCache()

        @server.tool()
        @cached(ttl=60, backend=cache)
        async def expensive(query: str) -> dict:
            nonlocal call_count
            call_count += 1
            return {"query": query, "count": call_count}

        client = TestClient(server)

        result1 = await client.call_tool("expensive", {"query": "hello"})
        result2 = await client.call_tool("expensive", {"query": "hello"})

        parsed1 = json.loads(result1[0].text)
        parsed2 = json.loads(result2[0].text)

        assert parsed1["count"] == 1
        assert parsed2["count"] == 1  # Cached
        assert call_count == 1

    async def test_different_args_not_cached_in_server(self):
        server = MCPServer(name="test")
        call_count = 0
        cache = InMemoryCache()

        @server.tool()
        @cached(ttl=60, backend=cache)
        async def expensive(query: str) -> dict:
            nonlocal call_count
            call_count += 1
            return {"query": query, "count": call_count}

        client = TestClient(server)

        await client.call_tool("expensive", {"query": "hello"})
        await client.call_tool("expensive", {"query": "world"})

        assert call_count == 2


# =====================================================================
# CacheMiddleware
# =====================================================================


class TestCacheMiddleware:
    async def test_caches_via_middleware(self):
        cache = InMemoryCache()
        mw = CacheMiddleware(cache, ttl=60)
        call_count = 0

        async def call_next(ctx):
            nonlocal call_count
            call_count += 1
            return f"result-{call_count}"

        from promptise.mcp.server._context import RequestContext

        ctx = RequestContext(server_name="test", tool_name="search")
        ctx.state["arguments"] = {"query": "hello"}

        result1 = await mw(ctx, call_next)
        result2 = await mw(ctx, call_next)

        assert result1 == "result-1"
        assert result2 == "result-1"  # Cached
        assert call_count == 1
