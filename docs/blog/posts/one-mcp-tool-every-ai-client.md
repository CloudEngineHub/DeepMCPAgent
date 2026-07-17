---
title: "Stop Rewriting Your Tools for Claude, Cursor, and ChatGPT"
description: "Write a tool once and Claude Desktop, Cursor, and ChatGPT can all call it — yet agent frameworks make you author tools as framework-native Python objects…"
keywords: "one mcp tool every ai client, reuse tools across ai clients, native mcp client, claude desktop cursor chatgpt tools, mcp vs bespoke tool wiring"
date: 2026-07-16
slug: one-mcp-tool-every-ai-client
categories:
  - MCP
---

# Stop Rewriting Your Tools for Claude, Cursor, and ChatGPT

Write one MCP tool every AI client can call — Claude Desktop, Cursor, and ChatGPT included — and you never rewrite the same logic per framework again. That is the whole promise of the Model Context Protocol: a tool is a small server that speaks a standard wire format, and anything that speaks the protocol can discover and invoke it. Yet most agent frameworks quietly break that promise. They ask you to author tools as framework-native Python objects that only their own runtime understands, so the moment you want to reuse tools across AI clients you are back to standing up a separate MCP server anyway. This post shows why that gap exists, and how being MCP-native on *both* sides — a server SDK to write the tool and a native MCP client to consume it — collapses two artifacts into one.

## Why one tool should reach every client

Picture a single tool: `search_invoices(customer_id, status)`. In an ideal world you write it once and it lights up everywhere a human or agent already works — the Claude Desktop tool tray, a Cursor MCP connection, a ChatGPT connector, and your own autonomous agents. The protocol was designed for exactly this. A tool publishes a name, a description, and a JSON Schema; every client speaks the same `list_tools` / `call_tool` handshake back to it.

The trouble is where your logic *lives*. When a framework's canonical way to give an agent a tool is a decorated Python function bound to that framework's `BaseTool` class, your invoice logic is now trapped inside that object. Claude Desktop cannot import a LangChain `BaseTool`. Cursor cannot call a CrewAI `@tool`. To reach those clients you wrap the same function a second time in an MCP server — a separate file, a separate process, a separate mental model. You wrote the tool once and maintained it twice. That is the essence of **mcp vs bespoke tool wiring**: the bespoke object is convenient inside one runtime and worthless outside it.

## Write the tool once as an MCP server

Promptise Foundry inverts the default. The first-class way to give tools to an agent is to point it at an MCP server, and the MCP Server SDK is how you *author* that server. The artifact you write is already the portable one — Claude Desktop, Cursor, and ChatGPT can connect to it directly because it is a real MCP endpoint, not a framework object pretending to be one.

```python
# invoice_server.py
from promptise.mcp.server import MCPServer

server = MCPServer(name="invoice-tools", version="1.0.0")


@server.tool()
async def search_invoices(customer_id: str, status: str = "open") -> list[dict]:
    """Find invoices for a customer, optionally filtered by status."""
    # Your real query goes here.
    return [{"id": "INV-1001", "customer_id": customer_id, "status": status}]


if __name__ == "__main__":
    server.run(transport="http", host="0.0.0.0", port=8080)
```

The function name becomes the tool name, the docstring becomes the description, and the type hints become the JSON Schema — generated and kept in sync for you. This one file is now every client's toolbelt at once. Point Claude Desktop, Cursor, or a ChatGPT connector at `http://localhost:8080/mcp` and `search_invoices` shows up, no per-client rewrite.

## The same server becomes your agent's toolbelt

Because Promptise is MCP-native on the consuming side too, your own agent uses that identical server. `build_agent(servers=...)` discovers every tool, converts each JSON Schema into a typed tool, and starts calling them — no manual wiring:

```python
import asyncio
from promptise import build_agent


async def main():
    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={"invoices": "http://localhost:8080/mcp"},
        instructions="You are a billing assistant. Use the tools to answer.",
    )
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "List open invoices for cust-42"}]}
    )
    print(result["messages"][-1].content)
    await agent.shutdown()


asyncio.run(main())
```

Set `OPENAI_API_KEY`, run the server file, then run this script — the agent calls the same `search_invoices` that Claude Desktop would. When you need lower-level control, the same discovery runs on a **native mcp client** you can drive by hand. `MCPMultiClient` connects to several servers, merges their tools into one list, and auto-routes each call to the server that owns it; `MCPToolAdapter` converts those tools into LangChain `BaseTool` objects for a hand-built graph:

```python
import asyncio
from promptise.mcp.client import MCPClient, MCPMultiClient, MCPToolAdapter


async def main():
    multi = MCPMultiClient({
        "invoices": MCPClient(url="http://localhost:8080/mcp"),
        "crm":      MCPClient(url="http://localhost:9090/mcp", api_key="sk-crm"),
    })
    async with multi:
        tools = await multi.list_tools()            # discover across all servers
        print(f"Total tools: {len(tools)}")
        print(multi.tool_to_server)                 # which server owns each tool

        adapter = MCPToolAdapter(multi)
        lc_tools = await adapter.as_langchain_tools()
        print(f"Converted {len(lc_tools)} tools for LangChain")


asyncio.run(main())
```

Crucially, this client is written from scratch — there is no third-party MCP library in the dependency tree. That is one fewer transitive package to pin, patch, and security-scan, and one place to look when transport breaks. The full parameter surface (bearer tokens, stdio subprocesses, custom headers, collision rules) is in the [MCP client reference](../../mcp/client/index.md), and the recursive schema handling — nested objects, arrays, `$ref`/`$defs`, unions, `on_before`/`on_after`/`on_error` tracing hooks — is documented in the [tool adapter reference](../../mcp/client/tool-adapter.md).

## What other frameworks do today

To be fair and precise: the major frameworks all *consume* MCP, and several do it well. What none of them make structural is the reverse direction — turning the tool *you author* inside the framework into an endpoint other clients can reach.

- **LangChain / LangGraph** consume MCP through the separate `langchain-mcp-adapters` package (`MultiServerMCPClient`), which converts a server's tools into LangChain tools. A tool you write as an `@tool` function is not itself published over MCP.
- **CrewAI** does the same via `crewai-tools`' `MCPServerAdapter`. It also ships `max_rpm` and other controls, but a CrewAI `@tool` stays a CrewAI object.
- **AutoGen** consumes MCP via `autogen-ext`'s `mcp_server_tools`. Authored `FunctionTool`s remain AutoGen objects.
- **LlamaIndex** consumes MCP via `llama-index-tools-mcp`'s `McpToolSpec`. Its `FunctionTool`s are LlamaIndex-native.
- **Pydantic AI** has the strongest built-in story: MCP client support ships in the core library (`MCPServerStreamableHTTP`, `MCPServerStdio`) with no add-on. That is a genuine partial overlap — worth saying plainly. But it is still client-side; a tool registered with `@agent.tool` is a Pydantic AI object, not an MCP endpoint Claude Desktop or Cursor can call.

So the exact delta is this. In all five, **MCP is an input**: they read tools from a server you point them at. Authoring a tool as a framework object does not, on its own, publish it over MCP — that publishing is a separate step with a separate library (the official `mcp` SDK or FastMCP). Two of those consumer adapters also mean an extra dependency: every framework above reaches MCP through the official `mcp` Python SDK, a perfectly reasonable choice whose trade-off is one more transitive package in your supply chain.

Promptise makes the capability first-class instead. The MCP Server SDK is the authoring path *and* the portable artifact, and the native client is dependency-light on the consuming path. One server is simultaneously your agent's toolbelt and every external client's — no second wrapper, no `mcp` package to audit. (The schema-as-contract discipline this buys you is why a careless field rename can still break every connected client — see [Why a Small MCP Tool Change Broke Every Connected Agent](version-mcp-tools-without-breaking-clients.md) for how versioning keeps that shared server safe.)

## Harden the shared server for every client

Because the tool is shared across Claude Desktop, Cursor, ChatGPT, and your agents, the server is where you enforce policy once for all of them. Add JWT or API-key auth at the transport, attach per-tool guards and rate limits, and turn on the dashboard — the same `@server.tool()` you already wrote now runs behind production controls without touching any client. The [production features guide](../../mcp/server/production-features.md) walks through auth middleware, per-tool roles, circuit breakers, metrics, and audit logging on exactly this kind of server.

```python
from promptise.mcp.server import MCPServer, AuthMiddleware, JWTAuth

server = MCPServer(name="invoice-tools", version="1.0.0")
server.add_middleware(AuthMiddleware(JWTAuth(secret="${JWT_SECRET}")))

# ... your @server.tool() definitions stay exactly as before ...

server.run(transport="http", host="0.0.0.0", port=8080, dashboard=True)
```

## Frequently asked questions

### Can Claude Desktop, Cursor, and ChatGPT really call the same tool?

Yes — that is the point of MCP. If your tool lives in an MCP server, each client connects to the server's URL (or stdio command), discovers tools via `list_tools`, and invokes them via `call_tool`. The **claude desktop cursor chatgpt tools** you configure are all pointing at one endpoint. Promptise's job is to make writing that endpoint (the Server SDK) and consuming it from your own agents (the native client) the same, first-class workflow.

### Why not just author tools in my agent framework and add MCP later?

Because "later" means maintaining the logic twice. A framework-native tool object works only inside that runtime; to reach other clients you wrap it again in a separate MCP server. Authoring the tool as an MCP server from the start means there is one artifact, and both your agent and every external client use it unchanged.

### Does Promptise depend on the official MCP SDK?

No. The client (`MCPClient`, `MCPMultiClient`, `MCPToolAdapter`) is a from-scratch implementation of the protocol with no third-party MCP library in the dependency tree. Frameworks that consume MCP through `langchain-mcp-adapters`, `crewai-tools`, `autogen-ext`, or `llama-index-tools-mcp` all wrap the official `mcp` package — a fine choice, just one more transitive dependency than a native client carries.

### Can I still bring my existing LangChain tools?

Yes. `build_agent(extra_tools=...)` accepts any LangChain `BaseTool`, and `MCPToolAdapter.as_langchain_tools()` goes the other way for MCP tools you want inside a LangGraph graph. The MCP-native path is the default for portability; framework-native tools remain a supported escape hatch.

## Next steps

Build one MCP server every client can use, then let your Promptise agent drive it: write the tool with `@server.tool()`, run it, and point both `build_agent(servers=...)` and Claude Desktop at the same URL. Start with the [production features guide](../../mcp/server/production-features.md) to harden that shared server, keep the [MCP client reference](../../mcp/client/index.md) and [tool adapter reference](../../mcp/client/tool-adapter.md) open as you wire agents to it, and you will maintain your tools in exactly one place.
