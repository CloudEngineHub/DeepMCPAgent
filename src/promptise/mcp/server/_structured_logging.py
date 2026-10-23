"""Structured JSON logging middleware for MCP servers.

Replaces the built-in ``LoggingMiddleware`` with structured JSON
output suitable for log aggregation (ELK, Datadog, Splunk, etc.).

Example::

    from promptise.mcp.server import MCPServer, StructuredLoggingMiddleware

    server = MCPServer(name="api")
    server.add_middleware(StructuredLoggingMiddleware())
"""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Callable
from typing import Any

from ._context import RequestContext

logger = logging.getLogger("promptise.server")


class StructuredLoggingMiddleware:
    """Emit structured JSON log entries for every tool call.

    Each log entry includes:
    - ``event``: ``"tool_call_start"`` or ``"tool_call_end"``
    - ``tool``: Tool name
    - ``request_id``: Unique request identifier
    - ``client_id``: Authenticated client (if any)
    - ``duration_ms``: Execution time (on completion)
    - ``status``: ``"ok"`` or ``"error"``
    - ``error``: Error message (on failure)
    - ``args``: The validated arguments (on ``tool_call_start``, with
      ``include_args=True``)

    Args:
        log_level: Python logging level (default ``logging.INFO``).
        include_args: Add the call's validated arguments to the
            ``tool_call_start`` entry as ``args`` (default ``False``).
            They pass through the framework's redaction first
            (:func:`promptise.observability.redact_sensitive`): the value
            of a credential key (``password``, ``api_key``, ``token``...)
            and credentials or PII in strings (API keys, JWTs, card
            numbers, email addresses...) are replaced with placeholders.
            Pattern matching can't recognise every kind of personal data,
            so leave this off for tools that take it.
    """

    def __init__(
        self,
        log_level: int = logging.INFO,
        *,
        include_args: bool = False,
    ) -> None:
        self._level = log_level
        self._include_args = include_args

    async def __call__(self, ctx: RequestContext, call_next: Callable[..., Any]) -> Any:
        entry: dict[str, Any] = {
            "event": "tool_call_start",
            "tool": ctx.tool_name,
            "request_id": ctx.request_id,
        }
        if ctx.client_id:
            entry["client_id"] = ctx.client_id
        if self._include_args:
            from ...observability import redact_sensitive
            from ._audit import _json_default

            arguments = ctx.state.get("_tool_arguments")
            # JSON data first (nested models as objects), so redaction
            # reaches the strings inside them.
            plain = json.loads(json.dumps(arguments, default=_json_default))
            entry["args"] = redact_sensitive(plain if isinstance(plain, dict) else {})

        logger.log(self._level, json.dumps(entry, default=str))

        start = time.perf_counter()
        try:
            result = await call_next(ctx)
            elapsed = time.perf_counter() - start
            end_entry: dict[str, Any] = {
                "event": "tool_call_end",
                "tool": ctx.tool_name,
                "request_id": ctx.request_id,
                "duration_ms": round(elapsed * 1000, 2),
                "status": "ok",
            }
            if ctx.client_id:
                end_entry["client_id"] = ctx.client_id
            logger.log(self._level, json.dumps(end_entry, default=str))
            return result
        except Exception as exc:
            elapsed = time.perf_counter() - start
            end_entry = {
                "event": "tool_call_end",
                "tool": ctx.tool_name,
                "request_id": ctx.request_id,
                "duration_ms": round(elapsed * 1000, 2),
                "status": "error",
                "error": str(exc),
            }
            if ctx.client_id:
                end_entry["client_id"] = ctx.client_id
            logger.log(self._level, json.dumps(end_entry, default=str))
            raise
