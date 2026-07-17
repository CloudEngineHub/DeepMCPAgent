---
title: "Limit an AI Agent's Irreversible Actions Per Run"
description: "The scariest autonomous failure isn't looping — it's ten wire transfers at 3am. Shows annotating delete/send/charge tools irreversible=True and capping…"
keywords: "limit irreversible actions ai agent, cap destructive agent actions, irreversible action guardrail, agent delete send charge limit, max irreversible per run, limit destructive tool calls"
date: 2026-07-16
slug: limit-irreversible-actions-ai-agent
categories:
  - Governance
---

# Limit an AI Agent's Irreversible Actions Per Run

To limit irreversible actions an AI agent takes in a single run, you need a control that counts *destructive* calls separately from everything else the agent does. This is a different problem from stopping a runaway loop, and it is the one that keeps operators awake. A looping agent that calls `get_status(id=42)` forty times is embarrassing and cheap. An agent that reasons its way into ten `issue_refund` calls, or fires the same dunning email to your entire customer list, or deletes ten accounts because a webhook payload looked plausible — that is not embarrassing, it is a Monday-morning incident review. The failure isn't volume. It's a *small number of destructive calls*, each one individually reasonable, that together are unrecoverable.

The uncomfortable part is that every per-run cap you already have makes this failure *worse*, not better, because it treats a database read and a wire transfer as the same unit of "one step."

## Why a uniform call cap can't stop the 3am wire transfers

Picture a billing agent with a sensible `max_tool_calls_per_run=25`. That cap feels safe. Now watch it fail: the agent spends three calls reading invoices, then issues twenty-two refunds. Twenty-five steps, cap respected, no exception raised — and twenty-two irreversible payouts have left your account. The cap did exactly what it promised. It counted to twenty-five. It has no idea that three of those steps were harmless reads and twenty-two were money leaving the building.

The only way to make a single uniform counter *safe* against destructive actions is to set it brutally low — say, `max_tool_calls_per_run=3`. But now you've strangled the agent's ability to *read*. It can't gather context, can't check a customer's history, can't verify a dispute before acting, because every harmless lookup burns the same scarce budget as a charge. You are forced to choose between an agent that can think and an agent that can't do damage. That trade-off is the whole problem.

The fix is to stop counting destructive actions in the same bucket as everything else. You want two independent envelopes: an unlimited-enough allowance for reads, and a hard, separately tracked ceiling on the handful of tools that move money, send messages, or delete data. "Read freely; perform at most two irreversible actions this run." That sentence is the guardrail. The rest of this post is how to declare it in a few lines.

## What other frameworks do today

To be fair and precise: every serious framework ships a per-run limiter, and each is real and useful within its scope. But look at *what unit* each one counts.

- **LangChain** — `AgentExecutor` takes `max_iterations` (default 15) and `max_execution_time`. Both bound how *many* steps or how *long* a run goes. A step that deletes a row and a step that reads one count identically.
- **LangGraph** — a `recursion_limit` (default 25) on graph execution, which is a uniform super-step count. LangGraph *does* go further than the others here: `interrupt_before=[...]` and the `interrupt()` primitive can pause the graph on a specific tool-executing node and wait for a human to resume. That is a genuine human-in-the-loop gate, and it is the closest thing in this list to destructive-action control. The exact delta: it is a *per-call approval pause*, not a *counted per-run ceiling*. It suspends execution and requires a human to approve (or you hard-configure which node halts); it does not model "let the agent perform up to N destructive actions autonomously, then block the N+1th." And it keys on which *node* you chose to interrupt, not on a reversibility property of the *tool*.
- **Pydantic AI** — `UsageLimits` (`request_limit`, `total_tokens_limit`, and friends) raise `UsageLimitExceeded` mid-run. All of these measure request or token *volume*, uniformly.
- **AutoGen** — `max_turns` and termination conditions like `MaxMessageTermination` end a conversation after a number of messages. A count of messages, not a count of destructive ones.
- **CrewAI** — an `Agent`'s `max_iter` caps reasoning iterations per task and `max_rpm` throttles the outbound request *rate*. Both are uniform quantity limits.

As far as their documented public APIs go, none of these expose *reversibility as a first-class property of a tool*, and none maintain a *separate counted budget for destructive actions* that runs independently of the total-call or token budget. You can absolutely build it yourself — wrap every risky tool, increment a counter in shared state, raise when it crosses a threshold — in any of these frameworks. The capability is reachable. Promptise's edge is not that they "can't count." It is that Promptise makes the irreversible-action ceiling a **structural, first-class property of the budget** so you don't hand-roll bookkeeping around every risky tool and hope you didn't miss one.

## Mark the tool irreversible, cap the count separately

In Promptise Foundry you do two things. First, annotate each destructive tool with `ToolCostAnnotation(irreversible=True)`. Second, set `max_irreversible_per_run` on the budget. From then on, the runtime maintains a dedicated counter — `run_irreversible` — that only advances when an irreversible tool is called, and trips its own violation the moment that counter exceeds your ceiling. Reads never touch it.

Here is the whole mechanism, runnable exactly as written. It drives the budget counters directly, so it needs **no API key** — no model is ever invoked:

```python
import asyncio
from promptise.runtime import BudgetConfig, ToolCostAnnotation, BudgetState


async def main() -> None:
    # Reads stay generous; destructive actions get a hard, SEPARATE ceiling.
    budget = BudgetConfig(
        enabled=True,
        max_tool_calls_per_run=100,     # generous — reads must never be the bottleneck
        max_irreversible_per_run=2,     # at most TWO delete/send/charge calls per run
        tool_costs={
            "search_customers": ToolCostAnnotation(cost_weight=0.5),                      # read
            "get_invoice":      ToolCostAnnotation(cost_weight=0.5),                      # read
            "send_email":       ToolCostAnnotation(cost_weight=2.0, irreversible=True),   # destructive
            "issue_refund":     ToolCostAnnotation(cost_weight=10.0, irreversible=True),  # destructive
            "delete_account":   ToolCostAnnotation(cost_weight=10.0, irreversible=True),  # destructive
        },
    )

    state = BudgetState(budget)  # pure counters — no model, no API key required

    plan = [
        "search_customers", "get_invoice", "search_customers",  # reads — stay green
        "issue_refund",   # irreversible #1  -> allowed
        "send_email",     # irreversible #2  -> allowed (ceiling reached)
        "delete_account", # irreversible #3  -> BLOCKED
    ]

    for tool in plan:
        violation = await state.record_tool_call(tool)
        if violation is not None:
            print(f"{tool:16} -> BLOCKED: {violation.limit_name} "
                  f"({violation.current_value}/{violation.limit_value})")
            break
        left = state.remaining()["irreversible_run"]
        print(f"{tool:16} -> ok   (irreversible left this run: {left})")


asyncio.run(main())
```

Run it and the read calls sail through while the third destructive call is stopped cold:

```
search_customers -> ok   (irreversible left this run: 2)
get_invoice      -> ok   (irreversible left this run: 2)
search_customers -> ok   (irreversible left this run: 2)
issue_refund     -> ok   (irreversible left this run: 1)
send_email       -> ok   (irreversible left this run: 0)
delete_account   -> BLOCKED: max_irreversible_per_run (3/2)
```

Two details make this the right guardrail. First, `irreversible left this run` stays at `2` through every read — the read budget and the destructive budget are genuinely independent counters, so lookups never erode your safety margin. Second, the violation names the exact limit that tripped (`max_irreversible_per_run`, `3/2`), not a generic "hit the cap." And note what the irreversible ceiling is *not*: it is a plain **count**, not the abstract `cost_weight` sum. Cost weights are units you define (they are explicitly not dollars — Promptise never queries a provider's pricing API); the irreversible ceiling sidesteps that entirely by counting *acts*, which is exactly what you want when the question is "how many unrecoverable things happened," not "how expensive were they." The full weighting model is in the [Autonomy Budget reference](../../runtime/governance/budget.md).

## Enforce it on a supervised process

`BudgetState` is the engine that runs *inside* the runtime; in production you rarely touch it directly. You declare the same budget on a `ProcessConfig` and let the [Agent Runtime](../../runtime/index.md) enforce it around every invocation of a supervised `AgentProcess`. The `on_exceeded` action decides what happens when the ceiling is breached:

```python
from promptise.runtime import (
    ProcessConfig,
    BudgetConfig,
    ToolCostAnnotation,
    EscalationTarget,
)

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Resolve billing disputes. Refund only when the evidence is clear.",
    budget=BudgetConfig(
        enabled=True,
        max_tool_calls_per_run=100,   # reads stay cheap and plentiful
        max_irreversible_per_run=2,   # the destructive ceiling — the whole point
        tool_costs={
            "search_customers": ToolCostAnnotation(cost_weight=0.5),
            "issue_refund":     ToolCostAnnotation(cost_weight=10.0, irreversible=True),
            "send_email":       ToolCostAnnotation(cost_weight=2.0, irreversible=True),
            "delete_account":   ToolCostAnnotation(cost_weight=10.0, irreversible=True),
        },
        on_exceeded="escalate",       # page a human, then suspend the process
        inject_remaining=True,        # the agent SEES its destructive budget shrink
        escalation=EscalationTarget(
            webhook_url="https://hooks.slack.com/services/XXX",
            event_type="agent.budget.irreversible",
        ),
    ),
)
```

Two things are worth calling out. With `inject_remaining=True`, the agent is *told* its remaining irreversible budget before every turn, so a well-behaved model self-throttles — it can see it has one destructive action left and choose the most important one. And `on_exceeded="escalate"` fires a webhook plus an EventBus event and *then* suspends the process, so a breach becomes a clean hand-off to an on-call human rather than a silent halt. (The enforcement action is budget-wide: it responds to any limit that trips, the irreversible ceiling included.) Because the action lands on the *process*, not inside one function call, it survives across trigger-driven invocations — the deeper reason a runtime beats an in-call counter is unpacked in [How to Stop a Runaway AI Agent (Runtime Kill Switches)](stop-a-runaway-ai-agent.md).

## Which tools deserve irreversible=True

The ceiling is only as good as your annotations, so be deliberate. Mark a tool `irreversible=True` when undoing its effect requires a human, a refund, an apology, or a lawyer:

- **Money movement** — `issue_refund`, `stripe_charge`, `initiate_payout`, `cancel_subscription`.
- **Outbound communication** — `send_email`, `send_sms`, `post_to_slack`, `publish_tweet`. You cannot un-send a message.
- **Destructive data operations** — `delete_account`, `drop_table`, `revoke_access`, `merge_pull_request`.
- **State the outside world reacts to** — `deploy_production`, `submit_order`, `file_ticket_with_vendor`.

Leave everything read-shaped un-annotated (it defaults to reversible, `cost_weight=1.0`): `search_*`, `get_*`, `list_*`, `preview_*`. When in doubt, mark it irreversible — a too-conservative ceiling costs you an escalation and a manual approve; a too-loose one costs you the incident. This is a `cap destructive agent actions` policy, and like any guardrail it composes: pair the irreversible ceiling with the [Behavioral Health reference](../../runtime/governance/health.md) detectors so a *stuck* agent that keeps retrying the same destructive call is caught by both the health monitor and the destructive-action budget. Health stops the agent that does the *same cheap nothing forever* (walked through in [Catch an AI Agent Stuck Repeating the Same Tool Call](ai-agent-stuck-repeating-tool-call.md)); the irreversible ceiling stops the agent that does *a few unrecoverable somethings*. Different failures, independent trip wires.

## Frequently asked questions

### How is `max_irreversible_per_run` different from `max_tool_calls_per_run`?

`max_tool_calls_per_run` counts *every* tool call — reads and writes alike — so protecting against destructive actions with it alone forces the cap so low that the agent can no longer read enough to act well. `max_irreversible_per_run` counts *only* calls to tools annotated `irreversible=True`, in a separate `run_irreversible` counter. Reads never advance it. That lets you grant a generous read budget and a tight destructive one at the same time, which is the entire point.

### Does the irreversible ceiling track dollars?

No. It is a plain **count** of irreversible tool calls, completely separate from the budget's `cost_weight` units (which are abstract weights you define, not real money — Promptise never queries an LLM provider's pricing API). Counting *acts* is deliberate: "at most two unrecoverable actions this run" is a safety statement, not a spend statement.

### What happens on the call that breaches the ceiling — is it blocked before it runs?

`record_tool_call` increments the counter and returns a `BudgetViolation` the moment the count exceeds your limit, before the runtime proceeds. On a supervised `AgentProcess`, the configured `on_exceeded` action then fires: `pause` suspends the process, `stop` ends it (manual restart required), and `escalate` sends a webhook plus an EventBus event and then suspends — a clean hand-off to a human.

### Is this the same as LangGraph's `interrupt_before`?

They solve overlapping problems differently. LangGraph's `interrupt_before` / `interrupt()` *pauses* on a chosen node and waits for a human to approve every gated call — a per-call approval gate. `max_irreversible_per_run` is a *counted autonomous ceiling*: it lets the agent perform up to N destructive actions with no human in the loop, then blocks the next one. Use an approval gate when a human must sign off on *each* destructive act; use the irreversible ceiling when the agent may act autonomously but must never exceed a per-run budget of unrecoverable actions. They compose cleanly if you want both.

### Can I set `max_irreversible_per_run=0`?

Yes — that forbids *any* irreversible action in an autonomous run, so the agent can read, plan, and draft, but every destructive tool trips the budget and escalates for human approval. It is the strictest `irreversible action guardrail`: full read autonomy, zero unattended writes.

## Next steps

Open your tool list, mark every `delete`/`send`/`charge` tool `ToolCostAnnotation(irreversible=True)`, and set `max_irreversible_per_run` to the number of unrecoverable actions you would tolerate a bug producing at 3am — for most teams that number is small. Start from the runnable snippet above to watch the ceiling trip deterministically, then move the same budget onto a `ProcessConfig` and let the [Agent Runtime](../../runtime/index.md) enforce it around every invocation. Use the [Autonomy Budget reference](../../runtime/governance/budget.md) to design the full envelope and the [Behavioral Health reference](../../runtime/governance/health.md) to add the behavioral trip wires that sit alongside it. Limit destructive tool calls before the agent ever runs unattended — it is a one-line ceiling that turns an incident review back into a routine escalation.
