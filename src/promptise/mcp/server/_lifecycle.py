"""Server lifecycle management: startup and shutdown hooks."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from typing import Any

logger = logging.getLogger("promptise.server")


class LifecycleManager:
    """Manages ordered startup and shutdown hooks.

    Hooks are executed in registration order on startup and in
    *reverse* order on shutdown (matching resource-cleanup semantics).
    """

    def __init__(self) -> None:
        self._startup_hooks: list[Callable[[], Any]] = []
        self._shutdown_hooks: list[Callable[[], Any]] = []
        self._started = False

    def add_startup(self, func: Callable[[], Any]) -> None:
        self._startup_hooks.append(func)

    def add_shutdown(self, func: Callable[[], Any]) -> None:
        self._shutdown_hooks.append(func)

    async def startup(self) -> None:
        """Run all startup hooks in order."""
        for hook in self._startup_hooks:
            result = hook()
            if asyncio.iscoroutine(result):
                await result
        self._started = True
        logger.debug("Lifecycle startup complete (%d hooks)", len(self._startup_hooks))

    async def shutdown(self, *, timeout: float | None = None) -> None:
        """Run all shutdown hooks in reverse order.

        Errors in individual hooks are logged but do not prevent
        remaining hooks from executing.

        Args:
            timeout: Max seconds to wait for all hooks to complete.
                ``None`` means wait indefinitely.  If exceeded, remaining
                hooks are skipped and a warning is logged.
        """

        async def _run_hooks() -> None:
            for hook in reversed(self._shutdown_hooks):
                try:
                    result = hook()
                    if asyncio.iscoroutine(result):
                        await result
                except Exception:
                    logger.exception("Error in shutdown hook %s", hook.__name__)

        if timeout is not None and timeout > 0:
            try:
                await asyncio.wait_for(_run_hooks(), timeout=timeout)
            except asyncio.TimeoutError:
                logger.warning(
                    "Shutdown timed out after %.1fs — some hooks may not have completed",
                    timeout,
                )
        else:
            await _run_hooks()

        self._started = False
        logger.debug("Lifecycle shutdown complete (%d hooks)", len(self._shutdown_hooks))

    @property
    def is_started(self) -> bool:
        return self._started
