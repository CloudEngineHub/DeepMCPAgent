---
title: "Set a Daily Budget for an Autonomous AI Agent"
description: "A per-run cap does nothing for an agent that fires every five minutes; shows daily counters with a configurable reset hour that persist across every…"
keywords: "daily budget for ai agent, limit ai agent per day, daily tool call cap, autonomous agent spend limit, cron agent budget, per-day agent limits"
date: 2026-07-16
slug: daily-budget-for-ai-agent
categories:
  - Governance
---

# Set a Daily Budget for an Autonomous AI Agent

Setting a real daily budget for AI agents means capping what an agent does across a whole day, not what it does inside one function call — and that distinction is the entire problem with the guardrails most frameworks ship. A per-run cap like `max_iterations` counts to N and resets to zero the moment the next invocation starts. That is fine for a chatbot a human is watching. It is worthless for an agent on a five-minute cron, because the thing you actually want to bound — total tool calls, total cost units, total runs — is spread across hundreds of invocations that never see each other's counters. This post shows how Promptise Foundry's `BudgetConfig` daily scope gives a long-lived, trigger-driven process a genuine 24-hour ceiling: `max_tool_calls_per_day`, `max_cost_per_day`, `max_runs_per_day`, and a configurable `daily_reset_hour_utc`, all tracked across every cron-, webhook-, and event-driven run.

<!-- more -->

The thesis in one line: a counter that resets on every invocation is not a daily budget. Counters that persist across every invocation and reset once, on a clock you choose, are.

## Why a per-run cap resets to zero on every trigger

Picture a reconciliation agent on a `*/5 * * * *` cron — a fresh invocation every five minutes, 288 times a day. You set `max_tool_calls_per_run=20` and feel safe. But look at what that cap actually promises: *no single invocation makes more than 20 tool calls.* It says nothing about the day. Twenty calls per run times 288 runs is 5,760 tool calls — every one of them individually "within limits."

The per-run counter is not broken; it is scoped wrong for this failure mode. A per-day limit for an AI agent has to survive the boundary between invocations, and a per-run counter is defined to not do that. In Promptise's runtime the two scopes are explicit and separate: `reset_run()` clears the per-run counters at the top of every invocation, while the daily counters (`daily_tool_calls`, `daily_runs`, `daily_cost`) keep climbing until `check_daily_reset()` clears them at your configured hour. A cron agent budget that only counts per-run has no memory of the eleven runs that came before this one. That is precisely the memory a daily tool call cap needs.

## What other frameworks do today

To be fair and precise: every serious framework ships an in-run control, and each is real and useful within its scope. What none of them ship is a cumulative *per-day* budget that spans a trigger-driven process's many invocations.

- **LangChain** — `AgentExecutor` takes `max_iterations` (default 15) and `max_execution_time`. Both bound a single `.invoke()`/`.ainvoke()` and reset on the next call.
- **LangGraph** — a `recursion_limit` (default 25) on graph execution; exceeding it raises `GraphRecursionError` inside one graph invocation.
- **Pydantic AI** — `UsageLimits` (`request_limit`, `total_tokens_limit`, and friends) passed into a run; a breach raises `UsageLimitExceeded` mid-run.
- **AutoGen** — teams accept `max_turns` and termination conditions (`MaxMessageTermination`, token-usage termination) that end one conversation.
- **CrewAI** — an `Agent`'s `max_iter` caps reasoning iterations per task, and `max_rpm` self-throttles the outbound request *rate*.

CrewAI's `max_rpm` deserves the closest look, because it is the one control here that spans time rather than a single run. But it is a rate limiter, not a cumulative budget: it smooths requests to N-per-minute so you do not burst a provider, and it does not maintain a running daily total, has no configurable daily reset, and never says "you have made your last allowed call for today, stop the process." The others are single per-run caps enforced as an exception (or termination) you catch inside one invocation. That is the exact delta. To get a cumulative daily ceiling on any of them, you keep the running total yourself in an external store, check it at the top of every scheduled run, and decide whether to skip firing — the supervising loop is yours to write and maintain.

Promptise's edge is not that these frameworks "can't count" — they count fine within a run. It is that Promptise makes the *daily* ceiling a first-class, structural property of the persistent process: the daily counters live on the process's budget state, survive every trigger-driven invocation, reset on a clock you set, and trip an out-of-band `pause`/`stop`/`escalate` on the process itself.

## Daily counters that survive every trigger-driven run

The [Autonomy Budget](../../runtime/governance/budget.md) system tracks two scopes at once, and the whole value is in keeping them apart:

| Scope | Fields | Resets |
|-------|--------|--------|
| **Per-run** | `max_tool_calls_per_run`, `max_llm_turns_per_run`, `max_cost_per_run`, `max_irreversible_per_run` | Start of every invocation |
| **Per-day** | `max_tool_calls_per_day`, `max_cost_per_day`, `max_runs_per_day` | Once, at `daily_reset_hour_utc` |

The per-day fields are the ceiling a scheduled agent actually needs. `max_runs_per_day` caps how many times the agent may wake at all — a hard lid on an over-eager trigger. `max_tool_calls_per_day` is your daily tool call cap across every run combined. `max_cost_per_day` sums each tool's `cost_weight` into a single daily total — this is the autonomous agent spend limit, with one honest caveat covered below. And `daily_reset_hour_utc` (default `0`, i.e. midnight UTC) lets you align the reset to your billing day or a low-traffic hour instead of an arbitrary boundary.

Because these counters hang off the long-lived `AgentProcess`, not off a single `ainvoke()`, they enforce a true 24-hour envelope. When a daily limit is breached the runtime does not throw into your call stack — it runs the configured action *out-of-band* on the process: `pause` suspends it (`RUNNING → SUSPENDED`, resumable), `stop` ends it (`RUNNING → STOPPING → STOPPED`, manual restart), and `escalate` fires a webhook plus an EventBus event and then suspends. The next cron tick then has nothing in `RUNNING` to fire against — the agent is genuinely capped for the day, not merely interrupted for one turn.

## Runnable: a cron agent with a real 24-hour ceiling

Here is a complete, runnable process that puts a daily ceiling on a five-minute cron agent. Every symbol is a real runtime API.

```python
import asyncio
from promptise.runtime import (
    AgentProcess,
    ProcessConfig,
    TriggerConfig,
    BudgetConfig,
    ToolCostAnnotation,
    EscalationTarget,
    ProcessState,
)


async def main() -> None:
    config = ProcessConfig(
        model="openai:gpt-5-mini",
        instructions="Reconcile open payment disputes until the queue is clear.",
        # Fires a fresh invocation every 5 minutes — 288 runs a day.
        triggers=[TriggerConfig(type="cron", cron_expression="*/5 * * * *")],
        budget=BudgetConfig(
            enabled=True,
            # Per-run cap: sane ceiling for ONE invocation.
            max_tool_calls_per_run=20,
            # Per-DAY caps: the ceiling that persists across all 288 runs.
            max_runs_per_day=200,            # stop waking after 200 invocations
            max_tool_calls_per_day=1500,     # daily tool call cap, all runs combined
            max_cost_per_day=500.0,          # abstract weight units you define, NOT dollars
            daily_reset_hour_utc=6,          # reset at 06:00 UTC, aligned to your billing day
            tool_costs={
                "issue_refund": ToolCostAnnotation(cost_weight=10.0, irreversible=True),
                "send_email":   ToolCostAnnotation(cost_weight=2.0, irreversible=True),
                "search":       ToolCostAnnotation(cost_weight=0.5),
            },
            on_exceeded="stop",              # daily breach halts the PROCESS, not one run
            inject_remaining=True,           # the agent sees its remaining daily budget
            escalation=EscalationTarget(webhook_url="https://hooks.slack.com/services/XXX"),
        ),
    )

    process = AgentProcess(name="dispute-agent", config=config)
    await process.start()
    print(process.state)                     # ProcessState.RUNNING

    # The cron now drives invocations. Per-run counters reset each tick; the
    # daily counters keep climbing until 06:00 UTC. When a per-day limit trips,
    # on_exceeded="stop" halts the process so the NEXT tick has nothing to run.

    await process.stop()
    assert process.state is ProcessState.STOPPED


asyncio.run(main())
```

If you want to *see* the daily counter outlive a per-run reset — with no API key and no LLM calls — drive the budget state directly. This is deterministic and safe to run anywhere:

```python
import asyncio
from promptise.runtime.budget import BudgetState
from promptise.runtime.config import BudgetConfig


async def main() -> None:
    budget = BudgetConfig(
        enabled=True,
        max_tool_calls_per_run=5,    # each run may make up to 5 calls
        max_tool_calls_per_day=12,   # ...but only 12 across the WHOLE day
    )
    state = BudgetState(budget)

    # Simulate three cron ticks. reset_run() clears the PER-RUN counter each
    # tick; the DAILY counter is never reset here — it accumulates.
    for tick in range(1, 4):
        await state.reset_run()
        await state.record_run_start()
        violation = None
        for _ in range(5):                          # 5 calls, within the per-run cap
            violation = await state.record_tool_call("search")
            if violation is not None:
                break
        remaining = state.remaining()
        print(
            f"tick {tick}: daily tool calls left = {remaining['tool_calls_day']}, "
            f"violation = {violation and violation.limit_name}"
        )


asyncio.run(main())
# tick 1: daily tool calls left = 7, violation = None
# tick 2: daily tool calls left = 2, violation = None
# tick 3: daily tool calls left = -1, violation = max_tool_calls_per_day
```

Every run stays under its per-run cap of five, so a per-run-only guard would let this agent run forever. The daily cap of twelve trips on the third tick anyway — because `reset_run()` cleared only the per-run counter and the daily counter remembered the ten calls from ticks one and two. That is the whole feature in six lines of output.

## Choosing your daily limits and reset hour

A few practical rules for setting per-day agent limits that hold up in production:

- **Be honest about what `max_cost_per_day` measures.** It sums each tool's `cost_weight` into abstract units *you* define — it is **not** dollars. Promptise never queries a provider's pricing API. A `cost_weight=10.0` refund is "ten budget units," not ten dollars. This governs *what the agent does*, not your token bill; for real monetary limits, watch your provider dashboard or export token counts through Promptise observability. Weight by relative risk (a baseline read = `1.0`, a refund = `10.0`) and the numbers stay meaningful without ever tracking money.
- **Set `daily_reset_hour_utc` to your billing day, not midnight UTC.** If your Stripe day rolls over at 06:00 UTC, reset there so a single "day" of agent activity maps to a single day of real invoices.
- **Use `max_runs_per_day` as the coarse safety net.** Even with generous per-call budgets, capping total invocations bounds a trigger that has gone haywire and started firing far more often than intended.
- **Pick the enforcement action to match recoverability.** `on_exceeded="stop"` is the right hard kill for a spend runaway you never want to auto-resume; `"escalate"` pages a human first; `"pause"` suspends for a transient spike. All three act on the process, so the day's ceiling is real either way.

A daily budget is one trip wire, not the whole safety story. For the process-level lifecycle it plugs into, see [How to Stop a Runaway AI Agent (Runtime Kill Switches)](stop-a-runaway-ai-agent.md); for the behavioral side — an agent that stays cheap but loops uselessly — see [Catch an AI Agent Stuck Repeating the Same Tool Call](ai-agent-stuck-repeating-tool-call.md). And when the agent should run until a goal is met rather than forever, pair the budget with a [Mission](../../runtime/governance/mission.md) so it stops itself on success instead of burning the day's ceiling.

## Frequently asked questions

### Does `max_iterations`, `recursion_limit`, or `UsageLimits` give an agent a daily budget?

No. Each of those bounds work *inside a single invocation* and resets when the next one starts. A cron, webhook, or event trigger begins a fresh invocation every time, so an agent that fires 288 times a day can make its full per-run quota on every one of them. A daily budget has to persist across invocations, which is exactly what `max_tool_calls_per_day`, `max_cost_per_day`, and `max_runs_per_day` on a supervised `AgentProcess` do.

### Is `max_cost_per_day` a real dollar spend limit?

No — it is measured in abstract weight units you assign via `ToolCostAnnotation`, plus counts of tool calls and runs. Promptise does not connect to any LLM provider's pricing API. Treat it as an autonomous agent spend limit on *behavior* (how many refunds, sends, or deletes per day), and enforce real monetary caps through your provider dashboard or observability exports.

### When do the daily counters actually reset?

Once per day, when the process crosses `daily_reset_hour_utc` (default `0`, midnight UTC). The runtime checks this via `check_daily_reset()` and zeroes `daily_tool_calls`, `daily_runs`, and `daily_cost`. Per-run counters are separate — they reset at the start of every single invocation and never affect the daily totals.

### What happens on the next cron tick after a daily limit trips?

With `on_exceeded="stop"`, the breach transitions the process to `STOPPED`, so the next trigger has nothing in `RUNNING` to fire against — the agent is capped until you restart it. With `"pause"` or `"escalate"` the process is suspended (and, for escalate, a webhook and EventBus event fire first), so it can be resumed once a human clears the cause.

### Can I combine per-run and per-day limits?

Yes — that is the intended design. Set `max_tool_calls_per_run` for a sane single-invocation ceiling and `max_tool_calls_per_day` / `max_cost_per_day` / `max_runs_per_day` for the cumulative daily envelope. The per-run cap protects any one run from going wide; the daily caps protect the day from a thousand individually-fine runs.

## Next steps

Set `max_cost_per_day` and `daily_reset_hour_utc` on your process budget and give your always-on agent a real 24-hour ceiling — start from the runnable cron process above, then tune the numbers to your tools. Read the [Agent Runtime overview](../../runtime/index.md) to see how processes and triggers fit together, and use the [Autonomy Budget reference](../../runtime/governance/budget.md) to design the exact per-run and per-day envelope your agent must never leave.
