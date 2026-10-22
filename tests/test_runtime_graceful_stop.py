"""Graceful stop: drain runs in flight, journal the ones a stop cancels.

Covers ``AgentProcess.stop(drain_timeout=...)``, ``abort_drain()``,
``ProcessConfig.drain_timeout``, ``AgentRuntime.run_forever`` /
``stop_all`` / ``shutdown`` with a drain, the second-signal escalation,
and the ``run_interrupted`` journal entry plus its recovery into the
dead letters of the next process object.
"""

from __future__ import annotations

import asyncio
import os
import signal
import subprocess
import sys
import textwrap
import time
from collections.abc import Callable
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage

from promptise.runtime import AgentRuntime, JournalConfig, ProcessConfig, RuntimeConfig
from promptise.runtime.journal import FileJournal
from promptise.runtime.lifecycle import ProcessState
from promptise.runtime.process import AgentProcess
from promptise.runtime.triggers.base import TriggerEvent

BUILD_TARGET = "promptise.agent.build_agent"
SRC_DIR = Path(__file__).resolve().parent.parent / "src"


async def _wait_for(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def _event(n: int) -> TriggerEvent:
    return TriggerEvent(trigger_id="t", trigger_type="manual", payload={"n": n})


class _SlowAgent:
    """A mock agent whose runs take *delay* seconds; records finished runs."""

    def __init__(self, delay: float) -> None:
        self.delay = delay
        self.started: list[str] = []
        self.finished: list[str] = []
        self.running = asyncio.Event()
        self.agent = AsyncMock()
        self.agent.ainvoke = AsyncMock(side_effect=self._ainvoke)
        self.agent.shutdown = AsyncMock()

    async def _ainvoke(self, inp: dict[str, Any], config: Any = None) -> dict[str, Any]:
        content = inp["messages"][-1]["content"]
        self.started.append(content)
        self.running.set()
        await asyncio.sleep(self.delay)
        self.finished.append(content)
        return {"messages": [*inp["messages"], AIMessage(content="done")]}

    def patch(self) -> Any:
        return patch(BUILD_TARGET, new_callable=lambda: AsyncMock(return_value=self.agent))


class TestProcessDrain:
    async def test_run_in_flight_finishes_within_the_drain(self) -> None:
        slow = _SlowAgent(0.3)
        with slow.patch():
            process = AgentProcess("drain", ProcessConfig())
            await process.start()
            await process.inject(_event(1))
            await slow.running.wait()
            await process.stop(drain_timeout=5)
        assert len(slow.finished) == 1
        assert process.dead_letters == []
        assert process.state == ProcessState.STOPPED

    async def test_default_stop_still_cancels_at_once(self) -> None:
        slow = _SlowAgent(30)
        with slow.patch():
            process = AgentProcess("now", ProcessConfig(drain_timeout=30))
            await process.start()
            await process.inject(_event(1))
            await slow.running.wait()
            started = time.monotonic()
            await process.stop()
        assert time.monotonic() - started < 5
        [letter] = process.dead_letters
        assert letter["error"] == "run cancelled"

    async def test_run_still_going_at_the_deadline_is_cancelled(self) -> None:
        slow = _SlowAgent(30)
        with slow.patch():
            process = AgentProcess("deadline", ProcessConfig())
            await process.start()
            await process.inject(_event(1))
            await slow.running.wait()
            started = time.monotonic()
            await process.stop(drain_timeout=0.2)
            elapsed = time.monotonic() - started
        assert 0.2 <= elapsed < 5
        [letter] = process.dead_letters
        assert letter["reason"] == "process stopped"
        assert letter["error"] == "run cancelled"
        assert slow.finished == []

    async def test_none_uses_the_configured_drain_timeout(self) -> None:
        slow = _SlowAgent(0.3)
        with slow.patch():
            process = AgentProcess("cfg", ProcessConfig(drain_timeout=5))
            await process.start()
            await process.inject(_event(1))
            await slow.running.wait()
            await process.stop(drain_timeout=None)
        assert len(slow.finished) == 1
        assert process.dead_letters == []

    def test_default_drain_timeout(self) -> None:
        assert ProcessConfig().drain_timeout == 5.0
        with pytest.raises(ValueError):
            ProcessConfig(drain_timeout=-1)

    async def test_queued_events_do_not_start_during_the_drain(self) -> None:
        slow = _SlowAgent(0.3)
        with slow.patch():
            process = AgentProcess("queued", ProcessConfig())
            await process.start()
            await process.inject(_event(1))
            await slow.running.wait()
            await process.inject(_event(2))
            await process.stop(drain_timeout=5)
        assert len(slow.started) == 1
        assert len(slow.finished) == 1
        [letter] = process.dead_letters
        assert letter["event"].payload == {"n": 2}
        assert letter["reason"] == "process stopped"
        assert letter["error"] is None

    async def test_new_events_are_refused_while_draining(self) -> None:
        slow = _SlowAgent(0.5)
        with slow.patch():
            process = AgentProcess("refuse", ProcessConfig())
            await process.start()
            await process.inject(_event(1))
            await slow.running.wait()
            stopping = asyncio.ensure_future(process.stop(drain_timeout=5))
            await _wait_for(lambda: process._draining)
            # A webhook answers 503 with this reason; inject() dead-letters.
            assert process._availability_reason() == "process stopped"
            await process.inject(_event(2))
            assert [e["event"].payload for e in process.dead_letters] == [{"n": 2}]
            await stopping
        assert len(slow.started) == 1

    async def test_abort_drain_cancels_at_once(self) -> None:
        slow = _SlowAgent(30)
        with slow.patch():
            process = AgentProcess("abort", ProcessConfig())
            await process.start()
            await process.inject(_event(1))
            await slow.running.wait()
            stopping = asyncio.ensure_future(process.stop(drain_timeout=30))
            await _wait_for(lambda: process._drain_abort is not None)
            started = time.monotonic()
            process.abort_drain()
            await asyncio.wait_for(stopping, 5)
        assert time.monotonic() - started < 5
        [letter] = process.dead_letters
        assert letter["error"] == "run cancelled"

    async def test_drain_without_runs_returns_at_once(self) -> None:
        slow = _SlowAgent(0)
        with slow.patch():
            process = AgentProcess("idle", ProcessConfig())
            await process.start()
            started = time.monotonic()
            await process.stop(drain_timeout=30)
        assert time.monotonic() - started < 5
        assert process.state == ProcessState.STOPPED

    async def test_restart_after_a_drained_stop_runs_events(self) -> None:
        slow = _SlowAgent(0)
        with slow.patch():
            process = AgentProcess("again", ProcessConfig())
            await process.start()
            await process.stop(drain_timeout=1)
            await process.start()
            await process.inject(_event(1))
            await _wait_for(lambda: len(slow.finished) == 1)
            await process.stop()


class TestInterruptedRunsAreKept:
    def _config(self, tmp_path: Path) -> ProcessConfig:
        return ProcessConfig(journal=JournalConfig(level="checkpoint", path=str(tmp_path)))

    async def test_cancelled_run_is_journaled(self, tmp_path) -> None:
        slow = _SlowAgent(30)
        with slow.patch():
            process = AgentProcess("journaled", self._config(tmp_path))
            await process.start()
            await process.inject(_event(7))
            await slow.running.wait()
            await process.stop()
        entries = await FileJournal(str(tmp_path)).read("journaled", entry_type="run_interrupted")
        [entry] = entries
        assert entry.data["event"]["payload"] == {"n": 7}
        assert entry.data["reason"] == "process stopped"
        assert entry.data["error"] == "run cancelled"
        checkpoint = await FileJournal(str(tmp_path)).last_checkpoint("journaled")
        assert checkpoint is not None
        [kept] = checkpoint["interrupted_runs"]
        assert kept["event"]["event_id"] == entry.data["event"]["event_id"]

    async def test_next_process_lists_it_and_can_redeliver(self, tmp_path) -> None:
        slow = _SlowAgent(30)
        with slow.patch():
            first = AgentProcess("kept", self._config(tmp_path))
            await first.start()
            await first.inject(_event(7))
            await slow.running.wait()
            await first.stop()
            [cut] = first.dead_letters
            await first.close()

        fast = _SlowAgent(0)
        with fast.patch():
            second = AgentProcess("kept", self._config(tmp_path))
            await second.start()
            assert second.recovered is not None
            assert second.recovered["interrupted_runs"] == 1
            [letter] = second.dead_letters
            assert letter["event"].event_id == cut["event"].event_id
            assert letter["event"].payload == {"n": 7}
            assert letter["error"] == "run cancelled"
            assert await second.redeliver_dead_letters() == 1
            await _wait_for(lambda: len(fast.finished) == 1)
            await second.stop()
            await second.close()

            # Redelivered and run: the third process doesn't get it again.
            third = AgentProcess("kept", self._config(tmp_path))
            await third.start()
            assert third.dead_letters == []
            assert third.recovered is not None
            assert third.recovered["interrupted_runs"] == 0
            await third.stop()

    async def test_unredelivered_runs_survive_another_stop(self, tmp_path) -> None:
        slow = _SlowAgent(30)
        with slow.patch():
            first = AgentProcess("twice", self._config(tmp_path))
            await first.start()
            await first.inject(_event(7))
            await slow.running.wait()
            await first.stop()
            await first.close()
            second = AgentProcess("twice", self._config(tmp_path))
            await second.start()
            await second.stop()
            await second.close()
            third = AgentProcess("twice", self._config(tmp_path))
            await third.start()
            assert [e["event"].payload for e in third.dead_letters] == [{"n": 7}]
            await third.stop()

    async def test_queued_events_are_not_journaled_as_interrupted(self, tmp_path) -> None:
        slow = _SlowAgent(0)
        with slow.patch():
            process = AgentProcess("queued", self._config(tmp_path))
            await process.start()
            await process.suspend()
            await process.inject(_event(1))
            await process.stop()
        assert await FileJournal(str(tmp_path)).read("queued", entry_type="run_interrupted") == []


class TestRuntimeDrain:
    async def test_run_forever_lets_the_run_finish(self) -> None:
        slow = _SlowAgent(0.3)
        with slow.patch():
            runtime = AgentRuntime(RuntimeConfig(processes={"a": ProcessConfig()}))
            task = asyncio.ensure_future(runtime.run_forever(install_signal_handlers=False))
            await _wait_for(lambda: runtime.processes["a"].state == ProcessState.RUNNING)
            await runtime.processes["a"].inject(_event(1))
            await slow.running.wait()
            runtime.request_shutdown()
            await asyncio.wait_for(task, 10)
        assert len(slow.finished) == 1
        assert runtime.processes["a"].dead_letters == []
        assert runtime.processes["a"].state == ProcessState.STOPPED

    async def test_run_forever_drain_timeout_zero_cancels(self) -> None:
        slow = _SlowAgent(30)
        with slow.patch():
            runtime = AgentRuntime(RuntimeConfig(processes={"a": ProcessConfig()}))
            task = asyncio.ensure_future(
                runtime.run_forever(install_signal_handlers=False, drain_timeout=0)
            )
            await _wait_for(lambda: runtime.processes["a"].state == ProcessState.RUNNING)
            await runtime.processes["a"].inject(_event(1))
            await slow.running.wait()
            runtime.request_shutdown()
            await asyncio.wait_for(task, 5)
        [letter] = runtime.processes["a"].dead_letters
        assert letter["error"] == "run cancelled"

    async def test_second_signal_cancels_the_drain(self) -> None:
        slow = _SlowAgent(30)
        with slow.patch():
            runtime = AgentRuntime(RuntimeConfig(processes={"a": ProcessConfig(drain_timeout=60)}))
            task = asyncio.ensure_future(runtime.run_forever(install_signal_handlers=False))
            await _wait_for(lambda: runtime.processes["a"].state == ProcessState.RUNNING)
            await runtime.processes["a"].inject(_event(1))
            await slow.running.wait()
            runtime._on_signal(signal.SIGTERM)
            await _wait_for(lambda: runtime.processes["a"]._drain_abort is not None)
            runtime._on_signal(signal.SIGTERM)
            await asyncio.wait_for(task, 5)
        [letter] = runtime.processes["a"].dead_letters
        assert letter["error"] == "run cancelled"

    async def test_stop_all_drains_processes_concurrently(self) -> None:
        slow = _SlowAgent(0.5)
        with slow.patch():
            runtime = AgentRuntime(
                RuntimeConfig(processes={"a": ProcessConfig(), "b": ProcessConfig()})
            )
            await runtime.start_all()
            await runtime.processes["a"].inject(_event(1))
            await runtime.processes["b"].inject(_event(2))
            await _wait_for(lambda: len(slow.started) == 2)
            started = time.monotonic()
            await runtime.stop_all(drain_timeout=5)
            elapsed = time.monotonic() - started
            await runtime.shutdown()
        assert len(slow.finished) == 2
        assert elapsed < 0.95  # both runs drained in parallel, not one after the other

    @pytest.mark.skipif(os.name != "posix", reason="SIGTERM can't be caught on Windows")
    def test_sigterm_lets_the_run_in_flight_finish(self, tmp_path) -> None:
        script = tmp_path / "serve.py"
        script.write_text(
            textwrap.dedent(
                """
                import asyncio, os, signal
                from unittest.mock import AsyncMock, patch
                from langchain_core.messages import AIMessage
                from promptise.runtime import AgentRuntime, ProcessConfig, RuntimeConfig
                from promptise.runtime.triggers.base import TriggerEvent

                finished = []

                async def ainvoke(inp, config=None):
                    loop = asyncio.get_running_loop()
                    loop.call_later(0.1, os.kill, os.getpid(), signal.SIGTERM)
                    await asyncio.sleep(1.0)
                    finished.append(1)
                    return {"messages": [*inp["messages"], AIMessage(content="ok")]}

                agent = AsyncMock()
                agent.ainvoke = AsyncMock(side_effect=ainvoke)
                agent.shutdown = AsyncMock()

                async def main():
                    runtime = AgentRuntime(RuntimeConfig(processes={"a": ProcessConfig()}))
                    process = runtime.processes["a"]

                    async def feed():
                        while process.state.value != "running":
                            await asyncio.sleep(0.01)
                        await process.inject(TriggerEvent(trigger_id="t", payload={"n": 1}))

                    feeder = asyncio.ensure_future(feed())
                    await runtime.run_forever()
                    await feeder
                    print("state:", process.state.value)
                    print("finished:", len(finished))
                    print("dead letters:", len(process.dead_letters))

                with patch("promptise.agent.build_agent", new=AsyncMock(return_value=agent)):
                    asyncio.run(main())
                """
            )
        )
        env = dict(os.environ, PYTHONPATH=str(SRC_DIR))
        result = subprocess.run(
            [sys.executable, str(script)], env=env, capture_output=True, text=True, timeout=60
        )
        assert result.returncode == 0, result.stderr
        assert "state: stopped" in result.stdout
        assert "finished: 1" in result.stdout
        assert "dead letters: 0" in result.stdout
