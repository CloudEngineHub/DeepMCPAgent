"""Tests for AgentProcess lifecycle, trigger processing, and error handling."""

from __future__ import annotations

import asyncio
import contextlib
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


# =========================================================================
# The process owns its MCP client: stop, restart and hot reload close it
# =========================================================================

_TINY_SERVER = """
import os, sys, time
from mcp.server.fastmcp import FastMCP

with open(sys.argv[1], "a") as f:
    f.write(f"{os.getpid()}\\n")

mcp = FastMCP("tiny")


@mcp.tool()
def ping() -> str:
    \"\"\"Ping.\"\"\"
    return "pong"


mcp.run()
if len(sys.argv) > 2:
    # A slow shutdown: record that it began (the client closed stdin),
    # then take a second to exit.
    with open(sys.argv[2], "a") as f:
        f.write(f"{os.getpid()}\\n")
    time.sleep(1)
"""


def _alive(pid: int) -> bool:
    import os
    import sys

    if sys.platform == "win32":
        # os.kill(pid, 0) is not a liveness probe on Windows (it raises
        # WinError 87); ask the process handle for its exit code instead.
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return False
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


async def _wait_dead(pids: list[int], timeout: float = 10.0) -> list[int]:
    """The PIDs still alive after *timeout* seconds."""
    for _ in range(int(timeout / 0.1)):
        alive = [pid for pid in pids if _alive(pid)]
        if not alive:
            return []
        await asyncio.sleep(0.1)
    return [pid for pid in pids if _alive(pid)]


class _ToolAgent:
    """Stands in for build_agent(): keeps the tools it was built with."""

    def __init__(self, tools: list) -> None:
        self.tools = tools
        self.on_invoke = None

    async def ainvoke(self, input, config=None):
        if self.on_invoke is not None:
            await self.on_invoke(self)
        return {"messages": [{"role": "assistant", "content": "ok"}]}

    async def shutdown(self) -> None:
        pass

    async def ping(self) -> str:
        tool = next(t for t in self.tools if t.name.endswith("ping"))
        return str(await tool.ainvoke({}))


class TestMCPClientLifecycle:
    @pytest.fixture
    def server(self, tmp_path, monkeypatch):
        import sys

        import promptise.agent
        from promptise.config import StdioServerSpec

        script = tmp_path / "tiny_server.py"
        script.write_text(_TINY_SERVER)
        pid_file = tmp_path / "pids"
        agents: list[_ToolAgent] = []

        async def fake_build_agent(**kwargs):
            agent = _ToolAgent(list(kwargs.get("extra_tools") or []))
            agents.append(agent)
            return agent

        monkeypatch.setattr(promptise.agent, "build_agent", fake_build_agent)

        def pids() -> list[int]:
            if not pid_file.exists():
                return []
            return [int(line) for line in pid_file.read_text().split()]

        spec = StdioServerSpec(command=sys.executable, args=[str(script), str(pid_file)])
        return {"tiny": spec}, pids, agents

    async def test_stop_closes_the_server(self, server) -> None:
        servers, pids, _ = server
        process = AgentProcess("leak", ProcessConfig(servers=servers))
        try:
            for cycle in range(1, 3):
                await process.start()
                assert len(pids()) == cycle
                await process.stop()
                assert await _wait_dead(pids()) == []
        finally:
            await process.stop()

    async def test_restart_closes_the_previous_server(self, server) -> None:
        servers, pids, _ = server
        process = AgentProcess("leak", ProcessConfig(servers=servers))
        await process.start()
        try:
            await process._restart(reason="test", counts=False)
            assert process.state == ProcessState.RUNNING
            first, second = pids()
            assert await _wait_dead([first]) == []
            assert _alive(second)
        finally:
            await process.stop()
        assert await _wait_dead(pids()) == []

    async def test_hot_reload_closes_the_previous_server(self, server) -> None:
        from promptise.runtime.config import ExecutionMode

        servers, pids, agents = server
        process = AgentProcess(
            "leak", ProcessConfig(servers=servers, execution_mode=ExecutionMode.OPEN)
        )
        await process.start()
        try:
            assert await process._hot_reload(reason="test") == "Rebuild successful"
            first, second = pids()
            assert await _wait_dead([first]) == []
            assert _alive(second)
            assert await agents[-1].ping() == "pong"
        finally:
            await process.stop()
        assert await _wait_dead(pids()) == []

    async def test_reload_during_a_run_keeps_its_tools_until_it_ends(self, server) -> None:
        from promptise.runtime.config import ExecutionMode

        servers, pids, agents = server
        process = AgentProcess(
            "leak", ProcessConfig(servers=servers, execution_mode=ExecutionMode.OPEN)
        )
        results: list[str] = []

        async def reload_then_call(agent: _ToolAgent) -> None:
            # What an open-mode agent does: a meta-tool reloads it mid-run,
            # and the run goes on with the tools it already has.
            await process._hot_reload(reason="agent asked")
            results.append(await agent.ping())
            results.append(str(_alive(pids()[0])))

        await process.start()
        try:
            agents[0].on_invoke = reload_then_call
            await process.inject(TriggerEvent(trigger_id="t", trigger_type="manual", payload={}))
            for _ in range(200):
                if process.status()["invocation_count"] == 1:
                    break
                await asyncio.sleep(0.05)
            assert results == ["pong", "True"]
            # Once the run is over, the replaced client is closed.
            assert await _wait_dead([pids()[0]]) == []
        finally:
            await process.stop()
        assert await _wait_dead(pids()) == []

    async def test_stop_while_the_replaced_server_closes(self, server, tmp_path) -> None:
        """stop() returns when it cancels a worker still closing a replaced server.

        After a run that reloaded the agent, its worker closes the replaced
        MCP client.  stop() cancelled the worker there, the multi-client
        swallowed the CancelledError, and the worker went back to waiting
        for events while stop() waited for it to end: a hang until CI's
        6-hour job timeout.
        """
        from promptise.runtime.config import ExecutionMode

        servers, pids, agents = server
        closing = tmp_path / "closing"
        spec = servers["tiny"]
        slow = spec.model_copy(update={"args": [*spec.args, str(closing)]})
        process = AgentProcess(
            "leak", ProcessConfig(servers={"tiny": slow}, execution_mode=ExecutionMode.OPEN)
        )

        async def reload(agent: _ToolAgent) -> None:
            await process._hot_reload(reason="agent asked")

        await process.start()
        stopping: asyncio.Task[None] | None = None
        try:
            agents[0].on_invoke = reload
            await process.inject(TriggerEvent(trigger_id="t", trigger_type="manual", payload={}))
            # The replaced server has begun its one-second shutdown, so the
            # worker is waiting for its client to close.
            for _ in range(200):
                if closing.exists():
                    break
                await asyncio.sleep(0.05)
            assert closing.exists()
            stopping = asyncio.create_task(process.stop())
            # Not wait_for(): cancelling stop() on timeout cancels the stuck
            # worker again, which ends it and hides the hang.
            done, _ = await asyncio.wait({stopping}, timeout=15)
            assert done, "stop() is still waiting for the worker it cancelled"
            await stopping
            assert process.state == ProcessState.STOPPED
        finally:
            if stopping is None:
                await process.stop()
            elif not stopping.done():
                # Another stop() would wait for this one's lock; cancelling
                # this one cancels the stuck worker again, which ends it.
                stopping.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await stopping
        assert await _wait_dead(pids()) == []
