# Event and Webhook Triggers

The `EventTrigger`, `MessageTrigger`, and `WebhookTrigger` provide event-driven activation for agent processes. EventTrigger listens to the framework's internal EventBus, MessageTrigger subscribes to MessageBroker topics, and WebhookTrigger exposes an HTTP endpoint for external systems.

```python
from promptise.runtime import ProcessConfig, TriggerConfig

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Handle incoming events and webhooks.",
    triggers=[
        TriggerConfig(type="event", event_type="pipeline.error"),
        TriggerConfig(type="webhook", webhook_path="/events", webhook_port=9090),
        TriggerConfig(type="message", topic="reports.daily"),
    ],
)
```

---

## Concepts

All three trigger types use an internal `asyncio.Queue` to decouple event delivery from consumption. Events arriving faster than the agent can process them are buffered (up to the queue capacity). If the queue fills, events are dropped with a warning.

| Trigger | Source | Use Case |
|---|---|---|
| `EventTrigger` | Internal `EventBus` | React to events from other agent processes |
| `MessageTrigger` | Internal `MessageBroker` | Subscribe to topic-based message streams |
| `WebhookTrigger` | External HTTP POST | Receive events from external systems (CI/CD, monitoring, APIs) |

---

## EventTrigger

Wraps an `EventBus` subscription and converts matching events into `TriggerEvent` objects.

### Configuration

```python
from promptise.runtime import TriggerConfig

# Listen for pipeline errors
TriggerConfig(type="event", event_type="pipeline.error")

# With source filtering
TriggerConfig(
    type="event",
    event_type="task.completed",
    event_source="data-pipeline",
)
```

### Direct instantiation

```python
from promptise.runtime.triggers.event import EventTrigger
from promptise.runtime.events import EventBus

bus = EventBus()

trigger = EventTrigger(
    bus,
    "pipeline.error",
    source_filter="data-pipeline",
    trigger_id="pipe-err-listener",
)

await trigger.start()
event = await trigger.wait_for_next()
print(event.payload)
# {
#     "event_type": "pipeline.error",
#     "event_id": "...",
#     "source": "data-pipeline",
#     "data": {...},
# }
await trigger.stop()
```

### Source filtering

When `source_filter` is set, only events from that specific source fire the trigger. Events from other sources are silently ignored. This is useful when multiple producers publish to the same event type.

### Event payload

| Field | Description |
|---|---|
| `event_type` | The EventBus event type string |
| `event_id` | Unique event identifier |
| `source` | Event source |
| `data` | Event data dict |

---

## MessageTrigger

Wraps a `MessageBroker` subscription for topic-based messaging.

### Configuration

```python
from promptise.runtime import TriggerConfig

# Subscribe to a specific topic
TriggerConfig(type="message", topic="reports.daily")

# Wildcard topics are supported by the broker
TriggerConfig(type="message", topic="reports.*")
```

### Direct instantiation

```python
from promptise.runtime.triggers.event import MessageTrigger
from promptise.runtime.broker import MessageBroker

broker = MessageBroker()

trigger = MessageTrigger(
    broker,
    "reports.daily",
    trigger_id="daily-report-sub",
)

await trigger.start()
event = await trigger.wait_for_next()
print(event.payload)
# {
#     "topic": "reports.daily",
#     "message_id": "...",
#     "sender": "report-generator",
#     "content": "...",
# }
await trigger.stop()
```

### Message payload

| Field | Description |
|---|---|
| `topic` | The topic the message arrived on |
| `message_id` | Unique message identifier |
| `sender` | Message sender |
| `content` | Message content |

---

## WebhookTrigger

An `aiohttp` HTTP server that listens for incoming POST requests and converts them to `TriggerEvent` objects.

### Configuration

```python
from promptise.runtime import TriggerConfig

TriggerConfig(
    type="webhook",
    webhook_path="/events",
    webhook_port=9090,
)
```

### Direct instantiation

```python
from promptise.runtime.triggers.webhook import WebhookTrigger

trigger = WebhookTrigger(
    path="/webhook",
    port=9090,
    host="0.0.0.0",
)

await trigger.start()
# HTTP server now listening at http://0.0.0.0:9090/webhook

event = await trigger.wait_for_next()
print(event.payload)   # POST body (JSON or text)
print(event.metadata)  # {"method": "POST", "path": "/webhook", ...}

await trigger.stop()
```

### How requests are handled

1. A POST request arrives at the configured path.
2. The body is parsed as JSON (falling back to plain text).
3. A `TriggerEvent` is created with the body as `payload`.
4. Request metadata (method, path, query params, safe headers) is stored in `metadata`.
5. The response `202 Accepted` is returned with the event ID.

Sensitive headers (`Authorization`, `Cookie`, `Set-Cookie`) are automatically stripped from the metadata.

### Health check endpoint

The webhook server also exposes `GET /health`:

```json
{
    "status": "healthy",
    "trigger_id": "webhook-9090/events",
    "queue_size": 0
}
```

### Webhook payload

The `payload` field contains the raw POST body:

- If the body is valid JSON, it is stored as a dict.
- Otherwise, it is stored as a string.

### Webhook metadata

| Field | Description |
|---|---|
| `method` | HTTP method (always `POST`) |
| `path` | Request path |
| `query` | Query parameters dict |
| `headers` | Safe headers (auth headers excluded) |
| `remote` | Client IP address |

### Queue overflow

If the webhook queue (capacity: 1000) is full, the server returns `503 Service Unavailable`:

```json
{"status": "error", "message": "queue full"}
```

---

## Shared Patterns

### Queue-based decoupling

All three triggers use `asyncio.Queue` to buffer events:

- `EventTrigger`: maxsize=100
- `MessageTrigger`: maxsize=100
- `WebhookTrigger`: maxsize=1000

### Graceful stop

When `stop()` is called, each trigger:

1. Unsubscribes from its event source (EventBus, MessageBroker, or shuts down the HTTP server).
2. Enqueues a sentinel event with `metadata={"_stop": True}`.
3. Any `wait_for_next()` call receiving the sentinel raises `asyncio.CancelledError`.

---

## API Summary

| Class | Description |
|---|---|
| `EventTrigger(event_bus, event_type, source_filter, trigger_id)` | EventBus trigger |
| `MessageTrigger(broker, topic, trigger_id)` | MessageBroker trigger |
| `WebhookTrigger(path, port, host)` | HTTP webhook trigger |

All implement the `BaseTrigger` protocol: `start()`, `stop()`, `wait_for_next()`.

---

## Tips and Gotchas

!!! tip "Use EventTrigger for inter-process coordination"
    When one process needs to trigger another, use `EventTrigger` with a shared `EventBus`. This is more efficient than webhooks for in-process communication.

!!! tip "Webhook security"
    The webhook server binds to `0.0.0.0` by default. In production, use a reverse proxy (nginx, Caddy) for TLS termination and authentication. The trigger itself does not perform any request authentication.

!!! info "aiohttp shipped with base install"
    `WebhookTrigger` uses `aiohttp`, which is included in the base `pip install promptise`.

!!! warning "EventBus/Broker must be shared"
    For `EventTrigger` and `MessageTrigger` to work, the same `EventBus` or `MessageBroker` instance must be shared between the event producer and the trigger. Pass them to the `AgentRuntime` constructor.

!!! warning "Queue capacity is finite"
    If events arrive faster than the agent processes them, the queue will fill and events will be dropped. Monitor queue sizes via the dashboard or `status()` API. Increase `ProcessConfig.concurrency` for high-throughput scenarios.

---

## What's Next

- [Triggers Overview](index.md) -- all trigger types and the base protocol
- [Cron Trigger](cron.md) -- time-based scheduling
- [File Watch Trigger](file-watch.md) -- filesystem monitoring
- [Configuration](../configuration.md) -- full `TriggerConfig` reference
