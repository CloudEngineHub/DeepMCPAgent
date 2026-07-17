---
title: "LangGraph vs Promptise: Long-Running Agents"
description: "An honest split of concerns: LangGraph is the better choice when you need fine-grained graph orchestration and checkpointing inside a single invocation …"
keywords: "LangGraph vs Promptise, LangGraph long-running agents, durable agent execution comparison, agent orchestration vs agent runtime, best framework for autonomous agents, agent checkpointing comparison"
date: 2026-07-16
slug: langgraph-vs-promptise-2
categories:
  - Runtime
---

# LangGraph vs Promptise: Long-Running Agents

If you are researching **LangGraph vs Promptise** for an agent that has to stay alive for hours or days — not just answer one request and exit — you are comparing two different layers of the stack, and it pays to be precise about which one you actually need. LangGraph excels at orchestrating the control flow *inside* a single invocation. Promptise's Agent Runtime targets the layer *above* the graph: an OS-level process that wakes on triggers, survives crashes, and stays under governance. By the end of this post you will be able to tell which layer your project needs, and you will have runnable code for a supervised, crash-recoverable agent.

<!-- more -->

## What LangGraph does well: the graph layer

LangGraph is a library for building stateful graphs of LLM calls. You define nodes, edges, and conditional routing, and it threads a state object through the graph as execution proceeds. Its checkpointer snapshots that state so a run can pause, resume, and support human-in-the-loop review inside one execution. For a support bot that branches on intent, or a research loop that fans out and reduces, that model is clean and expressive.

Promptise approaches control flow from the same place but hides more of it behind one factory. You call `build_agent()`, choose an `agent_pattern` (`react`, `verify`, `deliberate`, `debate`, `pipeline`, and more), or hand it a custom `PromptGraph`, and you get an agent with tool discovery, memory, and guardrails already wired. On the pure "shape the reasoning" axis, the two overlap heavily. If a graph is genuinely all you need, LangGraph is mature and well documented — a reasonable default.

The interesting question is what happens *after* the graph returns.

## Agent orchestration vs agent runtime: the real dividing line

The gap opens the moment your agent has to run without a human holding the loop open. Once an agent needs to live in production unattended, it needs things a graph library does not set out to solve:

- **Reactivity** — wake up on a cron schedule, an inbound webhook, a file landing in a directory, or an internal event, not just a synchronous request.
- **Resilience** — survive a process restart. If the box reboots mid-task, resume rather than lose the thread.
- **Governance** — budgets on tool calls and cost, health checks for stuck or looping behavior, and mission tracking.
- **Lifecycle** — a `created → running → suspended → stopped → failed` state machine you can inspect and control.

None of that is a graph concern. It is orchestration at the *process* level, and with a bare graph library you assemble it yourself from a scheduler, a database, a supervisor, and glue. Promptise packages it as the fourth pillar of the framework — the [Agent Runtime](../../runtime/index.md). It wraps a Promptise agent in an `AgentProcess` (a lifecycle container with a trigger queue, heartbeat, and conversation buffer) and supervises many processes under one `AgentRuntime` on a shared event bus. Five trigger types ship in the box: cron, webhook, file-watch, event, and message. For the conceptual background on why this layer exists at all, see [What Is an Autonomous AI Agent Runtime?](autonomous-ai-agent-runtime.md).

## Durable agent execution comparison: checkpoints vs journals

This is where the two frameworks are most often conflated, so a careful durable agent execution comparison matters. Both persist state, but they persist *different* state for *different* failure modes.

- **LangGraph checkpoints** snapshot the graph's state object at each super-step so a single run can pause and resume — ideal for human-in-the-loop and mid-conversation recovery *within* one invocation.
- **Promptise journals** record the full history of a *process* — every state transition, trigger firing, and invocation result — across many invocations over the process's whole lifetime.

The distinction drives crash recovery. When a Promptise process dies unexpectedly, the `ReplayEngine` reads the journal, finds the last checkpoint entry, and replays subsequent entries to reconstruct the process's context, lifecycle position, and conversation history — then restart policies bring it back automatically. That is recovery of a *long-lived supervised process*, not just a paused graph. The [journal system](../../runtime/journal/index.md) offers three detail levels — `none`, `checkpoint` (default), and `full` — so you trade storage against forensic depth. So in an agent checkpointing comparison the honest framing is not "which is better" but "which layer": LangGraph checkpoints a run; Promptise journals a process.

## Build a long-running, crash-recoverable agent

Here is the process layer end to end. This example supervises an agent that reacts to both a cron schedule and inbound alerts, journals every cycle for crash recovery, restarts itself on failure, and stays inside an autonomy budget. Every field below is a real `ProcessConfig` option.

```python
import asyncio
from promptise.runtime import (
    AgentRuntime, ProcessConfig, TriggerConfig,
    JournalConfig, BudgetConfig, ToolCostAnnotation,
)

async def main():
    async with AgentRuntime() as runtime:
        await runtime.add_process(
            "pipeline-guardian",
            ProcessConfig(
                model="openai:gpt-5-mini",
                instructions="Watch the data pipeline and remediate incidents.",
                servers={"ops": {"type": "http", "url": "http://localhost:8000/mcp"}},
                triggers=[
                    TriggerConfig(type="cron", cron_expression="*/5 * * * *"),
                    TriggerConfig(type="webhook", webhook_path="/alerts", webhook_port=9090),
                ],
                concurrency=2,

                # Crash recovery: journal every cycle, restart on failure
                journal=JournalConfig(level="checkpoint", backend="file", path="./journal"),
                restart_policy="on_failure",
                max_restarts=5,

                # Governance: cap what the agent may do autonomously
                budget=BudgetConfig(
                    enabled=True,
                    max_tool_calls_per_run=20,
                    max_cost_per_day=50.0,
                    on_exceeded="escalate",
                    tool_costs={
                        "restart_service": ToolCostAnnotation(cost_weight=5.0, irreversible=True),
                        "read_metrics": ToolCostAnnotation(cost_weight=0.5),
                    },
                ),
            ),
        )
        await runtime.start_all()
        print(runtime.status())      # per-process state, invocation counts, trigger status
        # ... the process now runs on its own until you stop it ...
        await runtime.stop_all()

asyncio.run(main())
```

Notice what you did *not* write: no scheduler, no supervisor loop, no serialization code for recovery, no database schema. Process supervision, journaling, and the `ReplayEngine` are the runtime's job. In LangGraph, the graph is yours to build — but the scheduler, the crash-recovery restart policy, and the governance envelope are all still homework.

## Governance keeps a multi-day agent supervised

An agent that lives for days is only safe if it cannot quietly run away. The `budget` block above is the [Autonomy Budget](../../runtime/governance/budget.md): explicit, enforced limits on tool calls, LLM turns, and cost, with per-tool cost weights and an `irreversible` flag for actions like restarting a service or issuing a refund. When the agent hits a limit, the runtime enforces your chosen action — `pause`, `stop`, or `escalate` (a webhook plus an event) — instead of letting the loop grind on. Budget is one of four opt-in governance subsystems, alongside behavioral health, the mission model, and secret scoping; all are disabled by default with zero overhead. This is the difference between an agent you *demo* and an agent you leave running over a weekend. For a full walkthrough of standing one up, follow [How to Build a Long-Running AI Agent](long-running-ai-agent.md).

## When LangGraph is the better fit

Being honest about a comparison means naming where the other tool wins. Reach for LangGraph, not the Promptise Agent Runtime, when:

- **Your unit of work is a single, complex invocation.** If the hard part is the branching, fan-out/reduce, and mid-run human review *inside* one request, LangGraph's graph model and checkpointer are purpose-built for exactly that.
- **You are already invested in the LangChain ecosystem** and want to stay within its abstractions, tooling, and community.
- **You do not need process-level concerns at all** — no triggers, no unattended lifetime, no crash-recovery-across-restarts. Adding a runtime you will not use is complexity for its own sake.

And the two are not mutually exclusive in spirit: LangGraph solves the *graph*, Promptise's runtime solves the *process*. If you need sophisticated in-invocation orchestration *and* an OS-level layer around it, you can shape the reasoning with Promptise's own `PromptGraph` while the runtime handles supervision — or keep your graph where it is and treat the comparison as a question of which layer you are missing today.

## Frequently asked questions

### Does LangGraph handle crash recovery for long-running agents?

LangGraph's checkpointer persists a graph's state so a single run can pause and resume, which covers mid-invocation recovery and human-in-the-loop. It does not, on its own, supervise an OS-level process across restarts, reschedule triggers, or reconstruct a process that died between invocations — that is what Promptise's journal plus `ReplayEngine` and restart policies provide.

### Is Promptise a drop-in replacement for LangGraph?

No, and it is not meant to be. They target different layers: LangGraph orchestrates control flow inside one invocation, while Promptise's Agent Runtime manages the process around many invocations. Promptise does offer in-invocation reasoning via `agent_pattern` and custom `PromptGraph`s, but if graph orchestration is your only need, LangGraph is a mature, focused choice.

### Which is the best framework for autonomous agents that run unattended?

For agents that must wake on schedules or webhooks, survive restarts, and stay under a budget without a human in the loop, you need a runtime — the process layer, not just a graph. Promptise's Agent Runtime provides that layer directly; with a graph-only library you would assemble the scheduler, supervisor, journal, and governance yourself.

## Next steps

See the split of concerns for yourself: run the [Quick Start](../../getting-started/quickstart.md) to build a first agent, then work through the [Agent Runtime](../../runtime/index.md) docs to feel the process layer — triggers, journaling, and governance — that this comparison is really about.
