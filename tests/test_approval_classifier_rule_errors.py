"""A classifier rule that raises never lets a call through.

A deny rule whose predicate raises denies the call (fail closed); an ask or
allow rule that raises sends it to the human fallback.  Before 1.7.1 a
raising predicate counted as "no match", so a broken deny rule let the very
calls it was written to stop fall through to the allow rules, the read-only
layer or the LLM classifier.
"""

from __future__ import annotations

import logging

import pytest

from promptise.approval import ApprovalDecision, ApprovalRequest
from promptise.approval_classifier import (
    ApprovalRule,
    AutoApprovalClassifier,
    RuleEvaluationError,
)


class _Fallback:
    def __init__(self, *, approved: bool) -> None:
        self.requests: list[ApprovalRequest] = []
        self._approved = approved

    async def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        return ApprovalDecision(approved=self._approved, reviewer_id="human")


async def _boom(request: ApprovalRequest) -> bool:
    raise RuntimeError("rule store unreachable")


def _request(tool: str) -> ApprovalRequest:
    return ApprovalRequest(request_id=f"req-{tool}", tool_name=tool, arguments={})


class TestDenyRuleFailsClosed:
    @pytest.mark.asyncio
    async def test_raising_deny_rule_denies_over_allow_rule(self, caplog):
        fallback = _Fallback(approved=True)
        classifier = AutoApprovalClassifier(
            deny_rules=[ApprovalRule(tool="send_*", predicate=_boom, reason="blocklist")],
            allow_rules=[ApprovalRule(tool="send_*")],
            fallback=fallback,
        )
        with caplog.at_level(logging.WARNING, logger="promptise.approval.classifier"):
            decision = await classifier.request_approval(_request("send_wire"))

        assert decision.approved is False
        assert decision.decided_by == "classifier"
        assert "deny rule could not be evaluated" in (decision.reason or "")
        assert "RuntimeError" in (decision.reason or "")
        assert decision.trace is not None and decision.trace.layer == "deny_rule"
        assert decision.trace.matched_rule is classifier._deny_rules[0]
        assert fallback.requests == []
        assert classifier.stats.deny_rule_hits == 1
        assert classifier.stats.errors == 1
        assert any("fail closed" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_raising_deny_rule_beats_read_only_layer(self):
        classifier = AutoApprovalClassifier(
            deny_rules=[ApprovalRule(predicate=_boom)],
            fallback=_Fallback(approved=True),
        )
        decision = await classifier.request_approval(_request("get_customer"))
        assert decision.approved is False

    @pytest.mark.asyncio
    async def test_deny_rule_for_another_tool_is_not_evaluated(self):
        # The tool filter runs before the predicate: a raising predicate on
        # a rule for another tool does not deny unrelated calls.
        classifier = AutoApprovalClassifier(
            deny_rules=[ApprovalRule(tool="delete_*", predicate=_boom)],
            fallback=_Fallback(approved=True),
        )
        decision = await classifier.request_approval(_request("get_customer"))
        assert decision.approved is True
        assert decision.trace is not None and decision.trace.layer == "read_only"


class TestAllowAndAskRulesEscalate:
    @pytest.mark.asyncio
    async def test_raising_allow_rule_goes_to_the_human(self, caplog):
        fallback = _Fallback(approved=False)
        classifier = AutoApprovalClassifier(
            allow_rules=[ApprovalRule(predicate=_boom)],
            fallback=fallback,
        )
        with caplog.at_level(logging.WARNING, logger="promptise.approval.classifier"):
            # A read-only name: without the escalation the read-only layer
            # would have approved it.
            decision = await classifier.request_approval(_request("get_customer"))

        assert decision.approved is False
        assert len(fallback.requests) == 1
        assert decision.trace is not None and decision.trace.layer == "error"
        assert "allow rule could not be evaluated" in decision.trace.rule_reason
        assert classifier.stats.errors == 1
        assert classifier.stats.allow_rule_hits == 0
        assert classifier.stats.read_only_allows == 0
        assert any("allow rule could not be evaluated" in r.getMessage() for r in caplog.records)

    @pytest.mark.asyncio
    async def test_raising_ask_rule_goes_to_the_human(self):
        fallback = _Fallback(approved=False)
        classifier = AutoApprovalClassifier(
            ask_rules=[ApprovalRule(predicate=_boom)],
            allow_rules=[ApprovalRule(tool="*")],
            fallback=fallback,
        )
        decision = await classifier.request_approval(_request("send_email"))
        assert decision.approved is False
        assert len(fallback.requests) == 1
        assert decision.trace is not None and decision.trace.layer == "ask_rule"


class TestRuleEvaluate:
    @pytest.mark.asyncio
    async def test_evaluate_raises_and_matches_stays_lenient(self):
        rule = ApprovalRule(predicate=_boom, reason="r")
        with pytest.raises(RuleEvaluationError) as info:
            await rule.evaluate(_request("x"))
        assert info.value.rule is rule
        assert isinstance(info.value.error, RuntimeError)
        # The public matches() keeps its documented no-match behaviour.
        assert await rule.matches(_request("x")) is False
