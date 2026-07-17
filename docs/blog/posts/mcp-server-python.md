---
title: "How to Build an MCP Server in Python (Tutorial)"
description: "A copy-paste tutorial that gets a typed, schema-validated tool serving over streamable HTTP in minutes — the JSON Schema is generated straight from your…"
keywords: "mcp server python, how to build an mcp server, python mcp server tutorial, mcp tools tutorial, @server.tool decorator"
date: 2026-07-16
slug: mcp-server-python
categories:
  - MCP
---

# How to Build an MCP Server in Python (Tutorial)

Building an MCP server Python developers can actually ship usually stalls on one thing: hand-writing JSON Schema for every tool so clients know how to call it. This tutorial skips that entirely. With Promptise Foundry you decorate a normal Python function with type hints, and the framework generates the schema, validates every call, and serves the tool over stdio, streamable HTTP, or SSE. By the end you'll have a typed tool running, exposed over HTTP, and covered by an in-process test — no schema boilerplate, no network mocking.

<!-- more -->

## What an MCP server is (and why type hints matter)

The Model Context Protocol (MCP) is the standard that lets AI agents discover and call your tools. A server advertises a list of tools; each tool has a name, a description, and a JSON Schema describing its parameters. Agents read that schema to build valid calls. If you're new to the protocol, the [What Is MCP?](../../getting-started/what-is-mcp.md) primer walks through the client/server model in plain terms.

The friction in most raw-SDK tutorials is that *you* write the schema by hand and keep it in sync with your function. Drift between the two is a common source of bugs: the agent sends what the schema promised, but the function expected something else. Promptise removes the second copy — your Python type hints are the single source of truth, and the schema is derived from them at registration time.

## Install Promptise and scaffold the server

Install the package into a virtual environment:

```bash
pip install promptise
```

Create a file called `weather_server.py`. An `MCPServer` is the central object: it owns the tool registry, the middleware chain, and the transport layer.

```python
# weather_server.py
from promptise.mcp.server import MCPServer

server = MCPServer(name="weather-tools", version="1.0.0")
```

That's the whole scaffold. Everything else is registering tools and choosing how to serve them.

## Write your first tool with the @server.tool decorator

Here's the core of the tutorial. Decorate an async function with `@server.tool()`. The function name becomes the tool name, the docstring becomes the description, and the type hints become the JSON Schema:

```python
# weather_server.py
from promptise.mcp.server import MCPServer

server = MCPServer(name="weather-tools", version="1.0.0")


@server.tool()
async def get_weather(city: str, units: str = "celsius") -> dict:
    """Get the current weather for a city."""
    return {"city": city, "temp": 21, "units": units, "conditions": "sunny"}


if __name__ == "__main__":
    server.run()  # stdio transport by default
```

From those type hints, Promptise generates a schema with a required `city` string and an optional `units` string that defaults to `"celsius"`. Every incoming call is validated against it before your function runs — a client that sends `city: 42` gets a structured validation error, and your handler never sees bad input.

For richer inputs, type a parameter as a Pydantic model and the framework produces a nested schema automatically:

```python
from pydantic import BaseModel, Field


class Location(BaseModel):
    city: str = Field(min_length=1, description="City name")
    country: str = Field(description="ISO country code")


@server.tool()
async def forecast(location: Location, days: int = 3) -> list[dict]:
    """Return a multi-day forecast for a location."""
    return [{"day": d, "high": 22, "low": 14} for d in range(1, days + 1)]
```

This is the feature that makes the `@server.tool()` decorator worth reaching for: **you describe the shape once, in Python, and the MCP schema stays generated and in sync.** The [Server Fundamentals reference](../../mcp/server/building-servers.md) covers the full decorator surface — custom names, tags, per-tool `timeout`, `rate_limit`, auth `roles`, and the MCP annotation hints like `destructive_hint`.

## Serve it over stdio, HTTP, or SSE

The same server object runs over three transports without code changes.

**stdio** is the default and is what desktop agent clients (like Claude Desktop) launch directly. Running `python weather_server.py` from the snippet above serves over stdio.

**Streamable HTTP** is what you want for a networked server that multiple agents reach over a URL. Point the CLI at your `server` object:

```bash
promptise serve weather_server:server --transport http --port 8080
```

Your tool is now discoverable at `http://localhost:8080`. Swap `--transport sse` for Server-Sent Events if a client needs it. The transport is a deployment choice, not a rewrite — the tool definitions, validation, and middleware are identical across all three.

Once the server is up, an agent connects as a client and starts calling tools. If you want to see the other side of that handshake, the [MCP Client Tutorial](mcp-client.md) shows how to connect an agent to the server you just built.

## Test your MCP tools without a network

You don't need to start a transport to verify behavior. `TestClient` runs the **full** pipeline in-process — input validation, dependency injection, guards, middleware, and your handler — so tests are as fast as a plain function call and exercise the exact path a real client hits.

```python
# test_weather.py
import asyncio

from promptise.mcp.server.testing import TestClient
from weather_server import server


async def main():
    client = TestClient(server)
    result = await client.call_tool("get_weather", {"city": "Berlin"})
    print(result[0].text)


asyncio.run(main())
```

The call returns MCP `TextContent`, so `result[0].text` holds the serialized output. Because `TestClient` shares the real code path, a passing in-process test means the same call will pass over HTTP. The [testing guide](../../mcp/server/testing.md) shows how to wire this into pytest and assert on validation errors, guard failures, and background tasks.

This is the whole loop for a `python mcp server tutorial`: define a typed tool, serve it, and test it — with the schema generated for you at each step.

## When the raw MCP SDK is a better fit

Promptise is opinionated: it's a framework that brings type-hint schemas, middleware, auth, and testing together. That's the right trade for most production servers, but not every project.

- **You're implementing or debugging the protocol itself.** If you need to inspect raw MCP wire messages or experiment with parts of the spec Promptise hasn't surfaced, the official low-level SDK gives you closer control.
- **You want zero framework dependencies.** For a tiny single-tool script where `pip install promptise` feels heavy, the reference SDK is leaner.
- **You need a language other than Python.** Promptise is Python-first. If your tool lives in TypeScript or Go, use the SDK for that ecosystem.

For a typed, testable, multi-tool server that you'll deploy and maintain, the generated-schema approach here saves real time. For protocol-level spelunking or a throwaway snippet, reach for the raw SDK.

## Frequently asked questions

### Do I have to write JSON Schema for MCP tools?

No. With Promptise, the JSON Schema is generated from your function's type hints — including Pydantic model parameters — at registration time. You write Python types once and the framework keeps the schema in sync, so there's no hand-written schema to drift out of date.

### How do I run the same MCP server over HTTP instead of stdio?

Use the CLI: `promptise serve weather_server:server --transport http --port 8080`. The `module:object` argument points at your `MCPServer` instance. Swap `--transport sse` for Server-Sent Events. Your tool definitions and validation stay identical across transports.

### Can I test an MCP server without starting a network transport?

Yes. `TestClient` runs the full server pipeline in-process — validation, guards, middleware, and your handler — with no sockets involved. Call `await TestClient(server).call_tool(name, args)` and assert on the returned content, exactly as a networked client would receive it.

## Next steps

Ship your first tool now: `pip install promptise` and expose it with `promptise serve myapp:server --transport http`. From here, follow the [Quick Start](../../getting-started/quickstart.md) to wire the server into an agent, then dig into the full decorator options in [Server Fundamentals](../../mcp/server/building-servers.md) to add auth, rate limits, and approval gates as your server grows.
