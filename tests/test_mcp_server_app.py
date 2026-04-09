"""Unit tests for promptise.mcp.server._app — MCPServer core class."""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from promptise.mcp.server import MCPServer


# ---------------------------------------------------------------------------
# Construction
# ---------------------------------------------------------------------------


class TestMCPServerInit:
    """Tests for MCPServer initialization."""

    def test_default_construction(self):
        server = MCPServer()
        assert server.name == "promptise-server"
        assert server.version == "0.1.0"
        assert server.instructions is None
        assert server._require_auth is False
        assert server._middlewares == []

    def test_custom_construction(self):
        server = MCPServer(
            name="test-server",
            version="2.0.0",
            instructions="Be helpful",
            require_auth=True,
        )
        assert server.name == "test-server"
        assert server.version == "2.0.0"
        assert server.instructions == "Be helpful"
        assert server._require_auth is True

    def test_registries_initialized(self):
        server = MCPServer()
        assert server._tool_registry is not None
        assert server._resource_registry is not None
        assert server._prompt_registry is not None

    def test_lifecycle_manager(self):
        server = MCPServer()
        assert server._lifecycle is not None

    def test_session_manager(self):
        server = MCPServer()
        assert server._session_manager is not None


# ---------------------------------------------------------------------------
# Tool registration
# ---------------------------------------------------------------------------


class TestToolDecorator:
    """Tests for @server.tool() decorator."""

    def test_register_simple_tool(self):
        server = MCPServer()

        @server.tool()
        async def add(a: int, b: int) -> int:
            """Add two numbers."""
            return a + b

        tools = server._tool_registry.list_all()
        assert len(tools) == 1
        assert tools[0].name == "add"

    def test_register_with_custom_name(self):
        server = MCPServer()

        @server.tool(name="custom_add")
        async def add(a: int, b: int) -> int:
            """Add two numbers."""
            return a + b

        tools = server._tool_registry.list_all()
        assert tools[0].name == "custom_add"

    def test_register_multiple_tools(self):
        server = MCPServer()

        @server.tool()
        async def tool_a() -> str:
            """Tool A."""
            return "a"

        @server.tool()
        async def tool_b() -> str:
            """Tool B."""
            return "b"

        @server.tool()
        async def tool_c() -> str:
            """Tool C."""
            return "c"

        tools = server._tool_registry.list_all()
        names = {t.name for t in tools}
        assert names == {"tool_a", "tool_b", "tool_c"}

    def test_register_with_tags(self):
        server = MCPServer()

        @server.tool(tags=["math", "arithmetic"])
        async def multiply(a: int, b: int) -> int:
            """Multiply two numbers."""
            return a * b

        tools = server._tool_registry.list_all()
        assert tools[0].tags == ["math", "arithmetic"]

    def test_register_with_auth(self):
        server = MCPServer()

        @server.tool(auth=True)
        async def protected() -> str:
            """Protected tool."""
            return "secret"

        tools = server._tool_registry.list_all()
        assert tools[0].auth is True

    def test_register_with_roles(self):
        server = MCPServer()

        @server.tool(roles=["admin", "editor"])
        async def admin_tool() -> str:
            """Admin only."""
            return "admin"

        tools = server._tool_registry.list_all()
        assert len(tools[0].guards) > 0  # Roles create guard objects

    def test_require_auth_forces_all_tools(self):
        server = MCPServer(require_auth=True)

        @server.tool()
        async def public_tool() -> str:
            """Should be forced to require auth."""
            return "data"

        tools = server._tool_registry.list_all()
        assert tools[0].auth is True

    def test_tool_with_timeout(self):
        server = MCPServer()

        @server.tool(timeout=5.0)
        async def slow_tool() -> str:
            """A slow tool."""
            return "done"

        tools = server._tool_registry.list_all()
        assert tools[0].timeout == 5.0


# ---------------------------------------------------------------------------
# Resource registration
# ---------------------------------------------------------------------------


class TestResourceDecorator:
    """Tests for @server.resource() decorator."""

    def test_register_resource(self):
        server = MCPServer()

        @server.resource("config://app")
        async def app_config() -> str:
            """Application configuration."""
            return '{"debug": true}'

        resources = server._resource_registry.list_all()
        assert len(resources) == 1
        assert resources[0].uri == "config://app"


class TestResourceTemplateDecorator:
    """Tests for @server.resource_template() decorator."""

    def test_register_template(self):
        server = MCPServer()

        @server.resource_template("users://{user_id}/profile")
        async def user_profile(user_id: str) -> str:
            """User profile."""
            return f'{{"user_id": "{user_id}"}}'

        templates = server._resource_registry.list_templates()
        assert len(templates) == 1


# ---------------------------------------------------------------------------
# Prompt registration
# ---------------------------------------------------------------------------


class TestPromptDecorator:
    """Tests for @server.prompt() decorator."""

    def test_register_prompt(self):
        server = MCPServer()

        @server.prompt()
        async def summarize(text: str) -> str:
            """Summarize the given text."""
            return f"Please summarize: {text}"

        prompts = server._prompt_registry.list_all()
        assert len(prompts) == 1
        assert prompts[0].name == "summarize"


# ---------------------------------------------------------------------------
# Middleware
# ---------------------------------------------------------------------------


class TestMiddleware:
    """Tests for middleware management."""

    def test_add_middleware(self):
        server = MCPServer()

        class DummyMiddleware:
            async def __call__(self, ctx, next_fn):
                return await next_fn(ctx)

        mw = DummyMiddleware()
        server.add_middleware(mw)
        assert mw in server._middlewares

    def test_add_multiple_middlewares(self):
        server = MCPServer()

        class MW1:
            async def __call__(self, ctx, next_fn):
                return await next_fn(ctx)

        class MW2:
            async def __call__(self, ctx, next_fn):
                return await next_fn(ctx)

        server.add_middleware(MW1())
        server.add_middleware(MW2())
        assert len(server._middlewares) == 2


# ---------------------------------------------------------------------------
# Lifecycle hooks
# ---------------------------------------------------------------------------


class TestLifecycleHooks:
    """Tests for startup/shutdown lifecycle hooks."""

    def test_on_startup(self):
        server = MCPServer()
        called = []

        @server.on_startup
        async def startup():
            called.append("started")

        # Lifecycle hooks are stored, not called yet
        assert len(server._lifecycle._startup_hooks) >= 1

    def test_on_shutdown(self):
        server = MCPServer()
        called = []

        @server.on_shutdown
        async def shutdown():
            called.append("stopped")

        assert len(server._lifecycle._shutdown_hooks) >= 1


# ---------------------------------------------------------------------------
# Exception handlers
# ---------------------------------------------------------------------------


class TestExceptionHandlers:
    """Tests for exception handler registration."""

    def test_register_handler(self):
        server = MCPServer()

        @server.exception_handler(ValueError)
        async def handle_value_error(exc):
            return {"error": str(exc)}

        assert server._exception_handlers is not None


# ---------------------------------------------------------------------------
# Input model pre-building
# ---------------------------------------------------------------------------


class TestInputModels:
    """Tests for input model pre-building on tool registration."""

    def test_input_model_built(self):
        server = MCPServer()

        @server.tool()
        async def calculate(expression: str, precision: int = 2) -> float:
            """Evaluate a math expression."""
            return 0.0

        # Input models are built on registration
        assert "calculate" in server._input_models
        model = server._input_models["calculate"]
        assert "expression" in model.model_fields
        assert "precision" in model.model_fields
