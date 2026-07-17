---
title: "Promptise Foundry vs LangGraph: Graph vs Runtime"
description: "LangGraph gives you a graph; you still build persistence, triggers and crash recovery yourself. The honest take: LangGraph's checkpointing is excellent for…"
keywords: "LangGraph vs Promptise, LangGraph alternative, LangGraph vs Promptise Foundry, stateful agent orchestration, LangGraph checkpointing"
date: 2026-07-16
slug: langgraph-vs-promptise
categories:
  - Comparisons
---

# Promptise Foundry vs LangGraph: Graph vs Runtime

If you are weighing **LangGraph vs Promptise**, you are really comparing two different layers of the stack: a graph library for wiring up agent control flow versus a runtime for keeping agents alive, triggered, and recoverable in production. Both are good at what they do, and they are not strictly competitors. This article draws the line honestly — where a graph is enough, where you end up rebuilding a runtime by hand, and how to build a crash-recoverable process in Promptise. By the end you will know which layer your project actually needs.

## What LangGraph gives you: a graph

LangGraph is a library for building stateful graphs of LLM calls. You define nodes, edges, and conditional routing, and it manages the state object as execution flows through the graph. Its checkpointing feature snapshots that state so a conversation can pause, resume, and support human-in-the-loop review. For multi-step conversational agents — a support bot that branches based on intent, a research loop that fans out and reduces — that model is clean and expressive.

Promptise Foundry approaches control flow from the same place but hides more of it behind a single factory. You call `build_agent()`, pick an `agent_pattern` (`react`, `verify`, `deliberate`, `debate`, `pipeline`, and more), or hand it a custom `PromptGraph`, and you get a ready agent with tool discovery, memory, and guardrails already wired. So on the pure "shape the reasoning" axis, the two overlap heavily. If graphs are all you need, LangGraph is a mature, well-documented choice and a reasonable default.

The interesting question is what happens after the graph returns.

## Stateful agent orchestration is more than a graph

The gap shows up the moment your agent needs to run without a human holding the loop open. Real deployments ask for things a graph library does not set out to solve:

- The agent must wake up on a **cron schedule**, an **inbound webhook**, a **file landing in a directory**, or an **internal event** — not just a synchronous request.
- The process must **survive a restart**. If the box reboots mid-task, it should resume, not lose the thread.
- You need **governance**: budgets on tool calls and cost, health checks for stuck or looping behavior, and mission tracking.
- Long-running work needs a **lifecycle** — created, running, suspended, stopped, failed — that you can inspect and control.

None of that is a graph concern. It is *stateful agent orchestration* at the process level, and with a graph library you assemble it yourself from a scheduler, a database, a supervisor, and a pile of glue. Promptise packages it as the fourth pillar of the framework: the **Agent Runtime**. The [Why Promptise](../../getting-started/why-promptise.md) page frames the same distinction — the framework's job is to remove the undifferentiated plumbing so you ship behavior, not infrastructure.

The Agent Runtime wraps a Promptise agent in an `AgentProcess` — a lifecycle container with a trigger queue, heartbeat, and conversation buffer — and an `AgentRuntime` supervises many processes on a shared event bus. Five trigger types ship in the box:

- `CronTrigger` — cron expressions for scheduled runs
- `EventTrigger` — subscribe to the internal event bus
- `MessageTrigger` — topic-based pub/sub with wildcards
- `WebhookTrigger` — HTTP POST with HMAC verification
- `FileWatchTrigger` — directory monitoring with glob patterns

You compose several triggers on one process, and the runtime handles the queueing and concurrency. That is the layer LangGraph deliberately leaves to you.

## LangGraph checkpointing vs journals and ReplayEngine

**LangGraph checkpointing** and Promptise's journals solve overlapping but different problems, and this is where the comparison is most useful.

Checkpointing in LangGraph snapshots graph *state* so a thread can pause and resume — ideal for interruptible conversations and approval steps. It is the mechanism that makes a conversational graph feel durable between turns.

Promptise journals record the *process*. Every state transition, trigger event, and invocation result is appended to an `InMemoryJournal` or a `FileJournal`. On restart, the `ReplayEngine` reconstructs the process from its last checkpoint plus the replayed journal, so a cron- or webhook-triggered agent that crashed mid-run comes back to its last known-good state rather than starting cold. The unit of recovery is the whole long-lived process, not a single conversation thread.

The honest summary: LangGraph checkpointing is excellent for durable *conversations*. Journals and `ReplayEngine` are built for durable *processes*. If your agent is a request/response graph, checkpointing may be all the durability you need. If it is a daemon that fires on a schedule and must outlive reboots, that is a runtime concern a graph library was never meant to own.

## Build a recoverable process in Promptise

Start where the durability actually lives: persistent conversation state. Point `build_agent()` at a conversation store and use `chat()`, which loads history, invokes, and persists automatically. Because the state is on disk, a fresh process — including one that restarts after a crash — resumes the same session.

```python
import asyncio
from promptise import build_agent, CallerContext
from promptise.conversations import SQLiteConversationStore

async def main():
    agent = await build_agent(
        model="openai:gpt-5-mini",
        instructions="You are an ops assistant tracking a nightly deployment checklist.",
        conversation_store=SQLiteConversationStore("ops.db"),
        agent_pattern="react",
        observe=True,  # timeline of every LLM turn and tool call
    )

    caller = CallerContext(user_id="alice", roles=["operator"])

    # State for session "nightly-ops" is written to ops.db.
    # Restart the process and the same session_id resumes where it left off.
    reply = await agent.chat(
        "Start the nightly backup and record the start time.",
        session_id="nightly-ops",
        caller=caller,
    )
    print(reply)

    await agent.shutdown()

asyncio.run(main())
```

That agent is already durable across restarts at the conversation level. The runtime layer promotes it into a governed, triggered process:

```python
from promptise.runtime import AgentProcess, AgentRuntime
```

You wrap the agent in an `AgentProcess`, attach a `FileJournal` for crash recovery, and add one or more triggers so it runs on its own — a `CronTrigger` for the nightly job, a `WebhookTrigger` for on-demand runs. In practice you usually declare all of this in an `.agent` manifest (model, instructions, servers, triggers, journal, budget, health, mission) and load it, so the process config lives in version control rather than in code. The end-to-end walkthrough — building the process, wiring triggers, and enabling `ReplayEngine` recovery — is in the [building agents guide](../../guides/building-agents.md).

## When LangGraph is the better fit

Being honest about the boundary matters more than winning a comparison.

**Reach for LangGraph when:**

- Your agent is fundamentally a **request/response graph** with intricate branching, and you do not need cron, webhook, or file triggers.
- You are already invested in the LangChain ecosystem and want the tightest integration with its graph tooling and community patterns.
- You want fine-grained, explicit control over every node and edge, and you prefer assembling persistence and scheduling from components you choose yourself.
- Conversation-level checkpointing is all the durability your use case requires.

**Reach for Promptise Foundry when:**

- You need agents that **run unattended** — triggered by schedule, webhook, event, message, or file — and **survive restarts** without you building a supervisor and scheduler.
- You want governance (budgets, health checks, missions) and MCP-native tool discovery as first-class, not add-ons.
- You would rather configure a manifest than hand-roll process lifecycle and crash recovery.

If you are still narrowing the field, our [2026 checklist for choosing an agent framework](choosing-an-agent-framework.md) and the broader [honest guide to the best AI agent framework](best-ai-agent-framework-2026.md) walk through the same trade-offs across more tools. Neither is a sales pitch — a graph library is often the right answer.

## Frequently asked questions

### Is Promptise a LangGraph alternative or a complement?

Both, depending on your layer. As a **LangGraph alternative**, Promptise's `agent_pattern` and `PromptGraph` cover the graph/control-flow job. As a complement, its Agent Runtime adds the process lifecycle, triggers, and crash recovery that sit *above* the graph — concerns LangGraph does not aim to solve.

### Does LangGraph checkpointing give me crash recovery?

For conversations, largely yes — checkpointing lets a thread pause and resume from saved state. What it does not give you is process-level recovery for a long-running, self-triggering daemon. Promptise's journals plus `ReplayEngine` reconstruct the whole process from its last checkpoint after a restart, which is a different and broader guarantee.

### Can I keep my LangGraph-style control flow in Promptise?

Yes. Use a built-in `agent_pattern` for common shapes, or define a custom `PromptGraph` with your own nodes when you need explicit control. You keep graph-based reasoning while gaining the runtime layer on top.

## Next steps

See when a runtime beats a graph, then build a recoverable process yourself. Start with the [Quick Start](../../getting-started/quickstart.md) to stand up an agent in a few lines, then follow the [building agents guide](../../guides/building-agents.md) to wrap it in an `AgentProcess` with triggers, journals, and `ReplayEngine` crash recovery.
