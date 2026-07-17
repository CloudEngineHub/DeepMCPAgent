---
title: "Migrating off LangChain to Promptise Foundry"
description: "A concrete, step-by-step path: keep your LangChain tools via the adapter, move orchestration, memory and auth to Promptise incrementally, and verify each…"
keywords: "migrating off LangChain, migrate from LangChain, LangChain to Promptise migration, replace LangChain in production, LangChain migration guide"
date: 2026-07-16
slug: migrating-off-langchain
categories:
  - Comparisons
---

# Migrating off LangChain to Promptise Foundry

Migrating off LangChain rarely fails because the new framework is worse — it fails because teams try to do it in one weekend, rip out everything at once, and lose the ability to tell whether the port is correct. This guide gives you the opposite: an incremental path where your existing LangChain tools keep working from day one, you move orchestration, memory, and auth one layer at a time, and you verify every step before the next. By the end you'll have a plan to migrate from LangChain to Promptise Foundry without a scary big-bang rewrite, plus a reproducible `.superagent` manifest that captures the finished agent.

## Why teams migrate from LangChain in production

If your prototype runs happily on LangChain, you don't need this post. The pressure to replace LangChain in production usually shows up later, once the agent has real users and real obligations:

- **Tool maintenance never ends.** Every integration is a hand-written wrapper you keep in sync with an upstream API. Multiply that by every service the agent touches.
- **Identity was an afterthought.** A prototype has one user; production has thousands, each allowed to see different data. Retrofitting per-request identity under deadline pressure is the worst way to design a security boundary.
- **Governance is bolted on.** Spend budgets, loop detection, and human approval for irreversible actions end up as bespoke glue rather than framework primitives.

Promptise Foundry is MCP-native and secure by default, so tools are *discovered, not wired*, and identity, tenancy, and governance are built in. The [Why Promptise Foundry](../../getting-started/why-promptise.md) page lays out that philosophy without hype — read it first so you know what you're migrating *toward*, not just away from.

## The migration strategy: incremental, not big-bang

The core idea is that Promptise agents are built on LangChain's `BaseTool` interface underneath. That's not an accident of history — it's the seam that makes a LangChain to Promptise migration safe. Because the tool type is shared, you can:

1. Drop your existing LangChain tools straight into a Promptise agent, unchanged.
2. Move one concern at a time — orchestration first, then memory, then auth — and re-test after each move.
3. Freeze the finished configuration in a declarative manifest so the result is reproducible.

You never have a moment where the agent is half-ported and untestable. Let's walk each step.

## Step 1 — Keep your LangChain tools with MCPToolAdapter

The first move is the one that de-risks everything else: get a Promptise agent running with the tools you already have. Two kinds of tools coexist in one agent. Existing LangChain tools pass in directly through `extra_tools`, and tools that already live behind a Model Context Protocol server become the same `BaseTool` type via `MCPToolAdapter`.

```python
import asyncio
from langchain_core.tools import tool                 # your existing LangChain tool
from promptise import build_agent
from promptise.mcp.client import MCPClient, MCPMultiClient, MCPToolAdapter


# A tool you already wrote for LangChain — no rewrite.
@tool
def lookup_order(order_id: str) -> str:
    """Look up an order by its ID."""
    return f"Order {order_id}: shipped"


async def main():
    # Tools that already speak MCP, converted to the same BaseTool type.
    multi = MCPMultiClient({"billing": MCPClient(url="https://mcp.internal/billing/mcp")})
    async with multi:
        adapter = MCPToolAdapter(multi)
        mcp_tools = await adapter.as_langchain_tools()

        # One Promptise agent runs both kinds of tools together.
        agent = await build_agent(
            model="openai:gpt-5-mini",
            extra_tools=[lookup_order, *mcp_tools],
            instructions="You are a customer support agent.",
        )
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": "Where is order 4471?"}]}
        )
        print(result["messages"][-1].content)
        await agent.shutdown()


asyncio.run(main())
```

Nothing about your tool code changed. That is the whole point of the adapter: it is an interop bridge, not a rewrite mandate. `MCPToolAdapter` handles recursive JSON Schema — nested objects, arrays of objects, `$ref`/`$defs`, and unions — so tools with complex arguments survive the conversion intact.

At this stage you've already got a working Promptise agent. Everything after this is *improvement*, and each improvement is optional and independently verifiable.

## Step 2 — Move orchestration, memory, and auth incrementally

Now migrate the concerns that were painful in LangChain, one at a time. Each is a single parameter on `build_agent()`, so you can add one, re-test, and commit before touching the next.

- **Orchestration.** Swap ad-hoc chains for a named execution pattern with `agent_pattern="react"` (or `verify`, `managed`, `research`, and others). No graph wiring by hand.
- **Memory.** Replace your custom retrieval glue with a first-class provider — `ChromaProvider` for local persistent vector search, `Mem0Provider` for enterprise graph memory. The agent auto-searches memory before each invocation and injects relevant results.
- **Auth and tenancy.** Attach per-request identity with `CallerContext(user_id=..., roles=[...], tenant_id=...)`. That identity propagates to cache, memory, and audit logs automatically — the retrofit you were dreading becomes a constructor argument.
- **Token cost.** Turn on `optimize_tools="semantic"` to select only the relevant tools per query using local embeddings, which the framework measures at **40–70% fewer tokens** on tool-heavy servers. Add `cache=SemanticCache()` for **30–50% cost reduction** on repeated, semantically similar queries.

For a deeper walkthrough of discovery, patterns, and configuration, the [building agents guide](../../guides/building-agents.md) is the reference to keep open while you port each layer.

## Step 3 — Capture the agent in a .superagent manifest

Once the agent behaves the way you want, freeze it. A `.superagent` YAML manifest defines the entire agent declaratively — model, instructions, servers, memory, cache, guardrails — with `${ENV_VAR}` resolution so no secrets live in the file. This is the reproducibility layer LangChain code rarely gives you: the agent becomes config you can diff, review, and deploy.

```yaml
version: "1.0"
agent:
  model: "openai:gpt-5-mini"
  instructions: "You are a customer support agent."
servers:
  billing:
    type: http
    url: "https://mcp.internal/billing/mcp"
    headers:
      Authorization: "Bearer ${BILLING_TOKEN}"
memory:
  provider: chroma
  persist_directory: ".promptise/memory"
optimize_tools: semantic
cache: true
guardrails: true
observability: true
```

Validate and run it from the CLI — no Python entrypoint required:

```bash
promptise validate support.superagent
promptise agent support.superagent
```

The manifest is the artifact that makes your migration *durable*: a new teammate reads one file to understand the whole agent, and CI can validate it on every change.

## Verify each step with the in-process TestClient

Incremental migration only works if you can prove each move is correct. When you promote a LangChain tool into a proper MCP server (so other agents can reuse it), test it in-process first — no network, no container, full pipeline.

```python
from promptise.mcp.server import MCPServer, TestClient


server = MCPServer("billing")


@server.tool()
async def get_balance(account: str) -> str:
    """Return the balance for an account."""
    return f"Account {account}: $42.00"


async def test_get_balance():
    result = await TestClient(server).call_tool("get_balance", {"account": "4471"})
    assert "42.00" in str(result)
```

`TestClient` runs the real request path — validation, guards, middleware, and handler — entirely in-process, so it slots straight into your existing pytest suite. Verify the tool, then wire it into the agent, then move on. For the mechanical checklist of installing, pinning versions, and confirming the switch, the [migration guide](../../resources/migration.md) has a copy-paste sequence you can run top to bottom.

## What doesn't map one-to-one — and when LangChain is the better fit

Honesty matters more than a clean pitch, so here's where a LangChain to Promptise migration is *not* a drop-in swap:

- **Bespoke LCEL pipelines.** Intricate LangChain Expression Language graphs don't translate node-for-node. You re-express the intent as a Promptise reasoning pattern or a custom `PromptGraph`, which is usually simpler but is still a rewrite of that layer.
- **Community integration breadth.** LangChain's catalog of prebuilt connectors is large and genuinely hard to match. If you depend on an obscure integration today, it may already exist there and not yet as an MCP server.
- **Callback and tracing ecosystems.** If your observability is deeply tied to a LangChain-specific tracing vendor, you'll move to Promptise's own observability transporters rather than reuse that wiring.

And to be fair about when you should *not* migrate at all: if you're building a throwaway prototype that will never get a second user, the governance, tenancy, and audit primitives that justify the switch are overhead you don't need yet. LangChain's fast start and huge ecosystem are real advantages for exploration. For a broader, vendor-neutral head-to-head across the field, see [Best AI Agent Framework in 2026: An Honest Guide](best-ai-agent-framework-2026.md) — it scores the trade-offs without crowning a predetermined winner.

## Frequently asked questions

### Can I keep my existing LangChain tools when migrating off LangChain?

Yes. Promptise agents use LangChain's `BaseTool` interface underneath, so your existing tools pass directly into `build_agent(extra_tools=[...])` with no rewrite. Tools that already live behind an MCP server convert to the same type through `MCPToolAdapter.as_langchain_tools()`, and both kinds run in one agent.

### Do I have to migrate everything at once to replace LangChain in production?

No — that's the failure mode this guide avoids. You get a working Promptise agent in Step 1 using your current tools, then move orchestration, memory, and auth one parameter at a time, re-testing after each change with the in-process `TestClient`. There is never a point where the agent is half-ported and untestable.

### Is a .superagent manifest required?

No, it's optional. You can run everything from Python with `build_agent()`. The manifest simply captures a finished agent as reviewable, version-controlled config so deployments are reproducible and a teammate can understand the whole agent from one file.

## Next steps

Follow the migration guide and `pip install promptise` to start the first incremental move — get an agent running with your existing tools before you change anything else. Begin with the [Quick Start](../../getting-started/quickstart.md), then use the [building agents guide](../../guides/building-agents.md) to port each layer with confidence.
