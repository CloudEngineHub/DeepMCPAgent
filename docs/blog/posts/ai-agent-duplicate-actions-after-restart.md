---
title: "Why AI Agents Double-Fire Actions After a Restart"
description: "Restart a naive agent mid-task and it re-runs from zero — re-charging the card, re-sending the email. This is the exactly-once/idempotency angle specifically…"
keywords: "ai agent duplicate actions after restart, exactly-once ai agent, agent re-runs side effects, idempotent agent tools, prevent duplicate agent actions, agent double charge after crash"
date: 2026-07-16
slug: ai-agent-duplicate-actions-after-restart
categories:
  - Runtime
---

# Why AI Agents Double-Fire Actions After a Restart

If you have ever debugged **ai agent duplicate actions after restart**, you know the failure is never the crash itself — it is what the agent does when it comes back. A stateless retry loop restarts the process from zero, and the agent cheerfully re-runs work it already finished: re-charging a card it already charged, re-sending an email it already sent. The crash was survivable. The re-execution is the incident. This post is specifically about the exactly-once / idempotency failure mode — not general crash recovery — and how Promptise Foundry attacks it with two independent guards: a replay engine that reconstructs state from *recorded results* instead of re-running completed work, and an irreversible-action budget that caps destructive calls even if the first guard has a bug.

## The double-fire failure mode, dissected

Picture a billing agent processing a support ticket. It calls `stripe_charge`, the charge succeeds, and then — before the agent writes "done" anywhere durable — the container gets OOM-killed or the node is rescheduled. A naive supervisor sees a dead process and restarts it. The agent has no memory of the charge, so it charges again. That is an **agent double charge after crash**, and it is the canonical example of why "just restart it" is not a durability strategy for autonomous agents.

The root cause is that the agent's real work lives in two places that a restart handles very differently:

- **State it accumulated** — a cursor, a running count, `charge_status = "charged"`. Lose this and the agent forgets what it did.
- **Side effects it already issued** — the actual HTTP call to Stripe. Repeat this and you have a duplicate.

A restart-from-zero loses the first and therefore repeats the second. The fix is not "crash less" — you cannot — it is to make the agent reconstruct what it already did *without re-doing it*. That is exactly what an [append-only journal](../../runtime/journal/index.md) plus a replay engine buys you, and it is a different guarantee from the general "resume my process" story in [AI Agent Crash Recovery with Journals & Replay](ai-agent-crash-recovery.md). Here we care about one property only: that the agent re-runs side effects **exactly zero** additional times.

## Exactly-once for completed work: replay reconstructs, it does not re-run

The core idea behind an **exactly-once ai agent** is deceptively simple: record the *result* of every completed action as a durable fact, and on restart rebuild your state from those facts rather than re-issuing the actions that produced them. Promptise's `ReplayEngine` does precisely this. It loads the last checkpoint, collects every journal entry written after it, and applies them in order to rebuild the process's context and lifecycle state — and it never re-executes the underlying tool calls. A recorded `invocation_result` is replayed as a *fact that this happened*, not as an instruction to happen again.

The script below proves the property end to end. It charges a customer, records the result, checkpoints, simulates a crash, then recovers — and shows the resume path skipping the charge because the journal already knows it succeeded.

```python
import asyncio

from promptise.runtime.journal import FileJournal, JournalEntry, ReplayEngine


async def charge_customer(journal: FileJournal, process_id: str) -> None:
    """Call the payment API once, then RECORD the result as a durable fact."""
    # ... the real Stripe call happens here, exactly one time ...
    await journal.append(JournalEntry(
        process_id=process_id,
        entry_type="invocation_result",
        data={"tool": "stripe_charge", "status": "succeeded", "charge_id": "ch_123"},
    ))
    await journal.append(JournalEntry(
        process_id=process_id,
        entry_type="context_update",
        data={"key": "charge_status", "value": "charged"},
    ))


async def main() -> None:
    journal = FileJournal(base_path=".promptise/journal")
    process_id = "billing-agent"

    # Run 1: start, checkpoint the pre-charge state, THEN charge and record it.
    await journal.append(JournalEntry(
        process_id=process_id,
        entry_type="state_transition",
        data={"from_state": "created", "to_state": "running"},
    ))
    await journal.checkpoint(process_id, {
        "context_state": {},
        "lifecycle_state": "running",
    })
    await charge_customer(journal, process_id)
    # --- CRASH: the container is killed before the task finishes ---

    # Restart: rebuild state from the journal. No tool is re-executed.
    engine = ReplayEngine(journal)
    recovered = await engine.recover(process_id)

    if recovered["context_state"].get("charge_status") == "charged":
        print("Already charged — resuming the task, not the charge.")   # exactly once
    else:
        await charge_customer(journal, process_id)   # runs only if never charged

    print(recovered["context_state"])     # {'charge_status': 'charged'}
    print(recovered["entries_replayed"])  # 2  (invocation_result + context_update)
    print(recovered["last_entry_type"])   # context_update

    await journal.close()


asyncio.run(main())
```

Two things make this work. First, the charge and its result are recorded *after* the checkpoint, so `recover()` has to replay them to rebuild the truth — `entries_replayed` reads `2`. Second, replaying the `invocation_result` entry updates the record of *what happened* (it is tracked in `last_entry_type`) but issues no HTTP call; only the `context_update` mutates state, flipping `charge_status` to `charged`. The resume path reads that flag and declines to charge again. That is what "**agent re-runs side effects** — never" looks like in practice. The full contract of the recovery dict and how to feed it back into a fresh `AgentContext` lives on the [Replay Engine](../../runtime/journal/replay.md) reference. Under `AgentRuntime`, journaling and this recovery run automatically before the process accepts new triggers, so you get the behavior without hand-writing any of it.

## The second guard: cap irreversible actions per run and per day

Replay gives you exactly-once for *completed* work. But the honest failure surface is bigger than "clean crash after a clean charge." What if a bug in your resume logic mis-reads the flag? What if the model, mid-task, decides to charge twice on its own? A single mechanism that you trust for correctness is a single point of failure. So Promptise adds a second, *independent* guard that does not care why a duplicate is about to happen: the [autonomy budget](../../runtime/governance/budget.md).

Mark the dangerous tools `irreversible=True` and set a hard ceiling on how many irreversible actions a run may take. The runtime enforces it out-of-band, around every invocation — so even a replay bug cannot turn into a double charge, because the second charge trips the cap before it reaches Stripe.

```python
from promptise.runtime import (
    ProcessConfig,
    BudgetConfig,
    EscalationTarget,
    ToolCostAnnotation,
)

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Process billing tasks for customer support tickets.",
    budget=BudgetConfig(
        enabled=True,
        # Guard against a double charge: at most one irreversible action per run.
        max_irreversible_per_run=1,
        # Bound irreversible actions per *day* via the daily ceilings.
        max_tool_calls_per_day=500,
        max_cost_per_day=100.0,
        tool_costs={
            "stripe_charge": ToolCostAnnotation(cost_weight=10.0, irreversible=True),
            "send_email": ToolCostAnnotation(cost_weight=2.0, irreversible=True),
            "search": ToolCostAnnotation(cost_weight=0.5),
        },
        on_exceeded="escalate",  # "pause", "stop", or "escalate"
        escalation=EscalationTarget(webhook_url="https://hooks.slack.com/..."),
    ),
)
```

`max_irreversible_per_run` is the direct per-run cap: destructive tools get counted separately from ordinary reads, and the run stops (or pauses, or escalates) the moment it exceeds the limit. There is no separate per-day counter for irreversible actions specifically, but you bound them across a day honestly through the daily ceilings — `max_tool_calls_per_day` and `max_cost_per_day` with the irreversible tools weighted heavily, so a runaway loop of charges exhausts the day's budget fast and escalates to a human. This is defense in depth: replay makes the common case exactly-once, and the budget makes the *worst* case bounded. To **prevent duplicate agent actions** you want both, because they fail independently.

## What other frameworks do today

"Just make it durable" hides a lot of variation, so it is worth being exact about what the landscape actually ships — and where a real capability stops short of solving the double-fire problem.

**Naive retry / supervisor loops.** A bare `while True` around an agent, or a Kubernetes restart policy, re-executes from scratch. Every completed side effect fires again. This is the baseline the whole post is about, and it is still the most common setup in the wild.

**Durable-execution engines — Temporal, Restate, DBOS.** These genuinely solve exactly-once at the workflow layer, and they are excellent at it. Temporal replays a workflow from its event history and does not re-run completed activities because their results are recorded; Restate journals each step; DBOS persists step state to Postgres. The delta is not durability — it is scope. They treat your agent as opaque code: no LLM tool discovery, no memory, no triggers, no irreversible-action governance. You get exactly-once *if* you restructure your agent as their workflow and hand-write the tool loop on top. Promptise's edge is making the same guarantee native to the agent runtime rather than a separate orchestrator you operate alongside — the tradeoffs are laid out in [Durable Execution for AI Agents in Python](durable-execution-for-ai-agents.md).

**LangGraph checkpointers.** This is the closest comparison and a real, well-engineered capability, so precision matters. LangGraph's `SqliteSaver` / `PostgresSaver` persist graph state at every super-step boundary, keyed by `thread_id`, and re-invoking a thread resumes from the last durable checkpoint — completed nodes are not re-run. That is materially better than a naive loop. The exact delta for the double-fire problem is two-fold. First, a checkpoint is written *between* nodes, so if a node issues a side effect and then crashes before it returns, resuming re-runs that node from the top and can re-fire the side effect — LangGraph's own guidance is that node side effects should be made idempotent for this reason, which leaves **idempotent agent tools** as your design problem. Second, there is no built-in cap on how many irreversible actions a run may take; that governance is not part of the checkpointer. Promptise closes the first gap by replaying *recorded results* rather than re-executing the interrupted step, and closes the second by making the irreversible-action budget a first-class, declarative part of the process. The mechanical checkpoint-vs-journal distinction is drawn in full in [LangGraph Checkpointing vs Journal-Replay Explained](langgraph-checkpointing-vs-journaling.md).

The framing that stays honest: nobody here "lacks durability." The difference is whether exactly-once-for-side-effects and a hard irreversible-action ceiling are things you *assemble* or things Promptise makes *structural*.

## Where idempotency is still your job

Two independent guards remove most of the double-fire surface, but they do not make the problem disappear, and pretending otherwise sets you up for a bad night.

- **The gap between "acted" and "recorded" is on you.** Replay reconstructs state from what the journal captured. If your tool charges the card and the process dies in the microsecond *before* the `invocation_result` is appended, the journal never learned about the charge — and on resume the agent will charge again. Close this window with an **idempotency key**: pass a stable key (the ticket ID, a UUID you generate before the call) to Stripe so a retried request is de-duplicated *at the provider*. Journaling shrinks the window; idempotency keys make the residual window safe.
- **Replay assumes entries apply cleanly in order.** It rebuilds the key-value context store, not long-term memory providers or the conversation buffer — those persist separately. Keep checkpoint data JSON-serializable, and do not smuggle non-idempotent side effects into replay expecting them to re-run.
- **The budget cap bounds damage; it does not repair it.** `max_irreversible_per_run=1` stops the *second* charge. It cannot un-charge the first if your business logic was wrong. It is a blast-radius limiter, not a correctness proof.

Say these limits out loud in your design review. Replay plus an irreversible-action cap plus provider-side idempotency keys is a genuinely robust exactly-once posture; any two of the three leaves a real hole.

## Frequently asked questions

### Why does my AI agent re-run actions after a restart?

Because a stateless restart loses the runtime state that recorded what the agent already did. The agent wakes with no memory of the completed charge or email, so it repeats it. The fix is durable state plus recovery-by-reconstruction: record each completed action's *result* to an append-only journal, and on restart use `ReplayEngine.recover(process_id)` to rebuild state from those recorded facts instead of re-executing the actions. Under `AgentRuntime` this happens automatically before the process takes new triggers.

### How do I prevent an agent from double-charging a card after a crash?

Combine three layers. First, record the charge result in the journal and gate the resume path on the recovered `charge_status` flag so replay never re-issues a completed charge. Second, set `max_irreversible_per_run=1` on the [budget](../../runtime/governance/budget.md) with `stripe_charge` marked `irreversible=True`, so a duplicate is stopped out-of-band even if the first layer misbehaves. Third, pass a stable idempotency key to the payment provider to cover the crash window between charging and recording.

### Does replaying the journal re-execute my tools?

No. `ReplayEngine` applies recorded state mutations (`context_update`) and lifecycle transitions in order to rebuild state, and it *notes* `invocation_result` entries without re-issuing the underlying tool calls. Given the same journal it always rebuilds the same state, and it never re-sends an email or re-charges a card. It is a state-reconstruction engine, not a re-execution engine.

### Isn't this the same as LangGraph checkpointing?

Partly. LangGraph checkpointers are real and durable, and resuming a thread skips already-completed nodes. The differences that matter for double-firing: a checkpoint is written at super-step boundaries, so an interrupted node re-runs from the top and can re-fire a side effect unless you made it idempotent yourself; and there is no built-in ceiling on irreversible actions. Promptise replays recorded results rather than re-executing the interrupted step and makes the irreversible-action cap a declarative part of the process. See [LangGraph Checkpointing vs Journal-Replay Explained](langgraph-checkpointing-vs-journaling.md).

## Next steps

Add a `FileJournal` and an irreversible-action budget to your process, kill it right after a charge, and watch the restart resume the task instead of re-charging your user. Start with the [journal system](../../runtime/journal/index.md) to record completed work, wire recovery with the [Replay Engine](../../runtime/journal/replay.md), and cap the blast radius with the [autonomy budget](../../runtime/governance/budget.md). Then read [Durable Execution for AI Agents in Python](durable-execution-for-ai-agents.md) for the architecture-level view of how the process, journal, and governance fit together.
