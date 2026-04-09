"""Tests for promptise.server authentication providers and middleware."""

from __future__ import annotations

import pytest

from promptise.mcp.server._auth import APIKeyAuth, AuthMiddleware, JWTAuth
from promptise.mcp.server._context import RequestContext
from promptise.mcp.server._errors import AuthenticationError
from promptise.mcp.server._types import ToolDef

# =====================================================================
# JWTAuth
# =====================================================================


class TestJWTAuth:
    def setup_method(self):
        self.auth = JWTAuth(secret="test-secret-key")

    async def test_authenticate_valid_token(self):
        token = self.auth.create_token({"sub": "user-42"})
        ctx = RequestContext(
            server_name="test",
            meta={"authorization": f"Bearer {token}"},
        )
        client_id = await self.auth.authenticate(ctx)
        assert client_id == "user-42"

    async def test_authenticate_without_bearer_prefix(self):
        token = self.auth.create_token({"sub": "user-42"})
        ctx = RequestContext(
            server_name="test",
            meta={"authorization": token},
        )
        client_id = await self.auth.authenticate(ctx)
        assert client_id == "user-42"

    async def test_authenticate_missing_token(self):
        ctx = RequestContext(server_name="test", meta={})
        with pytest.raises(AuthenticationError, match="Missing"):
            await self.auth.authenticate(ctx)

    async def test_authenticate_malformed_token(self):
        ctx = RequestContext(
            server_name="test",
            meta={"authorization": "not.a.jwt.at.all"},
        )
        with pytest.raises(AuthenticationError):
            await self.auth.authenticate(ctx)

    async def test_authenticate_invalid_signature(self):
        other_auth = JWTAuth(secret="different-secret")
        token = other_auth.create_token({"sub": "user"})
        ctx = RequestContext(
            server_name="test",
            meta={"authorization": f"Bearer {token}"},
        )
        with pytest.raises(AuthenticationError, match="signature"):
            await self.auth.authenticate(ctx)

    async def test_authenticate_expired_token(self):
        token = self.auth.create_token({"sub": "user"}, expires_in=-10)
        ctx = RequestContext(
            server_name="test",
            meta={"authorization": f"Bearer {token}"},
        )
        with pytest.raises(AuthenticationError, match="expired"):
            await self.auth.authenticate(ctx)

    async def test_create_token_includes_expiry(self):
        token = self.auth.create_token({"sub": "user"}, expires_in=3600)
        # Token should be verifiable
        ctx = RequestContext(
            server_name="test",
            meta={"authorization": token},
        )
        client_id = await self.auth.authenticate(ctx)
        assert client_id == "user"

    async def test_custom_meta_key(self):
        auth = JWTAuth(secret="secret", meta_key="x-token")
        token = auth.create_token({"sub": "user"})
        ctx = RequestContext(
            server_name="test",
            meta={"x-token": token},
        )
        client_id = await auth.authenticate(ctx)
        assert client_id == "user"

    async def test_client_id_fallback(self):
        token = self.auth.create_token({"client_id": "svc-1"})
        ctx = RequestContext(
            server_name="test",
            meta={"authorization": token},
        )
        client_id = await self.auth.authenticate(ctx)
        assert client_id == "svc-1"


# =====================================================================
# APIKeyAuth
# =====================================================================


class TestAPIKeyAuth:
    def setup_method(self):
        self.auth = APIKeyAuth(
            keys={
                "key-abc-123": "client-a",
                "key-def-456": "client-b",
            }
        )

    async def test_authenticate_valid_key(self):
        ctx = RequestContext(
            server_name="test",
            meta={"x-api-key": "key-abc-123"},
        )
        client_id = await self.auth.authenticate(ctx)
        assert client_id == "client-a"

    async def test_authenticate_missing_key(self):
        ctx = RequestContext(server_name="test", meta={})
        with pytest.raises(AuthenticationError, match="Missing"):
            await self.auth.authenticate(ctx)

    async def test_authenticate_invalid_key(self):
        ctx = RequestContext(
            server_name="test",
            meta={"x-api-key": "wrong-key"},
        )
        with pytest.raises(AuthenticationError, match="Invalid"):
            await self.auth.authenticate(ctx)

    async def test_custom_meta_key(self):
        auth = APIKeyAuth(
            keys={"my-key": "client"},
            header="api-token",
        )
        ctx = RequestContext(
            server_name="test",
            meta={"api-token": "my-key"},
        )
        client_id = await auth.authenticate(ctx)
        assert client_id == "client"


# =====================================================================
# AuthMiddleware
# =====================================================================


class TestAuthMiddleware:
    async def test_skips_non_auth_tools(self):
        auth = APIKeyAuth(keys={"k": "c"})
        mw = AuthMiddleware(auth)

        tdef = ToolDef(
            name="public",
            description="",
            handler=lambda: None,
            input_schema={},
            auth=False,
        )
        ctx = RequestContext(server_name="test", tool_name="public")
        ctx.state["tool_def"] = tdef

        called = False

        async def call_next(ctx):
            nonlocal called
            called = True
            return "ok"

        result = await mw(ctx, call_next)
        assert result == "ok"
        assert called
        assert ctx.client_id is None

    async def test_authenticates_auth_tools(self):
        auth = APIKeyAuth(keys={"key-1": "client-1"})
        mw = AuthMiddleware(auth)

        tdef = ToolDef(
            name="private",
            description="",
            handler=lambda: None,
            input_schema={},
            auth=True,
        )
        ctx = RequestContext(server_name="test", tool_name="private")
        ctx.state["tool_def"] = tdef
        ctx.meta["x-api-key"] = "key-1"

        async def call_next(ctx):
            return "secret"

        result = await mw(ctx, call_next)
        assert result == "secret"
        assert ctx.client_id == "client-1"

    async def test_rejects_auth_tools_without_creds(self):
        auth = APIKeyAuth(keys={"k": "c"})
        mw = AuthMiddleware(auth)

        tdef = ToolDef(
            name="private",
            description="",
            handler=lambda: None,
            input_schema={},
            auth=True,
        )
        ctx = RequestContext(server_name="test", tool_name="private")
        ctx.state["tool_def"] = tdef

        async def call_next(ctx):
            return "secret"

        with pytest.raises(AuthenticationError):
            await mw(ctx, call_next)
