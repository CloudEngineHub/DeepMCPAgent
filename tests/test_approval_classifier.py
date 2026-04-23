"""Tests for AutoApprovalClassifier — explicit decision hierarchy."""

from __future__ import annotations

import pytest

from promptise.approval import ApprovalDecision, ApprovalRequest
from promptise.approval_classifier import (
    DEFAULT_READ_ONLY_PREFIXES,
    ApprovalRule,
    AutoApprovalClassifier,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _RecordingFallback:
    """Fake handler — records every request and returns a configured outcome."""

    def __init__(self, *, approved: bool = True, reason: str = "fallback") -> None:
        self.requests: list[ApprovalRequest] = []
        self._approved = approved
        self._reason = reason

    async def request_approval(self, request: ApprovalRequest) -> ApprovalDecision:
        self.requests.append(request)
        return ApprovalDecision(
            approved=self._approved,
            reviewer_id="fallback",
            reason=self._reason,
        )


def _make_request(tool: str, **kwargs) -> ApprovalRequest:
    return ApprovalRequest(
        request_id=f"req-{tool}",
        tool_name=tool,
        arguments=kwargs.get("arguments", {}),
        agent_id=kwargs.get("agent_id"),
        caller_user_id=kwargs.get("user"),
    )


# ---------------------------------------------------------------------------
# ApprovalRule
# ---------------------------------------------------------------------------


class TestApprovalRule:
    @pytest.mark.asyncio
    async def test_glob_tool_match(self):
        rule = ApprovalRule(tool="get_*")
        assert await rule.matches(_make_request("get_users"))
        assert not await rule.matches(_make_request("delete_users"))

    @pytest.mark.asyncio
    async def test_user_filter(self):
        rule = ApprovalRule(tool="*", user="alice")
        assert await rule.matches(_make_request("anything", user="alice"))
        assert not await rule.matches(_make_request("anything", user="bob"))

    @pytest.mark.asyncio
    async def test_argument_substring(self):
        rule = ApprovalRule(tool="*", argument_contains="rm -rf")
        bad = _make_request("shell", arguments={"cmd": "sudo rm -rf /"})
        good = _make_request("shell", arguments={"cmd": "ls"})
        assert await rule.matches(bad)
        assert not await rule.matches(good)

    @pytest.mark.asyncio
    async def test_predicate(self):
        async def is_alice(req: ApprovalRequest) -> bool:
            return req.caller_user_id == "alice"

        rule = ApprovalRule(predicate=is_alice)
        assert await rule.matches(_make_request("x", user="alice"))
        assert not await rule.matches(_make_request("x", user="bob"))

    @pytest.mark.asyncio
    async def test_predicate_exception_treats_as_no_match(self):
        async def boom(req: ApprovalRequest) -> bool:
            raise RuntimeError("kaboom")

        rule = ApprovalRule(predicate=boom)
        assert await rule.matches(_make_request("x")) is False


# ---------------------------------------------------------------------------
# Decision hierarchy
# ---------------------------------------------------------------------------


class TestDecisionHierarchy:
    @pytest.mark.asyncio
    async def test_layer1_allow_rule_short_circuits(self):
        fb = _RecordingFallback(approved=False)
        clf = AutoApprovalClassifier(
            allow_rules=[ApprovalRule(tool="send_email", reason="trusted")],
            fallback=fb,
        )
        decision = await clf.request_approval(_make_request("send_email"))
        assert decision.approved is True
        assert decision.reason == "trusted"
        assert fb.requests == []
        assert clf.stats.allow_rule_hits == 1
        assert clf.last_trace.layer == "allow_rule"

    @pytest.mark.asyncio
    async def test_layer2_deny_rule_short_circuits(self):
        fb = _RecordingFallback(approved=True)
        clf = AutoApprovalClassifier(
            deny_rules=[ApprovalRule(tool="exec_*", reason="too risky")],
            fallback=fb,
        )
        decision = await clf.request_approval(_make_request("exec_shell"))
        assert decision.approved is False
        assert decision.reason == "too risky"
        assert fb.requests == []
        assert clf.stats.deny_rule_hits == 1

    @pytest.mark.asyncio
    async def test_allow_rules_take_precedence_over_deny(self):
        fb = _RecordingFallback()
        clf = AutoApprovalClassifier(
            allow_rules=[ApprovalRule(tool="exec_safe", reason="vetted")],
            deny_rules=[ApprovalRule(tool="exec_*", reason="risky")],
            fallback=fb,
        )
        decision = await clf.request_approval(_make_request("exec_safe"))
        assert decision.approved is True
        assert decision.reason == "vetted"

    @pytest.mark.asyncio
    async def test_layer3_read_only_auto_allow(self):
        fb = _RecordingFallback(approved=False)
        clf = AutoApprovalClassifier(fallback=fb)

        for tool in ["get_users", "list_files", "read_config", "search_docs"]:
            decision = await clf.request_approval(_make_request(tool))
            assert decision.approved is True
            assert "read-only" in decision.reason

        assert clf.stats.read_only_allows == 4
        assert fb.requests == []

    @pytest.mark.asyncio
    async def test_layer3_disabled_falls_through(self):
        fb = _RecordingFallback(approved=True)
        clf = AutoApprovalClassifier(read_only_auto_allow=False, fallback=fb)
        await clf.request_approval(_make_request("get_users"))
        # Read-only disabled → goes to fallback
        assert len(fb.requests) == 1
        assert clf.stats.read_only_allows == 0

    @pytest.mark.asyncio
    async def test_layer4_llm_classifier_allow(self):
        fb = _RecordingFallback(approved=False)

        async def llm(req):
            return "allow", "looks safe"

        clf = AutoApprovalClassifier(
            llm_classifier=llm,
            read_only_auto_allow=False,
            fallback=fb,
        )
        decision = await clf.request_approval(_make_request("custom_tool"))
        assert decision.approved is True
        assert decision.reason == "looks safe"
        assert clf.stats.llm_allows == 1
        assert fb.requests == []

    @pytest.mark.asyncio
    async def test_layer4_llm_classifier_deny(self):
        fb = _RecordingFallback(approved=True)

        async def llm(req):
            return "deny", "looks dangerous"

        clf = AutoApprovalClassifier(
            llm_classifier=llm,
            read_only_auto_allow=False,
            fallback=fb,
        )
        decision = await clf.request_approval(_make_request("custom_tool"))
        assert decision.approved is False
        assert decision.reason == "looks dangerous"
        assert clf.stats.llm_denies == 1

    @pytest.mark.asyncio
    async def test_layer4_llm_escalate_falls_to_fallback(self):
        fb = _RecordingFallback(approved=True)

        async def llm(req):
            return "escalate", "not sure"

        clf = AutoApprovalClassifier(
            llm_classifier=llm,
            read_only_auto_allow=False,
            fallback=fb,
        )
        decision = await clf.request_approval(_make_request("custom_tool"))
        assert decision.approved is True  # comes from fallback
        assert len(fb.requests) == 1
        assert clf.stats.llm_escalations == 1
        assert clf.stats.fallback_allows == 1

    @pytest.mark.asyncio
    async def test_layer5_fallback_when_no_rules_match(self):
        fb = _RecordingFallback(approved=False, reason="human said no")
        clf = AutoApprovalClassifier(read_only_auto_allow=False, fallback=fb)
        decision = await clf.request_approval(_make_request("modify_data"))
        assert decision.approved is False
        assert decision.reason == "human said no"
        assert clf.stats.fallback_denies == 1

    @pytest.mark.asyncio
    async def test_classifier_exception_falls_back(self):
        fb = _RecordingFallback(approved=True)

        async def boom(req):
            raise RuntimeError("classifier broken")

        clf = AutoApprovalClassifier(
            llm_classifier=boom,
            read_only_auto_allow=False,
            fallback=fb,
        )
        decision = await clf.request_approval(_make_request("custom_tool"))
        # Falls back; doesn't raise
        assert decision.approved is True
        assert clf.stats.errors == 1


# ---------------------------------------------------------------------------
# Stats
# ---------------------------------------------------------------------------


class TestClassifierStats:
    @pytest.mark.asyncio
    async def test_reset_stats(self):
        fb = _RecordingFallback()
        clf = AutoApprovalClassifier(fallback=fb)
        await clf.request_approval(_make_request("get_x"))
        assert clf.stats.read_only_allows == 1
        clf.reset_stats()
        assert clf.stats.read_only_allows == 0


class TestDefaultReadOnlyPrefixes:
    def test_includes_common_prefixes(self):
        for p in ("get_", "list_", "read_", "search_"):
            assert p in DEFAULT_READ_ONLY_PREFIXES
