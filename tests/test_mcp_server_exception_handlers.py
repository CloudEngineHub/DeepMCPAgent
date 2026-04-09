"""Tests for promptise.mcp.server exception handlers."""

from __future__ import annotations

import json

from promptise.mcp.server import MCPServer, TestClient, ToolError
from promptise.mcp.server._context import RequestContext
from promptise.mcp.server._exception_handlers import ExceptionHandlerRegistry

# =====================================================================
# ExceptionHandlerRegistry
# =====================================================================


class DatabaseError(Exception):
    pass


class ConnectionError_(DatabaseError):
    """Subclass of DatabaseError (underscore to avoid shadowing builtin)."""

    pass


class TimeoutError_(DatabaseError):
    pass


class UnrelatedError(Exception):
    pass


class TestExceptionHandlerRegistry:
    def test_register_and_find(self):
        registry = ExceptionHandlerRegistry()

        async def handler(ctx, exc):
            return ToolError("DB error", code="DB_ERROR")

        registry.register(DatabaseError, handler)
        assert registry.find_handler(DatabaseError("fail")) is handler

    def test_mro_lookup(self):
        """Should find handler for parent class when child class is raised."""
        registry = ExceptionHandlerRegistry()

        async def handler(ctx, exc):
            return ToolError("DB error")

        registry.register(DatabaseError, handler)
        # ConnectionError_ inherits from DatabaseError
        found = registry.find_handler(ConnectionError_("fail"))
        assert found is handler

    def test_specific_handler_preferred(self):
        """Most specific handler (child class) takes priority."""
        registry = ExceptionHandlerRegistry()

        async def generic(ctx, exc):
            return ToolError("generic")

        async def specific(ctx, exc):
            return ToolError("specific")

        registry.register(DatabaseError, generic)
        registry.register(ConnectionError_, specific)

        assert registry.find_handler(ConnectionError_("fail")) is specific
        assert registry.find_handler(TimeoutError_("fail")) is generic

    def test_no_handler_returns_none(self):
        registry = ExceptionHandlerRegistry()
        assert registry.find_handler(UnrelatedError("fail")) is None

    async def test_handle_returns_mcp_error(self):
        registry = ExceptionHandlerRegistry()

        async def handler(ctx, exc):
            return ToolError(str(exc), code="DB_ERROR", retryable=True)

        registry.register(DatabaseError, handler)

        ctx = RequestContext(server_name="test", tool_name="query")
        result = await registry.handle(ctx, DatabaseError("connection refused"))
        assert result is not None
        assert result.code == "DB_ERROR"
        assert result.retryable is True

    async def test_handle_returns_none_if_no_match(self):
        registry = ExceptionHandlerRegistry()
        ctx = RequestContext(server_name="test", tool_name="query")
        result = await registry.handle(ctx, UnrelatedError("nope"))
        assert result is None

    async def test_handle_sync_handler(self):
        registry = ExceptionHandlerRegistry()

        def handler(ctx, exc):
            return ToolError(str(exc), code="SYNC_ERROR")

        registry.register(DatabaseError, handler)
        ctx = RequestContext(server_name="test", tool_name="query")
        result = await registry.handle(ctx, DatabaseError("fail"))
        assert result is not None
        assert result.code == "SYNC_ERROR"

    async def test_handle_returns_none_if_handler_returns_non_mcp_error(self):
        registry = ExceptionHandlerRegistry()

        async def handler(ctx, exc):
            return "not an MCPError"

        registry.register(DatabaseError, handler)
        ctx = RequestContext(server_name="test", tool_name="query")
        result = await registry.handle(ctx, DatabaseError("fail"))
        assert result is None

    def test_len(self):
        registry = ExceptionHandlerRegistry()
        assert len(registry) == 0

        async def handler(ctx, exc):
            return ToolError("err")

        registry.register(DatabaseError, handler)
        assert len(registry) == 1

    def test_contains(self):
        registry = ExceptionHandlerRegistry()

        async def handler(ctx, exc):
            return ToolError("err")

        registry.register(DatabaseError, handler)
        assert DatabaseError in registry
        assert UnrelatedError not in registry


# =====================================================================
# Integration with MCPServer + TestClient
# =====================================================================


class TestExceptionHandlerIntegration:
    async def test_decorator_registers_handler(self):
        server = MCPServer(name="test")

        @server.exception_handler(DatabaseError)
        async def handle_db(ctx, exc):
            return ToolError(str(exc), code="DB_ERROR", retryable=True)

        assert DatabaseError in server._exception_handlers

    async def test_custom_handler_maps_error(self):
        server = MCPServer(name="test")

        @server.exception_handler(DatabaseError)
        async def handle_db(ctx, exc):
            return ToolError(str(exc), code="DB_ERROR", retryable=True)

        @server.tool()
        async def query(sql: str) -> list:
            raise DatabaseError("connection refused")

        client = TestClient(server)
        result = await client.call_tool("query", {"sql": "SELECT 1"})
        parsed = json.loads(result[0].text)
        assert parsed["error"]["code"] == "DB_ERROR"
        assert parsed["error"]["retryable"] is True
        assert "connection refused" in parsed["error"]["message"]

    async def test_subclass_exception_handled(self):
        server = MCPServer(name="test")

        @server.exception_handler(DatabaseError)
        async def handle_db(ctx, exc):
            return ToolError(str(exc), code="DB_ERROR")

        @server.tool()
        async def query(sql: str) -> list:
            raise ConnectionError_("peer reset")

        client = TestClient(server)
        result = await client.call_tool("query", {"sql": "SELECT 1"})
        parsed = json.loads(result[0].text)
        assert parsed["error"]["code"] == "DB_ERROR"
        assert "peer reset" in parsed["error"]["message"]

    async def test_unhandled_exception_falls_through(self):
        server = MCPServer(name="test")

        @server.exception_handler(DatabaseError)
        async def handle_db(ctx, exc):
            return ToolError(str(exc), code="DB_ERROR")

        @server.tool()
        async def query(sql: str) -> list:
            raise UnrelatedError("something else")

        client = TestClient(server)
        result = await client.call_tool("query", {"sql": "SELECT 1"})
        parsed = json.loads(result[0].text)
        assert parsed["error"]["code"] == "INTERNAL_ERROR"

    async def test_mcp_error_bypasses_custom_handlers(self):
        """MCPError subclasses should NOT go through custom handlers."""
        server = MCPServer(name="test")
        handler_called = False

        @server.exception_handler(Exception)
        async def handle_all(ctx, exc):
            nonlocal handler_called
            handler_called = True
            return ToolError("generic")

        @server.tool()
        async def fail(x: int) -> int:
            raise ToolError("explicit error", code="EXPLICIT")

        client = TestClient(server)
        result = await client.call_tool("fail", {"x": 1})
        parsed = json.loads(result[0].text)
        # MCPError is caught before custom handlers
        assert parsed["error"]["code"] == "EXPLICIT"
        assert handler_called is False

    async def test_multiple_handlers(self):
        server = MCPServer(name="test")

        @server.exception_handler(DatabaseError)
        async def handle_db(ctx, exc):
            return ToolError("DB error", code="DB_ERROR")

        @server.exception_handler(ValueError)
        async def handle_val(ctx, exc):
            return ToolError("Bad value", code="BAD_VALUE")

        @server.tool()
        async def db_fail(x: int) -> int:
            raise DatabaseError("db fail")

        @server.tool()
        async def val_fail(x: int) -> int:
            raise ValueError("bad value")

        client = TestClient(server)

        result1 = await client.call_tool("db_fail", {"x": 1})
        assert json.loads(result1[0].text)["error"]["code"] == "DB_ERROR"

        result2 = await client.call_tool("val_fail", {"x": 1})
        assert json.loads(result2[0].text)["error"]["code"] == "BAD_VALUE"

    async def test_sync_exception_handler(self):
        server = MCPServer(name="test")

        @server.exception_handler(DatabaseError)
        def handle_db(ctx, exc):
            return ToolError(str(exc), code="SYNC_DB_ERROR")

        @server.tool()
        async def query(sql: str) -> list:
            raise DatabaseError("sync handler test")

        client = TestClient(server)
        result = await client.call_tool("query", {"sql": "SELECT 1"})
        parsed = json.loads(result[0].text)
        assert parsed["error"]["code"] == "SYNC_DB_ERROR"
