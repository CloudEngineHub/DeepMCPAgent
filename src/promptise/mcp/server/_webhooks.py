"""Webhook notification middleware for MCP servers.

Fires HTTP webhooks on tool events (call, success, error), in the
background so a slow endpoint never delays a tool call.

Example::

    from promptise.mcp.server import MCPServer, WebhookMiddleware

    server = MCPServer(name="api")
    server.add_middleware(WebhookMiddleware(
        url="https://hooks.slack.com/...",
        events={"tool.error", "tool.success"},
        headers={"Authorization": "Bearer ..."},
    ))
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections import deque
from collections.abc import Callable
from typing import Any

from ._context import RequestContext

logger = logging.getLogger("promptise.server")


class WebhookMiddleware:
    """Fire webhooks on tool call events.

    Supports ``tool.success``, ``tool.error``, and ``tool.call`` events.
    Deliveries never block the tool call: each event is put on a bounded
    queue and sent by background tasks, at most *max_concurrency* at a time,
    one HTTP client per task.  When the queue is full the event is dropped
    (counted in :attr:`dropped`, with a warning).  Failed deliveries are
    logged at debug level and not retried.

    Call :meth:`flush` to wait for queued deliveries (tests, shutdown) and
    :meth:`aclose` to stop the delivery tasks.

    Args:
        url: Webhook endpoint URL.
        events: Set of event types to fire on (default: all).
        headers: Extra HTTP headers for the webhook request.
        timeout: HTTP timeout in seconds (default ``5.0``).
        max_queue: Events waiting for delivery at most (default ``1000``).
        max_concurrency: Deliveries in flight at most (default ``4``).
        history: Payloads kept in :attr:`sent` (default ``100``, the
            most recent).
    """

    def __init__(
        self,
        url: str,
        *,
        events: set[str] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 5.0,
        max_queue: int = 1000,
        max_concurrency: int = 4,
        history: int = 100,
    ) -> None:
        try:
            import httpx  # noqa: F401
        except ImportError:
            raise ImportError(
                "httpx is required to use WebhookMiddleware. Install with: pip install httpx"
            )
        if max_queue < 1 or max_concurrency < 1 or history < 0:
            raise ValueError("max_queue and max_concurrency must be >= 1, history >= 0")
        self._url = url
        self._events = events or {"tool.success", "tool.error", "tool.call"}
        self._headers = headers or {}
        self._timeout = timeout
        self._max_queue = max_queue
        self._max_concurrency = max_concurrency
        self._sent: deque[dict[str, Any]] = deque(maxlen=history)
        self._queue: asyncio.Queue[dict[str, Any]] | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._workers: set[asyncio.Task[None]] = set()
        #: Events dropped because the delivery queue was full.
        self.dropped = 0

    async def __call__(self, ctx: RequestContext, call_next: Callable[..., Any]) -> Any:
        if "tool.call" in self._events:
            self._fire("tool.call", ctx)

        try:
            result = await call_next(ctx)
            if "tool.success" in self._events:
                self._fire("tool.success", ctx)
            return result
        except Exception as exc:
            if "tool.error" in self._events:
                self._fire("tool.error", ctx, error=str(exc))
            raise

    def _fire(
        self,
        event: str,
        ctx: RequestContext,
        *,
        error: str | None = None,
    ) -> None:
        """Queue a webhook payload for delivery."""
        payload: dict[str, Any] = {
            "event": event,
            "tool": ctx.tool_name,
            "client_id": ctx.client_id,
            "request_id": ctx.request_id,
            "timestamp": time.time(),
        }
        if error:
            payload["error"] = error

        self._sent.append(payload)

        queue = self._queue_for_loop()
        try:
            queue.put_nowait(payload)
        except asyncio.QueueFull:
            self.dropped += 1
            logger.warning(
                "Webhook queue full (%d events): dropped a %s event (%d dropped so far)",
                self._max_queue,
                event,
                self.dropped,
            )
            return
        if len(self._workers) < self._max_concurrency:
            task = asyncio.get_running_loop().create_task(self._deliver(queue))
            self._workers.add(task)
            task.add_done_callback(self._workers.discard)

    def _queue_for_loop(self) -> asyncio.Queue[dict[str, Any]]:
        """The delivery queue of the running event loop."""
        loop = asyncio.get_running_loop()
        if self._queue is None or self._loop is not loop:
            self._loop = loop
            self._queue = asyncio.Queue(maxsize=self._max_queue)
            self._workers = set()
        return self._queue

    async def _deliver(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        """Send queued payloads until the queue is empty."""
        import httpx

        async with httpx.AsyncClient(timeout=self._timeout) as client:
            while True:
                try:
                    payload = queue.get_nowait()
                except asyncio.QueueEmpty:
                    return
                try:
                    await client.post(
                        self._url,
                        json=payload,
                        headers={
                            "Content-Type": "application/json",
                            **self._headers,
                        },
                    )
                except Exception as exc:
                    logger.debug("Webhook delivery failed: %s", exc)
                finally:
                    queue.task_done()

    async def flush(self, timeout: float | None = None) -> None:
        """Wait until every queued event has been delivered (or has failed).

        Raises:
            TimeoutError: When *timeout* seconds pass first.
        """
        if self._queue is None or self._loop is not asyncio.get_running_loop():
            return
        await asyncio.wait_for(self._queue.join(), timeout)

    async def aclose(self, timeout: float = 5.0) -> None:
        """Deliver what is queued (up to *timeout* seconds), then stop the delivery tasks."""
        try:
            await self.flush(timeout)
        except (TimeoutError, asyncio.TimeoutError):
            logger.warning("Webhook deliveries still queued after %.1fs; dropping them", timeout)
        workers = list(self._workers)
        for task in workers:
            task.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
        self._workers = set()
        self._queue = None

    @property
    def sent(self) -> list[dict[str, Any]]:
        """The most recent queued webhook payloads (``history`` of them; useful for testing)."""
        return list(self._sent)
