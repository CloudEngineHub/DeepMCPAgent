---
title: "LangGraph Checkpointing vs Journal-Replay Explained"
description: "LangGraph genuinely checkpoints and resumes, so the honest question isn't 'does it persist state' but 'what does a checkpoint capture versus a replayable…"
keywords: "langgraph checkpointing vs journaling, langgraph checkpointer durability, agent state checkpoint vs replay, langgraph resume after crash, replay engine vs checkpointer, langgraph durability limits"
date: 2026-07-16
slug: langgraph-checkpointing-vs-journaling
categories:
  - Runtime
---

# LangGraph Checkpointing vs Journal-Replay Explained

The honest way to frame **langgraph checkpointing vs journaling** is not "which one persists state" — both genuinely do — but "what unit each one persists": a LangGraph checkpoint saves the state of a single graph run, while a Promptise journal records the append-only event stream of a long-running, self-triggering process. That distinction sounds academic until a process reboots at 2am mid-run and you need it back exactly where it was. This post draws the line precisely, shows what each mechanism captures, and gives you a runnable script so you can watch a journal rebuild a crashed process from its own event log.

## What a LangGraph checkpoint actually captures

Give LangGraph credit where it is due, because a lot of "LangGraph alternative" content gets this wrong. LangGraph checkpointing is real, durable, and well-engineered. When you attach a checkpointer — `MemorySaver` for tests, or the durable `SqliteSaver`/`PostgresSaver` backends — LangGraph writes a **snapshot of the graph's channel state at every super-step**, keyed by the `thread_id` you pass in `config={"configurable": {"thread_id": ...}}`. Each snapshot carries metadata: the step number, which node produced which writes, and any pending writes. Because a checkpoint is saved per super-step, a thread accumulates a linear history you can walk with `get_state_history()` and fork from by `checkpoint_id`. That is genuine time-travel.

This is what powers LangGraph's human-in-the-loop story. `interrupt()` pauses the graph; re-invoking with the same `thread_id` resumes from the saved channels. And with a Postgres or SQLite saver, the snapshot survives a process restart — so **langgraph resume after crash** works: bring the process back, re-invoke the graph with the same thread, and it continues from the last durable checkpoint. If your agent is a request/response graph, that may be all the durability you ever need, and LangGraph is a mature, correct choice for it.

So this is not a "they can't do it" article. LangGraph checkpointer durability is real. The interesting question is what a checkpoint is *scoped to*, and what sits around it.

## What an append-only journal captures instead

A Promptise [journal](../../runtime/journal/index.md) is a different data structure aimed at a different unit. Rather than snapshotting the state of one graph invocation, it records a durable, append-only ledger of everything a **process** does over its whole lifetime. Every meaningful event becomes a `JournalEntry`:

| Entry type | What it records |
|---|---|
| `state_transition` | Lifecycle change (e.g. `created → running`, `running → suspended`) |
| `trigger_event` | A trigger fired — which type, and its payload |
| `invocation_start` / `invocation_result` | An agent invocation began and completed |
| `context_update` | A single context key changed value |
| `checkpoint` | A full state snapshot, taken after each trigger-invoke-result cycle |
| `error` | Something failed |

Two backends implement the same `JournalProvider` protocol: `InMemoryJournal` for tests and `FileJournal` — append-only JSONL on disk — for production. A journal level (`"none"`, `"checkpoint"`, `"full"`) controls how much detail you keep, with `"checkpoint"` as the production default.

The design is textbook **event sourcing**: checkpoints are the periodic snapshots, and the entries between them are the event log. That means a journal contains both the "current state" a checkpointer would give you *and* the ordered stream of what happened since — the transitions, the trigger firings, the invocation results. This is the heart of **agent state checkpoint vs replay**: a checkpoint alone answers "what was the state," while a journal also answers "what happened, in what order, driven by what."

## The precise delta: a run's snapshot vs a process's event log

Here is the exact difference, stated fairly. A LangGraph checkpoint is the state of *one graph run*, indexed by super-step and keyed by `thread_id`. A Promptise journal is the event ledger of a *supervised, long-lived process* that wakes itself up. The gap has three parts, and none of them is "LangGraph is missing durability."

**1. The unit of recovery.** LangGraph's checkpoint history is scoped to a graph invocation. To make it "resume after a crash," something outside LangGraph must re-invoke the graph with the right `thread_id` — the library does not wake itself. A Promptise [agent process](../../runtime/processes.md) is the durable unit: it owns a lifecycle (`CREATED → RUNNING → SUSPENDED → STOPPED / FAILED`), and its journal records that lifecycle so the process — not just a conversation thread — comes back.

**2. Triggers are first-class, in-process.** The open-source LangGraph library ships no trigger system; you invoke the graph yourself. LangGraph Platform, the hosted product, does add cron jobs and webhooks — so this is a partial feature living in the managed service, not in the library. In Promptise, triggers are library primitives you attach to a process in code: `CronTrigger`, `WebhookTrigger`, `FileWatchTrigger`, `EventTrigger`, and `MessageTrigger`. Every firing is a `trigger_event` in the journal, so recovery replays not just state but *what woke the process*.

**3. Governance and secrets wrap the run.** LangGraph checkpoints the graph; it does not ship budget limits, behavioral health checks, mission tracking, or per-process secret rotation around that run. Promptise makes those structural: budgets on tool calls and cost, health detection for stuck or looping behavior, mission evaluation, and TTL-scoped secrets — all opt-in on the same `ProcessConfig`, all observable in the same journal.

So the delta is not durability. It is the *unit* (a graph run versus a self-triggering process) and the supervision Promptise makes first-class around it. That is why comparing a **replay engine vs checkpointer** is comparing two layers, not two implementations of one thing. For the broader treatment of this design philosophy, see [Durable Execution for AI Agents in Python](durable-execution-for-ai-agents.md).

## Crash a process and replay it from the journal

The most convincing way to feel the difference is to run it. The script below records some work, takes a checkpoint (the LangGraph-style "current state"), writes more events *after* the snapshot, simulates a crash, and then uses the `ReplayEngine` to rebuild the exact final state. It uses only the real journal API and runs with no API key or LLM call.

```python
import asyncio

from promptise.runtime.journal import FileJournal, JournalEntry, ReplayEngine


async def main() -> None:
    journal = FileJournal(base_path=".promptise/journal")
    pid = "pipeline-monitor"

    # The process starts and runs a cycle. In a live process the runtime
    # writes these entries for you; here we append them by hand to see
    # exactly what a journal holds.
    await journal.append(JournalEntry(
        process_id=pid,
        entry_type="state_transition",
        data={"from_state": "created", "to_state": "running"},
    ))
    await journal.append(JournalEntry(
        process_id=pid,
        entry_type="trigger_event",
        data={"trigger_type": "cron", "scheduled_time": "2026-07-16T02:00:00"},
    ))

    # A checkpoint is a full state snapshot — the "current state" a bare
    # checkpointer would hand back on resume.
    await journal.checkpoint(pid, {
        "context_state": {"checks_run": 12, "pipeline_status": "healthy"},
        "lifecycle_state": "running",
    })

    # More events happen AFTER the snapshot. This is precisely the slice a
    # snapshot-only mechanism would lose on a crash.
    await journal.append(JournalEntry(
        process_id=pid,
        entry_type="trigger_event",
        data={"trigger_type": "cron", "scheduled_time": "2026-07-16T02:05:00"},
    ))
    await journal.append(JournalEntry(
        process_id=pid,
        entry_type="context_update",
        data={"key": "checks_run", "value": 13},
    ))
    await journal.append(JournalEntry(
        process_id=pid,
        entry_type="context_update",
        data={"key": "pipeline_status", "value": "degraded"},
    ))
    # --- CRASH: the box is rebooted right here, mid-run ---

    # Restart: the ReplayEngine loads the last checkpoint and replays every
    # entry written after it, rebuilding the process state deterministically.
    engine = ReplayEngine(journal)
    recovered = await engine.recover(pid)

    print(recovered["lifecycle_state"])   # running
    print(recovered["context_state"])     # {'checks_run': 13, 'pipeline_status': 'degraded'}
    print(recovered["entries_replayed"])  # 3

    await journal.close()


asyncio.run(main())
```

Read the output carefully. The checkpoint captured `checks_run = 12` and `pipeline_status = "healthy"`. A snapshot-only restore would hand you exactly that and drop the three post-checkpoint events. The `ReplayEngine` instead loads the checkpoint *and* replays the entries after it, so the process comes back as `checks_run = 13`, `pipeline_status = "degraded"` — the true last-known-good state. That is deterministic recovery: given the same journal, you always rebuild the same state. The full recovery contract, including the returned `last_entry_type` and how to feed the result into a fresh `AgentContext`, is documented on the [Replay Engine](../../runtime/journal/replay.md) page.

In a real deployment you rarely call `append()` and `checkpoint()` yourself — you declare a `JournalConfig` and the runtime writes a checkpoint after each trigger-invoke-result cycle, then runs the `ReplayEngine` automatically on restart before accepting new triggers. Because the journal also captured the trigger that woke the process, this same machinery is what lets a process pick up on a different node after its original host dies, as covered in [Fail an AI Agent Over to Another Node After a Crash](ai-agent-failover-to-another-node.md).

## When each one is the right tool

Being honest about the boundary matters more than winning the comparison.

**Reach for LangGraph checkpointing when** your agent is fundamentally a request/response graph with intricate branching; you want thread-level pause, resume, and time-travel for human-in-the-loop review; and something else in your stack already owns scheduling, supervision, and recovery of the process. Its checkpointer is a strong, durable fit for durable *conversations*, and the langgraph durability limits you should plan around are exactly its scope: the snapshot belongs to a graph run keyed by thread, and the library does not wake, govern, or fail the process over for you.

**Reach for journals plus the `ReplayEngine` when** the thing that must survive a crash is *the agent's own evolving process state* — a daemon that fires on a cron schedule or webhook, mutates context across hundreds of invocations, and must resume rather than restart. Here the append-only ledger of transitions, triggers, and invocations is the point, and event-sourced replay reconstructs the whole process, not one thread.

And know the limits on both sides. Replay reconstructs the key-value context and lifecycle state the journal captured; it deliberately does not re-execute side effects (a replayed `context_update` never re-charges a card), and it does not restore long-term memory providers or the conversation buffer unless you put them in the checkpoint payload. Naming those limits honestly is what makes the recovery guarantee trustworthy.

## Frequently asked questions

### Does LangGraph checkpointing give me durable crash recovery?

For a graph run, largely yes. With `SqliteSaver` or `PostgresSaver`, LangGraph persists a per-super-step snapshot keyed by `thread_id`, so restarting the process and re-invoking the graph resumes from the last durable checkpoint — including after a crash. What it does not give you is a self-triggering, governed *process* that reconstructs itself; that scope belongs to a runtime layer, not a graph library.

### What is the actual difference between a checkpointer and a replay engine?

A checkpointer restores by pointing the next graph invocation at the last saved snapshot. A `ReplayEngine` loads the last checkpoint *and* replays every journal entry written after it — an event-sourcing step — to rebuild a process's context and lifecycle state. Both use a "last checkpoint"; the replay engine adds the incremental event log on top and targets a supervised process rather than a graph thread.

### Can I keep LangGraph-style control flow and still get journaling?

Yes. In Promptise you shape reasoning with a built-in `agent_pattern` or a custom `PromptGraph`, then wrap that agent in an `AgentProcess` whose durability comes from the journal and `ReplayEngine`. You keep graph-based control flow and gain process-level triggers, governance, and crash recovery on top of it.

## Next steps

Compare it yourself instead of taking the summary on faith: attach a `FileJournal`, crash the process mid-run, and replay it deterministically from the journal. Start with the [journal system](../../runtime/journal/index.md) reference to wire a `JournalConfig` into a long-running [agent process](../../runtime/processes.md), then read the [Replay Engine](../../runtime/journal/replay.md) page to see exactly what `recover()` reconstructs — and what it honestly leaves to you.
