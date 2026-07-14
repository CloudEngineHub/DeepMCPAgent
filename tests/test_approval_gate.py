"""Server-side approval gates — HITL enforced where the tool lives.

Covers: pass-through for ungated tools, approve/deny via callback, timeout
denied-by-default (and the explicit allow opt-out), modified-arguments
fail-closed, the build-time no-gate configuration error, the PendingApprover
block → list → decide flow with role-guarded admin tools, and the
ElicitationApprover's fail-closed behavior without a live MCP session.
"""

from __future__ import annotations

import asyncio

import pytest

from promptise.approval import ApprovalDecision
from promptise.mcp.server import (
    ApprovalGateMiddleware,
    AuthMiddleware,
    ElicitationApprover,
    MCPServer,
    PendingApprover,
    TestClient,
)
from promptise.mcp.server._auth import APIKeyAuth
from promptise.mcp.server._context import ClientContext, RequestContext


def _server_with_gate(handler, **gate_kwargs) -> MCPServer:
    server = MCPServer(name="gated")
    server.add_middleware(ApprovalGateMiddleware(handler, **gate_kwargs))

    @server.tool(requires_approval=True)
    async def refund(order_id: str) -> str:
        """Refund an order."""
        return f"refunded {order_id}"

    @server.tool()
    async def lookup(order_id: str) -> str:
        """Look up an order."""
        return f"order {order_id}"

    return server


class TestGateCore:
    @pytest.mark.asyncio
    async def test_ungated_tool_passes_through(self):
        async def never_called(request):
            raise AssertionError("gate must not fire for ungated tools")

        client = TestClient(_server_with_gate(never_called))
        result = await client.call_tool("lookup", {"order_id": "o1"})
        assert result[0].text == "order o1"

    @pytest.mark.asyncio
    async def test_approved_call_proceeds(self):
        seen = {}

        async def approve(request):
            seen["request"] = request
            return ApprovalDecision(approved=True, reviewer_id="alice")

        client = TestClient(_server_with_gate(approve))
        result = await client.call_tool("refund", {"order_id": "o1"})
        assert result[0].text == "refunded o1"
        # The request carried the tool and its validated arguments
        assert seen["request"].tool_name == "refund"
        assert seen["request"].arguments == {"order_id": "o1"}

    @pytest.mark.asyncio
    async def test_denied_call_is_blocked(self):
        async def deny(request):
            return ApprovalDecision(approved=False, reviewer_id="bob", reason="not today")

        client = TestClient(_server_with_gate(deny))
        result = await client.call_tool("refund", {"order_id": "o1"})
        assert "APPROVAL_DENIED" in result[0].text
        assert "not today" in result[0].text
        assert "refunded" not in result[0].text

    @pytest.mark.asyncio
    async def test_bare_bool_callback_is_wrapped(self):
        client = TestClient(_server_with_gate(lambda request: True))
        result = await client.call_tool("refund", {"order_id": "o1"})
        assert result[0].text == "refunded o1"

    @pytest.mark.asyncio
    async def test_timeout_denies_by_default(self):
        async def stall(request):
            await asyncio.sleep(30)

        client = TestClient(_server_with_gate(stall, timeout=0.2))
        result = await client.call_tool("refund", {"order_id": "o1"})
        assert "APPROVAL_DENIED" in result[0].text
        assert "timed out" in result[0].text

    @pytest.mark.asyncio
    async def test_on_timeout_allow_proceeds(self):
        async def stall(request):
            await asyncio.sleep(30)

        client = TestClient(_server_with_gate(stall, timeout=0.2, on_timeout="allow"))
        result = await client.call_tool("refund", {"order_id": "o1"})
        assert result[0].text == "refunded o1"

    @pytest.mark.asyncio
    async def test_modified_arguments_fail_closed(self):
        async def modify(request):
            return ApprovalDecision(
                approved=True, modified_arguments={"order_id": "SOMETHING-ELSE"}
            )

        client = TestClient(_server_with_gate(modify))
        result = await client.call_tool("refund", {"order_id": "o1"})
        assert "APPROVAL_DENIED" in result[0].text
        assert "modified" in result[0].text
        assert "refunded" not in result[0].text

    @pytest.mark.asyncio
    async def test_request_metadata_carries_identity(self):
        seen = {}

        async def approve(request):
            seen["meta"] = request.metadata
            return ApprovalDecision(approved=True)

        server = MCPServer(name="gated")
        server.add_middleware(
            AuthMiddleware(APIKeyAuth(keys={"sk-1": {"client_id": "agent-1", "tenant_id": "acme"}}))
        )
        server.add_middleware(ApprovalGateMiddleware(approve))

        @server.tool(auth=True, requires_approval=True)
        async def refund(order_id: str) -> str:
            """Refund."""
            return "ok"

        client = TestClient(server)
        result = await client.call_tool("refund", {"order_id": "o1"}, headers={"x-api-key": "sk-1"})
        assert result[0].text == "ok"
        assert seen["meta"]["client_id"] == "agent-1"
        assert seen["meta"]["tenant_id"] == "acme"

    def test_config_validation(self):
        with pytest.raises(ValueError, match="on_timeout"):
            ApprovalGateMiddleware(lambda r: True, on_timeout="shrug")
        with pytest.raises(ValueError, match="timeout"):
            ApprovalGateMiddleware(lambda r: True, timeout=0)


class TestUngatedDeclarationFailsLoudly:
    def test_build_raises_without_gate(self):
        server = MCPServer(name="misconfigured")

        @server.tool(requires_approval=True)
        async def refund(order_id: str) -> str:
            """Refund."""
            return "ok"

        with pytest.raises(RuntimeError, match="ApprovalGateMiddleware"):
            server._build_lowlevel_server()

    @pytest.mark.asyncio
    async def test_testclient_raises_without_gate(self):
        server = MCPServer(name="misconfigured")

        @server.tool(requires_approval=True)
        async def refund(order_id: str) -> str:
            """Refund."""
            return "ok"

        with pytest.raises(RuntimeError, match="ApprovalGateMiddleware"):
            await TestClient(server).call_tool("refund", {"order_id": "o1"})


class TestPendingApprover:
    def _build(self) -> tuple[MCPServer, PendingApprover]:
        server = MCPServer(name="pending")
        server.add_middleware(
            AuthMiddleware(
                APIKeyAuth(
                    keys={
                        "sk-caller": {"client_id": "caller-1"},
                        "sk-approver": {"client_id": "human-1", "roles": ["approver"]},
                    }
                )
            )
        )
        approver = PendingApprover(server)
        server.add_middleware(ApprovalGateMiddleware(approver, timeout=5.0))

        @server.tool(auth=True, requires_approval=True)
        async def refund(order_id: str) -> str:
            """Refund."""
            return f"refunded {order_id}"

        return server, approver

    @pytest.mark.asyncio
    async def test_block_list_approve_flow(self):
        server, approver = self._build()
        client = TestClient(server)

        call = asyncio.create_task(
            client.call_tool("refund", {"order_id": "o1"}, headers={"x-api-key": "sk-caller"})
        )
        # Wait until the request lands in the pending store
        for _ in range(100):
            if approver.pending():
                break
            await asyncio.sleep(0.01)
        pending = approver.pending()
        assert len(pending) == 1
        assert pending[0]["tool"] == "refund"
        assert pending[0]["client_id"] == "caller-1"

        # A human with the approver role releases it via the admin tool
        decided = await client.call_tool(
            "approvals_decide",
            {"request_id": pending[0]["request_id"], "approve": True},
            headers={"x-api-key": "sk-approver"},
        )
        assert "true" in decided[0].text.lower() or "resolved" in decided[0].text

        result = await asyncio.wait_for(call, timeout=5)
        assert result[0].text == "refunded o1"
        assert approver.pending() == []

    @pytest.mark.asyncio
    async def test_deny_flow(self):
        server, approver = self._build()
        client = TestClient(server)

        call = asyncio.create_task(
            client.call_tool("refund", {"order_id": "o2"}, headers={"x-api-key": "sk-caller"})
        )
        for _ in range(100):
            if approver.pending():
                break
            await asyncio.sleep(0.01)
        rid = approver.pending()[0]["request_id"]
        assert approver.decide(rid, False, reviewer_id="human-1", reason="fraud check")

        result = await asyncio.wait_for(call, timeout=5)
        assert "APPROVAL_DENIED" in result[0].text
        assert "fraud check" in result[0].text

    @pytest.mark.asyncio
    async def test_admin_tools_are_role_guarded(self):
        server, _ = self._build()
        client = TestClient(server)

        # The caller key lacks the approver role → guard denies
        result = await client.call_tool("approvals_list", {}, headers={"x-api-key": "sk-caller"})
        assert "[]" not in result[0].text  # not an empty-list success
        assert "approver" in result[0].text or "denied" in result[0].text.lower()

        # The approver key lists fine (empty)
        ok = await client.call_tool("approvals_list", {}, headers={"x-api-key": "sk-approver"})
        assert ok[0].text == "[]"

    @pytest.mark.asyncio
    async def test_decide_unknown_request(self):
        server, approver = self._build()
        assert approver.decide("nope", True) is False

    @pytest.mark.asyncio
    async def test_queue_full_denies_immediately(self):
        from promptise.approval import ApprovalRequest

        approver = PendingApprover(max_pending=1)
        req1 = ApprovalRequest(request_id="r1", tool_name="t", arguments={})
        req2 = ApprovalRequest(request_id="r2", tool_name="t", arguments={})

        waiting = asyncio.create_task(approver.request_approval(req1))
        for _ in range(100):
            if approver.pending():
                break
            await asyncio.sleep(0.01)

        decision = await approver.request_approval(req2)
        assert decision.approved is False
        assert "full" in (decision.reason or "")

        approver.decide("r1", True)
        assert (await asyncio.wait_for(waiting, timeout=5)).approved is True


class TestElicitationApprover:
    @pytest.mark.asyncio
    async def test_no_session_fails_closed(self):
        # TestClient has no MCP session → the approver must deny, not allow
        client = TestClient(_server_with_gate(ElicitationApprover()))
        result = await client.call_tool("refund", {"order_id": "o1"})
        assert "APPROVAL_DENIED" in result[0].text
        assert "refunded" not in result[0].text

    @pytest.mark.asyncio
    async def test_live_session_approve_and_deny(self):
        class FakeSession:
            def __init__(self, approve: bool):
                self._approve = approve

            async def send_elicitation_request(self, message, requested_schema):
                return {"approve": self._approve, "reason": "checked"}

        approver = ElicitationApprover()
        from promptise.approval import ApprovalRequest

        req = ApprovalRequest(request_id="r1", tool_name="refund", arguments={"o": 1})

        ctx = RequestContext(server_name="s", tool_name="refund")
        ctx.client = ClientContext(client_id="c1")

        ctx.state["_mcp_session"] = FakeSession(approve=True)
        decision = await approver.request_approval_ctx(req, ctx)
        assert decision.approved is True

        ctx.state["_mcp_session"] = FakeSession(approve=False)
        decision = await approver.request_approval_ctx(req, ctx)
        assert decision.approved is False

    @pytest.mark.asyncio
    async def test_invalid_client_response_denies(self):
        class BadSession:
            async def send_elicitation_request(self, message, requested_schema):
                return {"unexpected": "shape"}

        approver = ElicitationApprover()
        from promptise.approval import ApprovalRequest

        req = ApprovalRequest(request_id="r1", tool_name="refund", arguments={})
        ctx = RequestContext(server_name="s", tool_name="refund")
        ctx.state["_mcp_session"] = BadSession()
        decision = await approver.request_approval_ctx(req, ctx)
        assert decision.approved is False


class TestApprovalRunsAfterGuards:
    """The gate must reject a caller the tool's guards deny BEFORE requesting
    approval — else unauthorized callers spam reviewers and fill the queue."""

    def _build(self):
        from promptise.mcp.server._guards import HasRole

        calls = {"approvals": 0}

        async def counting_handler(request):
            calls["approvals"] += 1
            return ApprovalDecision(approved=True)

        server = MCPServer(name="guarded-gate")
        server.add_middleware(
            AuthMiddleware(
                APIKeyAuth(
                    keys={
                        "sk-viewer": {"client_id": "v1", "roles": ["viewer"]},
                        "sk-admin": {"client_id": "a1", "roles": ["admin"]},
                    }
                )
            )
        )
        server.add_middleware(ApprovalGateMiddleware(counting_handler, timeout=5.0))

        @server.tool(auth=True, guards=[HasRole("admin")], requires_approval=True)
        async def refund(order_id: str) -> str:
            """Refund — admin only."""
            return f"refunded {order_id}"

        return server, calls

    @pytest.mark.asyncio
    async def test_unauthorized_caller_never_triggers_approval(self):
        server, calls = self._build()
        client = TestClient(server)

        # viewer lacks the admin role → denied BEFORE any approval is requested
        denied = await client.call_tool(
            "refund", {"order_id": "o1"}, headers={"x-api-key": "sk-viewer"}
        )
        assert "ACCESS_DENIED" in denied[0].text
        assert "refunded" not in denied[0].text
        assert calls["approvals"] == 0  # the human was never bothered

    @pytest.mark.asyncio
    async def test_authorized_caller_still_gated(self):
        server, calls = self._build()
        client = TestClient(server)

        ok = await client.call_tool("refund", {"order_id": "o1"}, headers={"x-api-key": "sk-admin"})
        assert ok[0].text == "refunded o1"
        assert calls["approvals"] == 1


class TestApprovalSurvivesComposition:
    """requires_approval must not be silently dropped by include_router/mount —
    the build-time invariant has to fire for composed servers too."""

    def test_included_router_gated_tool_requires_gate(self):
        from promptise.mcp.server import MCPRouter

        router = MCPRouter(prefix="billing")

        @router.tool(requires_approval=True)
        async def refund(order_id: str) -> str:
            """Refund."""
            return "ok"

        server = MCPServer(name="composed")
        server.include_router(router)
        # The flag survived composition → the ungated-build invariant fires
        assert server._tool_registry.get("billing_refund").requires_approval is True
        with pytest.raises(RuntimeError, match="ApprovalGateMiddleware"):
            server._build_lowlevel_server()

    @pytest.mark.asyncio
    async def test_included_router_gated_tool_enforced_with_gate(self):
        from promptise.mcp.server import MCPRouter

        seen = {"n": 0}

        async def deny(request):
            seen["n"] += 1
            return ApprovalDecision(approved=False, reason="no")

        router = MCPRouter(prefix="billing")

        @router.tool(requires_approval=True)
        async def refund(order_id: str) -> str:
            """Refund."""
            return "refunded"

        server = MCPServer(name="composed")
        server.include_router(router)
        server.add_middleware(ApprovalGateMiddleware(deny))

        # ... and actually enforces at call time
        result = await TestClient(server).call_tool("billing_refund", {"order_id": "o1"})
        assert "APPROVAL_DENIED" in result[0].text
        assert seen["n"] == 1

    def test_mounted_gated_tool_requires_gate(self):
        from promptise.mcp.server import mount

        child = MCPServer(name="ops")

        @child.tool(requires_approval=True)
        async def delete_all() -> str:
            """Delete everything."""
            return "gone"

        parent = MCPServer(name="parent")
        mount(parent, child, prefix="ops")
        assert parent._tool_registry.get("ops_delete_all").requires_approval is True
        with pytest.raises(RuntimeError, match="ApprovalGateMiddleware"):
            parent._build_lowlevel_server()


class TestSeparationOfDuties:
    """approvals_decide must reject self-approval (four-eyes)."""

    @pytest.mark.asyncio
    async def test_caller_cannot_approve_own_request(self):
        server = MCPServer(name="sod")
        server.add_middleware(
            AuthMiddleware(
                APIKeyAuth(
                    keys={
                        # caller ALSO holds the approver role
                        "sk-dual": {"client_id": "dana", "roles": ["approver"]},
                        "sk-other": {"client_id": "eve", "roles": ["approver"]},
                    }
                )
            )
        )
        approver = PendingApprover(server)
        server.add_middleware(ApprovalGateMiddleware(approver, timeout=5.0))

        @server.tool(auth=True, requires_approval=True)
        async def refund(order_id: str) -> str:
            """Refund."""
            return f"refunded {order_id}"

        client = TestClient(server)
        call = asyncio.create_task(
            client.call_tool("refund", {"order_id": "o1"}, headers={"x-api-key": "sk-dual"})
        )
        for _ in range(100):
            if approver.pending():
                break
            await asyncio.sleep(0.01)
        rid = approver.pending()[0]["request_id"]

        # dana (the caller) tries to approve her own request → refused
        self_decide = await client.call_tool(
            "approvals_decide",
            {"request_id": rid, "approve": True},
            headers={"x-api-key": "sk-dual"},
        )
        assert "four-eyes" in self_decide[0].text or "your own" in self_decide[0].text
        assert approver.pending()  # still pending — not resolved

        # a DIFFERENT approver can release it
        other = await client.call_tool(
            "approvals_decide",
            {"request_id": rid, "approve": True},
            headers={"x-api-key": "sk-other"},
        )
        assert "resolved" in other[0].text.lower() or "true" in other[0].text.lower()
        assert (await asyncio.wait_for(call, timeout=5))[0].text == "refunded o1"


class TestReviewerIdentityOnLivePath:
    """Regression for the test/prod divergence: SoD + reviewer attribution
    must work on the REAL transport closure, not only under TestClient.
    Previously approvals_decide read reviewer from an un-injected ctx param,
    so on live it was always 'unknown-reviewer' and SoD was bypassed."""

    async def _live_call(self, server, name, args, api_key):
        import mcp.types as t

        from promptise.mcp.server._context import set_request_headers

        ll = server._build_lowlevel_server()
        handler = ll.request_handlers[t.CallToolRequest]
        set_request_headers({"x-api-key": api_key})
        try:
            req = t.CallToolRequest(
                method="tools/call",
                params=t.CallToolRequestParams(name=name, arguments=args),
            )
            res = await handler(req)
            return res.root.content[0].text
        finally:
            set_request_headers({})

    @pytest.mark.asyncio
    async def test_self_approval_rejected_on_live_transport(self):
        server = MCPServer(name="live-sod")
        server.add_middleware(
            AuthMiddleware(
                APIKeyAuth(
                    keys={
                        "sk-dana": {"client_id": "dana", "roles": ["approver"]},
                        "sk-eve": {"client_id": "eve", "roles": ["approver"]},
                    }
                )
            )
        )
        approver = PendingApprover(server)
        server.add_middleware(ApprovalGateMiddleware(approver, timeout=5.0))

        @server.tool(auth=True, requires_approval=True)
        async def refund(order_id: str) -> str:
            """Refund."""
            return f"refunded {order_id}"

        call = asyncio.create_task(self._live_call(server, "refund", {"order_id": "o1"}, "sk-dana"))
        for _ in range(200):
            if approver.pending():
                break
            await asyncio.sleep(0.01)
        rid = approver.pending()[0]["request_id"]

        # dana (the caller) tries to self-approve on the LIVE path
        self_decide = await self._live_call(
            server, "approvals_decide", {"request_id": rid, "approve": True}, "sk-dana"
        )
        assert "four-eyes" in self_decide or "your own" in self_decide
        assert approver.pending()  # still pending

        # a different approver releases it, and the reviewer is correctly 'eve'
        pend_before = approver._pending[rid][1]
        await self._live_call(
            server, "approvals_decide", {"request_id": rid, "approve": True}, "sk-eve"
        )
        decision = pend_before.result()
        assert decision.reviewer_id == "eve"  # NOT 'unknown-reviewer'
        assert (await asyncio.wait_for(call, timeout=5)) == "refunded o1"


class TestHandlerCrashFailsClosed:
    @pytest.mark.asyncio
    async def test_raising_handler_denies(self):
        async def boom(request):
            raise RuntimeError("approval backend down")

        client = TestClient(_server_with_gate(boom))
        result = await client.call_tool("refund", {"order_id": "o1"})
        # Fail closed: the handler crash must NOT let the call through
        assert "refunded" not in result[0].text


class TestLiveContextParamInjection:
    """Regression for the framework gap that caused #7: a ctx: RequestContext
    parameter must be populated on the live transport path, not only under
    TestClient."""

    @pytest.mark.asyncio
    async def test_ctx_param_injected_on_live_path(self):
        import mcp.types as t

        server = MCPServer(name="ctx-probe")

        @server.tool()
        async def whoami(x: int, ctx: RequestContext) -> str:
            """Return whether ctx was injected."""
            return "injected" if ctx is not None else "NONE"

        ll = server._build_lowlevel_server()
        handler = ll.request_handlers[t.CallToolRequest]
        req = t.CallToolRequest(
            method="tools/call",
            params=t.CallToolRequestParams(name="whoami", arguments={"x": 1}),
        )
        res = await handler(req)
        assert res.root.content[0].text == "injected"


class TestRouterLevelGate:
    """A gate installed at ROUTER level covers its gated tools — the build
    invariant must not falsely reject a validly-gated composition."""

    def test_router_level_gate_builds(self):
        from promptise.mcp.server import MCPRouter

        async def handler(request):
            return ApprovalDecision(approved=False, reason="no")

        router = MCPRouter(prefix="r", middleware=[ApprovalGateMiddleware(handler)])

        @router.tool(requires_approval=True)
        async def wipe(x: int) -> int:
            """Wipe."""
            return x

        server = MCPServer(name="rr")
        server.include_router(router)
        # Must NOT raise — the gate is in router_middleware, compiled per-tool
        server._build_lowlevel_server()

    @pytest.mark.asyncio
    async def test_router_level_gate_enforces_via_testclient(self):
        from promptise.mcp.server import MCPRouter

        async def deny(request):
            return ApprovalDecision(approved=False, reason="nope")

        router = MCPRouter(prefix="r", middleware=[ApprovalGateMiddleware(deny)])

        @router.tool(requires_approval=True)
        async def wipe(x: int) -> int:
            """Wipe."""
            return x

        server = MCPServer(name="rr")
        server.include_router(router)
        result = await TestClient(server).call_tool("r_wipe", {"x": 1})
        assert "APPROVAL_DENIED" in result[0].text
