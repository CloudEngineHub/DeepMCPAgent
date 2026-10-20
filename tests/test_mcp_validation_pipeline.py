"""Schema-invalid tool calls run through the same pipeline on the wire as in TestClient.

Regression (v1.3.3): over a real transport the MCP SDK validated a call's
arguments against the tool's ``inputSchema`` before Promptise saw the call.
An invalid call therefore skipped the whole middleware chain (no
authentication, no rate limiting, no audit entry) and came back as plain
text, ``Input validation error: <jsonschema message>``, that quoted the
offending value in full: a 1 MB string against ``max_length=10`` produced a
1 MB error.

Now the server validates inside its own pipeline, on every transport and in
``TestClient``: authentication first, then rate limiting and audit, then a
bounded ``VALIDATION_ERROR`` envelope that never echoes the input.

Every server binds ``127.0.0.1`` on an OS-assigned port in-process.
"""

from __future__ import annotations

import json
from typing import Annotated, Any

import pytest
from _live_server import serve_mcp
from pydantic import Field

from promptise.mcp._compat import field as mcp_field
from promptise.mcp.client import MCPClient
from promptise.mcp.server import (
    APIKeyAuth,
    ApprovalGateMiddleware,
    AuditMiddleware,
    AuthMiddleware,
    MCPServer,
    RateLimitMiddleware,
    TestClient,
)
from promptise.mcp.server._validation import MAX_VALIDATION_MESSAGE_CHARS

KEY = "sk-test-1"
BIG = "x" * 1_000_000
# Generous: the envelope's message, field errors and suggestion together.
MAX_ERROR_CHARS = 4 * MAX_VALIDATION_MESSAGE_CHARS


def _server(audit: AuditMiddleware, *, burst: int = 100) -> MCPServer:
    server = MCPServer("validation-pipeline")
    server.add_middleware(AuthMiddleware(APIKeyAuth(keys={KEY: {"client_id": "agent-1"}})))
    server.add_middleware(RateLimitMiddleware(rate_per_minute=60, burst=burst))
    server.add_middleware(audit)

    @server.tool(auth=True)
    async def echo(text: Annotated[str, Field(max_length=10)]) -> str:
        """Echo a short text."""
        return text

    return server


def _error(text: str) -> dict[str, Any]:
    payload = json.loads(text)
    assert isinstance(payload, dict) and "error" in payload, text[:200]
    return payload["error"]


class _Wire:
    """Call tools over streamable HTTP and return (is_error, text)."""

    def __init__(self, client: MCPClient) -> None:
        self._client = client

    async def call(self, name: str, args: dict[str, Any]) -> tuple[bool, str]:
        result = await self._client.call_tool(name, args)
        return bool(mcp_field(result, "is_error")), result.content[0].text


class _InProcess:
    def __init__(self, client: TestClient, headers: dict[str, str] | None) -> None:
        self._client = client
        self._headers = headers

    async def call(self, name: str, args: dict[str, Any]) -> tuple[bool, str]:
        result = await self._client.call_tool(name, args, headers=self._headers)
        text = result[0].text
        return '"error"' in text[:40], text


async def _check_invalid_call_is_bounded_and_structured(caller: Any) -> None:
    is_error, text = await caller.call("echo", {"text": BIG})
    assert is_error
    assert len(text) < MAX_ERROR_CHARS, len(text)
    assert "xxxxxxxxxxxxxxxxxxxx" not in text
    err = _error(text)
    assert err["code"] == "VALIDATION_ERROR"
    assert "text" in err["details"]["field_errors"]


class TestOverHTTP:
    @pytest.mark.asyncio
    async def test_invalid_call_gets_a_bounded_validation_error_envelope(self) -> None:
        audit = AuditMiddleware(signed=False)
        async with serve_mcp(_server(audit)) as port:
            async with MCPClient(url=f"http://127.0.0.1:{port}/mcp", api_key=KEY) as client:
                await _check_invalid_call_is_bounded_and_structured(_Wire(client))

    @pytest.mark.asyncio
    async def test_authentication_runs_before_validation(self) -> None:
        audit = AuditMiddleware(signed=False)
        async with serve_mcp(_server(audit)) as port:
            async with MCPClient(url=f"http://127.0.0.1:{port}/mcp") as client:
                is_error, text = await _Wire(client).call("echo", {"text": BIG})
        assert is_error
        assert len(text) < MAX_ERROR_CHARS
        assert _error(text)["code"] == "AUTHENTICATION_ERROR"

    @pytest.mark.asyncio
    async def test_invalid_calls_are_rate_limited_and_audited(self) -> None:
        audit = AuditMiddleware(signed=False)
        async with serve_mcp(_server(audit, burst=2)) as port:
            async with MCPClient(url=f"http://127.0.0.1:{port}/mcp", api_key=KEY) as client:
                wire = _Wire(client)
                codes = []
                for args in ({"text": BIG}, {"text": BIG}, {"text": BIG}, {"text": "ok"}):
                    _, text = await wire.call("echo", args)
                    codes.append(_error(text)["code"])
        assert codes == [
            "VALIDATION_ERROR",
            "VALIDATION_ERROR",
            "RATE_LIMIT_EXCEEDED",
            "RATE_LIMIT_EXCEEDED",
        ]
        statuses = [(e["tool"], e["status"], e["client_id"]) for e in audit.entries]
        assert statuses[:2] == [("echo", "error", "agent-1")] * 2
        # The audit entry does not carry the rejected input either
        assert all(len(json.dumps(e)) < MAX_ERROR_CHARS for e in audit.entries)


class TestInProcessParity:
    @pytest.mark.asyncio
    async def test_invalid_call_gets_a_bounded_validation_error_envelope(self) -> None:
        audit = AuditMiddleware(signed=False)
        caller = _InProcess(TestClient(_server(audit)), {"x-api-key": KEY})
        await _check_invalid_call_is_bounded_and_structured(caller)

    @pytest.mark.asyncio
    async def test_authentication_runs_before_validation(self) -> None:
        audit = AuditMiddleware(signed=False)
        caller = _InProcess(TestClient(_server(audit)), None)
        _, text = await caller.call("echo", {"text": BIG})
        assert _error(text)["code"] == "AUTHENTICATION_ERROR"

    @pytest.mark.asyncio
    async def test_invalid_calls_are_rate_limited_and_audited(self) -> None:
        audit = AuditMiddleware(signed=False)
        caller = _InProcess(TestClient(_server(audit, burst=2)), {"x-api-key": KEY})
        codes = []
        for args in ({"text": BIG}, {"text": BIG}, {"text": "ok"}):
            _, text = await caller.call("echo", args)
            codes.append(_error(text)["code"])
        assert codes == ["VALIDATION_ERROR", "VALIDATION_ERROR", "RATE_LIMIT_EXCEEDED"]
        assert [e["status"] for e in audit.entries][:2] == ["error", "error"]

    @pytest.mark.asyncio
    async def test_invalid_call_never_reaches_a_human_approver(self) -> None:
        asked: list[Any] = []

        async def approve(request: Any) -> Any:
            from promptise.approval import ApprovalDecision

            asked.append(request)
            return ApprovalDecision(approved=True)

        server = MCPServer("gated")
        server.add_middleware(ApprovalGateMiddleware(approve))

        @server.tool(requires_approval=True)
        async def refund(order_id: Annotated[str, Field(max_length=10)]) -> str:
            """Refund an order."""
            return f"refunded {order_id}"

        result = await TestClient(server).call_tool("refund", {"order_id": BIG})
        assert _error(result[0].text)["code"] == "VALIDATION_ERROR"
        assert asked == []

    @pytest.mark.asyncio
    async def test_many_field_errors_stay_bounded(self) -> None:
        server = MCPServer("many")

        @server.tool()
        async def total(values: list[int]) -> int:
            """Sum values."""
            return sum(values)

        result = await TestClient(server).call_tool("total", {"values": ["nope"] * 50_000})
        text = result[0].text
        assert len(text) < MAX_ERROR_CHARS, len(text)
        err = _error(text)
        assert err["code"] == "VALIDATION_ERROR"
        assert err["details"]["omitted_errors"] > 0
