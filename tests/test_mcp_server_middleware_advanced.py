"""Tests for advanced MCP server middleware: circuit breaker, webhooks,
structured logging, session state, and tool transforms/versioning."""

from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock, patch

import pytest

from promptise.mcp.server._circuit_breaker import (
    CircuitBreakerMiddleware,
    CircuitOpenError,
    CircuitState,
)
from promptise.mcp.server._context import RequestContext
from promptise.mcp.server._session_state import SessionManager, SessionState
from promptise.mcp.server._structured_logging import StructuredLoggingMiddleware
from promptise.mcp.server._transforms import (
    NamespaceTransform,
    TagFilterTransform,
    VisibilityTransform,
)
from promptise.mcp.server._types import ToolDef
from promptise.mcp.server._versioning import VersionedToolRegistry, _version_key
from promptise.mcp.server._webhooks import WebhookMiddleware

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_ctx(tool_name: str = "test_tool", client_id: str | None = "client-1") -> RequestContext:
    """Build a minimal RequestContext for middleware tests."""
    ctx = RequestContext(
        server_name="test-server",
        tool_name=tool_name,
        request_id="req-abc",
        client_id=client_id,
    )
    return ctx


async def ok_handler(ctx: RequestContext) -> str:
    return "ok"


async def failing_handler(ctx: RequestContext) -> str:
    raise RuntimeError("downstream failure")


def make_tool(name: str, tags: list[str] | None = None) -> ToolDef:
    async def _handler(**kwargs): ...

    return ToolDef(
        name=name, description=f"Tool {name}", handler=_handler, input_schema={}, tags=tags or []
    )


# ===========================================================================
# CircuitBreakerMiddleware
# ===========================================================================


class TestCircuitBreakerMiddleware:
    """Tests for CircuitBreakerMiddleware."""

    def test_initial_state_is_closed(self) -> None:
        cb = CircuitBreakerMiddleware()
        assert cb.get_state("any_tool") == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_closed_passes_through_on_success(self) -> None:
        cb = CircuitBreakerMiddleware()
        ctx = make_ctx()
        result = await cb(ctx, ok_handler)
        assert result == "ok"
        assert cb.get_state(ctx.tool_name) == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_failure_increments_count(self) -> None:
        cb = CircuitBreakerMiddleware(failure_threshold=3)
        ctx = make_ctx()
        for _ in range(2):
            with pytest.raises(RuntimeError):
                await cb(ctx, failing_handler)
        # Still closed — threshold not reached yet
        assert cb.get_state(ctx.tool_name) == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_circuit_opens_after_threshold(self) -> None:
        cb = CircuitBreakerMiddleware(failure_threshold=3)
        ctx = make_ctx()
        for _ in range(3):
            with pytest.raises(RuntimeError):
                await cb(ctx, failing_handler)
        assert cb.get_state(ctx.tool_name) == CircuitState.OPEN

    @pytest.mark.asyncio
    async def test_open_circuit_raises_circuit_open_error(self) -> None:
        cb = CircuitBreakerMiddleware(failure_threshold=1, recovery_timeout=999.0)
        ctx = make_ctx()
        with pytest.raises(RuntimeError):
            await cb(ctx, failing_handler)
        # Now open — next call should raise CircuitOpenError immediately
        with pytest.raises(CircuitOpenError) as exc_info:
            await cb(ctx, ok_handler)
        assert exc_info.value.tool == ctx.tool_name

    @pytest.mark.asyncio
    async def test_excluded_tool_bypasses_circuit(self) -> None:
        cb = CircuitBreakerMiddleware(
            failure_threshold=1,
            excluded_tools={"health_check"},
        )
        ctx = make_ctx(tool_name="health_check")
        # Failure on excluded tool does NOT open circuit
        for _ in range(5):
            with pytest.raises(RuntimeError):
                await cb(ctx, failing_handler)
        assert cb.get_state("health_check") == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_recovery_timeout_transitions_to_half_open(self) -> None:
        cb = CircuitBreakerMiddleware(failure_threshold=1, recovery_timeout=0.0)
        ctx = make_ctx()
        with pytest.raises(RuntimeError):
            await cb(ctx, failing_handler)
        assert cb.get_state(ctx.tool_name) == CircuitState.OPEN
        # With 0.0 recovery timeout, next call should transition to HALF_OPEN and probe
        result = await cb(ctx, ok_handler)
        assert result == "ok"
        assert cb.get_state(ctx.tool_name) == CircuitState.CLOSED

    @pytest.mark.asyncio
    async def test_half_open_failure_reopens_circuit(self) -> None:
        cb = CircuitBreakerMiddleware(failure_threshold=1, recovery_timeout=0.0)
        ctx = make_ctx()
        with pytest.raises(RuntimeError):
            await cb(ctx, failing_handler)
        assert cb.get_state(ctx.tool_name) == CircuitState.OPEN
        # Recovery probe fails → back to OPEN
        with pytest.raises(RuntimeError):
            await cb(ctx, failing_handler)
        assert cb.get_state(ctx.tool_name) == CircuitState.OPEN

    def test_reset_specific_tool(self) -> None:
        cb = CircuitBreakerMiddleware()
        # Create entry
        _ = cb.get_state("tool_a")
        _ = cb.get_state("tool_b")
        cb.reset("tool_a")
        assert "tool_a" not in cb._circuits
        assert "tool_b" in cb._circuits

    def test_reset_all_tools(self) -> None:
        cb = CircuitBreakerMiddleware()
        _ = cb.get_state("tool_a")
        _ = cb.get_state("tool_b")
        cb.reset()
        assert cb._circuits == {}

    def test_success_resets_failure_count(self) -> None:
        cb = CircuitBreakerMiddleware(failure_threshold=3)
        # Record it
        circuit = cb._get_circuit("my_tool")
        circuit.failure_count = 2

        async def run() -> None:
            ctx = make_ctx(tool_name="my_tool")
            await cb(ctx, ok_handler)

        asyncio.run(run())
        assert cb._circuits["my_tool"].failure_count == 0


# ===========================================================================
# StructuredLoggingMiddleware
# ===========================================================================


class TestStructuredLoggingMiddleware:
    """Tests for StructuredLoggingMiddleware."""

    @pytest.mark.asyncio
    async def test_logs_start_and_end_on_success(self, caplog: pytest.LogCaptureFixture) -> None:
        middleware = StructuredLoggingMiddleware()
        ctx = make_ctx()

        with caplog.at_level(logging.INFO, logger="promptise.server"):
            result = await middleware(ctx, ok_handler)

        assert result == "ok"
        log_messages = [r.message for r in caplog.records]
        # Should have at least two JSON log entries
        starts = [m for m in log_messages if '"tool_call_start"' in m]
        ends = [m for m in log_messages if '"tool_call_end"' in m]
        assert len(starts) >= 1
        assert len(ends) >= 1

        end_data = json.loads(ends[0])
        assert end_data["status"] == "ok"
        assert "duration_ms" in end_data

    @pytest.mark.asyncio
    async def test_logs_error_on_failure(self, caplog: pytest.LogCaptureFixture) -> None:
        middleware = StructuredLoggingMiddleware()
        ctx = make_ctx()

        with caplog.at_level(logging.INFO, logger="promptise.server"):
            with pytest.raises(RuntimeError):
                await middleware(ctx, failing_handler)

        log_messages = [r.message for r in caplog.records]
        ends = [m for m in log_messages if '"tool_call_end"' in m]
        assert len(ends) >= 1
        end_data = json.loads(ends[0])
        assert end_data["status"] == "error"
        assert "error" in end_data
        assert "downstream failure" in end_data["error"]

    @pytest.mark.asyncio
    async def test_client_id_in_log(self, caplog: pytest.LogCaptureFixture) -> None:
        middleware = StructuredLoggingMiddleware()
        ctx = make_ctx(client_id="user-42")

        with caplog.at_level(logging.INFO, logger="promptise.server"):
            await middleware(ctx, ok_handler)

        log_messages = [r.message for r in caplog.records]
        start_entry = json.loads(log_messages[0])
        assert start_entry.get("client_id") == "user-42"

    @pytest.mark.asyncio
    async def test_no_client_id_omitted_from_log(self, caplog: pytest.LogCaptureFixture) -> None:
        middleware = StructuredLoggingMiddleware()
        ctx = make_ctx(client_id=None)

        with caplog.at_level(logging.INFO, logger="promptise.server"):
            await middleware(ctx, ok_handler)

        log_messages = [r.message for r in caplog.records]
        start_entry = json.loads(log_messages[0])
        assert "client_id" not in start_entry

    @pytest.mark.asyncio
    async def test_log_level_respected(self) -> None:
        middleware = StructuredLoggingMiddleware(log_level=logging.DEBUG)
        ctx = make_ctx()
        # Should not raise; log level is passed to logger.log
        result = await middleware(ctx, ok_handler)
        assert result == "ok"


# ===========================================================================
# WebhookMiddleware
# ===========================================================================


class TestWebhookMiddleware:
    """Tests for WebhookMiddleware."""

    @pytest.mark.asyncio
    async def test_fires_tool_call_and_success_events(self) -> None:
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            middleware = WebhookMiddleware(
                url="https://hooks.example.com/webhook",
                events={"tool.call", "tool.success"},
            )
            ctx = make_ctx()
            result = await middleware(ctx, ok_handler)

        assert result == "ok"
        # Two payloads in sent buffer: tool.call + tool.success
        assert len(middleware.sent) == 2
        event_types = {p["event"] for p in middleware.sent}
        assert event_types == {"tool.call", "tool.success"}

    @pytest.mark.asyncio
    async def test_fires_error_event_on_failure(self) -> None:
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            middleware = WebhookMiddleware(
                url="https://hooks.example.com/webhook",
                events={"tool.error"},
            )
            ctx = make_ctx()
            with pytest.raises(RuntimeError):
                await middleware(ctx, failing_handler)

        assert len(middleware.sent) == 1
        assert middleware.sent[0]["event"] == "tool.error"
        assert "error" in middleware.sent[0]
        assert "downstream failure" in middleware.sent[0]["error"]

    @pytest.mark.asyncio
    async def test_error_does_not_block_tool_call(self) -> None:
        """Webhook delivery failure should not propagate to the tool call."""
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client.post = AsyncMock(side_effect=Exception("connection refused"))
            mock_client_cls.return_value = mock_client

            middleware = WebhookMiddleware(
                url="https://unreachable.example.com/",
                events={"tool.call", "tool.success"},
            )
            ctx = make_ctx()
            # Tool call still succeeds even if webhook delivery fails
            result = await middleware(ctx, ok_handler)

        assert result == "ok"

    def test_sent_property_initial_empty(self) -> None:
        with patch("httpx.AsyncClient"):
            middleware = WebhookMiddleware(url="https://example.com")
        assert middleware.sent == []

    @pytest.mark.asyncio
    async def test_payload_structure(self) -> None:
        with patch("httpx.AsyncClient") as mock_client_cls:
            mock_client = AsyncMock()
            mock_client.__aenter__ = AsyncMock(return_value=mock_client)
            mock_client.__aexit__ = AsyncMock(return_value=None)
            mock_client_cls.return_value = mock_client

            middleware = WebhookMiddleware(
                url="https://example.com",
                events={"tool.call"},
            )
            ctx = make_ctx(tool_name="my_tool", client_id="client-99")
            await middleware(ctx, ok_handler)

        payload = middleware.sent[0]
        assert payload["event"] == "tool.call"
        assert payload["tool"] == "my_tool"
        assert payload["client_id"] == "client-99"
        assert payload["request_id"] == "req-abc"
        assert "timestamp" in payload


# ===========================================================================
# SessionState
# ===========================================================================


class TestSessionState:
    """Tests for SessionState and SessionManager."""

    def test_get_returns_default_when_missing(self) -> None:
        state = SessionState()
        assert state.get("missing") is None
        assert state.get("missing", "default") == "default"

    def test_set_and_get(self) -> None:
        state = SessionState()
        state.set("key", "value")
        assert state.get("key") == "value"

    def test_delete_removes_key(self) -> None:
        state = SessionState()
        state.set("key", "value")
        state.delete("key")
        assert state.get("key") is None

    def test_delete_nonexistent_key_is_noop(self) -> None:
        state = SessionState()
        state.delete("nonexistent")  # Should not raise

    def test_clear_removes_all_keys(self) -> None:
        state = SessionState()
        state.set("a", 1)
        state.set("b", 2)
        state.clear()
        assert state.keys() == []

    def test_contains_operator(self) -> None:
        state = SessionState()
        state.set("present", True)
        assert "present" in state
        assert "absent" not in state

    def test_keys_returns_all_keys(self) -> None:
        state = SessionState()
        state.set("x", 1)
        state.set("y", 2)
        assert set(state.keys()) == {"x", "y"}

    def test_to_dict_snapshot(self) -> None:
        state = SessionState()
        state.set("a", 1)
        state.set("b", [1, 2, 3])
        d = state.to_dict()
        assert d == {"a": 1, "b": [1, 2, 3]}
        # Mutating snapshot doesn't affect state
        d["a"] = 99
        assert state.get("a") == 1

    def test_overwrite_key(self) -> None:
        state = SessionState()
        state.set("k", "v1")
        state.set("k", "v2")
        assert state.get("k") == "v2"


class TestSessionManager:
    """Tests for SessionManager."""

    def test_get_or_create_returns_new_session(self) -> None:
        manager = SessionManager()
        session = manager.get_or_create("session-1")
        assert isinstance(session, SessionState)

    def test_get_or_create_returns_same_instance(self) -> None:
        manager = SessionManager()
        s1 = manager.get_or_create("session-1")
        s2 = manager.get_or_create("session-1")
        assert s1 is s2

    def test_different_sessions_are_isolated(self) -> None:
        manager = SessionManager()
        s1 = manager.get_or_create("session-1")
        s2 = manager.get_or_create("session-2")
        s1.set("key", "from-session-1")
        assert s2.get("key") is None

    def test_remove_cleans_up_session(self) -> None:
        manager = SessionManager()
        manager.get_or_create("session-1")
        assert manager.active_sessions == 1
        manager.remove("session-1")
        assert manager.active_sessions == 0

    def test_remove_nonexistent_is_noop(self) -> None:
        manager = SessionManager()
        manager.remove("nonexistent")  # Should not raise

    def test_active_sessions_count(self) -> None:
        manager = SessionManager()
        manager.get_or_create("s1")
        manager.get_or_create("s2")
        manager.get_or_create("s3")
        assert manager.active_sessions == 3
        manager.remove("s2")
        assert manager.active_sessions == 2


# ===========================================================================
# Tool Transforms
# ===========================================================================


class TestNamespaceTransform:
    """Tests for NamespaceTransform."""

    def test_prefixes_all_tool_names(self) -> None:
        transform = NamespaceTransform(prefix="myapp")
        tools = [make_tool("search"), make_tool("query")]
        result = transform.apply(tools)
        names = [t.name for t in result]
        assert names == ["myapp_search", "myapp_query"]

    def test_empty_prefix_leaves_names_unchanged(self) -> None:
        transform = NamespaceTransform(prefix="")
        tools = [make_tool("search")]
        result = transform.apply(tools)
        assert result[0].name == "search"

    def test_preserves_other_fields(self) -> None:
        transform = NamespaceTransform(prefix="ns")
        tool = make_tool("search")
        result = transform.apply([tool])
        assert result[0].description == tool.description
        assert result[0].input_schema == tool.input_schema

    def test_empty_tool_list(self) -> None:
        transform = NamespaceTransform(prefix="ns")
        assert transform.apply([]) == []


class TestVisibilityTransform:
    """Tests for VisibilityTransform."""

    def test_hides_tools_matching_predicate(self) -> None:
        transform = VisibilityTransform(hidden={"admin_tool": lambda ctx: True})
        tools = [make_tool("public_tool"), make_tool("admin_tool")]
        result = transform.apply(tools)
        names = [t.name for t in result]
        assert "admin_tool" not in names
        assert "public_tool" in names

    def test_shows_tools_when_predicate_returns_false(self) -> None:
        transform = VisibilityTransform(hidden={"admin_tool": lambda ctx: False})
        tools = [make_tool("public_tool"), make_tool("admin_tool")]
        result = transform.apply(tools)
        names = [t.name for t in result]
        assert "admin_tool" in names

    def test_empty_hidden_shows_all(self) -> None:
        transform = VisibilityTransform()
        tools = [make_tool("a"), make_tool("b")]
        result = transform.apply(tools)
        assert len(result) == 2

    def test_context_passed_to_predicate(self) -> None:
        received_ctx: list = []

        def predicate(ctx):
            received_ctx.append(ctx)
            return False

        ctx = make_ctx()
        transform = VisibilityTransform(hidden={"tool": predicate})
        transform.apply([make_tool("tool")], ctx=ctx)
        assert received_ctx[0] is ctx


class TestTagFilterTransform:
    """Tests for TagFilterTransform."""

    def test_filters_to_required_tags(self) -> None:
        transform = TagFilterTransform(required_tags={"public"})
        tools = [
            make_tool("public_tool", tags=["public"]),
            make_tool("private_tool", tags=["internal"]),
            make_tool("both_tool", tags=["public", "internal"]),
        ]
        result = transform.apply(tools)
        names = [t.name for t in result]
        assert "public_tool" in names
        assert "both_tool" in names
        assert "private_tool" not in names

    def test_no_matching_tools_returns_empty(self) -> None:
        transform = TagFilterTransform(required_tags={"nonexistent"})
        tools = [make_tool("tool", tags=["other"])]
        result = transform.apply(tools)
        assert result == []

    def test_multiple_required_tags(self) -> None:
        transform = TagFilterTransform(required_tags={"a", "b"})
        tools = [
            make_tool("has_a", tags=["a"]),
            make_tool("has_b", tags=["b"]),
            make_tool("has_both", tags=["a", "b"]),
            make_tool("has_neither", tags=["c"]),
        ]
        result = transform.apply(tools)
        names = [t.name for t in result]
        # Any tool with at least one matching tag
        assert "has_a" in names
        assert "has_b" in names
        assert "has_both" in names
        assert "has_neither" not in names


# ===========================================================================
# VersionedToolRegistry
# ===========================================================================


class TestVersionedToolRegistry:
    """Tests for VersionedToolRegistry."""

    def test_register_and_get_latest(self) -> None:
        registry = VersionedToolRegistry()
        td_v1 = make_tool("search")
        td_v2 = make_tool("search")
        registry.register("search", "1.0", td_v1)
        registry.register("search", "2.0", td_v2)
        result = registry.get("search")
        assert result is td_v2  # Latest

    def test_get_pinned_version(self) -> None:
        registry = VersionedToolRegistry()
        td_v1 = make_tool("search")
        td_v2 = make_tool("search")
        registry.register("search", "1.0", td_v1)
        registry.register("search", "2.0", td_v2)
        assert registry.get("search@1.0") is td_v1
        assert registry.get("search@2.0") is td_v2

    def test_get_unknown_tool_returns_none(self) -> None:
        registry = VersionedToolRegistry()
        assert registry.get("nonexistent") is None
        assert registry.get("nonexistent@1.0") is None

    def test_duplicate_version_raises(self) -> None:
        registry = VersionedToolRegistry()
        registry.register("search", "1.0", make_tool("search"))
        with pytest.raises(ValueError, match="already registered"):
            registry.register("search", "1.0", make_tool("search"))

    def test_has_tool(self) -> None:
        registry = VersionedToolRegistry()
        registry.register("search", "1.0", make_tool("search"))
        assert registry.has("search")
        assert not registry.has("nonexistent")

    def test_list_versions(self) -> None:
        registry = VersionedToolRegistry()
        registry.register("search", "1.0", make_tool("search"))
        registry.register("search", "2.0", make_tool("search"))
        registry.register("search", "3.0", make_tool("search"))
        versions = registry.list_versions("search")
        assert versions == ["1.0", "2.0", "3.0"]

    def test_list_versions_unknown_tool(self) -> None:
        registry = VersionedToolRegistry()
        assert registry.list_versions("unknown") == []

    def test_list_all_returns_all_tool_defs(self) -> None:
        registry = VersionedToolRegistry()
        td1 = make_tool("search")
        td2 = make_tool("search")
        td3 = make_tool("query")
        registry.register("search", "1.0", td1)
        registry.register("search", "2.0", td2)
        registry.register("query", "1.0", td3)
        all_tools = registry.list_all()
        assert len(all_tools) == 3

    def test_version_key_comparison(self) -> None:
        assert _version_key("2.0") > _version_key("1.0")
        assert _version_key("1.10") > _version_key("1.9")
        assert _version_key("3.0.0") > _version_key("2.9.9")
        assert _version_key("1.0") == _version_key("1.0")
