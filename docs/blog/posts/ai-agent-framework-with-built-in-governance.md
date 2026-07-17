---
title: "Which AI Agent Framework Has Built-In Governance?"
description: "Ask most frameworks to cap an agent at a cost ceiling, a limit on irreversible actions, or a mission it must actually make progress on, and the answer is…"
keywords: "ai agent framework with built-in governance, agent cost cap framework, irreversible action budget agent, which framework has agent governance, autonomous agent guardrails comparison"
date: 2026-07-16
slug: ai-agent-framework-with-built-in-governance
categories:
  - Comparisons
---

# Which AI Agent Framework Has Built-In Governance?

If you are shopping for an **AI agent framework with built-in governance** — one where you can cap an agent at a cost ceiling, bound the number of irreversible actions it may take, and hold it to a mission it must actually make progress on — the honest state of the market is that most frameworks answer "write it yourself." They ship a limiter, and a limiter is a real, useful thing. But a single step counter is not a governance envelope, and the gap between the two is exactly where unattended agents get expensive or dangerous. This post maps what governance each popular framework really ships, credits the single-dimension caps some of them provide, and shows the four first-class subsystems Promptise Foundry puts in the box — budget, health, mission, and secrets — each with the same `log`/`pause`/`stop`/`escalate` enforcement path.

## What built-in governance actually has to cover

"Governance" is a vague word, so pin it down. A governance layer is what stands between a capable agent and an unattended one. The moment you take the human out of the loop and put the agent on a trigger, four distinct failure modes appear, and a real governance layer has an answer for each:

- **Runaway cost and risk.** The agent keeps calling tools — and not all tools are equal. Twenty searches are fine; twenty wire transfers are a page-your-CFO incident. You need a budget that weighs a `stripe_charge` differently from a `search`, plus a separate ceiling on irreversible actions.
- **Silent stalls.** The agent gets stuck calling the same tool with the same arguments, or loops between two tools forever, each call comfortably cheap. A cost cap never trips; the agent makes zero progress and burns your context window. You need behavioral anomaly detection.
- **Trajectory drift.** Every individual step looks reasonable, but the agent has quietly wandered away from the goal it was launched to accomplish. You need something that evaluates *progress toward a mission*, not just per-step legality.
- **Credential sprawl.** A long-running agent needs secrets — an API key, a DB password — and those secrets need a lifecycle: scoped to the process, expiring on a TTL, rotatable without a restart, and wiped on stop. Leaving them in shared environment variables is the anti-pattern humans already abandoned.

A limiter addresses a slice of the first mode. A governance layer addresses all four, and ties them to a single escalation path so one integration pages a human regardless of which control fired.

## What other frameworks do today

To be fair and precise: every serious framework ships *some* self-imposed limit, and each is real within its scope. The gap is not that competitors "can't count" — they count fine. It is that what they ship is a single-dimension step or rate cap, not a governance envelope.

- **LangGraph** — `recursion_limit` (default 25) bounds how many super-steps a graph may execute in one run. Exceed it and the run raises `GraphRecursionError`. It is a hard ceiling on graph steps: it stops a runaway loop, but every step is worth the same, there is no separate notion of an irreversible action, and it hard-errors rather than pausing or escalating.
- **CrewAI** — `max_iter` caps how many reasoning iterations an agent may take before it must return an answer, and `max_rpm` self-throttles outbound requests per minute at the agent or crew level. Both are genuine caps. Neither weighs one tool against another, tracks irreversible actions separately, or watches the *shape* of the call history for a stall.
- **Pydantic AI** — `UsageLimits` bounds a run by request and token counts (`request_limit`, `total_tokens_limit`, `response_tokens_limit`, and related counters); breaching one raises `UsageLimitExceeded` mid-run. This is finer-grained than a step count — it can stop on tokens — but every request and every token is still worth the same.
- **AutoGen** — exposes conversation-turn caps such as `max_turns` on its team/group-chat patterns (and `max_consecutive_auto_reply` on the older conversable agents). That halts an infinite back-and-forth, but it is a turn ceiling, not a cost or action budget.

State the delta exactly. None of these expresses a *risk-weighted* cost budget where a `stripe_charge` draws the envelope down twenty times faster than a `search`; none maintains a *separate* cap on irreversible actions distinct from the total-call cap; none surfaces the remaining budget back into the model's own prompt so the agent economizes on its own; none detects behavioral anomalies (stuck / loop / empty / high-error) over call history; none evaluates progress toward a declared mission; and none manages a per-process secret lifecycle. You can approximate several of these by hand — wrap each tool, keep a weighted tally, diff the last N calls, raise when a threshold trips — but then the governance lives in glue code you own and maintain, not in policy. Promptise's edge is structural: it makes budget, health, mission, and secrets four first-class subsystems, each enforced by the runtime rather than by your wrapper. The broader "what's still left to you" ledger for each framework is worked through in [Enterprise-Ready Agent Framework Checklist: What's Left to You](enterprise-ready-agent-framework-checklist.md); this post drills into the governance row.

## Promptise's four governance subsystems

Promptise's runtime attaches governance to an `AgentProcess` — the supervised, trigger-driven container around a `build_agent()` agent. Each subsystem is opt-in configuration, not a control loop you write:

- **Budget** — per-run and per-day caps on tool calls, LLM turns, and abstract cost units, plus a separate `max_irreversible_per_run` ceiling. You annotate each tool with a `ToolCostAnnotation` so risk-weighted spend, not raw call count, drives the limit, and with `inject_remaining=True` the agent sees its remaining envelope in its own system prompt. The full weighting guidance lives in the [Autonomy Budget reference](../../runtime/governance/budget.md).
- **Health** — pure pattern matching over tool-call and response history, with **no LLM calls**, so it is effectively free and adds no latency. Four detectors cover the common stalls: *stuck* (same tool, same args, N times), *loop* (a repeating multi-tool sequence inside a window), *empty response*, and *high error rate*. See the [Behavioral Health reference](../../runtime/governance/health.md).
- **Mission** — turns a task-runner into a goal-driven process. Every `eval_every` invocations, a separate `eval_model` reads the recent conversation against your `objective` and `success_criteria` and returns a structured verdict (achieved, confidence, reasoning, progress). Because an LLM judge is probabilistic, a programmatic `success_check` callable runs *first* for deterministic completion criteria. On success with `auto_complete=True`, the process stops itself. Details in the [Mission-Oriented Process reference](../../runtime/governance/mission.md).
- **Secrets** — a per-process credential context with `${ENV_VAR}` resolution, TTL-based expiry, rotation without restart, access logging in the journal, and zero-fill revocation on stop. Values are never serialized to journal, checkpoint, or status. Covered in the [Secret Scoping reference](../../runtime/governance/secrets.md).

The through-line is enforcement. Every subsystem shares one `on_exceeded` / `on_anomaly` vocabulary — `log` and continue, `pause` the process, `stop` it outright, or `escalate` — and a single `escalate()` path (a webhook POST plus an EventBus event), so one Slack integration covers all four. One honest boundary, stated up front: the budget's cost is measured in **abstract weight units you define, not real LLM dollars.** Promptise never queries a provider's pricing API. This is a deliberate design choice explained in [Why Promptise Foundry](../../getting-started/why-promptise.md) — the budget governs *what the agent does*, and you pair it with provider-side token metrics for the monetary side.

## Runnable: a risk-weighted budget that trips before a count cap

Here is the difference between counting and governing, made concrete. The script below is fully runnable with no API key and no network — it drives the real `BudgetState` accounting directly so you can watch cheap and expensive calls draw the same envelope down at very different rates. Every symbol is an exported runtime API.

```python
import asyncio
from promptise.runtime import BudgetConfig, BudgetState, ToolCostAnnotation


async def main() -> None:
    budget = BudgetConfig(
        enabled=True,
        max_tool_calls_per_run=50,      # generous raw-call ceiling...
        max_cost_per_run=20.0,          # ...but only 20 risk-weighted units
        max_irreversible_per_run=2,     # at most two destructive actions
        tool_costs={
            "search_tickets": ToolCostAnnotation(cost_weight=0.5),
            "read_invoice":   ToolCostAnnotation(cost_weight=1.0),
            "send_email":     ToolCostAnnotation(cost_weight=3.0, irreversible=True),
            "issue_refund":   ToolCostAnnotation(cost_weight=10.0, irreversible=True),
        },
    )
    state = BudgetState(budget)

    # A read-heavy plan barely touches the envelope.
    for _ in range(10):
        await state.record_tool_call("search_tickets")
    print(state.budget_context())

    # One refund draws down as much as twenty searches.
    v = await state.record_tool_call("issue_refund")
    print("after refund:", state.budget_context(), "| violation:", v)

    # A second refund blows the per-run envelope -> BudgetViolation.
    v = await state.record_tool_call("issue_refund")
    print("violation:", v.limit_name, f"{v.current_value}/{v.limit_value}", "tool:", v.tool_name)


asyncio.run(main())
```

Running it prints:

```text
[Budget] Tool Calls Run: 40 | Cost Run: 15.0 | Irreversible Run: 2
after refund: [Budget] Tool Calls Run: 39 | Cost Run: 5.0 | Irreversible Run: 1 | violation: None
violation: max_cost_per_run 25.0/20.0 tool: issue_refund
```

The whole argument is in those three lines. Ten searches consumed only 5.0 of the 20-unit envelope — the raw call counter barely moved and the agent has 15.0 units of headroom. A *single* `issue_refund` then consumed 10.0 units, dropping headroom to 5.0 while the call counter ticked up by exactly one. The second refund would push the run to 25.0 units, so `record_tool_call` returns a `BudgetViolation` naming `max_cost_per_run` and the tool that tripped it — the count cap of 50 was nowhere near firing. A step cap of 25 (LangGraph's default) or an iteration cap would have waved both refunds straight through; a risk-weighted envelope catches the exact runaway that a count cap cannot express.

## Wiring the whole envelope onto one process

In production you don't drive `BudgetState` by hand — you attach the configs to a supervised process and let the runtime do the recording, enforcement, injection, evaluation, and secret lifecycle around every trigger-driven invocation. Here all four subsystems share one escalation target, so a single webhook pages you no matter which control fires:

```python
from promptise.runtime import (
    ProcessConfig, BudgetConfig, HealthConfig, MissionConfig,
    SecretScopeConfig, EscalationTarget, ToolCostAnnotation,
)

pager = EscalationTarget(webhook_url="https://hooks.slack.com/services/XXX")

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Reconcile open payment disputes until the queue is clear.",
    # 1) Budget: risk-weighted cost + a hard cap on irreversible actions
    budget=BudgetConfig(
        enabled=True,
        max_tool_calls_per_run=40,
        max_cost_per_run=20.0,          # abstract units, not dollars
        max_cost_per_day=200.0,
        max_irreversible_per_run=2,
        tool_costs={
            "search_disputes": ToolCostAnnotation(cost_weight=0.5),
            "issue_refund":    ToolCostAnnotation(cost_weight=10.0, irreversible=True),
        },
        on_exceeded="escalate",
        inject_remaining=True,          # the agent sees its remaining envelope
        escalation=pager,
    ),
    # 2) Health: catch stalls and loops (no LLM calls, effectively free)
    health=HealthConfig(
        enabled=True,
        stuck_threshold=3,
        loop_window=20,
        error_rate_threshold=0.5,
        on_anomaly="pause",
        escalation=pager,
    ),
    # 3) Mission: run until the queue is empty, then auto-complete
    mission=MissionConfig(
        enabled=True,
        objective="Resolve every open payment dispute",
        success_criteria="Zero disputes remain in the open queue",
        eval_every=3,
        confidence_threshold=0.7,
        timeout_hours=8,
        auto_complete=True,
        escalation=pager,
    ),
    # 4) Secrets: scoped, TTL-bounded, zero-filled on stop
    secrets=SecretScopeConfig(
        enabled=True,
        secrets={"stripe_key": "${STRIPE_API_KEY}"},
        default_ttl=3600,
        ttls={"stripe_key": 1800},      # 30-min TTL for the payment key
        revoke_on_stop=True,
    ),
)
```

That single `ProcessConfig` is the whole governance envelope. Wrap it in an `AgentProcess`, call `await process.start()`, and every trigger-driven invocation is recorded against the budget, watched for anomalies, evaluated against the mission, and served scoped secrets — with one escalation path behind all of it. The same policy can be declared in a `.agent` YAML manifest instead of Python; both forms sit side by side in the reference pages. This is the "governance without building a control plane" property that also underpins production concerns like tenant isolation — the same "is it structural or is it your glue code?" question the honest tenancy answer turns on in [Does LangChain Support Multi-Tenancy?](does-langchain-support-multi-tenancy.md).

## Frequently asked questions

### Do LangGraph, CrewAI, Pydantic AI, or AutoGen have built-in governance?

They ship real self-imposed limits, but not a governance envelope. LangGraph's `recursion_limit` caps graph super-steps and hard-errors at the ceiling. CrewAI's `max_iter` caps reasoning iterations and `max_rpm` throttles outbound request rate. Pydantic AI's `UsageLimits` bounds requests and tokens per run. AutoGen exposes conversation-turn caps like `max_turns`. All are single-dimension step, rate, or turn caps — none weighs tool risk, tracks irreversible actions separately, detects behavioral anomalies, evaluates mission progress, or manages a secret lifecycle. Promptise makes those four things first-class subsystems.

### Does the budget track my real dollar spend on OpenAI or Anthropic?

No. The budget measures abstract cost units you define via `ToolCostAnnotation`, plus counts of tool calls, LLM turns, and irreversible actions. Promptise never connects to a provider's pricing API, so `cost_weight=10.0` means "ten budget units," not "$10." It governs agent *behavior*. For real monetary limits, cap `max_llm_turns_per_run` and track token spend through your provider dashboard or Promptise observability, which exports per-invocation token counts to Prometheus. The [budget reference](../../runtime/governance/budget.md) walks through both approaches.

### What is the difference between the budget and the health subsystem?

The budget stops an agent that does *too much* — it trips on cost, call count, or irreversible actions. Health catches an agent that does the *same useless thing cheaply forever*, which a cost cap never sees because each call sits inside the envelope. Health is pure pattern matching over call history with no LLM calls, so it reacts immediately and costs nothing. You typically enable both.

### How is the mission different from a timeout?

A timeout stops an agent after N hours regardless of whether it succeeded or is one step from done. A mission evaluates *progress toward a goal*: an `eval_model` judges the conversation against your success criteria every few invocations, a programmatic `success_check` handles deterministic completion, and the process auto-completes on success or escalates on low confidence. The mission still supports `timeout_hours` and `max_invocations` as backstops — it just doesn't rely on them to decide the agent is finished.

### Can I declare governance without writing Python?

Yes. Every subsystem shown here maps to a field in a `.agent` YAML manifest — `budget:`, `health:`, `mission:`, and `secrets:` — with the same options. The manifest form is shown alongside the Python form in each governance reference page, so you can keep policy in version-controlled config rather than code.

## Next steps

Put hard limits on your agents without building a control plane. Start with the runnable budget script above to feel how risk-weighted units behave, then layer the four-subsystem `ProcessConfig` onto a supervised process. Read the [Autonomy Budget reference](../../runtime/governance/budget.md) for the full cost-weighting guidance, the [Mission-Oriented Process reference](../../runtime/governance/mission.md) to make an agent run until the job is genuinely done, and [Why Promptise Foundry](../../getting-started/why-promptise.md) for the design choices behind shipping governance in the box. When you are ready, `pip install promptise` and give your first unattended agent an envelope it cannot exceed.
