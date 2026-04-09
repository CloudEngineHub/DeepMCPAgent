# Promptise Foundry

The production framework for agentic AI systems. Build agents that discover tools, reason with custom patterns, remember context, enforce security, and run autonomously.

```bash
pip install promptise
```

## Quick Start

```python
import asyncio
from promptise import build_agent

async def main():
    agent = await build_agent(
        model="openai:gpt-4o-mini",
        instructions="You are a helpful assistant.",
    )
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Hello!"}]}
    )
    print(result["messages"][-1].content)
    await agent.shutdown()

asyncio.run(main())
```

See the full [Quick Start](getting-started/quickstart.md) for tools, reasoning patterns, and production features.

## Five Subsystems

| Subsystem | What it does | Start here |
|-----------|-------------|------------|
| **[Agent](core/index.md)** | Turn any LLM into a production agent. MCP tool discovery, memory, guardrails, semantic cache, streaming, approval workflows. | [Building Agents](guides/building-agents.md) |
| **[Reasoning Engine](core/engine.md)** | Design how your agent thinks. 20 composable nodes, 7 prebuilt patterns, 16 typed flags, 0.02ms overhead. Custom reasoning patterns for any task. | [Custom Reasoning](guides/custom-reasoning.md) |
| **[MCP Server](mcp/index.md)** | Build tool APIs that agents call. JWT auth, guards, middleware, rate limiting, audit logs, TestClient. The FastAPI of MCP. | [Building Servers](guides/production-mcp-servers.md) |
| **[Agent Runtime](runtime/index.md)** | Run agents autonomously. Cron triggers, crash recovery, budget enforcement, health monitoring, mission tracking, distributed coordination. | [Runtime Systems](guides/agentic-runtime.md) |
| **[Prompting](prompting/index.md)** | Prompts as software. Typed blocks with token budgeting, conversation flows, composable strategies, guards, version control, testing. | [Prompt Engineering](guides/prompt-engineering.md) |

## Hands-On Labs

Complete, runnable tutorials for real-world use cases:

- [Lab: Customer Support Agent](guides/lab-customer-support.md) — KB search, conversation phases, escalation, guardrails
- [Lab: Data Analysis Agent](guides/lab-data-analysis.md) — SQL queries, cross-table analysis, specialized reasoning pattern
- [Lab: Code Review Agent](guides/lab-code-review.md) — Adversarial critique, per-node models, security scanning

## Install

**Python 3.10+** required.

```bash
pip install promptise
```

Set your LLM provider key:

```bash
export OPENAI_API_KEY=sk-...
```

Verify:

```bash
python -c "from promptise import build_agent; print('Ready')"
```

## Next Steps

- [Quick Start](getting-started/quickstart.md) — Build your first agent in 5 minutes
- [Model Setup](getting-started/model-setup.md) — Configure OpenAI, Anthropic, Ollama, or any provider
- [Key Concepts](getting-started/concepts.md) — Architecture and design principles
- [Reasoning Engine](core/engine.md) — The custom execution runtime that powers every agent
