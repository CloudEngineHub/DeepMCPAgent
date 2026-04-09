"""Structured logging and metrics for MCP servers.

Provides a ``MetricsCollector`` that tracks per-tool call counts,
latency, and error rates, exposed via a metrics middleware and
resource.

Example::

    from promptise.mcp.server import MCPServer, MetricsMiddleware, MetricsCollector

    metrics = MetricsCollector()
    server = MCPServer(name="api")
    server.add_middleware(MetricsMiddleware(metrics))
    metrics.register_resource(server)
"""

from __future__ import annotations

import json
import time
from collections import defaultdict
from collections.abc import Callable
from typing import Any

from ._context import RequestContext


class MetricsCollector:
    """Collects per-tool metrics: call counts, latency, errors."""

    def __init__(self) -> None:
        self._calls: dict[str, int] = defaultdict(int)
        self._errors: dict[str, int] = defaultdict(int)
        self._total_latency: dict[str, float] = defaultdict(float)
        self._started_at: float = time.time()

    def record_call(self, tool_name: str, latency: float, *, error: bool = False) -> None:
        """Record a tool call."""
        self._calls[tool_name] += 1
        self._total_latency[tool_name] += latency
        if error:
            self._errors[tool_name] += 1

    def snapshot(self) -> dict[str, Any]:
        """Return a snapshot of all metrics."""
        tools: dict[str, Any] = {}
        for name in sorted(self._calls):
            calls = self._calls[name]
            avg_latency = self._total_latency[name] / calls if calls else 0.0
            tools[name] = {
                "calls": calls,
                "errors": self._errors.get(name, 0),
                "avg_latency_ms": round(avg_latency * 1000, 2),
            }
        return {
            "uptime_seconds": round(time.time() - self._started_at, 1),
            "tools": tools,
        }

    def register_resource(self, server: Any) -> None:
        """Register a ``metrics://server`` resource on *server*."""
        collector = self

        @server.resource(
            "metrics://server",
            name="metrics",
            description="Server metrics (call counts, latency, errors)",
            mime_type="application/json",
        )
        async def _metrics() -> str:
            return json.dumps(collector.snapshot(), indent=2)


class MetricsMiddleware:
    """Middleware that records per-tool call metrics.

    Args:
        collector: A ``MetricsCollector`` instance. Created automatically
            if not provided.
    """

    def __init__(self, collector: MetricsCollector | None = None) -> None:
        self.collector = collector or MetricsCollector()

    async def __call__(self, ctx: RequestContext, call_next: Callable[..., Any]) -> Any:
        start = time.perf_counter()
        error = False
        try:
            result = await call_next(ctx)
            return result
        except Exception:
            error = True
            raise
        finally:
            elapsed = time.perf_counter() - start
            self.collector.record_call(ctx.tool_name, elapsed, error=error)
