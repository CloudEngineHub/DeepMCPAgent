---
seo_title: "Crash Recovery with the Replay Engine"
description: "ReplayEngine rebuilds an agent process from its journal by loading the last checkpoint and replaying later entries, so a crashed process carries on."
---

# Replay Engine

`ReplayEngine` rebuilds a process's state from its journal: it loads the last checkpoint and replays the entries written after it. An `AgentProcess` uses it on its first `start()` when `JournalConfig.recover_on_start` is on (the default), so a process that crashed or was redeployed continues with its context state, conversation, budget counters and mission. Use the engine directly to inspect what a process would recover, or to build your own tooling on a journal.

```python
from promptise.runtime.journal import FileJournal, ReplayEngine

engine = ReplayEngine(FileJournal(".promptise/journal"))
recovered = await engine.recover("notes")
# {
#     "context_state": {"stage": "draft"},
#     "lifecycle_state": "stopped",
#     "last_entry_type": "state_transition",
#     "entries_replayed": 1,
#     "mission": None,
#     "checkpoint": {"context_state": {...}, "lifecycle_state": "running", "invocation_count": 2,
#                    "conversation": [...]},
# }
```

`recover()` only reads. It changes neither the journal nor any process.

---

## How recovery works

1. **Load the last checkpoint** with `journal.last_checkpoint(process_id)`. Its `context_state` and `lifecycle_state` are the starting point (the checkpoint is copied, so the stored one is never modified). Without a checkpoint, recovery starts from `context_state={}` and `lifecycle_state="created"`.
2. **Find the entries after it.** The engine reads every entry and keeps only those after the **last** `checkpoint` entry; older entries, older checkpoints included, are already part of the snapshot. Without a checkpoint, every entry is replayed.
3. **Replay them in order:**
    - `state_transition` sets `lifecycle_state` to its `to_state`;
    - `context_update` sets `context_state[key] = value`;
    - every other type only updates `last_entry_type`.

The result:

| Key | Type | Description |
|---|---|---|
| `context_state` | `dict[str, Any]` | The rebuilt context state |
| `lifecycle_state` | `str` | The last recorded process state (`"stopped"` after a clean stop, `"running"` after a crash) |
| `last_entry_type` | `str` | Type of the last replayed entry (`""` when nothing was replayed) |
| `entries_replayed` | `int` | Entries replayed after the checkpoint |
| `mission` | `dict \| None` | The checkpoint's mission state (`MissionTracker.to_dict()`), for `MissionTracker.restore()` |
| `checkpoint` | `dict` | The raw last checkpoint (`{}` when there is none): `conversation`, `invocation_count`, `budget`, ... |

---

## What the process does with it

On its first `start()`, an `AgentProcess` with a journal and `recover_on_start=True` calls `recover(name)` and applies the result before it journals its own first transition:

| Restored | From |
|---|---|
| Context state | `context_state` (recovered keys replace the same keys of `context.initial_state`) |
| Mission progress and history | `mission`, when the process has a mission |
| Budget counters | `checkpoint["budget"]`, when the process has a budget (a restart can't reset the daily limits) |
| Conversation history | `checkpoint["conversation"]`, when `context.conversation_history` is on |
| Invocation count | `checkpoint["invocation_count"]` |

`process.recovered` then reports what was restored, and a `recovered` entry is journaled. A journal with neither a checkpoint nor context writes restores nothing; a journal that can't be read is logged and the process starts fresh. See [Recovery on start](index.md#recovery-on-start).

You don't need the engine to recover a process. Use it to look before you start one:

```python
recovered = await ReplayEngine(journal).recover("notes")
if recovered["lifecycle_state"] == "running":
    print("notes crashed last time; it will resume with", sorted(recovered["context_state"]))
```

---

## Recovery scenarios

### A crash after a few runs

```
1. state_transition   created -> starting
2. state_transition   starting -> running
3. context_update     stage = "draft"
4. invocation_result  invocation 1
5. checkpoint         {context_state: {stage: "draft"}, lifecycle_state: "running", ...}
6. context_update     stage = "published"
--- crash ---
```

Recovery loads the checkpoint (entry 5), replays entry 6, and returns `context_state={"stage": "published"}`, `lifecycle_state="running"`, `entries_replayed=1`. The process restores the conversation and invocation count from the checkpoint, so the run that was cut off is the only thing lost.

### A crash before the first run finished

```
1. state_transition   created -> starting
2. state_transition   starting -> running
3. context_update     stage = "draft"
--- crash ---
```

No checkpoint: all three entries are replayed. The result is `context_state={"stage": "draft"}`, `lifecycle_state="running"`, `entries_replayed=3`, `checkpoint={}`.

### A new process

```python
await engine.recover("new-process")
# {"context_state": {}, "lifecycle_state": "created", "last_entry_type": "",
#  "entries_replayed": 0, "mission": None, "checkpoint": {}}
```

---

## Common pitfalls

- **Expecting side effects to be replayed.** Replay rebuilds state only. A tool call that sent an email is not repeated, and one that was cut off by the crash is not resumed. Make external actions idempotent (for example with an idempotency key stored in the context).
- **Expecting long-term memory from the journal.** Memory providers (ChromaDB, Mem0, ...) persist on their own; the journal holds the context state and the conversation buffer.
- **Values stored redacted.** With secret scoping, secrets were journaled as `[REDACTED]` and come back that way. Keep secrets in the secret scope, not in the context state.
- **Custom entry types.** Only `state_transition` and `context_update` change the replayed state. Record your own state as `context_update` entries (`data={"key": ..., "value": ...}`), or write it through `process.context.put()`, which does this for you.

---

## What's Next

- [Journal Overview](index.md) -- entry types, levels and recovery on start
- [Journal Backends](backends.md) -- `FileJournal` and `InMemoryJournal`
- [Rewind](rewind.md) -- go back to an earlier point instead of the latest one
- [Lifecycle](../lifecycle.md) -- the state machine behind `state_transition` entries
- [Context](../context.md) -- the state layer behind `context_update` entries

!!! tip "Learn the concept"

    - [Durable AI Agents: Crash Recovery and Safe Resume](https://www.promptise.com/guides/view/durable-ai-agent-crash-recovery) -- Resume from the last step after a crash, with idempotency keys.
    - [AI Agent State Between Runs: Context, Keys and Audit](https://www.promptise.com/guides/view/ai-agent-state-between-runs) -- What a process keeps between runs and how it is restored.
