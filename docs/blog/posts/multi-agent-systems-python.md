---
title: "How to Build Multi-Agent Systems in Python: 2026 Guide"
description: "The hub page most ranking results skip: instead of hand-waving about 'orchestration', it shows the four concrete coordination primitives (shared MCP servers…"
keywords: "multi-agent systems python, how to build a multi-agent system, ai agent teams, agent orchestration python, multi-agent architecture"
date: 2026-07-16
slug: multi-agent-systems-python
categories:
  - Use Cases
---

# How to Build Multi-Agent Systems in Python: 2026 Guide

Building multi-agent systems in Python is rarely blocked by the modeling — the hard part is coordinating several agents without turning your codebase into a tangle of ad-hoc HTTP calls and shared globals. Most guides stop at the word "orchestration" and hand you a box diagram. This one skips the hand-waving: you'll learn the four concrete coordination primitives Promptise Foundry gives you, see runnable code for cross-agent delegation over HTTP+JWT, and leave with a decision table so you can pick a topology instead of copying someone else's.

<!-- more -->

## When you actually need a multi-agent system

Start with the honest question: do you need more than one agent at all? A single, well-scoped agent with good tools handles far more than people expect. If your problem is "answer support tickets" or "review a pull request," reach for one agent first — the walkthrough in [How to Build a Customer Support AI Agent](customer-support-ai-agent.md) ships a production system with exactly one.

Multi-agent architecture earns its complexity when you have **distinct roles with distinct permissions or context windows** that shouldn't bleed into each other:

- A researcher that can hit the web but must never touch production data.
- An analyst that reads findings but shouldn't publish.
- A writer that publishes but shouldn't run arbitrary code.

Splitting these into separate agents keeps each one's tool surface small and its authority narrow. It also keeps token cost sane: Promptise's semantic tool optimization can trim each agent's exposed tools by 40–70% fewer tokens per query, and fewer roles per agent means fewer tools to filter.

**When another tool is the better fit:** if your workflow is a fixed directed graph with human-in-the-loop approval chains and complex branching, a dedicated orchestration engine (a workflow runner, a durable-execution framework) will serve you better than agent-to-agent messaging. Multi-agent primitives shine for *dynamic* delegation where the LLM decides who to ask; they are not a replacement for a deterministic DAG scheduler.

## The four coordination primitives (multi-agent architecture)

Promptise gives you four building blocks. Every AI agent team you build is some combination of them — there is no fifth secret pattern.

1. **Shared MCP servers** — multiple agents connect to the same tool server, which enforces access per agent via JWT roles and rate limits. Use when agents need the *same tools* with *different permissions*.
2. **Cross-agent delegation** — one agent calls another as if it were a tool (`ask_peer`) or fans a question out to several at once (`broadcast`). Use when one agent needs a specific answer from another.
3. **EventBus messaging** — decoupled pub/sub. Agents publish events; others react by topic without knowing who produced them. Use for loose coupling and pipelines.
4. **Shared context and state** — an `AgentContext` object with per-agent write permissions that several agents read and mutate. Use when agents collaborate on a common document or state.

The [multi-agent teams guide](../../guides/multi-agent-teams.md) covers all four with a full research-pipeline example. The rest of this post drills into the two that most people get wrong: delegation and runtime coordination.

## Cross-agent delegation with ask_peer and broadcast

Delegation is the primitive that makes a *team* rather than a pile of independent agents. In Promptise, you register peers on `build_agent()` through the `cross_agents` parameter. Each peer is reachable over HTTP with JWT authentication, so peers can live in the same process during development or on separate machines in production without changing your calling code.

Here is a runnable coordinator that delegates to two remote specialists:

```python
import asyncio
from promptise import build_agent
from promptise.config import HTTPServerSpec


async def main():
    # A coordinator that can delegate to two remote specialist agents.
    coordinator = await build_agent(
        model="openai:gpt-5-mini",
        servers={"docs": HTTPServerSpec(url="http://localhost:8002/mcp")},
        cross_agents={
            "researcher": "http://localhost:9001",    # remote peer over HTTP+JWT
            "fact_checker": "http://localhost:9002",
        },
        instructions=(
            "You coordinate a research team. Delegate open questions to the "
            "researcher and verification to the fact_checker, then synthesize."
        ),
    )

    # 1) Delegate to a single peer and await its answer.
    answer = await coordinator.ask_peer(
        "researcher",
        "Find the top 3 papers on agent orchestration from 2025",
    )

    # 2) Fan out the same question to several peers in parallel.
    #    A slow or failing peer degrades gracefully instead of blocking.
    verdicts = await coordinator.broadcast(
        ["researcher", "fact_checker"],
        "Verify this claim: MCP is becoming the default for agent tool use",
        timeout=30.0,
    )

    print(answer)
    print(verdicts)  # {"researcher": "...", "fact_checker": "..."}

    await coordinator.shutdown()


asyncio.run(main())
```

Two things make this production-grade rather than a toy. First, the LLM can also invoke delegation itself — passing `cross_agents` auto-generates tools the coordinator's model can choose to call mid-conversation, so you get *dynamic* routing, not just the hard-coded `ask_peer` calls above. Second, `broadcast` isolates failures per peer: if the fact-checker times out, the researcher's answer still comes back, and the timed-out slot is reported rather than raised. For the full parameter reference — timeouts, per-call context injection, and disabling the broadcast tool — see the [cross-agent delegation docs](../../core/agents/cross-agent.md).

A common error-handling pattern is to fall back to local work when a peer is unavailable:

```python
try:
    answer = await coordinator.ask_peer("researcher", question, timeout=30.0)
except TimeoutError:
    # Peer didn't respond — answer it locally instead of failing the run.
    result = await coordinator.ainvoke({"messages": [{"role": "user", "content": question}]})
    answer = result["messages"][-1].content
```

## Agent orchestration in Python with AgentRuntime and a shared EventBus

Delegation covers request/response. For long-running teams — agents that wake on a schedule, react to events, and survive restarts — you graduate to the **Agent Runtime**. `AgentRuntime` supervises multiple `AgentProcess` instances and wires them to a shared `EventBus`, so agent orchestration in Python stops being a pile of `asyncio` tasks you babysit.

```python
from promptise.runtime import AgentRuntime, AgentProcess
from promptise.runtime.triggers import EventTrigger

runtime = AgentRuntime()

runtime.add_process(AgentProcess(
    name="researcher",
    agent_config={
        "model": "openai:gpt-5-mini",
        "servers": shared_servers,
        "instructions": "Research the topic and publish findings as an event.",
    },
))

# The analyst wakes only when the researcher emits "research.complete".
runtime.add_process(AgentProcess(
    name="analyst",
    agent_config={
        "model": "openai:gpt-5-mini",
        "servers": shared_servers,
        "instructions": "Evaluate the findings and rate their quality 1-5.",
    },
    triggers=[EventTrigger(event_type="research.complete")],
))

await runtime.start_all()
await runtime.event_bus.emit("research.start", {"topic": "AI safety trends 2026"})
```

The value here is decoupling. The researcher never imports the analyst; it emits an event and moves on. You add a writer that listens for `analysis.approved` later without touching either existing agent — the same way you'd add a subscriber to a message bus. Under the hood every state transition and trigger is journaled, so a crashed process restarts from its last good checkpoint rather than losing the run. The [multi-agent teams guide](../../guides/multi-agent-teams.md) shows the complete researcher → analyst → writer pipeline with quality gates between stages.

## How to build a multi-agent system that scales: choosing a topology

The mistake that sinks most first attempts is reaching for the heaviest primitive. Match the primitive to the actual need:

| You need... | Reach for | Why |
|---|---|---|
| Same tools, different permissions per agent | **Shared MCP server** | Central auth, rate limits, and audit logging in one place |
| A specific answer from one named agent | **`ask_peer()`** | Simple request/response, awaited inline |
| The same input processed by several agents | **`broadcast()`** | Parallel fan-out with per-peer timeouts |
| Reactions to events without tight coupling | **EventBus** | Publishers don't know their subscribers |
| A shared document several agents edit | **`AgentContext`** | Per-agent write permissions guard state |
| A fixed DAG with human approvals | **External orchestration** | Deterministic scheduling beats LLM-chosen routing |

A practical progression: start with one agent, split off a second specialist behind `ask_peer` when a role clearly needs different permissions, add a shared MCP server once two agents want the same tools, and only introduce the EventBus when you have three or more agents reacting to each other. Teams that grew this way — from one agent to a coordinated system — are collected in the [Promptise showcase](../../resources/showcase.md), which is a useful sanity check on how far a topology should stretch before you split it.

Whatever the shape, keep two invariants: every agent authenticates (no anonymous peers), and every tool call is auditable. Multi-agent systems fail quietly when an agent gains authority no one meant to grant it — narrow roles and shared, audited tool servers are what keep that from happening.

## Frequently asked questions

### What is the difference between an agent and a multi-agent system?

A single agent is one LLM loop with its own tools, memory, and instructions. A multi-agent system is two or more such agents that coordinate — by sharing tool servers, delegating with `ask_peer`/`broadcast`, exchanging events, or reading and writing shared state. You move to multiple agents when roles need genuinely different permissions or context, not just to feel more sophisticated.

### Do I need a message queue or an orchestration framework to build one?

No. Promptise's `AgentRuntime` ships an in-process `EventBus`, journals, and lifecycle management, so a small-to-medium team needs no external broker. Reach for a dedicated orchestration engine only when your workflow is a fixed DAG with human-in-the-loop approval chains — deterministic scheduling that is deliberately outside the scope of agent-to-agent messaging.

### How do agents authenticate to each other?

Cross-agent delegation uses HTTP with JWT authentication, and shared MCP servers enforce roles per agent through the same token mechanism. Each agent carries a bearer token that encodes its roles and (optionally) its tenant, so a researcher agent literally cannot call a `publish` tool guarded for writers. See the [cross-agent delegation docs](../../core/agents/cross-agent.md) for the delegation side and the multi-agent teams guide for shared-server auth.

## Next steps

Skim the four coordination primitives above, then follow the [multi-agent teams guide](../../guides/multi-agent-teams.md) to wire your first two-agent system end to end. If you're new to Promptise, start with the [Quick Start](../../getting-started/quickstart.md) to get one agent talking, then split off a second specialist behind `ask_peer` the moment a role needs its own permissions.
