"""Tests for promptise.server middleware chain and built-in middleware."""

from __future__ import annotations

import asyncio

import pytest

from promptise.mcp.server._context import RequestContext
from promptise.mcp.server._middleware import (
    LoggingMiddleware,
    MiddlewareChain,
    TimeoutMiddleware,
)
from promptise.mcp.server._types import ToolDef

# =====================================================================
# MiddlewareChain
# =====================================================================


class TestMiddlewareChain:
    async def test_chain_runs_handler(self):
        chain = MiddlewareChain()

        async def handler(x: int) -> int:
            return x * 2

        ctx = RequestContext(server_name="test", tool_name="handler")
        result = await chain.run(ctx, handler, {"x": 5})
        assert result == 10

    async def test_chain_runs_middleware_in_order(self):
        order: list[str] = []

        async def mw1(ctx, call_next):
            order.append("mw1_before")
            result = await call_next(ctx)
            order.append("mw1_after")
            return result

        async def mw2(ctx, call_next):
            order.append("mw2_before")
            result = await call_next(ctx)
            order.append("mw2_after")
            return result

        chain = MiddlewareChain([mw1, mw2])

        async def handler(x: int) -> int:
            order.append("handler")
            return x

        ctx = RequestContext(server_name="test", tool_name="handler")
        result = await chain.run(ctx, handler, {"x": 42})

        assert result == 42
        assert order == ["mw1_before", "mw2_before", "handler", "mw2_after", "mw1_after"]

    async def test_chain_middleware_can_modify_result(self):
        async def double_result(ctx, call_next):
            result = await call_next(ctx)
            return result * 2

        chain = MiddlewareChain([double_result])

        async def handler(x: int) -> int:
            return x

        ctx = RequestContext(server_name="test", tool_name="handler")
        result = await chain.run(ctx, handler, {"x": 5})
        assert result == 10

    async def test_chain_middleware_can_short_circuit(self):
        async def blocker(ctx, call_next):
            return "blocked"

        chain = MiddlewareChain([blocker])

        async def handler(x: int) -> int:
            raise AssertionError("Should not be called")

        ctx = RequestContext(server_name="test", tool_name="handler")
        result = await chain.run(ctx, handler, {"x": 1})
        assert result == "blocked"

    async def test_chain_propagates_exceptions(self):
        async def passthrough(ctx, call_next):
            return await call_next(ctx)

        chain = MiddlewareChain([passthrough])

        async def handler(x: int) -> int:
            raise ValueError("boom")

        ctx = RequestContext(server_name="test", tool_name="handler")
        with pytest.raises(ValueError, match="boom"):
            await chain.run(ctx, handler, {"x": 1})

    async def test_chain_add_middleware(self):
        chain = MiddlewareChain()
        assert len(chain) == 0

        async def mw(ctx, call_next):
            return await call_next(ctx)

        chain.add(mw)
        assert len(chain) == 1

    async def test_chain_with_sync_handler(self):
        chain = MiddlewareChain()

        def handler(x: int) -> int:
            return x + 1

        ctx = RequestContext(server_name="test", tool_name="handler")
        result = await chain.run(ctx, handler, {"x": 9})
        assert result == 10


# =====================================================================
# LoggingMiddleware
# =====================================================================


class TestLoggingMiddleware:
    async def test_logging_middleware_passes_through(self):
        mw = LoggingMiddleware()

        async def handler(x: int) -> int:
            return x

        chain = MiddlewareChain([mw])
        ctx = RequestContext(server_name="test", tool_name="add")
        result = await chain.run(ctx, handler, {"x": 42})
        assert result == 42

    async def test_logging_middleware_on_error(self):
        mw = LoggingMiddleware()

        async def handler(x: int) -> int:
            raise RuntimeError("fail")

        chain = MiddlewareChain([mw])
        ctx = RequestContext(server_name="test", tool_name="add")
        with pytest.raises(RuntimeError, match="fail"):
            await chain.run(ctx, handler, {"x": 1})


# =====================================================================
# TimeoutMiddleware
# =====================================================================


class TestTimeoutMiddleware:
    async def test_timeout_passes_when_fast(self):
        mw = TimeoutMiddleware(default_timeout=5.0)

        async def handler(x: int) -> int:
            return x

        chain = MiddlewareChain([mw])
        ctx = RequestContext(server_name="test", tool_name="fast")
        result = await chain.run(ctx, handler, {"x": 1})
        assert result == 1

    async def test_timeout_raises_on_slow(self):
        mw = TimeoutMiddleware(default_timeout=0.05)

        async def handler(x: int) -> int:
            await asyncio.sleep(1.0)
            return x

        chain = MiddlewareChain([mw])
        ctx = RequestContext(server_name="test", tool_name="slow")

        from promptise.mcp.server._errors import ToolError

        with pytest.raises(ToolError, match="timed out"):
            await chain.run(ctx, handler, {"x": 1})

    async def test_timeout_uses_tool_def_timeout(self):
        mw = TimeoutMiddleware(default_timeout=10.0)

        async def handler(x: int) -> int:
            await asyncio.sleep(1.0)
            return x

        tdef = ToolDef(
            name="slow",
            description="",
            handler=handler,
            input_schema={},
            timeout=0.05,
        )
        chain = MiddlewareChain([mw])
        ctx = RequestContext(server_name="test", tool_name="slow")
        ctx.state["tool_def"] = tdef

        from promptise.mcp.server._errors import ToolError

        with pytest.raises(ToolError, match="timed out"):
            await chain.run(ctx, handler, {"x": 1})
