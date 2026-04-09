"""Tests for promptise.mcp.server MCPRouter."""

from __future__ import annotations

from promptise.mcp.server import HasRole, MCPRouter, MCPServer


class TestMCPRouterRegistration:
    def test_router_registers_tool(self):
        router = MCPRouter()

        @router.tool()
        async def search(query: str) -> list:
            return []

        assert router._tool_registry.get("search") is not None

    def test_router_registers_resource(self):
        router = MCPRouter()

        @router.resource("config://app")
        async def config() -> str:
            return "{}"

        assert router._resource_registry.get("config://app") is not None

    def test_router_registers_resource_template(self):
        router = MCPRouter()

        @router.resource_template("users://{user_id}")
        async def get_user(user_id: str) -> str:
            return user_id

        assert len(list(router._resource_registry.list_templates())) == 1

    def test_router_registers_prompt(self):
        router = MCPRouter()

        @router.prompt()
        async def summarize(text: str) -> str:
            return f"Summarize: {text}"

        assert router._prompt_registry.get("summarize") is not None


class TestIncludeRouter:
    def test_prefix_applied(self):
        server = MCPServer(name="test")
        router = MCPRouter(prefix="db")

        @router.tool()
        async def query(sql: str) -> list:
            return []

        server.include_router(router)
        assert server._tool_registry.get("db_query") is not None
        assert server._tool_registry.get("query") is None

    def test_no_prefix(self):
        server = MCPServer(name="test")
        router = MCPRouter()

        @router.tool()
        async def search(q: str) -> list:
            return []

        server.include_router(router)
        assert server._tool_registry.get("search") is not None

    def test_extra_prefix_on_include(self):
        server = MCPServer(name="test")
        router = MCPRouter(prefix="db")

        @router.tool()
        async def query(sql: str) -> list:
            return []

        server.include_router(router, prefix="v2")
        assert server._tool_registry.get("v2_db_query") is not None

    def test_tags_merged(self):
        server = MCPServer(name="test")
        router = MCPRouter(tags=["database"])

        @router.tool(tags=["read"])
        async def query(sql: str) -> list:
            return []

        server.include_router(router)
        tdef = server._tool_registry.get("query")
        assert "database" in tdef.tags
        assert "read" in tdef.tags

    def test_extra_tags_on_include(self):
        server = MCPServer(name="test")
        router = MCPRouter(tags=["db"])

        @router.tool()
        async def query(sql: str) -> list:
            return []

        server.include_router(router, tags=["v2"])
        tdef = server._tool_registry.get("query")
        assert "v2" in tdef.tags
        assert "db" in tdef.tags

    def test_auth_override(self):
        server = MCPServer(name="test")
        router = MCPRouter(auth=True)

        @router.tool()  # auth=False by default
        async def public_tool(x: int) -> int:
            return x

        server.include_router(router)
        tdef = server._tool_registry.get("public_tool")
        assert tdef.auth is True

    def test_auth_none_preserves_tool_setting(self):
        server = MCPServer(name="test")
        router = MCPRouter()  # auth=None

        @router.tool(auth=True)
        async def private(x: int) -> int:
            return x

        @router.tool()
        async def public(x: int) -> int:
            return x

        server.include_router(router)
        assert server._tool_registry.get("private").auth is True
        assert server._tool_registry.get("public").auth is False

    def test_router_middleware_stored(self):
        async def router_mw(ctx, call_next):
            return await call_next(ctx)

        server = MCPServer(name="test")
        router = MCPRouter(middleware=[router_mw])

        @router.tool()
        async def query(sql: str) -> list:
            return []

        server.include_router(router)
        tdef = server._tool_registry.get("query")
        assert router_mw in tdef.router_middleware

    def test_router_guards_merged(self):
        from promptise.mcp.server._guards import RequireAuth

        guard = RequireAuth()
        server = MCPServer(name="test")
        router = MCPRouter(guards=[guard])

        @router.tool()
        async def query(sql: str) -> list:
            return []

        server.include_router(router)
        tdef = server._tool_registry.get("query")
        assert guard in tdef.guards

    def test_resources_merged(self):
        server = MCPServer(name="test")
        router = MCPRouter()

        @router.resource("config://db")
        async def db_config() -> str:
            return "{}"

        server.include_router(router)
        assert server._resource_registry.get("config://db") is not None

    def test_prompts_merged(self):
        server = MCPServer(name="test")
        router = MCPRouter()

        @router.prompt()
        async def summarize(text: str) -> str:
            return text

        server.include_router(router)
        assert server._prompt_registry.get("summarize") is not None

    def test_input_model_copied(self):
        server = MCPServer(name="test")
        router = MCPRouter(prefix="db")

        @router.tool()
        async def query(sql: str, limit: int = 10) -> list:
            return []

        server.include_router(router)
        assert "db_query" in server._input_models


class TestNestedRouters:
    def test_nested_routers(self):
        server = MCPServer(name="test")
        parent = MCPRouter(prefix="api")
        child = MCPRouter(prefix="v1")

        @child.tool()
        async def search(q: str) -> list:
            return []

        parent.include_router(child)
        server.include_router(parent)

        assert server._tool_registry.get("api_v1_search") is not None

    def test_nested_tags_accumulate(self):
        server = MCPServer(name="test")
        parent = MCPRouter(prefix="api", tags=["api"])
        child = MCPRouter(prefix="db", tags=["database"])

        @child.tool(tags=["read"])
        async def query(sql: str) -> list:
            return []

        parent.include_router(child)
        server.include_router(parent)

        tdef = server._tool_registry.get("api_db_query")
        assert "api" in tdef.tags
        assert "database" in tdef.tags
        assert "read" in tdef.tags

    def test_nested_guards_accumulate(self):
        from promptise.mcp.server._guards import HasRole, RequireAuth

        server = MCPServer(name="test")
        parent = MCPRouter(guards=[RequireAuth()])
        child = MCPRouter(guards=[HasRole("admin")])

        @child.tool()
        async def dangerous(x: int) -> int:
            return x

        parent.include_router(child)
        server.include_router(parent)

        tdef = server._tool_registry.get("dangerous")
        assert len(tdef.guards) == 2


class TestRouterRolesShorthand:
    def test_roles_creates_has_role_guard(self):
        router = MCPRouter()

        @router.tool(roles=["admin"])
        async def delete(x: int) -> int:
            return x

        tdef = router._tool_registry.get("delete")
        assert len(tdef.guards) == 1
        assert isinstance(tdef.guards[0], HasRole)
        assert tdef.roles == ["admin"]

    def test_server_roles_shorthand(self):
        server = MCPServer(name="test")

        @server.tool(roles=["editor", "admin"])
        async def edit(x: int) -> int:
            return x

        tdef = server._tool_registry.get("edit")
        assert len(tdef.guards) == 1
        assert isinstance(tdef.guards[0], HasRole)
