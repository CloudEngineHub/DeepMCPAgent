"""One ``MCPClient`` reuses its HTTP connections, over REAL servers on loopback ports.

The MCP SDK stops reading the event stream that answers a request as soon as
it has the JSON-RPC response, and closes it before the end of the stream.  An
HTTP/1.1 connection closed in the middle of a response cannot go back to the
pool, so every request used to open a new TCP connection and leave a local
port in ``TIME_WAIT``: a busy client ran out of ports ("Can't assign requested
address").  The client now reads the end of the stream when it is closed.

The server records the client port of every request, one per TCP connection:

* many tool calls on one client reuse a pooled connection, on both transports;
* a connection the server closed while idle is replaced on the next call,
  and the MCP session carries on;
* per-caller sessions never share a connection, so each connection carries
  one caller's token.

The stream wrapper itself is tested without a network: a stream that does
not end, or goes on for too long, is closed as before.
"""

from __future__ import annotations

import asyncio
import functools
import warnings
from collections import defaultdict
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import pytest
from _live_server import serve_asgi

from promptise.mcp._compat import MCP_V2
from promptise.mcp.client import MCPClient, MCPMultiClient
from promptise.mcp.client import _client as client_module
from promptise.mcp.server import MCPServer

TIMEOUT = 10.0
CALLS = 30
#: The connections the tool calls of one client may use: one, plus another
#: when a call starts while the previous answer is still being read to its
#: end (a slow machine can make that two).  Before the fix, every call took
#: a new one.
MAX_CALL_CONNECTIONS = 3


@dataclass(frozen=True)
class _Request:
    connection: int  # the client's port: one per TCP connection
    method: str
    authorization: str


def _echo_server() -> MCPServer:
    server = MCPServer("echo")

    @server.tool()
    async def echo(text: str) -> str:
        """Return the text."""
        return text

    return server


@asynccontextmanager
async def _serve(
    server: MCPServer, *, transport: str = "http", **config: Any
) -> AsyncIterator[tuple[int, list[_Request]]]:
    """Serve *server* on loopback; yield its port and the MCP requests it receives."""
    requests: list[_Request] = []
    app = server.asgi_app(transport)

    async def recording(scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] == "http" and not scope["path"].startswith("/health"):
            headers = dict(scope["headers"])
            requests.append(
                _Request(
                    connection=scope["client"][1],
                    method=scope["method"],
                    authorization=headers.get(b"authorization", b"").decode(),
                )
            )
        await app(scope, receive, send)

    async with serve_asgi(recording, **config) as port:
        yield port, requests


def _url(port: int, transport: str) -> str:
    return f"http://127.0.0.1:{port}/{'sse' if transport == 'sse' else 'mcp'}"


def _posts(requests: list[_Request]) -> list[_Request]:
    return [r for r in requests if r.method == "POST"]


async def _echo(client: MCPClient, text: str) -> None:
    result = await asyncio.wait_for(client.call_tool("echo", {"text": text}), TIMEOUT)
    assert result.content[0].text == text


# =====================================================================
# One client, many calls
# =====================================================================


class TestOneClientReusesItsConnections:
    @pytest.mark.parametrize(
        ("transport", "provider"),
        [("http", False), ("http", True), ("sse", False)],
        ids=["http", "http-token-provider", "sse"],
    )
    async def test_tool_calls_reuse_pooled_connections(self, transport, provider):
        kwargs: dict[str, Any] = {}
        if provider:  # the client then brings its own httpx transport
            kwargs["bearer_token_provider"] = lambda force_refresh: "agent-token"
        async with _serve(_echo_server(), transport=transport) as (port, requests):
            async with MCPClient(
                url=_url(port, transport), transport=transport, **kwargs
            ) as client:
                await _echo(client, "warm-up")
                start = len(_posts(requests))
                for i in range(CALLS):
                    await _echo(client, str(i))
                calls = _posts(requests)[start:]
        assert len(calls) == CALLS
        assert len({r.connection for r in calls}) <= MAX_CALL_CONNECTIONS
        # The handshake and the event stream from the server come on top.
        assert len({r.connection for r in requests}) <= MAX_CALL_CONNECTIONS + 3

    @pytest.mark.skipif(MCP_V2, reason="mcp 1.x only: 2.x has no streamablehttp_client")
    async def test_an_older_sdk_api_reuses_connections_too(self, monkeypatch):
        """SDKs before ``streamable_http_client`` take the client through a factory."""
        import types

        import mcp.client
        from mcp.client import streamable_http

        original = streamable_http.streamablehttp_client
        factories: list[Any] = []

        @functools.wraps(original)
        def old_api(*args: Any, **kwargs: Any) -> Any:
            factories.append(kwargs.get("httpx_client_factory"))
            return original(*args, **kwargs)

        monkeypatch.setattr(
            mcp.client, "streamable_http", types.SimpleNamespace(streamablehttp_client=old_api)
        )
        async with _serve(_echo_server()) as (port, requests):
            with warnings.catch_warnings():
                warnings.simplefilter("ignore", DeprecationWarning)
                async with MCPClient(url=_url(port, "http")) as client:
                    await _echo(client, "warm-up")
                    start = len(_posts(requests))
                    for i in range(CALLS):
                        await _echo(client, str(i))
                    calls = _posts(requests)[start:]
        assert factories and factories[0] is not None
        assert len({r.connection for r in calls}) <= MAX_CALL_CONNECTIONS


# =====================================================================
# The server closes a pooled connection
# =====================================================================


class TestClosedConnectionIsReplaced:
    @pytest.mark.parametrize("transport", ["http", "sse"])
    async def test_next_call_opens_a_new_connection_on_the_same_session(self, transport):
        # The server closes connections idle for a second (a keep-alive
        # timeout, as every HTTP server has).
        async with _serve(_echo_server(), transport=transport, timeout_keep_alive=1) as (
            port,
            requests,
        ):
            async with MCPClient(url=_url(port, transport), transport=transport) as client:
                await _echo(client, "before")
                before = _posts(requests)[-1].connection
                await asyncio.sleep(2.0)  # the server has closed it by now
                await _echo(client, "after")
                after = _posts(requests)[-1].connection
                await _echo(client, "again")
                again = _posts(requests)[-1].connection
                assert client.session_generation == 1
        assert after != before
        assert again == after  # the new connection is pooled in turn


# =====================================================================
# Per-caller sessions
# =====================================================================


class TestCallersNeverShareAConnection:
    async def test_each_connection_carries_one_callers_token(self):
        callers = ("alice-token", "bob-token", "carol-token")
        async with _serve(_echo_server()) as (port, requests):
            agent = MCPClient(url=_url(port, "http"), bearer_token="agent-token")
            async with MCPMultiClient({"echo": agent}) as multi:
                await multi.list_tools()
                for _ in range(5):  # interleaved
                    for token in callers:
                        result = await asyncio.wait_for(
                            multi.call_tool("echo", {"text": token}, bearer_token=token), TIMEOUT
                        )
                        assert result.content[0].text == token
                await asyncio.wait_for(  # and concurrent
                    asyncio.gather(
                        *(
                            multi.call_tool("echo", {"text": token}, bearer_token=token)
                            for token in callers
                            for _ in range(4)
                        ),
                        agent.call_tool("echo", {"text": "agent"}),
                    ),
                    TIMEOUT,
                )
        tokens: dict[int, set[str]] = defaultdict(set)
        for request in requests:
            tokens[request.connection].add(request.authorization)
        assert all(len(seen) == 1 for seen in tokens.values()), dict(tokens)
        assert {next(iter(seen)) for seen in tokens.values()} == {
            f"Bearer {token}" for token in ("agent-token", *callers)
        }

    async def test_a_callers_calls_reuse_its_connections(self):
        async with _serve(_echo_server()) as (port, requests):
            agent = MCPClient(url=_url(port, "http"), bearer_token="agent-token")
            async with MCPMultiClient({"echo": agent}) as multi:
                await multi.list_tools()
                for token in ("alice-token", "bob-token"):  # open both sessions
                    await multi.call_tool("echo", {"text": token}, bearer_token=token)
                start = len(_posts(requests))
                for i in range(CALLS // 2):
                    for token in ("alice-token", "bob-token"):
                        await multi.call_tool("echo", {"text": str(i)}, bearer_token=token)
                calls = _posts(requests)[start:]
        for token in ("alice-token", "bob-token"):
            used = {r.connection for r in calls if r.authorization == f"Bearer {token}"}
            assert len(used) <= MAX_CALL_CONNECTIONS


# =====================================================================
# The stream wrapper, without a network
# =====================================================================


class _FakeBody:
    """A response body: *chunks*, then (unless *endless*) the end of the stream."""

    def __init__(self, chunks: list[bytes], *, endless: bool = False) -> None:
        self._chunks = chunks
        self._endless = endless
        self.read: list[bytes] = []
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for chunk in self._chunks:
            self.read.append(chunk)
            yield chunk
        if self._endless:
            await asyncio.Event().wait()

    async def aclose(self) -> None:
        self.closed = True


class TestReusableEventStream:
    async def test_close_reads_the_rest_of_the_stream(self):
        body = _FakeBody([b"event: message\n", b"data: {}\n\n", b""])
        stream = client_module._ReusableEventStream(body)
        async for _chunk in stream:
            break  # the SDK stops at the response
        await stream.aclose()
        assert body.read == [b"event: message\n", b"data: {}\n\n", b""]
        assert body.closed

    async def test_a_stream_that_does_not_end_is_closed_after_the_wait(self, monkeypatch):
        monkeypatch.setattr(client_module, "_SSE_DRAIN_SECONDS", 0.05)
        body = _FakeBody([b"data: {}\n\n"], endless=True)
        stream = client_module._ReusableEventStream(body)
        async for _chunk in stream:
            break
        await asyncio.wait_for(stream.aclose(), TIMEOUT)
        assert body.closed

    async def test_a_long_rest_is_not_read_to_the_end(self, monkeypatch):
        monkeypatch.setattr(client_module, "_SSE_DRAIN_BYTES", 10)
        body = _FakeBody([b"data: {}\n\n"] + [b"x" * 8] * 100)
        stream = client_module._ReusableEventStream(body)
        async for _chunk in stream:
            break
        await stream.aclose()
        assert len(body.read) < 10
        assert body.closed

    async def test_a_read_error_still_closes_the_stream(self):
        class _Broken(_FakeBody):
            async def __aiter__(self) -> AsyncIterator[bytes]:
                yield b"data: {}\n\n"
                raise ConnectionResetError("reset by peer")

        body = _Broken([])
        stream = client_module._ReusableEventStream(body)
        async for _chunk in stream:
            break
        await stream.aclose()
        assert body.closed

    async def test_only_an_event_stream_answering_a_post_is_wrapped(self):
        from promptise.mcp._compat import sdk_httpx

        def response(method: str, content_type: str) -> Any:
            request = sdk_httpx.Request(method, "http://127.0.0.1/mcp")
            return sdk_httpx.Response(
                200, headers={"content-type": content_type}, request=request, content=b""
            )

        answer = response("POST", "text/event-stream")
        await client_module._reuse_connection(answer)
        assert isinstance(answer.stream, client_module._ReusableEventStream)
        for method, content_type in (("GET", "text/event-stream"), ("POST", "application/json")):
            other = response(method, content_type)
            await client_module._reuse_connection(other)
            assert not isinstance(other.stream, client_module._ReusableEventStream)
