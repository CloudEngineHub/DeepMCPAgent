"""Tests for promptise.server health checks."""

from __future__ import annotations

import json

from promptise.mcp.server import MCPServer
from promptise.mcp.server._health import HealthCheck


class TestHealthCheck:
    async def test_liveness_returns_alive(self):
        health = HealthCheck()
        result = json.loads(await health.liveness())
        assert result["status"] == "alive"
        assert "uptime_seconds" in result

    async def test_readiness_no_checks(self):
        health = HealthCheck()
        result = json.loads(await health.readiness())
        assert result["status"] == "ready"
        assert result["checks"] == {}

    async def test_readiness_all_healthy(self):
        health = HealthCheck()
        health.add_check("db", lambda: True)
        health.add_check("cache", lambda: True)
        result = json.loads(await health.readiness())
        assert result["status"] == "ready"
        assert result["checks"]["db"]["healthy"] is True
        assert result["checks"]["cache"]["healthy"] is True

    async def test_readiness_required_check_fails(self):
        health = HealthCheck()
        health.add_check("db", lambda: False, required_for_ready=True)
        result = json.loads(await health.readiness())
        assert result["status"] == "not_ready"
        assert result["checks"]["db"]["healthy"] is False

    async def test_readiness_optional_check_fails(self):
        health = HealthCheck()
        health.add_check("cache", lambda: False, required_for_ready=False)
        result = json.loads(await health.readiness())
        # Optional failure doesn't affect readiness
        assert result["status"] == "ready"

    async def test_readiness_check_exception(self):
        health = HealthCheck()

        def bad_check():
            raise ConnectionError("connection refused")

        health.add_check("db", bad_check, required_for_ready=True)
        result = json.loads(await health.readiness())
        assert result["status"] == "not_ready"
        assert result["checks"]["db"]["healthy"] is False
        assert "connection refused" in result["checks"]["db"]["error"]

    async def test_readiness_async_check(self):
        health = HealthCheck()

        async def async_check():
            return True

        health.add_check("async_db", async_check)
        result = json.loads(await health.readiness())
        assert result["status"] == "ready"
        assert result["checks"]["async_db"]["healthy"] is True

    def test_register_resources(self):
        server = MCPServer(name="test")
        health = HealthCheck()
        health.register_resources(server)

        # Should have registered 2 resources
        liveness = server._resource_registry.get("health://liveness")
        readiness = server._resource_registry.get("health://readiness")
        assert liveness is not None
        assert readiness is not None
        assert liveness.mime_type == "application/json"

    async def test_registered_liveness_resource(self):
        server = MCPServer(name="test")
        health = HealthCheck()
        health.register_resources(server)

        rdef = server._resource_registry.get("health://liveness")
        result = json.loads(await rdef.handler())
        assert result["status"] == "alive"
