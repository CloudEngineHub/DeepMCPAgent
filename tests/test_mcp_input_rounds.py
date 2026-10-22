"""Elicitation and per-session state on the 2026-07-28 protocol revision.

2026-07-28 has no sessions and no server-to-client requests.  An
``Elicitor`` question ends the call with an ``InputRequiredResult``; the
client answers and calls again (input rounds).  A tool that takes a
``SessionState`` refuses such requests with ``SESSION_REQUIRED`` instead of
starting from empty state on every call.
"""

from __future__ import annotations

import asyncio
import gc
import json
import logging
from typing import Any

import httpx
import pytest
from _live_server import serve_asgi

from promptise.mcp._compat import MCP_V2
from promptise.mcp.client import MCPClient
from promptise.mcp.server import (
    ApprovalGateMiddleware,
    Depends,
    ElicitationApprover,
    Elicitor,
    MCPServer,
    SessionState,
)
from promptise.mcp.server._elicitation import InputRequired, InputRound, current_round
from promptise.mcp.server._session_state import SessionManager

TIMEOUT = 15
CONFIRM = {"type": "object", "properties": {"confirm": {"type": "boolean"}}}
WRAP = {"type": "object", "properties": {"wrap": {"type": "boolean"}}}
needs_v2 = pytest.mark.skipif(not MCP_V2, reason="needs the mcp 2.x SDK (2026-07-28)")


def _text(result: Any) -> str:
    return result.content[0].text


def _shop(runs: list[str]) -> MCPServer:
    server = MCPServer("shop")

    @server.tool()
    async def checkout(cart: str, elicit: Elicitor = Depends(Elicitor)) -> str:
        """Place the order for a cart."""
        runs.append(cart)
        confirm = await elicit.ask(f"Place the order for {cart}?", CONFIRM)
        if not confirm or not confirm.get("confirm"):
            return f"not placed: {elicit.last_failure}"
        wrap = await elicit.ask("Gift wrap?", WRAP)
        return f"placed {cart}, wrap={bool(wrap and wrap.get('wrap'))}"

    @server.tool()
    async def add_item(sku: str, session: SessionState = Depends(SessionState)) -> str:
        """Add an item to the cart."""
        items = session.get("items", []) + [sku]
        session.set("items", items)
        return json.dumps(items)

    return server


# ---------------------------------------------------------------------------
# No SDK client needed
# ---------------------------------------------------------------------------


class TestInputRound:
    def test_the_same_question_gets_the_same_key_each_round(self):
        first, second = InputRound(), InputRound()
        assert first.key("Place it?", CONFIRM) == second.key("Place it?", CONFIRM)
        # Asked twice in one run: two questions.
        assert first.key("Place it?", CONFIRM) != second.key("Gift wrap?", WRAP)
        again = InputRound()
        keys = [again.key("Sure?", CONFIRM), again.key("Sure?", CONFIRM)]
        assert keys[0] != keys[1]

    def test_answers_come_from_responses_and_state(self):
        asked = InputRound()
        k1 = asked.key("Place it?", CONFIRM)
        k2 = asked.key("Gift wrap?", WRAP)
        first = InputRound(input_responses={k1: {"action": "accept", "content": {"confirm": True}}})
        state = first.state()
        assert state is not None
        second = InputRound(
            input_responses={k2: {"action": "decline"}, "someone-else": {"action": "accept"}},
            request_state=state,
        )
        assert second.answers == {
            k1: {"action": "accept", "content": {"confirm": True}},
            k2: {"action": "decline", "content": None},
        }

    def test_foreign_state_is_ignored(self, caplog):
        with caplog.at_level(logging.WARNING, logger="promptise.server"):
            assert InputRound(request_state="not json").answers == {}
            assert InputRound(request_state='{"v": 99}').answers == {}
        assert "did not write" in caplog.text

    @pytest.mark.asyncio
    async def test_unanswered_question_raises_the_signal(self):
        elicitor = Elicitor()
        elicitor._bind(object(), 7)
        round_ = InputRound(client_capabilities=type("Caps", (), {"elicitation": object()})())
        token = current_round.set(round_)
        try:
            with pytest.raises(InputRequired) as raised:
                await elicitor.ask("Place it?", CONFIRM)
        finally:
            current_round.reset(token)
        # Not an Exception: `except Exception` in tools and middleware lets it through.
        assert not isinstance(raised.value, Exception)
        assert raised.value.key in InputRound().key("Place it?", CONFIRM)

    @pytest.mark.asyncio
    async def test_client_without_the_capability_gets_none_and_a_reason(self):
        elicitor = Elicitor()
        elicitor._bind(object(), 7)
        token = current_round.set(InputRound(client_capabilities=None))
        try:
            assert await elicitor.ask("Place it?", CONFIRM) is None
        finally:
            current_round.reset(token)
        assert elicitor.last_failure == "the client did not declare the elicitation capability"


class TestSessionManager:
    def test_state_follows_the_connection_object(self):
        class Connection:
            pass

        manager = SessionManager()
        first, second = Connection(), Connection()
        manager.for_connection(first).set("items", ["mug"])
        assert manager.for_connection(first).get("items") == ["mug"]
        assert manager.for_connection(second).get("items") is None
        del first
        gc.collect()
        # Dropped with its connection: a new connection can't inherit it.
        assert manager.active_sessions == 1


# ---------------------------------------------------------------------------
# Handshake connections and stateless servers (both SDKs)
# ---------------------------------------------------------------------------


class TestSessionStateOverHttp:
    async def test_state_persists_per_connection_and_is_not_shared(self):
        async with serve_asgi(_shop([]).asgi_app()) as port:
            url = f"http://127.0.0.1:{port}/mcp"
            async with MCPClient(url=url) as client:
                await client.call_tool("add_item", {"sku": "mug"})
                result = await client.call_tool("add_item", {"sku": "tea"})
                assert json.loads(_text(result)) == ["mug", "tea"]
            async with MCPClient(url=url) as other:
                result = await other.call_tool("add_item", {"sku": "pen"})
                assert json.loads(_text(result)) == ["pen"]

    async def test_stateless_server_refuses_session_state(self):
        async with serve_asgi(_shop([]).asgi_app(stateless=True)) as port:
            async with MCPClient(url=f"http://127.0.0.1:{port}/mcp") as client:
                result = await client.call_tool("add_item", {"sku": "mug"})
        error = json.loads(_text(result))["error"]
        assert error["code"] == "SESSION_REQUIRED"
        assert "stateless" in error["message"]


# ---------------------------------------------------------------------------
# 2026-07-28 with the official mcp 2.x client
# ---------------------------------------------------------------------------


@needs_v2
class TestInputRoundsWithTheSdkClient:
    @staticmethod
    def _answers(log: list[str], **content: Any):
        from mcp.types import ElicitResult

        async def answer(context: Any, params: Any) -> Any:
            log.append(params.message)
            if params.message == "Gift wrap?":
                return ElicitResult(action="accept", content={"wrap": True})
            if content.get("decline"):
                return ElicitResult(action="decline")
            return ElicitResult(action="accept", content={"confirm": True, "approve": True})

        return answer

    async def test_two_questions_over_three_rounds(self):
        from mcp.client import Client

        runs: list[str] = []
        asked: list[str] = []
        async with serve_asgi(_shop(runs).asgi_app()) as port:
            async with Client(
                f"http://127.0.0.1:{port}/mcp", elicitation_callback=self._answers(asked)
            ) as client:
                assert client.protocol_version == "2026-07-28"
                result = await asyncio.wait_for(
                    client.call_tool("checkout", {"cart": "c1"}), TIMEOUT
                )
        assert _text(result) == "placed c1, wrap=True"
        assert asked == ["Place the order for c1?", "Gift wrap?"]  # each asked once
        assert runs == ["c1", "c1", "c1"]  # the tool runs once per round

    async def test_a_declined_question(self):
        from mcp.client import Client

        async with serve_asgi(_shop([]).asgi_app()) as port:
            async with Client(
                f"http://127.0.0.1:{port}/mcp",
                elicitation_callback=self._answers([], decline=True),
            ) as client:
                result = await asyncio.wait_for(
                    client.call_tool("checkout", {"cart": "c1"}), TIMEOUT
                )
        assert _text(result) == "not placed: the client answered 'decline'"

    async def test_a_client_without_elicitation_is_told_why(self, caplog):
        from mcp.client import Client

        server = _shop([])
        server.add_middleware(ApprovalGateMiddleware(ElicitationApprover(), timeout=5))

        @server.tool(requires_approval=True)
        async def refund(order_id: str) -> str:
            """Refund an order."""
            return f"refunded {order_id}"

        with caplog.at_level(logging.WARNING, logger="promptise.server"):
            async with serve_asgi(server.asgi_app()) as port:
                async with Client(f"http://127.0.0.1:{port}/mcp") as client:
                    checkout = await client.call_tool("checkout", {"cart": "c1"})
                    denied = await client.call_tool("refund", {"order_id": "o1"})
        reason = "the client did not declare the elicitation capability"
        assert _text(checkout) == f"not placed: {reason}"
        assert denied.is_error and reason in json.loads(_text(denied))["error"]["message"]
        assert "did not declare the elicitation capability" in caplog.text

    async def test_session_state_is_refused_with_a_reason(self):
        from mcp.client import Client

        async with serve_asgi(_shop([]).asgi_app()) as port:
            async with Client(f"http://127.0.0.1:{port}/mcp") as client:
                result = await client.call_tool("add_item", {"sku": "mug"})
            async with Client(f"http://127.0.0.1:{port}/mcp", mode="legacy") as legacy:
                await legacy.call_tool("add_item", {"sku": "mug"})
                kept = await legacy.call_tool("add_item", {"sku": "tea"})
        error = json.loads(_text(result))["error"]
        assert result.is_error and error["code"] == "SESSION_REQUIRED"
        assert "2026-07-28" in error["message"] and "mode='legacy'" in error["message"]
        assert json.loads(_text(kept)) == ["mug", "tea"]

    async def test_request_state_the_server_did_not_seal_is_refused(self):
        from mcp.client import Client

        async with serve_asgi(_shop([]).asgi_app()) as port:
            url = f"http://127.0.0.1:{port}/mcp"
            async with Client(url) as client:  # learn the routing headers the SDK sends
                assert client.protocol_version == "2026-07-28"
            forged = json.dumps({"v": 1, "answers": {"promptise.elicit.x.1": {"action": "accept"}}})
            body = {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "checkout",
                    "arguments": {"cart": "c1"},
                    "requestState": forged,
                    "_meta": {
                        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                        "io.modelcontextprotocol/clientCapabilities": {"elicitation": {}},
                    },
                },
            }
            headers = {
                "Accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": "2026-07-28",
                "Mcp-Method": "tools/call",
                "Mcp-Name": "checkout",
            }
            async with httpx.AsyncClient(timeout=TIMEOUT) as http:
                reply = await http.post(url, json=body, headers=headers)
        text = reply.text
        assert "Invalid or expired requestState" in text
        assert "placed" not in text
