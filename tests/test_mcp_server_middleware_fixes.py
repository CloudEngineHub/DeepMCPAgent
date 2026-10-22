"""StructuredLoggingMiddleware(include_args=True), WebhookMiddleware delivery,
and the APIKeyAuth header name (provider and transport gate)."""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import Any
from unittest.mock import patch

import pytest

from promptise.mcp.server import (
    APIKeyAuth,
    AuthMiddleware,
    MCPServer,
    StructuredLoggingMiddleware,
    TestClient,
    WebhookMiddleware,
)
from promptise.mcp.server._context import RequestContext

# ---------------------------------------------------------------------------
# StructuredLoggingMiddleware(include_args=True)
# ---------------------------------------------------------------------------


def _logged_entries(caplog: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    entries = []
    for record in caplog.records:
        try:
            entries.append(json.loads(record.getMessage()))
        except ValueError:
            continue
    return entries


def _logging_server(include_args: bool) -> MCPServer:
    server = MCPServer("logs")
    server.add_middleware(StructuredLoggingMiddleware(include_args=include_args))

    @server.tool()
    async def sign_in(user: str, password: str, note: str = "") -> str:
        return "ok"

    return server


async def test_include_args_logs_redacted_arguments(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="promptise.server")
    await TestClient(_logging_server(True)).call_tool(
        "sign_in",
        {"user": "ada", "password": "hunter2", "note": "mail me at ada@example.com"},
    )
    (start,) = [e for e in _logged_entries(caplog) if e.get("event") == "tool_call_start"]
    assert start["args"]["user"] == "ada"
    assert start["args"]["password"] == "[REDACTED]"
    assert "ada@example.com" not in start["args"]["note"]
    assert "hunter2" not in caplog.text


async def test_arguments_are_not_logged_by_default(caplog: pytest.LogCaptureFixture) -> None:
    caplog.set_level(logging.INFO, logger="promptise.server")
    await TestClient(_logging_server(False)).call_tool("sign_in", {"user": "ada", "password": "x"})
    entries = [e for e in _logged_entries(caplog) if e.get("event", "").startswith("tool_call")]
    assert entries and all("args" not in e for e in entries)


# ---------------------------------------------------------------------------
# WebhookMiddleware
# ---------------------------------------------------------------------------


class _SlowClient:
    """Stands in for httpx.AsyncClient: every POST takes *delay* seconds."""

    posts: list[dict[str, Any]] = []
    in_flight = 0
    max_in_flight = 0
    delay = 0.3

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        pass

    async def __aenter__(self) -> _SlowClient:
        return self

    async def __aexit__(self, *exc: Any) -> None:
        return None

    async def post(self, url: str, *, json: Any, headers: Any) -> None:
        cls = type(self)
        cls.in_flight += 1
        cls.max_in_flight = max(cls.max_in_flight, cls.in_flight)
        try:
            await asyncio.sleep(cls.delay)
            cls.posts.append(json)
        finally:
            cls.in_flight -= 1


@pytest.fixture
def slow_client() -> Any:
    _SlowClient.posts = []
    _SlowClient.in_flight = 0
    _SlowClient.max_in_flight = 0
    with patch("httpx.AsyncClient", _SlowClient):
        yield _SlowClient


def _webhook_server(hook: WebhookMiddleware) -> MCPServer:
    server = MCPServer("hooks")
    server.add_middleware(hook)

    @server.tool()
    async def fast() -> str:
        return "ok"

    return server


async def test_webhook_delivery_does_not_delay_the_call(slow_client: Any) -> None:
    hook = WebhookMiddleware("https://hooks.example.com/x")
    client = TestClient(_webhook_server(hook))
    started = time.perf_counter()
    result = await client.call_tool("fast", {})
    elapsed = time.perf_counter() - started
    assert result[0].text == "ok"
    assert elapsed < slow_client.delay
    await hook.flush(timeout=5)
    assert {p["event"] for p in slow_client.posts} == {"tool.call", "tool.success"}


async def test_webhook_concurrency_is_bounded(slow_client: Any) -> None:
    slow_client.delay = 0.05
    hook = WebhookMiddleware(
        "https://hooks.example.com/x", events={"tool.success"}, max_concurrency=2
    )
    client = TestClient(_webhook_server(hook))
    for _ in range(10):
        await client.call_tool("fast", {})
    await hook.flush(timeout=5)
    assert len(slow_client.posts) == 10
    assert slow_client.max_in_flight == 2


async def test_full_queue_drops_events(slow_client: Any, caplog: pytest.LogCaptureFixture) -> None:
    hook = WebhookMiddleware(
        "https://hooks.example.com/x", events={"tool.success"}, max_queue=2, max_concurrency=1
    )
    client = TestClient(_webhook_server(hook))
    for _ in range(6):
        await client.call_tool("fast", {})
    assert hook.dropped >= 3
    assert "Webhook queue full" in caplog.text
    await hook.aclose(timeout=5)


async def test_sent_history_is_bounded(slow_client: Any) -> None:
    slow_client.delay = 0
    hook = WebhookMiddleware("https://hooks.example.com/x", events={"tool.success"}, history=3)
    client = TestClient(_webhook_server(hook))
    for _ in range(5):
        await client.call_tool("fast", {})
    await hook.flush(timeout=5)
    assert len(hook.sent) == 3


async def test_aclose_stops_delivery_tasks(slow_client: Any) -> None:
    hook = WebhookMiddleware("https://hooks.example.com/x")
    await TestClient(_webhook_server(hook)).call_tool("fast", {})
    await hook.aclose(timeout=5)
    assert len(slow_client.posts) == 2
    assert not hook._workers


# ---------------------------------------------------------------------------
# APIKeyAuth(header=...)
# ---------------------------------------------------------------------------


async def test_custom_header_name_is_case_insensitive() -> None:
    server = MCPServer("keys")
    server.add_middleware(AuthMiddleware(APIKeyAuth({"k1": "agent-1"}, header="X-Custom-Key")))

    @server.tool(auth=True)
    async def whoami(ctx: RequestContext) -> str:
        return ctx.client_id or ""

    # Over HTTP the header arrives lower-cased
    lower = TestClient(server, meta={"x-custom-key": "k1"})
    assert (await lower.call_tool("whoami", {}))[0].text == "agent-1"
    # Meta set by hand may keep the original case
    mixed = TestClient(server, meta={"X-Custom-Key": "k1"})
    assert (await mixed.call_tool("whoami", {}))[0].text == "agent-1"
    wrong = TestClient(server, meta={"x-api-key": "k1"})
    assert "AUTHENTICATION_ERROR" in (await wrong.call_tool("whoami", {}))[0].text


async def _gate_status(server: MCPServer, headers: list[tuple[bytes, bytes]]) -> int:
    from promptise.mcp.server._transport import _AuthGateASGI

    async def inner(scope: Any, receive: Any, send: Any) -> None:
        await send({"type": "http.response.start", "status": 200, "headers": []})

    gate = _AuthGateASGI(inner, server._transport_auth_gate())
    sent: list[dict[str, Any]] = []

    async def send(message: dict[str, Any]) -> None:
        sent.append(message)

    await gate({"type": "http", "path": "/mcp", "headers": headers}, None, send)
    return sent[0]["status"]


async def test_transport_gate_reads_the_configured_header() -> None:
    server = MCPServer("keys", require_auth=True)
    server.add_middleware(AuthMiddleware(APIKeyAuth({"k1": "agent-1"}, header="X-Custom-Key")))
    assert await _gate_status(server, [(b"x-custom-key", b"k1")]) == 200
    assert await _gate_status(server, [(b"x-custom-key", b"nope")]) == 401
    assert await _gate_status(server, [(b"x-api-key", b"k1")]) == 401


async def test_transport_gate_default_header_unchanged() -> None:
    server = MCPServer("keys", require_auth=True)
    server.add_middleware(AuthMiddleware(APIKeyAuth({"k1": "agent-1"})))
    assert await _gate_status(server, [(b"x-api-key", b"k1")]) == 200
    assert await _gate_status(server, []) == 401
