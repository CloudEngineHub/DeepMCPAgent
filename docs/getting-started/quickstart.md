---
title: Quick Start — Build your first AI agent with Promptise Foundry in 5 minutes
description: Step-by-step guide to building your first production-ready AI agent in Python with Promptise Foundry. Includes installation, model setup, MCP tool discovery, and your first agent.run() call.
keywords: AI agent tutorial, Python agent quickstart, build AI agent, Promptise Foundry tutorial, getting started, MCP agent example
---

# Quick Start

Build your first agent in 5 minutes. No external servers needed — everything runs locally.

## Install

```bash
pip install promptise
export OPENAI_API_KEY=sk-...  # Or any supported provider
```

## Your First Agent (30 seconds)

The simplest possible agent — just an LLM with instructions:

```python
import asyncio
from promptise import build_agent

async def main():
    agent = await build_agent(
        model="openai:gpt-4o-mini",
        instructions="You are a helpful assistant. Be concise.",
    )

    result = await agent.ainvoke({
        "messages": [{"role": "user", "content": "What is 42 * 17?"}]
    })
    print(result["messages"][-1].content)  # "42 * 17 = 714"
    await agent.shutdown()

asyncio.run(main())
```

That's it. `build_agent()` handles model initialization, message formatting, and execution.

## Add Tools (2 minutes)

Agents become useful when they can call tools. Create an MCP server in the same file:

```python
import asyncio
import sys
from promptise import build_agent
from promptise.config import StdioServerSpec
from promptise.mcp.server import MCPServer

# ── Build a tool server ──
server = MCPServer("my-tools")

@server.tool()
async def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    # In production, call a real API
    return f"Sunny, 22°C in {city}"

@server.tool()
async def calculate(expression: str) -> str:
    """Evaluate a math expression."""
    return str(eval(expression))  # noqa: S307

# Save as tools.py, then:

async def main():
    agent = await build_agent(
        model="openai:gpt-4o-mini",
        servers={
            "tools": StdioServerSpec(
                command=sys.executable,
                args=["tools.py"],
            ),
        },
        instructions="You are a helpful assistant with access to tools.",
    )

    result = await agent.ainvoke({
        "messages": [{"role": "user", "content": "What's the weather in Berlin?"}]
    })
    print(result["messages"][-1].content)
    # "It's sunny and 22°C in Berlin!"

    await agent.shutdown()

if __name__ == "__main__":
    # If run directly, start the MCP server
    if "--serve" in sys.argv:
        server.run(transport="stdio")
    else:
        asyncio.run(main())
```

The agent discovers `get_weather` and `calculate` automatically — no manual tool definitions.

## Add a Custom Reasoning Pattern (3 minutes)

Instead of the default tool loop, define how your agent thinks:

```python
from promptise.engine import PromptGraph, PromptNode, NodeFlag
from promptise.engine.reasoning_nodes import ThinkNode, SynthesizeNode

agent = await build_agent(
    model="openai:gpt-4o-mini",
    servers=my_servers,
    agent_pattern=PromptGraph("analyst", nodes=[
        ThinkNode("think", is_entry=True),          # Analyze the question
        PromptNode("research", inject_tools=True),   # Use tools to gather data
        SynthesizeNode("answer", is_terminal=True),  # Produce final answer
    ]),
)
```

The agent now thinks before acting and synthesizes a structured answer — instead of jumping straight to tool calls.

**7 built-in patterns available:**

```python
agent = await build_agent(..., agent_pattern="react")       # Default tool loop
agent = await build_agent(..., agent_pattern="peoatr")      # Plan → Act → Think → Reflect
agent = await build_agent(..., agent_pattern="research")    # Search → Verify → Synthesize
agent = await build_agent(..., agent_pattern="autonomous")  # Agent picks from node pool
agent = await build_agent(..., agent_pattern="deliberate")  # Think → Plan → Act → Observe → Reflect
agent = await build_agent(..., agent_pattern="debate")      # Proposer ↔ Critic → Judge
agent = await build_agent(..., agent_pattern="pipeline")    # Sequential chain
```

## Add Production Features (4 minutes)

Each capability is one parameter:

```python
from promptise import build_agent, CallerContext
from promptise.memory import ChromaProvider
from promptise.cache import SemanticCache
from promptise.conversations import SQLiteConversationStore

agent = await build_agent(
    model="openai:gpt-4o-mini",
    servers=my_servers,

    # Security: block injection attacks, detect PII
    guardrails=True,

    # Memory: remember context across conversations
    memory=ChromaProvider(persist_directory="./memory"),

    # Cache: serve similar queries instantly (30-50% cost savings)
    cache=SemanticCache(),

    # Conversations: persist chat history
    conversation_store=SQLiteConversationStore("conversations.db"),

    # Observability: trace every tool call, token, and decision
    observe=True,
)

# Use with per-user identity
result = await agent.ainvoke(
    {"messages": [{"role": "user", "content": "Analyze last quarter's revenue"}]},
    caller=CallerContext(user_id="analyst-42", roles=["analyst"]),
)
```

## What Happens Inside

When you call `ainvoke()`, this pipeline runs:

```
User message
    → Input guardrails (block injection, flag PII)
    → Memory search (inject relevant past context)
    → Cache check (return instantly if similar query cached)
    → Reasoning Engine (execute your reasoning pattern)
        → Tool discovery (auto-inject MCP tools)
        → LLM call (with system prompt, tools, context)
        → Tool execution (parallel when 2+ calls)
        → Loop until done (or budget exhausted)
    → Output guardrails (redact PII, credentials)
    → Cache store (save for future similar queries)
    → Conversation persist (store in SQLite/Postgres/Redis)
    → Return response
```

Every step is opt-in. Features you don't enable have zero overhead.

---

## Next Steps

| Want to... | Go to... |
|---|---|
| Use Claude, Gemini, Ollama, or local models | [Model Setup](model-setup.md) |
| Understand the architecture | [Key Concepts](concepts.md) |
| Design custom reasoning patterns | [Reasoning Patterns](../core/agents/reasoning-patterns.md) |
| Build a complete production agent | [Building Agents Guide](../guides/building-agents.md) |
| Build MCP tool servers | [Building MCP Servers](../guides/production-mcp-servers.md) |
| Build a customer support agent | [Lab: Customer Support](../guides/lab-customer-support.md) |
| Build a data analysis agent | [Lab: Data Analysis](../guides/lab-data-analysis.md) |
| Build a code review agent | [Lab: Code Review](../guides/lab-code-review.md) |
