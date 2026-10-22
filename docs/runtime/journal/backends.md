---
seo_title: "Agent Journal Backends: File and Memory"
description: "Store agent process journals in FileJournal JSONL files or InMemoryJournal for tests, strip secrets with RedactingJournal, or write your own journal backend."
---

# Journal Backends

A journal backend stores a process's entries and its latest checkpoint. Promptise ships `FileJournal` (append-only JSONL files, for anything that must survive a restart) and `InMemoryJournal` (a Python list, for tests), plus `RedactingJournal`, a wrapper that removes secret values before either one writes. A process picks its backend from `JournalConfig.backend`; you use the classes directly to read a journal, write tools around it, or test.

```python
from promptise.runtime.journal import FileJournal, InMemoryJournal, JournalEntry

journal = FileJournal(".promptise/journal")   # or InMemoryJournal()

await journal.append(JournalEntry(
    process_id="notes",
    entry_type="state_transition",
    data={"from_state": "created", "to_state": "starting"},
))
await journal.checkpoint("notes", {"context_state": {"stage": "draft"}, "lifecycle_state": "running"})

entries = await journal.read("notes")          # both entries, oldest first
latest = await journal.last_checkpoint("notes")
```

Both backends implement the same `JournalProvider` protocol (`append`, `read`, `checkpoint`, `last_checkpoint`, `close`), so code written against one works with the other.

---

## FileJournal

`FileJournal(base_path=".promptise/journal")` keeps one JSONL file per process and a separate checkpoint file. The directory is created if it is missing.

```
.promptise/journal/
  notes.jsonl              # every entry, one JSON object per line
  notes.checkpoint.json    # the latest checkpoint
  watcher.jsonl
  watcher.checkpoint.json
```

| Behavior | Details |
|---|---|
| Append | Each entry is one JSON line (`default=str` for values JSON can't encode). The file is opened and closed per write; no handle stays open |
| Read | Oldest first. `since` skips entries older than it, `entry_type` keeps one type, `limit` stops after that many matches. A malformed line (a write cut off by a crash) is skipped with a warning |
| Checkpoint | Written to a temporary file, flushed with `fsync`, then renamed over the old one, so a crash mid-write leaves the previous checkpoint intact. Also appended to the JSONL file as a `checkpoint` entry with `data={"state": state}` |
| Last checkpoint | Read from the `.checkpoint.json` file; `None` when it is missing or unreadable (logged) |
| File names | `/` and `\` in the process ID become `_`, IDs are cut to 200 characters, and a null byte raises `ValueError` |
| Errors | A failed write or read raises `JournalError` (`promptise.runtime.exceptions`) |
| `close()` | Does nothing (no handles are held) |

```python
from datetime import datetime, timezone
from promptise.runtime.exceptions import JournalError

recent = await journal.read(
    "notes",
    since=datetime(2026, 10, 19, tzinfo=timezone.utc),
    entry_type="invocation_result",
    limit=20,
)

try:
    await journal.append(entry)
except JournalError as exc:
    print(f"Journal write failed: {exc}")
```

An `AgentProcess` catches journal errors itself: they are logged and the process keeps running.

The JSONL files work with standard tools:

```bash
tail -n 5 .promptise/journal/notes.jsonl | python -m json.tool --json-lines
jq -c 'select(.entry_type == "error") | .data' .promptise/journal/notes.jsonl
```

---

## InMemoryJournal

`InMemoryJournal()` keeps entries in a list and checkpoints in a dict. Nothing is persisted, so it is for tests and short experiments; `JournalConfig(backend="memory")` gives a process one.

```python
from promptise.runtime.journal import InMemoryJournal, JournalEntry

journal = InMemoryJournal()
await journal.append(JournalEntry(process_id="test", entry_type="test_event", data={"k": "v"}))
assert len(await journal.read("test")) == 1

await journal.checkpoint("test", {"lifecycle_state": "running"})
assert await journal.last_checkpoint("test") == {"lifecycle_state": "running"}
```

`checkpoint()` stores a copy of the state, and the `checkpoint` entry it appends holds its own copy, so changing your dict afterwards changes neither. In tests, `journal._entries` (a `list[JournalEntry]`) and `journal._checkpoints` (a `dict`) expose the raw contents.

---

## RedactingJournal

`RedactingJournal(inner, redact)` wraps any backend. Every entry's `data` and every checkpoint state passes through `redact` before `inner` stores it; reads go straight to `inner`.

```python
from promptise.runtime.journal import FileJournal, RedactingJournal

journal = RedactingJournal(FileJournal(".promptise/journal"), scope.sanitize)
await journal.append(JournalEntry(process_id="bot", entry_type="note",
                                  data={"text": f"token is {token}"}))
# stored as {"text": "token is [REDACTED]"}
```

`redact` takes a (nested) value and returns a redacted copy; it must not modify its input. `SecretScope.sanitize` is the usual choice: it knows the current and rotated secret values. An `AgentProcess` with secret scoping enabled wraps its journal this way automatically. `journal.inner` is the wrapped backend.

---

## Comparing backends

| | FileJournal | InMemoryJournal |
|---|---|---|
| Survives a restart | Yes | No |
| Recovery and rewind across Python processes | Yes | Only within the same Python process |
| Checkpoint storage | `.checkpoint.json`, replaced atomically | A dict |
| Malformed data | Skipped with a warning | Not applicable |
| Readable by other programs | Yes (JSONL) | No |
| `close()` | No-op | No-op |

---

## Writing your own backend

Implement the `JournalProvider` protocol:

```python
from datetime import datetime
from typing import Any
from promptise.runtime.journal import JournalEntry

class PostgresJournal:
    async def append(self, entry: JournalEntry) -> None: ...
    async def read(self, process_id: str, *, since: datetime | None = None,
                   entry_type: str | None = None, limit: int | None = None) -> list[JournalEntry]: ...
    async def checkpoint(self, process_id: str, state: dict[str, Any]) -> None: ...
    async def last_checkpoint(self, process_id: str) -> dict[str, Any] | None: ...
    async def close(self) -> None: ...
```

Keep two rules the built-in backends follow, because recovery and rewind depend on them: `read()` returns entries oldest first, and `checkpoint()` also appends a `checkpoint` entry whose `data` is `{"state": state}`. `ProcessConfig.journal` only selects the built-in backends; to give a process your own, pass it to the code that reads it (`ReplayEngine`, `RewindEngine`).

---

## Common pitfalls

- **Unbounded files.** JSONL files only grow. Rotate or archive them outside Promptise for long-running processes; keep the newest file and the `.checkpoint.json` together.
- **Deleting only the JSONL file.** Recovery reads the `.checkpoint.json` too. To reset a process, delete both files (or point `path` at a new directory).
- **Sharing a directory between processes with the same name.** Files are named after the process, so they collide.
- **Assuming `limit` returns the newest entries.** `read(limit=n)` returns the *first* n matches; take `entries[-n:]` for the newest (the API and `promptise runtime logs` do).

---

## What's Next

- [Journal Overview](index.md): entry types, levels and recovery on start
- [Replay Engine](replay.md): how recovery rebuilds state
- [Rewind](rewind.md): roll a process back to an earlier entry
- [Configuration](../configuration.md): `JournalConfig` reference

!!! tip "Learn the concept"

    - [Durable AI Agents: Crash Recovery and Safe Resume](https://www.promptise.com/guides/view/durable-ai-agent-crash-recovery): Journal progress and resume so side effects never run twice.
