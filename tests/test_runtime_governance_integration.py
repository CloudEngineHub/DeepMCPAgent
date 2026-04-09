"""Integration tests for governance subsystem wiring in AgentProcess.

Verifies that budget, health, mission, and secrets are correctly
initialised, wired into _invoke_agent(), and respond to events.
Uses mocked agents — no real LLM calls.
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from promptise.runtime.config import (
    BudgetConfig,
    HealthConfig,
    MissionConfig,
    ProcessConfig,
    SecretScopeConfig,
)
from promptise.runtime.process import AgentProcess
from promptise.runtime.triggers.base import TriggerEvent

BUILD_TARGET = "promptise.agent.build_agent"


def _make_mock_agent() -> AsyncMock:
    agent = AsyncMock()
    agent.ainvoke = AsyncMock(return_value={"messages": [{"role": "assistant", "content": "Done"}]})
    agent.shutdown = AsyncMock()
    return agent


def _patch_build(agent: AsyncMock):
    return patch(BUILD_TARGET, new_callable=AsyncMock, return_value=agent)


# ===========================================================================
# Budget integration
# ===========================================================================


class TestBudgetIntegration:
    @pytest.mark.asyncio
    async def test_budget_state_created_when_enabled(self) -> None:
        config = ProcessConfig(budget=BudgetConfig(enabled=True))
        process = AgentProcess("test", config)
        assert process._budget is not None
        assert process._budget_enforcer is not None
        assert process._runtime_callback is not None

    @pytest.mark.asyncio
    async def test_budget_not_created_when_disabled(self) -> None:
        config = ProcessConfig(budget=BudgetConfig(enabled=False))
        process = AgentProcess("test", config)
        assert process._budget is None
        assert process._budget_enforcer is None

    @pytest.mark.asyncio
    async def test_budget_reset_run_awaited_on_invoke(self) -> None:
        agent = _make_mock_agent()
        config = ProcessConfig(budget=BudgetConfig(enabled=True, max_tool_calls_per_run=100))

        with _patch_build(agent):
            process = AgentProcess("test", config)
            await process.start()

            # Inject event and wait for processing
            await process.inject(TriggerEvent(trigger_id="t", trigger_type="test"))
            await asyncio.sleep(0.3)

            # Budget state should exist and reset_run should have been called
            assert process._budget is not None
            assert process._budget.run_tool_calls == 0  # Reset after invoke

            await process.stop()

    @pytest.mark.asyncio
    async def test_budget_remaining_in_status(self) -> None:
        config = ProcessConfig(budget=BudgetConfig(enabled=True, max_tool_calls_per_run=10))
        process = AgentProcess("test", config)
        status = process.status()
        assert "budget" in status
        assert isinstance(status["budget"], dict)

    @pytest.mark.asyncio
    async def test_budget_injected_into_context(self) -> None:
        agent = _make_mock_agent()
        config = ProcessConfig(
            budget=BudgetConfig(
                enabled=True,
                max_tool_calls_per_run=50,
                inject_remaining=True,
            )
        )

        with _patch_build(agent):
            process = AgentProcess("test", config)
            await process.start()

            await process.inject(TriggerEvent(trigger_id="t", trigger_type="test"))
            await asyncio.sleep(0.3)

            # Check that ainvoke was called with budget context
            call_args = agent.ainvoke.call_args
            messages = call_args[0][0]["messages"]
            budget_msgs = [m for m in messages if "[Budget Remaining]" in m.get("content", "")]
            assert len(budget_msgs) >= 1

            await process.stop()


# ===========================================================================
# Health integration
# ===========================================================================


class TestHealthIntegration:
    @pytest.mark.asyncio
    async def test_health_created_when_enabled(self) -> None:
        config = ProcessConfig(health=HealthConfig(enabled=True))
        process = AgentProcess("test", config)
        assert process._health is not None
        assert process._runtime_callback is not None

    @pytest.mark.asyncio
    async def test_health_not_created_when_disabled(self) -> None:
        config = ProcessConfig(health=HealthConfig(enabled=False))
        process = AgentProcess("test", config)
        assert process._health is None

    @pytest.mark.asyncio
    async def test_health_has_public_properties(self) -> None:
        config = ProcessConfig(health=HealthConfig(enabled=True))
        process = AgentProcess("test", config)
        assert process._health.anomalies == []
        assert process._health.latest_anomaly is None

    @pytest.mark.asyncio
    async def test_health_anomaly_count_in_status(self) -> None:
        config = ProcessConfig(health=HealthConfig(enabled=True))
        process = AgentProcess("test", config)
        status = process.status()
        assert "health_anomalies" in status
        assert status["health_anomalies"] == 0

    @pytest.mark.asyncio
    async def test_error_recording_on_failed_invoke(self) -> None:
        agent = _make_mock_agent()
        agent.ainvoke = AsyncMock(side_effect=RuntimeError("boom"))
        config = ProcessConfig(
            health=HealthConfig(enabled=True),
            max_consecutive_failures=5,
        )

        with _patch_build(agent):
            process = AgentProcess("test", config)
            await process.start()

            await process.inject(TriggerEvent(trigger_id="t", trigger_type="test"))
            await asyncio.sleep(0.3)

            # Health should have recorded an error
            assert len(process._health._error_window) >= 1
            assert process._health._error_window[-1] is True

            await process.stop()


# ===========================================================================
# Mission integration
# ===========================================================================


class TestMissionIntegration:
    @pytest.mark.asyncio
    async def test_mission_created_when_enabled(self) -> None:
        config = ProcessConfig(
            mission=MissionConfig(
                enabled=True,
                objective="Test",
                success_criteria="Done",
            )
        )
        process = AgentProcess("test", config)
        assert process._mission is not None

    @pytest.mark.asyncio
    async def test_mission_not_created_when_disabled(self) -> None:
        config = ProcessConfig(mission=MissionConfig(enabled=False))
        process = AgentProcess("test", config)
        assert process._mission is None

    @pytest.mark.asyncio
    async def test_mission_invocation_incremented(self) -> None:
        agent = _make_mock_agent()
        config = ProcessConfig(
            mission=MissionConfig(
                enabled=True,
                objective="Test",
                success_criteria="Done",
                eval_every=999,  # Don't trigger eval
            )
        )

        with _patch_build(agent):
            process = AgentProcess("test", config)
            await process.start()

            # Inject 3 events
            for _ in range(3):
                await process.inject(TriggerEvent(trigger_id="t", trigger_type="test"))
            await asyncio.sleep(0.5)

            assert process._mission.invocation_count == 3

            await process.stop()

    @pytest.mark.asyncio
    async def test_mission_context_injected(self) -> None:
        agent = _make_mock_agent()
        config = ProcessConfig(
            mission=MissionConfig(
                enabled=True,
                objective="Migrate tables",
                success_criteria="All pass",
                eval_every=999,
            )
        )

        with _patch_build(agent):
            process = AgentProcess("test", config)
            await process.start()

            await process.inject(TriggerEvent(trigger_id="t", trigger_type="test"))
            await asyncio.sleep(0.3)

            call_args = agent.ainvoke.call_args
            messages = call_args[0][0]["messages"]
            mission_msgs = [m for m in messages if "[Mission]" in m.get("content", "")]
            assert len(mission_msgs) >= 1
            assert "Migrate tables" in mission_msgs[0]["content"]

            await process.stop()

    @pytest.mark.asyncio
    async def test_mission_state_in_status(self) -> None:
        config = ProcessConfig(
            mission=MissionConfig(
                enabled=True,
                objective="Test",
                success_criteria="Done",
            )
        )
        process = AgentProcess("test", config)
        status = process.status()
        assert status["mission_state"] == "active"

    @pytest.mark.asyncio
    async def test_completed_mission_skips_invoke(self) -> None:
        agent = _make_mock_agent()
        config = ProcessConfig(
            mission=MissionConfig(
                enabled=True,
                objective="Test",
                success_criteria="Done",
            )
        )

        with _patch_build(agent):
            process = AgentProcess("test", config)
            await process.start()

            # Manually complete mission
            process._mission.complete()

            await process.inject(TriggerEvent(trigger_id="t", trigger_type="test"))
            await asyncio.sleep(0.3)

            # Agent should NOT have been invoked
            agent.ainvoke.assert_not_called()

            await process.stop()


# ===========================================================================
# Secrets integration
# ===========================================================================


class TestSecretsIntegration:
    @pytest.mark.asyncio
    async def test_secrets_created_when_enabled(self) -> None:
        config = ProcessConfig(
            secrets=SecretScopeConfig(
                enabled=True,
                secrets={"key": "value"},
            )
        )
        process = AgentProcess("test", config)
        assert process._secrets is not None

    @pytest.mark.asyncio
    async def test_secrets_not_created_when_disabled(self) -> None:
        config = ProcessConfig(secrets=SecretScopeConfig(enabled=False))
        process = AgentProcess("test", config)
        assert process._secrets is None

    @pytest.mark.asyncio
    async def test_secrets_resolved_on_start(self) -> None:
        agent = _make_mock_agent()
        config = ProcessConfig(
            secrets=SecretScopeConfig(
                enabled=True,
                secrets={"static": "my_value"},
            )
        )

        with _patch_build(agent):
            process = AgentProcess("test", config)
            await process.start()

            assert process._secrets.get("static") == "my_value"

            await process.stop()

    @pytest.mark.asyncio
    async def test_secrets_revoked_on_stop(self) -> None:
        agent = _make_mock_agent()
        config = ProcessConfig(
            secrets=SecretScopeConfig(
                enabled=True,
                secrets={"key": "secret_value"},
            )
        )

        with _patch_build(agent):
            process = AgentProcess("test", config)
            await process.start()
            assert process._secrets.get("key") == "secret_value"

            await process.stop()
            assert process._secrets.active_secret_count == 0

    @pytest.mark.asyncio
    async def test_secrets_count_in_status(self) -> None:
        config = ProcessConfig(
            secrets=SecretScopeConfig(
                enabled=True,
                secrets={"a": "1", "b": "2"},
            )
        )
        process = AgentProcess("test", config)
        # Before resolve, count is 0
        status = process.status()
        assert "active_secrets" in status


# ===========================================================================
# Combined governance
# ===========================================================================


class TestCombinedGovernance:
    @pytest.mark.asyncio
    async def test_all_subsystems_created(self) -> None:
        config = ProcessConfig(
            budget=BudgetConfig(enabled=True),
            health=HealthConfig(enabled=True),
            mission=MissionConfig(enabled=True, objective="Test", success_criteria="Done"),
            secrets=SecretScopeConfig(enabled=True, secrets={"k": "v"}),
        )
        process = AgentProcess("test", config)
        assert process._budget is not None
        assert process._health is not None
        assert process._mission is not None
        assert process._secrets is not None
        assert process._runtime_callback is not None

    @pytest.mark.asyncio
    async def test_all_subsystems_in_status(self) -> None:
        config = ProcessConfig(
            budget=BudgetConfig(enabled=True, max_tool_calls_per_run=10),
            health=HealthConfig(enabled=True),
            mission=MissionConfig(enabled=True, objective="Test", success_criteria="Done"),
            secrets=SecretScopeConfig(enabled=True, secrets={"k": "v"}),
        )
        process = AgentProcess("test", config)
        status = process.status()
        assert "budget" in status
        assert "health_anomalies" in status
        assert "mission_state" in status
        assert "active_secrets" in status

    @pytest.mark.asyncio
    async def test_zero_overhead_when_all_disabled(self) -> None:
        config = ProcessConfig()
        process = AgentProcess("test", config)
        assert process._budget is None
        assert process._health is None
        assert process._mission is None
        assert process._secrets is None
        assert process._runtime_callback is None

        status = process.status()
        assert "budget" not in status
        assert "health_anomalies" not in status
        assert "mission_state" not in status
        assert "active_secrets" not in status
