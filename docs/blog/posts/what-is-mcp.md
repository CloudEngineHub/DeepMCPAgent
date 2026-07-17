---
title: "What Is MCP? Model Context Protocol Explained"
description: "Most 'what is MCP' posts stop at the spec diagram. This hub explains MCP through a working example — an agent auto-discovering and calling real tools with…"
keywords: "what is mcp, model context protocol, mcp explained, mcp protocol, how mcp works"
date: 2026-07-16
slug: what-is-mcp
categories:
  - MCP
---

# What Is MCP? Model Context Protocol Explained

If you have ever asked *what is MCP* and come away with a spec diagram but no running code, this post is the fix. The **Model Context Protocol** (MCP) is an open standard for connecting language models to tools, data, and prompts in one uniform way — so a tool you build once can be called by any MCP-compatible agent. By the end of this article you will understand the protocol's moving parts and see an agent auto-discover and call a real tool in under ten lines of Python, with zero manual wiring.

<!-- more -->

This is the hub page for our MCP cluster. It stays practical: enough theory to reason about the protocol, then a working example you can run today, then links out to the deeper subtopics — building servers, connecting clients, auth, and testing.

## What is MCP, in one sentence

MCP is a JSON-RPC protocol that standardizes how an LLM application (the **client**) talks to a tool provider (the **server**). Instead of every framework inventing its own tool format, MCP defines one contract for discovery, invocation, and results.

The payoff is separation of concerns the agent ecosystem has been missing. Your tools live behind a stable interface. Your agent speaks that interface. Neither has to know the other's internals, and either side can be swapped without a rewrite. For a longer conceptual treatment — transports, resources, prompts, sampling, and elicitation — see the reference page [What is the Model Context Protocol](../../getting-started/what-is-mcp.md).

## Why the Model Context Protocol exists

Before MCP, wiring tools into agents was a per-framework tax. The same `search_orders` function had to be redefined as a LangChain tool, an OpenAI function spec, and an Anthropic tool block — three schemas, three integrations, three things to keep in sync.

MCP replaces that with four fixes:

- **One schema format.** Tool definitions are described once, in a standard shape.
- **One transport contract.** stdio, streamable HTTP, or SSE — the message flow is identical across all three.
- **One place for security.** Auth, rate limits, and audit live at the server boundary instead of scattered across agent code.
- **One interactive model.** Servers can request structured input from the user (elicitation) or call back to the client's LLM (sampling) using standard messages.

The net effect: your tools outlive any single agent framework. Migrate from one orchestration library to another and your MCP servers keep working untouched.

## How MCP works: clients, servers, and four primitives

Here is how MCP works at runtime. A client and server speak JSON-RPC over a transport, and the handshake is short:

1. The client connects and sends `initialize`.
2. The server replies with its capabilities.
3. The client calls `tools/list` to discover what is available.
4. The client calls `tools/call` with arguments.
5. The server returns the result.

That same flow works whether the server is a 40-line script or an enterprise gateway. On top of it, MCP exposes four primitives:

| Primitive | What it is | Example |
|---|---|---|
| **Tool** | A function the model can call | `search_orders(query)` |
| **Resource** | Read-only context the model can load | a document, a database row |
| **Prompt** | A reusable, parameterized template | `summarize(style="executive")` |
| **Sampling** | The server asks the client's LLM for a completion | a subagent calling back to the orchestrator |

Two later additions round it out: **elicitation** (the server asks the user for structured input mid-run) and **roots** (the server tells the client which filesystem paths it may touch).

## MCP explained through a working example

Theory is cheap. Here is the part most "what is MCP" explainers skip: an actual agent discovering and calling a tool. Promptise Foundry is MCP-native, so tools are *discovered, not wired*. Save this as `tools.py` and run it:

```python
import asyncio
import sys
from promptise import build_agent
from promptise.config import StdioServerSpec
from promptise.mcp.server import MCPServer

# 1. Define a tool server. Type hints become the JSON schema;
#    the docstring becomes the tool description.
server = MCPServer("orders")

@server.tool()
async def get_order_status(order_id: str) -> str:
    """Return the shipping status for an order ID."""
    return f"Order {order_id} shipped and arrives Thursday."

# 2. Build an agent that points at the server and discovers its tools.
async def main():
    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={
            "orders": StdioServerSpec(command=sys.executable, args=["tools.py"]),
        },
        instructions="You help customers with order questions.",
    )
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Where is order A-1007?"}]}
    )
    print(result["messages"][-1].content)
    await agent.shutdown()

if __name__ == "__main__":
    if "--serve" in sys.argv:
        server.run(transport="stdio")   # child process the agent spawns
    else:
        asyncio.run(main())
```

Run it with `python tools.py`. The agent connects to the `orders` server, calls `tools/list`, converts the tool's schema to something the model can invoke, decides on its own that `get_order_status` answers the question, calls it, and folds the result into a reply. You wrote no tool registration, no schema translation, and no dispatch logic.

## Auto-discovery: point an agent at a server and go

That last step is the feature worth dwelling on. **MCP tool auto-discovery** means you point `build_agent()` at a server and it handles the rest: discover every tool, convert each schema to a typed, callable tool, and route the model's calls to the right one.

The example above uses a local stdio server, but the same mechanism works against a remote HTTP endpoint — the agent does not care where the tools live:

```python
from promptise.config import HTTPServerSpec

agent = await build_agent(
    model="openai:gpt-5-mini",
    servers={
        "orders": StdioServerSpec(command=sys.executable, args=["tools.py"]),
        "billing": HTTPServerSpec(
            url="https://mcp.internal.example.com/mcp",
            bearer_token="...",
        ),
    },
    instructions="You are a support agent with order and billing tools.",
)
```

Add a second server and its tools simply appear in the agent's toolbox — no glue code, no manual merge. This is what "MCP-native" buys you: capabilities compose by connection, not by code changes. When you are ready to build the server side properly — auth, guards, rate limits, testing — start with [Building MCP Servers](../../mcp/server/building-servers.md), and to drive servers directly without an agent wrapper, see the [MCP Client](../../mcp/client/index.md) reference. For a full walkthrough of a real server, our tutorial [How to Build an MCP Server in Python](mcp-server-python.md) takes it from empty file to production.

## When MCP is the right fit (and when it isn't)

MCP is a standard, and standards earn their keep by being shared. It is worth the JSON-RPC layer when:

- You want tools that work across multiple LLM providers and agent frameworks.
- You are building a multi-tenant or shared tool service.
- You need one place for auth, rate limits, and audit.
- You want your tools to outlive any specific agent library.

It is honestly overkill when the alternative is simpler:

- **One agent calling one private function.** A direct Python call is less code and less latency than a protocol round-trip. Reach for MCP when a second consumer appears.
- **Throwaway prototypes.** If you are still deciding what the tool even does, the interface overhead does not pay off yet.

The good news is that migrating up is cheap: wrap that private function in `@server.tool()` later and every MCP client can reach it. You lose nothing by starting simple.

## Frequently asked questions

### Is MCP tied to Anthropic or Claude?

No. MCP is an open protocol with a public specification. It was introduced by Anthropic but is implemented across many clients and frameworks, and Promptise Foundry ships its own native client and server SDK with no third-party MCP dependencies. Any MCP-compatible agent can call any MCP-compliant server.

### What is the difference between an MCP client and an MCP server?

The server *provides* capabilities — tools, resources, and prompts — behind the standard interface. The client *consumes* them: it connects, discovers what is available, and invokes it. Your agent is the client; your tool provider is the server. In Promptise, `build_agent()` is the client and `MCPServer` is the server.

### Do I have to write schemas by hand?

No. On the server side, Promptise generates the JSON schema from your Python type hints and pulls the description from the docstring. On the client side, the agent converts each discovered schema into a typed, callable tool automatically. You write plain functions; the protocol plumbing is handled for you.

## Next steps

Follow the quickstart: point `build_agent()` at an MCP server and watch it discover tools in under ten lines. Start with the [Quick Start](../../getting-started/quickstart.md) to get an agent running, then read [What is the Model Context Protocol](../../getting-started/what-is-mcp.md) for the full conceptual map of transports, primitives, and where each subtopic in this cluster fits.
