---
title: "Governing Autonomous Agents: Budget & Health"
description: "The question every tech lead asks before shipping an autonomous agent: what stops it from looping forever or draining my tool budget? Covers the concrete…"
keywords: "autonomous agent governance, AI agent budget limits, agent guardrails runtime, stop agent infinite loop, agent health anomaly detection, mission-oriented agent"
date: 2026-07-16
slug: autonomous-agent-governance
categories:
  - Runtime
---

# Governing Autonomous Agents: Budget & Health

Autonomous agent governance is the set of controls that answer the one question every tech lead asks before an agent runs unattended in production: what stops it from looping forever, calling an expensive API 4,000 times, or issuing a refund at 3am with nobody watching? A `build_agent()` call gives you a capable request-response agent, but the moment you let it run on a trigger with no human in the loop, "capable" is not enough — you need an envelope it cannot exceed. By the end of this post you will know the concrete controls Promptise Foundry ships for that envelope, and how to wrap your own agent in a budget plus a health policy with a real hard stop.

## The go/no-go checklist for an unattended agent

An autonomous agent is just a stateless LLM wired to tools and a trigger. Left alone, three failure modes show up in production over and over:

- **Runaway cost** — the agent keeps calling tools (or the model) in a loop, and the bill arrives at the end of the month.
- **Silent stalls** — the agent gets stuck calling the same tool with the same arguments, or bounces between two tools indefinitely, making zero progress.
- **Trajectory drift** — every individual step looks reasonable, but the agent quietly wanders away from the goal it was launched to accomplish.

Promptise's runtime addresses each with a dedicated, opt-in governance subsystem: **Budget** for cost, **Health** for behavioral anomalies, and **Mission** for goal adherence — all three tied together by an **Escalation** path that pages a human. The [Agent Runtime overview](../../runtime/index.md) describes how these sit on top of an `AgentProcess`. This post is the practical go/no-go checklist: which control catches which failure, and what to configure before you flip the switch.

## AI agent budget limits: per-run and daily caps

The budget is your primary defense against runaway cost. It sets explicit AI agent budget limits on how much work an agent can do — measured in tool calls, LLM turns, and abstract "cost units" — across two scopes:

- **Per-run** — resets at the start of every trigger-driven invocation.
- **Per-day** — resets at a configurable UTC hour, capping total daily activity.

You annotate each tool with a `ToolCostAnnotation` so a Stripe charge weighs more than an internal search. One critical honesty point: the cost is **abstract weight units you define, not dollars**. Promptise does not connect to any provider's pricing API, so `cost_weight=10.0` means "ten budget units," not "$10." The budget governs *what the agent does* — how many tools it calls, how many irreversible actions it takes — not what your LLM provider charges. That distinction, and the recommended relative-weighting approach, is spelled out in the [Autonomy Budget reference](../../runtime/governance/budget.md).

Tools flagged `irreversible=True` (charges, deletes, sends) are also tracked separately, so you can allow the agent a generous read budget while capping destructive actions with `max_irreversible_per_run`.

## Agent health anomaly detection: stop an agent infinite loop

A budget stops an agent that does *too much*. It will not catch an agent that does the *same useless thing* cheaply forever. That is where behavioral health comes in — and it is how you actually stop an agent infinite loop before it burns your context window.

Health monitoring is pure pattern matching over tool-call and response history. It makes **no LLM calls**, so it is effectively free to run and adds no latency. Four detectors cover the common stall shapes:

- **Stuck** — the same tool with the same arguments called `stuck_threshold` times in a row.
- **Loop** — a repeating sequence like `search → read → analyze → search → read → analyze` inside the last `loop_window` calls.
- **Empty response** — several consecutive trivial (near-empty) outputs, signalling the agent has run out of anything useful to say.
- **High error rate** — a sliding-window failure rate above `error_rate_threshold`, which usually means an upstream dependency is down.

When agent health anomaly detection fires, you choose the action: `log` and continue, `pause` the process, `stop` it outright, or `escalate`. A cooldown prevents alert storms from the same anomaly type. Full detector semantics live in the [Behavioral Health reference](../../runtime/governance/health.md).

## Mission-oriented agents: LLM-as-judge plus a programmatic success_check

Budget and health are guardrails; they tell the agent when to stop misbehaving. They say nothing about whether the agent is *achieving its goal*. For long-running work — clearing a support queue, migrating a schema, monitoring a deployment until it stabilizes — you want a mission-oriented agent that runs until success is confirmed, then stops on its own.

The Mission subsystem evaluates progress every `eval_every` invocations using LLM-as-judge: a separate `eval_model` reads the recent conversation against your `objective` and `success_criteria` and returns a structured verdict (achieved, confidence, reasoning, progress summary). If confidence drops below your threshold, it escalates. If the objective is met and `auto_complete=True`, the process stops itself.

Because an LLM judge is probabilistic, Promptise also supports a **programmatic `success_check`** — a plain `(MissionEvidence) -> bool | None` callable that runs *before* the judge. Return `True` for done, `False` for not done, or `None` to defer to the LLM. This lets you encode objective, deterministic completion criteria ("the open-ticket count is zero," "all tables pass v2 validation") and reserve the model for the fuzzy cases. See the [Mission-Oriented Process reference](../../runtime/governance/mission.md) for the evaluation cycle and evidence bundle.

## Wiring it together: one governed process

Here is a complete, runnable process that layers all three controls plus escalation. Everything below uses real runtime APIs — no invented signatures.

```python
import asyncio
from promptise.runtime import (
    AgentProcess,
    ProcessConfig,
    BudgetConfig,
    HealthConfig,
    MissionConfig,
    EscalationTarget,
    ToolCostAnnotation,
)

async def main():
    config = ProcessConfig(
        model="openai:gpt-5-mini",
        instructions="Triage and resolve open support tickets until the queue is clear.",
        # 1) Budget: cap cost and destructive actions
        budget=BudgetConfig(
            enabled=True,
            max_tool_calls_per_run=20,
            max_cost_per_day=200.0,          # abstract units, not dollars
            max_irreversible_per_run=2,
            tool_costs={
                "refund_customer": ToolCostAnnotation(cost_weight=10.0, irreversible=True),
                "send_email":      ToolCostAnnotation(cost_weight=2.0, irreversible=True),
                "search_tickets":  ToolCostAnnotation(cost_weight=0.5),
            },
            on_exceeded="escalate",
            inject_remaining=True,           # agent sees its remaining budget
            escalation=EscalationTarget(webhook_url="https://hooks.slack.com/services/XXX"),
        ),
        # 2) Health: stop stuck agents and infinite loops (no LLM calls)
        health=HealthConfig(
            enabled=True,
            stuck_threshold=3,
            loop_window=20,
            error_rate_threshold=0.5,
            on_anomaly="escalate",
        ),
        # 3) Mission: run until the queue is empty, then auto-complete
        mission=MissionConfig(
            enabled=True,
            objective="Resolve every open support ticket",
            success_criteria="Zero tickets remain in the open queue",
            eval_every=3,
            confidence_threshold=0.7,
            timeout_hours=8,
            auto_complete=True,
        ),
    )

    process = AgentProcess(name="support-agent", config=config)
    await process.start()
    # Triggers fire invocations; budget, health, and mission run automatically.
    # ... run until the mission auto-completes or an operator stops it ...
    await process.stop()

asyncio.run(main())
```

With `inject_remaining=True`, the agent sees its remaining budget in its own system prompt, so it can prioritize cheap tools when it is running low. Every violation and anomaly is recorded in the process journal, and each governance subsystem shares the same `escalate()` path — a webhook POST plus an EventBus event — so a single Slack integration covers all three. The same policy can also be declared in a `.agent` manifest instead of Python; both forms are shown side by side in the reference pages.

## When a lighter setup is the better fit

Full runtime governance is worth it for agents that run unattended and take real-world actions. It is overkill in a few honest cases:

- **Request-response chatbots.** If a human reads every reply and no autonomous loop exists, a plain `build_agent()` with a sensible max-iteration cap is simpler. There is no unattended trajectory to police.
- **Short, one-shot tasks.** A single tool call behind an API endpoint does not need per-day budgets or mission evaluation. The ceremony outweighs the benefit.
- **You already own an external orchestrator.** If Temporal, Airflow, or a similar system already enforces retries, timeouts, and spend limits around the agent, layering Promptise's budget on top can duplicate controls. Use health and mission for the behavior it can see, and let the orchestrator own scheduling.

Governance earns its keep exactly when an agent runs on triggers, calls tools that cost money or cannot be undone, and needs to keep going until a goal is met. If none of that is true, start lighter. For the broader picture of when you cross that line, see [What Is an Autonomous AI Agent Runtime?](autonomous-ai-agent-runtime.md).

## Frequently asked questions

### Does the budget track my real dollar spend on OpenAI or Anthropic?

No. The budget measures abstract cost units you define through `ToolCostAnnotation`, plus counts of tool calls and LLM turns. It governs agent *behavior*, not provider pricing — Promptise never connects to a billing API. For real monetary limits, track token usage through your provider dashboard or Promptise observability, and use the budget to cap the actions that matter. The [budget reference](../../runtime/governance/budget.md) walks through both approaches.

### How do I stop an agent that is stuck in an infinite loop?

Enable `HealthConfig` with `on_anomaly="stop"` (or `"escalate"`). The stuck detector fires after `stuck_threshold` identical calls, and the loop detector catches repeating multi-tool patterns within `loop_window`. Detection is pure pattern matching with no LLM calls, so it reacts immediately and costs nothing.

### What is the difference between a budget limit and a mission?

A budget is a hard ceiling on *how much* an agent may do; a mission defines *what done looks like* and evaluates progress toward it. Budget and health keep a misbehaving agent inside its envelope, while the mission decides when the work is finished and stops the process on success — optionally via a deterministic `success_check` before the LLM judge runs.

## Next steps

Wrap your agent in a budget plus a health policy and set your first hard stop before production — start from the runnable process above, then tune the thresholds to your tools. Read the [Quick Start](../../getting-started/quickstart.md) to build the underlying agent, follow [How to Build a Long-Running AI Agent](long-running-ai-agent.md) to put it on a trigger, and use the [Mission-Oriented Process reference](../../runtime/governance/mission.md) to make it run until the job is genuinely done.
