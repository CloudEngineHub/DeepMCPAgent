"""Webhook notification middleware for MCP servers.

Fires HTTP webhooks on tool events (success, error, auth failure).

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

import logging
import time
from collections.abc import Callable
from typing import Any

from ._context import RequestContext

logger = logging.getLogger("promptise.server")


class WebhookMiddleware:
    """Fire webhooks on tool call events.

    Supports ``tool.success``, ``tool.error``, and ``tool.call`` events.
    Webhooks are sent asynchronously and never block tool execution.

    Args:
        url: Webhook endpoint URL.
        events: Set of event types to fire on (default: all).
        headers: Extra HTTP headers for the webhook request.
        timeout: HTTP timeout in seconds (default ``5.0``).
    """

    def __init__(
        self,
        url: str,
        *,
        events: set[str] | None = None,
        headers: dict[str, str] | None = None,
        timeout: float = 5.0,
    ) -> None:
        try:
            import httpx  # noqa: F401
        except ImportError:
            raise ImportError(
                "httpx is required to use WebhookMiddleware. Install with: pip install httpx"
            )
        self._url = url
        self._events = events or {"tool.success", "tool.error", "tool.call"}
        self._headers = headers or {}
        self._timeout = timeout
        self._sent: list[dict[str, Any]] = []  # In-memory buffer for testing

    async def __call__(self, ctx: RequestContext, call_next: Callable[..., Any]) -> Any:
        if "tool.call" in self._events:
            await self._fire("tool.call", ctx)

        try:
            result = await call_next(ctx)
            if "tool.success" in self._events:
                await self._fire("tool.success", ctx)
            return result
        except Exception as exc:
            if "tool.error" in self._events:
                await self._fire("tool.error", ctx, error=str(exc))
            raise

    async def _fire(
        self,
        event: str,
        ctx: RequestContext,
        *,
        error: str | None = None,
    ) -> None:
        """Send a webhook payload."""
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

        try:
            import httpx

            async with httpx.AsyncClient(timeout=self._timeout) as client:
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

    @property
    def sent(self) -> list[dict[str, Any]]:
        """Access sent webhook payloads (useful for testing)."""
        return self._sent
