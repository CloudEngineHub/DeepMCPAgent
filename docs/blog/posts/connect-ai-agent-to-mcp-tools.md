---
title: "Connect Your AI Agent to MCP Tools with Promptise"
description: "The production path, not a toy demo: point the agent at your MCP servers for auto-discovery, then flip tool optimization to SEMANTIC so local embeddings…"
keywords: "connect ai agent to mcp tools, promptise mcp tool discovery, reduce agent token usage, semantic tool selection python, mcp tools python agent"
date: 2026-07-16
slug: connect-ai-agent-to-mcp-tools
categories:
  - Building Agents
---

# Connect Your AI Agent to MCP Tools with Promptise

To connect an AI agent to MCP tools in production, you need two things: reliable auto-discovery so you never hand-wire schemas, and a way to stop a large tool catalog from bloating every prompt. Most tutorials give you the first and quietly skip the second — then your token bill climbs as you add servers. This post shows the production path with Promptise: point the agent at your MCP servers, let it discover every tool, and flip tool optimization to `SEMANTIC` so local embeddings select only the tools each query actually needs. By the end you'll have a working config, the air-gapped local-embeddings variant, and a clear sense of when you don't need any of it.

<!-- more -->

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## How Promptise MCP tool discovery works

Promptise is MCP-native, which means tools are not something you register by hand. You give `build_agent()` a set of server specs; on startup it connects to each server over the native MCP client, lists the tools, converts their JSON schemas into typed Python tools, and makes them callable by the model. Add a server, restart the agent, and the new tools appear — no glue code, no manual wiring.

That auto-discovery is the whole point of the protocol, and it's why an agent framework earns its keep here. If you're still deciding whether you need one at all, [What Is a Python AI Agent Framework? (And When You Need One)](python-ai-agent-framework.md) makes the honest case. For the mechanics of the factory function itself, the [building agents reference](../../core/agents/building-agents.md) documents every parameter.

## Connect your agent to MCP servers in one call

Here's the minimal, runnable version. It connects a single agent to two MCP servers — one launched over stdio, one reached over HTTP with a bearer token — and lets the model use whatever tools they expose.

```python
import asyncio
from promptise import build_agent
from promptise.config import StdioServerSpec, HTTPServerSpec


async def main():
    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={
            "local": StdioServerSpec(command="python", args=["tools.py"]),
            "api": HTTPServerSpec(
                url="https://mcp.example.com/mcp",
                bearer_token="...",
            ),
        },
        instructions="You are an operations assistant. Use the available tools.",
    )

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "What's the weather in Berlin?"}]}
    )
    print(result["messages"][-1].content)
    await agent.shutdown()


asyncio.run(main())
```

Set your `OPENAI_API_KEY`, point the specs at real servers, and this runs end to end. The agent discovers the tools from both servers, decides when to call them, and returns the final answer. That's the getting-started shape. The `servers` argument also accepts plain URLs and dict specs, so you can wire in whatever your infrastructure already exposes.

## Why big tool catalogs cost you tokens

Auto-discovery is convenient, but it has a downside nobody warns you about: every discovered tool's name, description, and parameter schema is serialized into the prompt on every single turn. Two servers with a dozen tools each is fine. Point your agent at a fleet of internal MCP servers — CRM, billing, search, deployment, analytics — and you're shipping hundreds of tool definitions to the model on every request, most of which are irrelevant to the question at hand.

You pay for that three ways:

- **Cost** — those tokens are billed on every turn, whether or not any tool is used.
- **Latency** — larger prompts take longer to process.
- **Accuracy** — a wall of near-duplicate tools makes it harder for the model to pick the right one.

Promptise gives you four tool-optimization levels to manage this, from `NONE` (send everything as-is) through `MINIMAL` and `STANDARD` (schema minification and description stripping) up to `SEMANTIC`, which is where the real savings live.

## Semantic tool selection in Python: fewer tokens, same capability

`SEMANTIC` mode adds per-invocation selection on top of the static optimizations. Promptise embeds each discovered tool's description locally, embeds the user's query, and sends the model only the most relevant tools for that specific request. Everything else stays available through a `request_more_tools` fallback, so the agent never loses a capability — it just stops paying to advertise tools it isn't using.

The framework's own figure for this is **40–70% fewer tool tokens** without dropping capability. You turn it on with a single argument:

```python
import asyncio
from promptise import build_agent
from promptise.config import HTTPServerSpec


async def main():
    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={
            "api": HTTPServerSpec(url="https://mcp.example.com/mcp", bearer_token="..."),
        },
        instructions="You are an operations assistant.",
        optimize_tools="semantic",   # local embeddings pick relevant tools per query
    )

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Refund order 8842 and email the customer."}]}
    )
    print(result["messages"][-1].content)
    await agent.shutdown()


asyncio.run(main())
```

For that query, the agent surfaces the refund and email tools and leaves the deployment, analytics, and search tools out of the prompt — while still being able to pull them in on demand. The embeddings run locally on the machine hosting the agent, so semantic selection adds no external API calls of its own.

## Air-gapped: local embeddings for tool selection

Because selection runs on a local embedding model, `SEMANTIC` mode works with no network egress at all — which matters for regulated and air-gapped deployments. When you need to pin an exact model or point at weights baked into a container image, pass a `ToolOptimizationConfig` and set `embedding_model` to a local path:

```python
import asyncio
from promptise import build_agent, OptimizationLevel, ToolOptimizationConfig
from promptise.config import HTTPServerSpec


async def main():
    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={"api": HTTPServerSpec(url="https://mcp.example.com/mcp", bearer_token="...")},
        instructions="You are an operations assistant.",
        optimize_tools=ToolOptimizationConfig(
            level=OptimizationLevel.SEMANTIC,
            embedding_model="/models/all-MiniLM-L6-v2",  # local path — no network needed
            semantic_top_k=8,                             # how many tools to surface per query
        ),
    )
    # ... use the agent, then:
    await agent.shutdown()


asyncio.run(main())
```

`semantic_top_k` controls how many tools reach the model each turn — lower for tighter prompts, higher if your queries routinely span several tools. The default embedding model is `all-MiniLM-L6-v2`; the `embedding_model` field takes either a model name or a filesystem path, so the same config works whether you download at runtime or ship the weights offline.

For copy-paste-ready versions of these recipes — including how to combine tool optimization with memory and caching — the [Cookbook](../../getting-started/cookbook.md) collects them as verified, task-oriented snippets. The broader [guide to building agents](../../guides/building-agents.md) walks through wiring optimization alongside guardrails, observability, and conversation persistence in a single agent.

## When plain tool discovery is the better fit

Semantic optimization is not free complexity you should always add. Reach for it when your token math justifies it — and skip it when it doesn't:

- **Small catalogs.** If your agent talks to one or two servers with a handful of tools, the static prompt is already cheap. `optimize_tools="minimal"` or leaving it off is simpler and just as fast.
- **Every-tool workflows.** If a typical request genuinely touches most of your tools, selection buys you little and the fallback round-trips can cost more than they save.
- **Ultra-low latency on tiny toolsets.** The local embedding step is fast, but on a three-tool agent it's overhead you don't need.

The honest rule: start with plain auto-discovery, watch your tool-token count as you add servers, and switch to `SEMANTIC` when the catalog grows past what you want to send every turn. If you're building your first agent from scratch, [How to Build an AI Agent in Python: The Complete Guide](how-to-build-an-ai-agent-in-python.md) covers that starting point before you optimize anything.

## Frequently asked questions

### Do I need to register MCP tools manually with Promptise?

No. You pass server specs to `build_agent()` and Promptise discovers every tool on startup — listing them over the native MCP client and converting each schema into a typed, callable tool. Adding a server means adding one entry to the `servers` argument, not writing wiring code.

### Does semantic tool selection ever hide a tool the agent needs?

No capability is lost. `SEMANTIC` mode surfaces the top-relevant tools per query and keeps the rest reachable through a `request_more_tools` fallback, so the model can pull in anything it needs mid-task. You're trimming the default prompt, not removing tools from the agent.

### Can tool optimization run without internet access?

Yes. Embedding and selection run on a local model, so `SEMANTIC` mode makes no external calls of its own. Point `embedding_model` at a local path such as `/models/all-MiniLM-L6-v2` and the whole selection step stays inside your environment — suitable for air-gapped deployments.

## Next steps

Copy the tool-optimization recipe from the [Cookbook](../../getting-started/cookbook.md) and cut your agent's token bill on the first run — flip `optimize_tools="semantic"` and watch the tool-token count drop. From there, the [Quick Start](../../getting-started/quickstart.md) gets a fresh agent talking to your first MCP server in a few minutes, and the [building agents reference](../../core/agents/building-agents.md) documents every optimization level in full.
