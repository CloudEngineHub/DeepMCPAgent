"""Tool calls refused before they ran are on the trace.

* A call the autonomy budget refuses (``max_irreversible_per_run``) is a
  ``tool.call`` + ``tool.result`` with ``status="denied"`` and the reason;
  before, only the journal and the ``permission_denied`` hook saw it.
* An approval denial marks the call's ``tool.call`` and ``tool.result`` as
  denied, and the ``approval.*`` events reach the timeline (and so the
  ``CALLBACK`` transporter), not only the event notifier.
* ``promptise run --devtools`` shows and counts refused calls.
"""

from __future__ import annotations

import asyncio
import io
from collections.abc import Callable
from typing import Any

import pytest
from rich.console import Console

from promptise import build_agent, tool
from promptise.approval import ApprovalDecision, ApprovalPolicy, CallbackApprovalHandler
from promptise.observability import TimelineEntry
from promptise.observability_config import ObservabilityConfig, TransporterType
from promptise.runtime import AgentProcess, BudgetConfig, ProcessConfig
from promptise.runtime.config import ToolCostAnnotation
from promptise.runtime.triggers.base import TriggerEvent
from promptise.testing import ScriptedModel, ToolCall


@pytest.fixture(autouse=True)
def _no_provider_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)


async def _wait_for(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def _wire_tool(ran: list[int]) -> Any:
    @tool
    def wire_money(amount: int) -> str:
        """Wire money."""
        ran.append(amount)
        return "sent"

    return wire_money


def _callback_observe(seen: list[TimelineEntry], **kwargs: Any) -> ObservabilityConfig:
    return ObservabilityConfig(
        transporters=[TransporterType.CALLBACK], on_event=seen.append, **kwargs
    )


def _of(seen: list[TimelineEntry], event_type: str) -> list[TimelineEntry]:
    return [e for e in seen if e.event_type.value == event_type]


# ---------------------------------------------------------------------------
# Autonomy budget
# ---------------------------------------------------------------------------


async def _run_budgeted(
    seen: list[TimelineEntry], **observe: Any
) -> tuple[list[int], AgentProcess]:
    ran: list[int] = []
    model = ScriptedModel([ToolCall("wire_money", {"amount": i}) for i in range(1, 5)] + ["done"])
    process = AgentProcess(
        "payer",
        ProcessConfig(
            model=model,
            extra_tools=[_wire_tool(ran)],
            budget=BudgetConfig(
                enabled=True,
                max_irreversible_per_run=3,
                tool_costs={"wire_money": ToolCostAnnotation(irreversible=True)},
            ),
            observe=_callback_observe(seen, **observe),
        ),
    )
    await process.start()
    try:
        await process.inject(TriggerEvent(trigger_id="t", trigger_type="manual", payload={}))
        await _wait_for(lambda: bool(_of(seen, "agent.output")))
        stats = process._agent.collector.get_stats()
    finally:
        await process.stop()
    process._test_stats = stats  # type: ignore[attr-defined]
    return ran, process


class TestBudgetRefusalIsTraced:
    async def test_fourth_irreversible_call_is_a_denied_tool_call(self) -> None:
        seen: list[TimelineEntry] = []
        ran, _ = await _run_budgeted(seen)
        assert ran == [1, 2, 3]

        calls = _of(seen, "tool.call")
        results = _of(seen, "tool.result")
        assert len(calls) == 4 and len(results) == 4
        refused_call, refused_result = calls[-1], results[-1]
        for entry in (refused_call, refused_result):
            assert entry.metadata["status"] == "denied"
            assert entry.metadata["denied_by"] == "budget"
            assert entry.metadata["tool_name"] == "wire_money"
            assert entry.metadata["limit_name"] == "max_irreversible_per_run"
            assert entry.metadata["reason"] == (
                "budget limit max_irreversible_per_run reached (4 > 3)"
            )
        assert refused_call.metadata["arguments"] == {"amount": 4}
        # Paired like any call, and part of the same agent run.
        assert refused_call.metadata["run_id"] == refused_result.metadata["run_id"]
        [run] = _of(seen, "agent.input")
        assert refused_call.parent_id == run.entry_id
        assert refused_call.agent_id == "payer"
        # The calls that ran are not marked.
        assert all("status" not in e.metadata for e in results[:3])

    async def test_stats_count_refusals_apart_from_errors(self) -> None:
        seen: list[TimelineEntry] = []
        _, process = await _run_budgeted(seen)
        stats = process._test_stats  # type: ignore[attr-defined]
        assert stats["tool_call_count"] == 4
        assert stats["tool_denied_count"] == 1
        assert stats["error_count"] == 0
        [output] = _of(seen, "agent.output")
        assert output.metadata["tool_call_count"] == 4

    async def test_arguments_follow_record_tool_io(self) -> None:
        seen: list[TimelineEntry] = []
        await _run_budgeted(seen, record_tool_io=False)
        refused = _of(seen, "tool.call")[-1]
        assert refused.metadata["status"] == "denied"
        assert "arguments" not in refused.metadata
        assert refused.metadata["arguments_length"] > 0

    async def test_without_observability_nothing_is_recorded(self) -> None:
        from promptise.observability import _record_denied_tool_call

        assert _record_denied_tool_call("t", reason="r", denied_by="budget") is False


# ---------------------------------------------------------------------------
# Approval
# ---------------------------------------------------------------------------


async def _run_approval(answers: list[bool], seen: list[TimelineEntry]) -> list[int]:
    ran: list[int] = []

    async def decide(request: Any) -> ApprovalDecision:
        approved = answers.pop(0)
        return ApprovalDecision(
            approved=approved, reason=None if approved else "too much", reviewer_id="rev-1"
        )

    model = ScriptedModel(
        [ToolCall("wire_money", {"amount": 1}), ToolCall("wire_money", {"amount": 2}), "done"]
    )
    agent = await build_agent(
        model=model,
        extra_tools=[_wire_tool(ran)],
        approval=ApprovalPolicy(tools=["wire_money"], handler=CallbackApprovalHandler(decide)),
        observe=_callback_observe(seen),
    )
    try:
        await agent.ainvoke({"messages": [{"role": "user", "content": "pay"}]})
    finally:
        await agent.shutdown()
    return ran


class TestApprovalOnTheTimeline:
    async def test_approval_events_reach_the_callback_transporter(self) -> None:
        seen: list[TimelineEntry] = []
        ran = await _run_approval([False, True], seen)
        assert ran == [2]
        requested = _of(seen, "approval.requested")
        assert [e.metadata["tool_name"] for e in requested] == ["wire_money", "wire_money"]
        assert requested[0].metadata["arguments"] == {"amount": 1}
        [denied] = _of(seen, "approval.denied")
        assert denied.metadata["reason"] == "too much"
        assert denied.metadata["decided_by"] == "reviewer"
        [granted] = _of(seen, "approval.granted")
        assert granted.metadata["reviewer"] == "rev-1"
        [run] = _of(seen, "agent.input")
        assert {e.parent_id for e in [*requested, denied, granted]} == {run.entry_id}
        assert denied.metadata["request_id"] == requested[0].metadata["request_id"]

    async def test_denied_call_result_is_marked_denied(self) -> None:
        seen: list[TimelineEntry] = []
        await _run_approval([False, True], seen)
        first, second = _of(seen, "tool.result")
        assert first.metadata["status"] == "denied"
        assert first.metadata["denied_by"] == "approval"
        assert first.metadata["decided_by"] == "reviewer"
        assert first.metadata["reason"] == "too much"
        assert "status" not in second.metadata
        assert len(_of(seen, "tool.call")) == 2

    async def test_denied_call_is_marked_denied_too(self) -> None:
        # The gate decides after the tool.call is recorded; the entry is
        # marked like a call the budget refused.
        seen: list[TimelineEntry] = []
        await _run_approval([False, True], seen)
        first, second = _of(seen, "tool.call")
        assert first.metadata["status"] == "denied"
        assert first.metadata["denied_by"] == "approval"
        assert first.metadata["reason"] == "too much"
        assert first.metadata["arguments"] == {"amount": 1}
        assert "status" not in second.metadata

    async def test_approval_timeout_is_on_the_timeline(self) -> None:
        seen: list[TimelineEntry] = []

        async def never(request: Any) -> ApprovalDecision:
            await asyncio.sleep(10)
            raise AssertionError("not reached")

        model = ScriptedModel([ToolCall("wire_money", {"amount": 1}), "done"])
        agent = await build_agent(
            model=model,
            extra_tools=[_wire_tool([])],
            approval=ApprovalPolicy(
                tools=["wire_money"], handler=CallbackApprovalHandler(never), timeout=0.05
            ),
            observe=_callback_observe(seen),
        )
        try:
            await agent.ainvoke({"messages": [{"role": "user", "content": "pay"}]})
        finally:
            await agent.shutdown()
        [timeout] = _of(seen, "approval.timeout")
        assert timeout.metadata["tool_name"] == "wire_money"
        [result] = _of(seen, "tool.result")
        assert result.metadata["status"] == "denied"
        assert result.metadata["decided_by"] == "gate"


# ---------------------------------------------------------------------------
# promptise run --devtools
# ---------------------------------------------------------------------------


def _denied_entries() -> list[dict[str, Any]]:
    meta = {
        "tool_name": "wire_money",
        "run_id": "r4",
        "status": "denied",
        "denied_by": "budget",
        "reason": "budget limit max_irreversible_per_run reached (4 > 3)",
    }
    return [
        {"event_type": "tool.call", "metadata": {**meta, "arguments": {"amount": 4}}},
        {"event_type": "tool.result", "metadata": {**meta, "latency_ms": 0.0}},
    ]


class TestDevtools:
    def test_run_stats_count_refused_calls(self) -> None:
        from promptise.run.stats import RunStats

        stats = RunStats()
        stats.add_entry(
            "payer", {"event_type": "tool.call", "metadata": {"tool_name": "wire_money"}}
        )
        stats.add_entry(
            "payer",
            {
                "event_type": "tool.result",
                "metadata": {"tool_name": "wire_money", "latency_ms": 10.0},
            },
        )
        for entry in _denied_entries():
            stats.add_entry("payer", entry)
        snap = stats.snapshot()
        assert snap["totals"]["tool_calls"] == 2
        assert snap["totals"]["tool_denied"] == 1
        assert snap["totals"]["errors"] == 0
        row = snap["tools"]["wire_money"]
        assert (row["calls"], row["denied"], row["errors"]) == (2, 1, 0)
        assert row["avg_ms"] == 10.0  # the refused call has no latency to average

    def test_summary_shows_refused_calls(self) -> None:
        from promptise.run.devtools import print_summary
        from promptise.run.stats import RunStats

        stats = RunStats()
        for entry in _denied_entries():
            stats.add_entry("payer", entry)
        out = io.StringIO()
        print_summary(stats.snapshot(), duration_s=1.0, console=Console(file=out, width=200))
        text = out.getvalue()
        assert "1 tool call(s) (1 refused)" in text
        [header] = [line for line in text.splitlines() if line.lstrip().startswith("tool ")]
        assert header.split() == ["tool", "calls", "errors", "refused", "avg", "ms"]

    def test_summary_without_refusals_is_unchanged(self) -> None:
        from promptise.run.devtools import print_summary

        out = io.StringIO()
        stats = {
            "totals": {"tool_calls": 1},
            "tools": {"t": {"calls": 1, "errors": 0, "denied": 0, "avg_ms": 5.0}},
        }
        print_summary(stats, duration_s=1.0, console=Console(file=out, width=200))
        assert "refused" not in out.getvalue()

    def test_live_trace_prints_the_refusal_once(self) -> None:
        from promptise.run.devtools import DevTracer

        out = io.StringIO()
        tracer = DevTracer(file=out)
        tracer.console = Console(file=out, width=200, highlight=False)
        for entry in _denied_entries():
            tracer({"kind": "timeline", "agent": "payer", "entry": entry})
        lines = [line for line in out.getvalue().splitlines() if line.strip()]
        assert len(lines) == 1
        assert "wire_money refused" in lines[0]
        assert "(budget)" in lines[0]
        assert "max_irreversible_per_run" in lines[0]

    def test_tui_marks_the_call_denied(self) -> None:
        from promptise.run.tui.state import RunState

        state = RunState()
        for i, entry in enumerate(_denied_entries()):
            state.apply(
                {
                    "kind": "timeline",
                    "agent": "payer",
                    "ts": 1.0 + i,
                    "entry": {"timestamp": 1.0 + i, **entry},
                }
            )
        call = state.tool_calls.last()
        assert call is not None
        assert call.status == "denied"
        assert call.error == "budget limit max_irreversible_per_run reached (4 > 3)"
        assert call.arguments == '{"amount": 4}'
