"""Tests for AuditMiddleware identity enrichment.

The tamper-evident audit log records the verified identity of the acting
agent (subject / issuer / audience / roles), inside the HMAC chain, so
"which agent did what" is attributable and integrity-protected — without
leaking the token or the full claim set.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from promptise.mcp.server import AuditMiddleware
from promptise.mcp.server._context import ClientContext, RequestContext


async def _audit(mw: AuditMiddleware, ctx: RequestContext) -> dict[str, Any]:
    async def call_next(c: RequestContext) -> str:
        return "ok"

    await mw(ctx, call_next)
    return mw.entries[-1]


class TestAuditIdentity:
    async def test_records_verified_identity(self) -> None:
        mw = AuditMiddleware(signed=False)
        client = ClientContext(
            client_id="agent-x",
            subject="agent-x",
            issuer="https://idp",
            audience="api://mcp",
            roles={"writer", "reader"},
        )
        ctx = RequestContext(
            server_name="s", tool_name="t", client_id="agent-x", client=client
        )
        entry = await _audit(mw, ctx)
        assert entry["client_id"] == "agent-x"
        assert entry["identity"] == {
            "subject": "agent-x",
            "issuer": "https://idp",
            "audience": "api://mcp",
            "roles": ["reader", "writer"],
        }

    async def test_no_identity_block_for_api_key_auth(self) -> None:
        # API-key auth: a client_id but no JWT subject/issuer/roles.
        mw = AuditMiddleware(signed=False)
        ctx = RequestContext(
            server_name="s",
            tool_name="t",
            client_id="client-1",
            client=ClientContext(client_id="client-1"),
        )
        entry = await _audit(mw, ctx)
        assert entry["client_id"] == "client-1"
        assert "identity" not in entry

    async def test_identity_is_inside_the_hmac_chain(self) -> None:
        mw = AuditMiddleware(signed=True, hmac_secret="test-audit-secret")
        ctx = RequestContext(
            server_name="s",
            tool_name="t",
            client_id="agent-x",
            client=ClientContext(subject="agent-x", issuer="https://idp"),
        )
        entry = await _audit(mw, ctx)
        assert mw.verify_chain() is True
        # Tampering with the recorded identity breaks the chain.
        entry["identity"]["subject"] = "impersonator"
        assert mw.verify_chain() is False

    async def test_no_token_or_full_claims_leaked(self) -> None:
        mw = AuditMiddleware(signed=False)
        client = ClientContext(
            subject="agent-x",
            issuer="https://idp",
            claims={"sub": "agent-x", "secret_claim": "sensitive-value"},
        )
        ctx = RequestContext(
            server_name="s", tool_name="t", client_id="agent-x", client=client
        )
        entry = await _audit(mw, ctx)
        assert "claims" not in entry["identity"]
        assert "sensitive-value" not in json.dumps(entry)


@pytest.mark.parametrize("signed", [True, False])
async def test_basic_entry_fields(signed: bool) -> None:
    mw = AuditMiddleware(signed=signed, hmac_secret="test-audit-secret")
    ctx = RequestContext(server_name="s", tool_name="mytool", client_id="c1")
    entry = await _audit(mw, ctx)
    assert entry["tool"] == "mytool"
    assert entry["status"] == "ok"
    assert "duration_s" in entry
    assert ("hmac" in entry) is signed
