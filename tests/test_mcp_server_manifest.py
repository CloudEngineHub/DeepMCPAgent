"""Tests for promptise.mcp.server manifest."""

from __future__ import annotations

import json

import pytest

from promptise.mcp.server import (
    MCPRouter,
    MCPServer,
    TestClient,
    build_manifest,
    register_manifest,
)

# =====================================================================
# build_manifest
# =====================================================================


class TestBuildManifest:
    def test_server_info(self):
        server = MCPServer(name="test-api", version="2.0.0", auto_manifest=False)
        manifest = build_manifest(server)
        assert manifest["server"]["name"] == "test-api"
        assert manifest["server"]["version"] == "2.0.0"

    def test_empty_server(self):
        server = MCPServer(name="test", auto_manifest=False)
        manifest = build_manifest(server)
        assert manifest["tools"] == []
        assert manifest["resources"] == []
        assert manifest["resource_templates"] == []
        assert manifest["prompts"] == []

    def test_tools_in_manifest(self):
        server = MCPServer(name="test", auto_manifest=False)

        @server.tool(tags=["math"])
        async def add(a: int, b: int) -> int:
            """Add two numbers."""
            return a + b

        manifest = build_manifest(server)
        assert len(manifest["tools"]) == 1
        tool = manifest["tools"][0]
        assert tool["name"] == "add"
        assert tool["description"] == "Add two numbers."
        assert tool["tags"] == ["math"]
        assert "input_schema" in tool

    def test_auth_in_manifest(self):
        server = MCPServer(name="test", auto_manifest=False)

        @server.tool(auth=True, roles=["admin"])
        async def protected(x: int) -> int:
            return x

        manifest = build_manifest(server)
        tool = manifest["tools"][0]
        assert tool["auth_required"] is True
        assert tool["roles"] == ["admin"]
        assert "HasRole" in tool["guards"]

    def test_resources_in_manifest(self):
        server = MCPServer(name="test", auto_manifest=False)

        @server.resource("config://app", mime_type="application/json")
        async def config() -> str:
            return "{}"

        manifest = build_manifest(server)
        assert len(manifest["resources"]) == 1
        res = manifest["resources"][0]
        assert res["uri"] == "config://app"
        assert res["mime_type"] == "application/json"

    def test_resource_templates_in_manifest(self):
        server = MCPServer(name="test", auto_manifest=False)

        @server.resource_template("users://{user_id}")
        async def get_user(user_id: str) -> str:
            return user_id

        manifest = build_manifest(server)
        assert len(manifest["resource_templates"]) == 1
        tmpl = manifest["resource_templates"][0]
        assert tmpl["uri_template"] == "users://{user_id}"

    def test_prompts_in_manifest(self):
        server = MCPServer(name="test", auto_manifest=False)

        @server.prompt()
        async def summarize(text: str) -> str:
            return text

        manifest = build_manifest(server)
        assert len(manifest["prompts"]) == 1
        prompt = manifest["prompts"][0]
        assert prompt["name"] == "summarize"

    def test_router_tools_in_manifest(self):
        server = MCPServer(name="test", auto_manifest=False)
        router = MCPRouter(prefix="db", tags=["database"])

        @router.tool()
        async def query(sql: str) -> list:
            return []

        server.include_router(router)
        manifest = build_manifest(server)
        assert len(manifest["tools"]) == 1
        assert manifest["tools"][0]["name"] == "db_query"
        assert "database" in manifest["tools"][0]["tags"]


# =====================================================================
# register_manifest
# =====================================================================


class TestRegisterManifest:
    def test_registers_resource(self):
        server = MCPServer(name="test", auto_manifest=False)

        @server.tool()
        async def add(a: int, b: int) -> int:
            return a + b

        register_manifest(server)
        rdef = server._resource_registry.get("docs://manifest")
        assert rdef is not None
        assert rdef.mime_type == "application/json"

    async def test_manifest_resource_returns_json(self):
        server = MCPServer(name="test-api", version="1.0.0", auto_manifest=False)

        @server.tool(tags=["math"])
        async def add(a: int, b: int) -> int:
            return a + b

        register_manifest(server)
        rdef = server._resource_registry.get("docs://manifest")
        result = json.loads(await rdef.handler())
        assert result["server"]["name"] == "test-api"
        assert len(result["tools"]) == 1
        assert result["tools"][0]["name"] == "add"

    def test_double_register_raises(self):
        server = MCPServer(name="test", auto_manifest=False)
        register_manifest(server)
        with pytest.raises(ValueError, match="already registered"):
            register_manifest(server)


# =====================================================================
# Integration via TestClient
# =====================================================================


class TestManifestViaTestClient:
    async def test_read_manifest_resource(self):
        server = MCPServer(name="my-api", version="1.0.0", auto_manifest=False)

        @server.tool(tags=["math"])
        async def add(a: int, b: int) -> int:
            """Add two numbers."""
            return a + b

        @server.resource("config://app")
        async def config() -> str:
            return "{}"

        register_manifest(server)
        client = TestClient(server)

        result = await client.read_resource("docs://manifest")
        manifest = json.loads(result)
        assert manifest["server"]["name"] == "my-api"
        assert len(manifest["tools"]) == 1
        assert manifest["tools"][0]["name"] == "add"
        # 2 resources: config://app + docs://manifest (itself)
        assert len(manifest["resources"]) == 2

    async def test_manifest_includes_itself(self):
        """When auto_manifest is True, the manifest resource should list itself."""
        server = MCPServer(name="test", auto_manifest=False)
        register_manifest(server)
        client = TestClient(server)
        resources = await client.list_resources()
        uris = {str(r.uri) for r in resources}
        assert "docs://manifest" in uris
