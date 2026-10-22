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


# ---------------------------------------------------------------------------
# Control API and CLI: drain on stop
# ---------------------------------------------------------------------------


async def _started_api(runtime: AgentRuntime, **kwargs: Any) -> Any:
    from promptise.runtime.api import OrchestrationAPI

    api = OrchestrationAPI(runtime, host="127.0.0.1", port=0, **kwargs)
    await api.start()
    return api


async def _post(url: str, body: Any = None) -> tuple[int, dict[str, Any]]:
    from aiohttp import ClientSession

    async with ClientSession() as session:
        async with session.post(url, json=body) as resp:
            return resp.status, await resp.json(content_type=None)


class TestApiStopDrain:
    async def test_drain_timeout_lets_the_run_finish(self) -> None:
        from promptise.runtime.api_client import OrchestrationClient

        slow = _SlowAgent(0.3)
        with slow.patch():
            runtime = AgentRuntime()
            await runtime.add_process("w", ProcessConfig(drain_timeout=0))
            await runtime.start_process("w")
            api = await _started_api(runtime)
            try:
                await runtime.processes["w"].inject(_event(1))
                await slow.running.wait()
                async with OrchestrationClient(f"http://127.0.0.1:{api.port}") as client:
                    result = await client.stop_process("w", drain_timeout=5)
            finally:
                await api.stop()
                await runtime.shutdown()
        assert result == {
            "name": "w",
            "state": "stopped",
            "drain_timeout": 5.0,
            "interrupted_runs": 0,
        }
        assert len(slow.finished) == 1
        assert runtime.processes["w"].dead_letters == []

    async def test_default_is_the_configured_drain_timeout(self) -> None:
        slow = _SlowAgent(0.3)
        with slow.patch():
            runtime = AgentRuntime()
            await runtime.add_process("w", ProcessConfig(drain_timeout=5))
            await runtime.start_process("w")
            api = await _started_api(runtime)
            try:
                await runtime.processes["w"].inject(_event(1))
                await slow.running.wait()
                status, body = await _post(f"http://127.0.0.1:{api.port}/api/v1/processes/w/stop")
            finally:
                await api.stop()
                await runtime.shutdown()
        assert status == 200
        assert body["drain_timeout"] == 5.0
        assert body["interrupted_runs"] == 0
        assert len(slow.finished) == 1

    async def test_zero_cancels_and_journals_the_run(self, tmp_path) -> None:
        slow = _SlowAgent(30)
        with slow.patch():
            runtime = AgentRuntime()
            config = ProcessConfig(
                drain_timeout=60, journal=JournalConfig(level="checkpoint", path=str(tmp_path))
            )
            await runtime.add_process("w", config)
            await runtime.start_process("w")
            api = await _started_api(runtime)
            try:
                await runtime.processes["w"].inject(_event(7))
                await slow.running.wait()
                started = time.monotonic()
                status, body = await _post(
                    f"http://127.0.0.1:{api.port}/api/v1/processes/w/stop", {"drain_timeout": 0}
                )
                elapsed = time.monotonic() - started
            finally:
                await api.stop()
                await runtime.shutdown()
        assert status == 200
        assert elapsed < 5
        assert body["drain_timeout"] == 0.0
        assert body["interrupted_runs"] == 1
        [letter] = runtime.processes["w"].dead_letters
        assert letter["error"] == "run cancelled"
        [entry] = await FileJournal(str(tmp_path)).read("w", entry_type="run_interrupted")
        assert entry.data["event"]["payload"] == {"n": 7}

    async def test_run_past_the_drain_is_cancelled_and_the_request_returns(self) -> None:
        slow = _SlowAgent(30)
        with slow.patch():
            runtime = AgentRuntime()
            await runtime.add_process("w", ProcessConfig())
            await runtime.start_process("w")
            api = await _started_api(runtime)
            try:
                await runtime.processes["w"].inject(_event(1))
                await slow.running.wait()
                started = time.monotonic()
                status, body = await _post(
                    f"http://127.0.0.1:{api.port}/api/v1/processes/w/stop", {"drain_timeout": 0.3}
                )
                elapsed = time.monotonic() - started
            finally:
                await api.stop()
                await runtime.shutdown()
        assert status == 200
        assert 0.3 <= elapsed < 5
        assert body["interrupted_runs"] == 1
        assert slow.finished == []

    @pytest.mark.parametrize("bad", [-1, 3601, "5", True, [5]])
    async def test_invalid_drain_timeout_is_refused(self, bad: Any) -> None:
        slow = _SlowAgent(0)
        with slow.patch():
            runtime = AgentRuntime()
            await runtime.add_process("w", ProcessConfig())
            await runtime.start_process("w")
            api = await _started_api(runtime)
            base = f"http://127.0.0.1:{api.port}/api/v1"
            try:
                stop = await _post(f"{base}/processes/w/stop", {"drain_timeout": bad})
                stop_all = await _post(f"{base}/runtime/stop-all", {"drain_timeout": bad})
                state = runtime.processes["w"].state
            finally:
                await api.stop()
                await runtime.shutdown()
        for status, body in (stop, stop_all):
            assert status == 422
            assert body["error"]["code"] == "INVALID_FIELD"
            assert "drain_timeout" in body["error"]["message"]
        assert state == ProcessState.RUNNING

    async def test_unknown_process_is_404(self) -> None:
        runtime = AgentRuntime()
        api = await _started_api(runtime)
        try:
            status, body = await _post(
                f"http://127.0.0.1:{api.port}/api/v1/processes/nope/stop", {"drain_timeout": 1}
            )
        finally:
            await api.stop()
        assert status == 404
        assert body["error"]["code"] == "PROCESS_NOT_FOUND"

    async def test_stop_all_drains(self) -> None:
        from promptise.runtime.api_client import OrchestrationClient

        slow = _SlowAgent(0.3)
        with slow.patch():
            runtime = AgentRuntime(
                RuntimeConfig(processes={"a": ProcessConfig(drain_timeout=0), "b": ProcessConfig()})
            )
            await runtime.start_all()
            api = await _started_api(runtime)
            try:
                await runtime.processes["a"].inject(_event(1))
                await runtime.processes["b"].inject(_event(2))
                await _wait_for(lambda: len(slow.started) == 2)
                async with OrchestrationClient(f"http://127.0.0.1:{api.port}") as client:
                    result = await client.stop_all(drain_timeout=5)
            finally:
                await api.stop()
                await runtime.shutdown()
        assert result == {"status": "stopped", "interrupted_runs": 0}
        assert len(slow.finished) == 2

    async def test_stop_all_default_uses_each_configured_drain(self) -> None:
        slow = _SlowAgent(0.5)
        with slow.patch():
            runtime = AgentRuntime(
                RuntimeConfig(
                    processes={"now": ProcessConfig(drain_timeout=0), "wait": ProcessConfig()}
                )
            )
            await runtime.start_all()
            api = await _started_api(runtime)
            try:
                await runtime.processes["now"].inject(_event(1))
                await runtime.processes["wait"].inject(_event(2))
                await _wait_for(lambda: len(slow.started) == 2)
                status, body = await _post(f"http://127.0.0.1:{api.port}/api/v1/runtime/stop-all")
            finally:
                await api.stop()
                await runtime.shutdown()
        assert status == 200
        assert body["interrupted_runs"] == 1
        assert [e["event"].payload for e in runtime.processes["now"].dead_letters] == [{"n": 1}]
        assert runtime.processes["wait"].dead_letters == []

    async def test_shutdown_passes_the_drain_to_the_callback(self) -> None:
        calls: list[dict[str, Any]] = []

        def on_shutdown(drain_timeout: float | None = None) -> None:
            calls.append({"drain_timeout": drain_timeout})

        api = await _started_api(AgentRuntime(), on_shutdown=on_shutdown)
        url = f"http://127.0.0.1:{api.port}/api/v1/runtime/shutdown"
        try:
            with_drain = await _post(url, {"drain_timeout": 12})
            await _wait_for(lambda: len(calls) == 1)
            without = await _post(url)
            await _wait_for(lambda: len(calls) == 2)
            bad = await _post(url, {"drain_timeout": -3})
        finally:
            await api.stop()
        assert with_drain == (202, {"status": "shutting_down", "drain_timeout": 12.0})
        assert without == (202, {"status": "shutting_down"})
        assert calls == [{"drain_timeout": 12.0}, {"drain_timeout": None}]
        assert bad[0] == 422 and bad[1]["error"]["code"] == "INVALID_FIELD"
        assert len(calls) == 2

    async def test_shutdown_callback_without_a_drain_argument(self) -> None:
        called = asyncio.Event()
        api = await _started_api(AgentRuntime(), on_shutdown=called.set)
        url = f"http://127.0.0.1:{api.port}/api/v1/runtime/shutdown"
        try:
            refused = await _post(url, {"drain_timeout": 1})
            assert not called.is_set()
            accepted = await _post(url)
            await asyncio.wait_for(called.wait(), 5)
        finally:
            await api.stop()
        assert refused[0] == 422
        assert "on_shutdown" in refused[1]["error"]["message"]
        assert accepted[0] == 202

    async def test_runtime_request_shutdown_overrides_the_drain(self) -> None:
        slow = _SlowAgent(30)
        with slow.patch():
            runtime = AgentRuntime(RuntimeConfig(processes={"a": ProcessConfig(drain_timeout=60)}))
            task = asyncio.ensure_future(runtime.run_forever(install_signal_handlers=False))
            await _wait_for(lambda: runtime.processes["a"].state == ProcessState.RUNNING)
            await runtime.processes["a"].inject(_event(1))
            await slow.running.wait()
            with pytest.raises(ValueError):
                runtime.request_shutdown(drain_timeout=-1)
            runtime.request_shutdown(drain_timeout=0)
            await asyncio.wait_for(task, 5)
        [letter] = runtime.processes["a"].dead_letters
        assert letter["error"] == "run cancelled"
        assert runtime._shutdown_drain_timeout is None


class _ServerThread:
    """A runtime and its control API on an event loop in another thread
    (the CLI runs its own loop with ``asyncio.run``)."""

    def __init__(self, configs: dict[str, ProcessConfig], **api_kwargs: Any) -> None:
        import threading

        self.loop = asyncio.new_event_loop()
        self.runtime: AgentRuntime | None = None
        self.api: Any = None
        self._configs = configs
        self._api_kwargs = api_kwargs
        self._ready = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._setup())
        self._ready.set()
        self.loop.run_forever()

    async def _setup(self) -> None:
        self.runtime = AgentRuntime(RuntimeConfig(processes=self._configs))
        await self.runtime.start_all()
        self.api = await _started_api(self.runtime, **self._api_kwargs)

    def run(self, coro: Any, timeout: float = 30.0) -> Any:
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.api.port}"

    def __enter__(self) -> _ServerThread:
        self._thread.start()
        assert self._ready.wait(30), "server thread did not start"
        return self

    def __exit__(self, *exc: object) -> None:
        async def teardown() -> None:
            await self.api.stop()
            assert self.runtime is not None
            await self.runtime.shutdown()

        try:
            self.run(teardown())
        finally:
            self.loop.call_soon_threadsafe(self.loop.stop)
            self._thread.join(30)
            self.loop.close()


def _wait_sync(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = time.monotonic() + timeout
    while not cond():
        if time.monotonic() > deadline:
            raise AssertionError("condition not met in time")
        time.sleep(0.01)


class TestCliStopDrain:
    @pytest.fixture(autouse=True)
    def _no_state(self, tmp_path, monkeypatch) -> None:
        from promptise.runtime import _cli_state

        monkeypatch.setenv(_cli_state.STATE_DIR_ENV, str(tmp_path / "state"))
        monkeypatch.delenv(_cli_state.TOKEN_ENV, raising=False)

    def _invoke(self, *args: str) -> Any:
        from typer.testing import CliRunner

        from promptise.runtime.cli import runtime_app

        return CliRunner().invoke(runtime_app, list(args))

    def test_process_stop_drains(self) -> None:
        slow = _SlowAgent(0.3)
        with slow.patch(), _ServerThread({"w": ProcessConfig(drain_timeout=0)}) as server:
            assert server.runtime is not None
            server.run(server.runtime.processes["w"].inject(_event(1)))
            _wait_sync(lambda: len(slow.started) == 1)
            result = self._invoke(
                "stop", "--url", server.url, "--process", "w", "--drain-timeout", "5"
            )
            dead = server.runtime.processes["w"].dead_letters
        assert result.exit_code == 0, result.output
        assert "Stopped process w" in result.output
        assert "cancelled" not in result.output
        assert len(slow.finished) == 1
        assert dead == []

    def test_process_stop_drain_zero_reports_the_cancelled_run(self) -> None:
        slow = _SlowAgent(30)
        with slow.patch(), _ServerThread({"w": ProcessConfig(drain_timeout=60)}) as server:
            assert server.runtime is not None
            server.run(server.runtime.processes["w"].inject(_event(1)))
            _wait_sync(lambda: len(slow.started) == 1)
            started = time.monotonic()
            result = self._invoke("stop", "--url", server.url, "w", "--drain-timeout", "0")
            elapsed = time.monotonic() - started
            [letter] = server.runtime.processes["w"].dead_letters
        assert result.exit_code == 0, result.output
        assert elapsed < 10
        assert "1 run(s) in flight were cancelled" in result.output
        assert letter["error"] == "run cancelled"

    def test_plain_api_falls_back_to_stop_all_with_the_drain(self) -> None:
        slow = _SlowAgent(0.3)
        with slow.patch(), _ServerThread({"w": ProcessConfig(drain_timeout=0)}) as server:
            assert server.runtime is not None
            server.run(server.runtime.processes["w"].inject(_event(1)))
            _wait_sync(lambda: len(slow.started) == 1)
            result = self._invoke("stop", "--url", server.url, "--drain-timeout", "5")
            state = server.runtime.processes["w"].state
        assert result.exit_code == 0, result.output
        assert "Stopped every process" in result.output
        assert state == ProcessState.STOPPED
        assert len(slow.finished) == 1

    @pytest.mark.parametrize("bad", ["-1", "3601", "soon"])
    def test_invalid_drain_timeout_is_a_usage_error(self, bad: str) -> None:
        result = self._invoke("stop", "--url", "http://127.0.0.1:9", "--drain-timeout", bad)
        assert result.exit_code == 2
        assert "--drain-timeout" in result.output

    def test_runtime_stop_sends_the_drain_and_waits_for_it(self, monkeypatch) -> None:
        import os as _os

        from promptise.runtime import _cli_state
        from promptise.runtime import cli as runtime_cli

        state = _cli_state.RuntimeState(
            name="rt",
            pid=_os.getpid(),
            host="127.0.0.1",
            port=9,
            token="t",
            started_at=_cli_state.RuntimeState.now(),
            ready=True,
        )
        _cli_state.write_state(state, exclusive=False)
        sent: list[Any] = []
        waited: list[float] = []

        class _Client:
            async def shutdown(self, *, drain_timeout: float | None = None) -> dict[str, Any]:
                sent.append(drain_timeout)
                return {"status": "shutting_down"}

        async def fake_call(target: Any, action: Any, *, timeout: float = 30.0) -> Any:
            return await action(_Client())

        def fake_wait(pid: int, timeout: float, state: Any = None) -> bool:
            waited.append(timeout)
            return True

        monkeypatch.setattr(runtime_cli, "_call", fake_call)
        monkeypatch.setattr(runtime_cli, "wait_for_exit", fake_wait)
        result = self._invoke("stop", "rt", "--drain-timeout", "45")
        assert result.exit_code == 0, result.output
        assert sent == [45.0]
        assert waited == [55.0]  # --timeout 30 is raised to the drain plus 10 s
        _cli_state.write_state(state, exclusive=False)
        result = self._invoke("stop", "rt")
        assert result.exit_code == 0, result.output
        assert sent == [45.0, None]
        assert waited == [55.0, 30.0]

    async def test_cli_shutdown_request_carries_the_drain_to_run_forever(self) -> None:
        from promptise.runtime import cli as runtime_cli

        slow = _SlowAgent(30)
        with slow.patch():
            runtime = AgentRuntime(RuntimeConfig(processes={"a": ProcessConfig(drain_timeout=60)}))
            shutdown = runtime_cli._ShutdownRequest()
            api = runtime_cli._make_api(runtime, "127.0.0.1", 0, "tok", shutdown)
            await api.start()
            task = asyncio.ensure_future(
                runtime_cli._wait_for_shutdown(runtime, shutdown, lambda: None)
            )
            try:
                await _wait_for(lambda: runtime.processes["a"].state == ProcessState.RUNNING)
                await runtime.processes["a"].inject(_event(1))
                await slow.running.wait()
                from promptise.runtime.api_client import OrchestrationClient

                async with OrchestrationClient(f"http://127.0.0.1:{api.port}", token="tok") as c:
                    answer = await c.shutdown(drain_timeout=0)
                assert await asyncio.wait_for(task, 10) is True
            finally:
                await api.stop()
        assert answer == {"status": "shutting_down", "drain_timeout": 0.0}
        assert shutdown.drain_timeout == 0.0
        [letter] = runtime.processes["a"].dead_letters
        assert letter["error"] == "run cancelled"
