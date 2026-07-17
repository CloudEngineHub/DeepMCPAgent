---
title: "How to Test MCP Servers Without a Live Server"
description: "Most MCP testing advice means booting a server and firing HTTP at it — slow and flaky in CI. This shows the full request pipeline (validation, DI, guards…"
keywords: "testing mcp servers, mcp server testing, test mcp tools, mcp testclient, pytest mcp server"
date: 2026-07-16
slug: testing-mcp-servers
categories:
  - MCP
---

# How to Test MCP Servers Without a Live Server

Testing MCP servers usually means starting a process, waiting for it to bind a port, and firing HTTP or stdio requests at it from a separate test runner. That works, but it is slow, it is flaky in CI, and it turns a two-line assertion into a fixture that manages subprocesses and sockets. This post shows the alternative: run the complete request pipeline — validation, dependency injection, guards, middleware, and your handler — entirely in-process with Promptise Foundry's `TestClient`, so your tests stay as fast and deterministic as plain function calls. By the end you will be able to test tools, guards, middleware, and error handling without ever opening a network connection.

## Why booting a server is the wrong default for tests

A live-server test has to solve problems that have nothing to do with your tool logic. You pick a free port, start the transport, poll until it is ready, run your assertions, and tear it all down — and every one of those steps is a chance for a race condition. Under parallel CI workers, port collisions and startup timeouts produce the exact kind of intermittent red build that erodes trust in the suite.

The deeper problem is that a raw HTTP call skips nothing and tests nothing precisely. If you want to assert that a guard denied an unauthenticated request, you still have to stand up the whole transport to reach the guard. If you are new to the protocol itself, the companion post [What Is MCP? Model Context Protocol Explained](what-is-mcp.md) covers the request/response model these tests exercise.

What you actually want is to invoke the same pipeline the transport would, minus the socket. That is what `TestClient` gives you.

## What the in-process TestClient actually runs

The point of `TestClient` is fidelity: it does not shortcut to your handler. Every `call_tool` reproduces the exact stages the real MCP transport runs, in order:

1. **Input validation** against the tool's auto-generated Pydantic model
2. **Dependency injection** — `Depends(...)` parameters resolved
3. **Context injection** — parameters typed as `RequestContext` populated
4. **Guard checks** — `RequireAuth`, `HasRole`, and any custom guards evaluated
5. **Middleware chain** — server- and router-level middleware in registration order
6. **Handler invocation** — your actual tool function
7. **Result serialization** — the return value converted to MCP `TextContent`
8. **Background tasks** — anything scheduled during the call is executed
9. **Error handling** — `MCPError` subclasses serialized to structured JSON

Because the same pipeline runs, a passing in-process test is a real signal about production behavior, not an approximation. The full behavior of each stage is documented in [MCP server testing](../../mcp/server/testing.md).

## Your first pytest MCP server test

Here is a complete, runnable example. Define a server exactly as you would ship it, then drive it with `TestClient`. No transport, no fixtures managing subprocesses.

```python
import pytest
from promptise.mcp.server import MCPServer
from promptise.mcp.server.testing import TestClient

server = MCPServer(name="calculator")


@server.tool()
async def add(a: int, b: int) -> int:
    """Add two numbers."""
    return a + b


@pytest.mark.asyncio
async def test_add_returns_sum():
    client = TestClient(server)
    result = await client.call_tool("add", {"a": 2, "b": 3})
    # call_tool returns a list[TextContent], exactly like the real server
    assert result[0].text == "5"


@pytest.mark.asyncio
async def test_validation_rejects_bad_types():
    client = TestClient(server)
    result = await client.call_tool("add", {"a": "not-a-number", "b": 3})
    # Validation runs before your handler, so this never reaches add()
    assert "error" in result[0].text.lower()
```

Two things are worth calling out. First, the schema for `add` is generated from its type hints, so the second test proves that validation rejects a bad payload before your code runs — you get that coverage for free. Second, `call_tool` returns a `list[TextContent]` just like the wire protocol, so your assertions match what a real client would receive. If you have not built a server yet, [How to Build an MCP Server in Python](mcp-server-python.md) walks through the decorators, and the [building servers guide](../../mcp/server/building-servers.md) is the full reference.

## Testing guards and auth without a live server

The highest-value tests are usually the security ones, and they are exactly what a live-server setup makes painful. With `TestClient` you simulate request metadata — the equivalent of HTTP headers — by passing a `meta` dict, so you can assert both the denied and the authorized path in the same test module.

```python
import jwt as pyjwt
import pytest
from promptise.mcp.server import MCPServer, AuthMiddleware, JWTAuth, RequireAuth
from promptise.mcp.server.testing import TestClient

server = MCPServer(name="secure")
server.add_middleware(AuthMiddleware(JWTAuth(secret="test-secret")))


@server.tool(guards=[RequireAuth()])
async def account_balance() -> str:
    """Return a protected value."""
    return "1234.56"


@pytest.mark.asyncio
async def test_denied_without_token():
    client = TestClient(server)
    result = await client.call_tool("account_balance", {})
    # The guard blocks the call before the handler runs
    assert "ACCESS_DENIED" in result[0].text


@pytest.mark.asyncio
async def test_allowed_with_valid_token():
    token = pyjwt.encode({"sub": "alice"}, "test-secret", algorithm="HS256")
    client = TestClient(server, meta={"authorization": f"Bearer {token}"})
    result = await client.call_tool("account_balance", {})
    assert result[0].text == "1234.56"
```

Because `AuthMiddleware` and the `RequireAuth` guard run through the same pipeline the transport uses, these two tests give you real confidence that your authentication is wired correctly. The same pattern extends to `HasRole` and `HasAllRoles` — encode the roles into the test token and assert the boundary. The full set of providers and guards is covered in [auth and security](../../mcp/server/auth-security.md).

## Testing middleware, errors, and tool discovery

`TestClient` is not limited to happy-path tool calls. Middleware runs in registration order, so you can assert timeout and logging behavior in-process:

```python
from promptise.mcp.server import MCPServer, TimeoutMiddleware
from promptise.mcp.server.testing import TestClient

server = MCPServer(name="ops")
server.add_middleware(TimeoutMiddleware(default_timeout=5.0))
```

Error handling is equally testable. When a tool raises or is missing, the client returns structured error JSON instead of raising — matching the real server — so you assert on the error code:

```python
import json
import pytest
from promptise.mcp.server.testing import TestClient


@pytest.mark.asyncio
async def test_unknown_tool_returns_structured_error():
    client = TestClient(server)
    result = await client.call_tool("does_not_exist", {})
    error = json.loads(result[0].text)
    assert error["error"]["code"] == "TOOL_NOT_FOUND"
```

And discovery — what clients actually see — is a one-liner. `list_tools()` returns the same MCP `Tool` objects the transport advertises, which is the right way to test mcp tools appear (or stay hidden behind visibility transforms) as you intend:

```python
tools = await client.list_tools()
assert "add" in [t.name for t in tools]
```

The client also exposes `read_resource`, `list_resources`, `get_prompt`, and `list_prompts`, so resources and prompt templates get the same in-process treatment as tools.

## When a live server test is the better fit

`TestClient` is the right default for unit and integration tests of your server logic, but it is honest to name its boundary. It deliberately replaces the transport, so it does not exercise the wire itself. Prefer a real server, started with `promptise serve myapp:server --transport http --port 8080`, when you need to verify things that live below the pipeline:

- **Transport and framing** — actual HTTP status codes, SSE streaming chunks, or stdio message boundaries
- **CORS and network-level auth gates** enforced at the transport
- **Interoperability** with a specific third-party MCP client or an end-to-end smoke test before release

A healthy suite uses both: `TestClient` for the fast, deterministic bulk of your `mcp server testing`, and a small number of live-transport tests as a final smoke check. Reach for the live server when the transport is the thing under test — not when your tool logic is.

## Frequently asked questions

### Do I need pytest to use TestClient?

No. `TestClient` is a plain async class — `await client.call_tool(...)` works in any async test runner or even a standalone script. pytest with `pytest-asyncio` is the common choice because the `@pytest.mark.asyncio` decorator and fixtures make an async MCP server test suite tidy, but nothing in the client depends on it.

### Does TestClient run my middleware and guards, or skip them?

It runs them. The whole point is fidelity: validation, DI, context injection, guards, the middleware chain, your handler, serialization, background tasks, and error handling all execute in the same order as the real transport. The only thing it omits is the network layer itself.

### How do I simulate an authenticated request in a test?

Pass a `meta` dict when you construct the client, for example `TestClient(server, meta={"authorization": "Bearer <token>"})`. That metadata is copied into every `RequestContext` the client creates, so your `AuthMiddleware` and guards see it exactly as they would a real header. Encode a JWT with your test secret to exercise the authorized path.

## Next steps

Add one `TestClient` test today and cover your guards and middleware without ever booting a server — it is the fastest reliability win you can make to an MCP codebase. Start from the [Quick Start](../../getting-started/quickstart.md) to scaffold a server, then work through the [MCP server testing guide](../../mcp/server/testing.md) to cover resources, prompts, and error paths.
