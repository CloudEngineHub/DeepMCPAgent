# Journal Backends

The journal system ships with two backends: `FileJournal` for production use with persistent append-only JSONL files, and `InMemoryJournal` for testing and development.

```python
from promptise.runtime.journal import FileJournal, InMemoryJournal, JournalEntry

# Production: file-based persistence
journal = FileJournal(base_path=".promptise/journal")

# Testing: in-memory, no persistence
journal = InMemoryJournal()

# Both implement the same JournalProvider protocol
await journal.append(JournalEntry(
    process_id="my-process",
    entry_type="state_transition",
    data={"from_state": "created", "to_state": "running"},
))
entries = await journal.read("my-process")
```

---

## FileJournal

The `FileJournal` stores journal entries as append-only JSONL (JSON Lines) files on disk, one file per process. Checkpoints are stored in separate JSON files.

### File layout

```
.promptise/journal/
  my-process.jsonl              # Journal entries (one JSON object per line)
  my-process.checkpoint.json    # Latest checkpoint state
  another-process.jsonl
  another-process.checkpoint.json
```

### Creating a FileJournal

```python
from promptise.runtime.journal import FileJournal

# Default path
journal = FileJournal()  # .promptise/journal/

# Custom path
journal = FileJournal(base_path="/var/log/agents/journal")
```

The `base_path` directory is created automatically if it does not exist.

### Appending entries

```python
from promptise.runtime.journal import JournalEntry

await journal.append(JournalEntry(
    process_id="data-watcher",
    entry_type="trigger_event",
    data={"trigger_type": "cron", "scheduled_time": "2026-03-04T10:05:00"},
))
```

Each entry is serialized to a single JSON line and appended to the process's JSONL file. File handles are opened and closed per operation -- no persistent handles are held.

### Reading entries

```python
# Read all entries
entries = await journal.read("data-watcher")

# Filter by time
from datetime import datetime, UTC
entries = await journal.read(
    "data-watcher",
    since=datetime(2026, 3, 4, 10, 0, tzinfo=UTC),
)

# Filter by entry type
entries = await journal.read(
    "data-watcher",
    entry_type="state_transition",
)

# Limit results
entries = await journal.read("data-watcher", limit=20)

# Combine filters
entries = await journal.read(
    "data-watcher",
    since=datetime(2026, 3, 4, tzinfo=UTC),
    entry_type="trigger_event",
    limit=50,
)
```

Malformed lines in the JSONL file are skipped with a warning, allowing the journal to tolerate partial writes from crashes.

### Checkpointing

```python
# Store a checkpoint
await journal.checkpoint("data-watcher", {
    "context_state": {"pipeline_status": "healthy", "check_count": 42},
    "lifecycle_state": "running",
})

# Read the last checkpoint
checkpoint = await journal.last_checkpoint("data-watcher")
# {"context_state": {...}, "lifecycle_state": "running"}
```

The checkpoint is written as a complete JSON file (overwriting the previous checkpoint) and also appended to the journal as an entry with `entry_type="checkpoint"`.

### Error handling

File I/O errors raise `JournalError`:

```python
from promptise.runtime.exceptions import JournalError

try:
    await journal.append(entry)
except JournalError as e:
    print(f"Failed to write: {e}")
```

### Closing

`FileJournal.close()` is a no-op since file handles are not held open. It is provided for protocol compatibility.

---

## InMemoryJournal

The `InMemoryJournal` stores all entries in a Python list. Nothing is persisted to disk. This backend is intended for testing and development.

### Creating an InMemoryJournal

```python
from promptise.runtime.journal import InMemoryJournal

journal = InMemoryJournal()
```

### Usage

The API is identical to `FileJournal`:

```python
from promptise.runtime.journal import JournalEntry

# Append
await journal.append(JournalEntry(
    process_id="test-process",
    entry_type="test_event",
    data={"key": "value"},
))

# Read
entries = await journal.read("test-process")
assert len(entries) == 1

# Checkpoint
await journal.checkpoint("test-process", {"state": "running"})
checkpoint = await journal.last_checkpoint("test-process")
assert checkpoint == {"state": "running"}

# Close (no-op)
await journal.close()
```

### Internal access (testing)

For test assertions, the internal state is accessible:

```python
# All entries
journal._entries  # list[JournalEntry]

# All checkpoints
journal._checkpoints  # dict[str, dict[str, Any]]
```

---

## Comparing Backends

| Feature | FileJournal | InMemoryJournal |
|---|---|---|
| Persistence | Append-only JSONL files | None (lost on process exit) |
| Crash recovery | Supports replay from disk | Not applicable |
| Performance | Disk I/O per operation | Instant (memory) |
| Checkpoint storage | Separate `.checkpoint.json` file | In-memory dict |
| Malformed entry handling | Skips with warning | Not applicable |
| Concurrent safety | File-level (OS) | Single-process |
| Close behavior | No-op | No-op |

---

## API Summary

### FileJournal

| Method | Description |
|---|---|
| `FileJournal(base_path)` | Create with base directory (default: `.promptise/journal`) |
| `await append(entry)` | Append a `JournalEntry` to the JSONL file |
| `await read(process_id, since, entry_type, limit)` | Read entries with optional filters |
| `await checkpoint(process_id, state)` | Store a checkpoint and record in journal |
| `await last_checkpoint(process_id)` | Read the last checkpoint, or `None` |
| `await close()` | No-op (protocol compatibility) |

### InMemoryJournal

| Method | Description |
|---|---|
| `InMemoryJournal()` | Create an empty in-memory journal |
| `await append(entry)` | Append to internal list |
| `await read(process_id, since, entry_type, limit)` | Read with filters |
| `await checkpoint(process_id, state)` | Store checkpoint and record entry |
| `await last_checkpoint(process_id)` | Read the last checkpoint, or `None` |
| `await close()` | No-op |

---

## Tips and Gotchas

!!! tip "Use FileJournal in production"
    Even for non-critical processes, the file journal provides valuable observability. The JSONL format is human-readable and can be processed with standard tools like `jq`.

!!! tip "Process ID sanitization"
    `FileJournal` sanitizes process IDs for filesystem safety: forward slashes and backslashes are replaced with underscores in filenames.

!!! tip "View journal files directly"
    JSONL files can be inspected with standard tools:
    ```bash
    # View last 10 entries
    tail -n 10 .promptise/journal/data-watcher.jsonl | python -m json.tool

    # Filter by entry type
    jq 'select(.entry_type == "trigger_event")' .promptise/journal/data-watcher.jsonl
    ```

!!! warning "Checkpoints overwrite"
    Each `checkpoint()` call overwrites the previous checkpoint file. Only the most recent checkpoint is available via `last_checkpoint()`. The full checkpoint history is preserved in the JSONL journal entries.

!!! warning "No entry deletion"
    Neither backend supports deleting individual entries. JSONL files grow indefinitely. Implement log rotation externally for long-running processes.

---

## What's Next

- [Journal Overview](index.md) -- journal concepts and entry types
- [Replay Engine](replay.md) -- crash recovery from journal entries
- [Configuration](../configuration.md) -- `JournalConfig` reference
