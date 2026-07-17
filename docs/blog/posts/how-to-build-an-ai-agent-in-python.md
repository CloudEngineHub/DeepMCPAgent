---
title: "How to Build an AI Agent in Python: The Complete Guide"
description: "Most 'build an agent' tutorials hand-wire tool schemas and hard-code one model, so the reader is stuck the moment they change anything. This hub shows the…"
keywords: "how to build an ai agent in python, build ai agent python, python ai agent tutorial, llm agent python, ai agent from scratch python, python agent with tools"
date: 2026-07-16
slug: how-to-build-an-ai-agent-in-python
categories:
  - Building Agents
---

# How to Build an AI Agent in Python: The Complete Guide

If you have searched for how to build an AI agent in Python, you have probably found tutorials that hand-wire tool schemas and hard-code a single model. They work until the first thing changes — you swap `gpt-4o-mini` for a local Llama, add a second tool, or move from a notebook to a service — and then you are rewriting glue code instead of building features. This guide takes a different route. By the end you will have a real LLM agent in Python, understand how it discovers and calls tools automatically, and know which parts of the stack you should build yourself versus let a framework handle.

<!-- more -->

## What "building an AI agent in Python" actually means

An AI agent is more than a single call to an LLM. A raw completion returns text; an agent runs a loop: it reads your instructions, decides whether it needs a tool, calls that tool, reads the result, and repeats until it can answer. To build an AI agent in Python you need four moving parts working together:

- **A model** — the reasoning engine (OpenAI, Anthropic, a local model via Ollama).
- **Tools** — functions the model can call to fetch data or take action.
- **A control loop** — the code that turns a tool request into an actual function call and feeds the result back.
- **Context** — the instructions, memory, and state that keep the agent on task.

The tutorials that age badly are the ones where you write all four by hand and tightly couple them. Promptise Foundry's `build_agent()` factory gives you the loop, the model abstraction, and automatic tool discovery in one call, so the code below stays the same whether you have one tool or fifty. For the full conceptual picture, the [building agents guide](../../guides/building-agents.md) walks through each part in order.

## Build an LLM agent in Python in about 15 lines

Start with the smallest thing that is still a real agent: an LLM with instructions and an execution loop. Install the package and set an API key first:

```bash
pip install promptise
export OPENAI_API_KEY=sk-...
```

Then the agent itself:

```python
import asyncio
from promptise import build_agent

async def main():
    agent = await build_agent(
        model="openai:gpt-5-mini",
        instructions="You are a helpful research assistant. Be concise.",
    )

    result = await agent.ainvoke({
        "messages": [{"role": "user", "content": "Explain what an AI agent is in two sentences."}]
    })
    print(result["messages"][-1].content)

    await agent.shutdown()

asyncio.run(main())
```

That is a complete, runnable Python AI agent tutorial in fewer than fifteen lines. `build_agent()` initializes the model, formats messages, runs the reasoning loop, and returns the final message. There is no framework ceremony and nothing to wire by hand yet — but there is also nothing this agent can *do* beyond talk. Tools change that.

## Build a Python agent with tools using automatic MCP discovery

This is where hand-rolled tutorials fall apart. Normally you would write a JSON schema for each function, register it with the model's tool-calling API, parse the model's tool request, dispatch to the right function, and serialize the result back — per tool, per model. Promptise Foundry replaces all of that with the Model Context Protocol (MCP). You define a tool as a plain typed function; the schema is generated from the type hints; and `build_agent()` **discovers** every tool on a server at startup and makes it callable.

Here is a self-contained example. The tool server and the agent live in the same file, and the agent finds the tools on its own:

```python
import asyncio
import sys
from promptise import build_agent
from promptise.config import StdioServerSpec
from promptise.mcp.server import MCPServer

# --- Define tools as plain typed functions ---
server = MCPServer("research-tools")

@server.tool()
async def get_stock_price(symbol: str) -> str:
    """Return the latest price for a ticker symbol."""
    prices = {"AAPL": 228.5, "MSFT": 465.2}
    return f"{symbol}: ${prices.get(symbol, 0.0)}"

@server.tool()
async def word_count(text: str) -> int:
    """Count the words in a block of text."""
    return len(text.split())

# Save the block above as tools.py, then run the agent below.

async def main():
    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={
            "tools": StdioServerSpec(command=sys.executable, args=["tools.py"]),
        },
        instructions="You are a research assistant. Use tools when they help.",
    )

    result = await agent.ainvoke({
        "messages": [{"role": "user", "content": "What is Apple's stock price right now?"}]
    })
    print(result["messages"][-1].content)

    await agent.shutdown()

asyncio.run(main())
```

Notice what you did *not* write: no tool schema, no JSON parsing, no dispatch table. You pointed `build_agent()` at a server, and it discovered `get_stock_price` and `word_count`, converted their type hints into typed tools, and started calling them when the query warranted. This is the core of building a **Python agent with tools** the maintainable way — adding a tool is one decorated function, not a schema-and-dispatch chore. If you want the mechanics of the tool-calling loop itself, the companion post [Tool Calling in Python: Connect an LLM to Tools](tool-calling-in-python.md) goes deeper.

MCP also means your tools are not locked to this one agent. Any MCP-speaking client can use the same server, and you can point the agent at remote servers over HTTP just as easily as local ones by swapping `StdioServerSpec` for `HTTPServerSpec`.

## Stay model-agnostic: swap providers without a rewrite

The second thing hand-rolled tutorials get wrong is hard-coding one provider's SDK. When your prompt logic, tool wiring, and provider client are tangled together, switching models means a rewrite. `build_agent()` is model-agnostic by design — the `model` argument is just a string, and everything else stays identical:

```python
# Hosted OpenAI
agent = await build_agent(model="openai:gpt-5-mini", instructions="...")

# Anthropic
agent = await build_agent(model="anthropic:claude-sonnet-4.5", instructions="...")

# Fully local, air-gapped
agent = await build_agent(model="ollama:llama3", instructions="...")
```

The same agent code, the same tools, the same loop — only the string changes. That is what makes this a factory rather than a single-provider wrapper: your investment in tools and instructions carries across providers, so you can prototype on a cheap hosted model and move sensitive workloads to a local one without touching the rest of your app.

## From a script to production: what you don't have to build

The examples above are complete agents, but a script that runs once is not a production system. The honest gap between a tutorial agent and a deployed one is memory, persistence, safety, and observability — and each of those is a one-parameter addition on the same factory call rather than a new subsystem you write:

- `memory=` — a vector memory provider so the agent recalls earlier context.
- `conversation_store=` — durable multi-turn sessions (SQLite, Postgres, Redis).
- `guardrails=True` — local scanners for prompt injection, PII, and secrets.
- `observe=True` — a timeline of every LLM turn and tool call for debugging.

You do not have to adopt any of these to get started, and you do not have to re-architect to add them later. The [core agents reference](../../core/agents/building-agents.md) documents each parameter with its trade-offs, and the [building an AI agent guide](../../guides/building-agents.md) shows how they compose in a realistic app. The point of a factory function is that the fifteen-line agent and the production agent are the *same* function call with more arguments filled in.

## When a lighter setup is the better fit

A framework is not always the right call, and it is worth being honest about that. If your entire task is a single stateless prompt with no tools — summarize this text, classify this ticket — then calling a provider SDK directly is simpler and adds no dependency. You reach for `build_agent()` when you need the agent *loop*: tool use, multiple turns, memory, session persistence, or the ability to swap models later. If you are still deciding whether an agent framework earns its place in your stack at all, [What Is a Python AI Agent Framework? (And When You Need One)](python-ai-agent-framework.md) lays out the decision without the sales pitch.

## Frequently asked questions

### Do I need to write tool schemas by hand to build an agent in Python?

No. With Promptise Foundry you define each tool as a typed Python function decorated with `@server.tool()`, and the JSON schema is generated automatically from the type hints. `build_agent()` discovers the tools from the MCP server at startup, so there is no manual schema authoring, registration, or dispatch code.

### Can I build an AI agent in Python without OpenAI?

Yes. The `model` argument is a provider-prefixed string, so `anthropic:claude-sonnet-4.5` and `ollama:llama3` work with the exact same agent code as `openai:gpt-5-mini`. Local models via Ollama let you run an agent fully offline for air-gapped or privacy-sensitive use.

### What is the difference between an LLM call and an AI agent?

A single LLM call returns text and stops. An LLM agent runs a loop — it can decide to call a tool, read the result, and continue reasoning until it can answer. That control loop, plus tools and context, is what turns a raw model into an agent, and it is exactly what `build_agent()` provides.

## Next steps

Start with the [5-minute Quickstart](../../getting-started/quickstart.md) and have a working agent before you finish your coffee. When you are ready to go past a single script, follow the [building agents guide](../../guides/building-agents.md) to add tools, memory, and persistence one parameter at a time.
