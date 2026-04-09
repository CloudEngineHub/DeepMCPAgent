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
