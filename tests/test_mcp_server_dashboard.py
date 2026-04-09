"""Tests for the MCP server dashboard, require_auth, and auth gate features."""

from __future__ import annotations

import time

import pytest

from promptise.mcp.server import (
    APIKeyAuth,
    AuthMiddleware,
    DashboardMiddleware,
    DashboardState,
    JWTAuth,
    MCPRouter,
    MCPServer,
)
from promptise.mcp.server._dashboard import (
    BANNER,
    LOGO,
    TABS,
    Dashboard,
    RequestLog,
    _build_endpoint,
    _fmt_lat,
    _fmt_lat_colored,
    _format_duration,
    _kv,
    _shorten_error,
)
from promptise.mcp.server._testing import TestClient

# =====================================================================
# DashboardState tests
# =====================================================================


class TestDashboardState:
    def test_initial_state(self):
        state = DashboardState(server_name="test", version="1.0")
        assert state.total_requests == 0
        assert state.total_errors == 0
        assert len(state.recent_requests) == 0
        assert len(state.active_sessions) == 0

    def test_record_successful_request(self):
        state = DashboardState()
        log = RequestLog(
            timestamp=time.time(),
            client_id="agent-1",
            tool_name="search",
            success=True,
            latency_ms=5.0,
        )
        state.record_request(log)

        assert state.total_requests == 1
        assert state.total_errors == 0
        assert state.tool_calls["search"] == 1
        assert state.tool_errors.get("search", 0) == 0
        assert state.tool_latency["search"] == 5.0
        assert "agent-1" in state.active_sessions
        assert state.active_sessions["agent-1"]["request_count"] == 1

    def test_record_failed_request(self):
        state = DashboardState()
        log = RequestLog(
            timestamp=time.time(),
            client_id="agent-2",
            tool_name="delete",
            success=False,
            latency_ms=2.0,
            error_code="AUTHENTICATION_ERROR",
        )
        state.record_request(log)

        assert state.total_requests == 1
        assert state.total_errors == 1
        assert state.tool_calls["delete"] == 1
        assert state.tool_errors["delete"] == 1

    def test_record_anonymous_request(self):
        state = DashboardState()
        log = RequestLog(
            timestamp=time.time(),
            client_id=None,
            tool_name="list",
            success=True,
            latency_ms=1.0,
        )
        state.record_request(log)

        assert state.total_requests == 1
        assert len(state.active_sessions) == 0  # No session for anonymous

    def test_session_tracking(self):
        state = DashboardState()
        ts = time.time()

        for i in range(5):
            state.record_request(
                RequestLog(
                    timestamp=ts + i,
                    client_id="agent-1",
                    tool_name="list",
                    success=True,
                    latency_ms=1.0,
                )
            )

        assert state.active_sessions["agent-1"]["request_count"] == 5
        assert state.active_sessions["agent-1"]["last_seen"] == ts + 4

    def test_multiple_agents(self):
        state = DashboardState()
        for agent in ["agent-admin", "agent-viewer", "agent-finance"]:
            state.record_request(
                RequestLog(
                    timestamp=time.time(),
                    client_id=agent,
                    tool_name="list",
                    success=True,
                    latency_ms=1.0,
                )
            )

        assert len(state.active_sessions) == 3

    def test_ring_buffer_maxlen(self):
        state = DashboardState()
        state.recent_requests = __import__("collections").deque(maxlen=5)

        for i in range(10):
            state.record_request(
                RequestLog(
                    timestamp=time.time(),
                    client_id=None,
                    tool_name=f"tool_{i}",
                    success=True,
                    latency_ms=1.0,
                )
            )

        assert len(state.recent_requests) == 5
        assert state.total_requests == 10

    def test_tool_latency_accumulation(self):
        state = DashboardState()
        for lat in [10.0, 20.0, 30.0]:
            state.record_request(
                RequestLog(
                    timestamp=time.time(),
                    client_id=None,
                    tool_name="search",
                    success=True,
                    latency_ms=lat,
                )
            )

        assert state.tool_latency["search"] == 60.0

    def test_min_max_latency(self):
        """Min and max latency are tracked per tool."""
        state = DashboardState()
        for lat in [10.0, 5.0, 20.0, 3.0, 15.0]:
            state.record_request(
                RequestLog(
                    timestamp=time.time(),
                    client_id=None,
                    tool_name="search",
                    success=True,
                    latency_ms=lat,
                )
            )

        assert state.tool_min_latency["search"] == 3.0
        assert state.tool_max_latency["search"] == 20.0

    def test_min_max_latency_single_call(self):
        """Single call sets both min and max to the same value."""
        state = DashboardState()
        state.record_request(
            RequestLog(
                timestamp=time.time(),
                client_id=None,
                tool_name="ping",
                success=True,
                latency_ms=7.5,
            )
        )

        assert state.tool_min_latency["ping"] == 7.5
        assert state.tool_max_latency["ping"] == 7.5

    def test_session_error_count(self):
        """Session tracks error count per agent."""
        state = DashboardState()
        ts = time.time()

        # 3 successes, 2 errors
        for i, success in enumerate([True, True, False, True, False]):
            state.record_request(
                RequestLog(
                    timestamp=ts + i,
                    client_id="agent-1",
                    tool_name="test",
                    success=success,
                    latency_ms=1.0,
                    error_code=None if success else "TEST_ERROR",
                )
            )

        session = state.active_sessions["agent-1"]
        assert session["request_count"] == 5
        assert session["error_count"] == 2

    def test_session_tools_used(self):
        """Session tracks unique tools used by each agent."""
        state = DashboardState()
        ts = time.time()

        for i, tool in enumerate(["search", "list", "search", "delete"]):
            state.record_request(
                RequestLog(
                    timestamp=ts + i,
                    client_id="agent-1",
                    tool_name=tool,
                    success=True,
                    latency_ms=1.0,
                )
            )

        tools_used = state.active_sessions["agent-1"]["tools_used"]
        assert tools_used == {"search", "list", "delete"}


# =====================================================================
# DashboardMiddleware tests
# =====================================================================


class TestDashboardMiddleware:
    @pytest.mark.asyncio
    async def test_records_successful_call(self):
        state = DashboardState()
        mw = DashboardMiddleware(state)

        class FakeCtx:
            client_id = "test-agent"
            tool_name = "my_tool"

        async def call_next(ctx):
            return "result"

        result = await mw(FakeCtx(), call_next)

        assert result == "result"
        assert state.total_requests == 1
        assert state.total_errors == 0
        assert state.tool_calls["my_tool"] == 1

    @pytest.mark.asyncio
    async def test_records_failed_call(self):
        state = DashboardState()
        mw = DashboardMiddleware(state)

        class FakeCtx:
            client_id = "test-agent"
            tool_name = "my_tool"

        class MyError(Exception):
            code = "TEST_ERROR"

        async def call_next(ctx):
            raise MyError("boom")

        with pytest.raises(MyError):
            await mw(FakeCtx(), call_next)

        assert state.total_requests == 1
        assert state.total_errors == 1
        assert state.tool_errors["my_tool"] == 1


# =====================================================================
# Helper function tests
# =====================================================================


class TestHelpers:
    def test_format_duration_seconds(self):
        assert _format_duration(5) == "5s"
        assert _format_duration(0) == "0s"
        assert _format_duration(59) == "59s"

    def test_format_duration_minutes(self):
        assert _format_duration(60) == "1m 0s"
        assert _format_duration(125) == "2m 5s"

    def test_format_duration_hours(self):
        assert _format_duration(3600) == "1h 0m"
        assert _format_duration(3661) == "1h 1m"

    def test_build_endpoint(self):
        assert _build_endpoint("stdio", "0.0.0.0", 8080) == "stdin/stdout"
        assert _build_endpoint("http", "127.0.0.1", 8080) == "http://127.0.0.1:8080/mcp"
        assert _build_endpoint("sse", "0.0.0.0", 9090) == "http://0.0.0.0:9090/sse"

    def test_shorten_error(self):
        assert _shorten_error(None) == "ERROR"
        assert _shorten_error("AUTHENTICATION_ERROR") == "AUTH"
        assert _shorten_error("ValidationError") == "VALID"
        assert _shorten_error("TIMEOUT") == "TIMEOUT"
        assert _shorten_error("RateLimitError") == "RATE"
        assert _shorten_error("INTERNAL_ERROR") == "INTERNAL"
        assert _shorten_error("CustomCode") == "CustomCode"

    def test_fmt_lat(self):
        assert _fmt_lat(150.0) == "150ms"
        assert _fmt_lat(5.0) == "5ms"
        assert _fmt_lat(0.5) == "0.5ms"
        assert _fmt_lat(1.0) == "1ms"

    def test_fmt_lat_colored(self):
        # Just verify it returns a Text object without crashing
        result = _fmt_lat_colored(150.0)
        assert result is not None
        result = _fmt_lat_colored(50.0)
        assert result is not None
        result = _fmt_lat_colored(5.0)
        assert result is not None
        result = _fmt_lat_colored(0.3)
        assert result is not None

    def test_kv(self):
        cell = _kv("Label", "42", "bold cyan")
        assert cell is not None
        assert "42" in str(cell)
        assert "Label" in str(cell)

    def test_logo_is_banner_alias(self):
        """BANNER is an alias for LOGO (backward compat)."""
        assert BANNER is LOGO

    def test_logo_non_empty(self):
        """Logo has content."""
        assert len(LOGO) > 0
        assert all(len(line) > 0 for line in LOGO)


# =====================================================================
# Tab definitions
# =====================================================================


class TestTabs:
    def test_tab_count(self):
        assert len(TABS) == 6

    def test_tab_structure(self):
        for num, label in TABS:
            assert isinstance(num, str)
            assert isinstance(label, str)
            assert len(num) > 0
            assert len(label) > 0

    def test_tab_labels(self):
        labels = [label for _, label in TABS]
        assert "Overview" in labels
        assert "Tools" in labels
        assert "Agents" in labels
        assert "Logs" in labels
        assert "Metrics" in labels
        assert "Raw Logs" in labels


# =====================================================================
# Dashboard tab navigation tests
# =====================================================================


class TestDashboardNavigation:
    def _make_dashboard(self):
        state = DashboardState(
            server_name="test-server",
            version="1.0.0",
            transport="http",
            host="127.0.0.1",
            port=8080,
        )
        return Dashboard(state)

    def test_initial_tab(self):
        dash = self._make_dashboard()
        assert dash._current_tab == 0

    def test_next_tab(self):
        dash = self._make_dashboard()
        dash._next_tab()
        assert dash._current_tab == 1
        dash._next_tab()
        assert dash._current_tab == 2

    def test_prev_tab(self):
        dash = self._make_dashboard()
        dash._current_tab = 2
        dash._prev_tab()
        assert dash._current_tab == 1
        dash._prev_tab()
        assert dash._current_tab == 0

    def test_next_tab_wraps(self):
        dash = self._make_dashboard()
        dash._current_tab = len(TABS) - 1
        dash._next_tab()
        assert dash._current_tab == 0

    def test_prev_tab_wraps(self):
        dash = self._make_dashboard()
        dash._current_tab = 0
        dash._prev_tab()
        assert dash._current_tab == len(TABS) - 1

    def test_direct_tab_set(self):
        dash = self._make_dashboard()
        dash._current_tab = 3
        assert dash._current_tab == 3


# =====================================================================
# Dashboard rendering tests (smoke test — just ensure no exceptions)
# =====================================================================


class TestDashboardRendering:
    def _make_state(self, **kwargs):
        defaults = dict(
            server_name="test-server",
            version="1.0.0",
            transport="http",
            host="127.0.0.1",
            port=8080,
        )
        defaults.update(kwargs)
        return DashboardState(**defaults)

    def test_render_empty_state(self):
        state = self._make_state()
        dash = Dashboard(state)
        layout = dash._render()
        assert layout is not None

    def test_render_with_tools(self):
        state = self._make_state(
            tools=[
                {"name": "search", "auth": True, "roles": ["admin"], "tags": ["read"]},
                {"name": "list", "auth": False, "roles": [], "tags": []},
            ],
        )
        dash = Dashboard(state)
        layout = dash._render()
        assert layout is not None

    def test_render_with_requests(self):
        state = self._make_state()
        state.record_request(
            RequestLog(
                timestamp=time.time(),
                client_id="agent-1",
                tool_name="search",
                success=True,
                latency_ms=5.0,
            )
        )
        state.record_request(
            RequestLog(
                timestamp=time.time(),
                client_id=None,
                tool_name="delete",
                success=False,
                latency_ms=2.0,
                error_code="AUTHENTICATION_ERROR",
            )
        )

        dash = Dashboard(state)
        layout = dash._render()
        assert layout is not None

    def test_render_all_tabs(self):
        """Every tab renders without exceptions."""
        state = self._make_state(
            tools=[
                {"name": "search", "auth": True, "roles": ["admin", "viewer"], "tags": ["read"]},
                {"name": "delete", "auth": True, "roles": ["admin"], "tags": ["write"]},
                {"name": "list", "auth": False, "roles": [], "tags": []},
            ],
        )
        # Add some request data
        ts = time.time()
        for i in range(5):
            state.record_request(
                RequestLog(
                    timestamp=ts + i,
                    client_id=f"agent-{i % 2}",
                    tool_name="search" if i % 2 == 0 else "list",
                    success=i != 3,
                    latency_ms=float(i + 1),
                    error_code="TEST_ERROR" if i == 3 else None,
                )
            )

        dash = Dashboard(state)

        for tab_idx in range(len(TABS)):
            dash._current_tab = tab_idx
            layout = dash._render()
            assert layout is not None, f"Tab {tab_idx} ({TABS[tab_idx][1]}) failed"

    def test_render_overview_tab(self):
        state = self._make_state()
        dash = Dashboard(state)
        dash._current_tab = 0
        panel = dash._tab_overview()
        assert panel is not None

    def test_render_tools_tab(self):
        state = self._make_state(
            tools=[
                {"name": "tool_a", "auth": True, "roles": ["admin"], "tags": ["tag1"]},
            ],
        )
        dash = Dashboard(state)
        panel = dash._tab_tools()
        assert panel is not None

    def test_render_agents_tab_empty(self):
        state = self._make_state()
        dash = Dashboard(state)
        panel = dash._tab_agents()
        assert panel is not None

    def test_render_agents_tab_with_sessions(self):
        state = self._make_state()
        state.record_request(
            RequestLog(
                timestamp=time.time(),
                client_id="agent-1",
                tool_name="search",
                success=True,
                latency_ms=5.0,
            )
        )
        dash = Dashboard(state)
        panel = dash._tab_agents()
        assert panel is not None

    def test_render_logs_tab_empty(self):
        state = self._make_state()
        dash = Dashboard(state)
        panel = dash._tab_logs()
        assert panel is not None

    def test_render_logs_tab_with_requests(self):
        state = self._make_state()
        state.record_request(
            RequestLog(
                timestamp=time.time(),
                client_id="agent-1",
                tool_name="search",
                success=True,
                latency_ms=5.0,
            )
        )
        state.record_request(
            RequestLog(
                timestamp=time.time(),
                client_id=None,
                tool_name="delete",
                success=False,
                latency_ms=200.0,
                error_code="AUTH_ERROR",
            )
        )
        dash = Dashboard(state)
        panel = dash._tab_logs()
        assert panel is not None

    def test_render_metrics_tab_empty(self):
        state = self._make_state()
        dash = Dashboard(state)
        panel = dash._tab_metrics()
        assert panel is not None

    def test_render_raw_logs_tab_empty(self):
        state = self._make_state()
        dash = Dashboard(state)
        panel = dash._tab_raw_logs()
        assert panel is not None

    def test_render_raw_logs_tab_with_records(self):
        import logging

        state = self._make_state()
        dash = Dashboard(state)
        # Manually inject a log record into the capture handler
        record = logging.LogRecord(
            name="test.logger",
            level=logging.INFO,
            pathname="test.py",
            lineno=1,
            msg="Hello from test",
            args=None,
            exc_info=None,
        )
        dash._log_capture.records.append(record)
        panel = dash._tab_raw_logs()
        assert panel is not None

    def test_render_metrics_tab_with_data(self):
        state = self._make_state()
        for lat in [5.0, 10.0, 200.0]:
            state.record_request(
                RequestLog(
                    timestamp=time.time(),
                    client_id="agent-1",
                    tool_name="search",
                    success=True,
                    latency_ms=lat,
                )
            )
        state.record_request(
            RequestLog(
                timestamp=time.time(),
                client_id="agent-1",
                tool_name="search",
                success=False,
                latency_ms=3.0,
                error_code="TIMEOUT",
            )
        )
        dash = Dashboard(state)
        panel = dash._tab_metrics()
        assert panel is not None

    def test_render_header(self):
        state = self._make_state()
        dash = Dashboard(state)
        panel = dash._header()
        assert panel is not None

    def test_render_nav_bar(self):
        state = self._make_state()
        dash = Dashboard(state)
        panel = dash._nav_bar()
        assert panel is not None

    def test_nav_bar_highlights_active(self):
        """Nav bar changes based on active tab."""
        state = self._make_state()
        dash = Dashboard(state)

        dash._current_tab = 0
        nav0 = dash._nav_bar()
        dash._current_tab = 3
        nav3 = dash._nav_bar()

        # Both should render without error
        assert nav0 is not None
        assert nav3 is not None


# =====================================================================
# require_auth tests
# =====================================================================


class TestRequireAuth:
    def test_require_auth_forces_tool_auth(self):
        server = MCPServer(name="locked", require_auth=True)

        @server.tool()
        async def public_tool() -> str:
            """Should become auth-required."""
            return "ok"

        # The tool should have auth=True even though we didn't set it
        tdef = server._tool_registry.get("public_tool")
        assert tdef is not None
        assert tdef.auth is True

    def test_without_require_auth(self):
        server = MCPServer(name="open")

        @server.tool()
        async def public_tool() -> str:
            """Should stay public."""
            return "ok"

        tdef = server._tool_registry.get("public_tool")
        assert tdef is not None
        assert tdef.auth is False

    def test_require_auth_with_explicit_auth(self):
        server = MCPServer(name="locked", require_auth=True)

        @server.tool(auth=True, roles=["admin"])
        async def admin_tool() -> str:
            """Explicitly auth — should keep auth=True."""
            return "ok"

        tdef = server._tool_registry.get("admin_tool")
        assert tdef.auth is True

    def test_require_auth_affects_router_tools(self):
        server = MCPServer(name="locked", require_auth=True)
        router = MCPRouter(prefix="api")

        @router.tool()
        async def router_tool() -> str:
            return "ok"

        server.include_router(router)

        tdef = server._tool_registry.get("api_router_tool")
        assert tdef is not None
        assert tdef.auth is True

    def test_auth_provider_tracking(self):
        server = MCPServer(name="test")
        jwt = JWTAuth(secret="test-secret")
        server.add_middleware(AuthMiddleware(jwt))

        assert server._auth_provider is jwt


# =====================================================================
# JWTAuth.verify_token tests
# =====================================================================


class TestVerifyToken:
    def test_verify_valid_token(self):
        jwt = JWTAuth(secret="test-secret")
        token = jwt.create_token({"sub": "user1"})
        assert jwt.verify_token(token) is True

    def test_verify_invalid_token(self):
        jwt = JWTAuth(secret="test-secret")
        assert jwt.verify_token("not-a-token") is False

    def test_verify_wrong_secret(self):
        jwt1 = JWTAuth(secret="secret-1")
        jwt2 = JWTAuth(secret="secret-2")
        token = jwt1.create_token({"sub": "user1"})
        assert jwt2.verify_token(token) is False

    def test_verify_expired_token(self):
        jwt = JWTAuth(secret="test-secret")
        token = jwt.create_token({"sub": "user1"}, expires_in=-1)
        assert jwt.verify_token(token) is False


class TestAPIKeyAuthVerifyToken:
    def test_verify_valid_key(self):
        auth = APIKeyAuth(keys={"key-123": "client-1"})
        assert auth.verify_token("key-123") is True

    def test_verify_invalid_key(self):
        auth = APIKeyAuth(keys={"key-123": "client-1"})
        assert auth.verify_token("bad-key") is False


# =====================================================================
# Integration: require_auth with TestClient
# =====================================================================


class TestRequireAuthIntegration:
    @pytest.mark.asyncio
    async def test_require_auth_blocks_unauthenticated(self):
        server = MCPServer(name="locked", require_auth=True)
        jwt = JWTAuth(secret="test-secret")
        server.add_middleware(AuthMiddleware(jwt))

        @server.tool()
        async def my_tool() -> str:
            return "secret data"

        # Call without auth → should fail
        client = TestClient(server)
        result = await client.call_tool("my_tool", {})
        text = result[0].text
        assert "AUTHENTICATION" in text.upper() or "Missing" in text

    @pytest.mark.asyncio
    async def test_require_auth_allows_authenticated(self):
        server = MCPServer(name="locked", require_auth=True)
        jwt = JWTAuth(secret="test-secret")
        server.add_middleware(AuthMiddleware(jwt))

        @server.tool()
        async def my_tool() -> str:
            return "secret data"

        token = jwt.create_token({"sub": "user-1", "roles": []})
        client = TestClient(server, meta={"authorization": f"Bearer {token}"})
        result = await client.call_tool("my_tool", {})
        assert result[0].text == "secret data"


# =====================================================================
# HTTP header bridging tests
# =====================================================================


class TestHeaderBridging:
    """Verify that HTTP headers are threaded into RequestContext.meta
    via the contextvar bridge so authentication works over real HTTP."""

    def test_set_and_get_request_headers(self):
        """Headers contextvar round-trips correctly."""
        from promptise.mcp.server._context import (
            clear_request_headers,
            get_request_headers,
            set_request_headers,
        )

        set_request_headers({"authorization": "Bearer tok123", "x-custom": "value"})
        headers = get_request_headers()
        assert headers["authorization"] == "Bearer tok123"
        assert headers["x-custom"] == "value"

        clear_request_headers()
        assert get_request_headers() == {}

    def test_get_request_headers_default_empty(self):
        """Returns empty dict when no headers have been set."""
        from promptise.mcp.server._context import (
            clear_request_headers,
            get_request_headers,
        )

        clear_request_headers()
        assert get_request_headers() == {}

    @pytest.mark.asyncio
    async def test_meta_populated_from_contextvar(self):
        """When _request_headers contextvar is set, call_tool populates ctx.meta."""
        from promptise.mcp.server._context import (
            clear_request_headers,
            set_request_headers,
        )

        server = MCPServer(name="header-test")
        jwt_auth = JWTAuth(secret="test-secret")
        server.add_middleware(AuthMiddleware(jwt_auth))

        captured_meta = {}

        @server.tool(auth=True)
        async def check_meta() -> str:
            from promptise.mcp.server import get_context

            ctx = get_context()
            captured_meta.update(ctx.meta)
            return "ok"

        token = jwt_auth.create_token({"sub": "user-1", "roles": []})

        # Simulate what the transport layer does: set the contextvar
        set_request_headers({"authorization": f"Bearer {token}"})

        try:
            client = TestClient(server)
            # TestClient also passes its own meta, but the contextvar
            # should also work.  Here we use a plain TestClient (no meta)
            # and rely on the contextvar to supply the auth header.
            result = await client.call_tool("check_meta", {})
            # The contextvar should have populated ctx.meta
            assert captured_meta.get("authorization") == f"Bearer {token}"
            assert result[0].text == "ok"
        finally:
            clear_request_headers()

    @pytest.mark.asyncio
    async def test_auth_works_via_header_bridge(self):
        """Full auth flow through header bridging (simulated HTTP path)."""
        from promptise.mcp.server._context import (
            clear_request_headers,
            set_request_headers,
        )

        server = MCPServer(name="bridge-test")
        jwt_auth = JWTAuth(secret="bridge-secret")
        server.add_middleware(AuthMiddleware(jwt_auth))

        @server.tool(auth=True, roles=["admin"])
        async def admin_only() -> str:
            return "admin data"

        token = jwt_auth.create_token({"sub": "agent-admin", "roles": ["admin"]})

        # Set headers via contextvar (as the transport does)
        set_request_headers({"authorization": f"Bearer {token}"})

        try:
            # Use plain TestClient (no meta) — auth comes from contextvar
            client = TestClient(server)
            result = await client.call_tool("admin_only", {})
            assert result[0].text == "admin data"
        finally:
            clear_request_headers()

    @pytest.mark.asyncio
    async def test_auth_fails_without_header_bridge(self):
        """Without header bridge, auth correctly fails."""
        from promptise.mcp.server._context import clear_request_headers

        server = MCPServer(name="no-bridge-test")
        jwt_auth = JWTAuth(secret="bridge-secret")
        server.add_middleware(AuthMiddleware(jwt_auth))

        @server.tool(auth=True)
        async def my_tool() -> str:
            return "data"

        # Clear contextvar — no headers available
        clear_request_headers()

        client = TestClient(server)
        result = await client.call_tool("my_tool", {})
        text = result[0].text
        assert "AUTHENTICATION" in text.upper() or "Missing" in text

    @pytest.mark.asyncio
    async def test_role_enforcement_via_header_bridge(self):
        """Role-based guards work through the header bridge."""
        from promptise.mcp.server._context import (
            clear_request_headers,
            set_request_headers,
        )

        server = MCPServer(name="role-test")
        jwt_auth = JWTAuth(secret="role-secret")
        server.add_middleware(AuthMiddleware(jwt_auth))

        @server.tool(auth=True, roles=["finance"])
        async def finance_only() -> str:
            return "salary data"

        # Agent with viewer role (not finance) → should be denied
        viewer_token = jwt_auth.create_token({"sub": "agent-viewer", "roles": ["viewer"]})
        set_request_headers({"authorization": f"Bearer {viewer_token}"})

        try:
            client = TestClient(server)
            result = await client.call_tool("finance_only", {})
            text = result[0].text
            assert "ACCESS" in text.upper() or "DENIED" in text.upper() or "role" in text.lower()
        finally:
            clear_request_headers()

        # Agent with finance role → should succeed
        finance_token = jwt_auth.create_token({"sub": "agent-finance", "roles": ["finance"]})
        set_request_headers({"authorization": f"Bearer {finance_token}"})

        try:
            client = TestClient(server)
            result = await client.call_tool("finance_only", {})
            assert result[0].text == "salary data"
        finally:
            clear_request_headers()


# =====================================================================
# Transport auth gate tests
# =====================================================================


class TestAuthGate:
    @pytest.mark.asyncio
    async def test_auth_gate_rejects_no_auth(self):
        from promptise.mcp.server._transport import _AuthGateASGI

        calls = []

        async def inner_app(scope, receive, send):
            calls.append("called")

        jwt = JWTAuth(secret="test-secret")
        gate = _AuthGateASGI(inner_app, jwt.verify_token)

        responses = []

        async def send(msg):
            responses.append(msg)

        await gate(
            {"type": "http", "headers": []},
            None,
            send,
        )

        assert len(calls) == 0  # Inner app not called
        assert responses[0]["status"] == 401

    @pytest.mark.asyncio
    async def test_auth_gate_allows_valid_token(self):
        from promptise.mcp.server._transport import _AuthGateASGI

        calls = []

        async def inner_app(scope, receive, send):
            calls.append("called")

        jwt = JWTAuth(secret="test-secret")
        token = jwt.create_token({"sub": "user1"})
        gate = _AuthGateASGI(inner_app, jwt.verify_token)

        await gate(
            {
                "type": "http",
                "headers": [
                    (b"authorization", f"Bearer {token}".encode()),
                ],
            },
            None,
            None,
        )

        assert len(calls) == 1  # Inner app was called

    @pytest.mark.asyncio
    async def test_auth_gate_passes_lifespan(self):
        from promptise.mcp.server._transport import _AuthGateASGI

        calls = []

        async def inner_app(scope, receive, send):
            calls.append("called")

        jwt = JWTAuth(secret="test-secret")
        gate = _AuthGateASGI(inner_app, jwt.verify_token)

        await gate({"type": "lifespan"}, None, None)

        assert len(calls) == 1  # Lifespan passes through

    @pytest.mark.asyncio
    async def test_auth_gate_rejects_invalid_token(self):
        from promptise.mcp.server._transport import _AuthGateASGI

        calls = []

        async def inner_app(scope, receive, send):
            calls.append("called")

        jwt = JWTAuth(secret="test-secret")
        gate = _AuthGateASGI(inner_app, jwt.verify_token)

        responses = []

        async def send(msg):
            responses.append(msg)

        await gate(
            {
                "type": "http",
                "headers": [
                    (b"authorization", b"Bearer invalid-token"),
                ],
            },
            None,
            send,
        )

        assert len(calls) == 0
        assert responses[0]["status"] == 401
