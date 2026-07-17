---
title: "MCP vs Function Calling: What's the Difference?"
description: "An honest breakdown: raw function calling is the right call for a handful of hardcoded tools in one process — MCP earns its keep once tools live behind a…"
keywords: "mcp vs function calling, mcp vs tool calling, model context protocol vs function calling, when to use mcp, function calling alternatives"
date: 2026-07-16
slug: mcp-vs-function-calling
categories:
  - MCP
---

# MCP vs Function Calling: What's the Difference?

The MCP vs function calling debate confuses a lot of teams, because the two are not actually competitors — they operate at different layers. Function calling is how an LLM decides *which* tool to call and *with what arguments*. The Model Context Protocol (MCP) is how those tools get *packaged, served, and shared*. By the end of this post you'll know exactly where the crossover point sits, so you can keep raw function calling while your tools fit in one file and reach for MCP the moment they don't — without paying a token tax as your catalog grows.

<!-- more -->

## What function calling actually is

Function calling is a feature of the model API. You describe your tools as JSON Schema, send those definitions with every request, and the model responds with a structured tool call: a name plus arguments. Your code executes the function and feeds the result back.

That's the whole loop. It's simple, fast, and has zero moving parts beyond your own process:

- Tool schemas live inline in your code.
- The model picks a tool and fills in arguments.
- *You* run the function — the model never touches your logic directly.

For a handful of tools that live in one script, this is the correct design. There is no server to run, no auth to configure, nothing to version. If you have three functions and one deployment, adding an abstraction layer would be pure overhead.

## What MCP adds on top of function calling

MCP doesn't replace function calling — it standardizes the layer *above* it. Instead of hardcoding tool schemas into every agent, you expose your tools from an MCP server, and any MCP-compatible client discovers them at runtime by calling `tools/list`. The client still uses ordinary function calling under the hood to invoke them.

The practical difference is who owns the tool definition. With raw function calling, the definition is copy-pasted into each agent that needs it. With MCP, the definition lives in one server and every agent — yours, Claude Desktop, Cursor, an internal support bot — reads the same source of truth. If you're new to the protocol, the [What Is MCP? Model Context Protocol Explained](what-is-mcp.md) post and the [What is MCP](../../getting-started/what-is-mcp.md) guide walk through the wire format and the discovery handshake.

This is why the honest framing isn't "MCP vs tool calling." It's *inline tools vs served tools*. Function calling is the mechanism; MCP is the deployment model.

## When to use MCP (and when to skip it)

Here's the crossover checklist. Stay on plain function calling while all of these are true:

- **One process.** Your tools run in the same deployment as your agent.
- **One consumer.** Only this agent calls them.
- **No shared auth or tenancy.** You don't need per-caller identity, roles, or tenant isolation on the tools themselves.
- **Stable surface.** You're not versioning tool contracts for external callers.
- **Small catalog.** A handful of tools you can hold in your head.

Reach for MCP once *any* of these flips:

- **Tools live behind a server** — a database service, an internal API, a separate team's system.
- **You need auth, roles, rate limits, or audit** on the tools, enforced server-side rather than trusted from each client.
- **Tools are shared across agents** — the same `refund` tool used by three bots shouldn't be defined three times.
- **You need versioning** — clients on `search@1.0` while others migrate to `search`.
- **The catalog is large or growing** — dozens of tools that no single prompt should carry all at once.

### When function calling is the better fit

Be honest with yourself here: if you're shipping a single-purpose script with four tools and no plans to share them, MCP is added machinery you don't need yet. Running a server, wiring auth, and discovering tools over a transport all cost complexity. Plain function calling — tools defined inline, executed in-process — is genuinely the right call until one of the crossover conditions above is met. Promptise Foundry is built around MCP, but the framework's own [Why Promptise](../../getting-started/why-promptise.md) philosophy is to be honest about when a simpler tool wins.

## Keeping prompt tokens flat as your tool catalog grows

The most common objection to MCP is real: once you point an agent at several servers, you can have 30, 50, or 80 tools, and every full tool schema gets sent to the model on *every* call. That's often the single largest token cost after the conversation itself — 5,000 to 15,000+ tokens per request just for definitions. This is exactly where a naive "just discover all the tools" approach falls over.

Promptise Foundry solves it with **4-level tool optimization**, controlled by one parameter on `build_agent()`:

- **`NONE`** (default) — send every tool's full schema, unchanged.
- **`MINIMAL`** — strip verbose per-field descriptions and truncate tool descriptions. Static, no behavioral change.
- **`STANDARD`** — deeper minification plus flattening of nested schemas.
- **`SEMANTIC`** — embed every tool description locally, then select only the tools relevant to *this* query before the call.

Semantic mode is the one that keeps prompt tokens flat as the catalog grows: instead of shipping all 50 schemas, it uses a local embedding model to pick the top matches for each query — **40–70% fewer tokens** on tool definitions, with a `request_more_tools` fallback so the agent can self-recover if the selection ever misses.

```python
import asyncio
from promptise import build_agent
from promptise.config import HTTPServerSpec


async def main():
    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={
            "crm": HTTPServerSpec(url="https://mcp.internal/crm/mcp"),
            "billing": HTTPServerSpec(url="https://mcp.internal/billing/mcp"),
        },
        instructions="You are a support agent.",
        optimize_tools="semantic",  # local embeddings pick only relevant tools per query
    )

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Refund invoice INV-2043"}]}
    )
    print(result["messages"][-1].content)
    await agent.shutdown()


asyncio.run(main())
```

Need finer control — a fixed top-K, a local air-gapped embedding model, or tools that must never be dropped? Pass a config instead of a preset string:

```python
from promptise import build_agent, ToolOptimizationConfig, OptimizationLevel

agent = await build_agent(
    model="openai:gpt-5-mini",
    servers=servers,
    optimize_tools=ToolOptimizationConfig(
        level=OptimizationLevel.SEMANTIC,
        semantic_top_k=8,
        preserve_tools={"process_refund"},  # always selected, even on a weak match
    ),
)
```

Because embeddings run locally with `sentence-transformers`, there's no extra API call and no data leaving your box — you can even point `embedding_model` at a local directory for fully offline deployments.

## A minimal served tool, end to end

Moving a function behind MCP is small. You define the tool with a decorator, and the schema is generated from your type hints — the same JSON Schema function calling needs, just produced for you:

```python
from promptise.mcp.server import MCPServer, TestClient

server = MCPServer("billing-tools")


@server.tool()
async def refund(invoice_id: str, amount: float) -> dict:
    """Refund an invoice by id."""
    return {"invoice_id": invoice_id, "refunded": amount}


async def check():
    # Full pipeline in-process — no network, no live model
    result = await TestClient(server).call_tool(
        "refund", {"invoice_id": "INV-2043", "amount": 40.0}
    )
    print(result)
```

That `refund` tool is now discoverable by any MCP client, and you can layer auth, roles, rate limits, and approval gates onto it without changing the agent code. The [Building MCP Servers](../../mcp/server/building-servers.md) guide covers decorators, guards, and middleware; the [native MCP client](../../mcp/client/index.md) shows how an agent consumes servers over stdio, HTTP, and SSE. For a full walkthrough of building one from scratch, see [How to Build an MCP Server in Python (Tutorial)](mcp-server-python.md).

## Frequently asked questions

### Is MCP a replacement for function calling?

No. Function calling is how the model selects a tool and its arguments; MCP is how tools are served and discovered. An MCP client still uses function calling under the hood — MCP just moves the tool definitions out of your prompt and into a shared server, so multiple agents read one source of truth.

### When should I use MCP instead of hardcoded tools?

Use MCP once tools live behind a server, need server-side auth, roles, or audit, are shared across more than one agent, or require versioned contracts. If you have a few tools in a single process with one consumer, plain function calling is simpler and perfectly appropriate.

### Does adding many MCP tools bloat my prompt and cost?

It can, if you send every schema every time. Promptise Foundry's semantic tool optimization mitigates this by embedding tool descriptions locally and selecting only the relevant ones per query — cited at 40–70% fewer tokens on tool definitions — with a fallback tool so the agent can request more if needed.

## Next steps

Still on hardcoded function calling? Use the crossover checklist above, then reach for `build_agent()` the moment your tools outgrow a single file. Start with the [Quick Start](../../getting-started/quickstart.md) to wire your first MCP-backed agent, then revisit [What Is MCP?](../../getting-started/what-is-mcp.md) to see how discovery keeps your tools in one source of truth as you turn on `optimize_tools="semantic"`.
