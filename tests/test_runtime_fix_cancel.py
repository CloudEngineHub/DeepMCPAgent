"""Cancellation can't be lost on the way to ``AgentProcess.stop()``.

Before Python 3.12, ``asyncio.wait_for`` can swallow a cancellation of the
calling task when the awaited call finishes at the same moment.  A worker
cancelled by ``stop()`` while a hook ran then went back to its queue, and
``stop()`` waited for it forever (seen on Windows CI with Python 3.10/3.11).
"""

from __future__ import annotations

import asyncio

import pytest

from promptise.runtime import HookEvent, ProcessConfig
from promptise.runtime.hooks import HookManager, _run_with_timeout
from promptise.runtime.process import AgentProcess, _cancel_and_wait

pytestmark = pytest.mark.asyncio


class TestRunWithTimeout:
    async def test_returns_when_the_call_finishes(self) -> None:
        done = []

        async def quick() -> None:
            done.append(1)

        await _run_with_timeout(quick(), 1.0)
        assert done == [1]

    async def test_times_out_and_cancels_the_call(self) -> None:
        cancelled = asyncio.Event()

        async def slow() -> None:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                cancelled.set()
                raise

        with pytest.raises(asyncio.TimeoutError):
            await _run_with_timeout(slow(), 0.05)
        assert cancelled.is_set()

    async def test_raises_the_calls_error(self) -> None:
        async def boom() -> None:
            raise ValueError("bad hook")

        with pytest.raises(ValueError, match="bad hook"):
            await _run_with_timeout(boom(), 1.0)

    async def test_the_callers_cancellation_gets_through(self) -> None:
        started = asyncio.Event()

        async def slow() -> None:
            started.set()
            await asyncio.sleep(10)

        caller = asyncio.ensure_future(_run_with_timeout(slow(), 5.0))
        await started.wait()
        caller.cancel()
        with pytest.raises(asyncio.CancelledError):
            await caller

    async def test_a_hook_timeout_still_counts_as_an_error(self) -> None:
        manager = HookManager()

        async def slow(_ctx) -> None:
            await asyncio.sleep(10)

        manager.register(HookEvent.PERMISSION_DENIED, slow, timeout=0.05)
        result = await manager.dispatch(HookEvent.PERMISSION_DENIED, data={})
        assert result.errors and "exceeded its timeout" in str(result.errors[0][1])


class TestCancelAndWait:
    async def test_a_task_that_swallows_one_cancellation_still_ends(self) -> None:
        swallowed = []

        async def stubborn() -> None:
            try:
                await asyncio.sleep(10)
            except asyncio.CancelledError:
                swallowed.append(1)  # like the pre-3.12 wait_for race
            await asyncio.sleep(10)  # carries on until cancelled again

        task = asyncio.ensure_future(stubborn())
        await asyncio.sleep(0)
        await asyncio.wait_for(_cancel_and_wait([task]), timeout=5)
        assert task.done() and swallowed == [1]

    async def test_finished_and_failed_tasks_are_fine(self) -> None:
        async def fail() -> None:
            raise RuntimeError("already failed")

        failed = asyncio.ensure_future(fail())
        finished = asyncio.ensure_future(asyncio.sleep(0))
        await asyncio.sleep(0.01)
        await _cancel_and_wait([failed, finished])
        assert failed.done() and finished.done()

    async def test_stop_ends_a_worker_that_swallowed_its_cancellation(self) -> None:
        process = AgentProcess("stubborn", ProcessConfig())
        await process.start()
        try:
            swallowed = []

            async def stubborn_worker() -> None:
                try:
                    await asyncio.sleep(10)
                except asyncio.CancelledError:
                    swallowed.append(1)
                await asyncio.sleep(10)

            extra = asyncio.ensure_future(stubborn_worker())
            process._worker_tasks.append(extra)
            await asyncio.sleep(0)
        finally:
            await asyncio.wait_for(process.stop(), timeout=10)
        assert extra.done() and swallowed == [1]
