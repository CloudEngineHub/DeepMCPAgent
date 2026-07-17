---
title: "AI Agent Crash Recovery with Journals & Replay"
description: "A technical deep dive into why 'restart from zero' is unacceptable for autonomous agents and how deterministic replay fixes it: FileJournal records every…"
keywords: "AI agent crash recovery, agent state persistence, agent replay engine, resume AI agent after crash, deterministic agent replay, FileJournal checkpoint"
date: 2026-07-16
slug: ai-agent-crash-recovery
categories:
  - Runtime
---

# AI Agent Crash Recovery with Journals & Replay

AI agent crash recovery is the difference between a long-running agent that shrugs off an OOM kill and one that silently loses hours of accumulated state. A stateless LLM call is easy to retry; an autonomous process that has run 40 invocations, mutated its context, and fired triggers along the way is not. If that process dies and restarts from zero, it re-does completed work, double-fires side effects, and forgets what it learned. This post shows how Promptise Foundry solves that with an append-only journal plus a replay engine — and, just as importantly, where replay honestly cannot help.

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## Why "restart from zero" breaks autonomous agents

A short-lived agent that answers one question and exits has nothing to recover. The moment you move to a **long-running** agent — one wrapped in an [agent process](../../runtime/processes.md) with triggers, a heartbeat, and its own context state — durability becomes a hard requirement. These processes accumulate state across hundreds of invocations, and they crash for the usual boring reasons: a node gets rescheduled, the kernel reaps the container, a dependency throws, the box reboots.

"Restart from zero" fails these agents in three concrete ways:

- **Duplicated work.** An invoice-watcher that had processed 40 records starts again at zero and re-processes all 40.
- **Lost decisions.** Context keys the agent set for itself — `pipeline_status = "degraded"`, a cursor, a running count — vanish.
- **Broken governance.** Budgets, mission progress, and health counters reset, so guardrails that should be tightening instead loosen.

You cannot prevent crashes. You can make them cheap to recover from. That is the entire job of AI agent crash recovery: turn a fatal event into a resumable one.

## How agent state persistence works: journals + checkpoints

Promptise Foundry's answer to agent state persistence is the [journal system](../../runtime/journal/index.md) — a durable, append-only record that sits between the runtime and disk. Every meaningful thing a process does is written as a `JournalEntry`: state transitions, trigger firings, invocation results, context updates, and periodic full-state checkpoints.

The design is textbook **event sourcing**. Two ingredients do the work:

- **The event log** — an ordered stream of entries describing what happened, in the order it happened.
- **Checkpoints** — a full state snapshot taken after each trigger → invoke → result cycle, so recovery does not have to replay from the beginning of time.

A journal level controls how much detail you keep:

| Level | What it records | When to use |
|---|---|---|
| `"none"` | Nothing | Fire-and-forget processes where history is disposable |
| `"checkpoint"` | State snapshots per cycle | **Default.** Best balance of recovery fidelity and disk cost |
| `"full"` | Every side effect: tool calls, LLM responses, each context mutation | Debugging, audit, compliance |

Two backends implement the same `JournalProvider` protocol: `InMemoryJournal` for tests, and `FileJournal` — append-only JSONL on disk — for production. Because they share an interface, you develop against memory and ship against files with no code change.

## Deterministic agent replay in practice

Recovery is handled by the `ReplayEngine`, the agent replay engine that turns a journal back into live state. It does exactly three things: load the last checkpoint, collect every entry written after it, and apply those entries in order to rebuild the process's context and lifecycle state.

Here is a complete, runnable script. It records some work, takes a checkpoint, writes one more update, simulates a crash, and then recovers — proving the process comes back exactly where it left off.

```python
import asyncio

from promptise.runtime.journal import FileJournal, JournalEntry, ReplayEngine


async def main() -> None:
    journal = FileJournal(base_path=".promptise/journal")

    # --- Normal operation: record work as it happens ---
    await journal.append(JournalEntry(
        process_id="invoice-watcher",
        entry_type="state_transition",
        data={"from_state": "created", "to_state": "running"},
    ))

    # Snapshot the last-known-good state after a completed cycle.
    await journal.checkpoint("invoice-watcher", {
        "context_state": {"invoices_processed": 40},
        "lifecycle_state": "running",
    })

    # More work happens *after* the checkpoint...
    await journal.append(JournalEntry(
        process_id="invoice-watcher",
        entry_type="context_update",
        data={"key": "invoices_processed", "value": 41},
    ))
    # --- CRASH: the process is killed right here ---

    # --- Restart: rebuild state straight from the journal ---
    engine = ReplayEngine(journal)
    recovered = await engine.recover("invoice-watcher")

    print(recovered["lifecycle_state"])    # running
    print(recovered["context_state"])      # {'invoices_processed': 41}
    print(recovered["entries_replayed"])   # 1

    await journal.close()


asyncio.run(main())
```

`recover()` returns a dict with `context_state`, `lifecycle_state`, `last_entry_type`, and `entries_replayed`. Notice the count read `40` at checkpoint time but recovers as `41` — the post-checkpoint `context_update` was replayed on top of the snapshot. That is what "deterministic agent replay" means: given the same journal, you always rebuild the same state. To finish the resume, you feed the recovered values back into a fresh `AgentContext(initial_state=...)` and `ProcessLifecycle`, both documented on the [Replay Engine](../../runtime/journal/replay.md) page.

## Wiring a FileJournal checkpoint into a live process

You rarely call `append()` and `checkpoint()` by hand — the runtime does it for you once journaling is configured. In a process manifest or runtime config you declare a `JournalConfig`, and the [agent runtime](../../runtime/index.md) writes a FileJournal checkpoint after every trigger-invoke-result cycle automatically:

```python
from promptise.runtime.config import JournalConfig

# Production: checkpoint level, durable file backend.
JournalConfig(level="checkpoint", backend="file", path=".promptise/journal")

# Debugging: capture every side effect for fine-grained replay.
JournalConfig(level="full", backend="file")
```

With that in place, an agent process running under `AgentRuntime` is crash-resilient by default. On restart, the runtime runs the replay engine before accepting new triggers, so the process picks up its counters, cursors, and lifecycle state instead of starting fresh. You can inspect the whole trail from the CLI with `promptise runtime logs invoice-watcher --lines 50`, which reads directly from the journal. For the bigger picture of what a governed, triggered process looks like end to end, see [What Is an Autonomous AI Agent Runtime?](autonomous-ai-agent-runtime.md).

## The honest limits of replay

Deterministic replay is powerful, but it is not magic, and pretending otherwise sets you up for a bad incident. Three limits matter:

- **Side effects are not re-executed.** The journal records *state changes*, not the external actions that caused them. If invocation 41 charged a card or sent an email, replay restores the resulting state — it does not, and must not, re-charge the card. That is a feature: replay is for reconstructing memory, not for re-issuing irreversible actions.
- **Idempotency is on you.** Because replay assumes entries apply cleanly in sequence, your tools should tolerate being resumed near a partial operation. Use idempotency keys for anything with external effects so a resume after a mid-write crash cannot double-apply.
- **Some state lives elsewhere.** Replay rebuilds the key-value context store. It deliberately does not restore long-term memory providers (ChromaDB, Mem0 — those have their own persistence) or the short-term conversation buffer. If you need conversation history to survive a crash, include it in your checkpoint payload, and keep checkpoint data JSON-serializable.

Naming these limits is the point. Replay gives you exact recovery of the state the journal actually captured — nothing more, nothing less.

## When a different approach is a better fit

Journals earn their keep for stateful, long-running, autonomous processes. They are overkill elsewhere, and it is worth being honest about that:

- **Stateless request/response agents.** If your agent handles one turn and exits, there is nothing to replay. Set `level="none"` and skip the machinery entirely.
- **A durable job queue is your real need.** If the actual problem is "retry this unit of work until it succeeds," a task/queue system with at-least-once delivery and idempotent consumers is a cleaner fit than reconstructing agent state.
- **Full time-travel debugging or bitemporal audit.** Promptise journals target crash recovery and observability, not arbitrary historical querying across every version of every value. A dedicated event store gives you richer replay-any-point-in-history semantics if that is what you need.

Reach for journals when the thing that must survive a crash is *the agent's own evolving state*. For a broader treatment of designing agents that stay up for days, see [How to Build a Long-Running AI Agent](long-running-ai-agent.md).

## Frequently asked questions

### How do I resume an AI agent after a crash?

Enable a `FileJournal` (via `JournalConfig(level="checkpoint", backend="file")`) so the runtime records state transitions and checkpoints as it works. On restart, `ReplayEngine.recover(process_id)` loads the last checkpoint and replays the entries after it, returning the reconstructed `context_state` and `lifecycle_state`. Under `AgentRuntime` this happens automatically before the process accepts new triggers.

### Is agent replay deterministic?

Yes for state reconstruction: given the same journal, `ReplayEngine` always rebuilds the same context and lifecycle state, because it only applies recorded state mutations in order. It is *not* a re-execution engine — tool calls and other external side effects are never replayed, so a resume never re-sends an email or re-charges a payment.

### What is the difference between checkpoint and full journal levels?

`"checkpoint"` stores a full state snapshot after each trigger-invoke-result cycle, which is enough to recover with minimal disk cost — this is the production default. `"full"` additionally records every context update and side effect, giving finer-grained replay and a complete audit trail at the cost of more writes. Use `"full"` when debugging a specific issue or meeting compliance requirements.

## Next steps

Enable a FileJournal on your process, kill it mid-run, and watch `ReplayEngine` restore it exactly where it left off — then decide whether `"checkpoint"` or `"full"` fits your durability budget. Start from the [Quick Start](../../getting-started/quickstart.md) to stand up your first agent, then read the [journal system](../../runtime/journal/index.md) reference to wire crash recovery into a long-running process.
