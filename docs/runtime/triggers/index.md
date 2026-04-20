# Triggers Overview

Triggers are the activation mechanism for agent processes. They produce `TriggerEvent` objects that wake an `AgentProcess` and cause it to invoke its agent. The runtime ships with five trigger types covering time-based scheduling, HTTP webhooks, filesystem monitoring, inter-process events, and message broker topics.

```python
from promptise.runtime import ProcessConfig, TriggerConfig

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="You respond to events and scheduled tasks.",
    triggers=[
        TriggerConfig(type="cron", cron_expression="*/5 * * * *"),
        TriggerConfig(type="webhook", webhook_path="/events", webhook_port=9090),
        TriggerConfig(type="file_watch", watch_path="/data/inbox", watch_patterns=["*.csv"]),
    ],
)
```

---

## Concepts

Every trigger implements the `BaseTrigger` protocol -- a simple contract with three methods:

1. **`start()`** -- begin listening for trigger conditions.
2. **`stop()`** -- stop the trigger and release resources.
3. **`wait_for_next()`** -- async method that blocks until the next event occurs and returns a `TriggerEvent`.

The `AgentProcess` runs a listener loop for each trigger: it calls `wait_for_next()`, receives a `TriggerEvent`, and enqueues it for processing. The event payload is then injected into the agent's context as part of the invocation.

---

## Available Trigger Types

| Type | Class | Description | Dependencies |
|---|---|---|---|
| `cron` | `CronTrigger` | Fires on a cron schedule | `croniter` (optional, for full expression support) |
| `webhook` | `WebhookTrigger` | HTTP endpoint that fires on POST requests | `aiohttp` |
| `file_watch` | `FileWatchTrigger` | Fires when files change on the filesystem | `watchdog` (optional, falls back to polling) |
| `event` | `EventTrigger` | Fires on EventBus events | None (uses framework EventBus) |
| `message` | `MessageTrigger` | Fires on MessageBroker messages | None (uses framework MessageBroker) |

---

## BaseTrigger Protocol

All trigger implementations must satisfy this protocol:

```python
from promptise.runtime.triggers.base import BaseTrigger

class BaseTrigger(Protocol):
    trigger_id: str

    async def start(self) -> None:
        """Start listening for trigger conditions."""
        ...

    async def stop(self) -> None:
        """Stop the trigger and release resources."""
        ...

    async def wait_for_next(self) -> TriggerEvent:
        """Block until the next trigger event occurs."""
        ...
```

The `trigger_id` is a unique string identifier auto-generated at construction (e.g., `cron-a1b2c3d4`, `webhook-9090/events`).

---

## TriggerEvent

When a trigger fires, it produces a `TriggerEvent` dataclass that carries metadata about what caused the firing:

```python
from promptise.runtime.triggers.base import TriggerEvent

event = TriggerEvent(
    trigger_id="cron-a1b2c3d4",
    trigger_type="cron",
    payload={"scheduled_time": "2026-03-04T10:05:00+00:00"},
    metadata={"cron_expression": "*/5 * * * *"},
)
```

| Field | Type | Description |
|---|---|---|
| `trigger_id` | `str` | Which trigger produced this event |
| `trigger_type` | `str` | Type of trigger (`cron`, `webhook`, `event`, etc.) |
| `event_id` | `str` | Unique event identifier (auto-generated UUID) |
| `timestamp` | `datetime` | When the event was produced (UTC) |
| `payload` | `dict[str, Any]` | Trigger-specific data |
| `metadata` | `dict[str, Any]` | Additional context |

### Payload by trigger type

| Trigger | Payload Contents |
|---|---|
| `cron` | `scheduled_time`, `cron_expression` |
| `webhook` | The POST request body (JSON or text) |
| `file_watch` | `path`, `filename`, `event_type` |
| `event` | `event_type`, `event_id`, `source`, `data` |
| `message` | `topic`, `message_id`, `sender`, `content` |

### Serialization

```python
data = event.to_dict()
restored = TriggerEvent.from_dict(data)
```

---

## Factory Function

The `create_trigger` factory creates a trigger from a `TriggerConfig`:

```python
from promptise.runtime.triggers import create_trigger
from promptise.runtime.config import TriggerConfig

config = TriggerConfig(type="cron", cron_expression="*/10 * * * *")
trigger = create_trigger(config)
```

For `event` triggers, pass the `event_bus` parameter. For `message` triggers, pass the `broker` parameter:

```python
trigger = create_trigger(config, event_bus=bus)
trigger = create_trigger(config, broker=broker)
```

The factory raises `TriggerError` if the trigger type is unknown or required dependencies are missing.

---

## Custom Trigger Types

The trigger system is extensible. You can implement your own trigger types and register them with the framework so they work seamlessly with `TriggerConfig` and `create_trigger()`.

### Implementing a custom trigger

Any class that satisfies the `BaseTrigger` protocol can be used as a trigger:

```python
import asyncio
from promptise.runtime.triggers.base import BaseTrigger, TriggerEvent


class SQSTrigger:
    """Trigger that fires when messages arrive on an AWS SQS queue."""

    def __init__(self, queue_url: str, *, trigger_id: str | None = None) -> None:
        self.trigger_id = trigger_id or f"sqs-{id(self):x}"
        self._queue_url = queue_url
        self._running = False
        self._task: asyncio.Task | None = None

    async def start(self) -> None:
        self._running = True

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()

    async def wait_for_next(self) -> TriggerEvent:
        # Poll SQS, return TriggerEvent when a message arrives
        ...
```

### Registering a custom trigger type

Use `register_trigger_type` to make your trigger available to `create_trigger()`:

```python
from promptise.runtime import register_trigger_type
from promptise.runtime.config import TriggerConfig


def sqs_factory(config, *, event_bus=None, broker=None):
    """Factory function for SQS triggers."""
    queue_url = config.custom_config["queue_url"]
    return SQSTrigger(queue_url)


# Register the type
register_trigger_type("sqs", sqs_factory)

# Now you can use it in TriggerConfig
config = TriggerConfig(
    type="sqs",
    custom_config={"queue_url": "https://sqs.us-east-1.amazonaws.com/123/my-queue"},
)
trigger = create_trigger(config)
```

The factory callable must accept `(config: TriggerConfig, *, event_bus=None, broker=None)` and return a `BaseTrigger` instance. Use `config.custom_config` to pass arbitrary parameters to your trigger.

### Registry API

| Function | Description |
|---|---|
| `register_trigger_type(name, factory)` | Register a custom trigger type |
| `register_trigger_type(name, factory, overwrite=True)` | Replace an existing registration |
| `unregister_trigger_type(name)` | Remove a registered type (no-op if unknown) |
| `registered_trigger_types()` | List all registered type names (built-in + custom) |

!!! tip "Use `custom_config` for trigger parameters"
    The `TriggerConfig.custom_config` field is a `dict[str, Any]` designed for custom trigger types. Put all your trigger-specific configuration there. Built-in types use their own dedicated fields (`cron_expression`, `webhook_path`, etc.).

!!! warning "Overwriting built-in types"
    You can replace built-in types with `overwrite=True`, but this affects all processes in the runtime. Only do this if you need to substitute the default implementation (e.g., a custom cron engine).

---

## Configuring Triggers

Triggers are configured in `ProcessConfig.triggers` as a list of `TriggerConfig` objects:

```python
from promptise.runtime import ProcessConfig, TriggerConfig

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Multi-trigger agent.",
    triggers=[
        # Every 5 minutes
        TriggerConfig(type="cron", cron_expression="*/5 * * * *"),

        # On file changes
        TriggerConfig(
            type="file_watch",
            watch_path="/data/inbox",
            watch_patterns=["*.csv", "*.json"],
            watch_events=["created", "modified"],
        ),

        # On EventBus events
        TriggerConfig(
            type="event",
            event_type="pipeline.error",
            event_source="data-pipeline",
        ),
    ],
)
```

---

## API Summary

| Class / Function | Description |
|---|---|
| `BaseTrigger` | Protocol that all triggers implement |
| `TriggerEvent` | Event dataclass produced by triggers |
| `CronTrigger` | Time-based cron schedule trigger |
| `EventTrigger` | EventBus event trigger |
| `MessageTrigger` | MessageBroker topic trigger |
| `WebhookTrigger` | HTTP webhook trigger |
| `FileWatchTrigger` | Filesystem change trigger |
| `create_trigger(config)` | Factory function to create triggers from config |
| `register_trigger_type(name, factory)` | Register a custom trigger type |
| `unregister_trigger_type(name)` | Remove a registered trigger type |
| `registered_trigger_types()` | List all registered type names |

---

## Concurrency Architecture

Understanding how triggers, queues, and workers interact is critical for production deployments.

### How events flow

```
┌─────────────────┐
│  CronTrigger    │──┐
│  (listener task)│  │
└─────────────────┘  │
┌─────────────────┐  │     ┌─────────────────┐     ┌──────────────┐
│  WebhookTrigger │──┼────→│  trigger_queue   │────→│  Worker #1   │──→ invoke_agent()
│  (listener task)│  │     │  (maxsize=1000)  │     └──────────────┘
└─────────────────┘  │     │                  │     ┌──────────────┐
┌─────────────────┐  │     │                  │────→│  Worker #2   │──→ invoke_agent()
│  EventTrigger   │──┘     └─────────────────┘     └──────────────┘
│  (listener task)│               ↑                       ↑
└─────────────────┘          Bounded queue          Semaphore(concurrency)
```

1. **Each trigger runs its own listener task** — a cron trigger sleeping until its next tick does not block a webhook from accepting requests. All triggers operate in parallel as independent `asyncio.Task`s.

2. **All triggers feed into one shared queue** — `asyncio.Queue(maxsize=1000)`. This is the central dispatch point. When any trigger fires, its `TriggerEvent` is placed in this queue.

3. **Worker tasks consume from the queue** — the process starts `config.concurrency` worker tasks (default: 1). Each worker pulls the next event, acquires the concurrency semaphore, and calls `invoke_agent()`.

4. **The semaphore controls parallelism** — with `concurrency=1`, invocations run one at a time (queued). With `concurrency=3`, up to 3 agent invocations can run simultaneously.

### What happens when multiple triggers fire at once

If a cron trigger and 3 webhook requests all fire within the same second:

- All 4 events are enqueued into the trigger queue (4 slots used out of 1000)
- Worker(s) process them in order
- With `concurrency=1`: events are processed sequentially, ~10-30s per invocation
- With `concurrency=3`: first 3 events process in parallel, 4th waits for a free worker

### What happens when the queue is full

If the queue reaches its 1000-event capacity:

- New trigger events are **dropped** with a warning log
- The webhook still returns `202 Accepted` to the caller (it doesn't know about the drop)
- The cron trigger silently skips the tick

For high-throughput scenarios, increase `concurrency` or add backpressure at the trigger level.

### What happens when the agent is suspended

When the process enters `SUSPENDED` state (e.g., budget exceeded):

- Workers detect the state and **re-queue** the event (put it back)
- Workers sleep 0.5s before checking the queue again
- Events are **not lost** during suspension — they wait until the process resumes

### Configuring concurrency

```python
config = ProcessConfig(
    model="openai:gpt-4o-mini",
    instructions="High-throughput event handler.",
    concurrency=3,  # Allow 3 parallel agent invocations
    triggers=[
        TriggerConfig(type="webhook", webhook_path="/events", webhook_port=9090),
        TriggerConfig(type="cron", cron_expression="*/1 * * * *"),
    ],
)
```

!!! warning "Concurrency and state"
    With `concurrency > 1`, multiple invocations share the same `AgentContext` and conversation buffer. Make sure your agent instructions are safe for concurrent execution, or keep `concurrency=1` (default) for sequential processing.

### Failure handling

Each worker tracks consecutive failures. If `max_consecutive_failures` is reached (default: 5), the process transitions to `FAILED` state. The journal records the failure for crash recovery.

```python
config = ProcessConfig(
    ...
    max_consecutive_failures=3,  # Transition to FAILED after 3 consecutive errors
)
```

---

## Tips and Gotchas

!!! tip "Multiple triggers per process"
    A process can have any number of triggers. Each runs its own listener loop. Events from all triggers are enqueued into the same processing queue.

!!! tip "filter_expression for pre-filtering"
    All trigger types support an optional `filter_expression` in `TriggerConfig`. This allows cheap pre-filtering before invoking the LLM, saving tokens on irrelevant events.

!!! info "Dependencies shipped with base install"
    `WebhookTrigger` uses `aiohttp` and `FileWatchTrigger` uses `watchdog`. Both ship with the base `pip install promptise`.

!!! warning "Queue overflow"
    Each trigger has an internal queue (default capacity: 100-1000). If events arrive faster than the agent can process them, the oldest events are dropped with a warning.

---

## What's Next

- [Cron Trigger](cron.md) -- schedule-based triggering
- [Event and Webhook Triggers](event-webhook.md) -- event-driven and HTTP-driven triggering
- [File Watch Trigger](file-watch.md) -- filesystem change detection
- [Configuration](../configuration.md) -- full `TriggerConfig` reference
