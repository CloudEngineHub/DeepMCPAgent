---
title: "Catch an AI Agent Stuck Repeating the Same Tool Call"
description: "Names the exact failure — an agent calling get_status(id=42) forever — and separates it cleanly from context bloat: this is behavioral-health detection, not…"
keywords: "ai agent stuck repeating tool call, agent calls same tool repeatedly, detect stuck llm agent, agent repeats same action, stuck agent detection, agent same tool same arguments loop"
date: 2026-07-16
slug: ai-agent-stuck-repeating-tool-call
categories:
  - Governance
---

# Catch an AI Agent Stuck Repeating the Same Tool Call

An **AI agent stuck repeating tool call** after identical tool call — say, `get_status(id=42)` on every single turn — burns its entire iteration budget without making a shred of progress, and most frameworks will not tell you it is happening until the budget is spent. This is one of the cheapest, most insidious ways an autonomous agent fails: it is not crashing, it is not erroring, it is confidently doing the exact same nothing over and over. By the end of this post you will know precisely why an iteration counter catches this late (or never), how a purpose-built **stuck detector** trips after N identical consecutive calls with zero extra LLM calls, and how to wire pause / stop / escalate around it in a few lines.

## The exact failure: an agent stuck on get_status(id=42)

Here is the shape of it. Your agent is supposed to kick off a job and wait for it to finish. It calls `start_job()`, gets back an id, then polls:

```
get_status(id=42)  → "running"
get_status(id=42)  → "running"
get_status(id=42)  → "running"
get_status(id=42)  → "running"   ← 40 more of these
```

The job is genuinely still running, or the status field never flips, or the model simply forgot it already asked — the *cause* varies. The *signature* does not: the **same tool, called with the same arguments, consecutively**, with no intervening progress. The agent is not exploring. It is not retrying with a backoff. It is wedged, and every poll costs a full LLM turn plus a tool round-trip. Left alone, it will happily do this until it hits a context limit or an iteration cap — long after any human would have noticed.

The key insight is that this failure has a *fingerprint*: `(tool_name, arguments)` repeating. That fingerprint is cheap to compute and impossible to miss if you are actually looking for it. The problem is that most agent stacks are not looking for it — they are only counting.

## This is a behavioral-health failure, not a context-window one

It is easy to lump this in with the other thing people call "stuck": the agent whose transcript has grown so large that the model loses the thread and re-fetches facts it already has. That is a real failure too, but it is a *different* one — a context-window and attention problem, and the fix is bounding what the model sees per turn. We cover that failure and its `context_scope` remedy in [When Agent Tool Loops Fail: Fixing Context Bloat](agent-stuck-in-tool-loop.md).

The failure in *this* post is not about context size at all. An agent can be three turns in, with a tiny transcript that fits comfortably in the window, and still be pinned on `get_status(id=42)`. No amount of context management will catch that, because there is nothing wrong with the context — the wrongness is in the *behavior over time*. Catching it needs a monitor that watches the sequence of actions, not a prompt that trims the transcript. That is what **behavioral health monitoring** is: pure pattern matching over tool-call history, with no LLM in the loop.

## What other frameworks do today

To be fair and precise: every serious framework ships a guardrail here, and each is real and useful within its scope. But look closely at *what* they measure.

- **LangChain** — `AgentExecutor` takes `max_iterations` (default 15) and `max_execution_time`. When the count is exceeded it stops that run. It counts **how many** steps happened.
- **LangGraph** — a `recursion_limit` (default 25) on graph execution; exceeding it raises `GraphRecursionError`. Again, a **count** of super-steps.
- **CrewAI** — an `Agent`'s `max_iter` caps reasoning iterations per task, and `max_rpm` throttles the outbound request *rate*. Both are quantity limits.
- **AutoGen** — `max_turns` and termination conditions such as `MaxMessageTermination` end a conversation after a number of messages.
- **Pydantic AI** — `UsageLimits` (`request_limit`, `total_tokens_limit`) raise `UsageLimitExceeded` mid-run on volume.

Every one of these bounds a runaway *eventually*, and that is genuinely valuable. Here is the exact delta: **none of them key on whether the same tool was called with the same arguments N times in a row.** A count of 15, 25, or 30 says nothing about *what* those calls were — `get_status(id=42)` forty times and forty distinct, productive calls look identical to an integer. So a stuck agent silently burns its *entire* iteration budget before any counter trips, and when it finally does trip, all you learn is "hit the cap," not "it was wedged on one call." You can absolutely build repeat-detection yourself with a callback that inspects each step — the capability is reachable in every one of these frameworks. Promptise's edge is not that they "can't count." It is that Promptise makes the identical-call detector a **first-class, structural property of the running process** so you do not have to hand-roll it, tune it, and remember to wire it into every agent.

## How the STUCK detector catches it in three tries

Promptise Foundry's [Behavioral Health monitor](../../runtime/governance/health.md) runs a `STUCK` detector that does exactly the thing an iteration cap cannot. On every tool call it records a `(tool_name, hash(arguments))` tuple. When the last `stuck_threshold` tuples are all identical, it emits an `Anomaly` — immediately, deterministically, and with **zero LLM calls** because it is pure pattern matching over history. The default `stuck_threshold` is `3`: three identical consecutive calls and it trips.

Because detection is free and synchronous, you can run it on the hot path without a cost or latency penalty. Here is the whole thing, runnable as-is — it needs no API key, because no model is ever invoked:

```python
import asyncio
from promptise.runtime import HealthConfig, HealthMonitor


async def main() -> None:
    # Pure pattern matching — zero LLM calls, no API key required.
    monitor = HealthMonitor(
        HealthConfig(enabled=True, stuck_threshold=3),
        process_id="poller",
    )

    # The agent gets wedged: it polls the same job id every turn.
    for attempt in range(1, 6):
        anomaly = await monitor.record_tool_call("get_status", {"id": 42})
        label = f"call {attempt}: get_status(id=42)"
        if anomaly is not None:
            print(f"{label}  ->  {anomaly.anomaly_type.value.upper()}: {anomaly.description}")
            break
        print(f"{label}  ->  ok")


asyncio.run(main())
```

Run it and the third call trips the wire:

```
call 1: get_status(id=42)  ->  ok
call 2: get_status(id=42)  ->  ok
call 3: get_status(id=42)  ->  STUCK: Agent stuck: tool 'get_status' called 3 times consecutively with identical arguments
```

Three tries, not thirty. The returned `Anomaly` carries `anomaly_type` (`AnomalyType.STUCK`), a human-readable `description`, and a `details` dict with the offending `tool_name` — everything you need to log, alert, or halt on. Change one argument between calls and the fingerprint changes, so a genuinely-progressing loop (`get_status(id=42)`, then `get_status(id=43)`) never trips. The monitor also ships companion detectors for repeating multi-tool *patterns* (`LOOP`), consecutive empty responses, and high error rates — but for the specific "same call, forever" failure, `STUCK` is the one you want.

## Wire it into a supervised process

The bare `HealthMonitor` above is what runs *inside* the runtime, but you rarely touch it directly. In production you declare a `HealthConfig` on a `ProcessConfig` and let the [Agent Runtime](../../runtime/index.md) drive it around every invocation of a supervised `AgentProcess`. The `on_anomaly` action decides what happens when the wire trips:

```python
from promptise.runtime import ProcessConfig, HealthConfig, EscalationTarget

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Poll each submitted job until it reaches a terminal state.",
    health=HealthConfig(
        enabled=True,
        stuck_threshold=3,          # same tool + args 3x in a row = stuck
        on_anomaly="escalate",      # "log", "pause", or "escalate"
        cooldown=300,               # 5 min between repeats of the same anomaly type
        escalation=EscalationTarget(
            webhook_url="https://hooks.slack.com/services/XXX",
            event_type="agent.health.stuck",
        ),
    ),
)
```

The four `on_anomaly` behaviors are the whole enforcement story: `"log"` records the anomaly and keeps going, `"pause"` suspends the process, `"stop"` ends it, and `"escalate"` fires the webhook plus an EventBus event and then suspends — a clean hand-off to a human, the pattern in [Escalate to a Human When an AI Agent Keeps Failing](escalate-ai-agent-to-human.md). Because the action lands on the *process*, not on your prompt, it survives across trigger-driven invocations — the deeper reason a runtime beats an in-call counter, unpacked in [How to Stop a Runaway AI Agent (Runtime Kill Switches)](stop-a-runaway-ai-agent.md).

Health is deliberately not the whole safety envelope. It answers "is the agent behaving?"; the [Autonomy Budget](../../runtime/governance/budget.md) answers "is the agent doing too much?" — a hard cap on tool calls, weighted cost units, and irreversible actions. The two are complementary: budget stops an agent that does too much, health stops one that does the same cheap nothing forever. Run both and a stuck poller trips the health wire in three calls while a runaway spender trips the budget — two different runaways, two independent trip wires.

## Frequently asked questions

### Why does my agent keep calling the same tool with the same arguments?

Usually the tool's result did not change the model's belief state — the job is still `running`, the record still looks the same — so the model re-issues the identical call expecting a different outcome. It is a behavioral loop, not a reasoning breakthrough waiting to happen. The `STUCK` detector catches it by fingerprinting `(tool_name, hash(arguments))` and tripping after `stuck_threshold` identical consecutive calls, regardless of how much iteration budget remains.

### How is this different from a max-iterations or recursion limit?

Those count *how many* calls happen; the `STUCK` detector inspects *what* the calls are. A cap of 25 lets an agent repeat one useless call 25 times before it fires. `stuck_threshold=3` fires on the third identical call — long before the budget is gone — and its anomaly tells you exactly which call was stuck, not merely that a ceiling was hit.

### Does the stuck detector cost extra LLM calls or latency?

No. Detection is pure pattern matching over the in-memory tool-call history — hashing arguments and comparing the last N tuples. There is no model invocation, so it is free to run on every call and adds no measurable latency. That is why the runnable example above works with no API key set.

### Will it fire on a legitimate polling loop?

Only if the arguments are truly identical every time. Because the fingerprint includes the hashed arguments, a loop that advances (`id=42` then `id=43`, or a changing cursor) never trips `STUCK`. For a loop that legitimately re-polls the *same* id many times, either raise `stuck_threshold`, set `on_anomaly="log"` to observe without halting, or vary an argument (such as an attempt counter) so real progress is visible to the detector.

## Next steps

Add `HealthConfig(enabled=True, stuck_threshold=3)` to your process and watch the runtime catch a repeating call on the third try instead of the thirtieth. Start from the [Quick Start](../../getting-started/quickstart.md) to stand up an agent, wrap it in a supervised process from the [Agent Runtime overview](../../runtime/index.md), then use the [Behavioral Health reference](../../runtime/governance/health.md) and the [Autonomy Budget reference](../../runtime/governance/budget.md) to design the exact behavior-and-volume envelope your agent must never leave.
