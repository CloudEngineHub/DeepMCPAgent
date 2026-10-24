"""The MCP server docs' examples run as written.

Each test pulls a ``python`` code block out of a page under ``docs/`` by its
heading, executes it, and exercises what it defines, so an example that
drifts from the API fails here instead of in a reader's terminal.  Prose
claims about behaviour (what lands in the audit log, when a warning is
logged) are checked against the real behaviour too.
"""

from __future__ import annotations

import inspect
import json
import logging
import re
import sys
import types
from pathlib import Path
from typing import Any

import pytest

from promptise.mcp.server import TestClient

DOCS = Path(__file__).resolve().parent.parent / "docs"


def _code_block(page: str, heading: str, index: int = 0) -> str:
    """The *index*-th ``python`` block in the section under *heading*."""
    lines = (DOCS / page).read_text(encoding="utf-8").splitlines(keepends=True)
    section: list[str] | None = None
    level = 0
    in_fence = False
    for line in lines:
        if line.startswith("```"):
            in_fence = not in_fence
        heading_match = None if in_fence else re.match(r"(#+) (.*?)\s*$", line)
        if section is None:
            if heading_match and heading_match.group(2) == heading:
                section, level = [], len(heading_match.group(1))
            continue
        if heading_match and len(heading_match.group(1)) <= level:
            break
        section.append(line)
    assert section is not None, f"heading {heading!r} not found in {page}"
    blocks = re.findall(r"```python\n(.*?)```", "".join(section), re.DOTALL)
    assert len(blocks) > index, f"no python block #{index} under {heading!r} in {page}"
    return blocks[index]


def _run(code: str, **namespace: Any) -> dict[str, Any]:
    ns: dict[str, Any] = {"__name__": "docs_example", **namespace}
    exec(compile(code, "<docs example>", "exec"), ns)
    return ns


def _text(result: list[Any]) -> str:
    return result[0].text


# ---------------------------------------------------------------------------
# testing.md
# ---------------------------------------------------------------------------


async def test_testing_with_authentication_example_passes():
    ns = _run(_code_block("mcp/server/testing.md", "Testing with authentication"))
    tests = [f for name, f in ns.items() if name.startswith("test_") and callable(f)]
    assert len(tests) == 3
    for test in tests:
        assert inspect.iscoroutinefunction(test)
        await test()


# ---------------------------------------------------------------------------
# auth-security.md
# ---------------------------------------------------------------------------


async def test_custom_auth_provider_example_gives_the_caller_its_roles():
    ns = _run(_code_block("mcp/server/auth-security.md", "Custom auth provider"))
    from promptise.mcp.server import AuthMiddleware, MCPServer, RequestContext

    tokens = {
        "tok-admin": {"active": True, "sub": "ops-bot", "roles": ["admin"], "scope": "read"},
        "tok-viewer": {"active": True, "sub": "viewer-bot", "roles": ["viewer"]},
        "tok-revoked": {"active": False},
    }

    async def introspect(token: str) -> dict:
        return tokens.get(token, {"active": False})

    server = MCPServer("crm")
    server.add_middleware(AuthMiddleware(ns["OAuth2Introspection"](introspect)))

    @server.tool(roles=["admin"])
    async def purge_cache(ctx: RequestContext) -> str:
        """Admins only."""
        return f"purged by {ctx.client.client_id} with scopes {sorted(ctx.client.scopes)}"

    async def call(token: str) -> str:
        meta = {"authorization": f"Bearer {token}"}
        return _text(await TestClient(server, meta=meta).call_tool("purge_cache", {}))

    assert await call("tok-admin") == "purged by ops-bot with scopes ['read']"
    assert "ACCESS_DENIED" in await call("tok-viewer")
    assert "AUTHENTICATION_ERROR" in await call("tok-revoked")


async def test_custom_provider_with_averify_token_is_refused_at_the_gate(tmp_path, caplog):
    import hashlib

    from promptise.mcp.server import AuthMiddleware, MCPServer, RequestContext
    from promptise.mcp.server._transport import _AuthGateASGI

    ns = _run(_code_block("mcp/server/auth-security.md", "Custom auth provider", index=1))
    key = "crm-" + "k" * 12
    store = tmp_path / "keys.json"
    entry = {"client": "crm-agent", "revoked": False}
    store.write_text(json.dumps({hashlib.sha256(key.encode()).hexdigest(): entry}))

    server = MCPServer("crm", require_auth=True)
    server.add_middleware(AuthMiddleware(ns["RevocableKeyAuth"](store)))

    @server.tool()
    async def whoami(ctx: RequestContext) -> str:
        return str(ctx.client_id)

    server.asgi_app()
    assert "HTTP auth gate is off" not in caplog.text  # the prose: no startup warning

    async def gate_status() -> int:
        sent: list[dict[str, Any]] = []

        async def ok_app(scope: Any, receive: Any, send: Any) -> None:
            pass

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)

        gate = _AuthGateASGI(ok_app, server._transport_auth_gate())
        await gate(
            {"type": "http", "path": "/mcp", "headers": [(b"x-api-key", key.encode())]}, None, send
        )
        return next((m["status"] for m in sent if m["type"] == "http.response.start"), 200)

    client = TestClient(server, meta={"x-api-key": key})
    assert _text(await client.call_tool("whoami", {})) == "crm-agent"
    assert await gate_status() == 200

    entry["revoked"] = True
    store.write_text(json.dumps({hashlib.sha256(key.encode()).hexdigest(): entry}))
    assert "AUTHENTICATION_ERROR" in _text(await client.call_tool("whoami", {}))
    assert await gate_status() == 401


# ---------------------------------------------------------------------------
# caching-performance.md
# ---------------------------------------------------------------------------


async def test_tiered_rate_limiting_example_limits_by_tier():
    ns = _run(_code_block("mcp/server/caching-performance.md", "Tiered rate limiting"))
    client = TestClient(ns["server"])

    async def codes(key: str, n: int) -> list[str]:
        out = []
        for _ in range(n):
            text = _text(
                await client.call_tool("search", {"query": "x"}, headers={"x-api-key": key})
            )
            out.append(json.loads(text)["error"]["code"] if text.startswith("{") else "ok")
        return out

    assert await codes("sk-free", 4) == ["ok", "ok", "ok", "RATE_LIMIT_EXCEEDED"]
    assert await codes("sk-premium", 10) == ["ok"] * 10


# ---------------------------------------------------------------------------
# showcase.md
# ---------------------------------------------------------------------------


@pytest.fixture
def secrets_env(monkeypatch, tmp_path):
    monkeypatch.setenv("JWT_SECRET", "showcase-jwt-secret-at-least-32-bytes")
    monkeypatch.setenv("AUDIT_SECRET", "showcase-audit-secret")
    monkeypatch.chdir(tmp_path)
    return tmp_path


async def test_showcase_payments_example(secrets_env, monkeypatch):
    from promptise.mcp.server import JWTAuth, verify_audit_log

    charged: list[dict[str, Any]] = []

    async def create_async(**kwargs: Any) -> Any:
        charged.append(kwargs)
        return types.SimpleNamespace(id="ch_1", status="succeeded")

    fake_stripe = types.ModuleType("stripe")
    fake_stripe.Charge = types.SimpleNamespace(create_async=create_async)  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "stripe", fake_stripe)

    code = _code_block("resources/showcase.md", "2. Payment processing server with audit logging")
    assert "${" not in code  # keys come from the environment, not literal placeholders
    ns = _run(code)

    token = JWTAuth(secret="showcase-jwt-secret-at-least-32-bytes").create_token(
        {"sub": "billing-agent"}, expires_in=60
    )
    args = {"customer_id": "cus_1", "amount_cents": 1250, "description": "Plan"}
    anonymous = _text(await TestClient(ns["server"]).call_tool("charge_customer", args))
    assert "AUTHENTICATION_ERROR" in anonymous
    client = TestClient(ns["server"], meta={"authorization": f"Bearer {token}"})
    result = json.loads(_text(await client.call_tool("charge_customer", args)))
    assert result == {"id": "ch_1", "status": "succeeded"}
    assert charged == [
        {"customer": "cus_1", "amount": 1250, "currency": "usd", "description": "Plan"}
    ]

    log = secrets_env / "audit.jsonl"
    report = verify_audit_log(str(log), "showcase-audit-secret")  # signed with the env key
    assert report.ok and report.entries == 2
    refused, charge = (json.loads(line) for line in log.read_text().splitlines())
    assert refused["status"] == "error" and refused["client_id"] is None
    assert charge["status"] == "ok" and charge["client_id"] == "billing-agent"


async def test_showcase_analytics_example(secrets_env):
    from promptise.mcp.server import JWTAuth

    code = _code_block("resources/showcase.md", "1. Database query tool with role-based access")
    assert "${" not in code
    ns = _run(code)
    server = ns["server"]
    assert server._tool_registry.get("query_analytics").auth is True
    viewer = JWTAuth(secret="showcase-jwt-secret-at-least-32-bytes").create_token(
        {"sub": "bot", "roles": ["viewer"]}, expires_in=60
    )
    client = TestClient(server, meta={"authorization": f"Bearer {viewer}"})
    denied = _text(await client.call_tool("run_migration", {"migration_name": "m1"}))
    assert "ACCESS_DENIED" in denied


# ---------------------------------------------------------------------------
# approval-gates.md "Audit visibility" and observability.md "Key rotation"
# ---------------------------------------------------------------------------


async def test_approval_denial_audit_entry_is_as_documented(tmp_path, caplog):
    from promptise.mcp.server import ApprovalGateMiddleware, AuditMiddleware, MCPServer

    log = tmp_path / "audit.jsonl"
    server = MCPServer("refunds")
    server.add_middleware(AuditMiddleware(log_path=str(log), hmac_secret="k"))
    server.add_middleware(ApprovalGateMiddleware(lambda request: False))

    @server.tool(requires_approval=True)
    async def issue_refund(order_id: str) -> str:
        return "refunded"

    with caplog.at_level(logging.INFO, logger="promptise.server"):
        reply = json.loads(
            _text(await TestClient(server).call_tool("issue_refund", {"order_id": "A"}))
        )
    request_id = reply["error"]["details"]["approval_request_id"]  # the client gets it

    entry = json.loads(log.read_text().splitlines()[-1])
    assert entry["status"] == "error"
    assert entry["error"] == "ApprovalDeniedError: Approval denied for tool 'issue_refund'"
    assert request_id not in log.read_text()  # not in the audit entry
    assert any(
        "Approval DENIED" in r.getMessage() and request_id in r.getMessage() for r in caplog.records
    )


async def test_key_rotation_warning_comes_on_the_first_audited_call(tmp_path, caplog):
    from promptise.mcp.server import AuditMiddleware, MCPServer

    log = tmp_path / "audit.jsonl"

    def server_with(key: str) -> MCPServer:
        server = MCPServer("records")
        server.add_middleware(AuditMiddleware(log_path=str(log), hmac_secret=key))

        @server.tool()
        async def ping() -> str:
            return "pong"

        return server

    await TestClient(server_with("old-key")).call_tool("ping", {})
    with caplog.at_level(logging.WARNING):
        rotated = server_with("new-key")
        rotated.asgi_app()  # "startup": nothing read yet
        assert not any("key rotated" in r.getMessage() for r in caplog.records)
        await TestClient(rotated).call_tool("ping", {})
    assert any("key rotated" in r.getMessage() for r in caplog.records)


# ---------------------------------------------------------------------------
# multi-user-systems.md and secure-multi-tenant-platform.md
# ---------------------------------------------------------------------------


async def test_multi_user_customer_ops_server_example(secrets_env):
    from unittest.mock import AsyncMock

    from promptise.mcp.server import JWTAuth, verify_audit_log

    code = _code_block("guides/multi-user-systems.md", "A real MCP server with 30 tools")
    assert "${" not in code  # keys come from the environment, not literal placeholders
    db = types.SimpleNamespace(get_customer=AsyncMock(return_value={"id": "c1"}))
    ns = _run(code, db=db)
    server = ns["server"]
    assert len(server._tool_registry.list_all()) == 30  # as the guide says

    anonymous = _text(
        await TestClient(server).call_tool("customers_get_customer", {"customer_id": "c1"})
    )
    assert "AUTHENTICATION_ERROR" in anonymous
    token = JWTAuth(secret="showcase-jwt-secret-at-least-32-bytes").create_token(
        {"sub": "support-agent", "scope": "read"}, expires_in=60
    )
    client = TestClient(server, meta={"authorization": f"Bearer {token}"})
    result = json.loads(
        _text(await client.call_tool("customers_get_customer", {"customer_id": "c1"}))
    )
    assert result == {"id": "c1"}

    report = verify_audit_log(str(secrets_env / "audit.jsonl"), "showcase-audit-secret")
    assert report.ok and report.entries == 2


@pytest.mark.parametrize(
    ("page", "heading"),
    [
        ("guides/multi-user-systems.md", "Step 4: Enable Tamper-Evident Audit Logging"),
        ("guides/secure-multi-tenant-platform.md", "Step 4: Tamper-evident audit, tenant-stamped"),
    ],
)
async def test_guide_audit_examples_sign_with_the_environment_key(secrets_env, page, heading):
    from promptise.mcp.server import MCPServer, verify_audit_log

    server = MCPServer("guide")

    @server.tool()
    async def ping() -> str:
        return "pong"

    code = _code_block(page, heading)
    assert "${" not in code
    _run(code, server=server)  # AuditMiddleware(secret=...) raised TypeError here
    await TestClient(server).call_tool("ping", {})
    report = verify_audit_log(str(secrets_env / "audit.jsonl"), "showcase-audit-secret")
    assert report.ok and report.entries == 1


_KEY_TAKING_CALLS = re.compile(
    r"\b(JWTAuth|AsymmetricJWTAuth|AuditMiddleware|HTTPServerSpec|MCPClient|"
    r"PostgresConversationStore)\((?:[^()]|\([^()]*\))*?[\"']\$\{",
    re.DOTALL,
)


def _python_blocks(path: Path) -> list[str]:
    return re.findall(r"```python\n(.*?)```", path.read_text(encoding="utf-8"), re.DOTALL)


def test_no_doc_example_passes_a_literal_env_placeholder_or_audit_secret_kwarg():
    """``"${VAR}"`` is only expanded by ``.superagent`` files, the env resolver
    and a few documented parameters; these calls take it literally.  And
    ``AuditMiddleware`` has no ``secret=`` parameter (it is ``hmac_secret=``)."""
    problems = []
    for path in sorted(DOCS.rglob("*.md")):
        for block in _python_blocks(path):
            if re.search(r"AuditMiddleware\((?:[^()]|\([^()]*\))*?\bsecret=", block, re.DOTALL):
                problems.append(f"{path.relative_to(DOCS)}: AuditMiddleware(secret=...)")
            match = _KEY_TAKING_CALLS.search(block)
            if match:
                problems.append(f'{path.relative_to(DOCS)}: {match.group(1)}("${{...}}")')
    assert problems == []
