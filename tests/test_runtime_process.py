"""Tests for AgentProcess lifecycle, trigger processing, and error handling."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from promptise.runtime.config import ContextConfig, ProcessConfig
from promptise.runtime.lifecycle import ProcessState
from promptise.runtime.process import AgentProcess
from promptise.runtime.triggers.base import TriggerEvent

BUILD_TARGET = "promptise.agent.build_agent"


def _make_mock_agent() -> AsyncMock:
    """Create a mock PromptiseAgent."""
    agent = AsyncMock()
    agent.ainvoke = AsyncMock(return_value={"messages": [{"role": "assistant", "content": "Done"}]})
    agent.shutdown = AsyncMock()
    return agent


def _patch_build(agent: AsyncMock | None = None):
    """Patch build_agent to return a mock agent."""
    if agent is None:
        agent = _make_mock_agent()
    return patch(BUILD_TARGET, new_callable=lambda: AsyncMock(return_value=agent))


# =========================================================================
# Lifecycle
# =========================================================================


class TestAgentProcessLifecycle:
    """Verify state machine transitions in AgentProcess."""

    async def test_start_transitions_to_running(self) -> None:
        with _patch_build():
            process = AgentProcess("test", ProcessConfig())
            assert process.state == ProcessState.CREATED
            await process.start()
            assert process.state == ProcessState.RUNNING
            await process.stop()

    async def test_stop_transitions_to_stopped(self) -> None:
        with _patch_build():
            process = AgentProcess("test", ProcessConfig())
            await process.start()
            await process.stop()
            assert process.state == ProcessState.STOPPED

    async def test_double_stop_is_noop(self) -> None:
        with _patch_build():
            process = AgentProcess("test", ProcessConfig())
            await process.start()
            await process.stop()
            await process.stop()  # Should not raise
            assert process.state == ProcessState.STOPPED

    async def test_suspend_resume(self) -> None:
        with _patch_build():
            process = AgentProcess("test", ProcessConfig())
            await process.start()
            await process.suspend()
            assert process.state == ProcessState.SUSPENDED
            await process.resume()
            assert process.state == ProcessState.RUNNING
            await process.stop()

    async def test_context_manager(self) -> None:
        with _patch_build():
            async with AgentProcess("test", ProcessConfig()) as process:
                assert process.state == ProcessState.RUNNING
            assert process.state == ProcessState.STOPPED

    async def test_lifecycle_history_recorded(self) -> None:
        with _patch_build():
            process = AgentProcess("test", ProcessConfig())
            await process.start()
            await process.stop()
            history = process.lifecycle.history
            states = [t.to_state for t in history]
            assert ProcessState.STARTING in states
            assert ProcessState.RUNNING in states
            assert ProcessState.STOPPING in states
            assert ProcessState.STOPPED in states


# =========================================================================
# Status
# =========================================================================


class TestAgentProcessStatus:
    """Verify status snapshot."""

    async def test_status_while_running(self) -> None:
        with _patch_build():
            process = AgentProcess("monitor", ProcessConfig())
            await process.start()
            status = process.status()
            assert status["name"] == "monitor"
            assert status["state"] == "running"
            assert status["invocation_count"] == 0
            assert status["uptime_seconds"] is not None
            assert status["uptime_seconds"] >= 0
            await process.stop()

    def test_status_before_start(self) -> None:
        process = AgentProcess("test", ProcessConfig())
        status = process.status()
        assert status["state"] == "created"
        assert status["uptime_seconds"] is None

    def test_repr(self) -> None:
        process = AgentProcess("test", ProcessConfig())
        r = repr(process)
        assert "test" in r
        assert "created" in r


# =========================================================================
# Trigger Processing
# =========================================================================


class TestAgentProcessTriggers:
    """Verify trigger event injection and processing."""

    async def test_inject_event_invokes_agent(self) -> None:
        agent = _make_mock_agent()
        with _patch_build(agent):
            process = AgentProcess("test", ProcessConfig())
            await process.start()

            event = TriggerEvent(
                trigger_id="manual",
                trigger_type="manual",
                payload={"message": "hello"},
            )
            await process.inject(event)

            # Give worker time to process
            await asyncio.sleep(0.15)

            assert process._invocation_count == 1
            assert agent.ainvoke.await_count == 1
            await process.stop()

    async def test_multiple_events_processed(self) -> None:
        with _patch_build():
            process = AgentProcess("test", ProcessConfig())
            await process.start()

            for i in range(3):
                await process.inject(
                    TriggerEvent(
                        trigger_id="manual",
                        trigger_type="manual",
                        payload={"i": i},
                    )
                )

            # Give workers time
            await asyncio.sleep(0.5)

            assert process._invocation_count == 3
            await process.stop()

    async def test_queue_size_in_status(self) -> None:
        process = AgentProcess("test", ProcessConfig())
        event = TriggerEvent(trigger_id="t", trigger_type="test")
        await process.inject(event)
        status = process.status()
        assert status["queue_size"] == 1


# =========================================================================
# Concurrency
# =========================================================================


class TestAgentProcessConcurrency:
    """Verify concurrency semaphore limits parallel invocations."""

    async def test_concurrency_limit(self) -> None:
        agent = _make_mock_agent()

        # Track concurrent calls
        max_concurrent = 0
        current_concurrent = 0
        lock = asyncio.Lock()

        async def slow_invoke(x: dict, **kwargs) -> dict:
            nonlocal max_concurrent, current_concurrent
            async with lock:
                current_concurrent += 1
                max_concurrent = max(max_concurrent, current_concurrent)
            await asyncio.sleep(0.1)
            async with lock:
                current_concurrent -= 1
            return {"messages": [{"role": "assistant", "content": "ok"}]}

        agent.ainvoke = AsyncMock(side_effect=slow_invoke)

        with _patch_build(agent):
            config = ProcessConfig(concurrency=2)
            process = AgentProcess("test", config)
            await process.start()

            # Inject 4 events
            for i in range(4):
                await process.inject(
                    TriggerEvent(trigger_id="t", trigger_type="test", payload={"i": i})
                )

            # Wait for all to complete
            await asyncio.sleep(0.6)

            assert max_concurrent <= 2
            assert process._invocation_count >= 4
            await process.stop()


# =========================================================================
# Error Handling
# =========================================================================


class TestAgentProcessFailures:
    """Verify consecutive failure handling."""

    async def test_consecutive_failures_trigger_failed_state(self) -> None:
        agent = _make_mock_agent()
        agent.ainvoke = AsyncMock(side_effect=RuntimeError("boom"))

        with _patch_build(agent):
            config = ProcessConfig(max_consecutive_failures=2)
            process = AgentProcess("test", config)
            await process.start()

            for _ in range(3):
                await process.inject(TriggerEvent(trigger_id="t", trigger_type="test"))

            await asyncio.sleep(0.3)
            assert process.state == ProcessState.FAILED
            await process.stop()

    async def test_success_resets_failure_count(self) -> None:
        agent = _make_mock_agent()
        call_count = 0

        async def sometimes_fail(x: dict, **kwargs) -> dict:
            nonlocal call_count
            call_count += 1
            if call_count == 1:
                raise RuntimeError("fail once")
            return {"messages": [{"role": "assistant", "content": "ok"}]}

        agent.ainvoke = AsyncMock(side_effect=sometimes_fail)

        with _patch_build(agent):
            config = ProcessConfig(max_consecutive_failures=3)
            process = AgentProcess("test", config)
            await process.start()

            await process.inject(TriggerEvent(trigger_id="t", trigger_type="test"))
            await asyncio.sleep(0.15)
            await process.inject(TriggerEvent(trigger_id="t", trigger_type="test"))
            await asyncio.sleep(0.15)

            assert process.state == ProcessState.RUNNING
            assert process._consecutive_failures == 0
            await process.stop()


# =========================================================================
# Context Integration
# =========================================================================


class TestAgentProcessContext:
    """Verify context is available and configured."""

    def test_context_initialized_from_config(self) -> None:
        config = ProcessConfig()
        config.context = ContextConfig(initial_state={"counter": 0})
        process = AgentProcess("test", config)
        assert process.context.get("counter") == 0

    def test_context_writable_keys(self) -> None:
        config = ProcessConfig()
        config.context = ContextConfig(
            initial_state={"counter": 0, "name": "test"},
            writable_keys=["counter"],
        )
        process = AgentProcess("test", config)
        process.context.put("counter", 1)
        assert process.context.get("counter") == 1
        with pytest.raises(KeyError):
            process.context.put("name", "changed")


# =========================================================================
# Agent Build Failure
# =========================================================================


class TestAgentProcessBuildFailure:
    """Verify handling when agent building fails."""

    async def test_build_failure_transitions_to_failed(self) -> None:
        with patch(
            BUILD_TARGET,
            new_callable=lambda: AsyncMock(side_effect=RuntimeError("no API key")),
        ):
            process = AgentProcess("test", ProcessConfig())
            with pytest.raises(RuntimeError, match="no API key"):
                await process.start()
            assert process.state == ProcessState.FAILED
