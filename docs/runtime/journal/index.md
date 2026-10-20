# Journal System

The journal is a process's durable, append-only record: state transitions, context writes, run results, errors, restarts, and a checkpoint of the recoverable state after every run. It powers three things: you can see what a process did (`promptise runtime logs`, `GET /processes/{name}/journal`), a process picks up where it left off after a crash or restart, and you can [rewind](rewind.md) a process to an earlier point.

```python
from promptise.runtime import AgentProcess, JournalConfig, ProcessConfig

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="You write release notes.",
    journal=JournalConfig(level="checkpoint", backend="file", path=".promptise/journal"),
)

process = AgentProcess("notes", config)
await process.start()
process.context.put("stage", "draft")
# ... trigger events run ...
await process.stop()

# Next time (a new Python process, after a crash or a deploy):
again = AgentProcess("notes", config)
await again.start()
print(again.recovered)
# {'lifecycle_state': 'stopped', 'entries_replayed': 1, 'context_keys': ['stage'],
#  'mission_restored': False, 'budget_restored': False,
#  'conversation_messages': 2, 'invocation_count': 1}
print(again.context.get("stage"))   # 'draft'
```

The entries land in `.promptise/journal/notes.jsonl`, and the latest checkpoint in `.promptise/journal/notes.checkpoint.json`. Entries are filed under the **process name**, not its internal ID, so a restarted process (which gets a new `process_id`) and `promptise runtime logs notes` find them.

---

## Turning it on

Journaling is **off by default** for a process: `ProcessConfig.journal` defaults to `JournalConfig(level="none")`. Pass a `JournalConfig` to turn it on; `JournalConfig()` itself defaults to `level="checkpoint"`.

| Field | Type | Default | Description |
|---|---|---|---|
| `level` | `"none" \| "checkpoint" \| "full"` | `"checkpoint"` | How much is recorded (see below). `"none"` creates no journal at all |
| `backend` | `"file" \| "memory"` | `"file"` | `FileJournal` (JSONL on disk) or `InMemoryJournal` (lost when the Python process exits) |
| `path` | `str` | `".promptise/journal"` | Directory of the file backend, relative to the working directory |
| `recover_on_start` | `bool` | `True` | Restore the recorded state on the process object's first `start()` (see [Recovery on start](#recovery-on-start)) |

In a manifest:

```yaml
journal:
  level: checkpoint
  backend: file
  path: .promptise/journal
  recover_on_start: true
```

Unknown keys are ignored with a `FutureWarning`.

### Levels

| Level | What the process records |
|---|---|
| `"none"` | Nothing (no journal object; rewind and recovery are unavailable) |
| `"checkpoint"` | State transitions, every context write (`context_update`), each run's result, run errors, restarts, recovery and rewind markers, operator actions from the API, secret accesses, mission evaluations, and a checkpoint after every run |
| `"full"` | Everything above, plus each trigger event, each run's start, and every tool call and tool result |

A failing journal backend is logged and never stops the process.

---

## Entry types

Every record is a `JournalEntry`:

| Field | Type | Description |
|---|---|---|
| `entry_id` | `str` | Unique ID (a UUID). This is what [rewind](rewind.md) targets |
| `process_id` | `str` | The process **name** the entry is filed under |
| `timestamp` | `datetime` | When it was recorded (UTC) |
| `entry_type` | `str` | One of the types below |
| `data` | `dict[str, Any]` | Type-specific payload |

What an `AgentProcess` writes:

| `entry_type` | Level | When | `data` |
|---|---|---|---|
| `state_transition` | checkpoint | Every lifecycle transition | `from_state`, `to_state`, `reason`, `process_id` |
| `context_update` | checkpoint | Every write to the context state (by your code, the API's `PATCH /context`, or the agent's state tool) | `key`, `value`, `timestamp`, `source` |
| `invocation_result` | checkpoint | A run finished | `invocation`, `event_id`, `trigger_type`, `duration_ms`, `response` (first 2000 characters) |
| `checkpoint` | checkpoint | After every run, after a failed or refused run when a budget is enabled, when the mission's state changes, on `stop()`, after a rewind | `{"state": {...}}`, see [Checkpoints](#checkpoints) |
| `error` | checkpoint | A run raised | `event_id`, `error_type`, `error` (first 1000 characters) |
| `restart` | checkpoint | An automatic restart begins | `reason`, `attempt` (`None` for a `max_lifetime` recycle) |
| `restart_exhausted` | checkpoint | `max_restarts` reached | `reason`, `restarts`, `max_restarts`, `error` |
| `recovered` | checkpoint | Journal recovery restored state at start | The same dict as `process.recovered` |
| `rewind` | checkpoint | A rewind was applied | `target_entry_id`, `mode`, `actor`, `entries_affected`, `conversation_affected`, `code_affected`, `summary_note` |
| `api_request` | checkpoint | A successful mutating request to the [Orchestration API](../api.md) | Method, route, path parameters, status and client address (never the body) |
| `secret_access` | checkpoint | [Secret scoping](../governance/secrets.md) read, rotated, revoked or expired a secret | `action` (`"access"`, `"rotation"`, `"revoke_all"`, `"expired"`), `secret_name` (never the value) |
| `mission_evaluation` | checkpoint | The [mission](../governance/mission.md) judge evaluated progress | `achieved`, `confidence`, `reasoning`, `progress_summary`, `mission_state`, `invocation_number`, `source` |
| `trigger_event` | full | A run starts for a trigger event | `trigger_id`, `trigger_type`, `event_id`, `payload` |
| `invocation_start` | full | Before the model is called | `invocation`, `event_id` |
| `tool_call` | full | The agent called a tool | `tool`, `args` (redacted) |
| `tool_result` | full | A tool returned | `tool`, `result` (first 2000 characters). Results of `get_secret` are never journaled |

Every entry has an `entry_id`, so any entry can be a rewind target.

Your own code can append any other type with `journal.append(JournalEntry(...))`. Entries serialize with `entry.to_dict()` and `JournalEntry.from_dict(data)`.

### Checkpoints

A checkpoint is a full snapshot of what a process can recover:

| Key | Content |
|---|---|
| `context_state` | The context key-value state |
| `lifecycle_state` | The process state when it was written |
| `invocation_count` | Runs so far |
| `conversation` | The conversation buffer (short-term memory) |
| `budget` | The budget counters (when a budget is enabled) |
| `mission` | The mission's state and evaluation history (when a mission is enabled) |

The context writes made before a checkpoint are journaled before it, so the journal's order matches what the checkpoint contains.

---

## Recovery on start

With `recover_on_start=True` (the default), the **first** `start()` of a process object reads the journal for its name and restores what it recorded, before its own first transition is journaled:

- the context state: the last checkpoint's `context_state` plus the `context_update` entries written after it (replayed with [`ReplayEngine`](replay.md)); recovered keys replace the same keys of `context.initial_state`;
- the mission (`MissionTracker.restore`), when a mission is enabled;
- the budget counters, when a budget is enabled, so a restart can't reset the daily limits;
- the conversation history, when `context.conversation_history` is on;
- the invocation count.

Without a checkpoint (a crash before the first run ended), the `context_update` entries alone are replayed. A journal that holds neither restores nothing, and a journal that can't be read is logged and the process starts fresh.

`process.recovered` reports what was restored (or is `None`): `lifecycle_state` (`"stopped"` after a clean stop, `"running"` after a crash), `entries_replayed`, `context_keys`, `mission_restored`, `budget_restored`, `conversation_messages` and `invocation_count`. The same dict is journaled as a `recovered` entry and passed to `SESSION_START` [hooks](../hooks.md) as `recovered`.

Not recovered from the journal: long-term memory (its provider persists it), secrets, queued trigger events and dead letters, and anything a tool did outside the process. Later starts of the same object (an automatic restart, `restart_process`) keep the live state and don't read the journal again. Set `recover_on_start=False` to always start fresh.

---

## Secrets never reach the journal in plain text

When [secret scoping](../governance/secrets.md) is enabled, the process wraps its journal in a `RedactingJournal`. Every string in an entry's `data` and in a checkpoint passes through the secret scope's redactor before it reaches the backend, so tool arguments, run results and the conversation snapshot store `[REDACTED]` instead of a secret value, also after a rotation. `get_secret` results are never journaled at all.

To redact your own journal the same way:

```python
from promptise.runtime.journal import FileJournal, RedactingJournal

journal = RedactingJournal(FileJournal(".promptise/journal"), scope.sanitize)
```

The redactor must return a redacted copy and leave its input unchanged. Values that were stored redacted come back redacted on recovery and rewind.

---

## Reading entries

| From | How |
|---|---|
| The terminal | `promptise runtime logs notes` (newest entries, with their entry IDs; `--follow` for more), see [CLI](../cli.md) |
| The Orchestration API | `GET /api/v1/processes/{name}/journal?limit=50&type=checkpoint`; `OrchestrationClient.get_journal(name, limit=, entry_type=)` |
| Python | `await journal.read(name, since=..., entry_type=..., limit=...)` on a backend |

```python
from datetime import datetime, timezone
from promptise.runtime.journal import FileJournal

journal = FileJournal(".promptise/journal")
entries = await journal.read("notes")
errors = await journal.read("notes", entry_type="error",
                            since=datetime(2026, 10, 1, tzinfo=timezone.utc))
checkpoint = await journal.last_checkpoint("notes")
```

Backends implement the `JournalProvider` protocol: `append(entry)`, `read(process_id, *, since, entry_type, limit)`, `checkpoint(process_id, state)`, `last_checkpoint(process_id)` and `close()`. See [Journal Backends](backends.md).

---

## Common pitfalls

- **Expecting a journal by default.** A `ProcessConfig` without `journal=` writes nothing, so `logs`, recovery and rewind have nothing to work with. Set `JournalConfig()` (checkpoint level).
- **Two processes with the same name and path.** Entries are filed under the name, so they share one file and recover each other's state. Give each process its own name or `path`.
- **A relative `path` and a different working directory.** `.promptise/journal` is relative to where the process starts. A process started from another directory finds no journal and starts fresh; use an absolute path in services.
- **`backend: memory` and recovery.** The in-memory journal disappears with the Python process, so nothing survives a crash. Use it for tests.
- **`recover_on_start` and a fresh run.** A test or a new deployment that should start clean must set `recover_on_start=False` or use a new journal directory; otherwise it continues the old state.
- **`full` level and storage.** Tool results and payloads can be large, and JSONL files are never rotated by Promptise. Rotate or archive them externally.

---

## What's Next

- [Journal Backends](backends.md) -- `FileJournal`, `InMemoryJournal` and `RedactingJournal`
- [Replay Engine](replay.md) -- how recovery rebuilds state from the journal
- [Rewind](rewind.md) -- roll a process back to an earlier entry
- [Configuration](../configuration.md) -- `JournalConfig` among the other options
- [CLI Commands](../cli.md) -- `promptise runtime logs` and `promptise runtime rewind`

!!! tip "Learn the concept"

    - [Durable AI Agents: Crash Recovery and Safe Resume](https://www.promptise.com/guides/view/durable-ai-agent-crash-recovery) -- Journal progress and resume so side effects never run twice.
    - [AI Agent State Between Runs: Context, Keys and Audit](https://www.promptise.com/guides/view/ai-agent-state-between-runs) -- What a process remembers between runs and how it is recorded.
