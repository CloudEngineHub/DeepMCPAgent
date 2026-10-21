"""Run MCP servers in-process on a loopback port, and stop them cleanly.

Every test that starts a real HTTP/SSE server inside the pytest process goes
through :func:`serve_mcp` (an ``MCPServer``) or :func:`serve_asgi` (any ASGI
app), so they all share one start/stop routine.

Why it matters: sse-starlette keeps a process-wide flag,
``AppStatus.should_exit``, that a real ``SIGTERM`` sets to drain open SSE
streams.  Its shutdown watcher also polls the ``should_exit`` of whichever
uvicorn server owns the ``SIGTERM`` handler.  In one process with several
servers (nested, restarted, or one per test) a stopped server would leave
the flag set, and every later SSE response, including the next server's
``initialize`` answer, then closes at once: the client waits forever.

The helpers here therefore:

* never let a test server install signal handlers, so the watcher can never
  latch onto a test server and set the flag behind our back (and pytest's
  own Ctrl-C handling keeps working);
* set the flag while a server stops, to drain its SSE streams as a real
  ``SIGTERM`` would, and always clear it once the server has stopped.

``conftest.py`` also clears the flag before every test, as a backstop for a
server started some other way.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager, contextmanager
from typing import Any

import uvicorn

# The real class, captured before any test patches ``uvicorn.Server``.
_UvicornServer = uvicorn.Server

_START_POLLS = 400
_POLL_SECONDS = 0.025
_STOP_SECONDS = 15.0


def reset_sse_shutdown() -> None:
    """Clear sse-starlette's process-wide shutdown flag (no-op without it)."""
    try:
        from sse_starlette.sse import AppStatus
    except ImportError:  # pragma: no cover - sse-starlette is a dependency
        return
    AppStatus.should_exit = False


def _drain_sse_streams() -> None:
    """Ask open SSE streams to close, as a real ``SIGTERM`` would."""
    try:
        from sse_starlette.sse import AppStatus
    except ImportError:  # pragma: no cover
        return
    AppStatus.should_exit = True


class _TestServer(_UvicornServer):
    """A uvicorn server that leaves the process's signal handlers alone."""

    @contextmanager
    def capture_signals(self) -> Iterator[None]:
        yield


async def _wait_started(server: uvicorn.Server, task: asyncio.Future[Any]) -> int:
    for _ in range(_START_POLLS):
        if task.done():
            task.result()  # surfaces a startup failure
            raise RuntimeError("server exited before it started")
        if server.started:
            return int(server.servers[0].sockets[0].getsockname()[1])
        await asyncio.sleep(_POLL_SECONDS)
    raise RuntimeError("server did not start")


async def _stop(server: uvicorn.Server | None, task: asyncio.Future[Any]) -> None:
    try:
        if server is not None:
            _drain_sse_streams()
            server.should_exit = True
        try:
            await asyncio.wait_for(task, _STOP_SECONDS)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            task.cancel()
    finally:
        reset_sse_shutdown()


@asynccontextmanager
async def serve_mcp(
    server: Any,
    *,
    port: int = 0,
    transport: str = "http",
    graceful_timeout: float = 5.0,
    **run_kwargs: Any,
) -> AsyncIterator[int]:
    """Run *server* (an ``MCPServer``) through ``run_async``; yield its port.

    ``port=0`` binds a free ephemeral port; pass a port to restart a server
    where a stopped one listened.  Extra keyword arguments go to
    ``run_async`` (``cors=``, ``allowed_origins=``, ...).
    """
    instances: list[uvicorn.Server] = []

    class _Recording(_TestServer):
        def __init__(self, config: uvicorn.Config) -> None:
            super().__init__(config)
            instances.append(self)

    # Patched only until run_async has built its server, so a nested
    # serve_mcp never subclasses another one's recorder.
    previous = uvicorn.Server
    uvicorn.Server = _Recording  # type: ignore[misc]
    task = asyncio.ensure_future(
        server.run_async(transport=transport, host="127.0.0.1", port=port, **run_kwargs)
    )
    try:
        try:
            for _ in range(_START_POLLS):
                if instances or task.done():
                    break
                await asyncio.sleep(_POLL_SECONDS)
        finally:
            uvicorn.Server = previous  # type: ignore[misc]
        if not instances:
            if task.done():
                task.result()
            raise RuntimeError("server did not start")
        bound = await _wait_started(instances[0], task)
        instances[0].config.timeout_graceful_shutdown = graceful_timeout
        yield bound
    finally:
        await _stop(instances[0] if instances else None, task)


@asynccontextmanager
async def serve_asgi(
    app: Any, *, graceful_timeout: float = 2.0, **config: Any
) -> AsyncIterator[int]:
    """Run an ASGI *app* under uvicorn on ``127.0.0.1:<ephemeral>``; yield the port.

    Extra keyword arguments go to ``uvicorn.Config`` (``timeout_keep_alive=``, ...).
    """
    server = _TestServer(
        uvicorn.Config(app, host="127.0.0.1", port=0, log_level="warning", **config)
    )
    task = asyncio.ensure_future(server.serve())
    try:
        bound = await _wait_started(server, task)
        server.config.timeout_graceful_shutdown = graceful_timeout
        yield bound
    finally:
        await _stop(server, task)
