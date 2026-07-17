---
title: "How to Schedule an AI Agent with Cron Triggers"
description: "The default answer online is 'wrap your script in system crontab' — which restarts cold and loses memory, journal, and budget between runs. This shows how…"
keywords: "scheduled AI agent, cron AI agent, cron job AI agent, schedule LLM agent, recurring AI agent task, CronTrigger"
date: 2026-07-16
slug: scheduled-ai-agent
categories:
  - Runtime
---

# How to Schedule an AI Agent with Cron Triggers

Building a **scheduled AI agent** usually starts with the same advice you find everywhere: wrap your Python script in system crontab and walk away. That works until you notice the agent has no memory of yesterday's run, no record of what it did, and no ceiling on how much it can spend. This post shows you a better pattern — attach a `CronTrigger` to a persistent `AgentProcess` so every scheduled run shares a conversation buffer, a durable journal, and governance limits. By the end you will have a cron AI agent running on a real schedule in about five lines of configuration.

## Why crontab is the wrong home for an LLM agent

A system cron entry runs your script cold. The interpreter boots, your code builds an agent, does one thing, and the process exits. Nothing survives to the next run. For a shell script that rotates logs, that is exactly what you want. For an LLM agent, it quietly removes the four things that make scheduled autonomy safe:

- **No memory between runs.** The agent can't reference what it saw yesterday, so it re-derives context from scratch every time — more tokens, less continuity.
- **No journal.** If a 03:00 run fails, there is no durable record of what happened. You debug from stdout you probably didn't capture.
- **No budget.** A stuck agent looping over tool calls at midnight will keep calling until something external stops it. Cron won't.
- **No crash recovery.** A killed process is just gone. The next cron tick starts over with no knowledge that the last one died mid-task.

A schedule LLM agent needs a home that outlives a single tick. That home is a long-running process, and the [Agent Runtime](../../runtime/index.md) exists to provide exactly that: lifecycle management, triggers, persistent state, and governance around a stateless model. For the bigger picture on why stateless models need a process wrapper at all, see [What Is an Autonomous AI Agent Runtime?](autonomous-ai-agent-runtime.md).

## Schedule an AI agent with a CronTrigger

In Promptise Foundry you don't call `agent.ainvoke()` on a timer yourself. You declare a `CronTrigger` on an `AgentProcess`, start the process once, and let it wake itself on schedule. The process stays resident; the trigger fires; the agent runs; the process goes back to waiting.

Here is a complete, runnable scheduled AI agent. It wakes every day at 09:00, writes to a file-backed journal, and refuses to exceed 20 tool calls in a single run:

```python
import asyncio
from promptise.runtime import (
    AgentProcess,
    ProcessConfig,
    TriggerConfig,
    JournalConfig,
    BudgetConfig,
)


async def main():
    process = AgentProcess(
        name="daily-digest",
        config=ProcessConfig(
            model="openai:gpt-5-mini",
            instructions=(
                "Each morning, summarize what changed since your last run "
                "and note anything that needs a human's attention."
            ),
            triggers=[
                # 09:00 every day — a standard cron expression
                TriggerConfig(type="cron", cron_expression="0 9 * * *"),
            ],
            # Durable audit log — every trigger, transition, and result
            journal=JournalConfig(backend="file", path="./digest-journal"),
            # Hard ceiling so a stuck run can't spend unbounded tool calls
            budget=BudgetConfig(
                enabled=True,
                max_tool_calls_per_run=20,
                on_exceeded="pause",
            ),
        ),
    )

    await process.start()          # builds the agent, starts the trigger
    print(process.status())        # {'state': 'running', 'invocation_count': 0, ...}

    # The process now runs autonomously on the cron schedule.
    # Keep the runtime alive for as long as you want it scheduled:
    await asyncio.sleep(3600)

    await process.stop()


asyncio.run(main())
```

The `cron_expression` is a standard five-field cron string. `"0 9 * * *"` is daily at 09:00; `"*/5 * * * *"` is every five minutes; `"0 0 * * 0"` is weekly at midnight Sunday. Full expression support comes from the optional `croniter` dependency, and simple `*/N` intervals work without it. The trigger types and their payloads are documented in the [Triggers Overview](../../runtime/triggers/index.md).

That's the five-line core the CTA promises: a `TriggerConfig(type="cron", ...)` entry, wrapped in a `ProcessConfig`, wrapped in an `AgentProcess`. Everything else on the config is opt-in.

## What a persistent AgentProcess keeps between runs

The difference between this and a crontab entry is what survives across ticks. Because the `AgentProcess` stays resident, each scheduled invocation shares state that a cold restart would throw away:

- **Conversation buffer.** A rolling short-term memory of recent invocations, so this morning's run can reference the previous one without re-reading everything.
- **AgentContext.** A key-value store with an audit trail — every write records what changed and when. The context is injected into each invocation, so the agent always sees its accumulated state.
- **Journal.** With `JournalConfig(backend="file", ...)`, every state transition, trigger event, and invocation result is written to a durable log. If the process crashes, the `ReplayEngine` reconstructs its last known state from that journal.
- **Governance.** The `BudgetConfig` above tracks tool calls across the run and enforces the ceiling. Health and mission subsystems layer on the same way — all opt-in, zero overhead when disabled.

All of these are fields on the `ProcessConfig` you already saw. The full field reference, including restart policies and heartbeat tuning, lives in the [Agent Processes](../../runtime/processes.md) guide. The practical upshot: a scheduled run is no longer an isolated cold start. It is one tick in the life of a process that remembers, records, and self-limits.

## Compose cron with other triggers for one recurring AI agent task

A real agent rarely wants only a schedule. You often want it to run on a timer *and* react to the world in between. Because triggers are just a list on the process config, you can attach several to one `AgentProcess` — they all wake the same agent and share the same context, journal, and budget.

Here a single process runs a scheduled sweep every five minutes and also fires whenever a new CSV lands in an inbox directory:

```python
from promptise.runtime import ProcessConfig, TriggerConfig

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Reconcile the ledger on schedule and whenever new data arrives.",
    triggers=[
        TriggerConfig(type="cron", cron_expression="*/5 * * * *"),
        TriggerConfig(
            type="file_watch",
            watch_path="/data/inbox",
            watch_patterns=["*.csv"],
        ),
    ],
    concurrency=2,   # allow two trigger invocations to run at once
)
```

This is the pattern that a plain cron job for an AI agent can't express: one governed identity that both wakes on a clock and responds to events, without spinning up a fresh process for each. The runtime ships five trigger types in total — cron, webhook, file watch, event, and message — and you mix them freely. When you outgrow a single process and want to run many scheduled agents together under one supervisor, an `AgentRuntime` manages a fleet of `AgentProcess` instances with a shared event bus and central start/stop control. That progression from one process to a supervised long-running deployment is covered in [How to Build a Long-Running AI Agent](long-running-ai-agent.md).

## When system cron is the better fit

Be honest with yourself about the workload. If your scheduled job is genuinely stateless — call an API, transform a file, exit — and doesn't touch an LLM, then system crontab is simpler and you should use it. There is no reason to run a resident process for something that has no state worth keeping. The same goes for a one-shot script you trigger manually a few times a month; the ceremony of a runtime buys you nothing there.

The `AgentProcess` pattern earns its keep when three things are true at once: an LLM is in the loop, the runs relate to each other over time, and an unattended failure or a runaway loop would actually cost you something. That is when memory between runs, a durable journal, and an enforced budget stop being nice-to-haves and start being the difference between an agent you trust overnight and one you don't.

## Frequently asked questions

### How do I schedule an AI agent to run every day?

Add a `CronTrigger` to your agent's `ProcessConfig` with a daily cron expression such as `"0 9 * * *"` for 09:00, then call `await process.start()` once. The `AgentProcess` stays resident and invokes the agent on that schedule. Full cron-expression support requires the optional `croniter` package; simple `*/N` intervals work without it.

### Can a scheduled agent remember what it did on the previous run?

Yes. Because the `AgentProcess` is long-lived, each scheduled run shares the same conversation buffer and `AgentContext`, so the agent can reference earlier invocations. Enabling a file-backed journal also gives you crash recovery — the runtime replays the last known state after a restart. A raw system-crontab script gets none of this, since each tick is a cold start.

### What stops a scheduled AI agent from looping forever?

Attach a `BudgetConfig` to the process and set limits like `max_tool_calls_per_run`. When a run hits the ceiling, the runtime enforces your chosen action — `pause`, stop, or escalate — instead of letting the agent keep calling tools unattended. Budget is fully opt-in and adds no overhead when disabled.

## Next steps

Add a `CronTrigger` to your agent in five lines and schedule its first supervised run: drop a `TriggerConfig(type="cron", ...)` into a `ProcessConfig`, start the `AgentProcess`, and check `process.status()`. From there, work through the [Quick Start](../../getting-started/quickstart.md) to build the underlying agent, then read the [Agent Processes](../../runtime/processes.md) guide to layer on journals, health checks, and mission governance as you move toward production.
