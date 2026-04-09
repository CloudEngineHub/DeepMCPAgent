"""Tests for promptise.server observability (metrics)."""

from __future__ import annotations

import json

import pytest

from promptise.mcp.server import MCPServer
from promptise.mcp.server._context import RequestContext
from promptise.mcp.server._observability import MetricsCollector, MetricsMiddleware

# =====================================================================
# MetricsCollector
# =====================================================================


class TestMetricsCollector:
    def test_record_call(self):
        collector = MetricsCollector()
        collector.record_call("search", 0.1)
        collector.record_call("search", 0.2)
        collector.record_call("query", 0.05, error=True)

        snap = collector.snapshot()
        assert snap["tools"]["search"]["calls"] == 2
        assert snap["tools"]["search"]["errors"] == 0
        assert snap["tools"]["search"]["avg_latency_ms"] == pytest.approx(150.0, abs=1)
        assert snap["tools"]["query"]["calls"] == 1
        assert snap["tools"]["query"]["errors"] == 1

    def test_snapshot_empty(self):
        collector = MetricsCollector()
        snap = collector.snapshot()
        assert snap["tools"] == {}
        assert "uptime_seconds" in snap

    def test_register_resource(self):
        server = MCPServer(name="test")
        collector = MetricsCollector()
        collector.register_resource(server)

        rdef = server._resource_registry.get("metrics://server")
        assert rdef is not None
        assert rdef.mime_type == "application/json"

    async def test_metrics_resource_returns_snapshot(self):
        server = MCPServer(name="test")
        collector = MetricsCollector()
        collector.record_call("add", 0.01)
        collector.register_resource(server)

        rdef = server._resource_registry.get("metrics://server")
        result = json.loads(await rdef.handler())
        assert result["tools"]["add"]["calls"] == 1


# =====================================================================
# MetricsMiddleware
# =====================================================================


class TestMetricsMiddleware:
    async def test_records_successful_call(self):
        collector = MetricsCollector()
        mw = MetricsMiddleware(collector)

        async def call_next(ctx):
            return "ok"

        ctx = RequestContext(server_name="test", tool_name="search")
        result = await mw(ctx, call_next)

        assert result == "ok"
        snap = collector.snapshot()
        assert snap["tools"]["search"]["calls"] == 1
        assert snap["tools"]["search"]["errors"] == 0

    async def test_records_failed_call(self):
        collector = MetricsCollector()
        mw = MetricsMiddleware(collector)

        async def call_next(ctx):
            raise RuntimeError("boom")

        ctx = RequestContext(server_name="test", tool_name="search")
        with pytest.raises(RuntimeError):
            await mw(ctx, call_next)

        snap = collector.snapshot()
        assert snap["tools"]["search"]["calls"] == 1
        assert snap["tools"]["search"]["errors"] == 1

    async def test_creates_default_collector(self):
        mw = MetricsMiddleware()
        assert mw.collector is not None

        async def call_next(ctx):
            return "ok"

        ctx = RequestContext(server_name="test", tool_name="add")
        await mw(ctx, call_next)
        assert mw.collector.snapshot()["tools"]["add"]["calls"] == 1
