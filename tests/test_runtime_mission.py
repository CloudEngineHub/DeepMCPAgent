"""Tests for promptise.runtime.mission — MissionTracker, MissionState, MissionEvaluation."""

from __future__ import annotations

import time

from promptise.runtime.config import MissionConfig
from promptise.runtime.mission import MissionEvaluation, MissionState, MissionTracker

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
