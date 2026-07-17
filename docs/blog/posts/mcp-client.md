---
title: "MCP Client Tutorial: Connect Agents to MCP Servers"
description: "Covers the client side most tutorials skip — connecting to one or many servers behind a single unified tool list with auto-routing, plus converting MCP tools…"
keywords: "mcp client, mcp client python, connect to mcp server, mcp client tutorial, langchain mcp adapter"
date: 2026-07-16
slug: mcp-client
categories:
  - MCP
---

# MCP Client Tutorial: Connect Agents to MCP Servers

Most tutorials teach you to build an MCP server, then stop — but an MCP client is what actually connects your agent to that server, discovers its tools, and calls them. This tutorial covers the side most guides skip: how to connect to one server, fan out across many servers behind a single unified tool list with auto-routing, and convert MCP tools into LangChain tools you can drop into any agent. Everything here uses Promptise Foundry's native MCP client — three small classes, no third-party MCP dependency to audit or update. By the end you'll be able to wire an agent to any MCP server in a few lines of Python.

<!-- more -->

## What an MCP client actually does

If you're new to the protocol, start with [What Is MCP? Model Context Protocol Explained](what-is-mcp.md) or the [What is MCP](../../getting-started/what-is-mcp.md) concepts page. The short version: MCP standardizes how an LLM application discovers and calls tools exposed by a separate server process. The server publishes tools; the client is the piece that speaks the protocol back to it.

A client's job is narrow but load-bearing:

- **Handle transport** — streamable HTTP, SSE, or a local stdio subprocess.
- **Inject auth** — a bearer token as an `Authorization` header, or an API key as `x-api-key`.
- **Discover tools** — `list_tools()` returns every tool with its name, description, and JSON Schema.
- **Invoke tools** — `call_tool(name, arguments)` runs one and returns a typed result.

Promptise Foundry ships this as a from-scratch implementation with no third-party MCP client library in the dependency tree. That matters for production: fewer transitive packages to pin, patch, and security-scan, and one place to look when something breaks.

## Connect to one MCP server with MCPClient

`MCPClient` connects to a single server. It's an async context manager, so the connection stays open for the lifetime of the `async with` block — open once, reuse it for every call, don't spin up a new client per tool.

```python
import asyncio
from promptise.mcp.client import MCPClient

async def main():
    async with MCPClient(
        url="http://localhost:8080/mcp",
        bearer_token="eyJhbGciOiJIUzI1NiIs...",  # from your IdP in production
    ) as client:
        tools = await client.list_tools()
        print(f"Discovered {len(tools)} tools")

        result = await client.call_tool("get_weather", {"city": "Berlin"})
        print(result.content[0].text)  # "Sunny in Berlin"

asyncio.run(main())
```

Swap `bearer_token=` for `api_key="sk-..."` if the server uses API-key auth, or drop both for an unauthenticated local server. To talk to a tool server running as a local subprocess instead of over the network, pass `transport="stdio"` with `command` and `args` — the client launches and manages the process for you. The full parameter table (timeouts, custom headers, `fetch_token` for dev token endpoints) lives in the [MCP client reference](../../mcp/client/index.md).

## Fan out across servers with MCPMultiClient and auto-routing

Real agents rarely talk to one server. You might have an HR server, a docs server, and an internal API server — each with its own URL and credentials. `MCPMultiClient` connects to all of them at once, aggregates their tools into a single list, and routes each `call_tool` to the server that actually owns the tool. You call tools by name; the client figures out where they live.

```python
import asyncio
from promptise.mcp.client import MCPClient, MCPMultiClient

async def main():
    multi = MCPMultiClient({
        "hr":   MCPClient(url="http://hr-server:8080/mcp", bearer_token=hr_token),
        "docs": MCPClient(url="http://docs-server:9090/mcp", api_key="sk-docs"),
    })

    async with multi:
        tools = await multi.list_tools()          # discover across all servers
        print(f"Total tools: {len(tools)}")

        # Routed automatically to whichever server owns the tool
        result = await multi.call_tool("search_employees", {"query": "python"})
        print(result.content[0].text)

        print(multi.tool_to_server)  # {"search_employees": "hr", "search_docs": "docs"}

asyncio.run(main())
```

Two things to know when you connect to a server this way:

- **Call `list_tools()` first.** Auto-routing is built from discovery, so `call_tool()` raises a `MCPClientError` if you haven't discovered tools yet.
- **Watch for name collisions.** If two servers export a tool with the same name, the last one discovered wins and a warning is logged. Namespace your tools server-side to avoid it — see [Building servers](../../mcp/server/building-servers.md) for namespace prefixes and transforms.

## Convert MCP tools to LangChain with the MCP tool adapter

Discovering and calling tools by hand is useful for scripts and health checks, but an agent needs those tools in a form its framework understands. `MCPToolAdapter` is the langchain mcp adapter that turns every discovered MCP tool into a standard LangChain `BaseTool` — complete with a Pydantic `args_schema` generated from the MCP JSON Schema, including nested objects, arrays, `$ref`/`$defs` references, unions, and field constraints.

```python
import asyncio
from promptise import build_agent
from promptise.mcp.client import MCPClient, MCPMultiClient, MCPToolAdapter

async def main():
    multi = MCPMultiClient({
        "hr": MCPClient(url="http://localhost:8080/mcp", bearer_token=token),
    })

    async with multi:
        adapter = MCPToolAdapter(multi)
        lc_tools = await adapter.as_langchain_tools()
        print(f"Converted {len(lc_tools)} tools for LangChain")

        agent = await build_agent(
            model="openai:gpt-5-mini",
            extra_tools=lc_tools,
            instructions="You are an HR assistant. Use the tools to answer.",
        )
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": "Find remote Python engineers"}]}
        )
        print(result["messages"][-1].content)
        await agent.shutdown()

asyncio.run(main())
```

The adapter also accepts `on_before`, `on_after`, and `on_error` callbacks so you can trace every tool invocation into Prometheus, OpenTelemetry, or your own logs without wrapping each tool by hand. The [tool adapter reference](../../mcp/client/index.md) documents the recursive schema handling and introspection helpers in full.

## When you don't need the client directly

Here's the honest part most tutorials leave out: for the common case, you don't call `MCPClient` yourself at all. `build_agent(servers=...)` uses this same native client under the hood — point it at server URLs or stdio specs and it discovers and wires every tool for you:

```python
agent = await build_agent(
    model="openai:gpt-5-mini",
    servers={"api": "https://mcp.example.com/mcp"},
    instructions="Answer using the available tools.",
)
```

Reach for the client classes directly when you need control the factory doesn't give you: writing a non-agent script that calls tools imperatively, aggregating servers with custom collision rules, converting MCP tools for a hand-built LangGraph graph, or running discovery in a health check. And if you're already committed to another stack — the official MCP Python SDK client, or a hosted platform that manages MCP connections and credentials for you — that's a perfectly good fit, especially when you want a vendor to own transport and secret rotation. Promptise Foundry's client is the better choice when you want a dependency-light, self-contained implementation you can read end to end and pass straight into `build_agent`.

## Frequently asked questions

### How do I connect to an MCP server in Python?

Open an `MCPClient` as an async context manager with the server URL and any auth (`bearer_token` or `api_key`), call `list_tools()` to discover tools, then `call_tool(name, arguments)` to run one. For a local tool server, use `transport="stdio"` with `command` and `args` instead of a URL. The connection stays open for the whole `async with` block, so keep one client alive rather than creating one per call.

### What's the difference between MCPClient and MCPMultiClient?

`MCPClient` talks to exactly one server. `MCPMultiClient` wraps a dict of named `MCPClient` instances, merges their tools into one list, and auto-routes each `call_tool` to the server that owns the tool via its `tool_to_server` map. Use the multi-client whenever an agent needs tools from more than one server — just remember to call `list_tools()` before your first `call_tool()`.

### Can I use MCP tools with LangChain or LangGraph?

Yes. `MCPToolAdapter.as_langchain_tools()` converts discovered MCP tools into standard LangChain `BaseTool` instances with typed Pydantic schemas. Pass the result to `build_agent(extra_tools=...)` or into any LangChain-compatible workflow.

## Next steps

Wire an agent to any MCP server with `MCPClient` — or fan out across servers with `MCPMultiClient` — then hand the tools to `build_agent` and let it drive. Start with the [Quick Start](../../getting-started/quickstart.md), then keep the [MCP client reference](../../mcp/client/index.md) open as you build.
