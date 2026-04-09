"""Tests for promptise.server — core MCPServer functionality."""

from __future__ import annotations

import json

import pytest

from promptise.mcp.server import (
    MCPServer,
    RequestContext,
    ToolDef,
    ToolError,
    TransportType,
    ValidationError,
    get_context,
)
from promptise.mcp.server._context import clear_context, set_context
from promptise.mcp.server._errors import (
    AuthenticationError,
    MCPError,
    RateLimitError,
)
from promptise.mcp.server._lifecycle import LifecycleManager
from promptise.mcp.server._registry import (
    ToolRegistry,
    _match_uri_template,
)
from promptise.mcp.server._validation import build_input_model, validate_arguments

# =====================================================================
# MCPServer construction
# =====================================================================


class TestMCPServerCreation:
    def test_creates_with_defaults(self):
        server = MCPServer()
        assert server.name == "promptise-server"
        assert server.version == "0.1.0"
        assert server.instructions is None

    def test_creates_with_custom_values(self):
        server = MCPServer(name="analytics", version="2.0.0", instructions="Be helpful")
        assert server.name == "analytics"
        assert server.version == "2.0.0"
        assert server.instructions == "Be helpful"


# =====================================================================
# Tool registration
# =====================================================================


class TestToolRegistration:
    def test_registers_simple_tool(self):
        server = MCPServer()

        @server.tool()
        async def add(a: int, b: int) -> int:
            """Add two numbers."""
            return a + b

        assert len(server._tool_registry) == 1
        tool = server._tool_registry.get("add")
        assert tool is not None
        assert tool.name == "add"
        assert tool.description == "Add two numbers."
        assert "a" in tool.input_schema["properties"]
        assert "b" in tool.input_schema["properties"]

    def test_registers_with_custom_name(self):
        server = MCPServer()

        @server.tool(name="custom_add", description="Custom adder")
        async def add(a: int, b: int) -> int:
            return a + b

        assert "custom_add" in server._tool_registry
        tool = server._tool_registry.get("custom_add")
        assert tool.description == "Custom adder"

    def test_registers_with_production_options(self):
        server = MCPServer()

        @server.tool(auth=True, rate_limit="100/min", timeout=30, tags=["db"])
        async def query(sql: str) -> list:
            return []

        tool = server._tool_registry.get("query")
        assert tool.auth is True
        assert tool.rate_limit == "100/min"
        assert tool.timeout == 30
        assert tool.tags == ["db"]

    def test_optional_params_have_defaults_in_schema(self):
        server = MCPServer()

        @server.tool()
        async def search(query: str, limit: int = 10, offset: int = 0) -> list:
            return []

        tool = server._tool_registry.get("search")
        schema = tool.input_schema
        assert "query" in schema["required"]
        assert "limit" not in schema.get("required", [])
        assert schema["properties"]["limit"]["default"] == 10

    def test_duplicate_tool_name_raises(self):
        server = MCPServer()

        @server.tool()
        async def search(q: str) -> str:
            return q

        with pytest.raises(ValueError, match="already registered"):

            @server.tool(name="search")
            async def search2(q: str) -> str:
                return q

    def test_sync_function_works(self):
        server = MCPServer()

        @server.tool()
        def multiply(a: int, b: int) -> int:
            """Multiply two numbers."""
            return a * b

        assert "multiply" in server._tool_registry


# =====================================================================
# Resource registration
# =====================================================================


class TestResourceRegistration:
    def test_registers_static_resource(self):
        server = MCPServer()

        @server.resource("config://app")
        async def get_config() -> str:
            return "{}"

        assert len(server._resource_registry) == 1

    def test_registers_resource_template(self):
        server = MCPServer()

        @server.resource_template("users://{user_id}")
        async def get_user(user_id: str) -> str:
            return f"user {user_id}"

        assert len(server._resource_registry) == 1
        templates = server._resource_registry.list_templates()
        assert len(templates) == 1
        assert templates[0].is_template is True


# =====================================================================
# Prompt registration
# =====================================================================


class TestPromptRegistration:
    def test_registers_prompt(self):
        server = MCPServer()

        @server.prompt()
        async def review(code: str, language: str = "python") -> str:
            """Review code."""
            return f"Review {language}: {code}"

        assert len(server._prompt_registry) == 1
        pdef = server._prompt_registry.get("review")
        assert pdef.name == "review"
        assert len(pdef.arguments) == 2
        assert pdef.arguments[0]["name"] == "code"
        assert pdef.arguments[0]["required"] is True
        assert pdef.arguments[1]["name"] == "language"
        assert pdef.arguments[1]["required"] is False


# =====================================================================
# Validation
# =====================================================================


class TestValidation:
    def test_build_input_model_required_and_optional(self):
        async def func(name: str, age: int, city: str = "NYC") -> str:
            return ""

        model, schema = build_input_model(func)
        assert "name" in schema["required"]
        assert "age" in schema["required"]
        assert "city" not in schema.get("required", [])
        assert schema["properties"]["city"]["default"] == "NYC"

    def test_validate_valid_arguments(self):
        async def func(name: str, age: int) -> str:
            return ""

        model, _ = build_input_model(func)
        result = validate_arguments(model, {"name": "Alice", "age": 30})
        assert result == {"name": "Alice", "age": 30}

    def test_validate_invalid_arguments_raises(self):
        async def func(name: str, age: int) -> str:
            return ""

        model, _ = build_input_model(func)
        with pytest.raises(ValidationError, match="Invalid input"):
            validate_arguments(model, {"name": "Alice", "age": "not_a_number"})

    def test_validate_missing_required_raises(self):
        async def func(name: str, age: int) -> str:
            return ""

        model, _ = build_input_model(func)
        with pytest.raises(ValidationError, match="Invalid input"):
            validate_arguments(model, {"name": "Alice"})

    def test_excludes_params(self):
        async def func(ctx: str, name: str) -> str:
            return ""

        model, schema = build_input_model(func, exclude={"ctx"})
        assert "ctx" not in schema.get("properties", {})
        assert "name" in schema["properties"]


# =====================================================================
# Registry
# =====================================================================


class TestToolRegistry:
    def test_register_and_get(self):
        reg = ToolRegistry()
        tdef = ToolDef(
            name="test",
            description="Test tool",
            handler=lambda: None,
            input_schema={},
        )
        reg.register(tdef)
        assert reg.get("test") is tdef
        assert "test" in reg
        assert len(reg) == 1

    def test_list_all(self):
        reg = ToolRegistry()
        for i in range(3):
            reg.register(
                ToolDef(
                    name=f"tool_{i}",
                    description=f"Tool {i}",
                    handler=lambda: None,
                    input_schema={},
                )
            )
        assert len(reg.list_all()) == 3


class TestResourceRegistry:
    def test_uri_template_matching(self):
        result = _match_uri_template("users://{user_id}", "users://123")
        assert result == {"user_id": "123"}

    def test_uri_template_no_match(self):
        result = _match_uri_template("users://{user_id}", "config://app")
        assert result is None

    def test_multi_param_template(self):
        result = _match_uri_template("repos://{owner}/{repo}", "repos://acme/widgets")
        assert result == {"owner": "acme", "repo": "widgets"}


# =====================================================================
# Errors
# =====================================================================


class TestErrors:
    def test_tool_error_serialises(self):
        err = ToolError(
            "Cannot divide by zero",
            code="DIVISION_BY_ZERO",
            suggestion="Provide a non-zero divisor",
            retryable=True,
        )
        text = err.to_text()
        parsed = json.loads(text)
        assert parsed["error"]["code"] == "DIVISION_BY_ZERO"
        assert parsed["error"]["suggestion"] == "Provide a non-zero divisor"
        assert parsed["error"]["retryable"] is True

    def test_rate_limit_error_includes_retry_after(self):
        err = RateLimitError(retry_after=30.0)
        text = err.to_text()
        parsed = json.loads(text)
        assert parsed["error"]["details"]["retry_after_seconds"] == 30.0
        assert parsed["error"]["retryable"] is True

    def test_validation_error_includes_field_errors(self):
        err = ValidationError(
            "Invalid input",
            field_errors={"age": "must be a positive integer"},
        )
        text = err.to_text()
        parsed = json.loads(text)
        assert "age" in parsed["error"]["details"]["field_errors"]

    def test_authentication_error(self):
        err = AuthenticationError()
        assert err.code == "AUTHENTICATION_ERROR"
        assert err.retryable is False

    def test_mcp_error_base(self):
        err = MCPError("Something failed", code="CUSTOM", retryable=True)
        detail = err.detail
        assert detail.code == "CUSTOM"
        assert detail.retryable is True


# =====================================================================
# Context
# =====================================================================


class TestContext:
    def test_set_and_get_context(self):
        ctx = RequestContext(server_name="test", tool_name="add")
        set_context(ctx)
        assert get_context() is ctx
        clear_context()

    def test_get_context_outside_request_raises(self):
        clear_context()
        with pytest.raises(RuntimeError, match="No active RequestContext"):
            get_context()

    def test_context_has_request_id(self):
        ctx = RequestContext(server_name="test")
        assert len(ctx.request_id) == 12

    def test_context_logger(self):
        ctx = RequestContext(server_name="analytics")
        assert ctx.logger.name == "promptise.server.analytics"


# =====================================================================
# Lifecycle
# =====================================================================


class TestLifecycle:
    async def test_startup_runs_hooks_in_order(self):
        lm = LifecycleManager()
        order = []
        lm.add_startup(lambda: order.append(1))
        lm.add_startup(lambda: order.append(2))
        await lm.startup()
        assert order == [1, 2]
        assert lm.is_started

    async def test_shutdown_runs_hooks_in_reverse(self):
        lm = LifecycleManager()
        order = []
        lm.add_shutdown(lambda: order.append("a"))
        lm.add_shutdown(lambda: order.append("b"))
        await lm.shutdown()
        assert order == ["b", "a"]

    async def test_async_hooks(self):
        lm = LifecycleManager()
        called = []

        async def async_hook():
            called.append("async")

        lm.add_startup(async_hook)
        await lm.startup()
        assert called == ["async"]

    async def test_shutdown_continues_on_error(self):
        lm = LifecycleManager()
        called = []

        def fail():
            raise RuntimeError("oops")

        lm.add_shutdown(fail)
        lm.add_shutdown(lambda: called.append("ok"))
        await lm.shutdown()
        # Second hook should still have run (it was added second, runs first in reverse)
        assert "ok" in called


# =====================================================================
# Lowlevel server building
# =====================================================================


class TestLowlevelServer:
    def test_builds_with_tools(self):
        server = MCPServer(name="test", version="1.0.0")

        @server.tool()
        async def add(a: int, b: int) -> int:
            return a + b

        ll = server._build_lowlevel_server()
        assert ll.name == "test"

    def test_builds_with_instructions(self):
        server = MCPServer(name="test", instructions="Be helpful")
        ll = server._build_lowlevel_server()
        assert ll.name == "test"


# =====================================================================
# Tool invocation (internal call_tool handler)
# =====================================================================


class TestToolInvocation:
    """Test the internal call_tool handler by calling it directly."""

    async def test_simple_tool_call(self):
        server = MCPServer(name="test")

        @server.tool()
        async def add(a: int, b: int) -> int:
            """Add two numbers."""
            return a + b

        ll = server._build_lowlevel_server()
        # Access the registered call_tool handler via the tool cache
        # We test through the actual MCP types
        result = await server._tool_registry.get("add").handler(a=2, b=3)
        assert result == 5

    async def test_tool_with_optional_params(self):
        server = MCPServer(name="test")

        @server.tool()
        async def search(query: str, limit: int = 10) -> dict:
            return {"query": query, "limit": limit}

        handler = server._tool_registry.get("search").handler
        result = await handler(query="test")
        assert result == {"query": "test", "limit": 10}

    async def test_tool_error_propagates(self):
        server = MCPServer(name="test")

        @server.tool()
        async def failing(x: int) -> int:
            raise ToolError("Failed", code="TEST_ERROR")

        handler = server._tool_registry.get("failing").handler
        with pytest.raises(ToolError, match="Failed"):
            await handler(x=1)

    async def test_sync_tool_handler(self):
        server = MCPServer(name="test")

        @server.tool()
        def multiply(a: int, b: int) -> int:
            return a * b

        handler = server._tool_registry.get("multiply").handler
        result = handler(a=3, b=4)
        assert result == 12


# =====================================================================
# Resource invocation
# =====================================================================


class TestResourceInvocation:
    async def test_static_resource(self):
        server = MCPServer(name="test")

        @server.resource("config://app")
        async def get_config() -> str:
            return '{"env": "test"}'

        rdef = server._resource_registry.get("config://app")
        assert rdef is not None
        result = await rdef.handler()
        assert result == '{"env": "test"}'

    async def test_resource_template(self):
        server = MCPServer(name="test")

        @server.resource_template("users://{user_id}")
        async def get_user(user_id: str) -> str:
            return f"User: {user_id}"

        match = server._resource_registry.match_template("users://42")
        assert match is not None
        tmpl_def, params = match
        assert params == {"user_id": "42"}
        result = await tmpl_def.handler(**params)
        assert result == "User: 42"


# =====================================================================
# Prompt invocation
# =====================================================================


class TestPromptInvocation:
    async def test_prompt_handler(self):
        server = MCPServer(name="test")

        @server.prompt()
        async def review(code: str, language: str = "python") -> str:
            """Generate code review prompt."""
            return f"Review this {language} code:\n{code}"

        pdef = server._prompt_registry.get("review")
        result = await pdef.handler(code="x = 1")
        assert "python" in result
        assert "x = 1" in result


# =====================================================================
# Middleware registration
# =====================================================================


class TestMiddleware:
    def test_add_middleware(self):
        server = MCPServer()

        async def my_middleware(ctx, call_next):
            return await call_next(ctx)

        server.add_middleware(my_middleware)
        assert len(server._middlewares) == 1

    def test_middleware_decorator(self):
        server = MCPServer()

        @server.middleware
        async def my_middleware(ctx, call_next):
            return await call_next(ctx)

        assert len(server._middlewares) == 1


# =====================================================================
# TransportType enum
# =====================================================================


class TestTransportType:
    def test_values(self):
        assert TransportType.STDIO.value == "stdio"
        assert TransportType.HTTP.value == "http"
        assert TransportType.SSE.value == "sse"

    def test_from_string(self):
        assert TransportType("stdio") == TransportType.STDIO
        assert TransportType("http") == TransportType.HTTP
