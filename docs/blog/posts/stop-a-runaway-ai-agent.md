---
title: "How to Stop a Runaway AI Agent (Runtime Kill Switches)"
description: "A max_iterations or recursion_limit cap raises an exception inside a single ainvoke() that you must catch — which does nothing to halt an agent already…"
keywords: "stop a runaway ai agent, runaway agent kill switch, ai agent kill switch, stop autonomous agent loop, ai agent runtime guardrails, runtime kill switch for llm agents"
date: 2026-07-16
slug: stop-a-runaway-ai-agent
categories:
  - Governance
---

# How to Stop a Runaway AI Agent (Runtime Kill Switches)

To reliably stop a runaway AI agent you need a kill switch that acts on the *process*, not a counter that raises an exception inside one function call. That distinction is the whole game. Almost every framework hands you a per-run cap — `max_iterations`, `recursion_limit`, a usage limit — and calls it a safety control. But those caps raise an exception inside a single `ainvoke()`, and an exception only exists if there is a caller sitting there to catch it. The moment your agent is running unattended on a cron, a webhook, or a file trigger, there is no such caller: each trigger fires a fresh invocation, the counter resets to zero, and nothing halts the thing that is actually misbehaving. This post reframes the runaway agent kill switch as *runtime enforcement* — budget, behavioral-health, and mission breaches that become out-of-band `pause`/`stop`/`escalate` actions on a supervised `AgentProcess` — and shows you how to wire one in a few lines.

The thesis in one line: a counter that throws is not a kill switch. A runtime that halts the process is.

## Why a counter that throws is not a kill switch

Picture a support agent on a five-minute cron. Each tick, your scheduler calls `await agent.ainvoke(...)`. Inside that call, a per-run cap does its job perfectly: if the agent loops past `max_iterations`, the invocation ends. Good.

Now the loop the cap was meant to catch is not *inside* one invocation — it is *across* invocations. The agent finishes a run cleanly, the cron fires again 300 seconds later, and it does the same useless, expensive thing. The counter reset at zero at the top of every run, so it never trips. Your "kill switch" is a speed bump the trigger drives around every five minutes.

Three properties of a real kill switch fall out of this:

- **It lives above the invocation, not inside it.** A single `ainvoke()` cannot police the process that keeps launching new invocations.
- **It acts, it does not just raise.** A raised exception is a message to a caller. If the failure mode is a trigger-driven process, the action you want is to *stop the process* — transition it out of `RUNNING` so no further triggers fire.
- **It watches behavior, not just quantity.** "Made 30 calls" and "made the same 30 calls in a loop while every dependency was 500-ing" are very different runaways. A single integer cannot tell them apart.

This is exactly the gap between a per-run counter and ai agent runtime guardrails.

## What other frameworks do today

To be fair and precise, every serious framework ships *something* here, and each of these is real and useful within its scope:

- **LangChain** — `AgentExecutor` takes `max_iterations` (default 15) and `max_execution_time`. When exceeded, it stops that executor run and returns (or forces a final answer via `early_stopping_method`). It is a step cap inside one `.invoke()`/`.ainvoke()`.
- **LangGraph** — a `recursion_limit` (default 25) on graph execution. Exceeding it raises `GraphRecursionError` inside a single graph invocation.
- **Pydantic AI** — `UsageLimits` (`request_limit`, `total_tokens_limit`, and friends) passed into a run. Breaching one raises `UsageLimitExceeded` mid-run.
- **AutoGen** — teams like `RoundRobinGroupChat` accept `max_turns` and termination conditions (`MaxMessageTermination`, token-usage termination) that end the conversation.
- **CrewAI** — an `Agent`'s `max_iter` caps reasoning iterations per task, and `max_rpm` self-throttles the outbound request rate.

Every one of these is a *single blunt per-run control enforced as an exception (or termination) you catch inside one invocation or conversation*. That is the exact delta. None of them combine, in one policy: behavioral anomaly detection (identical-call, repeating-pattern, empty-response, error-rate), an irreversible-action cap distinct from the total-call cap, and an LLM-judge stop condition — and then halt a supervised, trigger-driven process *out-of-band* with an escalation. To get that today, you assemble the supervising loop, catch the exception, decide whether to keep firing triggers, and wire the alerting yourself. Promptise's edge is not that these frameworks "can't count" — they count fine — it is that Promptise makes the kill switch a first-class property of the running process instead of a value you thread through a loop you wrote.

## Runtime enforcement: out-of-band pause, stop, and escalate

Promptise Foundry's [Agent Runtime](../../runtime/index.md) wraps your `build_agent()` in a supervised `AgentProcess` with a real lifecycle: `CREATED → STARTING → RUNNING → STOPPING → STOPPED`, plus `SUSPENDED` and `FAILED`. Triggers drive invocations against a process in `RUNNING`; governance runs *around* every one of those invocations, not inside your prompt.

Three subsystems act as independent trip wires, each with its own enforcement action:

| Subsystem | Catches | Enforcement actions |
|-----------|---------|---------------------|
| **Budget** | Runaway cost, too many tool calls, too many irreversible actions | `pause`, `stop`, `escalate` |
| **Health** | Stuck agents, tool loops, empty responses, high error rate | `log`, `pause`, `escalate` |
| **Mission** | Trajectory drift away from the goal | escalate on low confidence; auto-stop on success |

The important word is *out-of-band*. When a budget limit is breached, the enforcer does not throw into your call stack — it calls `process.stop()` (or `process.suspend()`) on the supervised process. `stop` transitions `RUNNING → STOPPING → STOPPED` and requires a manual restart; `pause` moves it to `SUSPENDED` so an operator or the runtime can resume it; `escalate` fires a webhook plus an EventBus event and then suspends. Because the action lands on the process, the *next* cron tick or webhook has nothing to run against — the runaway is actually stopped, not merely interrupted for one turn. Every breach is also written to the process journal, and all three subsystems share the same `escalate()` path, so a single Slack integration covers the lot.

## Runnable: wrap build_agent() in a governed process

Here is a complete, runnable process that installs all three kill switches. Every symbol below is a real runtime API — no invented signatures.

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
    ProcessState,
)


async def main() -> None:
    config = ProcessConfig(
        model="openai:gpt-5-mini",
        instructions="Reconcile open payment disputes until the queue is clear.",
        # Kill switch 1 — Budget: hard-stop on runaway cost or destructive actions
        budget=BudgetConfig(
            enabled=True,
            max_tool_calls_per_run=25,
            max_cost_per_day=300.0,          # abstract units you define, NOT dollars
            max_irreversible_per_run=3,      # separate cap on charges/deletes/sends
            tool_costs={
                "issue_refund": ToolCostAnnotation(cost_weight=10.0, irreversible=True),
                "send_email":   ToolCostAnnotation(cost_weight=2.0, irreversible=True),
                "search":       ToolCostAnnotation(cost_weight=0.5),
            },
            on_exceeded="stop",              # RUNNING -> STOPPING -> STOPPED, manual restart
            inject_remaining=True,           # the agent sees its remaining budget
        ),
        # Kill switch 2 — Health: escalate on a stuck or looping agent (no LLM calls)
        health=HealthConfig(
            enabled=True,
            stuck_threshold=3,               # same tool + args 3x in a row = stuck
            loop_window=20,                  # scan last 20 calls for repeating patterns
            error_rate_threshold=0.5,        # 50%+ failing calls = anomaly
            on_anomaly="escalate",           # webhook + EventBus, then suspend
            escalation=EscalationTarget(
                webhook_url="https://hooks.slack.com/services/XXX",
                event_type="agent.health.anomaly",
            ),
        ),
        # Kill switch 3 — Mission: stop when done, escalate when confidence drops
        mission=MissionConfig(
            enabled=True,
            objective="Reconcile every open payment dispute",
            success_criteria="Zero disputes remain in the open queue",
            eval_every=3,                    # LLM-as-judge every 3 invocations
            confidence_threshold=0.7,        # below this -> escalate
            timeout_hours=8,
            auto_complete=True,              # success -> STOPPING -> STOPPED
        ),
    )

    process = AgentProcess(name="dispute-agent", config=config)
    await process.start()
    print(process.state)                     # ProcessState.RUNNING

    # Triggers now fire invocations; budget, health, and mission run out-of-band on
    # every one. A breach halts the PROCESS — not just the current ainvoke().

    await process.stop()                     # explicit operator kill switch
    assert process.state is ProcessState.STOPPED


asyncio.run(main())
```

That is the entire wiring. `on_exceeded="stop"` is your automatic hard kill switch for cost and destructive-action runaways; `await process.stop()` is the manual one an on-call engineer can pull. Note one honest detail baked into the budget: `max_cost_per_day` is measured in **abstract weight units you define, not dollars** — Promptise never connects to a provider's pricing API. It governs *what the agent does*, not what your LLM bill is. The full weighting guidance is in the [Autonomy Budget reference](../../runtime/governance/budget.md).

## Which trip wire catches which runaway

The three subsystems are not redundant — each catches a runaway the others cannot see.

**Budget** stops an agent that does *too much*. It caps total tool calls, weighted cost units, and — critically — irreversible actions separately, so you can grant a generous read budget while allowing only three refunds per run. Set `on_exceeded="stop"` and a cost blowout ends the process; set `"escalate"` and it pages a human first.

**Health** stops an agent that does the *same useless thing cheaply forever* — the runaway a cost budget will happily fund. Detection is pure pattern matching over tool-call history with **no LLM calls**, so it is free and instant: the `stuck` detector fires after N identical calls, and the `loop` detector catches repeating multi-tool sequences. This is the surgical way to stop an autonomous agent loop; the mechanics are in the [Behavioral Health reference](../../runtime/governance/health.md), and the specific stuck case is walked through in [Catch an AI Agent Stuck Repeating the Same Tool Call](ai-agent-stuck-repeating-tool-call.md).

**Mission** stops an agent whose every step looks fine but which has quietly drifted off the goal. An LLM-as-judge evaluates progress every `eval_every` invocations; if confidence drops below your threshold it escalates, and if the objective is met with `auto_complete=True` the process stops itself. When mission (or budget or health) escalates, it hands off cleanly to a human — the pattern in [Escalate to a Human When an AI Agent Keeps Failing](escalate-ai-agent-to-human.md).

## When a max-iteration cap is genuinely enough

Runtime kill switches earn their keep for agents that run unattended and take real-world actions. They are honest overkill in a few cases, and a plain per-run cap is the simpler right answer:

- **Request-response chatbots.** A human reads every reply and there is no autonomous loop across invocations. A `build_agent()` with a sensible max-iteration cap is all you need — there is no process trajectory to police.
- **Single-shot API handlers.** One tool call behind an endpoint does not need per-day budgets or an LLM judge. The ceremony outweighs the benefit.
- **You already own an external supervisor.** If Temporal, Airflow, or a similar system already enforces stop conditions and alerting around the agent, layer Promptise's health and mission checks for the behavior it can see and let the orchestrator own scheduling.

The line is crossed the moment an agent runs on a trigger, calls tools that cost money or cannot be undone, and needs to keep going until a goal is met. That is precisely when a counter-that-throws stops being a kill switch and a process-that-halts starts being one.

## Frequently asked questions

### Does `max_iterations` or `recursion_limit` stop a runaway agent running on a cron?

No. Those caps bound work *inside a single invocation* and raise (or terminate) when exceeded. A cron/webhook/file trigger starts a fresh invocation each time, resetting the counter, so an agent that loops across invocations is never caught. To stop that, the control has to act on the process itself — which is what a `stop`/`pause`/`escalate` enforcement action on a supervised `AgentProcess` does.

### What is the difference between `pause` and `stop`?

`pause` suspends the process (`RUNNING → SUSPENDED`); it can be resumed by an operator or the runtime, so it is the right choice when the runaway might be transient. `stop` ends the process (`RUNNING → STOPPING → STOPPED`) and requires a manual restart — the true kill switch for a breach you never want to auto-recover from. `escalate` notifies a human via webhook and EventBus, then suspends.

### Can behavioral health hard-stop the process by itself?

Health's enforcement actions are `log`, `pause`, and `escalate`. For a *stop*, either point health at `escalate` and let the on-call engineer pull `process.stop()`, or rely on the budget's `on_exceeded="stop"` for cost and irreversible-action runaways. Keeping health on `pause`/`escalate` is deliberate: a behavioral anomaly is often recoverable, and suspending preserves the journal for diagnosis.

### Are the budget's cost units real dollars?

No. Cost is abstract weight units you assign via `ToolCostAnnotation`, plus counts of tool calls and LLM turns. Promptise never queries a provider's pricing API. The budget governs agent *behavior*; for real monetary limits, track token spend through your provider dashboard or Promptise observability.

## Next steps

Wrap your existing `build_agent()` in an `AgentProcess` and set your first budget plus health kill switch before it ever runs unattended — start from the runnable process above, then tune the thresholds to your tools. Read the [Agent Runtime overview](../../runtime/index.md) for how processes and triggers fit together, and use the [Autonomy Budget reference](../../runtime/governance/budget.md) and [Behavioral Health reference](../../runtime/governance/health.md) to design the exact envelope your agent must never leave.
