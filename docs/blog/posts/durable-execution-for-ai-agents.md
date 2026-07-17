---
title: "Durable Execution for AI Agents in Python"
description: "The cluster hub, framed around the term of art 'durable execution.' Durable-execution engines (Temporal, Restate, DBOS) give exactly-once workflows but treat…"
keywords: "durable execution for ai agents, durable ai agent, durable execution python, crash-recoverable ai agent, stateful long-running agent, agent-native durable execution"
date: 2026-07-16
slug: durable-execution-for-ai-agents
categories:
  - Runtime
---

# Durable Execution for AI Agents in Python

Durable execution for AI agents is the property that lets a long-running, self-triggering agent survive a crash and come back exactly where it left off — same context, same counters, same lifecycle state — instead of restarting from zero. The term "durable execution" comes from the workflow world, where engines like Temporal, Restate, and DBOS made it famous. This page explains what the phrase means once your unit of work stops being a deterministic workflow and becomes a stateful agent, why the existing tools only solve half of it, and how Promptise Foundry closes the gap with a supervised process, an append-only journal, and a replay engine. It is the hub for the whole durability cluster, so it stays at the architecture altitude and points you at the deep dives for each mechanism.

<!-- more -->

## From durable workflows to durable agents

Durable execution has a precise meaning: the runtime persists the progress of a computation so that, after any failure, it resumes from the last recorded point rather than re-running from the start. Classic durable-execution engines achieve this by recording every step of a workflow to a log and deterministically replaying that log on recovery.

An autonomous agent needs the same guarantee, but its "computation" looks nothing like a workflow. A workflow is a bounded, mostly-deterministic function you wrote. A [long-running agent](long-running-ai-agent.md) is an open-ended process: it wakes on triggers, calls an LLM whose output you cannot predict, mutates its own key-value state, fires side effects, and keeps running for days. Two things it accumulates make durability non-negotiable:

- **State that only exists at runtime** — a cursor, a running count, `pipeline_status = "degraded"`, budget consumed so far, mission progress.
- **Side effects it must not repeat** — an invoice already processed, an email already sent, a card already charged.

Lose the first on a crash and the agent forgets what it learned. Repeat the second and you have a duplicate-charge incident. Durable execution for AI agents is the discipline that prevents both.

## What other frameworks do today

It is worth being exact about the landscape, because two very different tools both get called "durable," and the distinction is the whole point.

**Durable-execution engines — Temporal, Restate, DBOS.** These are battle-tested and genuinely excellent at what they do. Temporal replays a workflow from its event history to reconstruct state and gives you exactly-once activity semantics; Restate journals each handler step and durable promise; DBOS persists workflow and step state to Postgres and recovers in-flight work after a crash. The honest delta is not durability — it is scope. All three treat your agent as opaque code. They ship no LLM tool discovery, no memory providers, no triggers, and no governance. If you build an agent on Temporal, you write the tool-calling loop, the memory layer, the trigger plumbing, and the budget/health/mission logic yourself, on top of the workflow primitives. The durability is first-class; the *agent* is entirely your problem.

**LangGraph checkpointers.** This is the closest comparison, and it is a real capability, so precision matters. LangGraph's `SqliteSaver` and `PostgresSaver` persist graph state per thread at every super-step boundary, and they support resuming a thread, human-in-the-loop interrupts, and even time-travel over a thread's checkpoint history. What they do not do is change what a checkpoint *is*: it is the state of one graph run, keyed by `thread_id`. It is not a supervised, self-triggering process with a decision journal, budget and health governance, and secrets rotation wrapped around it. If your graph is invoked, resumes, and finishes, that is exactly the right tool. If you need a process that lives for days, wakes itself on a cron or an event, and stays inside guardrails the entire time, the checkpointer is one component of that system rather than the system itself. We cover the mechanical differences in [LangGraph Checkpointing vs Journal-Replay Explained](langgraph-checkpointing-vs-journaling.md).

Promptise's edge is not "we have durability and they don't." It is that Promptise makes the *supervision layer* — the process, the journal, and the governance around it — a first-class, structural part of the agent, so durable execution is something you configure rather than something you assemble.

## How Promptise makes durability first-class

Three pieces cooperate, and each maps to a page in the durability cluster.

**The process.** An [`AgentProcess`](../../runtime/processes.md) is the lifecycle container that turns a stateless LLM wrapper into a supervised, long-lived unit. It moves through an explicit state machine — `CREATED → STARTING → RUNNING → SUSPENDED → STOPPING → STOPPED / FAILED` — with a trigger queue, a heartbeat, a concurrency semaphore, and its own context state. Because every transition is explicit, every transition is recordable.

**The journal.** The [journal system](../../runtime/journal/index.md) is an append-only record that sits between the runtime and disk. Every meaningful event — a state transition, a trigger firing, an invocation result, a context mutation, and a periodic full-state checkpoint — is written as a `JournalEntry`. This is textbook event sourcing: an ordered event log plus checkpoints so recovery never has to replay from the beginning of time. Two backends share one `JournalProvider` protocol — `InMemoryJournal` for tests and `FileJournal` (append-only JSONL) for production — so you develop against memory and ship against files with no code change. A journal *level* (`"none"`, `"checkpoint"`, `"full"`) controls how much detail you keep.

**The replay engine.** The [`ReplayEngine`](../../runtime/journal/replay.md) turns a journal back into live state. On restart it loads the last checkpoint, collects every entry written after it, and applies them in order to rebuild the process's context and lifecycle state. Under `AgentRuntime` this runs automatically before the process accepts new triggers, so the agent picks up its counters and cursors instead of starting fresh.

The reason this is "first-class" and not "assembled" is that governance rides the same journal. Budget, [behavioral health](behavioral-anomaly-detection-for-ai-agents.md), and mission progress are recorded alongside state, so a recovered process comes back with its guardrails intact — a dimension the workflow engines leave entirely to you. See [autonomous agent governance](autonomous-agent-governance.md) for how those layers fit together.

## A crash-recoverable agent, end to end

Here is a complete, runnable script. It records a lifecycle transition, checkpoints the process's state after a completed cycle, writes one more update, simulates a `kill -9`, and then recovers — proving the process comes back exactly where it left off. It needs no API key, because it exercises the durable spine directly:

```python
import asyncio

from promptise.runtime.journal import FileJournal, JournalEntry, ReplayEngine
from promptise.runtime.lifecycle import ProcessState


async def main() -> None:
    journal = FileJournal(base_path=".promptise/journal")

    # An AgentProcess records each lifecycle transition as it runs.
    await journal.append(JournalEntry(
        process_id="market-watcher",
        entry_type="state_transition",
        data={"from_state": ProcessState.STARTING, "to_state": ProcessState.RUNNING},
    ))

    # After a completed trigger -> invoke -> result cycle it checkpoints its state.
    await journal.checkpoint("market-watcher", {
        "context_state": {"reports_written": 12},
        "lifecycle_state": ProcessState.RUNNING,
    })

    # More work lands *after* the checkpoint, then the box is killed (kill -9).
    await journal.append(JournalEntry(
        process_id="market-watcher",
        entry_type="context_update",
        data={"key": "reports_written", "value": 13},
    ))

    # Restart: rebuild the exact state straight from the journal.
    recovered = await ReplayEngine(journal).recover("market-watcher")

    print(recovered["lifecycle_state"])   # running
    print(recovered["context_state"])     # {'reports_written': 13}
    print(recovered["entries_replayed"])  # 1

    await journal.close()


asyncio.run(main())
```

Notice the count read `12` at checkpoint time but recovers as `13` — the post-checkpoint `context_update` was replayed on top of the snapshot. That is the guarantee: given the same journal, you always rebuild the same state. In a real deployment you never call `append()` and `checkpoint()` by hand; you declare a journal in the process config and the runtime writes checkpoints after every trigger-invoke-result cycle for you. The step-by-step mechanics — how entries after a checkpoint are collected and applied, and how you feed the recovered values back into a fresh context — live in the [crash-recovery deep dive](ai-agent-crash-recovery.md).

## The durability subtopics, mapped

This page is the hub; each durability concern has its own dedicated page so you can go as deep as you need:

- **Crash recovery mechanics** — the exact journal-plus-replay flow, journal levels, and the honest limits of replay: [AI Agent Crash Recovery with Journals & Replay](ai-agent-crash-recovery.md).
- **Checkpointing, compared** — how per-thread graph checkpoints differ from a process decision journal: [LangGraph Checkpointing vs Journal-Replay Explained](langgraph-checkpointing-vs-journaling.md).
- **Failover across nodes** — recovering a process onto a *different* machine after the original host dies: [Fail an AI Agent Over to Another Node After a Crash](ai-agent-failover-to-another-node.md).
- **Behavioral health** — detecting stuck, looping, or degraded processes before they burn budget: [behavioral anomaly detection](behavioral-anomaly-detection-for-ai-agents.md).
- **Governance that survives recovery** — budget, mission, and secrets that ride the same journal: [autonomous agent governance](autonomous-agent-governance.md).
- **The runtime, end to end** — what a triggered, governed, durable process looks like as a whole: [What Is an Autonomous AI Agent Runtime?](autonomous-ai-agent-runtime.md).

Reach for this machinery when the thing that must survive a crash is *the agent's own evolving state*. For a stateless request/response agent there is nothing to replay — set the journal level to `"none"` and skip it entirely.

## Frequently asked questions

### Is durable execution for AI agents the same as Temporal or DBOS?

It is the same guarantee applied to a different unit of work. Temporal, Restate, and DBOS deliver exactly-once durable execution for workflows — deterministic code you write — and they do it extremely well. They do not model an agent: no tool discovery, memory, triggers, or governance ship with them, so you build the entire agent layer yourself. Promptise applies durable execution to a supervised agent process directly, with the journal, replay, and governance already wired in.

### How is a Promptise journal different from a LangGraph checkpointer?

LangGraph's `SqliteSaver`/`PostgresSaver` persist the state of a single graph run per thread at super-step boundaries and support resume and time-travel — that part is real and works well. The difference is scope: a thread checkpoint is one graph run, whereas a Promptise `FileJournal` is the append-only decision log of a long-lived, self-triggering process, and `ReplayEngine` reconstructs that process — context, lifecycle state, and the governance around it — after a crash. See the [checkpointing comparison](langgraph-checkpointing-vs-journaling.md) for a side-by-side.

### Does replay re-run my tool calls and side effects?

No. The journal records *state changes*, not the external actions that caused them. `ReplayEngine` restores the state that resulted from invocation 41; it never re-charges a card or re-sends an email. That is intentional — replay reconstructs memory, it does not re-issue irreversible actions, so your tools should stay idempotent for anything with external effects.

### Which journal backend should I use?

Use `InMemoryJournal` in tests and `FileJournal` (append-only JSONL) in production; they share the same `JournalProvider` protocol, so you swap one for the other with no code change. Keep the level at `"checkpoint"` for a durable, low-cost recovery baseline, and switch to `"full"` when you need every side effect recorded for debugging or audit.

## Next steps

`pip install promptise`, wrap your agent in an `AgentProcess` with a `FileJournal`, and watch it resume from the last checkpoint after a `kill -9`. Start from the [agent processes](../../runtime/processes.md) reference to stand up a supervised process, wire durability with the [journal system](../../runtime/journal/index.md) and its [replay engine](../../runtime/journal/replay.md), then follow the [crash-recovery deep dive](ai-agent-crash-recovery.md) to make recovery automatic under `AgentRuntime`.
