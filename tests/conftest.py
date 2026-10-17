"""Pytest configuration and fixtures."""

# ── Test-suite stability hygiene ──────────────────────────────────────────
# Must run BEFORE any test imports torch / transformers / sentence-transformers.
#
# Those ML libraries (used by guardrails, embeddings, RAG) spin up thread and
# process parallelism. Combined with the fork()s that later tests perform
# (subprocess execution, multiprocessing in runtime tests), that parallelism can
# kill the multiprocessing ``resource_tracker`` ("process died unexpectedly") and
# leave subprocess spawning in a degraded state — which made a handful of
# subprocess-based tests (shell interpolation, shell hooks) flake intermittently
# only when run as part of the full suite. Pinning single-threaded, fork-safe
# defaults removes that contention and makes the suite deterministic. This is
# standard hygiene for ML-backed test suites and affects tests only.
import os

os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
os.environ.setdefault("OMP_NUM_THREADS", "1")
os.environ.setdefault("MKL_NUM_THREADS", "1")
os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")

# CLI output under test must be plain text: on GitHub Actions Rich/Typer
# colourise output (FORCE_COLOR / GITHUB_ACTIONS), which interleaves escape
# sequences inside the phrases the CLI tests look for. NO_COLOR switches colour
# off for Rich and Click; typer forces a terminal under GITHUB_ACTIONS unless
# told not to. (Not TERM=dumb: Rich then pins the width to 80 and ignores the
# COLUMNS the CLI tests set on their CliRunner to keep phrases on one line.)
os.environ["NO_COLOR"] = "1"
os.environ["_TYPER_FORCE_DISABLE_TERMINAL"] = "1"
os.environ.pop("FORCE_COLOR", None)

from dotenv import load_dotenv  # noqa: E402

# Load environment variables from .env file
load_dotenv()


import sys  # noqa: E402
from typing import Any  # noqa: E402

import pytest  # noqa: E402


def pytest_configure(config: pytest.Config) -> None:
    """Add the pending asyncio tasks to pytest-timeout's dump of a hung test.

    A thread dump of a hung async test only shows the event loop waiting in
    ``select()``; the tasks' await chains show what it waits for.
    """
    timeout = config.pluginmanager.get_plugin("timeout")
    dump_stacks = getattr(timeout, "dump_stacks", None)
    if dump_stacks is None or getattr(dump_stacks, "with_asyncio_tasks", False):
        return

    def dump_stacks_and_tasks(terminal: Any) -> None:
        dump_stacks(terminal)
        _dump_asyncio_tasks(terminal)

    dump_stacks_and_tasks.with_asyncio_tasks = True  # type: ignore[attr-defined]
    timeout.dump_stacks = dump_stacks_and_tasks


def _dump_asyncio_tasks(terminal: Any) -> None:
    """Write where each task of every running event loop is suspended."""
    import asyncio
    import gc
    import traceback

    for loop in [o for o in gc.get_objects() if isinstance(o, asyncio.AbstractEventLoop)]:
        if not loop.is_running():
            continue
        try:
            tasks = asyncio.all_tasks(loop)
        except RuntimeError:  # the loop changed its task set meanwhile
            continue
        for task in tasks:
            frames = []
            awaiting = task.get_coro()
            # Follow the await chain down to the future it waits for.
            while awaiting is not None:
                frame = getattr(awaiting, "cr_frame", None) or getattr(awaiting, "gi_frame", None)
                if frame is None:
                    break
                frames.append((frame, frame.f_lineno))
                awaiting = getattr(awaiting, "cr_await", None) or getattr(
                    awaiting, "gi_yieldfrom", None
                )
            terminal.sep("~", title=f"asyncio task {task.get_name()}")
            terminal.write("".join(traceback.StackSummary.extract(frames).format()))


@pytest.fixture(autouse=True)
def _clear_sse_shutdown_flag():
    """Start every test with sse-starlette's process-wide shutdown flag clear.

    A uvicorn server stopped in-process can leave ``AppStatus.should_exit``
    set, and every later SSE response then closes at once (the client
    hangs).  ``_live_server`` clears it after each server it stops; this
    catches a server started any other way.  Only touched once a test has
    imported sse-starlette.
    """
    sse = sys.modules.get("sse_starlette.sse")
    if sse is not None:
        sse.AppStatus.should_exit = False
    yield
