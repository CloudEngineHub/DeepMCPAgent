"""Tests for promptise.server rate limiting."""

from __future__ import annotations

import pytest

from promptise.mcp.server._context import RequestContext
from promptise.mcp.server._errors import RateLimitError
from promptise.mcp.server._rate_limit import (
    RateLimitMiddleware,
    TokenBucketLimiter,
)

# =====================================================================
# TokenBucketLimiter
# =====================================================================


class TestTokenBucketLimiter:
    def test_allows_within_burst(self):
        limiter = TokenBucketLimiter(rate_per_minute=60, burst=5)
        for _ in range(5):
            allowed, _ = limiter.consume("client-1")
            assert allowed is True

    def test_rejects_after_burst_exhausted(self):
        limiter = TokenBucketLimiter(rate_per_minute=60, burst=3)
        for _ in range(3):
            limiter.consume("client-1")

        allowed, retry_after = limiter.consume("client-1")
        assert allowed is False
        assert retry_after > 0

    def test_separate_keys_independent(self):
        limiter = TokenBucketLimiter(rate_per_minute=60, burst=2)
        limiter.consume("client-a")
        limiter.consume("client-a")
        # client-a exhausted
        allowed_a, _ = limiter.consume("client-a")
        assert allowed_a is False

        # client-b still has tokens
        allowed_b, _ = limiter.consume("client-b")
        assert allowed_b is True

    def test_retry_after_is_reasonable(self):
        limiter = TokenBucketLimiter(rate_per_minute=60, burst=1)
        limiter.consume("k")
        allowed, retry_after = limiter.consume("k")
        assert allowed is False
        # At 60/min = 1/sec, retry should be ~1 second
        assert 0 < retry_after <= 2.0

    def test_default_burst_equals_rate(self):
        limiter = TokenBucketLimiter(rate_per_minute=100)
        # Should allow 100 calls in burst
        for i in range(100):
            allowed, _ = limiter.consume("k")
            assert allowed is True, f"Failed at call {i}"

        allowed, _ = limiter.consume("k")
        assert allowed is False


# =====================================================================
# RateLimitMiddleware
# =====================================================================


class TestRateLimitMiddleware:
    async def test_allows_within_limit(self):
        mw = RateLimitMiddleware(rate_per_minute=60, burst=10)

        async def call_next(ctx):
            return "ok"

        ctx = RequestContext(server_name="test", tool_name="search")
        result = await mw(ctx, call_next)
        assert result == "ok"

    async def test_rejects_over_limit(self):
        mw = RateLimitMiddleware(rate_per_minute=60, burst=2)

        async def call_next(ctx):
            return "ok"

        ctx = RequestContext(server_name="test", tool_name="search")
        await mw(ctx, call_next)
        await mw(ctx, call_next)

        with pytest.raises(RateLimitError):
            await mw(ctx, call_next)

    async def test_per_tool_keying(self):
        mw = RateLimitMiddleware(rate_per_minute=60, burst=2, per_tool=True)

        async def call_next(ctx):
            return "ok"

        ctx1 = RequestContext(server_name="test", tool_name="search")
        ctx2 = RequestContext(server_name="test", tool_name="query")

        # Exhaust limit for "search"
        await mw(ctx1, call_next)
        await mw(ctx1, call_next)
        with pytest.raises(RateLimitError):
            await mw(ctx1, call_next)

        # "query" should still work
        result = await mw(ctx2, call_next)
        assert result == "ok"

    async def test_per_client_keying(self):
        mw = RateLimitMiddleware(rate_per_minute=60, burst=2)

        async def call_next(ctx):
            return "ok"

        ctx_a = RequestContext(server_name="test", tool_name="search")
        ctx_a.client_id = "client-a"
        ctx_b = RequestContext(server_name="test", tool_name="search")
        ctx_b.client_id = "client-b"

        # Exhaust limit for client-a
        await mw(ctx_a, call_next)
        await mw(ctx_a, call_next)
        with pytest.raises(RateLimitError):
            await mw(ctx_a, call_next)

        # client-b should still work
        result = await mw(ctx_b, call_next)
        assert result == "ok"

    async def test_custom_key_func(self):
        mw = RateLimitMiddleware(
            rate_per_minute=60,
            burst=1,
            key_func=lambda ctx: f"custom:{ctx.tool_name}",
        )

        async def call_next(ctx):
            return "ok"

        ctx = RequestContext(server_name="test", tool_name="search")
        await mw(ctx, call_next)

        with pytest.raises(RateLimitError):
            await mw(ctx, call_next)


# =====================================================================
# parse_rate_limit — declared-limit spec parsing
# =====================================================================


class TestParseRateLimit:
    def test_per_minute(self):
        from promptise.mcp.server import parse_rate_limit

        assert parse_rate_limit("100/min") == (100.0, 100)

    def test_per_second_normalises_to_per_minute(self):
        from promptise.mcp.server import parse_rate_limit

        assert parse_rate_limit("10/sec") == (600.0, 10)

    def test_per_hour_normalises_to_per_minute(self):
        from promptise.mcp.server import parse_rate_limit

        rate, burst = parse_rate_limit("120/hour")
        assert burst == 120
        assert rate == pytest.approx(2.0)

    def test_unit_aliases(self):
        from promptise.mcp.server import parse_rate_limit

        for unit in ("s", "sec", "second", "m", "min", "minute", "h", "hr", "hour"):
            rate, burst = parse_rate_limit(f"6/{unit}")
            assert burst == 6
            assert rate > 0

    def test_whitespace_and_case_tolerated(self):
        from promptise.mcp.server import parse_rate_limit

        assert parse_rate_limit(" 10 / MIN ") == (10.0, 10)

    def test_missing_slash_raises(self):
        from promptise.mcp.server import parse_rate_limit

        with pytest.raises(ValueError, match="expected"):
            parse_rate_limit("100")

    def test_non_integer_count_raises(self):
        from promptise.mcp.server import parse_rate_limit

        with pytest.raises(ValueError, match="integer"):
            parse_rate_limit("ten/min")

    def test_zero_and_negative_count_raise(self):
        from promptise.mcp.server import parse_rate_limit

        with pytest.raises(ValueError, match="positive"):
            parse_rate_limit("0/min")
        with pytest.raises(ValueError, match="positive"):
            parse_rate_limit("-5/min")

    def test_unknown_unit_raises(self):
        from promptise.mcp.server import parse_rate_limit

        with pytest.raises(ValueError, match="unknown unit"):
            parse_rate_limit("100/fortnight")


# =====================================================================
# Declared per-tool rate limits — @server.tool(rate_limit=...) enforced
# =====================================================================


class TestDeclaredRateLimitEnforcement:
    async def test_declared_limit_enforced_via_testclient(self):
        from promptise.mcp.server import MCPServer, TestClient

        server = MCPServer(name="rl-declared")

        @server.tool(rate_limit="2/min")
        async def ping() -> str:
            """Ping."""
            return "pong"

        client = TestClient(server)
        assert (await client.call_tool("ping", {}))[0].text == "pong"
        assert (await client.call_tool("ping", {}))[0].text == "pong"
        third = (await client.call_tool("ping", {}))[0].text
        assert "RATE_LIMIT_EXCEEDED" in third
        assert "retry_after_seconds" in third

    async def test_undeclared_tool_is_not_limited(self):
        from promptise.mcp.server import MCPServer, TestClient

        server = MCPServer(name="rl-mixed")

        @server.tool(rate_limit="1/min")
        async def limited() -> str:
            """Limited."""
            return "L"

        @server.tool()
        async def unlimited() -> str:
            """Unlimited."""
            return "U"

        client = TestClient(server)
        await client.call_tool("limited", {})  # consume the only token
        for _ in range(5):
            assert (await client.call_tool("unlimited", {}))[0].text == "U"

    async def test_buckets_are_per_tool(self):
        from promptise.mcp.server import MCPServer, TestClient

        server = MCPServer(name="rl-independent")

        @server.tool(rate_limit="1/min")
        async def alpha() -> str:
            """Alpha."""
            return "a"

        @server.tool(rate_limit="1/min")
        async def beta() -> str:
            """Beta."""
            return "b"

        client = TestClient(server)
        assert (await client.call_tool("alpha", {}))[0].text == "a"
        assert "RATE_LIMIT_EXCEEDED" in (await client.call_tool("alpha", {}))[0].text
        # beta has its own bucket — still allowed
        assert (await client.call_tool("beta", {}))[0].text == "b"

    def test_malformed_spec_fails_at_registration(self):
        from promptise.mcp.server import MCPServer

        server = MCPServer(name="rl-typo")

        with pytest.raises(ValueError, match="Invalid rate_limit"):

            @server.tool(rate_limit="100/mn")
            async def typo() -> str:
                """Typo'd limit."""
                return "x"

    def test_auto_inserted_once_on_live_build(self):
        """The live server path auto-inserts the enforcement middleware at
        build time (like PerToolConcurrencyLimiter), guarded against
        double-insert on rebuild."""
        from promptise.mcp.server import DeclaredRateLimitMiddleware, MCPServer

        server = MCPServer(name="rl-live")

        @server.tool(rate_limit="5/min")
        async def limited() -> str:
            """Limited."""
            return "x"

        server._build_lowlevel_server()
        count = sum(isinstance(m, DeclaredRateLimitMiddleware) for m in server._middlewares)
        assert count == 1

        server._build_lowlevel_server()  # rebuild must not duplicate
        count = sum(isinstance(m, DeclaredRateLimitMiddleware) for m in server._middlewares)
        assert count == 1

    def test_not_inserted_when_no_tool_declares(self):
        from promptise.mcp.server import DeclaredRateLimitMiddleware, MCPServer

        server = MCPServer(name="rl-none")

        @server.tool()
        async def plain() -> str:
            """Plain."""
            return "x"

        server._build_lowlevel_server()
        assert not any(isinstance(m, DeclaredRateLimitMiddleware) for m in server._middlewares)

    async def test_buckets_keyed_per_client_when_authenticated(self):
        """With client_id populated (as AuthMiddleware does before this
        middleware runs — first-added = outermost), each client gets an
        independent bucket; without it, callers share the global bucket."""
        from types import SimpleNamespace

        from promptise.mcp.server import DeclaredRateLimitMiddleware

        mw = DeclaredRateLimitMiddleware()
        tdef = SimpleNamespace(rate_limit="1/min")

        async def _next(ctx):
            return "ok"

        def _ctx(client_id: str | None) -> RequestContext:
            ctx = RequestContext(server_name="s", tool_name="t")
            ctx.state["tool_def"] = tdef
            ctx.client_id = client_id
            return ctx

        # client A consumes its only token; A's second call is limited
        assert await mw(_ctx("client-a"), _next) == "ok"
        with pytest.raises(RateLimitError):
            await mw(_ctx("client-a"), _next)

        # client B is unaffected — independent bucket
        assert await mw(_ctx("client-b"), _next) == "ok"

        # unauthenticated callers share one "global" bucket
        mw2 = DeclaredRateLimitMiddleware()
        assert await mw2(_ctx(None), _next) == "ok"
        with pytest.raises(RateLimitError):
            await mw2(_ctx(None), _next)
