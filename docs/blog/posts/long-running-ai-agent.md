---
title: "How to Build a Long-Running AI Agent"
description: "Most tutorials show a bare while True loop that loses all state and stops when the terminal closes. This walks through the real lifecycle container \u2026"
keywords: "long-running AI agent, persistent AI agent, always-on AI agent, agent process lifecycle, suspend and resume agent, AI agent that runs continuously"
date: 2026-07-16
slug: long-running-ai-agent
categories:
  - Runtime
---

# How to Build a Long-Running AI Agent

Building a long-running AI agent is where most tutorials quietly fall apart: they show you a `while True:` loop that calls the model, prints a reply, and forgets everything the moment the terminal closes. That loop is not a persistent AI agent — it's a script with a heartbeat problem, no crash recovery, and no way to run more than one thing at a time. By the end of this post you'll understand what a real lifecycle container manages for you, and you'll have a runnable Promptise Foundry agent that keeps living, remembering, and reacting across hours and restarts.

## Why a `while True` loop is not a persistent AI agent

The naive pattern looks fine in a demo:

```python
while True:
    event = get_next_event()
    reply = agent.invoke(event)     # blocks; state lives only in local vars
    print(reply)
```

It breaks the instant you ask anything real of it. There is no state that survives a restart, so a crash means starting from zero. It processes one event at a time, so a slow tool call stalls everything behind it. It has no health signal, so you can't tell a working agent from a hung one. And when the process dies — a deploy, an OOM kill, a closed SSH session — the agent dies with it and forgets every conversation it ever had.

An **always-on AI agent** needs the same things any long-lived process needs: a defined lifecycle, concurrency control, a heartbeat, buffered short-term memory, and a durable record it can replay after a crash. Writing all of that by hand is how a "quick agent script" turns into a broken distributed system. Promptise Foundry gives you the container instead.

## What the agent process lifecycle actually manages

In Promptise, the unit of a long-running AI agent is the **`AgentProcess`** — a lifecycle container that wraps a normal `PromptiseAgent` and adds everything the bare loop was missing. It follows a deterministic state machine (CREATED → STARTING → RUNNING → SUSPENDED → STOPPING → STOPPED, plus FAILED) where every transition is validated and recorded. Under the hood the same container gives you four things you'd otherwise hand-roll:

- **A trigger queue** — events (cron ticks, webhooks, file changes, messages from other agents) land in a queue instead of blocking the caller. The process drains it on its own schedule.
- **A heartbeat** — a periodic liveness signal so you can tell a healthy agent from a stuck one, and so idle detection has a clock to work against.
- **A concurrency semaphore** — a bounded number of trigger events can be handled in parallel. Set `concurrency=3` and the process runs up to three invocations at once and no more, so one burst of events can't fork-bomb your model budget.
- **A conversation buffer** — a rolling short-term memory of recent turns that persists across invocations, so the agent that answers this trigger remembers the last one.

That is the whole point of the [agent process lifecycle](../../runtime/processes.md): you stop thinking about the loop and start configuring behavior. The full state machine, `status()` fields, and restart policies are documented on the [Agent Processes](../../runtime/processes.md) page.

## Build your first always-on AI agent

Here's a complete, runnable long-running AI agent. It wakes every five minutes on a cron trigger, handles up to two events concurrently, emits a heartbeat, and suspends itself after fifteen idle minutes to save resources. The only requirement to run it is an `OPENAI_API_KEY` (and, if you point at a real MCP server, that server running).

```python
import asyncio
from promptise.runtime import AgentProcess, ProcessConfig, TriggerConfig


async def main():
    process = AgentProcess(
        name="inbox-triager",
        config=ProcessConfig(
            model="openai:gpt-5-mini",
            instructions="You triage incoming support tickets and flag urgent ones.",
            servers={"tools": {"type": "http", "url": "http://localhost:8000/mcp"}},
            triggers=[
                TriggerConfig(type="cron", cron_expression="*/5 * * * *"),
            ],
            concurrency=2,          # concurrency semaphore: at most 2 invocations at once
            heartbeat_interval=10.0,  # liveness signal every 10 seconds
            idle_timeout=900.0,     # SUSPEND after 15 idle minutes; a trigger resumes it
        ),
    )

    await process.start()           # CREATED -> STARTING -> RUNNING
    print(process.status())         # state, invocation_count, queue_size, conversation_messages

    # The process now lives on its own, waking on the cron schedule and
    # keeping its conversation buffer between invocations.
    await asyncio.sleep(3600)

    await process.stop()            # RUNNING -> STOPPING -> STOPPED


asyncio.run(main())
```

Notice what you did *not* write: no loop, no queue plumbing, no semaphore, no heartbeat thread, no manual conversation history. You declared the behavior on `ProcessConfig` and called `start()`. The trigger queue, heartbeat, concurrency semaphore, and conversation buffer are all inside the container.

To exercise it without waiting for the clock, push an event in yourself — `await process.inject(...)` drops a `TriggerEvent` straight into the queue, which is exactly how the runtime's tests drive a process deterministically. And `process.status()` returns a live dict — `state`, `invocation_count`, `queue_size`, `conversation_messages` — so you can watch the agent work.

## Suspend and resume: keeping an AI agent that runs continuously efficient

An AI agent that runs continuously does not need to burn resources continuously. That's what `idle_timeout` buys you. When no events arrive for the configured window, the process transitions RUNNING → SUSPENDED: its triggers stay armed, but the agent stops holding active resources. The next event that hits the trigger queue wakes it straight back to RUNNING. You get an always-on agent without paying always-on cost — the ability to **suspend and resume** the agent is built into the state machine, not something you script around it.

Crash recovery is the other half of "keeps living across restarts." Every state transition, trigger event, and invocation result can be written to a **journal**. If the process dies, the runtime's replay engine reconstructs its last known good state — lifecycle position, context, and conversation history — from that journal, and a `restart_policy` of `"on_failure"` brings it back automatically. The bare loop loses everything on a crash; a journaled process picks up where it left off. The [Agent Runtime overview](../../runtime/index.md) walks through how journals, replay, and governance fit together.

## Manage many agents with AgentRuntime and deploy from a manifest

One process is a good start; production systems run several. **`AgentRuntime`** is the manager — think of it as the daemon that supervises your `AgentProcess` instances, gives them a shared event bus and message broker for talking to each other, and offers one control surface to start, stop, and inspect them all.

```python
import asyncio
from promptise.runtime import AgentRuntime, ProcessConfig, TriggerConfig


async def main():
    async with AgentRuntime() as runtime:
        await runtime.add_process(
            "inbox-triager",
            ProcessConfig(
                model="openai:gpt-5-mini",
                instructions="You triage incoming support tickets.",
                triggers=[TriggerConfig(type="cron", cron_expression="*/5 * * * *")],
            ),
        )
        await runtime.start_all()
        print(runtime.status())      # per-process state and invocation counts
        await asyncio.sleep(3600)
    # stop_all() runs automatically on context exit


asyncio.run(main())
```

For anything you plan to deploy, define the process declaratively in a `.agent` manifest instead of Python, so the configuration is version-controlled and reviewable:

```yaml
# inbox-triager.agent
version: "1.0"
name: inbox-triager
model: openai:gpt-5-mini
instructions: |
  You triage incoming support tickets and flag urgent ones.
servers:
  tools:
    type: http
    url: http://localhost:8000/mcp
triggers:
  - type: cron
    cron_expression: "*/5 * * * *"
```

Load and start it with `await runtime.load_manifest("inbox-triager.agent")`, or run it straight from the CLI with `promptise runtime start inbox-triager.agent`. The full schema — triggers, memory, journal, governance, and open mode — is on the [Agent Manifests](../../runtime/manifests.md) page. For the bigger picture of why a runtime beats a request-response agent, see [What Is an Autonomous AI Agent Runtime?](autonomous-ai-agent-runtime.md).

## When you don't need a long-running agent

Be honest with yourself before reaching for a runtime. If your agent answers a single request and exits — a chatbot turn, a one-shot summarization, a CLI command — you do not need an `AgentProcess`. A plain `await build_agent(...)` followed by `await agent.ainvoke(...)` is simpler, cheaper, and easier to reason about, and adding lifecycle machinery to it is pure overhead. The runtime earns its keep exactly when your agent must outlive a single request: react to events, hold state over time, run on a schedule, or survive a crash. If none of those are true, stay with the request-response agent and revisit this when your requirements grow.

## Frequently asked questions

### What makes an AI agent "long-running"?

A long-running AI agent outlives a single request. Instead of exiting after one reply, it stays resident, reacts to triggers (schedules, webhooks, file changes, messages), keeps state across invocations, and recovers from crashes. In Promptise that resident unit is the `AgentProcess`, supervised by an `AgentRuntime`.

### How does a persistent AI agent survive a crash or restart?

By journaling. When journaling is enabled, every state transition and invocation result is written to a durable log. After a crash, the replay engine rebuilds the process's last known state — lifecycle position, context, and conversation history — and a `restart_policy` of `"on_failure"` restarts it automatically, so the agent resumes instead of starting over.

### Can one process handle multiple events at once?

Yes, up to the limit you set. The `concurrency` field on `ProcessConfig` is enforced by an internal semaphore: `concurrency=3` lets three trigger invocations run in parallel and queues the rest. This keeps a burst of events from overwhelming your model budget while still processing work concurrently.

## Next steps

Copy the `AgentProcess` starter above and deploy your first always-on agent from a `.agent` manifest — start it locally, watch `status()`, then move the config into a manifest and run it with `promptise runtime start`. From here, work through the [Quick Start](../../getting-started/quickstart.md) to get an agent running end to end, then add a schedule with [How to Schedule an AI Agent with Cron Triggers](scheduled-ai-agent.md) and read the [Agent Processes](../../runtime/processes.md) reference to tune heartbeats, idle timeouts, and restart policies for production.
