---
title: "Time-Travel Debug an AI Agent by Rewinding State"
description: "When an autonomous agent does something baffling, 'read the stdout' isn't a debugging strategy. This shows how the journal + ReplayEngine let you rewind a…"
keywords: "time-travel debug ai agent, rewind ai agent state, replay agent decisions, agent postmortem debugging, reconstruct why agent did x, deterministic agent replay"
date: 2026-07-16
slug: time-travel-debug-ai-agent
categories:
  - Runtime
---

# Time-Travel Debug an AI Agent by Rewinding State

To **time-travel debug an AI agent**, you rewind its recorded history to the exact step before it did something baffling, rebuild the state it was actually looking at, and replay its decisions deterministically — instead of squinting at stdout and guessing. When a request/response function misbehaves you set a breakpoint and re-run it. An autonomous agent gives you neither luxury: it woke itself on a trigger you didn't watch, called an LLM whose output you can't reproduce, mutated its own state across dozens of invocations, and fired a side effect you now have to explain. This post shows how a Promptise journal plus its rewind and replay engines turn "why did it do that?" into a reproducible postmortem, and where that differs — honestly — from LangGraph's graph-state time-travel.

<!-- more -->

## Why "read the stdout" fails on an autonomous agent

Logs answer *what* the process printed. A postmortem needs *why it decided* — and those are different data structures. A log line like `applied hold on acme-42` tells you the outcome but not the state the agent held at the instant it chose the hold: which context keys were set, what the conversation looked like, which trigger had just fired. By the time you're reading logs, that state is gone. The process has moved on, mutated its counters, and overwritten the exact values that would explain the decision.

Three properties of a [long-running agent](durable-execution-for-ai-agents.md) make plain logging a dead end for debugging:

- **The inputs aren't in the request.** The agent wasn't called by a user with a payload you can re-send. It was woken by a cron tick or a webhook, and its "input" was its own accumulated state.
- **The decision isn't deterministic.** Re-running the same prompt against the LLM won't necessarily reproduce the choice, so "just run it again" doesn't reconstruct the incident.
- **The state that explains it is transient.** A running count, an `account_status` flag, a mission cursor — the values that drove the decision existed only in memory and were already overwritten.

So a real postmortem needs a durable record of *every decision and the state around it*, plus a way to rebuild any past moment on demand. That is what a journal is for.

## The journal is a flight recorder for every decision

A Promptise [journal](../../runtime/journal/index.md) is an append-only ledger that sits between the agent runtime and disk. Every meaningful event a process emits becomes a `JournalEntry`: lifecycle `state_transition`s, `trigger_event`s (which trigger woke it, with the payload), `invocation_start` / `invocation_result` pairs, `context_update`s when a single state key changes, `tool_call`s, and periodic full-state `checkpoint`s. Two backends implement the same `JournalProvider` protocol — `InMemoryJournal` for tests and `FileJournal` (append-only JSONL) for production — so you develop against memory and ship against disk with no code change.

That ledger is a flight recorder, not a log file, and the difference is decisive for debugging. A log is a stream of strings you `grep`. A journal is a *structured, ordered, replayable* record: because each entry carries its type and payload, you can reconstruct the process's state at any point by replaying the entries up to it. Nothing is ever edited or deleted — entries only get appended — so the record you debug against is the record that actually happened.

Two engines read that ledger, in opposite directions:

- The [`ReplayEngine`](../../runtime/journal/replay.md) rebuilds *current* state by replaying **forward** from the last checkpoint. That's the crash-recovery path — it answers "where did the process end up?"
- The [`RewindEngine`](../../runtime/journal/rewind.md) goes **backward**: pick any entry in history and rebuild the state as it was *just before* that entry. That's the postmortem path — it answers "what did the agent see the moment before it acted?"

Time-travel debugging is those two directions used together: replay forward to the broken end state, rewind backward to the clean state one step before the bad decision, then diff them.

## Rewind to the moment before the bad decision

Here is a complete, runnable postmortem. A billing-ops agent applied an irreversible account hold on a live customer, and nobody can say why from the logs. The script below records the process's decision history, replays it forward to the broken state, finds the baffling action, and rewinds to the exact moment before it — reconstructing the state and conversation the agent actually had. It uses only the real journal API and runs with no API key and no LLM call.

```python
import asyncio

from promptise.runtime.journal import (
    InMemoryJournal,
    JournalEntry,
    ReplayEngine,
    RewindEngine,
    RewindMode,
)


async def main() -> None:
    journal = InMemoryJournal()
    pid = "billing-ops"

    # A live process writes these entries as it runs. We append them by
    # hand so the whole decision history is visible in one place.
    events = [
        ("e01", "state_transition", {"from_state": "created", "to_state": "running"}),
        ("e02", "trigger_event", {"trigger_type": "cron", "at": "2026-07-16T02:00:00"}),
        ("e03", "context_update", {"key": "account_status", "value": "active"}),
        ("e04", "user_prompt", {"prompt": "Reconcile account acme-42 for the July cycle."}),
        ("e05", "context_update", {"key": "invoice_state", "value": "reconciling"}),
        # A dunning webhook fires MID-reconciliation and flips the flag.
        ("e06", "trigger_event", {"trigger_type": "webhook", "event": "payment_failed"}),
        ("e07", "context_update", {"key": "account_status", "value": "delinquent"}),
        ("e08", "assistant_message", {"content": "acme-42 is delinquent; applying an account hold."}),
        # The baffling action — an irreversible hold on a live customer.
        ("e09", "tool_call", {"tool": "apply_account_hold", "args": {"account": "acme-42"}}),
        ("e10", "context_update", {"key": "action", "value": "account_hold_applied"}),
    ]
    for entry_id, entry_type, data in events:
        await journal.append(JournalEntry(
            entry_id=entry_id, process_id=pid, entry_type=entry_type, data=data,
        ))

    # 1. What state did the process end up in? Replay the journal forward.
    recovered = await ReplayEngine(journal).recover(pid)
    print("current (broken) state:", recovered["context_state"])

    # 2. Find the baffling action in the decision history.
    history = await journal.read(pid)
    bad = next(e for e in history if e.entry_type == "tool_call")
    print("baffling action:", bad.data["tool"], "->", bad.entry_id)

    # 3. Preview a rewind to the moment *just before* that action (dry-run).
    rewind = RewindEngine(journal)
    plan = await rewind.plan(process_id=pid, target_entry_id=bad.entry_id)
    print("plan:", plan.summary)

    # 4. Reconstruct the exact state the agent saw before it acted.
    #    record=False keeps the postmortem read-only — nothing is written.
    before = await rewind.apply(
        process_id=pid,
        target_entry_id=bad.entry_id,
        mode=RewindMode.BOTH,
        record=False,
    )
    print("state before the decision:", before.context_state)
    print("what the agent had said:", [m["content"] for m in before.conversation])


asyncio.run(main())
```

Running it prints:

```text
current (broken) state: {'account_status': 'delinquent', 'invoice_state': 'reconciling', 'action': 'account_hold_applied'}
baffling action: apply_account_hold -> e09
plan: preview only: 2 entries affected (0 conversation, 2 code/state).
state before the decision: {'account_status': 'delinquent', 'invoice_state': 'reconciling'}
what the agent had said: ['Reconcile account acme-42 for the July cycle.', 'acme-42 is delinquent; applying an account hold.']
```

Read the reconstruction, not the log. At the moment before the hold, `account_status` was already `delinquent` **while** `invoice_state` was still `reconciling` — two flags that should never be true at once. Walk backward through the history and the cause is right there: a `payment_failed` webhook (`e06`) fired *in the middle of* an invoice reconciliation the cron had started, and its handler flipped `account_status` to `delinquent` (`e07`) under the running reconcile. The agent didn't hallucinate; it acted correctly on a state that a racing trigger had corrupted. Stdout would have shown you `account_hold_applied` and nothing else. The journal shows you the race.

Three things make this a real postmortem rather than a lucky guess. `plan()` is a dry-run that reports the blast radius before you touch anything — here, two code/state entries after the target. `apply(..., record=False)` reconstructs the pre-decision state **without mutating the journal**, so inspecting an incident leaves no trace; the [rewind engine](../../runtime/journal/rewind.md) is non-destructive by design and, when you *do* want the rewind recorded, writes a new `rewind` entry noting who did it and why rather than editing history. And it is *deterministic*: given the same journal you rebuild the same state every time, which is exactly what makes a bug reproducible enough to fix.

## What other frameworks do today

Precision matters here, because the closest comparison genuinely has time-travel and it would be dishonest to pretend otherwise.

**LangGraph has real graph-state time-travel.** With a checkpointer attached, LangGraph saves a snapshot of the graph's channel state at every super-step, keyed by `thread_id`. You can walk that history with `get_state_history()`, resume from any past `checkpoint_id`, and even branch by editing channel values at a checkpoint with `update_state()`. That is authentic time-travel, and it's the backbone of LangGraph's human-in-the-loop story — pause a graph, rewind a step, tweak the state, and re-run. If your unit of work is a single graph invocation, that is a strong, mature tool and you should use it.

The delta is *what gets rewound*. A LangGraph checkpoint is the state of one graph run — the channel values of a thread. It is not a replayable journal of every lifecycle transition, every trigger firing, and every invocation of a long-lived, self-triggering process. In our example the root cause was a `webhook` trigger landing between two `cron`-driven steps; that inter-invocation event isn't part of a graph run's channel snapshot, so a thread's state history can't point at it. You can rewind *the state of a run*, but not to "the exact step before a bad decision" across a process's whole event history, because the triggers, the invocation boundaries, and the lifecycle transitions that frame the decision live outside the graph checkpoint.

So Promptise's edge isn't "LangGraph can't time-travel" — it can. It's that Promptise journals the **whole process history**, which makes step-accurate postmortem replay a structural property rather than something scoped to one run. A rewind lands on any entry — a trigger, a transition, a tool call — not just a super-step boundary, and the reconstruction includes the triggers that woke the agent. For the mechanical side-by-side of per-thread checkpoints versus a process decision journal, see [LangGraph Checkpointing vs Journal-Replay Explained](langgraph-checkpointing-vs-journaling.md). (Durable-workflow engines like Temporal and DBOS also replay an event history, but to reconstruct deterministic *workflow* state, not to give you a rewind-and-inspect debugger over an agent's decisions — that landscape is covered in the [durable-execution hub](durable-execution-for-ai-agents.md).)

## Fix forward without losing the good work

A postmortem usually ends with a decision: undo the bad step and let the agent continue. Rewinding an agent isn't as blunt as rewinding a function, because after the bad decision the process may have accumulated genuinely useful work you don't want to throw away. The [`RewindEngine`](../../runtime/journal/rewind.md) exposes five modes so the rollback can be as surgical as the incident demands:

| Mode | What it does |
|---|---|
| `BOTH` | Full rollback — rewind conversation *and* context state to the target point. |
| `CONVERSATION_ONLY` | Restore the conversation buffer but keep tool results and state changes. Use when the agent went off-rails in chat but the accumulated work is sound. |
| `CODE_ONLY` | Restore context state and tool results but keep the conversation — "undo" a tool call without losing the reasoning that led there. |
| `SUMMARIZE` | Change nothing; inject a system note summarizing the skipped interval so the agent remembers it tried something without re-running it. |
| `CANCEL` | Dry-run — return the plan and touch nothing. Always safe. |

For our billing incident you'd rewind `account_status` with `CODE_ONLY`, keeping the reasoning trail that documents the race while undoing the corrupted flag — then re-run the reconcile with the webhook handler fixed. Because the rewind itself is appended as a new `rewind` entry (unless you pass `record=False` for a pure read-only inspection), the audit trail shows the rollback happened, who triggered it, and which mode was used. You never lose the evidence of the incident by fixing it.

Two honest limits keep the guarantee trustworthy. Reconstruction rebuilds the key-value context, lifecycle state, and conversation the journal captured — it does not re-execute side effects, so a replayed `context_update` never re-charges a card or re-sends an email, and it does not restore memory providers or the conversation buffer unless you put them in the checkpoint payload. And time-travel is only as complete as the journal level: `"checkpoint"` gives you a durable recovery baseline, while `"full"` records every side effect and is what you want when you know you'll be debugging a specific class of decision.

## Frequently asked questions

### How do I reconstruct why an agent did something after the fact?

Read the journal for the process, find the entry for the baffling action (`await journal.read(process_id)` then locate the `tool_call` or `context_update`), and call `RewindEngine.apply(..., mode=RewindMode.BOTH, record=False)` targeting that entry. It rebuilds the context state, lifecycle state, and conversation as they were *just before* the action — the exact inputs the agent decided on — without mutating the journal. Diff that against the current state from `ReplayEngine.recover()` and the offending change is isolated.

### Is rewinding the agent destructive — will I lose data?

No. The journal is append-only; `RewindEngine` never edits or deletes entries. `plan()` is a pure dry-run, and `apply(..., record=False)` reconstructs state for inspection without writing anything. When you *do* apply a rollback to fix forward, it appends a new `rewind` entry recording the target, the mode, and the actor, so the rollback is itself auditable. The original history always survives.

### Is the replay deterministic enough to reproduce a bug?

Yes, for the state the journal records. Both engines are pure functions of the entry log: given the same journal, `ReplayEngine.recover()` always rebuilds the same current state and `RewindEngine.apply()` always rebuilds the same past state. What is *not* reproduced is a fresh LLM call — replay reconstructs the recorded decision, it does not re-ask the model. That is the point: you debug the decision that actually happened, not a new roll of the dice.

### How is this different from LangGraph's `get_state_history()` and `update_state()`?

LangGraph's time-travel is real and rewinds the channel state of one graph run keyed by `thread_id`. Promptise rewinds a *process* — its whole append-only journal of transitions, triggers, invocations, and tool calls — so you can land on the exact step before a bad decision, including the inter-invocation trigger that caused it, which isn't part of a graph-run snapshot. See [LangGraph Checkpointing vs Journal-Replay Explained](langgraph-checkpointing-vs-journaling.md) for the full comparison.

## Next steps

Reproduce a bad decision exactly: rewind the journal to the invocation before it and replay step by step. Start from the [journal system](../../runtime/journal/index.md) reference to attach a `FileJournal` to a long-running process, use the [rewind engine](../../runtime/journal/rewind.md) to reconstruct any past state for a postmortem, and read the [replay engine](../../runtime/journal/replay.md) page to see how forward recovery rebuilds current state — the other half of time-travel debugging.
