"""Background tasks for MCP server handlers.

Run fire-and-forget work after the handler returns its response.
Errors in background tasks are logged but never propagated to the client.

Example::

    from promptise.mcp.server import MCPServer, Depends
    from promptise.mcp.server._background import BackgroundTasks

    @server.tool()
    async def process(data: str, bg: BackgroundTasks = Depends(BackgroundTasks)) -> str:
        bg.add(send_notification, data)
        bg.add(log_analytics, "process", data)
        return "Done"
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("promptise.server")


class BackgroundTasks:
    """Collect and execute background tasks after the handler returns.

    Tasks are added during handler execution and run sequentially
    after the response is sent.  Errors are logged, never raised.

    Can be injected via ``Depends(BackgroundTasks)`` in tool handlers.
    """

    def __init__(self) -> None:
        self._tasks: list[tuple[Callable[..., Any], tuple[Any, ...], dict[str, Any]]] = []

    def add(
        self,
        func: Callable[..., Any],
        *args: Any,
        **kwargs: Any,
    ) -> None:
        """Schedule a task to run after the handler returns.

        Args:
            func: Sync or async callable.
            args: Positional arguments.
            kwargs: Keyword arguments.
        """
        self._tasks.append((func, args, kwargs))

    async def execute(self) -> None:
        """Run all queued tasks sequentially.

        Errors are logged and swallowed — they never reach the client.
        """
        for func, args, kwargs in self._tasks:
            try:
                result = func(*args, **kwargs)
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                logger.exception(
                    "Background task %s failed",
                    getattr(func, "__name__", repr(func)),
                )
        self._tasks.clear()

    @property
    def pending(self) -> int:
        """Number of tasks waiting to be executed."""
        return len(self._tasks)
