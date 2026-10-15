"""Requests for methods the MCP SDK does not know get ``-32601``, quietly.

Regression (v1.3.3): an mcp 2.x client (current Pydantic AI, OpenAI Agents
SDK) opens every connection with ``server/discover``. The 1.x SDK a Promptise
server runs on failed to validate it and logged, once per connection::

    WARNING:root:Failed to validate request: 31 validation errors for ClientRequest
    ... (94 lines)

and answered ``-32602 Invalid params``. The client then fell back to
``initialize``. Now the server answers ``-32601 Method not found`` (the
JSON-RPC answer for an unknown method) without the warning; the 2.x client
falls back exactly as before, and mcp 1.x clients are unaffected.

The stdio test starts the server as a subprocess with this checkout on
``PYTHONPATH`` and speaks newline-delimited JSON-RPC to it, as a 2.x client
does.  No mcp 2.x install is needed.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import sys
import textwrap
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp.shared.message import SessionMessage
from mcp.types import METHOD_NOT_FOUND, JSONRPCMessage, JSONRPCNotification, JSONRPCRequest

from promptise.mcp.server import MCPServer

SRC = Path(__file__).resolve().parents[1] / "src"

# What mcp 2.3's client sends first (params trimmed to what matters here)
DISCOVER: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 0,
    "method": "server/discover",
    "params": {"_meta": {"io.modelcontextprotocol/protocolVersion": "2026-07-28"}},
}
INITIALIZE: dict[str, Any] = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2025-06-18",
        "capabilities": {},
        "clientInfo": {"name": "test", "version": "0"},
    },
}
INITIALIZED = {"jsonrpc": "2.0", "method": "notifications/initialized"}
CALL = {
    "jsonrpc": "2.0",
    "id": 2,
    "method": "tools/call",
    "params": {"name": "add", "arguments": {"a": 2, "b": 3}},
}


def _server() -> MCPServer:
    server = MCPServer("interop")

    @server.tool()
    async def add(a: int, b: int) -> int:
        """Add two numbers."""
        return a + b

    return server


def _msg(payload: dict[str, Any]) -> SessionMessage:
    return SessionMessage(message=JSONRPCMessage.model_validate(payload))


@pytest.mark.asyncio
async def test_unknown_request_gets_method_not_found_without_a_warning(
    caplog: pytest.LogCaptureFixture,
) -> None:
    server = _server()
    ll = server._build_lowlevel_server()
    to_server_send, to_server_recv = anyio.create_memory_object_stream[Any](10)
    from_server_send, from_server_recv = anyio.create_memory_object_stream[Any](10)

    async def reply() -> dict[str, Any]:
        with anyio.fail_after(5):
            message = await from_server_recv.receive()
        return message.message.model_dump(by_alias=True, mode="json", exclude_none=True)

    caplog.set_level(logging.DEBUG)
    async with anyio.create_task_group() as tg:
        tg.start_soon(ll.run, to_server_recv, from_server_send, ll.create_initialization_options())
        await to_server_send.send(_msg(DISCOVER))
        discovered = await reply()
        assert discovered["id"] == 0
        assert discovered["error"]["code"] == METHOD_NOT_FOUND
        assert discovered["error"]["data"] == "server/discover"

        # An unknown notification is ignored; the session carries on
        await to_server_send.send(_msg({"jsonrpc": "2.0", "method": "notifications/nope"}))

        await to_server_send.send(_msg(INITIALIZE))
        initialized = await reply()
        assert initialized["result"]["serverInfo"]["name"] == "interop"
        await to_server_send.send(_msg(INITIALIZED))
        await to_server_send.send(_msg(CALL))
        called = await reply()
        assert called["id"] == 2
        assert called["result"]["content"][0]["text"] == "5"
        await to_server_send.aclose()
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert warnings == [], [r.getMessage()[:200] for r in warnings]


def test_known_methods_pass_through() -> None:
    from promptise.mcp.server._app import _unknown_method_reply

    for payload in (INITIALIZE, CALL, {"jsonrpc": "2.0", "id": 9, "method": "ping"}):
        assert _unknown_method_reply(_msg(payload)) is None
    assert _unknown_method_reply(_msg(INITIALIZED)) is None
    # Responses and non-messages are never touched
    assert _unknown_method_reply(_msg({"jsonrpc": "2.0", "id": 3, "result": {}})) is None
    assert _unknown_method_reply(Exception("transport error")) is None
    assert isinstance(_msg(DISCOVER).message.root, JSONRPCRequest)
    assert isinstance(_msg(INITIALIZED).message.root, JSONRPCNotification)


@pytest.mark.asyncio
async def test_stdio_server_answers_discover_quietly(tmp_path: Path) -> None:
    script = tmp_path / "server.py"
    script.write_text(
        textwrap.dedent(
            """
            from promptise.mcp.server import MCPServer

            server = MCPServer("interop")

            @server.tool()
            async def add(a: int, b: int) -> int:
                \"\"\"Add two numbers.\"\"\"
                return a + b

            server.run(transport="stdio")
            """
        )
    )
    env = {**os.environ, "PYTHONPATH": str(SRC)}
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        str(script),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=env,
    )
    assert proc.stdin is not None and proc.stdout is not None

    async def send(payload: dict[str, Any]) -> None:
        assert proc.stdin is not None
        proc.stdin.write((json.dumps(payload) + "\n").encode())
        await proc.stdin.drain()

    async def receive() -> dict[str, Any]:
        assert proc.stdout is not None
        line = await asyncio.wait_for(proc.stdout.readline(), 30)
        return json.loads(line)

    try:
        await send(DISCOVER)
        discovered = await receive()
        assert discovered["error"]["code"] == METHOD_NOT_FOUND
        await send(INITIALIZE)
        assert "result" in await receive()
        await send(INITIALIZED)
        await send(CALL)
        called = await receive()
        assert called["result"]["content"][0]["text"] == "5"
    finally:
        proc.stdin.close()
        try:
            _, stderr = await asyncio.wait_for(proc.communicate(), 30)
        except asyncio.TimeoutError:
            proc.kill()
            _, stderr = await proc.communicate()
    log = stderr.decode()
    assert "Failed to validate request" not in log, log[:500]
    assert "validation errors" not in log, log[:500]
