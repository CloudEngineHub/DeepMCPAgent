"""Every approval request id takes exactly one decision.

The docs said request ids are "tracked to prevent replay attacks", but the
agent's gate only wrote ids to a set nobody read: a second decision for the
same id, a stale poll response for another request, or a replayed signed
webhook request was accepted.  These tests pin what is enforced since 1.7.1.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time

import httpx
import pytest
from langchain_core.tools import tool

from promptise.approval import (
    ApprovalDecision,
    ApprovalPolicy,
    ApprovalReplayError,
    ApprovalRequest,
    QueueApprovalHandler,
    RequestIdCache,
    WebhookApprovalHandler,
    verify_webhook_signature,
    wrap_tools_with_approval,
)
from promptise.mcp.server import ApprovalGateMiddleware, MCPServer, PendingApprover, TestClient


@tool
async def issue_refund(order_id: str) -> str:
    """Refund an order."""
    return f"refunded {order_id}"


class _Approver:
    def __init__(self) -> None:
        self.calls = 0

    async def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        self.calls += 1
        return ApprovalDecision(approved=True, reviewer_id="alice")


async def _wait_for(predicate, *, attempts: int = 200) -> None:
    for _ in range(attempts):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not reached")


# ---------------------------------------------------------------------------
# RequestIdCache
# ---------------------------------------------------------------------------


class TestRequestIdCache:
    def test_claim_once(self):
        cache = RequestIdCache()
        assert cache.claim("a") is True
        assert cache.claim("a") is False
        assert "a" in cache and "b" not in cache
        assert len(cache) == 1

    def test_ids_expire_after_their_ttl(self, monkeypatch):
        now = [1000.0]
        monkeypatch.setattr("promptise.approval.time.monotonic", lambda: now[0])
        cache = RequestIdCache(ttl=10)
        assert cache.claim("a")
        assert cache.claim("b", ttl=100)
        now[0] += 11
        assert "a" not in cache
        assert "b" in cache
        assert cache.claim("a") is True  # forgotten, so usable again

    def test_size_is_bounded(self):
        cache = RequestIdCache(max_size=3)
        for rid in "abcd":
            assert cache.claim(rid)
        assert len(cache) == 3
        assert "a" not in cache  # the oldest is dropped first
        assert "d" in cache

    def test_rejects_bad_arguments(self):
        with pytest.raises(ValueError):
            RequestIdCache(ttl=0)
        with pytest.raises(ValueError):
            RequestIdCache(max_size=0)


# ---------------------------------------------------------------------------
# The agent's gate
# ---------------------------------------------------------------------------


class TestAgentGate:
    @pytest.mark.asyncio
    async def test_reused_request_id_is_refused(self, monkeypatch, caplog):
        # Force the gate to produce the same id twice, as a replay would.
        monkeypatch.setattr("promptise.approval.secrets.token_hex", lambda n=32: "f" * 32)
        handler = _Approver()
        policy = ApprovalPolicy(tools=["issue_refund"], handler=handler, redact_sensitive=False)
        decisions: list[ApprovalDecision] = []
        policy.on_decision = lambda request, decision: decisions.append(decision)
        (wrapped,) = wrap_tools_with_approval([issue_refund], policy)

        assert await wrapped.ainvoke({"order_id": "A-1"}) == "refunded A-1"
        with caplog.at_level(logging.WARNING, logger="promptise.approval"):
            second = await wrapped.ainvoke({"order_id": "A-1"})

        assert second.startswith("DENIED:")
        assert "already decided" in second
        assert handler.calls == 1  # the reviewer was not asked again
        assert decisions[-1].decided_by == "gate"
        assert any("refused a second decision" in r.getMessage() for r in caplog.records)

    def test_gate_remembers_ids_for_the_policy_timeout(self):
        policy = ApprovalPolicy(tools=["issue_refund"], handler=_Approver(), timeout=42)
        (wrapped,) = wrap_tools_with_approval([issue_refund], policy)
        assert wrapped._state.used_request_ids.ttl == 42


# ---------------------------------------------------------------------------
# QueueApprovalHandler
# ---------------------------------------------------------------------------


class TestQueueHandler:
    @pytest.mark.asyncio
    async def test_second_decision_is_refused_and_first_stands(self, caplog):
        handler = QueueApprovalHandler()
        request = ApprovalRequest(request_id="q1", tool_name="t", arguments={}, timeout=5)
        waiter = asyncio.create_task(handler.request_approval(request))
        await _wait_for(lambda: "q1" in handler._pending)

        handler.submit_decision("q1", ApprovalDecision(approved=False, reviewer_id="bob"))
        with caplog.at_level(logging.WARNING, logger="promptise.approval"):
            with pytest.raises(ApprovalReplayError):
                handler.submit_decision("q1", ApprovalDecision(approved=True, reviewer_id="eve"))
        decision = await asyncio.wait_for(waiter, timeout=5)

        assert decision.approved is False and decision.reviewer_id == "bob"
        assert any("refused a second decision" in r.getMessage() for r in caplog.records)
        # After the request finished, a late duplicate is still a replay
        # (and still a KeyError for callers that only catch that).
        with pytest.raises(KeyError, match="already decided"):
            handler.submit_decision("q1", ApprovalDecision(approved=True))
        # Unknown ids keep their old error.
        with pytest.raises(KeyError, match="No pending"):
            handler.submit_decision("never", ApprovalDecision(approved=True))

    @pytest.mark.asyncio
    async def test_decided_request_id_cannot_be_asked_again(self):
        handler = QueueApprovalHandler()
        request = ApprovalRequest(request_id="q2", tool_name="t", arguments={}, timeout=5)
        waiter = asyncio.create_task(handler.request_approval(request))
        await _wait_for(lambda: "q2" in handler._pending)
        handler.submit_decision("q2", ApprovalDecision(approved=True))
        await waiter
        with pytest.raises(ApprovalReplayError):
            await handler.request_approval(request)


# ---------------------------------------------------------------------------
# WebhookApprovalHandler and verify_webhook_signature
# ---------------------------------------------------------------------------


class TestWebhookHandler:
    @staticmethod
    def _handler(poll_answers: list[dict]) -> WebhookApprovalHandler:
        answers = iter(poll_answers)

        def service(request: httpx.Request) -> httpx.Response:
            if request.method == "POST":
                return httpx.Response(202)
            return httpx.Response(200, json=next(answers))

        return WebhookApprovalHandler(
            url="http://approvals.test/requests",
            # A fixed poll URL: the service answers for whichever request it
            # decided last.
            poll_url="http://approvals.test/decisions",
            secret="s3cret",
            poll_interval=0.5,
            allow_private_networks=True,
            http_client=httpx.AsyncClient(transport=httpx.MockTransport(service)),
        )

    @pytest.mark.asyncio
    async def test_poll_response_for_another_request_is_ignored(self, caplog):
        request = ApprovalRequest(request_id="w2", tool_name="t", arguments={}, timeout=10)
        handler = self._handler(
            [
                {"request_id": "w1", "approved": True},  # stale decision for another request
                {"request_id": "w2", "approved": False, "reviewer_id": "bob"},
            ]
        )
        with caplog.at_level(logging.WARNING, logger="promptise.approval"):
            decision = await handler.request_approval(request)
        assert decision.approved is False
        assert decision.reviewer_id == "bob"
        assert any("ignored a poll response" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_responses_without_request_id_still_work(self):
        request = ApprovalRequest(request_id="w3", tool_name="t", arguments={}, timeout=10)
        decision = await self._handler([{"approved": True}]).request_approval(request)
        assert decision.approved is True

    @pytest.mark.asyncio
    async def test_decided_request_id_cannot_be_decided_again(self):
        request = ApprovalRequest(request_id="w4", tool_name="t", arguments={}, timeout=10)
        handler = self._handler([{"approved": True}, {"approved": True}])
        assert (await handler.request_approval(request)).approved is True
        with pytest.raises(ApprovalReplayError):
            await handler.request_approval(request)


class TestVerifyWebhookSignatureReplay:
    @staticmethod
    def _signed(request_id: str) -> tuple[bytes, str]:
        request = ApprovalRequest(request_id=request_id, tool_name="t", arguments={"a": 1})
        return json.dumps(request.to_dict()).encode(), request.compute_hmac("s3cret")

    def test_replayed_signed_request_is_rejected(self, caplog):
        cache = RequestIdCache()
        body, signature = self._signed("v1")
        assert verify_webhook_signature(body, signature, "s3cret", max_age=300, replay_cache=cache)
        with caplog.at_level(logging.WARNING, logger="promptise.approval"):
            assert not verify_webhook_signature(
                body, signature, "s3cret", max_age=300, replay_cache=cache
            )
        assert any("refused a second decision" in r.getMessage() for r in caplog.records)
        # A different request still verifies.
        other_body, other_signature = self._signed("v2")
        assert verify_webhook_signature(
            other_body, other_signature, "s3cret", max_age=300, replay_cache=cache
        )

    def test_ids_are_remembered_for_twice_max_age(self, monkeypatch):
        cache = RequestIdCache()
        body, signature = self._signed("v3")
        now = [time.monotonic()]
        monkeypatch.setattr("promptise.approval.time.monotonic", lambda: now[0])
        assert verify_webhook_signature(body, signature, "s3cret", max_age=60, replay_cache=cache)
        now[0] += 119
        assert "v3" in cache
        now[0] += 2
        assert "v3" not in cache

    def test_a_bad_signature_does_not_use_up_the_id(self):
        cache = RequestIdCache()
        body, signature = self._signed("v4")
        assert not verify_webhook_signature(body, "0" * 64, "s3cret", replay_cache=cache)
        assert "v4" not in cache
        assert verify_webhook_signature(body, signature, "s3cret", replay_cache=cache)

    def test_without_a_cache_behaviour_is_unchanged(self):
        body, signature = self._signed("v5")
        assert verify_webhook_signature(body, signature, "s3cret")
        assert verify_webhook_signature(body, signature, "s3cret")


# ---------------------------------------------------------------------------
# The MCP server: PendingApprover and ApprovalGateMiddleware
# ---------------------------------------------------------------------------


class TestPendingApprover:
    @pytest.mark.asyncio
    async def test_second_decision_is_refused_and_logged(self, caplog):
        approver = PendingApprover()
        request = ApprovalRequest(request_id="p1", tool_name="t", arguments={}, timeout=5)
        waiter = asyncio.create_task(approver.request_approval(request))
        await _wait_for(lambda: bool(approver.pending()))

        assert approver.decide("p1", False, reviewer_id="bob") is True
        with caplog.at_level(logging.WARNING, logger="promptise.server.approval"):
            assert approver.decide("p1", True, reviewer_id="eve") is False
        decision = await asyncio.wait_for(waiter, timeout=5)
        assert decision.approved is False and decision.reviewer_id == "bob"
        assert approver.was_decided("p1")
        assert any("refused a second decision" in r.getMessage() for r in caplog.records)

        # Asking again under the decided id is refused without queueing it.
        again = await approver.request_approval(request)
        assert again.approved is False
        assert "already used" in (again.reason or "")
        assert approver.pending() == []


class TestServerGate:
    @pytest.mark.asyncio
    async def test_reused_request_id_is_refused(self, monkeypatch):
        monkeypatch.setattr(
            "promptise.mcp.server._approval_gate.secrets.token_hex", lambda n=32: "e" * 32
        )
        handler = _Approver()
        server = MCPServer(name="gated")
        server.add_middleware(ApprovalGateMiddleware(handler))

        @server.tool(requires_approval=True)
        async def refund(order_id: str) -> str:
            """Refund an order."""
            return f"refunded {order_id}"

        client = TestClient(server)
        first = await client.call_tool("refund", {"order_id": "o1"})
        second = await client.call_tool("refund", {"order_id": "o1"})
        assert first[0].text == "refunded o1"
        assert "APPROVAL_DENIED" in second[0].text
        assert "already decided" in second[0].text
        assert handler.calls == 1  # the reviewer was not asked again
