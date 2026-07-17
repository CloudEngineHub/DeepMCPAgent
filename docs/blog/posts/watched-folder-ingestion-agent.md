---
title: "Build a Crash-Safe Watched-Folder Ingestion Agent"
description: "A single end-to-end build, not a tour of trigger types (that overview lives elsewhere). Wire a FileWatchTrigger to a supervised AgentProcess, then solve the…"
keywords: "watched folder ingestion agent, idempotent file processing agent, drop folder ai pipeline, process files on arrival without reprocessing, durable file ingestion agent, restart-safe file processing"
date: 2026-07-16
slug: watched-folder-ingestion-agent
categories:
  - Runtime
---

# Build a Crash-Safe Watched-Folder Ingestion Agent

A **watched folder ingestion agent** watches a drop folder, wakes the instant a file lands, and runs an LLM over it — extract, classify, route, emit. This is not a tour of the runtime's trigger types; that overview lives in the [Triggers guide](../../runtime/triggers/index.md). It is one end-to-end build. We wire a `FileWatchTrigger` to a supervised `AgentProcess`, and then solve the three things a naive watcher gets wrong the moment it meets production: reprocessing after a crash, an unbounded bill when a thousand files land at once, and losing everything the agent learned between drops. The watch itself is the easy part. The durable, restart-safe pipeline around it is the point.

## Why a naive watcher reprocesses everything after a crash

The tutorial version of this is a twenty-line `watchdog` or `inotify` script: register a handler, glob the directory on startup to catch anything that arrived while you were down, and process each match. It works on your laptop. Then it meets a crash.

A hand-rolled watcher starts cold. It keeps no durable record of which files it already finished, so its recovery strategy is "scan the folder and process what's there." After a `kill -9` halfway through a batch, that scan re-runs the *entire* folder — including the files it already processed a minute ago. For a pure transform that just overwrites an output file, re-running is harmless. For an ingestion agent whose steps have downstream side effects — an invoice posted to your ledger, a webhook fired, a row inserted, an email sent — re-running means every one of those side effects fires a second time. A duplicate-charge incident is a crash away.

So a real **drop folder AI pipeline** needs three properties the naive script lacks:

- **Idempotency.** Once a file is finished, a restart must skip it — no reprocessing, no re-emitted side effect.
- **A budget ceiling.** A flood of two thousand drops at 3am must not run the agent up an unbounded bill.
- **Memory across drops.** Each file should benefit from what earlier files taught the agent, instead of starting from a blank slate every time.

The Agent Runtime gives you all three by binding the watch to a *supervised process* with a durable journal. Let's build it.

## Wire a FileWatchTrigger to a supervised AgentProcess

In Promptise Foundry you don't run a bare event loop and call `agent.ainvoke()` yourself. You declare a `FileWatchTrigger` on an `AgentProcess`, start the process once, and let it wake itself whenever a matching file appears. The process stays resident; the trigger fires with the file's path in its payload; the agent runs; the process goes back to waiting.

Here is the full configuration. Glob patterns keep temporary and swap files out; a file-backed journal makes the run durable; a budget caps the batch; and `ContextConfig` seeds the journaled ledger of finished files that the next section relies on:

```python
from promptise.runtime import (
    ProcessConfig,
    TriggerConfig,
    JournalConfig,
    BudgetConfig,
    ContextConfig,
)

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions=(
        "You ingest one dropped file per invocation. The file path is in the "
        "trigger payload. Before doing any work, check the 'processed' list in "
        "your context — if the file is already there, stop immediately and do "
        "nothing. Otherwise extract the invoice fields, post them downstream, "
        "then append the filename to 'processed'."
    ),
    triggers=[
        TriggerConfig(
            type="file_watch",
            watch_path="/data/inbox",
            watch_patterns=["*.pdf", "*.csv"],   # ignore .tmp, .part, swap files
            watch_events=["created"],
        ),
    ],
    # Durable ledger of every trigger, transition, and context write
    journal=JournalConfig(backend="file", path="./ingest-journal"),
    # A flood of drops can't run up an unbounded bill
    budget=BudgetConfig(
        enabled=True,
        max_tool_calls_per_run=8,
        max_cost_per_day=25.0,
        on_exceeded="pause",
    ),
    # The processed-file ledger the agent reads and writes
    context=ContextConfig(
        writable_keys=["processed"],
        initial_state={"processed": []},
    ),
)
```

That is the whole watched folder ingestion agent as configuration. `watch_patterns` is standard glob syntax matched against the filename, so `["*.pdf", "*.csv"]` reacts to real payloads and ignores the half-written `.part` files a naive `["*"]` watcher would choke on. Every field on the trigger is documented in the [File Watch Trigger](../../runtime/triggers/file-watch.md) reference, and every field on the process — restart policy, heartbeat, concurrency — in the [Agent Processes](../../runtime/processes.md) guide.

Because the `AgentProcess` stays resident, two things a cold script throws away survive between drops for free: the **conversation buffer** (a rolling short-term memory of recent invocations) and the **AgentContext** (a key-value store with an audit trail, injected into every invocation). That is the "memory carried across drops" property — this afternoon's file is processed by an agent that still remembers this morning's.

## Make it idempotent: a journaled ledger of finished files

Here is the mechanism that makes the pipeline **restart-safe**. Every write to `AgentContext` is recorded in the journal as a `context_update` entry. When the agent finishes a file and calls `ctx.put("processed", [...])`, that write becomes a durable journal record. On restart, the `ReplayEngine` loads the last checkpoint and replays every `context_update` written after it, rebuilding the exact `processed` list the agent had when the box died. The agent's instruction to "check `processed` before doing any work" then does the rest: a file already in the ledger is skipped, so its side effects never fire twice.

You normally never touch the journal by hand — you declare a `JournalConfig` and the runtime writes checkpoints after every trigger-invoke-result cycle. But the durable spine runs with no API key and no LLM call, so you can prove the guarantee to yourself directly. This script finishes two files, checkpoints, finishes a third *after* the checkpoint, crashes, and then rebuilds the ledger from the journal:

```python
import asyncio

from promptise.runtime.journal import FileJournal, JournalEntry, ReplayEngine


async def main() -> None:
    journal = FileJournal(base_path=".promptise/ingest-journal")
    pid = "invoice-ingestor"

    # A drop lands three files. The process starts a batch.
    await journal.append(JournalEntry(
        process_id=pid,
        entry_type="state_transition",
        data={"from_state": "created", "to_state": "running"},
    ))
    await journal.append(JournalEntry(
        process_id=pid,
        entry_type="trigger_event",
        data={"trigger_type": "file_watch", "path": "/data/inbox/invoice-003.pdf"},
    ))

    # Two files finish. Each is recorded in the journaled processed-file
    # ledger the instant its side effect commits, then a checkpoint snapshots it.
    await journal.checkpoint(pid, {
        "context_state": {"processed": ["invoice-001.pdf", "invoice-002.pdf"]},
        "lifecycle_state": "running",
    })

    # The third file finishes AFTER the checkpoint — one more context_update.
    await journal.append(JournalEntry(
        process_id=pid,
        entry_type="context_update",
        data={"key": "processed",
              "value": ["invoice-001.pdf", "invoice-002.pdf", "invoice-003.pdf"]},
    ))
    # --- CRASH: kill -9 mid-batch, right here ---

    # Restart: rebuild the processed-file ledger from the journal.
    recovered = await ReplayEngine(journal).recover(pid)
    done = set(recovered["context_state"]["processed"])
    print(recovered["lifecycle_state"])      # running
    print(sorted(done))                       # all three survived the crash
    print(recovered["entries_replayed"])      # 1

    # A restart re-scans the folder — but skips anything already finished.
    on_disk = ["invoice-001.pdf", "invoice-002.pdf", "invoice-003.pdf", "invoice-004.pdf"]
    to_process = [f for f in on_disk if f not in done]
    print(to_process)                         # ['invoice-004.pdf'] — no reprocessing

    await journal.close()


asyncio.run(main())
```

Read the output carefully. The checkpoint captured two finished files; the third finished *after* it, as a `context_update` the crash could easily have lost. Replay loads the snapshot **and** applies that trailing entry, so the ledger comes back with all three. When the restarted process re-scans a folder that now also holds `invoice-004.pdf`, it processes exactly one file — the new one. That is what turns a blind re-scan into an **idempotent file processing agent**: the ledger is the source of truth for "already done," and it survives the crash because it lives in the journal. The full recovery contract — what `recover()` reconstructs and what it deliberately leaves to you — is on the [journal system](../../runtime/journal/index.md) page.

One honest limit worth stating: replay reconstructs *state*, not side effects. It never re-posts an invoice or re-fires a webhook — that is the point. Your downstream tool call still has to be the thing that commits, with the ledger write immediately after, so a crash between them is retried rather than double-emitted. The deeper treatment of this event-sourced design is in [Durable Execution for AI Agents in Python](durable-execution-for-ai-agents.md).

## Cap the batch with a budget so a flood can't run up the bill

Idempotency stops you paying twice for the same file. A budget stops a *flood* of distinct files from draining your account before a human notices. The `BudgetConfig` in the process config above enforces two ceilings: `max_tool_calls_per_run=8` bounds any single file's work, and `max_cost_per_day=25.0` bounds the whole day across every drop. When a ceiling is hit, `on_exceeded="pause"` suspends the process instead of letting it keep calling tools unattended — you can also `"stop"` or `"escalate"` to a webhook.

This matters precisely because a **durable file ingestion agent** is unattended by design. It runs at 3am, and if someone accidentally `rsync`s ten thousand files into the inbox, the naive watcher happily processes all ten thousand and you find out from the bill. The budget makes "how much can this cost in a day" a declared invariant rather than a post-incident discovery. Because the budget counters ride the same journal as the ledger, a recovered process comes back with its spend-so-far intact — governance survives the crash exactly like state does.

## What other frameworks do today

It is worth being exact about the landscape, because several tools solve *part* of this well and it would be dishonest to wave them away.

**Hand-rolled `watchdog` / `inotify` scripts.** These genuinely detect file arrivals — that part is solid and `watchdog` is what Promptise itself uses under the hood. What they ship no answer for is durability: there is no persistent record of what was finished, so the standard recovery pattern is a full re-scan that re-emits every side effect. You can bolt on a "seen files" set in SQLite yourself, and people do — but you are then hand-building the journaled ledger, the checkpointing, and the budget that Promptise makes structural.

**Workflow orchestrators — Airflow, Prefect.** These have real file sensors (`FileSensor`, Prefect's filesystem triggers) that wake a run when a path appears, and their runs are retryable. That is a legitimate partial overlap on the *trigger* and on retry idempotency at the task level. The exact delta: they are pipeline schedulers, not LLM-agent runtimes. There is no built-in agent, memory across drops, tool-call budget, or LLM-aware processed-file ledger — you write the ingestion logic, the dedup keying, and the cost guard yourself inside a task. They solve "run something when a file lands," not "run a governed, idempotent agent that remembers."

**Durable-execution engines — Temporal, Restate, DBOS.** These are excellent at exactly-once workflows and can absolutely give you idempotent processing keyed by a workflow ID. The honest delta is scope, not durability: they treat your agent as opaque code. No file-watch trigger, no tool discovery, no memory provider, no budget/health governance ships with them. You would model the folder scan as a workflow and build the entire agent layer on top. The durability is first-class; the *ingestion agent* is your problem.

**LangGraph.** The open-source library ships no trigger system at all — you invoke the graph yourself — and its `SqliteSaver`/`PostgresSaver` checkpointers persist the state of a graph run keyed by `thread_id`, which is real, durable crash recovery for a conversation. LangGraph Platform, the hosted product, adds cron and webhook triggers as a managed feature. Even so, a checkpoint is scoped to one graph run, not a long-lived process with a durable ledger of which *files* it finished; the file-watch-to-idempotent-ingestion path is still yours to assemble. The precise mechanics of that difference are in [LangGraph Checkpointing vs Journal-Replay Explained](langgraph-checkpointing-vs-journaling.md).

Promptise's edge here is not "these tools can't do it." It is that the process, the journaled processed-file ledger, and the budget are *structural, first-class parts of the agent* — you configure a restart-safe ingestion pipeline instead of assembling one from five libraries.

## Frequently asked questions

### How does the agent avoid reprocessing a file after a crash?

It keeps a `processed` list in `AgentContext` and appends each filename the moment that file's side effect commits. Every context write is recorded in the journal as a `context_update` entry, so on restart the `ReplayEngine` replays them and rebuilds the exact list. The agent checks that list before doing any work and skips anything already in it, so a mid-batch crash resumes without reprocessing and without re-emitting downstream side effects.

### Do I have to write the journal entries myself?

No. In production you declare a `JournalConfig(backend="file", ...)` on the `ProcessConfig` and the runtime writes checkpoints after every trigger-invoke-result cycle automatically, then runs the `ReplayEngine` on restart before accepting new triggers. The by-hand `append()`/`checkpoint()` script in this post exists only to let you watch the durable spine work with no API key.

### What stops a flood of dropped files from running up a huge bill?

Attach a `BudgetConfig` with limits like `max_tool_calls_per_run` and `max_cost_per_day`. When a ceiling is hit, the runtime enforces your `on_exceeded` action — `pause`, `stop`, or `escalate` — instead of processing an unbounded batch unattended. The budget counters are journaled, so a recovered process comes back with its spend-so-far intact.

### Which glob patterns should I watch?

Match the real payloads and nothing else. `watch_patterns=["*.pdf", "*.csv"]` reacts to finished files and ignores the `.part`, `.tmp`, and swap files that tools write mid-upload. Patterns are matched against the filename, not the full path. Narrow patterns also reduce event volume on noisy directories.

## Next steps

Point a `FileWatchTrigger` at your drop folder, journal each processed file, and crash the process mid-batch — watch it resume without reprocessing a single one. Start from the [File Watch Trigger](../../runtime/triggers/file-watch.md) reference to tune patterns and events, wire durability with the [journal system](../../runtime/journal/index.md), and read the [Agent Processes](../../runtime/processes.md) guide to layer on restart policies and health checks as you move this from a laptop demo to a production **restart-safe file processing** pipeline.
