---
title: "Cap Tool Calls and Cost Per AI Agent Run"
description: "A uniform iteration cap treats a search and a wire transfer identically; shows relative-risk cost_weight budgeting per run plus injecting remaining budget…"
keywords: "cap ai agent tool calls per run, limit agent cost per run, budget for ai agent, per-run tool call limit, tool cost weighting, limit llm agent actions"
date: 2026-07-16
slug: cap-ai-agent-tool-calls-per-run
categories:
  - Governance
---

# Cap Tool Calls and Cost Per AI Agent Run

To cap AI agent tool calls per run in a way that actually reflects risk, you have to stop counting calls and start pricing them — because a plain iteration cap treats a `search` and a `stripe_charge` as the same event. Set `max_iterations=20` and an agent can do twenty harmless lookups, or it can do nineteen lookups and one wire transfer, and your "safety limit" registers both runs as identical. The count is the wrong unit. What you want is a per-run *envelope* where a read costs almost nothing, an irreversible action costs a lot, and — crucially — the agent can see how much of the envelope it has left and spend accordingly. This post shows how to build that with Promptise Foundry's `BudgetConfig`: relative-risk `cost_weight` per tool, a separate cap on irreversible actions, and `inject_remaining` so the model self-prioritizes toward cheap tools first.

The thesis in one line: counting tool calls tells you *how busy* an agent was; weighting them tells you *how much risk* it took.

## A uniform cap counts a wire transfer like a web search

Consider a billing agent that reconciles disputes. In a healthy run it searches records, reads a few invoices, and occasionally issues a refund. A naive `max_iterations=20` cap lets it take twenty steps, full stop. But the steps are not fungible:

- Twenty `search` calls: cheap, reversible, low-stakes. Twenty is fine — arguably too conservative.
- Twenty `issue_refund` calls: twenty irreversible money movements. That is a page-your-CFO incident, and the exact same integer allowed it.

A single counter cannot distinguish these because it has no notion of what each call *does*. To bound behavior meaningfully you need three things a bare iteration cap does not give you: a per-tool weight so risky calls draw down the budget faster, a distinct ceiling on irreversible actions, and a way to make the remaining budget legible to the agent so it economizes on its own. Promptise's [autonomy budget](../../runtime/governance/budget.md) supplies all three as configuration, not as a supervising loop you have to write yourself.

## What other frameworks do today

To be fair and precise, every serious framework ships a per-run limiter, and each is real and useful within its scope:

- **LangChain** — `AgentExecutor` takes `max_iterations` (default 15) and `max_execution_time`. When the agent exceeds the step or time budget, the executor run stops (or forces a final answer via `early_stopping_method`). It is a per-run cap on the *number of steps*, enforced inside one `.invoke()`/`.ainvoke()`.
- **Pydantic AI** — `UsageLimits` bounds a run by request and token counts (`request_limit`, `total_tokens_limit`, `response_tokens_limit`, and related counters); breaching one raises `UsageLimitExceeded` mid-run. It is genuinely finer-grained than a step count — it can stop on tokens — but every request and every token is worth the same.

The delta is specific, and it is not "these frameworks can't count" — they count fine. It is that both count each unit *equally*. Neither lets you say a `stripe_charge` should draw down the run budget twenty times faster than a `search`; neither maintains a *separate* cap on irreversible actions distinct from the total-call cap; and neither surfaces the remaining budget back to the model, so the agent has no idea it is running low and cannot choose the cheaper path. You can approximate the first two by hand — wrap each tool, keep your own weighted tally, raise when it crosses a threshold — but then risk-weighting lives in glue code you maintain, not in the policy. Promptise's edge is making risk-weighted, agent-visible budgeting a *first-class* property of a run: you declare the weights once and the runtime does the accounting, the enforcement, and the context injection.

One honest boundary, stated up front: Promptise's budget is measured in **abstract action-cost units you define, not real LLM dollars** — more on that below.

## Weight the action, not the call: cost_weight budgeting

Instead of `max_iterations`, you set a per-run *cost* ceiling and annotate each tool with a `ToolCostAnnotation`. The weight is relative to a baseline read operation (`cost_weight=1.0`); anything without an annotation defaults to `1.0`.

```python
from promptise.runtime import BudgetConfig, ToolCostAnnotation

budget = BudgetConfig(
    enabled=True,
    max_tool_calls_per_run=40,     # a generous ceiling on raw call count...
    max_cost_per_run=20.0,         # ...but only 20 risk-weighted units per run
    max_llm_turns_per_run=12,      # and a hard cap on reasoning turns
    max_irreversible_per_run=2,    # at most two destructive actions per run
    tool_costs={
        "search":        ToolCostAnnotation(cost_weight=0.5),                   # baseline read, cheap
        "get_invoice":   ToolCostAnnotation(cost_weight=1.0),
        "send_email":    ToolCostAnnotation(cost_weight=3.0, irreversible=True),
        "stripe_charge": ToolCostAnnotation(cost_weight=10.0, irreversible=True),
    },
    on_exceeded="stop",            # RUNNING -> STOPPING -> STOPPED on breach
    inject_remaining=True,         # the agent sees its remaining envelope
)
```

Read this as a single sentence of policy: *the agent may take up to 40 calls or 20 weighted units per run, whichever comes first, and no more than two of those calls may be irreversible.* Under this envelope the agent can run forty `search` calls (40 × 0.5 = 20.0 units — right at the ceiling), or two `stripe_charge` calls (2 × 10.0 = 20.0), or any mix in between. The count cap and the cost cap are independent trip wires, and `max_irreversible_per_run` is a third, orthogonal one: even with cost to spare, a third refund in a run is refused. That is the distinction a lone `max_iterations` cannot express.

## Runnable: watch a per-run budget deplete unevenly

Here is a complete, runnable script — no API key, no network — that exercises the budget accounting directly so you can watch cheap and expensive calls draw the envelope down at different rates. Every symbol is a real, exported runtime API.

```python
import asyncio
from promptise.runtime import BudgetConfig, BudgetState, ToolCostAnnotation


async def main() -> None:
    budget = BudgetConfig(
        enabled=True,
        max_tool_calls_per_run=40,     # plenty of calls...
        max_cost_per_run=20.0,         # ...but only 20 risk-weighted units
        tool_costs={
            "search":        ToolCostAnnotation(cost_weight=0.5),
            "get_invoice":   ToolCostAnnotation(cost_weight=1.0),
            "send_email":    ToolCostAnnotation(cost_weight=3.0, irreversible=True),
            "stripe_charge": ToolCostAnnotation(cost_weight=10.0, irreversible=True),
        },
    )
    state = BudgetState(budget)

    # A read-heavy plan: 12 cheap lookups spend only 6.0 of 20 units.
    for _ in range(12):
        await state.record_tool_call("search")
    print(state.budget_context())

    # One wire transfer costs as much as 20 searches.
    v = await state.record_tool_call("stripe_charge")
    print("after charge:", state.budget_context(), "| violation:", v)

    # A second charge blows the per-run envelope -> BudgetViolation.
    v = await state.record_tool_call("stripe_charge")
    print("violation:", v.limit_name, f"{v.current_value}/{v.limit_value}", "tool:", v.tool_name)


asyncio.run(main())
```

Running it prints:

```text
[Budget] Tool Calls Run: 28 | Cost Run: 14.0
after charge: [Budget] Tool Calls Run: 27 | Cost Run: 4.0 | violation: None
violation: max_cost_per_run 26.0/20.0 tool: stripe_charge
```

The story is in those three lines. Twelve searches consumed only 6.0 of the 20-unit envelope — call count barely moved and the agent has 14.0 units of headroom. A *single* `stripe_charge` then consumed 10.0 units, dropping headroom to 4.0 while the raw call counter ticked up by just one. The second charge would push the run to 26.0 units, so `record_tool_call` returns a `BudgetViolation` naming `max_cost_per_run` and the tool that tripped it. A per-run tool call limit expressed in *risk-weighted units* catches the exact runaway — a couple of expensive actions — that a count cap of 40 would have waved straight through.

## Show the agent its envelope with inject_remaining

Accounting is half the value; the other half is telling the agent. With `inject_remaining=True`, Promptise prepends that same `[Budget]` line to the system prompt before every invocation, so the model plans against real numbers rather than guessing. In practice this nudges the agent to front-load cheap reads and defer or batch expensive actions when it is running low — it spends its envelope on cheap tools first, exactly the behavior you want. In [open mode](../../runtime/index.md) the agent can also call the `check_budget` meta-tool to inspect its limits programmatically mid-run.

In production you don't drive `BudgetState` by hand — you attach the `BudgetConfig` to a supervised process and let the runtime do the recording, enforcement, and injection around every trigger-driven invocation:

```python
import asyncio
from promptise.runtime import (
    AgentProcess,
    ProcessConfig,
    BudgetConfig,
    ToolCostAnnotation,
    ProcessState,
)


async def main() -> None:
    config = ProcessConfig(
        model="openai:gpt-5-mini",
        instructions="Reconcile open payment disputes until the queue is clear.",
        budget=BudgetConfig(
            enabled=True,
            max_tool_calls_per_run=40,
            max_cost_per_run=20.0,
            max_irreversible_per_run=2,
            tool_costs={
                "search":        ToolCostAnnotation(cost_weight=0.5),
                "issue_refund":  ToolCostAnnotation(cost_weight=10.0, irreversible=True),
                "send_email":    ToolCostAnnotation(cost_weight=3.0, irreversible=True),
            },
            on_exceeded="stop",     # hard-stop the process on a per-run breach
            inject_remaining=True,  # every invocation sees its remaining envelope
        ),
    )

    process = AgentProcess(name="dispute-agent", config=config)
    await process.start()
    assert process.state is ProcessState.RUNNING
    # Triggers now fire invocations; the budget is recorded, enforced, and
    # injected on every one — no supervising loop of your own.
    await process.stop()


asyncio.run(main())
```

The honesty this design demands: **`max_cost_per_run=20.0` is twenty units *you* defined, not twenty dollars.** Promptise never queries a provider's pricing API, so the budget governs *what the agent does* — which tools, how many, how risky — not what your token bill is. That is a deliberate boundary, not a gap: an agent making a hundred cheap LLM turns can burn real money the cost budget never sees. For actual dollars, cap `max_llm_turns_per_run`, and track token spend through your provider dashboard or Promptise observability's per-invocation token counts, which export to Prometheus. Use the two together: the budget for behavioral limits, observability for the monetary ones. And note the budget funds one runaway it cannot catch on its own — an agent looping cheaply forever, each call comfortably inside the envelope. That is [behavioral health's](../../runtime/governance/health.md) job; the specific case is walked through in [Catch an AI Agent Stuck Repeating the Same Tool Call](ai-agent-stuck-repeating-tool-call.md).

## Frequently asked questions

### How is `cost_weight` budgeting different from `max_iterations`?

`max_iterations` counts steps and treats every step as equal, so it cannot tell twenty searches apart from twenty charges. `cost_weight` prices each tool relative to a baseline read (`1.0`): a `search` at `0.5` and a `stripe_charge` at `10.0` draw the same `max_cost_per_run` envelope down at very different rates. You get a count cap *and* a risk-weighted cost cap as independent limits, plus a separate `max_irreversible_per_run` ceiling.

### Are the budget's cost units real dollars?

No. Cost is abstract weight units you assign via `ToolCostAnnotation`, plus counts of tool calls and LLM turns. Promptise never connects to any provider's pricing API and does not track monetary spend. The budget governs agent *behavior*; for real dollar limits, cap `max_llm_turns_per_run` and monitor token spend through your provider dashboard or Promptise observability.

### What happens when a per-run limit is exceeded?

`record_tool_call` returns a `BudgetViolation` naming the limit hit, and the runtime executes your `on_exceeded` action: `"pause"` suspends the process, `"stop"` ends it and requires a manual restart, and `"escalate"` fires a webhook plus an EventBus event before suspending. Per-run counters reset automatically at the start of the next invocation.

### Does the agent actually see its remaining budget?

Yes, when `inject_remaining=True`. Promptise formats the remaining envelope as a `[Budget]` line and injects it into the system prompt before each invocation, so the agent can prioritize cheap tools and defer expensive ones. In open mode the agent can also call `check_budget` to read its limits programmatically.

### Can I stop an agent that keeps running across many separate invocations?

The per-run caps reset each invocation by design. To bound activity across a trigger-driven process — many cron ticks or webhooks — use the daily limits (`max_tool_calls_per_day`, `max_cost_per_day`) or the process-level kill switches in [How to Stop a Runaway AI Agent](stop-a-runaway-ai-agent.md), which act on the process itself rather than one call.

## Next steps

Replace your `max_iterations` cap with a `BudgetConfig`: set `max_tool_calls_per_run` and `max_cost_per_run`, annotate every irreversible tool with a `cost_weight` and `irreversible=True`, add a `max_irreversible_per_run` ceiling, and flip `inject_remaining=True` so the agent respects the envelope it can see. Start from the runnable script above to feel how the weights behave, then read the [Autonomy Budget reference](../../runtime/governance/budget.md) for the full weighting guidance and the [Agent Runtime overview](../../runtime/index.md) for how budgets attach to supervised, trigger-driven processes.
