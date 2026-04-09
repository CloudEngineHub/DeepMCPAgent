"""Tests for promptise.mcp.server guards and role-based access control."""

from __future__ import annotations

import json

import pytest

from promptise.mcp.server import (
    AuthMiddleware,
    HasAllRoles,
    HasRole,
    JWTAuth,
    MCPRouter,
    MCPServer,
    RequireAuth,
    RequireClientId,
    TestClient,
)
from promptise.mcp.server._context import RequestContext
from promptise.mcp.server._errors import AuthenticationError
from promptise.mcp.server._guards import Guard
from promptise.mcp.server._testing import check_guards

# =====================================================================
# Guard protocol
# =====================================================================


class TestGuardProtocol:
    def test_has_role_is_guard(self):
        assert isinstance(HasRole("admin"), Guard)

    def test_has_all_roles_is_guard(self):
        assert isinstance(HasAllRoles("admin", "editor"), Guard)

    def test_require_auth_is_guard(self):
        assert isinstance(RequireAuth(), Guard)

    def test_require_client_id_is_guard(self):
        assert isinstance(RequireClientId("abc"), Guard)


# =====================================================================
# RequireAuth
# =====================================================================


class TestRequireAuth:
    async def test_allows_authenticated(self):
        guard = RequireAuth()
        ctx = RequestContext(server_name="test", tool_name="t")
        ctx.client_id = "user-42"
        assert await guard.check(ctx) is True

    async def test_denies_unauthenticated(self):
        guard = RequireAuth()
        ctx = RequestContext(server_name="test", tool_name="t")
        assert await guard.check(ctx) is False


# =====================================================================
# HasRole
# =====================================================================


class TestHasRole:
    async def test_allows_matching_role(self):
        guard = HasRole("admin")
        ctx = RequestContext(server_name="test", tool_name="t")
        ctx.state["roles"] = {"admin", "user"}
        assert await guard.check(ctx) is True

    async def test_allows_any_matching_role(self):
        guard = HasRole("admin", "editor")
        ctx = RequestContext(server_name="test", tool_name="t")
        ctx.state["roles"] = {"editor"}
        assert await guard.check(ctx) is True

    async def test_denies_no_matching_role(self):
        guard = HasRole("admin")
        ctx = RequestContext(server_name="test", tool_name="t")
        ctx.state["roles"] = {"user"}
        assert await guard.check(ctx) is False

    async def test_denies_empty_roles(self):
        guard = HasRole("admin")
        ctx = RequestContext(server_name="test", tool_name="t")
        assert await guard.check(ctx) is False

    async def test_multiple_required_any_matches(self):
        guard = HasRole("admin", "super_admin")
        ctx = RequestContext(server_name="test", tool_name="t")
        ctx.state["roles"] = {"super_admin"}
        assert await guard.check(ctx) is True


# =====================================================================
# HasAllRoles
# =====================================================================


class TestHasAllRoles:
    async def test_allows_all_roles_present(self):
        guard = HasAllRoles("admin", "editor")
        ctx = RequestContext(server_name="test", tool_name="t")
        ctx.state["roles"] = {"admin", "editor", "user"}
        assert await guard.check(ctx) is True

    async def test_denies_partial_roles(self):
        guard = HasAllRoles("admin", "editor")
        ctx = RequestContext(server_name="test", tool_name="t")
        ctx.state["roles"] = {"admin"}
        assert await guard.check(ctx) is False

    async def test_denies_empty_roles(self):
        guard = HasAllRoles("admin")
        ctx = RequestContext(server_name="test", tool_name="t")
        assert await guard.check(ctx) is False


# =====================================================================
# RequireClientId
# =====================================================================


class TestRequireClientId:
    async def test_allows_matching_client(self):
        guard = RequireClientId("client-a", "client-b")
        ctx = RequestContext(server_name="test", tool_name="t")
        ctx.client_id = "client-a"
        assert await guard.check(ctx) is True

    async def test_denies_wrong_client(self):
        guard = RequireClientId("client-a")
        ctx = RequestContext(server_name="test", tool_name="t")
        ctx.client_id = "client-x"
        assert await guard.check(ctx) is False

    async def test_denies_no_client(self):
        guard = RequireClientId("client-a")
        ctx = RequestContext(server_name="test", tool_name="t")
        assert await guard.check(ctx) is False


# =====================================================================
# check_guards helper
# =====================================================================


class TestCheckGuards:
    async def test_no_guards_passes(self):
        ctx = RequestContext(server_name="test", tool_name="t")
        await check_guards([], ctx)  # Should not raise

    async def test_single_guard_passes(self):
        ctx = RequestContext(server_name="test", tool_name="t")
        ctx.client_id = "user"
        await check_guards([RequireAuth()], ctx)

    async def test_single_guard_fails(self):
        ctx = RequestContext(server_name="test", tool_name="t")
        with pytest.raises(AuthenticationError) as exc_info:
            await check_guards([RequireAuth()], ctx)
        assert exc_info.value.code == "ACCESS_DENIED"

    async def test_multiple_guards_all_pass(self):
        ctx = RequestContext(server_name="test", tool_name="t")
        ctx.client_id = "user-1"
        ctx.state["roles"] = {"admin"}
        await check_guards([RequireAuth(), HasRole("admin")], ctx)

    async def test_first_guard_fails_short_circuits(self):
        ctx = RequestContext(server_name="test", tool_name="t")
        # RequireAuth fails (no client_id), HasRole never checked
        with pytest.raises(AuthenticationError):
            await check_guards([RequireAuth(), HasRole("admin")], ctx)


# =====================================================================
# JWT role extraction
# =====================================================================


class TestJWTRoleExtraction:
    async def test_jwt_stores_payload(self):
        jwt_auth = JWTAuth(secret="test-secret")
        token = jwt_auth.create_token({"sub": "user-42", "roles": ["admin", "editor"]})

        ctx = RequestContext(server_name="test", tool_name="t")
        ctx.meta["authorization"] = f"Bearer {token}"

        client_id = await jwt_auth.authenticate(ctx)
        assert client_id == "user-42"
        assert "_jwt_payload" in ctx.state
        assert ctx.state["_jwt_payload"]["roles"] == ["admin", "editor"]

    async def test_auth_middleware_extracts_roles(self):
        jwt_auth = JWTAuth(secret="test-secret")
        token = jwt_auth.create_token({"sub": "user-42", "roles": ["admin", "editor"]})

        server = MCPServer(name="test")
        server.add_middleware(AuthMiddleware(jwt_auth))

        @server.tool(auth=True)
        async def get_roles(ctx: RequestContext) -> list:
            return sorted(ctx.state.get("roles", set()))

        client = TestClient(server, meta={"authorization": f"Bearer {token}"})
        result = await client.call_tool("get_roles", {})
        parsed = json.loads(result[0].text)
        assert "admin" in parsed
        assert "editor" in parsed

    async def test_auth_middleware_empty_roles_if_no_roles_in_jwt(self):
        jwt_auth = JWTAuth(secret="test-secret")
        token = jwt_auth.create_token({"sub": "user-42"})

        server = MCPServer(name="test")
        server.add_middleware(AuthMiddleware(jwt_auth))

        @server.tool(auth=True)
        async def get_roles(ctx: RequestContext) -> list:
            return sorted(ctx.state.get("roles", set()))

        client = TestClient(server, meta={"authorization": f"Bearer {token}"})
        result = await client.call_tool("get_roles", {})
        parsed = json.loads(result[0].text)
        assert parsed == []


# =====================================================================
# Full integration: JWT → AuthMiddleware → Guards → Handler
# =====================================================================


class TestFullAuthGuardFlow:
    async def test_jwt_auth_guard_allows(self):
        jwt_auth = JWTAuth(secret="test-secret")
        token = jwt_auth.create_token({"sub": "admin-user", "roles": ["admin"]})

        server = MCPServer(name="test")
        server.add_middleware(AuthMiddleware(jwt_auth))

        @server.tool(auth=True, roles=["admin"])
        async def admin_action(x: int) -> int:
            return x * 10

        client = TestClient(server, meta={"authorization": f"Bearer {token}"})
        result = await client.call_tool("admin_action", {"x": 5})
        assert result[0].text == "50"

    async def test_jwt_auth_guard_denies_wrong_role(self):
        jwt_auth = JWTAuth(secret="test-secret")
        token = jwt_auth.create_token({"sub": "basic-user", "roles": ["user"]})

        server = MCPServer(name="test")
        server.add_middleware(AuthMiddleware(jwt_auth))

        @server.tool(auth=True, roles=["admin"])
        async def admin_action(x: int) -> int:
            return x * 10

        client = TestClient(server, meta={"authorization": f"Bearer {token}"})
        result = await client.call_tool("admin_action", {"x": 5})
        parsed = json.loads(result[0].text)
        assert parsed["error"]["code"] == "ACCESS_DENIED"

    async def test_jwt_auth_guard_denies_no_token(self):
        jwt_auth = JWTAuth(secret="test-secret")

        server = MCPServer(name="test")
        server.add_middleware(AuthMiddleware(jwt_auth))

        @server.tool(auth=True, roles=["admin"])
        async def admin_action(x: int) -> int:
            return x * 10

        client = TestClient(server)
        result = await client.call_tool("admin_action", {"x": 5})
        parsed = json.loads(result[0].text)
        assert parsed["error"]["code"] == "AUTHENTICATION_ERROR"

    async def test_router_level_guard_with_jwt(self):
        jwt_auth = JWTAuth(secret="test-secret")
        token = jwt_auth.create_token({"sub": "admin-user", "roles": ["admin"]})

        server = MCPServer(name="test")
        server.add_middleware(AuthMiddleware(jwt_auth))

        router = MCPRouter(prefix="admin")

        @router.tool(auth=True, roles=["admin"])
        async def delete_user(user_id: str) -> str:
            return f"Deleted {user_id}"

        server.include_router(router)

        client = TestClient(server, meta={"authorization": f"Bearer {token}"})
        result = await client.call_tool("admin_delete_user", {"user_id": "u-42"})
        assert "Deleted u-42" in result[0].text

    async def test_has_all_roles_guard_full_flow(self):
        jwt_auth = JWTAuth(secret="test-secret")
        token = jwt_auth.create_token({"sub": "super-admin", "roles": ["admin", "super_admin"]})

        server = MCPServer(name="test")
        server.add_middleware(AuthMiddleware(jwt_auth))

        @server.tool(auth=True, guards=[HasAllRoles("admin", "super_admin")])
        async def nuke(x: int) -> int:
            return x

        client = TestClient(server, meta={"authorization": f"Bearer {token}"})
        result = await client.call_tool("nuke", {"x": 99})
        assert result[0].text == "99"

    async def test_has_all_roles_guard_partial_denied(self):
        jwt_auth = JWTAuth(secret="test-secret")
        token = jwt_auth.create_token({"sub": "admin-user", "roles": ["admin"]})

        server = MCPServer(name="test")
        server.add_middleware(AuthMiddleware(jwt_auth))

        @server.tool(auth=True, guards=[HasAllRoles("admin", "super_admin")])
        async def nuke(x: int) -> int:
            return x

        client = TestClient(server, meta={"authorization": f"Bearer {token}"})
        result = await client.call_tool("nuke", {"x": 99})
        parsed = json.loads(result[0].text)
        assert parsed["error"]["code"] == "ACCESS_DENIED"

    async def test_unauthenticated_tool_no_guard_check(self):
        """Tools without auth=True skip auth and guards entirely."""
        server = MCPServer(name="test")

        @server.tool()
        async def public(x: int) -> int:
            return x

        client = TestClient(server)
        result = await client.call_tool("public", {"x": 42})
        assert result[0].text == "42"


# =====================================================================
# Roles shorthand
# =====================================================================


class TestRolesShorthand:
    def test_roles_creates_has_role_guard_on_server(self):
        server = MCPServer(name="test")

        @server.tool(roles=["admin", "editor"])
        async def edit(x: int) -> int:
            return x

        tdef = server._tool_registry.get("edit")
        assert len(tdef.guards) == 1
        assert isinstance(tdef.guards[0], HasRole)

    def test_roles_creates_has_role_guard_on_router(self):
        router = MCPRouter()

        @router.tool(roles=["admin"])
        async def delete(x: int) -> int:
            return x

        tdef = router._tool_registry.get("delete")
        assert len(tdef.guards) == 1
        assert isinstance(tdef.guards[0], HasRole)

    def test_roles_combined_with_explicit_guards(self):
        server = MCPServer(name="test")

        @server.tool(guards=[RequireAuth()], roles=["admin"])
        async def protected(x: int) -> int:
            return x

        tdef = server._tool_registry.get("protected")
        assert len(tdef.guards) == 2
        assert isinstance(tdef.guards[0], RequireAuth)
        assert isinstance(tdef.guards[1], HasRole)

    def test_roles_stored_in_tooldef(self):
        server = MCPServer(name="test")

        @server.tool(roles=["admin", "editor"])
        async def edit(x: int) -> int:
            return x

        tdef = server._tool_registry.get("edit")
        assert tdef.roles == ["admin", "editor"]
