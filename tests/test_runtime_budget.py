"""Tests for promptise.runtime.budget — BudgetState, BudgetViolation, BudgetEnforcer."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from promptise.runtime.budget import BudgetEnforcer, BudgetState, BudgetViolation
from promptise.runtime.config import BudgetConfig, ToolCostAnnotation


class TestBudgetViolation:
    def test_fields(self):
        v = BudgetViolation(limit_name="max_tool_calls_per_run", limit_value=10, current_value=11)
        assert v.limit_name == "max_tool_calls_per_run"
        assert v.limit_value == 10
        assert v.current_value == 11
        assert v.tool_name is None

    def test_with_tool_name(self):
        v = BudgetViolation(limit_name="x", limit_value=1, current_value=2, tool_name="search")
        assert v.tool_name == "search"


class TestRecordToolCall:
    @pytest.mark.asyncio
    async def test_increments_counters(self):
        state = BudgetState(BudgetConfig())
        await state.record_tool_call("some_tool")
        assert state.run_tool_calls == 1
        assert state.daily_tool_calls == 1

    @pytest.mark.asyncio
    async def test_returns_none_under_limit(self):
        state = BudgetState(BudgetConfig(max_tool_calls_per_run=100))
        result = await state.record_tool_call("tool_a")
        assert result is None

    @pytest.mark.asyncio
    async def test_returns_violation_when_exceeding_per_run(self):
        state = BudgetState(BudgetConfig(max_tool_calls_per_run=2))
        await state.record_tool_call("t1")
        await state.record_tool_call("t2")
        result = await state.record_tool_call("t3")
        assert isinstance(result, BudgetViolation)
        assert result.limit_name == "max_tool_calls_per_run"

    @pytest.mark.asyncio
    async def test_applies_cost_weight(self):
        config = BudgetConfig(
            tool_costs={"expensive": ToolCostAnnotation(cost_weight=5.0)},
        )
        state = BudgetState(config)
        await state.record_tool_call("expensive")
        assert state.run_cost == 5.0
        assert state.daily_cost == 5.0

    @pytest.mark.asyncio
    async def test_tracks_irreversible_actions(self):
        config = BudgetConfig(
            tool_costs={"delete": ToolCostAnnotation(irreversible=True)},
        )
        state = BudgetState(config)
        await state.record_tool_call("delete")
        assert state.run_irreversible == 1

    @pytest.mark.asyncio
    async def test_daily_tool_call_limit(self):
        state = BudgetState(BudgetConfig(max_tool_calls_per_day=2))
        await state.record_tool_call("t")
        await state.record_tool_call("t")
        result = await state.record_tool_call("t")
        assert isinstance(result, BudgetViolation)
        assert result.limit_name == "max_tool_calls_per_day"


class TestRecordLlmTurn:
    @pytest.mark.asyncio
    async def test_increments_counter(self):
        state = BudgetState(BudgetConfig())
        await state.record_llm_turn()
        assert state.run_llm_turns == 1

    @pytest.mark.asyncio
    async def test_returns_violation_when_exceeding_limit(self):
        state = BudgetState(BudgetConfig(max_llm_turns_per_run=2))
        await state.record_llm_turn()
        await state.record_llm_turn()
        result = await state.record_llm_turn()
        assert isinstance(result, BudgetViolation)
        assert result.limit_name == "max_llm_turns_per_run"


class TestRecordRunStart:
    @pytest.mark.asyncio
    async def test_increments_daily_runs(self):
        state = BudgetState(BudgetConfig())
        result = await state.record_run_start()
        assert result is None
        assert state.daily_runs == 1

    @pytest.mark.asyncio
    async def test_returns_violation_over_daily_limit(self):
        state = BudgetState(BudgetConfig(max_runs_per_day=1))
        await state.record_run_start()
        result = await state.record_run_start()
        assert isinstance(result, BudgetViolation)
        assert result.limit_name == "max_runs_per_day"


class TestResetRun:
    @pytest.mark.asyncio
    async def test_resets_per_run_but_not_daily(self):
        state = BudgetState(BudgetConfig())
        await state.record_run_start()
        await state.record_tool_call("t")
        await state.record_llm_turn()

        daily_runs_before = state.daily_runs
        daily_tools_before = state.daily_tool_calls

        await state.reset_run()

        assert state.run_tool_calls == 0
        assert state.run_llm_turns == 0
        assert state.run_cost == 0.0
        assert state.run_irreversible == 0
        assert state.daily_runs == daily_runs_before
        assert state.daily_tool_calls == daily_tools_before


class TestRemaining:
    @pytest.mark.asyncio
    async def test_returns_correct_remaining(self):
        state = BudgetState(BudgetConfig(max_tool_calls_per_run=10, max_llm_turns_per_run=5))
        await state.record_tool_call("t")
        await state.record_tool_call("t")
        await state.record_llm_turn()
        rem = state.remaining()
        assert rem["tool_calls_run"] == 8
        assert rem["llm_turns_run"] == 4

    def test_returns_none_for_unlimited(self):
        state = BudgetState(BudgetConfig())
        rem = state.remaining()
        assert rem["tool_calls_run"] is None
        assert rem["llm_turns_run"] is None
        assert rem["cost_run"] is None


class TestBudgetContext:
    @pytest.mark.asyncio
    async def test_returns_formatted_string(self):
        state = BudgetState(BudgetConfig(max_tool_calls_per_run=10))
        await state.record_tool_call("t")
        ctx = state.budget_context()
        assert isinstance(ctx, str)
        assert "[Budget]" in ctx

    def test_returns_empty_when_no_limits(self):
        state = BudgetState(BudgetConfig())
        ctx = state.budget_context()
        assert ctx == ""


class TestSerialization:
    @pytest.mark.asyncio
    async def test_round_trip(self):
        config = BudgetConfig(max_tool_calls_per_run=50, max_runs_per_day=10)
        state = BudgetState(config)
        await state.record_run_start()
        await state.record_tool_call("tool_x")
        await state.record_tool_call("tool_y")
        await state.record_llm_turn()

        data = state.to_dict()
        assert isinstance(data, dict)

        restored = BudgetState.from_dict(data, config)
        assert restored.run_tool_calls == state.run_tool_calls
        assert restored.daily_tool_calls == state.daily_tool_calls
        assert restored.run_llm_turns == state.run_llm_turns
        assert restored.daily_runs == state.daily_runs


class TestBudgetEnforcerPause:
    @pytest.mark.asyncio
    async def test_pause_calls_suspend(self):
        config = BudgetConfig(max_tool_calls_per_run=1, on_exceeded="pause")
        state = BudgetState(config)
        process = MagicMock()
        process.suspend = AsyncMock()
        process.stop = AsyncMock()

        enforcer = BudgetEnforcer(config)

        await state.record_tool_call("t")
        violation = await state.record_tool_call("t")
        assert violation is not None
        await enforcer.handle_violation(violation, process)
        process.suspend.assert_awaited_once()
        process.stop.assert_not_awaited()


class TestBudgetEnforcerStop:
    @pytest.mark.asyncio
    async def test_stop_calls_stop(self):
        config = BudgetConfig(max_tool_calls_per_run=1, on_exceeded="stop")
        state = BudgetState(config)
        process = MagicMock()
        process.suspend = AsyncMock()
        process.stop = AsyncMock()

        enforcer = BudgetEnforcer(config)

        await state.record_tool_call("t")
        violation = await state.record_tool_call("t")
        assert violation is not None
        await enforcer.handle_violation(violation, process)
        process.stop.assert_awaited_once()


class TestDailyLimits:
    @pytest.mark.asyncio
    async def test_daily_run_limit_enforced_across_resets(self):
        state = BudgetState(BudgetConfig(max_runs_per_day=2))

        r1 = await state.record_run_start()
        assert r1 is None
        await state.reset_run()

        r2 = await state.record_run_start()
        assert r2 is None
        await state.reset_run()

        r3 = await state.record_run_start()
        assert isinstance(r3, BudgetViolation)
