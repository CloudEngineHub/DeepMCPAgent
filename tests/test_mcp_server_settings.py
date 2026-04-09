"""Tests for promptise.mcp.server settings."""

from __future__ import annotations

import json

from promptise.mcp.server import Depends, MCPServer, ServerSettings, TestClient

# =====================================================================
# ServerSettings
# =====================================================================


class TestServerSettings:
    def test_defaults(self):
        settings = ServerSettings()
        assert settings.server_name == "promptise-server"
        assert settings.log_level == "INFO"
        assert settings.timeout_default == 30.0

    def test_env_override(self, monkeypatch):
        monkeypatch.setenv("PROMPTISE_SERVER_NAME", "my-server")
        monkeypatch.setenv("PROMPTISE_LOG_LEVEL", "DEBUG")
        monkeypatch.setenv("PROMPTISE_TIMEOUT_DEFAULT", "60.0")

        settings = ServerSettings()
        assert settings.server_name == "my-server"
        assert settings.log_level == "DEBUG"
        assert settings.timeout_default == 60.0

    def test_extra_env_ignored(self, monkeypatch):
        monkeypatch.setenv("PROMPTISE_UNKNOWN_FIELD", "value")
        settings = ServerSettings()  # Should not raise
        assert not hasattr(settings, "unknown_field")


# =====================================================================
# Custom subclass
# =====================================================================


class MyAppSettings(ServerSettings):
    database_url: str = "sqlite:///local.db"
    api_key: str = ""
    max_results: int = 100

    model_config = {"env_prefix": "MY_APP_", "extra": "ignore"}


class TestCustomSettings:
    def test_subclass_defaults(self):
        settings = MyAppSettings()
        assert settings.database_url == "sqlite:///local.db"
        assert settings.api_key == ""
        assert settings.max_results == 100

    def test_subclass_env_override(self, monkeypatch):
        monkeypatch.setenv("MY_APP_DATABASE_URL", "postgres://localhost/db")
        monkeypatch.setenv("MY_APP_API_KEY", "sk-test-123")
        monkeypatch.setenv("MY_APP_MAX_RESULTS", "50")

        settings = MyAppSettings()
        assert settings.database_url == "postgres://localhost/db"
        assert settings.api_key == "sk-test-123"
        assert settings.max_results == 50


# =====================================================================
# Integration with Depends
# =====================================================================


class TestSettingsWithDepends:
    async def test_inject_settings(self):
        server = MCPServer(name="test")

        @server.tool()
        async def show_config(
            settings: ServerSettings = Depends(ServerSettings),
        ) -> dict:
            return {
                "server_name": settings.server_name,
                "log_level": settings.log_level,
            }

        client = TestClient(server)
        result = await client.call_tool("show_config", {})
        parsed = json.loads(result[0].text)
        assert parsed["server_name"] == "promptise-server"
        assert parsed["log_level"] == "INFO"

    async def test_inject_custom_settings(self, monkeypatch):
        monkeypatch.setenv("MY_APP_DATABASE_URL", "postgres://test/db")

        server = MCPServer(name="test")

        @server.tool()
        async def db_info(
            settings: MyAppSettings = Depends(MyAppSettings),
        ) -> dict:
            return {"db": settings.database_url}

        client = TestClient(server)
        result = await client.call_tool("db_info", {})
        parsed = json.loads(result[0].text)
        assert parsed["db"] == "postgres://test/db"
