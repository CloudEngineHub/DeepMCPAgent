---
title: "Add Budget + Health Kill-Switches to a Runtime Agent"
description: "The copy-paste BOFU recipe: turn on per-run plus daily budget, irreversible caps, and the four health detectors on one ProcessConfig (or .agent YAML), pick…"
keywords: "configure ai agent budget and health, add budget and health to agent, agent budget config python, ProcessConfig budget health, runtime governance setup, agent governance yaml manifest"
date: 2026-07-16
slug: configure-ai-agent-budget-and-health
categories:
  - Governance
---

# Add Budget + Health Kill-Switches to a Runtime Agent

To **configure AI agent budget and health** kill-switches you should not need a monitoring side-project, a supervisor loop, or a scatter of `try/except` blocks around every tool call — you need one config block on the process. This is the copy-paste recipe, not the why-you-need-it explainer: paste one `ProcessConfig` (or one `.agent` YAML manifest), fill in your per-run and daily limits, your irreversible-action cap, the four behavioral-health detectors, pick `pause` / `stop` / `escalate`, point a webhook at Slack, and ship a governed, self-limiting agent in a single file. The runtime enforces the whole declaration for you, around every invocation, out-of-band. Below is the exact block.

<!-- more -->

If you want the argument for *why* runtime enforcement beats a per-run counter that throws, read the companion post [How to Stop a Runaway AI Agent (Runtime Kill Switches)](stop-a-runaway-ai-agent.md). This post assumes you're sold and just want the block.

## The one governance block you paste

Here is a complete, runnable governance block: budget and health, each with its own escalation target, on a single [`ProcessConfig`](../../runtime/index.md). Every symbol is a real runtime API. This snippet imports, constructs, and asserts on the resolved config with **no API key and no network** — copy it, run it, then swap in your own numbers.

```python
from promptise.runtime import (
    AgentProcess,
    ProcessConfig,
    BudgetConfig,
    HealthConfig,
    EscalationTarget,
    ToolCostAnnotation,
    ProcessState,
)

# One governance block: budget + health + escalation on a single ProcessConfig.
config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Reconcile open refund requests until the queue is clear.",
    # --- Budget: cap how much the agent is allowed to DO ---
    budget=BudgetConfig(
        enabled=True,
        max_tool_calls_per_run=25,     # per invocation
        max_cost_per_day=300.0,        # abstract weight units you define, NOT dollars
        max_irreversible_per_run=3,    # separate cap on charges / deletes / sends
        tool_costs={
            "issue_refund": ToolCostAnnotation(cost_weight=10.0, irreversible=True),
            "send_email":   ToolCostAnnotation(cost_weight=2.0, irreversible=True),
            "search":       ToolCostAnnotation(cost_weight=0.5),
        },
        on_exceeded="stop",            # RUNNING -> STOPPING -> STOPPED, manual restart
        inject_remaining=True,         # the agent sees its remaining budget
        escalation=EscalationTarget(
            webhook_url="https://hooks.slack.com/services/XXX",
            event_type="agent.budget.exceeded",
        ),
    ),
    # --- Health: catch bad BEHAVIOR (stuck / looping / erroring), no LLM calls ---
    health=HealthConfig(
        enabled=True,
        stuck_threshold=3,             # same tool + args 3x in a row = stuck
        loop_window=20,                # scan last 20 calls for repeating patterns
        error_rate_threshold=0.5,      # 50%+ failing calls = anomaly
        on_anomaly="escalate",         # webhook + EventBus, then suspend
        cooldown=300,                  # 5 min between repeats of the same anomaly
        escalation=EscalationTarget(
            webhook_url="https://hooks.slack.com/services/XXX",
            event_type="agent.health.anomaly",
        ),
    ),
)

# The runtime enforces this declaration for you — no try/except in your loop.
process = AgentProcess(name="refund-agent", config=config)

assert config.budget.enabled and config.budget.on_exceeded == "stop"
assert config.health.enabled and config.health.on_anomaly == "escalate"
assert process.state is ProcessState.CREATED
print("governed process:", process.name, "->", process.state.value)
```

That is the whole thing. There is no supervising loop to write, no counter to thread through your prompt, no exception handler around `issue_refund`. You declare the envelope; the runtime keeps the agent inside it on every trigger-driven invocation and takes the configured action the moment a limit is breached. When you're ready to run for real, `await process.start()` transitions it `CREATED → STARTING → RUNNING`, and your triggers begin firing invocations against a supervised process.

## What each field does, and how enforcement fires

The block above pulls two independent trip wires. Budget governs **quantity** — how much the agent does. Health governs **behavior** — whether what it's doing makes sense. They see different failure modes, so you want both.

**Budget** (`BudgetConfig`) counts tool calls, weighted cost units, and irreversible actions across two scopes: per-run (reset at the top of every invocation) and per-day (reset at `daily_reset_hour_utc`, default midnight UTC). The fields you'll reach for most:

| Field | What it caps |
|-------|--------------|
| `max_tool_calls_per_run` | Total tool calls in a single invocation |
| `max_cost_per_run` / `max_cost_per_day` | Sum of `cost_weight` values (abstract units, per-run and daily) |
| `max_irreversible_per_run` | Tools marked `irreversible=True` — counted *separately* from the total |
| `tool_costs` | Per-tool `ToolCostAnnotation(cost_weight=…, irreversible=…)`; unlisted tools default to weight `1.0` |
| `on_exceeded` | `"pause"`, `"stop"`, or `"escalate"` |

The `max_irreversible_per_run` cap is the one you can't get from a plain step counter: it lets you grant a generous read budget while allowing only three refunds per run, because `issue_refund` and `send_email` carry `irreversible=True` and `search` does not. One honest caveat baked into the design — `max_cost_per_day` is measured in **abstract weight units you define, not dollars**. Promptise never queries a provider's pricing API; the budget governs *what the agent does*, not your token bill. The full weighting guidance is in the [Autonomy Budget reference](../../runtime/governance/budget.md).

**Health** (`HealthConfig`) is pure pattern matching over tool-call history with **zero LLM calls**, so it's free and instant. Four detectors:

- **Stuck** — the same `(tool, args)` `stuck_threshold` times in a row. The insidious case walked through in [Catch an AI Agent Stuck Repeating the Same Tool Call](ai-agent-stuck-repeating-tool-call.md).
- **Loop** — a repeating multi-tool sequence (`search → read → search → read`) within the last `loop_window` calls.
- **Empty response** — `empty_threshold` consecutive responses shorter than `empty_max_chars`.
- **Error rate** — failed-call fraction over a sliding window exceeding `error_rate_threshold`.

One field difference to internalize when you paste: **`on_exceeded` accepts `pause` / `stop` / `escalate`, but `on_anomaly` accepts `log` / `pause` / `escalate` — health has no `stop`.** That's deliberate: a behavioral anomaly is often recoverable, so health suspends and preserves the journal for diagnosis rather than killing the process outright. If you want a behavioral anomaly to hard-stop, point health at `escalate` and let the on-call engineer pull `process.stop()`, or lean on the budget's `on_exceeded="stop"` for the cost and irreversible-action runaways. The detector mechanics are in the [Behavioral Health reference](../../runtime/governance/health.md).

## The same governance as a `.agent` YAML manifest

Every field maps one-to-one to a `.agent` manifest, so the same envelope is declarative in code or in YAML — pick whichever your deploy pipeline prefers. The runtime validates `budget:` and `health:` blocks against the exact same `BudgetConfig` / `HealthConfig` schemas.

```yaml title="refund-agent.agent"
version: "1.0"
name: refund-agent
model: openai:gpt-5-mini
instructions: |
  Reconcile open refund requests until the queue is clear.
triggers:
  - type: cron
    cron_expression: "*/5 * * * *"
budget:
  enabled: true
  max_tool_calls_per_run: 25
  max_cost_per_day: 300.0
  max_irreversible_per_run: 3
  on_exceeded: stop
  inject_remaining: true
  tool_costs:
    issue_refund:
      cost_weight: 10.0
      irreversible: true
    search:
      cost_weight: 0.5
  escalation:
    webhook_url: "https://hooks.slack.com/services/XXX"
    event_type: agent.budget.exceeded
health:
  enabled: true
  stuck_threshold: 3
  loop_window: 20
  error_rate_threshold: 0.5
  on_anomaly: escalate
  cooldown: 300
  escalation:
    webhook_url: "https://hooks.slack.com/services/XXX"
    event_type: agent.health.anomaly
```

Load it with `promptise` or `AgentRuntime.load_directory("agents/")` and the process comes up governed. Nothing else about your agent changes — same model, same instructions, same tools. Governance is additive and opt-in: both blocks default to `enabled: false` with zero overhead, so you can commit the manifest and flip the switch when the agent graduates from prototype to production.

## What other frameworks do today

To be fair and precise: every serious framework ships *something* in this space, and each is real and useful within its scope. The gap is not that they can't count — it's that there's no single declarative place to say all of this at once.

- **LangChain** — `AgentExecutor` takes `max_iterations` (default 15) and `max_execution_time`. On breach it stops that executor run (or forces a final answer via `early_stopping_method`). It's a step/time cap inside one `.invoke()`/`.ainvoke()`.
- **LangGraph** — a `recursion_limit` (default 25) on graph execution; exceeding it raises `GraphRecursionError` inside a single graph invocation.
- **Pydantic AI** — `UsageLimits` (`request_limit`, `total_tokens_limit`, and friends) passed into a run; breaching one raises `UsageLimitExceeded` mid-run.
- **AutoGen** — teams like `RoundRobinGroupChat` accept `max_turns` plus termination conditions (`MaxMessageTermination`, token-usage termination) that end the conversation.
- **CrewAI** — an `Agent`'s `max_iter` caps reasoning iterations per task and `max_rpm` self-throttles the outbound request rate.

Every one of these is a single blunt per-run control, enforced as an exception or termination you catch inside one invocation. That's the exact delta. None of them gives you **one config block** that combines, and enforces for you: a weighted-cost budget with a *separate* irreversible-action cap, behavioral-health detectors (stuck / loop / empty-response / error-rate) that run without any LLM calls, and an escalation that fires a webhook and halts the process out-of-band. To assemble that today you scatter `try/except` limit-catching through your loop, hand-roll a monitor that tracks tool-call history, decide per breach whether to keep firing triggers, and wire the alerting yourself — code you own, test, and maintain. Promptise's edge is not that those frameworks lack limits; it's that Promptise makes budget, health, and escalation a **first-class, structural property of the running process** — one block the runtime enforces — instead of a value you thread through supervising code you wrote.

## Choosing pause vs stop vs escalate — and wiring the webhook

The last decision is which action each trip wire takes. The semantics are identical across budget and health (minus health's missing `stop`), and each maps to a real lifecycle transition on the supervised process:

| Action | What the runtime does |
|--------|-----------------------|
| `pause` | Suspend the process (`RUNNING → SUSPENDED`); an operator or the runtime can resume it |
| `stop` | End it (`RUNNING → STOPPING → STOPPED`); requires a manual restart — the true kill switch (budget only) |
| `escalate` | Fire the escalation notification, then suspend |
| `log` | Record the anomaly and continue (health only) |

A practical default: budget `on_exceeded="stop"` for the runaways you never want auto-recovered (cost blowouts, too many irreversible actions), and health `on_anomaly="escalate"` so a human sees a stuck or looping agent before deciding. When either subsystem escalates, it POSTs a JSON payload to `escalation.webhook_url` and emits `escalation.event_type` on the shared EventBus, then suspends — fire-and-forget, so a slow webhook never blocks the agent. Both subsystems share the same `escalate()` path, so one Slack incoming-webhook URL covers the lot. The payload includes the process name, the violation, and a timestamp, which is enough to route straight to an on-call channel. Every breach is also written to the process journal, so even a `stop` leaves a full trail for the post-mortem.

## Frequently asked questions

### Do I have to enable both budget and health?

No. Both default to `enabled=False` with zero overhead, and each works standalone. But they catch different runaways: budget stops an agent that does *too much*, health stops one that does the *same cheap useless thing forever* — the runaway a cost budget will happily fund. In production you usually want both.

### Can behavioral health hard-stop the process by itself?

No — `on_anomaly` accepts only `log`, `pause`, and `escalate`. For a hard stop on a behavioral anomaly, set `on_anomaly="escalate"` and have the on-call engineer pull `process.stop()`, or rely on the budget's `on_exceeded="stop"` for cost and irreversible-action runaways. Keeping health on `pause`/`escalate` is intentional: anomalies are often recoverable, and suspending preserves the journal for diagnosis.

### Is `max_cost_per_day` measured in dollars?

No. Cost is abstract weight units you assign via `ToolCostAnnotation.cost_weight`, plus counts of tool calls and LLM turns. Promptise never connects to a provider's pricing API. It governs agent *behavior*; for real monetary limits, track token spend through your provider dashboard or Promptise observability. See the [Autonomy Budget reference](../../runtime/governance/budget.md) for the three weighting approaches.

### What's the difference between this and a `max_iterations` cap?

`max_iterations` bounds work *inside a single invocation* and resets to zero on the next trigger, so an agent that loops *across* invocations (cron, webhook, file-watch) is never caught. Budget and health act on the supervised process itself via `pause`/`stop`/`escalate`, so the action lands on the thing that keeps launching invocations. The full argument is in [How to Stop a Runaway AI Agent](stop-a-runaway-ai-agent.md).

### Does the agent know about its own budget?

Yes, when `inject_remaining=True` (the default). Before every invocation the runtime injects a `[Budget Remaining]` line into the system prompt, so the agent can prioritize cheaper tools or skip non-essential work as it runs low. In open mode it can also call the `check_budget` meta-tool to inspect limits programmatically.

## Next steps

Copy the `ProcessConfig` governance block above, drop in your own per-run and daily limits, your irreversible-action cap, and a Slack webhook, and you have a self-limiting agent in one file. Then:

- Tune the weights and detectors against the [Autonomy Budget reference](../../runtime/governance/budget.md) and the [Behavioral Health reference](../../runtime/governance/health.md).
- See how processes, triggers, and governance fit together in the [Agent Runtime overview](../../runtime/index.md).
- Understand *why* this beats a per-run counter in [How to Stop a Runaway AI Agent](stop-a-runaway-ai-agent.md), and dig into the trickiest failure mode in [Catch an AI Agent Stuck Repeating the Same Tool Call](ai-agent-stuck-repeating-tool-call.md).

Flip `enabled: true` before your agent ever runs unattended — governance is cheapest to add on day one and most expensive to add after the first 3 a.m. incident.
