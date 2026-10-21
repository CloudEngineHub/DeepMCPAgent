"""MCP fixes in 1.5.2 (both SDK lines; the mcp 1.x CI job runs this file too).

- A server's own JSON-RPC error with code -32000 is the server's error, not a
  lost connection. 1.5.1's client read every -32000 as the SDK's "Connection
  closed", and a Promptise server answers a ``ToolError`` raised in a resource
  or prompt handler with -32000, so reading a missing resource reported
  ``Connection to ... was lost (MCPError: There is no handbook page ...)``
  although the connection was fine.
- A stdio server answers the requests still running when the client closes
  stdin, waiting at most ``shutdown_timeout`` seconds. Both SDKs cancel them
  when the read side ends, so a client that sent a ``tools/call`` and closed
  stdin at once usually got no answer.

The stdio tests start the server as a subprocess with this checkout on
``PYTHONPATH``.
"""

from __future__ import annotations

import asyncio
import json
import os
import sys
import textwrap
import time
from pathlib import Path
from typing import Any

import anyio
import pytest
from mcp.shared.message import SessionMessage

from promptise.mcp._compat import MCP_V2, McpError, field, mcp_error
from promptise.mcp.client import MCPClient, MCPClientError

SRC = Path(__file__).resolve().parents[1] / "src"
ENV = {**os.environ, "PYTHONPATH": str(SRC)}
TIMEOUT = 30.0

HANDBOOK_SERVER = """
from promptise.mcp.server import MCPServer, ToolError

server = MCPServer("handbook")
PAGES = {"refunds": "# Refund policy"}


@server.resource_template("docs://pages/{slug}", mime_type="text/markdown")
async def docs_page(slug: str) -> str:
    \"\"\"One handbook page.\"\"\"
    if slug not in PAGES:
        raise ToolError(f"There is no handbook page '{slug}'.")
    return PAGES[slug]


@server.prompt()
async def answer(question: str) -> str:
    \"\"\"Answer a question.\"\"\"
    raise ToolError("No answer for that.")


@server.tool()
async def lookup(slug: str) -> str:
    \"\"\"Look a page up.\"\"\"
    if slug not in PAGES:
        raise ToolError(f"There is no handbook page '{slug}'.")
    return PAGES[slug]


server.run(transport="stdio")
"""


def _write(tmp_path: Path, source: str) -> str:
    script = tmp_path / "server.py"
    script.write_text(textwrap.dedent(source))
    return str(script)


# ── 1. A server's -32000 error is not a lost connection ───────────────


async def test_server_errors_over_stdio_are_the_servers_errors(tmp_path: Path) -> None:
    script = _write(tmp_path, HANDBOOK_SERVER)
    client = MCPClient(transport="stdio", command=sys.executable, args=[script], env=ENV)
    async with client:
        with pytest.raises(MCPClientError) as info:
            await asyncio.wait_for(client.read_resource("docs://pages/holidays"), TIMEOUT)
        assert str(info.value) == (
            "Failed to read resource 'docs://pages/holidays': There is no handbook page 'holidays'."
        )
        with pytest.raises(MCPClientError) as info:
            await asyncio.wait_for(client.get_prompt("answer", {"question": "?"}), TIMEOUT)
        assert str(info.value) == "Failed to get prompt 'answer': No answer for that."
        # A tool's ToolError is an error result, not a JSON-RPC error.
        result = await asyncio.wait_for(client.call_tool("lookup", {"slug": "x"}), TIMEOUT)
        assert field(result, "is_error") is True
        # The connection is fine: the same session keeps working.
        assert client.session_generation == 1
        page = await asyncio.wait_for(client.read_resource("docs://pages/refunds"), TIMEOUT)
        assert "Refund policy" in str(page)
        result = await asyncio.wait_for(client.call_tool("lookup", {"slug": "refunds"}), TIMEOUT)
        assert field(result, "is_error") is False


class TestServerErrorIsNotAClosedConnection:
    """``_on_current_session`` passes a server's error on, whatever its code.

    The SDK's own ``Connection closed`` is still a lost connection (see
    ``TestTransportClosedUnderACall`` in test_mcp_client_connection_errors.py).
    """

    @staticmethod
    def _client() -> tuple[MCPClient, asyncio.Event]:
        client = MCPClient(url="http://127.0.0.1:9/mcp", auto_reconnect=False)
        stop = asyncio.Event()
        client._runner = asyncio.create_task(stop.wait())  # a live session
        client._closing = asyncio.Event()
        return client, stop

    @pytest.mark.parametrize(
        ("message", "data"),
        [
            ("There is no handbook page 'holidays'.", {"code": "TOOL_ERROR"}),
            ("There is no handbook page 'holidays'.", None),
            # The SDK's message, but with the data a server's error carries.
            ("Connection closed", {"code": "TOOL_ERROR"}),
        ],
    )
    async def test_minus_32000_from_the_server_is_raised_as_is(
        self, message: str, data: dict[str, Any] | None
    ) -> None:
        client, stop = self._client()
        error = mcp_error(-32000, message, data)

        async def request() -> None:
            raise error

        try:
            with pytest.raises(McpError) as info:
                await client._on_current_session(request())
            assert info.value is error
        finally:
            stop.set()
            assert client._runner is not None
            await client._runner


# ── 2. stdin EOF: running requests are answered first ─────────────────

SLOW_SERVER = """
import asyncio
import sys

from promptise.mcp.server import MCPServer

server = MCPServer("slow", shutdown_timeout=float(sys.argv[1]))


@server.tool()
async def wait(seconds: float) -> str:
    \"\"\"Sleep, then answer.\"\"\"
    await asyncio.sleep(seconds)
    return f"waited {seconds}"


server.run(transport="stdio")
"""


def _requests(seconds: float) -> bytes:
    """initialize, initialized and a tools/call, as one write (then EOF)."""
    messages: list[dict[str, Any]] = [
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "by-hand", "version": "0"},
            },
        },
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "wait", "arguments": {"seconds": seconds}},
        },
    ]
    return "".join(json.dumps(m) + "\n" for m in messages).encode()


async def _pipe_and_close(
    tmp_path: Path, *, seconds: float, shutdown_timeout: float
) -> tuple[dict[Any, dict[str, Any]], int, float, str]:
    """Write the requests, close stdin, and read everything until the server exits."""
    script = _write(tmp_path, SLOW_SERVER)
    proc = await asyncio.create_subprocess_exec(
        sys.executable,
        script,
        str(shutdown_timeout),
        stdin=asyncio.subprocess.PIPE,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
        env=ENV,
    )
    try:
        stdout, stderr = await asyncio.wait_for(proc.communicate(_requests(seconds)), 60)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.communicate()
        raise AssertionError("the stdio server did not exit after stdin closed") from None
    finished = time.monotonic()
    answers = {m["id"]: m for m in map(json.loads, stdout.decode().splitlines()) if "id" in m}
    assert proc.returncode is not None
    return answers, proc.returncode, finished, stderr.decode()


async def test_stdio_server_answers_a_running_call_after_stdin_closes(tmp_path: Path) -> None:
    answers, returncode, _, stderr = await _pipe_and_close(
        tmp_path, seconds=0.3, shutdown_timeout=30
    )
    assert returncode == 0, stderr[-2000:]
    assert "result" in answers[1]
    assert answers.get(2, {}).get("result", {}).get("content") == [
        {"type": "text", "text": "waited 0.3"}
    ], (answers, stderr[-2000:])
    assert "Traceback" not in stderr, stderr[-2000:]


async def test_stdio_server_waits_at_most_shutdown_timeout(tmp_path: Path) -> None:
    started = time.monotonic()
    answers, returncode, finished, stderr = await _pipe_and_close(
        tmp_path, seconds=120, shutdown_timeout=0.5
    )
    assert returncode == 0, stderr[-2000:]
    assert finished - started < 20  # interpreter start-up, plus the 0.5 s bound
    assert "result" in answers[1]
    assert "result" not in answers.get(2, {})  # never answered with a result
    assert "still running after 0.5s (shutdown_timeout)" in stderr


def _msg(payload: dict[str, Any]) -> SessionMessage:
    if MCP_V2:
        from mcp.types import jsonrpc_message_adapter

        return SessionMessage(message=jsonrpc_message_adapter.validate_python(payload))
    from mcp.types import JSONRPCMessage

    return SessionMessage(message=JSONRPCMessage.model_validate(payload))


async def test_drain_stops_waiting_when_the_server_asks_the_client() -> None:
    """After EOF the client can't answer an elicitation: don't wait for it."""
    from promptise.mcp.server._transport import _StdinDrain

    drain = _StdinDrain(timeout=None)  # no bound: only the request ends the wait
    stdin_send, stdin_recv = anyio.create_memory_object_stream[Any](10)
    to_session, session_in = anyio.create_memory_object_stream[Any](10)
    session_out, from_session = anyio.create_memory_object_stream[Any](10)
    stdout_send, stdout_recv = anyio.create_memory_object_stream[Any](10)
    call = {"jsonrpc": "2.0", "id": 7, "method": "tools/call", "params": {"name": "x"}}
    await stdin_send.send(_msg(call))
    await stdin_send.aclose()  # EOF right after the call
    async with anyio.create_task_group() as tg:
        tg.start_soon(drain.forward_out, from_session, stdout_send)
        with anyio.fail_after(5):
            async with anyio.create_task_group() as reading:
                reading.start_soon(drain.forward_in, stdin_recv, to_session)
                assert (await session_in.receive()).message == _msg(call).message
                # The handler asks the client; the drain gives up at once.
                ask = {"jsonrpc": "2.0", "id": 0, "method": "elicitation/create", "params": {}}
                await session_out.send(_msg(ask))
        # forward_in returned, so the session's read side ended.
        with pytest.raises(anyio.EndOfStream):
            await session_in.receive()
        await session_out.aclose()
    assert stdout_recv.receive_nowait().message == _msg(ask).message
