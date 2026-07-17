---
title: "Escalate to a Human When an AI Agent Keeps Failing"
description: "When a downstream tool degrades, a bare agent retries into a hot loop and nobody finds out until the bill; shows the sliding-window error-rate detector…"
keywords: "escalate ai agent to human, ai agent human escalation, agent high error rate detection, escalate on repeated failure, agent failure webhook alert, when to page a human for an agent"
date: 2026-07-16
slug: escalate-ai-agent-to-human
categories:
  - Governance
---

# Escalate to a Human When an AI Agent Keeps Failing

The moment to **escalate an AI agent to a human** is not when it throws one exception — it is when a downstream tool has quietly degraded and the agent has been failing, retrying, and failing again on a loop that no caller is watching. That is the runaway nobody sees coming. A single failed tool call is easy: it raises, something catches it, maybe it retries. But when an API you depend on starts returning 500s across the board, an unattended agent does not stop. It keeps invoking, keeps burning tokens and compute, and keeps producing nothing — and because each trigger fires a fresh run, there is no exception bubbling up to anyone. You find out at the end of the month, on the bill. This post is about closing that gap with a first-class escalation path: a sliding-window error-rate detector that decides *the tool is down, get a human*, pages your on-call channel, and suspends the process — automatically.

<!-- more -->

## The failure mode: a degraded tool and a silent retry loop

Picture a reconciliation agent on a five-minute cron. Every tick it pulls the open-payout queue, calls a payments API to settle each item, and writes the result back. It has run cleanly for weeks.

Then the payments API has a bad afternoon. Every settle call returns `503`. Here is what a bare agent does with that:

- Tick fires. The agent tries to settle, gets a `503`, and — being a helpful agent — retries, reasons about the error, tries a slightly different call, fails again, and eventually gives up on that item *for this run*.
- The invocation ends without raising. From the outside it "completed." No alert, no page.
- Three hundred seconds later the cron fires again. The API is still down. The agent does the exact same expensive, useless thing.

Multiply that by a queue of items and a dependency outage that lasts hours, and you have an agent that made thousands of doomed tool calls and a token bill to match — all while its process state cheerfully reads `RUNNING`. The failure is not in any one call; it is in the *rate* of failure across calls, sustained over time. That is precisely the signal a per-call exception handler cannot see, and it is exactly when **ai agent human escalation** should kick in on its own.

The thing you want is boring and specific: *if a meaningful fraction of recent tool calls are failing, stop guessing and page a person.* Simple to state. Almost nobody ships it.

## What other frameworks do today

To be fair and precise: every serious framework gives you *a* way to bound a misbehaving run, and each is real within its scope. What none of them give you is a built-in error-rate detector bound to an escalation channel. Here is the honest map.

- **LangChain** — `AgentExecutor` takes `max_iterations` and `max_execution_time`, and individual tool/LLM calls can be wrapped with `.with_retry(...)` for per-call resilience. Retries handle a single flaky call; they do not measure the fraction of calls failing across a run, and nothing pages a human when that fraction stays high.
- **LangGraph** — a `recursion_limit` (default 25) that raises `GraphRecursionError` inside one graph invocation. It counts steps, not failures, and the error is yours to catch and route.
- **Pydantic AI** — `UsageLimits` (`request_limit`, `total_tokens_limit`, …) that raise `UsageLimitExceeded` mid-run. Again a quantity cap surfaced as an exception, not a failure-rate monitor.
- **CrewAI** — an `Agent`'s `max_iter` caps reasoning iterations and `max_rpm` self-throttles outbound requests; task execution retries on error. Useful, but there is no aggregate "too many of these are failing → escalate" trip wire.
- **AutoGen** — termination conditions (`MaxMessageTermination`, token-usage termination) and `max_turns` that end a conversation. They stop the chat; they do not watch tool error rate or notify anyone.

You can, of course, bolt external observability (LangSmith, Langfuse, your APM) on top and configure an alert when error rate climbs. That is worth doing — but it watches from *outside* the agent. It pages you; it does not itself halt the process. The loop keeps running until a human wakes up and pulls a lever.

So the exact delta is this. Across these frameworks a failure surfaces as **a raised exception or a terminated run that you must catch and route yourself** — and retries, where they exist, are per-call resilience, not fleet-level failure detection. To get "sustained high error rate → page a human → suspend the process" you assemble it: track failures across calls, compute a rate, decide a threshold, wire the alert, and remember to actually stop the agent. Promptise's edge is not that the others "can't" — it is that Promptise makes **agent high error rate detection**, the escalation, and the suspend a single first-class property of a supervised process, not glue you write around a loop.

## A sliding-window error-rate detector, wired to escalate()

Promptise Foundry's [Agent Runtime](../../runtime/index.md) wraps your `build_agent()` in a supervised `AgentProcess`, and around every invocation it runs governance — including a behavioral **health** monitor. One of its detectors is exactly the one this failure mode needs: high error rate over a sliding window.

The mechanics are deliberately dumb, in the good way:

- It keeps a fixed-size window of the last N tool outcomes (`error_window`, default 10).
- Each failed call records a `True`; each success records a `False`.
- Once the window is full, if the fraction of failures is at or above `error_rate_threshold` (default `0.5`), it emits a `HIGH_ERROR_RATE` anomaly.

Detection is **pure pattern matching — no LLM calls**, so it is free and instant. You are not paying a model to notice that another model's tools are down. And because it is a *rate over a window*, it distinguishes a single blip (one failure in ten, ignored) from a real outage (five of the last ten, tripped).

When an anomaly fires, the `on_anomaly` action decides what happens. Set it to `"escalate"` and the runtime does two things through the shared `escalate()` path, then suspends the process:

1. **Webhook POST** — if the `EscalationTarget` has a `webhook_url`, it fires a JSON payload (the anomaly type and its evidence) to that endpoint. Point it at a Slack incoming webhook and the page lands in your on-call channel. That is your **agent failure webhook alert**.
2. **EventBus emit** — if the target has an `event_type`, it emits that event on the runtime bus so other processes or dashboards can react in-band.

Both are fire-and-forget: escalation never raises back into agent execution, so a flaky Slack endpoint can't take the agent down with it. After escalating, the process transitions to `SUSPENDED` — the runaway is halted, its journal preserved for diagnosis, waiting for a human.

The last piece is what keeps escalation from becoming its own kind of noise: a **per-anomaly-type cooldown**. Each anomaly type tracks its own last-fired time; within the `cooldown` window (default 300s) a second anomaly *of the same type* is detected but suppressed — you get one page for "the payments API is down," not one per failing call. A different anomaly type (a stuck loop, say) is unaffected and can still fire. That is the answer to *when to page a human for an agent* without paging them forty times a minute.

## Runnable: watch the detector fire and the cooldown hold

Here is the detector on its own — no API key, no network, fully deterministic. It shows a healthy agent whose downstream tool suddenly starts failing, the sliding window tripping a `HIGH_ERROR_RATE` anomaly, and the cooldown swallowing the duplicate page. Every symbol is a real runtime API.

```python
import asyncio
from promptise.runtime import HealthConfig, HealthMonitor, AnomalyType


async def main() -> None:
    # A downstream tool has started failing. Watch the sliding window trip.
    monitor = HealthMonitor(
        HealthConfig(
            enabled=True,
            error_window=6,            # judge the last 6 tool outcomes
            error_rate_threshold=0.5,  # >= 50% failing = anomaly
            on_anomaly="escalate",     # webhook + EventBus, then suspend
            cooldown=300.0,            # one page per 5 min, per anomaly type
        ),
        process_id="pipeline-monitor",
    )

    # Three healthy calls, then the dependency degrades and starts 500-ing.
    for _ in range(3):
        await monitor.record_success()

    fired = None
    for i in range(3):
        fired = await monitor.record_error()
        print(f"error {i + 1}: anomaly = {fired.anomaly_type.value if fired else None}")

    assert fired is not None and fired.anomaly_type is AnomalyType.HIGH_ERROR_RATE
    print("description:", fired.description)
    print("details:", fired.details)

    # The next failure would trip again — but the cooldown suppresses the duplicate page.
    again = await monitor.record_error()
    print("suppressed by cooldown:", again is None)


asyncio.run(main())
```

Run it and you get:

```
error 1: anomaly = None
error 2: anomaly = None
error 3: anomaly = high_error_rate
description: High error rate: 50% errors in last 6 invocations (threshold: 50%)
details: {'error_rate': 0.5, 'error_count': 3, 'window_size': 6, 'threshold': 0.5}
suppressed by cooldown: True
```

The anomaly does not fire on the first error, or the second — it waits until the window is full and the *rate* crosses the line, then fires once and holds. That is the whole idea: **escalate on repeated failure**, not on the first hiccup.

## Wire it into a supervised process

In production you don't call the monitor yourself — you attach a `HealthConfig` to a `ProcessConfig`, and the supervised `AgentProcess` records outcomes and enforces the action around every invocation. This is the deployment shape, and it matches the CTA: point `on_anomaly="escalate"` at an `EscalationTarget` and route repeated tool failures straight to on-call.

```python
import asyncio
from promptise.runtime import (
    AgentProcess,
    ProcessConfig,
    HealthConfig,
    EscalationTarget,
    ProcessState,
)


async def main() -> None:
    config = ProcessConfig(
        model="openai:gpt-5-mini",
        instructions="Settle every open payout, then stop.",
        health=HealthConfig(
            enabled=True,
            error_window=20,           # judge the last 20 tool outcomes
            error_rate_threshold=0.4,  # 40%+ failing = the dependency is down
            on_anomaly="escalate",     # page a human via webhook + EventBus, then suspend
            cooldown=900.0,            # at most one page every 15 min per anomaly type
            escalation=EscalationTarget(
                webhook_url="https://hooks.slack.com/services/XXX",
                event_type="agent.health.anomaly",
            ),
        ),
    )

    process = AgentProcess(name="payout-monitor", config=config)
    await process.start()
    print(process.state)               # ProcessState.RUNNING

    # Triggers now fire invocations. If the payout API degrades and the
    # error rate crosses 40% over the last 20 calls, the runtime fires the
    # Slack webhook + EventBus event and suspends the process — out of band.

    await process.stop()
    assert process.state is ProcessState.STOPPED


asyncio.run(main())
```

Two honest notes. First, `escalate` **suspends** the process (`RUNNING → SUSPENDED`); it does not tear it down. That is intentional — a dependency outage is usually transient, and suspending preserves the journal and lets an operator resume once the tool recovers. Health's enforcement actions are `log`, `pause`, and `escalate`; if you want a hard stop for a breach you never want to auto-recover from, that belongs on the budget subsystem, which is covered in [How to Stop a Runaway AI Agent (Runtime Kill Switches)](stop-a-runaway-ai-agent.md). Second, the same `escalate()` path is shared by the budget and [mission](../../runtime/governance/mission.md) subsystems, so one Slack integration covers cost blowouts, trajectory drift, *and* tool-failure storms — you wire the target once.

## When paging a human is the wrong call

Escalation earns its keep for unattended agents that take real actions against flaky dependencies. It is honest overkill in a few cases:

- **One expected-flaky call.** If a single tool fails occasionally and a retry or a circuit breaker handles it cleanly, you do not need a page. Keep `error_rate_threshold` high enough that normal transient noise never trips it, and let per-call retries do their job.
- **Interactive chat.** A human is already reading every reply. There is no unattended loop to police, and a stuck/failing turn is visible in real time.
- **You already have external supervision.** If Temporal, Airflow, or your APM already alerts on failure rate *and* can halt the agent, layer Promptise's health check for the behavior it sees in-process and let the orchestrator own scheduling.

The line is crossed the moment the agent runs on a trigger, depends on a tool that can degrade, and would otherwise loop on the failure forever. Note also what this detector does *not* catch: an agent that fails *cheaply and silently* by returning trivial empty answers, or one stuck calling the identical tool with identical arguments. Those are separate health detectors — the stuck case is walked through in [Catch an AI Agent Stuck Repeating the Same Tool Call](ai-agent-stuck-repeating-tool-call.md). The full detector catalogue and thresholds live in the [Behavioral Health reference](../../runtime/governance/health.md).

## Frequently asked questions

### When should I escalate an AI agent to a human instead of just retrying?

Retry a *single* failed call — that is per-call resilience. Escalate when the *rate* of failure stays high across many calls, because that signals the tool itself is down and no amount of retrying will help. The sliding-window error-rate detector draws that line for you: below the threshold it is noise you absorb; at or above it, the dependency is broken and a human should look. That is the difference between resilience and **escalate on repeated failure**.

### Does the error-rate detector make any LLM calls?

No. All health detection is pure pattern matching over tool-outcome history — a fixed-size window of successes and failures with a fraction check. It costs nothing and runs instantly, which is the point: you should not pay a model to notice that another model's tools are failing.

### How do I avoid being paged on every single failing call?

The per-anomaly-type cooldown. When an anomaly fires, its type records a last-fired timestamp; any further anomaly of the same type inside the `cooldown` window is detected but suppressed. You get one **agent failure webhook alert** for the outage, not one per call. Different anomaly types have independent cooldowns, so a genuinely new problem still pages you.

### What exactly happens when `on_anomaly="escalate"` fires?

The runtime calls the shared `escalate()` path: it POSTs the anomaly payload to your `EscalationTarget.webhook_url` (e.g. a Slack incoming webhook) and emits `EscalationTarget.event_type` on the runtime EventBus. Both are fire-and-forget, so a flaky endpoint can't disrupt the agent. Then the process transitions to `SUSPENDED`, halting further trigger-driven runs while preserving the journal.

### Can it hard-stop the process instead of suspending?

Health's actions are `log`, `pause`, and `escalate` — `escalate` suspends after notifying. That is deliberate: a failure storm is usually a transient dependency outage worth resuming from. For an unconditional kill on cost or destructive-action runaways, use the budget subsystem's `on_exceeded="stop"`, described in the runaway-agent post linked above.

## Next steps

Set `on_anomaly="escalate"` with an `EscalationTarget` webhook on your next unattended agent and route repeated tool failures straight to your on-call channel — start from the runnable detector above to see it fire, then attach the `HealthConfig` to a real `ProcessConfig` as in the deployment example. Read the [Agent Runtime overview](../../runtime/index.md) for how processes and triggers fit together, the [Behavioral Health reference](../../runtime/governance/health.md) to tune `error_window`, `error_rate_threshold`, and `cooldown` for your tools, and the [mission governance guide](../../runtime/governance/mission.md) to add a goal-drift trip wire on the same shared escalation path.
