"""Tests for promptise.runtime.mission — MissionTracker, MissionState, MissionEvaluation."""

from __future__ import annotations

import asyncio
import json
import time
from contextlib import nullcontext
from typing import Any

from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage

from promptise.runtime.config import MissionConfig
from promptise.runtime.mission import (
    MissionEvaluation,
    MissionEvidence,
    MissionState,
    MissionTracker,
)

# ---------------------------------------------------------------------------
# MissionState enum
# ---------------------------------------------------------------------------


class TestMissionState:
    def test_active_exists(self):
        assert MissionState.ACTIVE is not None

    def test_paused_exists(self):
        assert MissionState.PAUSED is not None

    def test_completed_exists(self):
        assert MissionState.COMPLETED is not None

    def test_failed_exists(self):
        assert MissionState.FAILED is not None

    def test_members_are_unique(self):
        values = [m.value for m in MissionState]
        assert len(values) == len(set(values))


# ---------------------------------------------------------------------------
# MissionEvaluation dataclass
# ---------------------------------------------------------------------------


class TestMissionEvaluation:
    def test_creation(self):
        ev = MissionEvaluation(
            achieved=False,
            confidence=0.85,
            reasoning="Good progress on the objective.",
            progress_summary="Step 1 and 2 done.",
        )
        assert ev.confidence == 0.85
        assert "Good progress" in ev.reasoning
        assert ev.achieved is False

    def test_creation_minimal(self):
        ev = MissionEvaluation(
            achieved=True, confidence=1.0, reasoning="Done.", progress_summary=""
        )
        assert ev.achieved is True


# ---------------------------------------------------------------------------
# MissionTracker — initial state
# ---------------------------------------------------------------------------


class TestInitialState:
    def test_initial_state_is_active(self):
        cfg = MissionConfig(objective="Summarise data", success_criteria="accuracy")
        tracker = MissionTracker(cfg, process_id="test")
        assert tracker.state == MissionState.ACTIVE

    def test_initial_invocation_count_is_zero(self):
        cfg = MissionConfig(objective="test", success_criteria="test criteria")
        tracker = MissionTracker(cfg, process_id="test")
        assert tracker.invocation_count == 0


# ---------------------------------------------------------------------------
# MissionTracker — invocation counting
# ---------------------------------------------------------------------------


class TestInvocationCounting:
    def test_increment_invocation(self):
        cfg = MissionConfig(objective="test", success_criteria="test criteria")
        tracker = MissionTracker(cfg, process_id="test")
        tracker.increment_invocation()
        assert tracker.invocation_count == 1

    def test_multiple_increments(self):
        cfg = MissionConfig(objective="test", success_criteria="test criteria")
        tracker = MissionTracker(cfg, process_id="test")
        for _ in range(5):
            tracker.increment_invocation()
        assert tracker.invocation_count == 5


# ---------------------------------------------------------------------------
# MissionTracker — should_evaluate
# ---------------------------------------------------------------------------


class TestShouldEvaluate:
    def test_returns_true_at_eval_interval(self):
        cfg = MissionConfig(objective="test", success_criteria="test criteria", eval_every=3)
        tracker = MissionTracker(cfg, process_id="test")
        for _ in range(3):
            tracker.increment_invocation()
        assert tracker.should_evaluate() is True

    def test_returns_false_between_intervals(self):
        cfg = MissionConfig(objective="test", success_criteria="test criteria", eval_every=5)
        tracker = MissionTracker(cfg, process_id="test")
        for _ in range(3):
            tracker.increment_invocation()
        assert tracker.should_evaluate() is False

    def test_returns_false_when_not_active(self):
        cfg = MissionConfig(objective="test", success_criteria="test criteria", eval_every=1)
        tracker = MissionTracker(cfg, process_id="test")
        tracker.increment_invocation()
        tracker.complete()
        assert tracker.should_evaluate() is False

    def test_returns_false_when_invocation_count_zero(self):
        cfg = MissionConfig(objective="test", success_criteria="test criteria", eval_every=1)
        tracker = MissionTracker(cfg, process_id="test")
        assert tracker.should_evaluate() is False


# ---------------------------------------------------------------------------
# MissionTracker — timeout
# ---------------------------------------------------------------------------


class TestTimeout:
    def test_no_timeout_when_zero(self):
        cfg = MissionConfig(objective="test", success_criteria="test criteria", timeout_hours=0)
        tracker = MissionTracker(cfg, process_id="test")
        assert tracker.is_timed_out() is False

    def test_not_timed_out_when_fresh(self):
        cfg = MissionConfig(objective="test", success_criteria="test criteria", timeout_hours=1)
        tracker = MissionTracker(cfg, process_id="test")
        assert tracker.is_timed_out() is False

    def test_timed_out_after_deadline(self):
        cfg = MissionConfig(objective="test", success_criteria="test criteria", timeout_hours=1)
        tracker = MissionTracker(cfg, process_id="test")
        # Move started_at back in time by 2 hours
        tracker._started_at = time.monotonic() - 2 * 3600
        assert tracker.is_timed_out() is True


# ---------------------------------------------------------------------------
# MissionTracker — invocation limit
# ---------------------------------------------------------------------------


class TestInvocationLimit:
    def test_no_limit_when_zero(self):
        cfg = MissionConfig(objective="test", success_criteria="test criteria", max_invocations=0)
        tracker = MissionTracker(cfg, process_id="test")
        for _ in range(100):
            tracker.increment_invocation()
        assert tracker.is_over_limit() is False

    def test_not_over_limit_below_max(self):
        cfg = MissionConfig(objective="test", success_criteria="test criteria", max_invocations=10)
        tracker = MissionTracker(cfg, process_id="test")
        for _ in range(5):
            tracker.increment_invocation()
        assert tracker.is_over_limit() is False

    def test_over_limit_at_max(self):
        cfg = MissionConfig(objective="test", success_criteria="test criteria", max_invocations=3)
        tracker = MissionTracker(cfg, process_id="test")
        for _ in range(3):
            tracker.increment_invocation()
        assert tracker.is_over_limit() is True

    def test_over_limit_above_max(self):
        cfg = MissionConfig(objective="test", success_criteria="test criteria", max_invocations=3)
        tracker = MissionTracker(cfg, process_id="test")
        for _ in range(5):
            tracker.increment_invocation()
        assert tracker.is_over_limit() is True


# ---------------------------------------------------------------------------
# MissionTracker — state transitions
# ---------------------------------------------------------------------------


class TestStateTransitions:
    def test_fail_transitions_to_failed(self):
        cfg = MissionConfig(objective="test", success_criteria="test criteria")
        tracker = MissionTracker(cfg, process_id="test")
        tracker.fail("test failure")
        assert tracker.state == MissionState.FAILED

    def test_pause_transitions_to_paused(self):
        cfg = MissionConfig(objective="test", success_criteria="test criteria")
        tracker = MissionTracker(cfg, process_id="test")
        tracker.pause()
        assert tracker.state == MissionState.PAUSED

    def test_resume_from_paused_to_active(self):
        cfg = MissionConfig(objective="test", success_criteria="test criteria")
        tracker = MissionTracker(cfg, process_id="test")
        tracker.pause()
        assert tracker.state == MissionState.PAUSED
        tracker.resume()
        assert tracker.state == MissionState.ACTIVE

    def test_resume_does_nothing_if_not_paused(self):
        cfg = MissionConfig(objective="test", success_criteria="test criteria")
        tracker = MissionTracker(cfg, process_id="test")
        tracker.complete()
        tracker.resume()
        # Should remain COMPLETED, not revert to ACTIVE
        assert tracker.state == MissionState.COMPLETED

    def test_resume_does_nothing_if_active(self):
        cfg = MissionConfig(objective="test", success_criteria="test criteria")
        tracker = MissionTracker(cfg, process_id="test")
        tracker.resume()
        assert tracker.state == MissionState.ACTIVE

    def test_complete_transitions_to_completed(self):
        cfg = MissionConfig(objective="test", success_criteria="test criteria")
        tracker = MissionTracker(cfg, process_id="test")
        tracker.complete()
        assert tracker.state == MissionState.COMPLETED


# ---------------------------------------------------------------------------
# MissionTracker — context_summary
# ---------------------------------------------------------------------------


class TestContextPrompt:
    def test_includes_objective(self):
        cfg = MissionConfig(
            objective="Find all security vulnerabilities",
            criteria=["completeness", "accuracy"],
        )
        tracker = MissionTracker(cfg, process_id="test")
        prompt = tracker.context_summary()
        assert "Find all security vulnerabilities" in prompt

    def test_includes_criteria(self):
        cfg = MissionConfig(
            objective="Summarise the report",
            success_criteria="brevity and accuracy",
        )
        tracker = MissionTracker(cfg, process_id="test")
        prompt = tracker.context_summary()
        assert "brevity" in prompt
        assert "accuracy" in prompt

    def test_includes_progress_info(self):
        cfg = MissionConfig(objective="test", success_criteria="test criteria")
        tracker = MissionTracker(cfg, process_id="test")
        for _ in range(4):
            tracker.increment_invocation()
        prompt = tracker.context_summary()
        # Should mention invocation count or progress
        assert "4" in prompt

    def test_includes_last_evaluation(self):
        cfg = MissionConfig(objective="test", success_criteria="quality")
        tracker = MissionTracker(cfg, process_id="test")
        ev = MissionEvaluation(
            achieved=False,
            confidence=0.7,
            reasoning="Decent progress so far.",
            progress_summary="Step 1 done.",
        )
        tracker._evaluations.append(ev)
        prompt = tracker.context_summary()
        assert "0.70" in prompt or "Decent progress" in prompt


# ---------------------------------------------------------------------------
# MissionTracker — properties
# ---------------------------------------------------------------------------


class TestProperties:
    def test_state_property(self):
        cfg = MissionConfig(objective="test", success_criteria="test criteria")
        tracker = MissionTracker(cfg, process_id="test")
        assert tracker.state == MissionState.ACTIVE
        tracker.fail("test failure")
        assert tracker.state == MissionState.FAILED

    def test_evaluations_property(self):
        cfg = MissionConfig(objective="test", success_criteria="test criteria")
        tracker = MissionTracker(cfg, process_id="test")
        assert isinstance(tracker.evaluations, list)
        assert len(tracker.evaluations) == 0


# ---------------------------------------------------------------------------
# MissionTracker — serialization round-trip
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_to_dict_returns_dict(self):
        cfg = MissionConfig(objective="test", criteria=["a", "b"])
        tracker = MissionTracker(cfg, process_id="test")
        tracker.increment_invocation()
        data = tracker.to_dict()
        assert isinstance(data, dict)

    def test_from_dict_restores_tracker(self):
        cfg = MissionConfig(objective="round-trip test", criteria=["c1"])
        tracker = MissionTracker(cfg, process_id="test")
        tracker.increment_invocation()
        tracker.increment_invocation()
        data = tracker.to_dict()

        restored = MissionTracker.from_dict(data, cfg)
        assert isinstance(restored, MissionTracker)
        assert restored.invocation_count == 2

    def test_round_trip_preserves_state(self):
        cfg = MissionConfig(objective="stateful", criteria=["x"])
        tracker = MissionTracker(cfg, process_id="test")
        tracker.pause()
        data = tracker.to_dict()
        restored = MissionTracker.from_dict(data, cfg)
        assert restored.state == MissionState.PAUSED

    def test_round_trip_preserves_objective(self):
        cfg = MissionConfig(objective="find bugs", criteria=["coverage"])
        tracker = MissionTracker(cfg, process_id="test")
        data = tracker.to_dict()
        restored = MissionTracker.from_dict(data, cfg)
        prompt = restored.context_summary()
        assert "find bugs" in prompt


# ---------------------------------------------------------------------------
# LLM judge and max_invocations, end to end (scripted models, no API key)
# ---------------------------------------------------------------------------


class _ScriptedJudge(GenericFakeChatModel):
    """A chat model that replays scripted replies and accepts bind_tools()."""

    def bind_tools(self, tools: Any, **kwargs: Any) -> _ScriptedJudge:
        return self


def _judge(reply: dict[str, Any]) -> _ScriptedJudge:
    return _ScriptedJudge(messages=iter([AIMessage(content=json.dumps(reply))]))


class TestLLMJudge:
    """The judge is a real ``build_agent`` agent (it lacked ``servers``)."""

    async def test_judge_verdict_completes_the_mission(self) -> None:
        tracker = MissionTracker(
            MissionConfig(enabled=True, objective="Say done.", success_criteria="Said done."),
            process_id="p",
        )
        evaluation = await tracker.evaluate(
            MissionEvidence(conversation=[{"role": "assistant", "content": "done"}]),
            _judge(
                {
                    "achieved": True,
                    "confidence": 0.95,
                    "reasoning": "The agent said done.",
                    "progress_summary": "Complete",
                }
            ),
        )
        assert evaluation.reasoning == "The agent said done."
        assert evaluation.achieved is True
        assert evaluation.confidence == 0.95
        assert evaluation.source == "llm"
        assert tracker.state == MissionState.COMPLETED

    async def test_confident_not_yet_keeps_the_mission_active(self) -> None:
        tracker = MissionTracker(
            MissionConfig(enabled=True, objective="Clear the backlog", success_criteria="Empty"),
            process_id="p",
        )
        evaluation = await tracker.evaluate(
            MissionEvidence(conversation=[{"role": "assistant", "content": "3 left"}]),
            _judge(
                {
                    "achieved": False,
                    "confidence": 0.9,
                    "reasoning": "3 items remain.",
                    "progress_summary": "7 of 10",
                }
            ),
        )
        assert evaluation.reasoning == "3 items remain."
        assert tracker.state == MissionState.ACTIVE
        assert tracker.should_evaluate() is False  # no invocation counted yet

    async def test_judge_writes_nothing_to_stdout(self, capsys) -> None:
        """The judge has no tools by design; building it must not print a
        "No tools discovered" notice into the application's output."""
        tracker = MissionTracker(
            MissionConfig(enabled=True, objective="Say done.", success_criteria="Said done."),
            process_id="p",
        )
        for _ in range(2):
            await tracker.evaluate(
                MissionEvidence(conversation=[{"role": "assistant", "content": "working"}]),
                _judge({"achieved": False, "confidence": 0.9, "reasoning": "Not yet."}),
            )
        assert capsys.readouterr().out == ""


class _StubAgent:
    def __init__(self) -> None:
        self.calls = 0

    async def ainvoke(self, input: Any, config: Any = None) -> dict[str, Any]:
        self.calls += 1
        return {"messages": [{"role": "assistant", "content": f"run {self.calls}"}]}

    async def shutdown(self) -> None:
        pass


async def _process_with_stub(config: Any, **kwargs: Any) -> tuple[Any, _StubAgent]:
    from promptise.runtime.process import AgentProcess

    process = AgentProcess("m", config, **kwargs)
    agent = _StubAgent()

    async def build() -> None:
        process._agent = agent

    process._build_agent = build  # type: ignore[method-assign]
    return process, agent


class TestMaxInvocations:
    async def test_mission_fails_after_max_invocations(self) -> None:
        from promptise import CallbackSink, EventNotifier
        from promptise.runtime.config import ProcessConfig
        from promptise.runtime.triggers.base import TriggerEvent

        seen: list[Any] = []
        notifier = EventNotifier(sinks=[CallbackSink(seen.append)])
        process, agent = await _process_with_stub(
            ProcessConfig(
                mission=MissionConfig(
                    enabled=True,
                    objective="o",
                    success_criteria="c",
                    eval_every=100,  # no judge call in this test
                    max_invocations=2,
                )
            ),
            event_notifier=notifier,
        )
        await process.start()
        try:
            for i in range(5):
                await process.inject(
                    TriggerEvent(trigger_id="t", trigger_type="manual", payload={"i": i})
                )
            for _ in range(100):
                if process._trigger_queue.empty():
                    break
                await asyncio.sleep(0.02)
            await asyncio.sleep(0.2)  # let the last event finish
            assert agent.calls == 2
            assert process._mission.state == MissionState.FAILED
        finally:
            await process.stop()
        failed = [e for e in seen if e.event_type == "mission.failed"]
        assert len(failed) == 1
        assert failed[0].data["reason"] == "max_invocations"

    async def test_unlimited_by_default(self) -> None:
        from promptise.runtime.config import ProcessConfig
        from promptise.runtime.triggers.base import TriggerEvent

        process, agent = await _process_with_stub(
            ProcessConfig(
                mission=MissionConfig(
                    enabled=True, objective="o", success_criteria="c", eval_every=100
                )
            )
        )
        await process.start()
        try:
            for i in range(4):
                await process.inject(
                    TriggerEvent(trigger_id="t", trigger_type="manual", payload={"i": i})
                )
            for _ in range(100):
                if agent.calls == 4:
                    break
                await asyncio.sleep(0.02)
            assert agent.calls == 4
            assert process._mission.state == MissionState.ACTIVE
        finally:
            await process.stop()


# ---------------------------------------------------------------------------
# MissionConfig.on_complete — deprecated: it was never read
# ---------------------------------------------------------------------------


class TestOnCompleteDeprecated:
    def test_default_config_does_not_warn(self) -> None:
        import warnings

        with warnings.catch_warnings():
            warnings.simplefilter("error")
            MissionConfig(enabled=True, objective="o", success_criteria="c")

    def test_setting_it_warns_that_it_is_ignored(self) -> None:
        import pytest

        for value in ("stop", "continue", "suspend"):
            with pytest.warns(FutureWarning, match="on_complete is ignored.*auto_complete"):
                MissionConfig(enabled=True, objective="o", on_complete=value)

    async def _completed_process(self, **mission: Any) -> Any:
        import pytest

        from promptise.runtime.config import ProcessConfig
        from promptise.runtime.triggers.base import TriggerEvent

        expected = pytest.warns(FutureWarning) if "on_complete" in mission else nullcontext()
        with expected:
            config = MissionConfig(enabled=True, objective="o", success_criteria="c", **mission)
        process, agent = await _process_with_stub(ProcessConfig(mission=config))
        await process.start()
        tracker = process._mission

        async def achieved(evidence: Any, model: Any) -> MissionEvaluation:
            tracker.complete()
            return MissionEvaluation(
                achieved=True, confidence=0.95, reasoning="done", progress_summary="done"
            )

        tracker.evaluate = achieved  # the judge, without a model call
        await process.inject(TriggerEvent(trigger_id="t", trigger_type="manual", payload={}))
        for _ in range(100):
            if agent.calls and tracker.state == MissionState.COMPLETED:
                break
            await asyncio.sleep(0.02)
        await asyncio.sleep(0.1)  # let the post-invoke step finish
        return process

    async def test_continue_does_not_keep_the_process_running(self) -> None:
        """What happens on completion is auto_complete's call alone."""
        from promptise.runtime.lifecycle import ProcessState

        process = await self._completed_process(on_complete="continue")
        try:
            assert process.state == ProcessState.STOPPED
        finally:
            await process.stop()

    async def test_auto_complete_false_keeps_the_process_running(self) -> None:
        from promptise.runtime.lifecycle import ProcessState

        process = await self._completed_process(auto_complete=False)
        try:
            assert process.state == ProcessState.RUNNING
        finally:
            await process.stop()


def test_on_complete_round_trip_does_not_warn() -> None:
    """A dumped config (API, journal, distributed runtime) is re-validated."""
    import warnings

    from promptise.runtime.config import ProcessConfig

    config = ProcessConfig(mission=MissionConfig(enabled=True, objective="o"))
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        ProcessConfig.model_validate(config.model_dump())
        ProcessConfig.model_validate_json(config.model_dump_json())
