"""``@server.tool(version=...)`` and ``server.add_transform(...)``."""

from __future__ import annotations

import json
from typing import Any

import pytest
from _mcp_memory import connected_session

from promptise.mcp.server import (
    APIKeyAuth,
    AuthMiddleware,
    MCPRouter,
    MCPServer,
    NamespaceTransform,
    TagFilterTransform,
    TestClient,
    ToolDef,
    VisibilityTransform,
)


def _text(result: list[Any]) -> str:
    return result[0].text


# ---------------------------------------------------------------------------
# Versions
# ---------------------------------------------------------------------------


def _versioned_server() -> MCPServer:
    server = MCPServer("api")

    @server.tool(version="1.0")
    async def search(query: str) -> str:
        """Search (v1)."""
        return f"v1:{query}"

    @server.tool(version="2.0")
    async def search(query: str, limit: int = 5) -> str:  # noqa: F811
        """Search (v2)."""
        return f"v2:{query}:{limit}"

    return server


async def test_versions_are_listed_under_pinned_names_and_the_bare_name() -> None:
    tools = {t.name: t for t in await TestClient(_versioned_server()).list_tools()}
    assert set(tools) == {"search", "search_v1_0", "search_v2_0"}
    assert tools["search"].description == "Search (v2)."
    assert tools["search_v1_0"].description == "Search (v1)."


async def test_bare_name_runs_the_latest_and_pinned_names_their_version() -> None:
    client = TestClient(_versioned_server())
    assert _text(await client.call_tool("search", {"query": "q", "limit": 2})) == "v2:q:2"
    assert _text(await client.call_tool("search_v2_0", {"query": "q"})) == "v2:q:5"
    assert _text(await client.call_tool("search_v1_0", {"query": "q"})) == "v1:q"


async def test_latest_is_by_number_not_registration_order() -> None:
    server = MCPServer("api")

    @server.tool(version="1.10")
    async def lookup() -> str:
        return "1.10"

    @server.tool(version="1.9")
    async def lookup() -> str:  # noqa: F811
        return "1.9"

    assert _text(await TestClient(server).call_tool("lookup", {})) == "1.10"


async def test_each_version_validates_its_own_arguments() -> None:
    client = TestClient(_versioned_server())
    bad = json.loads(_text(await client.call_tool("search_v2_0", {"query": "q", "limit": "x"})))
    assert bad["error"]["code"] == "VALIDATION_ERROR"


def test_duplicate_version_and_mixing_with_unversioned_raise() -> None:
    server = _versioned_server()
    with pytest.raises(ValueError, match="version '1.0' is already registered"):

        @server.tool(name="search", version="1.0")
        async def again(query: str) -> str:
            return ""

    with pytest.raises(ValueError, match="already registered"):

        @server.tool(name="search")
        async def plain(query: str) -> str:
            return ""

    other = MCPServer("api")

    @other.tool()
    async def fetch() -> str:
        return ""

    with pytest.raises(ValueError, match="without a version"):

        @other.tool(name="fetch", version="2.0")
        async def fetch_v2() -> str:
            return ""


def test_version_without_letters_or_digits_raises() -> None:
    server = MCPServer("api")
    with pytest.raises(ValueError, match="letter or digit"):

        @server.tool(version="..")
        async def tool() -> str:
            return ""


async def test_router_versions_get_the_prefix() -> None:
    router = MCPRouter(prefix="billing")

    @router.tool(version="1.0")
    async def charge(amount: int) -> str:
        return f"v1:{amount}"

    @router.tool(version="2.0")
    async def charge(amount: int) -> str:  # noqa: F811
        return f"v2:{amount}"

    server = MCPServer("api")
    server.include_router(router)
    client = TestClient(server)
    names = {t.name for t in await client.list_tools()}
    assert names == {"billing_charge", "billing_charge_v1_0", "billing_charge_v2_0"}
    assert _text(await client.call_tool("billing_charge", {"amount": 3})) == "v2:3"
    assert server._tool_versions.list_versions("billing_charge") == ["1.0", "2.0"]


# ---------------------------------------------------------------------------
# Transforms
# ---------------------------------------------------------------------------


def _tagged_server() -> MCPServer:
    server = MCPServer("api")

    @server.tool(tags=["public"])
    async def search(query: str) -> str:
        return f"found {query}"

    @server.tool(tags=["admin"])
    async def reset() -> str:
        return "reset"

    return server


async def test_namespace_transform_renames_listed_tools_and_routes_calls() -> None:
    server = _tagged_server()
    server.add_transform(NamespaceTransform(prefix="app"))
    client = TestClient(server)
    assert {t.name for t in await client.list_tools()} == {"app_search", "app_reset"}
    assert _text(await client.call_tool("app_search", {"query": "x"})) == "found x"
    # The registered name keeps working
    assert _text(await client.call_tool("search", {"query": "x"})) == "found x"


async def test_tag_filter_transform_hides_but_does_not_block() -> None:
    server = _tagged_server()
    server.add_transform(TagFilterTransform(required_tags={"public"}))
    client = TestClient(server)
    assert [t.name for t in await client.list_tools()] == ["search"]
    assert _text(await client.call_tool("reset", {})) == "reset"


async def test_transforms_run_in_order() -> None:
    server = _tagged_server()
    server.add_transform(NamespaceTransform(prefix="app"))
    server.add_transform(VisibilityTransform(hidden={"app_reset": lambda ctx: True}))
    assert [t.name for t in await TestClient(server).list_tools()] == ["app_search"]


async def test_visibility_transform_sees_the_authenticated_caller() -> None:
    server = _tagged_server()
    server.add_middleware(AuthMiddleware(APIKeyAuth({"k-admin": "alice", "k-user": "bob"})))

    def not_alice(ctx: Any) -> bool:
        return ctx.client_id != "alice"

    server.add_transform(VisibilityTransform(hidden={"reset": not_alice}))
    as_admin = TestClient(server, meta={"x-api-key": "k-admin"})
    as_user = TestClient(server, meta={"x-api-key": "k-user"})
    anonymous = TestClient(server)
    assert {t.name for t in await as_admin.list_tools()} == {"search", "reset"}
    assert {t.name for t in await as_user.list_tools()} == {"search"}
    assert {t.name for t in await anonymous.list_tools()} == {"search"}


async def test_raising_predicate_hides_the_tool() -> None:
    server = _tagged_server()
    server.add_transform(VisibilityTransform(hidden={"reset": lambda ctx: 1 / 0}))
    assert [t.name for t in await TestClient(server).list_tools()] == ["search"]


async def test_custom_async_transform_rewrites_descriptions() -> None:
    from dataclasses import replace

    class Describe:
        async def apply(self, tools: list[ToolDef], ctx: Any = None) -> list[ToolDef]:
            return [replace(t, description=f"[{ctx.server_name}] {t.description}") for t in tools]

    server = _tagged_server()
    server.add_transform(Describe())
    descriptions = {t.description for t in await TestClient(server).list_tools()}
    assert all(d.startswith("[api] ") for d in descriptions)


def test_add_transform_rejects_non_transforms() -> None:
    with pytest.raises(TypeError, match="no apply"):
        MCPServer("api").add_transform(object())


async def test_manifest_lists_transformed_tools() -> None:
    server = _tagged_server()
    server.add_transform(NamespaceTransform(prefix="app"))
    server.add_transform(TagFilterTransform(required_tags={"public"}))
    manifest = json.loads(await TestClient(server).read_resource("docs://manifest"))
    assert [t["name"] for t in manifest["tools"]] == ["app_search"]


async def test_live_server_lists_and_calls_transformed_tools() -> None:
    """The real MCP handlers (not only TestClient) apply transforms."""
    server = _tagged_server()
    server.add_transform(NamespaceTransform(prefix="app"))

    @server.tool(version="1.0")
    async def lookup() -> str:
        return "one"

    async with connected_session(server._build_lowlevel_server()) as s:
        listed = await s.list_tools()
        assert {t.name for t in listed.tools} == {
            "app_search",
            "app_reset",
            "app_lookup",
            "app_lookup_v1_0",
        }
        result = await s.call_tool("app_lookup_v1_0", {})
        assert result.content[0].text == "one"
