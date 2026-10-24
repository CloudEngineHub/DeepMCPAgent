"""Revoking API keys and JWTs while the server runs.

Before 1.7.1 ``APIKeyAuth`` copied its keys at construction, so a leaked key
stayed valid until a restart, and the JWT providers had no deny list: a
token stayed valid until its ``exp``.  A revocation now applies to the next
request.

Key and secret values are built at runtime so they don't look like real
credentials.
"""

from __future__ import annotations

import time
from types import SimpleNamespace

import jwt as pyjwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa

from promptise.mcp.server import AuthMiddleware, MCPServer, TestClient
from promptise.mcp.server._auth import APIKeyAuth, AsymmetricJWTAuth, JwksAuth, JWTAuth
from promptise.mcp.server._context import RequestContext
from promptise.mcp.server._errors import AuthenticationError
from promptise.mcp.server._transport import _AuthGateASGI

KEY_A = "key" + "-alpha-" + "1" * 8
KEY_B = "key" + "-bravo-" + "2" * 8
SECRET = "unit" + "-test-" + "s" * 24


def _key_ctx(key: str) -> RequestContext:
    return RequestContext(server_name="t", meta={"x-api-key": key})


def _bearer_ctx(token: str) -> RequestContext:
    return RequestContext(server_name="t", meta={"authorization": f"Bearer {token}"})


def _server(auth) -> MCPServer:
    server = MCPServer(name="revocable")
    server.add_middleware(AuthMiddleware(auth))

    @server.tool(auth=True)
    async def whoami(ctx: RequestContext) -> str:
        """The verified caller."""
        return str(ctx.client_id)

    return server


async def _gate_status(gate: _AuthGateASGI, headers: list[tuple[bytes, bytes]]) -> int:
    sent: list[dict] = []

    async def receive():  # pragma: no cover - never read
        return {"type": "http.request"}

    async def send(message):
        sent.append(message)

    await gate({"type": "http", "path": "/mcp", "headers": headers}, receive, send)
    starts = [m for m in sent if m.get("type") == "http.response.start"]
    return starts[0]["status"] if starts else 200


async def _ok_app(scope, receive, send):
    pass


# =====================================================================
# APIKeyAuth
# =====================================================================


class TestAPIKeyRevocation:
    async def test_revoke_key_applies_to_the_next_request(self):
        auth = APIKeyAuth(keys={KEY_A: "agent-1"})
        client = TestClient(_server(auth))

        ok = await client.call_tool("whoami", {}, headers={"x-api-key": KEY_A})
        assert ok[0].text == "agent-1"

        assert auth.revoke_key(KEY_A) is True
        denied = await client.call_tool("whoami", {}, headers={"x-api-key": KEY_A})
        assert "Invalid API key" in denied[0].text
        assert auth.revoke_key(KEY_A) is False  # already gone
        assert auth.verify_token(KEY_A) is False

    async def test_add_key_without_restart(self):
        auth = APIKeyAuth(keys={KEY_A: "agent-1"})
        auth.add_key(KEY_B, {"client_id": "agent-2", "roles": ["admin"], "tenant_id": "acme"})
        ctx = _key_ctx(KEY_B)
        assert await auth.authenticate(ctx) == "agent-2"
        assert ctx.state["roles"] == {"admin"}
        assert ctx.state["_api_key_tenant"] == "acme"

    async def test_sync_keys_callable_is_read_on_every_request(self):
        current = {KEY_A: "agent-1"}
        auth = APIKeyAuth(keys=lambda: current)
        assert await auth.authenticate(_key_ctx(KEY_A)) == "agent-1"
        current.pop(KEY_A)
        with pytest.raises(AuthenticationError, match="Invalid API key"):
            await auth.authenticate(_key_ctx(KEY_A))
        assert auth.verify_token(KEY_A) is False

    async def test_async_lookup(self):
        store = {KEY_A: {"client_id": "agent-1", "roles": ["read"]}}

        async def lookup(key: str):
            return store.get(key)

        auth = APIKeyAuth(lookup=lookup)
        ctx = _key_ctx(KEY_A)
        assert await auth.authenticate(ctx) == "agent-1"
        assert ctx.state["roles"] == {"read"}
        assert await auth.averify_token(KEY_A) is True
        # A sync caller cannot run the async store: it fails closed.
        assert auth.verify_token(KEY_A) is False

        del store[KEY_A]
        with pytest.raises(AuthenticationError, match="Invalid API key"):
            await auth.authenticate(_key_ctx(KEY_A))
        assert await auth.averify_token(KEY_A) is False

    async def test_a_failing_store_rejects_the_key(self, caplog):
        def lookup(key: str):
            raise ConnectionError("database down")

        auth = APIKeyAuth(lookup=lookup)
        with pytest.raises(AuthenticationError, match="Could not check the API key"):
            await auth.authenticate(_key_ctx(KEY_A))
        assert auth.verify_token(KEY_A) is False
        assert "ConnectionError" in caplog.text

    def test_runtime_helpers_need_the_dict_form(self):
        auth = APIKeyAuth(lookup=lambda key: None)
        with pytest.raises(TypeError, match="keys dict"):
            auth.revoke_key(KEY_A)
        with pytest.raises(TypeError, match="keys dict"):
            auth.add_key(KEY_A, "agent-1")

    def test_keys_or_lookup_is_required(self):
        with pytest.raises(ValueError):
            APIKeyAuth()
        with pytest.raises(ValueError):
            APIKeyAuth(keys={}, lookup=lambda key: None)

    async def test_http_gate_sees_a_revoked_key(self):
        auth = APIKeyAuth(keys={KEY_A: "agent-1"})
        server = MCPServer(name="gated", require_auth=True)
        server.add_middleware(AuthMiddleware(auth))
        verify = server._transport_auth_gate()
        assert verify is not None
        gate = _AuthGateASGI(_ok_app, verify)
        header = [(b"x-api-key", KEY_A.encode())]

        assert await _gate_status(gate, header) == 200
        auth.revoke_key(KEY_A)
        assert await _gate_status(gate, header) == 401

    async def test_http_gate_runs_an_async_lookup(self):
        valid = {KEY_A}

        async def lookup(key: str):
            return "agent-1" if key in valid else None

        server = MCPServer(name="gated", require_auth=True)
        server.add_middleware(AuthMiddleware(APIKeyAuth(lookup=lookup)))
        gate = _AuthGateASGI(_ok_app, server._transport_auth_gate())
        header = [(b"x-api-key", KEY_A.encode())]

        assert await _gate_status(gate, header) == 200
        valid.clear()
        assert await _gate_status(gate, header) == 401


# =====================================================================
# JWTAuth
# =====================================================================


class TestJWTRevocation:
    async def test_revoked_jti_is_rejected_even_from_the_cache(self):
        auth = JWTAuth(secret=SECRET)
        token = auth.create_token({"sub": "agent-7", "jti": "t-1"})
        assert await auth.authenticate(_bearer_ctx(token)) == "agent-7"  # now cached

        auth.revoke("t-1")
        with pytest.raises(AuthenticationError, match="revoked"):
            await auth.authenticate(_bearer_ctx(token))
        assert auth.verify_token(token) is False

        other = auth.create_token({"sub": "agent-7", "jti": "t-2"})
        assert await auth.authenticate(_bearer_ctx(other)) == "agent-7"

        assert auth.unrevoke("t-1") is True
        assert await auth.authenticate(_bearer_ctx(token)) == "agent-7"

    async def test_revoked_subject_rejects_all_its_tokens(self):
        auth = JWTAuth(secret=SECRET, revoked_subjects=["agent-9"])
        token = auth.create_token({"sub": "agent-9"})
        with pytest.raises(AuthenticationError, match="revoked"):
            await auth.authenticate(_bearer_ctx(token))
        assert await auth.authenticate(_bearer_ctx(auth.create_token({"sub": "agent-1"})))

    async def test_constructor_deny_list(self):
        auth = JWTAuth(secret=SECRET, revoked_jtis=["t-9"])
        token = auth.create_token({"sub": "a", "jti": "t-9"})
        assert auth.verify_token(token) is False

    async def test_entries_past_until_are_dropped(self, monkeypatch):
        auth = JWTAuth(secret=SECRET)
        auth.revoke("t-old", until=time.time() - 1)
        auth.revoke("t-new")  # prunes the expired entry
        assert "t-old" not in auth._deny._jtis
        assert "t-new" in auth._deny._jtis

    async def test_async_hook_is_asked_on_every_request(self):
        revoked: set[str] = set()
        asked: list[str] = []

        async def is_revoked(claims: dict) -> bool:
            asked.append(claims.get("jti"))
            return claims.get("jti") in revoked

        auth = JWTAuth(secret=SECRET, is_revoked=is_revoked)
        token = auth.create_token({"sub": "a", "jti": "t-3"})
        assert await auth.authenticate(_bearer_ctx(token)) == "a"
        revoked.add("t-3")
        with pytest.raises(AuthenticationError, match="revoked"):
            await auth.authenticate(_bearer_ctx(token))  # cached, still asked
        assert asked == ["t-3", "t-3"]
        assert await auth.averify_token(token) is False
        # verify_token cannot await the hook: it fails closed.
        revoked.clear()
        assert auth.verify_token(token) is False
        assert await auth.averify_token(token) is True

    async def test_a_failing_hook_rejects_the_token(self):
        def is_revoked(claims: dict) -> bool:
            raise TimeoutError("redis")

        auth = JWTAuth(secret=SECRET, is_revoked=is_revoked)
        token = auth.create_token({"sub": "a"})
        with pytest.raises(AuthenticationError, match="Could not check"):
            await auth.authenticate(_bearer_ctx(token))
        assert auth.verify_token(token) is False

    async def test_revoke_takes_effect_on_a_running_server(self):
        auth = JWTAuth(secret=SECRET)
        token = auth.create_token({"sub": "agent-7", "jti": "t-5"})
        client = TestClient(_server(auth))
        headers = {"authorization": f"Bearer {token}"}
        assert (await client.call_tool("whoami", {}, headers=headers))[0].text == "agent-7"
        auth.revoke(subject="agent-7")
        denied = await client.call_tool("whoami", {}, headers=headers)
        assert "revoked" in denied[0].text

    async def test_http_gate_uses_the_async_hook(self):
        revoked: set[str] = set()

        async def is_revoked(claims: dict) -> bool:
            return claims.get("sub") in revoked

        auth = JWTAuth(secret=SECRET, is_revoked=is_revoked)
        server = MCPServer(name="gated", require_auth=True)
        server.add_middleware(AuthMiddleware(auth))
        gate = _AuthGateASGI(_ok_app, server._transport_auth_gate())
        header = [(b"authorization", f"Bearer {auth.create_token({'sub': 'a'})}".encode())]

        assert await _gate_status(gate, header) == 200
        revoked.add("a")
        assert await _gate_status(gate, header) == 401

    def test_revoke_needs_something_to_revoke(self):
        with pytest.raises(ValueError):
            JWTAuth(secret=SECRET).revoke()
        with pytest.raises(TypeError):
            JWTAuth(secret=SECRET, revoked_jtis="t-1")


# =====================================================================
# JwksAuth and AsymmetricJWTAuth
# =====================================================================

_PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)


def _mint(claims: dict) -> str:
    return pyjwt.encode(claims, _PRIVATE_KEY, algorithm="RS256", headers={"kid": "k1"})


class _StubKeys:
    def get_signing_key_from_jwt(self, token: str):
        return SimpleNamespace(key=_PRIVATE_KEY.public_key())


class TestJwksRevocation:
    @staticmethod
    def _auth(**kwargs) -> JwksAuth:
        auth = JwksAuth(jwks_url="https://idp.example.com/jwks", audience="api://mcp", **kwargs)
        auth._jwk_client = _StubKeys()
        return auth

    async def test_revoke_jti(self):
        auth = self._auth()
        token = _mint({"sub": "bot", "aud": "api://mcp", "jti": "j-1"})
        assert await auth.authenticate(_bearer_ctx(token)) == "bot"
        auth.revoke("j-1")
        with pytest.raises(AuthenticationError, match="revoked"):
            await auth.authenticate(_bearer_ctx(token))
        assert auth.verify_token(token) is False

    async def test_hook(self):
        async def is_revoked(claims: dict) -> bool:
            return claims.get("sub") == "bot"

        auth = self._auth(is_revoked=is_revoked)
        token = _mint({"sub": "bot", "aud": "api://mcp"})
        with pytest.raises(AuthenticationError, match="revoked"):
            await auth.authenticate(_bearer_ctx(token))
        assert await auth.averify_token(token) is False

    def test_from_discovery_passes_the_deny_list(self):
        auth = JwksAuth.from_discovery(
            issuer="https://idp.example.com", audience="api://mcp", revoked_subjects=["bot"]
        )
        assert "bot" in auth._deny._subjects


class TestAsymmetricRevocation:
    async def test_revoke_subject(self):
        from cryptography.hazmat.primitives import serialization

        pem = (
            _PRIVATE_KEY.public_key()
            .public_bytes(
                serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
            )
            .decode()
        )
        auth = AsymmetricJWTAuth(public_key=pem)
        token = pyjwt.encode({"sub": "bot", "exp": int(time.time()) + 60}, _PRIVATE_KEY, "RS256")
        assert await auth.authenticate(_bearer_ctx(token)) == "bot"
        auth.revoke(subject="bot")
        with pytest.raises(AuthenticationError, match="revoked"):
            await auth.authenticate(_bearer_ctx(token))


# =====================================================================
# Custom providers: a refusal stops the call, however it is expressed
# =====================================================================


class _RevocableKeys:
    """A custom provider over a key store that can revoke keys while it runs."""

    def __init__(self, refuse_by: str = "raise") -> None:
        self.store = {KEY_A: {"client": "agent-1", "revoked": False}}
        self._refuse_by = refuse_by

    async def authenticate(self, ctx: RequestContext):
        entry = self.store.get(ctx.meta.get("x-api-key", ""))
        if entry is None or entry["revoked"]:
            if self._refuse_by == "raise":
                raise AuthenticationError("API key revoked")
            return {"none": None, "false": False, "empty": ""}[self._refuse_by]
        return entry["client"]


def _counting_server(auth, ran: list[str]) -> MCPServer:
    server = MCPServer(name="crm", require_auth=True)
    server.add_middleware(AuthMiddleware(auth))

    @server.tool()
    async def list_contacts(ctx: RequestContext, company: str) -> str:
        ran.append(str(ctx.client_id))
        return f"contacts at {company}"

    return server


class TestCustomProviderRefusal:
    @pytest.mark.parametrize("refuse_by", ["raise", "none", "false", "empty"])
    async def test_a_refusal_ends_the_call_before_the_tool_runs(self, refuse_by, caplog):
        # Returning None, False or "" let the call reach the tool with no
        # client id; only a raised AuthenticationError refused it.
        auth = _RevocableKeys(refuse_by)
        ran: list[str] = []
        client = TestClient(_counting_server(auth, ran))
        headers = {"x-api-key": KEY_A}

        ok = await client.call_tool("list_contacts", {"company": "Acme"}, headers=headers)
        assert "contacts at Acme" in ok[0].text
        auth.store[KEY_A]["revoked"] = True
        refused = await client.call_tool("list_contacts", {"company": "Acme"}, headers=headers)

        assert '"AUTHENTICATION_ERROR"' in refused[0].text
        assert ran == ["agent-1"]
        if refuse_by != "raise":
            assert "instead of a client id" in caplog.text

    async def test_without_a_verifier_the_server_warns_that_the_http_gate_is_off(self, caplog):
        server = _counting_server(_RevocableKeys(), [])
        assert server._transport_auth_gate() is None
        server.asgi_app()
        assert "HTTP auth gate is off" in caplog.text

    async def test_averify_token_arms_the_http_gate_for_a_custom_provider(self, caplog):
        class GatedKeys(_RevocableKeys):
            async def averify_token(self, credential: str) -> bool:
                entry = self.store.get(credential)
                return entry is not None and not entry["revoked"]

        auth = GatedKeys()
        server = _counting_server(auth, [])
        server.asgi_app()
        assert "HTTP auth gate is off" not in caplog.text
        gate = _AuthGateASGI(_ok_app, server._transport_auth_gate())
        header = [(b"x-api-key", KEY_A.encode())]

        assert await _gate_status(gate, header) == 200
        auth.store[KEY_A]["revoked"] = True
        assert await _gate_status(gate, header) == 401
