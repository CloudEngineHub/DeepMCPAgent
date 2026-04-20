# File Watch Trigger

The `FileWatchTrigger` monitors a directory for file changes and fires trigger events when files are created, modified, deleted, or moved. It uses `watchdog` for native OS filesystem notifications when available, with an automatic polling fallback.

```python
from promptise.runtime.triggers.file_watch import FileWatchTrigger

trigger = FileWatchTrigger(
    watch_path="/data/inbox",
    patterns=["*.csv", "*.json"],
)
await trigger.start()

event = await trigger.wait_for_next()
print(event.payload)
# {"path": "/data/inbox/new_data.csv", "filename": "new_data.csv", "event_type": "created"}

await trigger.stop()
```

---

## Concepts

The `FileWatchTrigger` bridges the filesystem and the agent runtime. When files appear in or change within a watched directory, the trigger produces `TriggerEvent` objects that wake the agent. This is ideal for data ingestion pipelines, file-based workflows, and monitoring drop folders.

Two backends are supported:

- **Watchdog** (recommended) -- uses native OS filesystem notifications (inotify on Linux, FSEvents on macOS, ReadDirectoryChangesW on Windows). Install with `pip install watchdog`.
- **Polling fallback** -- scans the directory at regular intervals, comparing file modification times. Works everywhere but uses more CPU and has higher latency.

---

## Configuration

### Via TriggerConfig

```python
from promptise.runtime import ProcessConfig, TriggerConfig

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Process new data files as they arrive.",
    triggers=[
        TriggerConfig(
            type="file_watch",
            watch_path="/data/inbox",
            watch_patterns=["*.csv", "*.json"],
            watch_events=["created", "modified"],
        ),
    ],
)
```

### Direct instantiation

```python
from promptise.runtime.triggers.file_watch import FileWatchTrigger

trigger = FileWatchTrigger(
    watch_path="/data/inbox",
    patterns=["*.csv", "*.json"],
    events=["created", "modified", "deleted"],
    recursive=True,
    debounce_seconds=0.5,
    poll_interval=1.0,
)
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `watch_path` | `str` | required | Directory to monitor |
| `patterns` | `list[str]` | `["*"]` | Glob patterns to match filenames |
| `events` | `list[str]` | `["created", "modified"]` | Filesystem events to react to |
| `recursive` | `bool` | `True` | Watch subdirectories |
| `debounce_seconds` | `float` | `0.5` | Debounce interval to avoid duplicate events |
| `poll_interval` | `float` | `1.0` | Polling interval in seconds (fallback only) |

---

## Supported Events

| Event | Description |
|---|---|
| `created` | A new file was created in the watched directory |
| `modified` | An existing file's content was changed |
| `deleted` | A file was removed |
| `moved` | A file was moved or renamed (watchdog backend only) |

Configure which events to react to via the `events` parameter or `watch_events` in `TriggerConfig`.

---

## Pattern Matching

Patterns use standard glob syntax and are matched against the **filename** (not the full path):

```python
# Match CSV and JSON files
patterns=["*.csv", "*.json"]

# Match all Python files
patterns=["*.py"]

# Match everything (default)
patterns=["*"]

# Match specific prefixes
patterns=["report_*.xlsx"]
```

---

## Event Payload

When the trigger fires, the `TriggerEvent.payload` contains:

| Field | Description |
|---|---|
| `path` | Full filesystem path of the changed file |
| `filename` | Just the filename (basename) |
| `event_type` | `"created"`, `"modified"`, `"deleted"`, or `"moved"` |

The `metadata` includes:

| Field | Description |
|---|---|
| `watch_path` | The configured watch directory |
| `patterns` | The configured glob patterns |

Example:

```python
event = await trigger.wait_for_next()
print(event.payload)
# {
#     "path": "/data/inbox/report_2026.csv",
#     "filename": "report_2026.csv",
#     "event_type": "created",
# }
print(event.metadata)
# {
#     "watch_path": "/data/inbox",
#     "patterns": ["*.csv", "*.json"],
# }
```

---

## Debouncing

Many filesystem operations generate multiple events for a single logical change (e.g., a file write triggers both `created` and `modified`). The `debounce_seconds` parameter suppresses duplicate events for the same file and event type within the debounce window.

```python
# Suppress duplicates within 1 second
trigger = FileWatchTrigger(
    watch_path="/data/inbox",
    debounce_seconds=1.0,
)
```

The deduplication cache is garbage-collected automatically.

---

## Directory Creation

If the `watch_path` does not exist when `start()` is called, the trigger creates it automatically:

```python
trigger = FileWatchTrigger(watch_path="/data/new_inbox")
await trigger.start()  # Creates /data/new_inbox if missing
```

---

## Backend Selection

The backend is selected automatically based on available dependencies:

```python
# With watchdog installed
trigger = FileWatchTrigger(watch_path="/data/inbox")
print(trigger)
# FileWatchTrigger(path='/data/inbox', patterns=['*'], backend='watchdog')

# Without watchdog
# FileWatchTrigger(path='/data/inbox', patterns=['*'], backend='polling')
```

### Watchdog backend

- Uses native OS notifications for near-instant detection.
- Handles `created`, `modified`, `deleted`, and `moved` events.
- Runs an `Observer` thread that dispatches events to the async queue via `call_soon_threadsafe`.

### Polling backend

- Scans the directory at `poll_interval` intervals.
- Compares file modification times to detect changes.
- Detects `created`, `modified`, and `deleted` events (not `moved`).
- Uses `asyncio.wait_for` with a stop event for cancellable sleeping.

---

## Lifecycle

```python
trigger = FileWatchTrigger(
    watch_path="/data/inbox",
    patterns=["*.csv"],
)

await trigger.start()

# Process events in a loop
while True:
    try:
        event = await trigger.wait_for_next()
        print(f"File: {event.payload['filename']} ({event.payload['event_type']})")
    except asyncio.CancelledError:
        break

await trigger.stop()
```

When `stop()` is called:

1. The watchdog observer is stopped and joined (or the polling task is cancelled).
2. A sentinel event is enqueued to unblock any waiting `wait_for_next()`.
3. The sentinel causes `wait_for_next()` to raise `asyncio.CancelledError`.

---

## API Summary

| Method / Property | Description |
|---|---|
| `FileWatchTrigger(watch_path, patterns, events, recursive, debounce_seconds, poll_interval)` | Create a file watch trigger |
| `trigger_id` | Unique identifier: `file_watch-{path}` |
| `await start()` | Start watching (creates directory if needed) |
| `await stop()` | Stop watching and release resources |
| `await wait_for_next()` | Block until a matching file change occurs |

---

## Tips and Gotchas

!!! tip "watchdog is already installed"
    Polling works but introduces latency equal to the `poll_interval`. Native filesystem notifications via `watchdog` detect changes nearly instantly. `watchdog` ships with the base `pip install promptise`.

!!! tip "Narrow your patterns"
    Use specific glob patterns to avoid processing temporary files, swap files, and other noise. For example, `["*.csv"]` is better than `["*"]` for a data ingestion pipeline.

!!! tip "Increase debounce for noisy directories"
    Some tools write files in multiple steps (create, write, flush). Increase `debounce_seconds` to 1.0 or higher to coalesce these into a single event.

!!! warning "Recursive watching can be expensive"
    Watching a large directory tree recursively may consume significant resources, especially with the polling backend. Monitor the queue size and consider watching specific subdirectories instead.

!!! warning "moved events are watchdog-only"
    The polling backend detects moves as a `deleted` event for the old path and a `created` event for the new path. Only the watchdog backend produces a single `moved` event.

!!! warning "Queue overflow"
    The file watch queue has a capacity of 1000 events. In directories with very high file churn, events may be dropped. Consider increasing `debounce_seconds` or narrowing `patterns` to reduce event volume.

---

## What's Next

- [Triggers Overview](index.md) -- all trigger types and the base protocol
- [Cron Trigger](cron.md) -- time-based scheduling
- [Event and Webhook Triggers](event-webhook.md) -- event-driven activation
