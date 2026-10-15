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
        ctx = RequestContext(server_name="s", tool_name="t", client_id="agent-x", client=client)
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
        ctx = RequestContext(server_name="s", tool_name="t", client_id="agent-x", client=client)
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


# ---------------------------------------------------------------------------
# Regression tests: what the middleware writes to the log file
# ---------------------------------------------------------------------------

_KEY = "test-audit-secret"


def _file_entries(path: Any) -> list[dict[str, Any]]:
    with open(path) as f:
        return [json.loads(line) for line in f]


def _links(entries: list[dict[str, Any]]) -> bool:
    prev = "0" * 64
    for e in entries:
        if e["prev_hash"] != prev:
            return False
        prev = e["hmac"]
    return True


def _records_server(audit: AuditMiddleware) -> Any:
    from promptise.mcp.server import MCPServer

    server = MCPServer(name="records")
    server.add_middleware(audit)

    @server.tool()
    async def view_record(patient_id: str) -> str:
        return f"record {patient_id}"

    return server


class TestAuditLogFile:
    async def test_include_args_records_the_tool_arguments(self, tmp_path: Any) -> None:
        # It logged the public keys of ctx.state instead: the ToolDef repr
        # (handler, schema...) and never the arguments the docs promise.
        from promptise.mcp.server import TestClient

        path = tmp_path / "audit.jsonl"
        audit = AuditMiddleware(log_path=str(path), hmac_secret=_KEY, include_args=True)
        await TestClient(_records_server(audit)).call_tool("view_record", {"patient_id": "P-1"})
        [entry] = _file_entries(path)
        assert entry["args"] == {"patient_id": "P-1"}

    async def test_file_holds_entries_in_chain_order(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # Writes ran in the thread pool after the chain lock was released, so
        # a slow write landed after a later entry: the file looked reordered.
        import asyncio
        import time

        from promptise.mcp.server import TestClient

        path = tmp_path / "audit.jsonl"
        audit = AuditMiddleware(log_path=str(path), hmac_secret=_KEY, include_args=True)
        real_write = audit._write_log_line

        def slow_first_write(line: str) -> None:
            if '"P-0"' in line:
                time.sleep(0.2)
            real_write(line)

        monkeypatch.setattr(audit, "_write_log_line", slow_first_write)
        client = TestClient(_records_server(audit))
        await asyncio.gather(
            *[client.call_tool("view_record", {"patient_id": f"P-{i}"}) for i in range(4)]
        )
        entries = _file_entries(path)
        assert len(entries) == 4
        assert _links(entries)

    async def test_failed_write_does_not_break_the_file_chain(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # The chain advanced past an entry that never reached the file, so
        # the next entry looked like it followed a deleted one.
        path = tmp_path / "audit.jsonl"
        mw = AuditMiddleware(log_path=str(path), hmac_secret=_KEY)
        real_write = mw._write_log_line
        calls = {"n": 0}

        def flaky(line: str) -> None:
            calls["n"] += 1
            if calls["n"] == 2:
                raise OSError("disk full")
            real_write(line)

        monkeypatch.setattr(mw, "_write_log_line", flaky)
        for _ in range(3):
            await _audit(mw, RequestContext(server_name="s", tool_name="t", client_id="c"))
        entries = _file_entries(path)
        assert len(entries) == 2
        assert _links(entries)
        assert [e["seq"] for e in entries] == [0, 1]

    async def test_restart_continues_the_chain_in_the_same_file(self, tmp_path: Any) -> None:
        # Every restart began a new chain at the genesis hash, so entries
        # just before a restart could be deleted without a trace.
        path = tmp_path / "audit.jsonl"
        for _ in range(2):
            mw = AuditMiddleware(log_path=str(path), hmac_secret=_KEY)
            for _ in range(2):
                await _audit(mw, RequestContext(server_name="s", tool_name="t", client_id="c"))
            assert mw.verify_chain()
        entries = _file_entries(path)
        assert _links(entries)
        assert [e["seq"] for e in entries] == [0, 1, 2, 3]
        assert entries[2].get("resumed") is True

    def test_signed_file_log_requires_a_key(
        self, tmp_path: Any, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        # A random per-process key made the file impossible to verify.
        monkeypatch.delenv("PROMPTISE_AUDIT_SECRET", raising=False)
        with pytest.raises(ValueError, match="PROMPTISE_AUDIT_SECRET"):
            AuditMiddleware(log_path=str(tmp_path / "audit.jsonl"))
        AuditMiddleware(log_path=str(tmp_path / "plain.jsonl"), signed=False)
        AuditMiddleware()  # in-memory only: an auto-generated key is fine
        monkeypatch.setenv("PROMPTISE_AUDIT_SECRET", "from-env")
        AuditMiddleware(log_path=str(tmp_path / "audit.jsonl"))

    async def test_memory_buffer_is_bounded(self) -> None:
        # entries grew without limit on a long-running server.
        mw = AuditMiddleware(hmac_secret=_KEY, max_memory_entries=3)
        for _ in range(5):
            await _audit(mw, RequestContext(server_name="s", tool_name="t", client_id="c"))
        assert len(mw.entries) == 3
        assert [e["seq"] for e in mw.entries] == [2, 3, 4]
        assert mw.verify_chain()
        mw.entries[1]["status"] = "error"
        assert not mw.verify_chain()


# ---------------------------------------------------------------------------
# Error codes: a guard denial is not a failed login
# ---------------------------------------------------------------------------

_ERR_SECRET = "audit-error-code-test-secret-32-bytes"
_ERR_KEY = "audit-error-code-hmac-key-0123456789"


def _orders_server(audit: AuditMiddleware) -> tuple[Any, Any]:
    from promptise.mcp.server import AuthMiddleware, JWTAuth, MCPServer, RateLimitMiddleware

    server = MCPServer("orders")
    jwt_auth = JWTAuth(secret=_ERR_SECRET)
    # Audit outermost, so it sees what AuthMiddleware rejects too. Burst 3:
    # alice's third allowed call is her last.
    server.add_middleware(audit)
    server.add_middleware(AuthMiddleware(jwt_auth))
    server.add_middleware(RateLimitMiddleware(rate_per_minute=1, burst=3))

    @server.tool(roles=["manager"])
    async def refund_order(order_id: str) -> str:
        return f"refunded {order_id}"

    @server.tool(auth=True)
    async def lookup(order_id: int) -> str:
        return f"order {order_id}"

    return server, jwt_auth


class TestAuditErrorCode:
    async def test_entries_carry_the_code_the_client_received(self, tmp_path: Any) -> None:
        from promptise.mcp.server import TestClient, verify_audit_log

        log = tmp_path / "audit.jsonl"
        audit = AuditMiddleware(log_path=str(log), hmac_secret=_ERR_KEY)
        server, jwt_auth = _orders_server(audit)
        alice = jwt_auth.create_token({"sub": "alice", "roles": ["support"]})

        def client(token: str | None) -> Any:
            meta = {"authorization": f"Bearer {token}"} if token else {}
            return TestClient(server, meta=meta)

        denied = await client(alice).call_tool("refund_order", {"order_id": "A1"})
        assert "ACCESS_DENIED" in denied[0].text
        await client("not-a-jwt").call_tool("refund_order", {"order_id": "A1"})
        await client(alice).call_tool("lookup", {"order_id": "not-an-int"})
        await client(alice).call_tool("lookup", {"order_id": 1})
        await client(alice).call_tool("lookup", {"order_id": 2})

        entries = [json.loads(line) for line in log.read_text().splitlines()]
        assert [(e["tool"], e["status"], e.get("error_code")) for e in entries] == [
            ("refund_order", "error", "ACCESS_DENIED"),
            ("refund_order", "error", "AUTHENTICATION_ERROR"),
            ("lookup", "error", "VALIDATION_ERROR"),
            ("lookup", "ok", None),
            ("lookup", "error", "RATE_LIMIT_EXCEEDED"),
        ]
        # The class name stays in ``error`` (unchanged format).
        assert entries[0]["error"].startswith("AuthenticationError: ")
        # error_code is inside the HMAC chain, and the file verifies.
        assert verify_audit_log(log, _ERR_KEY).ok
        tampered = dict(entries[0], error_code="AUTHENTICATION_ERROR")
        lines = log.read_text().splitlines()
        lines[0] = json.dumps(tampered)
        log.write_text("\n".join(lines) + "\n")
        assert not verify_audit_log(log, _ERR_KEY).ok

    async def test_no_code_for_exceptions_that_are_not_mcp_errors(self) -> None:
        mw = AuditMiddleware(signed=False)
        ctx = RequestContext(server_name="s", tool_name="t")

        async def boom(c: RequestContext) -> str:
            raise RuntimeError("bug")

        with pytest.raises(RuntimeError):
            await mw(ctx, boom)
        entry = mw.entries[-1]
        assert entry["error"] == "RuntimeError: bug"
        assert "error_code" not in entry

    async def test_old_entries_without_error_code_still_verify(self, tmp_path: Any) -> None:
        """A 1.3.4 log (errors without ``error_code``) verifies, and a
        1.3.5 server continues its chain."""
        from promptise.mcp.server import verify_audit_log
        from promptise.mcp.server._audit import GENESIS_HASH, canonical_entry, entry_mac

        log = tmp_path / "audit.jsonl"
        prev = GENESIS_HASH
        old_lines = []
        for seq, (status, error) in enumerate(
            [("ok", None), ("error", "AuthenticationError: Requires any of roles [manager]")]
        ):
            entry: dict[str, Any] = {
                "timestamp": 1.0 + seq,
                "tool": "refund_order",
                "client_id": "alice",
                "request_id": f"r{seq}",
                "status": status,
                "duration_s": 0.001,
            }
            if error:
                entry["error"] = error
            entry = canonical_entry(entry)
            entry["seq"] = seq
            entry["prev_hash"] = prev
            entry["hmac"] = entry_mac(_ERR_KEY.encode(), entry)
            prev = entry["hmac"]
            old_lines.append(json.dumps(entry))
        log.write_text("\n".join(old_lines) + "\n")
        assert verify_audit_log(log, _ERR_KEY).ok

        audit = AuditMiddleware(log_path=str(log), hmac_secret=_ERR_KEY)
        ctx = RequestContext(server_name="s", tool_name="refund_order")

        async def deny(c: RequestContext) -> str:
            from promptise.mcp.server import AuthenticationError

            raise AuthenticationError("denied", code="ACCESS_DENIED")

        with pytest.raises(Exception):
            await audit(ctx, deny)
        lines = log.read_text().splitlines()
        assert len(lines) == 3
        assert json.loads(lines[2])["error_code"] == "ACCESS_DENIED"
        report = verify_audit_log(log, _ERR_KEY)
        assert report.ok, report.first_problem
