---
seo_title: "File Watch Trigger: Run Agents on File Changes"
description: "Run an agent when files are created, modified, deleted or moved: glob patterns, debouncing, back-pressure and watchdog or polling backends for network shares."
---

# File Watch Trigger

The `FileWatchTrigger` runs an agent when files in a directory are created, modified, deleted or moved. Use it for drop folders, data ingestion and file-based workflows. It uses native OS notifications through `watchdog`, or scans the directory on an interval when you choose polling (network shares, bind mounts).

```python
from promptise.runtime import ProcessConfig, TriggerConfig

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Summarise each new CSV file in /data/inbox.",
    triggers=[
        TriggerConfig(
            type="file_watch",
            watch_path="/data/inbox",
            watch_patterns=["*.csv"],
        ),
    ],
)
```

On its own, outside a process:

```python
from promptise.runtime.triggers.file_watch import FileWatchTrigger

trigger = FileWatchTrigger(watch_path="/data/inbox", patterns=["*.csv", "*.json"])
await trigger.start()

event = await trigger.wait_for_next()
print(event.payload)
# {"path": "/data/inbox/new_data.csv", "filename": "new_data.csv",
#  "event_type": "created", "event_types": ["created", "modified"]}

await trigger.stop()
```

---

## Concepts

When files appear in or change within a watched directory, the trigger produces `TriggerEvent` objects that wake the agent. The payload carries the path, not the content: the agent reads the file with its own tools.

Two backends detect changes:

- **watchdog** -- native OS notifications (inotify on Linux, FSEvents on macOS, ReadDirectoryChangesW on Windows), near-instant. `watchdog` ships with `pip install promptise`.
- **polling** -- scans the directory every `watch_poll_interval` seconds and compares each file's modification time and size. Works everywhere, including network shares and container bind mounts where OS notifications are unreliable, at the cost of latency and CPU.

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
            watch_path="/mnt/share/inbox",
            watch_patterns=["*.csv", "*.json"],
            watch_events=["created", "modified", "moved"],
            watch_debounce_seconds=1.0,
            watch_recursive=False,
            watch_backend="polling",     # network share: don't rely on OS notifications
            watch_poll_interval=5.0,
            watch_max_pending_events=5000,
        ),
    ],
)
```

| Field | Default | Description |
|---|---|---|
| `watch_path` | required | Directory to monitor (created at start if missing) |
| `watch_patterns` | `["*"]` | Glob patterns matched against the file name |
| `watch_events` | `["created", "modified"]` | Which (merged) events run the agent: any of `created`, `modified`, `deleted`, `moved`; at least one |
| `watch_debounce_seconds` | `0.5` | Window in which events for the same file are merged into one (`>= 0`) |
| `watch_recursive` | `True` | Also watch subdirectories |
| `watch_backend` | `"auto"` | `"auto"` (watchdog when installed, otherwise polling), `"watchdog"` (fail if it isn't installed) or `"polling"` |
| `watch_poll_interval` | `1.0` | Seconds between scans with the polling backend (`> 0`, at most 3600) |
| `watch_max_pending_events` | `10000` | How many changed files may wait for the process before new changes are dropped (`>= 1`); see [Back-pressure](#back-pressure) |
| `filter_expression` | `None` | Extra filter on the event (see [Filtering](index.md#filtering-events-before-the-agent-runs)) |

Unknown event names, an unknown backend, `watch_backend="watchdog"` without `watchdog` installed, and out-of-range numbers are rejected when the config is created. Setting a `watch_*` option on another trigger type is a validation error too.

Inside a process, the trigger's ID is a [stable ID](index.md#trigger-ids) such as `file_watch-06bfa13759`.

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
    backend="auto",
    max_pending_events=10_000,
)
```

| Parameter | Type | Default | Description |
|---|---|---|---|
| `watch_path` | `str` | required | Directory to monitor |
| `patterns` | `list[str]` | `["*"]` | Glob patterns to match file names |
| `events` | `list[str]` | `["created", "modified"]` | Merged events that fire the trigger |
| `recursive` | `bool` | `True` | Watch subdirectories |
| `debounce_seconds` | `float` | `0.5` | Window in which events for one file are merged |
| `poll_interval` | `float` | `1.0` | Seconds between scans (polling backend) |
| `backend` | `str` | `"auto"` | Keyword-only: `"auto"`, `"watchdog"` or `"polling"` |
| `max_pending_events` | `int` | `10000` | Keyword-only: most files waiting before changes are dropped |

The constructor raises `ValueError` for an unknown event type or backend, `backend="watchdog"` without `watchdog`, a `poll_interval` that isn't positive, a negative `debounce_seconds`, or a `max_pending_events` below 1.

---

## Supported Events

| Event | Description |
|---|---|
| `created` | A new file was created in the watched directory |
| `modified` | An existing file's content was changed |
| `deleted` | A file was removed |
| `moved` | A file was moved or renamed into place (watchdog backend only) |

Configure which events to react to via `events` / `watch_events`. The check runs on the **merged** event for each file (see [Debouncing](#debouncing)), so `watch_events=["deleted"]` fires only when a file is gone, never for a new or changed one.

Editors and many upload tools save atomically: they write a temporary file and rename it over the target. With the watchdog backend that shows up as `moved` (to the target name) rather than `created` or `modified`, so the default `watch_events` miss it. Add `"moved"` to `watch_events` if files arrive that way. The polling backend sees a rename as a `deleted` event for the old name and a `created` event for the new one.

Directory events are ignored; only files fire the trigger.

---

## Pattern Matching

Patterns use standard glob syntax and are matched against the **file name** (not the full path):

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
| `path` | Full file-system path of the changed file |
| `filename` | Just the file name (basename) |
| `event_type` | The merged change: `"created"`, `"modified"`, `"deleted"`, or `"moved"` |
| `event_types` | The raw events merged into this one, in arrival order (e.g. `["created", "modified"]`) |

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
#     "event_types": ["created", "modified"],
# }
print(event.metadata)
# {
#     "watch_path": "/data/inbox",
#     "patterns": ["*.csv", "*.json"],
# }
```

---

## Debouncing

The operating system often reports one logical change as several events: writing a new file usually produces `created` **and** `modified`, and copying a large file can produce several `modified`. Without merging, the agent would run once per raw event.

The trigger therefore collects every event for the same path during a `debounce_seconds` window (starting with the first event) and then emits **one** trigger event describing the net change:

| What happened in the window | `event_type` |
|---|---|
| File no longer exists | `deleted` (nothing at all if it was also created in the window) |
| File was created (or deleted and recreated) | `created` |
| File was moved/renamed into place | `moved` |
| Anything else | `modified` |

That merged type is then checked against `events` / `watch_events`. One `write_text()` to a new file therefore runs the agent once, with `event_type="created"` and `event_types=["created", "modified"]`.

```python
# Merge everything that happens to a file within 1 second
trigger = FileWatchTrigger(
    watch_path="/data/inbox",
    debounce_seconds=1.0,
)
```

A larger window coalesces tools that write in several steps; the trade-off is that events arrive that much later. The window is fixed: it does not wait for the file to stop changing, so a slow upload can still be half-written when the agent runs.

---

## Back-pressure

Changes are not dropped when the process is busy. The trigger's own queue holds up to 1000 events; once it is full, further changes wait in a per-file backlog and move to the queue, oldest first, as the process takes events. A second change to a file that is already waiting is merged into its event, so a busy file costs one slot, not one per write.

Only when `watch_max_pending_events` **different** files are waiting (queued plus backlog) is a change to a new file dropped. Each drop is counted, and the first drop and every hundredth after it are logged as errors:

```text
FileWatchTrigger file_watch-06bfa13759: 5000 files are waiting for the process
(max_pending_events=5000); dropped the change to /data/inbox/f5001.csv (1 dropped so far).
Raise watch_max_pending_events or process events faster.
```

The counters are on the trigger and in the process status:

| Counter | Meaning |
|---|---|
| `pending_events` | Events waiting for the process (queued plus backlog) |
| `dropped_events` | Changes dropped because `max_pending_events` files were waiting |

```python
process.status()["triggers"]
# [{'id': 'file_watch-06bfa13759', 'type': 'FileWatchTrigger', 'source': 'config',
#   'dropped_events': 0, 'pending_events': 12}]
```

A suspended process keeps the backlog; the events run after `resume()`. A process suspended by `idle_timeout` resumes by itself when a file event arrives.

---

## Directory Creation

If the `watch_path` does not exist when `start()` is called, the trigger creates it (with parent directories):

```python
trigger = FileWatchTrigger(watch_path="/data/new_inbox")
await trigger.start()  # Creates /data/new_inbox if missing
```

---

## Backend Selection

`backend` / `watch_backend` picks how changes are detected; `trigger.backend` reports the one in use:

```python
trigger = FileWatchTrigger(watch_path="/data/inbox")
print(trigger.backend)  # "watchdog" (or "polling" when watchdog isn't installed)
print(trigger)
# FileWatchTrigger(path='/data/inbox', patterns=['*'], events=['created', 'modified'],
#                  backend='watchdog', recursive=True)
```

### Watchdog backend

- Uses native OS notifications for near-instant detection.
- Handles `created`, `modified`, `deleted`, and `moved` events.
- Runs an `Observer` thread that hands events to the event loop with `call_soon_threadsafe`.

### Polling backend

- Takes a snapshot of the directory at `start()`, then scans every `poll_interval` seconds.
- Compares each file's modification time (nanoseconds) and size, so a rewrite inside the file system's timestamp resolution is still seen.
- Detects `created`, `modified`, and `deleted` events (not `moved`).
- Use it for NFS/SMB shares and container bind mounts, where native notifications may not arrive.

Neither backend reports files that already exist when the trigger starts. Process those at start-up yourself (for example with a one-off `process.inject()` per file).

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
2. Changes still waiting (the backlog and open debounce windows) are turned into events that `drain_pending()` returns. In a process, they are dead-lettered with reason `process stopped`, or kept for the next run when the process restarts.
3. A sentinel event is enqueued; it makes `wait_for_next()` raise `asyncio.CancelledError`.

In a process, add or remove a file watch while it runs with [`process.add_trigger()` / `process.remove_trigger()`](index.md#adding-and-removing-triggers-at-runtime). A `FILE_CHANGED` [hook](../hooks.md) sees every file event before it is queued and can drop it (counted in `filtered_count`).

---

## API Summary

| Method / Property | Description |
|---|---|
| `FileWatchTrigger(watch_path, patterns, events, recursive, debounce_seconds, poll_interval, *, backend, max_pending_events)` | Create a file watch trigger |
| `trigger_id` | Identifier (standalone default: `file_watch-{path}`; in a process: the stable config ID) |
| `backend` | The backend in use: `"watchdog"` or `"polling"` |
| `pending_events` | Events waiting for the consumer (queued plus backlog) |
| `dropped_events` | Changes dropped because `max_pending_events` files were waiting |
| `await start()` | Start watching (creates the directory if needed) |
| `await stop()` | Stop watching and release resources |
| `await wait_for_next()` | Block until a matching file change occurs |

---

## Common pitfalls

- **Atomic saves and uploads.** Files renamed into place arrive as `moved` with watchdog. Add `"moved"` to `watch_events`, or the agent never runs for them.
- **Half-written files.** The debounce window is fixed. For slow uploads, have the producer write to a temporary name that doesn't match `watch_patterns` and rename it when done, or raise `watch_debounce_seconds`.
- **The agent triggers itself.** An agent that writes output into the watched folder (or a subfolder, with `watch_recursive=True`) wakes itself up. Write elsewhere, narrow `watch_patterns`, or set `watch_recursive=False` and write to a subfolder.
- **Network shares and bind mounts.** Native notifications are often missing there. Use `watch_backend="polling"`.
- **Large trees with polling.** Every scan walks the whole tree. Watch a narrower directory, set `watch_recursive=False`, or raise `watch_poll_interval`.
- **A growing `pending_events`.** The process can't keep up. Raise `concurrency`, narrow `watch_patterns`, or raise `watch_max_pending_events`; watch `dropped_events` for losses.
- **Files that were there before start.** They don't fire the trigger.

!!! warning "File contents are untrusted input"
    The trigger passes only the path and file name, but an agent that then reads the file reads whatever someone dropped into the folder. Treat that content like a webhook body: gate side-effecting tools with approval (see [Trigger payloads are untrusted input](index.md#trigger-payloads-are-untrusted-input)).

---

## What's Next

- [Triggers Overview](index.md) -- stable trigger IDs, live trigger changes and the event queue
- [Cron Trigger](cron.md) -- time-based scheduling
- [Event and Webhook Triggers](event-webhook.md) -- event-driven activation
- [Hooks](../hooks.md) -- the `FILE_CHANGED` hook

!!! tip "Learn the concept"

    - [Event-Driven AI Agents: Webhook, Cron and File Triggers](https://www.promptise.com/guides/view/event-driven-ai-agents-webhook-cron-triggers) -- File-triggered agents alongside webhook and cron triggers.
    - [AI Agent File Watcher: Process New Files Automatically](https://www.promptise.com/guides/view/ai-agent-file-watcher) -- Debouncing, half-written files, archiving processed files and avoiding loops.
