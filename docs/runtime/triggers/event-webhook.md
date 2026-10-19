# Event and Webhook Triggers

These three triggers run an agent when something happens elsewhere. `WebhookTrigger` exposes an HTTP endpoint for external systems (GitHub, Stripe, CI, monitoring). `EventTrigger` and `MessageTrigger` listen to an event bus or a message broker that you share between processes, so one process can wake another.

```python
import os
from promptise.runtime import ProcessConfig, TriggerConfig

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Triage new GitHub issues.",
    triggers=[
        TriggerConfig(
            type="webhook",
            webhook_path="/github",
            webhook_port=9090,
            hmac_secret=os.environ["GITHUB_WEBHOOK_SECRET"],
            signature_scheme="github",
            filter_expression="payload['action'] == 'opened'",
        ),
    ],
)
```

---

## Concepts

| Trigger | Source | Use case |
|---|---|---|
| `WebhookTrigger` | External HTTP POST | Receive events from external systems (CI/CD, monitoring, SaaS webhooks) |
| `EventTrigger` | The runtime's `event_bus` | React to events from other agent processes or your own code |
| `MessageTrigger` | The runtime's `broker` | Subscribe to topic-based message streams |

Each trigger buffers events in its own `asyncio.Queue` until the process's listener takes them. What happens when a buffer is full differs:

- **Webhook:** the request is refused with `503` and a `Retry-After: 30` header, so the sender can retry. Nothing is dropped silently.
- **Event and message:** the queue holds 100 events; beyond that, new events are dropped with a warning in the log.

---

## EventTrigger

Subscribes to one event type on an event bus and turns matching events into `TriggerEvent` objects.

### The event bus

The runtime doesn't ship an event bus. `EventTrigger` works with any object that has these async methods, so you can wrap Redis Pub/Sub, Kafka, or an in-memory dispatcher:

| Method | Called by |
|---|---|
| `await subscribe(event_type, handler)` | `EventTrigger.start()` |
| `await unsubscribe(event_type, handler)` | `EventTrigger.stop()` |
| `await emit(event_type, payload)` | Budget, health and mission [escalations](../governance/budget.md#enforcement-actions) with an `EscalationTarget(event_type=...)` |

The handler is an async callable that receives the event object. The trigger reads its `event_id`, `source` and `data` attributes (missing ones become `""` / `{}`).

A minimal in-memory bus:

```python
from dataclasses import dataclass, field
from uuid import uuid4


@dataclass
class BusEvent:
    data: dict
    source: str = ""
    event_id: str = field(default_factory=lambda: uuid4().hex)


class InMemoryBus:
    def __init__(self) -> None:
        self._handlers: dict[str, list] = {}

    async def subscribe(self, event_type, handler):
        self._handlers.setdefault(event_type, []).append(handler)

    async def unsubscribe(self, event_type, handler):
        self._handlers.get(event_type, []).remove(handler)

    async def emit(self, event_type, data, source=""):
        for handler in list(self._handlers.get(event_type, [])):
            await handler(BusEvent(data=data, source=source))
```

Pass the same instance to the runtime and to whatever produces the events:

```python
from promptise.runtime import AgentRuntime, ProcessConfig, TriggerConfig

bus = InMemoryBus()
runtime = AgentRuntime(event_bus=bus)
await runtime.add_process(
    "pipeline-doctor",
    ProcessConfig(
        model="openai:gpt-5-mini",
        instructions="Diagnose failed pipeline jobs.",
        triggers=[TriggerConfig(type="event", event_type="pipeline.error", event_source="data-pipeline")],
    ),
)
await runtime.start_all()

await bus.emit("pipeline.error", {"job": "nightly-load"}, source="data-pipeline")
```

### Configuration

| Field | Default | Description |
|---|---|---|
| `event_type` | required | Event type to subscribe to |
| `event_source` | `None` | Only events whose `source` equals this fire the trigger |
| `filter_expression` | `None` | Skip events before the agent runs (see [Filtering](index.md#filtering-events-before-the-agent-runs)) |

### Direct instantiation

```python
from promptise.runtime.triggers.event import EventTrigger

trigger = EventTrigger(bus, "pipeline.error", source_filter="data-pipeline")

await trigger.start()
event = await trigger.wait_for_next()
print(event.payload)
# {'event_type': 'pipeline.error', 'event_id': '9b5e8cdb…', 'source': 'data-pipeline',
#  'data': {'job': 'nightly-load'}}
await trigger.stop()
```

### Event payload

| Field | Description |
|---|---|
| `event_type` | The subscribed event type |
| `event_id` | The bus event's `event_id` |
| `source` | The bus event's `source` |
| `data` | The bus event's `data` |

---

## MessageTrigger

Subscribes to one topic on a message broker.

### The message broker

Like the event bus, the broker is any object with these async methods:

| Method | Called by |
|---|---|
| `await subscribe(topic, handler)` | `MessageTrigger.start()`; returns a subscription ID |
| `await unsubscribe(subscription_id)` | `MessageTrigger.stop()` |

The handler receives a message object; the trigger reads `message_id`, `sender` and `content` (falling back to `str(message)` for `content`). Topic matching, including wildcards such as `reports.*`, is up to your broker.

```python
from dataclasses import dataclass, field
from uuid import uuid4


@dataclass
class Message:
    content: str
    sender: str = ""
    message_id: str = field(default_factory=lambda: uuid4().hex)


class InMemoryBroker:
    def __init__(self) -> None:
        self._subs: dict[str, tuple[str, object]] = {}

    async def subscribe(self, topic, handler):
        sub_id = uuid4().hex
        self._subs[sub_id] = (topic, handler)
        return sub_id

    async def unsubscribe(self, sub_id):
        self._subs.pop(sub_id, None)

    async def publish(self, topic, content, sender=""):
        for t, handler in list(self._subs.values()):
            if t == topic:
                await handler(Message(content=content, sender=sender))


runtime = AgentRuntime(broker=InMemoryBroker())
# TriggerConfig(type="message", topic="reports.daily")
```

### Direct instantiation

```python
from promptise.runtime.triggers.event import MessageTrigger

broker = InMemoryBroker()
trigger = MessageTrigger(broker, "reports.daily")

await trigger.start()
await broker.publish("reports.daily", "Report ready", sender="report-generator")
event = await trigger.wait_for_next()
print(event.payload)
# {'topic': 'reports.daily', 'message_id': '8796…', 'sender': 'report-generator',
#  'content': 'Report ready'}
await trigger.stop()
```

### Message payload

| Field | Description |
|---|---|
| `topic` | The subscribed topic |
| `message_id` | The message's `message_id` |
| `sender` | The message's `sender` |
| `content` | The message's `content` |

---

## WebhookTrigger

An `aiohttp` HTTP server that listens for POST requests and turns each accepted request into a `TriggerEvent`. Each webhook trigger runs its own server on its own port.

### Configuration

```python
import os
from promptise.runtime import TriggerConfig

TriggerConfig(
    type="webhook",
    webhook_path="/github",
    webhook_port=9090,
    webhook_host="127.0.0.1",          # default: loopback only
    hmac_secret=os.environ["GITHUB_WEBHOOK_SECRET"],
    signature_scheme="github",         # generic | github | stripe
    allowed_sources=["203.0.113.0/24"],  # the sender's published IP ranges
    webhook_max_body_bytes=256 * 1024,   # default 1 MiB
    filter_expression="payload['action'] == 'opened'",
)
```

| Field | Default | Description |
|---|---|---|
| `webhook_path` | `"/webhook"` | URL path that accepts POST requests |
| `webhook_port` | `9090` | TCP port (1025–65535) |
| `webhook_host` | `"127.0.0.1"` | Interface to bind. The default only accepts local connections; use `"0.0.0.0"` to accept requests from other machines |
| `hmac_secret` | `None` | Shared secret; when set, requests without a valid signature get `401` |
| `signature_scheme` | `"generic"` | How the signature is computed (table below); requires `hmac_secret` |
| `signature_header` | scheme default | Override the header the signature is read from; requires `hmac_secret` |
| `signature_tolerance` | `300` | Max age in seconds of a `stripe` signature timestamp; inside that window each signed request is accepted once (replay protection) |
| `allowed_sources` | `[]` (any) | Client IPs or CIDR ranges allowed to call the webhook; others get `403` |
| `webhook_max_body_bytes` | `1048576` (1 MiB) | Largest request body accepted, 1 byte to 1 GiB; larger bodies get `413` |
| `filter_expression` | `None` | Skip events before the agent runs (see [Filtering](index.md#filtering-events-before-the-agent-runs)) |

In a manifest, keep the secret out of the file with an environment reference: `hmac_secret: ${GITHUB_WEBHOOK_SECRET}`. The secret is a `SecretStr`: it is masked in `repr()`, in config dumps and in `GET /triggers`.

### Authentication: HMAC signatures

With `hmac_secret` set, every request must carry an HMAC-SHA256 signature of the raw body, made with the shared secret. The comparison is timing-safe.

| `signature_scheme` | Header | Value |
|---|---|---|
| `generic` | `X-Webhook-Signature` | `sha256=<hex HMAC of body>` |
| `github` | `X-Hub-Signature-256` | `sha256=<hex HMAC of body>`, as GitHub sends it |
| `stripe` | `Stripe-Signature` | `t=<unix ts>,v1=<hex HMAC of "<ts>.<body>">`, as Stripe sends it; rejected when the timestamp is older than `signature_tolerance`, or when the same signed request was already accepted |

`generic` and `github` signatures cover only the body, with no timestamp, so anyone who captures a signed request can send it again. Serve the webhook over TLS (a reverse proxy in front of it) so requests can't be captured, and make the agent's actions safe to repeat where you can.

Signing a request for the `generic` scheme:

```python
import hashlib, hmac, json, urllib.request

body = json.dumps({"order_id": 42}).encode()
signature = "sha256=" + hmac.new(b"my-secret", body, hashlib.sha256).hexdigest()
urllib.request.urlopen(urllib.request.Request(
    "http://127.0.0.1:9090/webhook",
    data=body,
    headers={"Content-Type": "application/json", "X-Webhook-Signature": signature},
))
```

For other providers that sign the body with HMAC-SHA256 and a `sha256=` prefix, keep `signature_scheme="generic"` and set `signature_header` to their header name.

#### Stripe replay protection

A `stripe` signature is accepted once. The webhook records it only when it takes the event, so a sender's retry is not mistaken for a replay:

- after a `503` (process unavailable or queue full), the signature was never recorded and the retry goes through;
- after a `500` (an unexpected error while handling the request), the signature is released again, so the retry goes through;
- after a `202` or a `200 ignored`, the same signature gets `401`.

Recorded signatures are kept for twice `signature_tolerance`.

!!! warning "Set a secret before you expose the port"
    Without `hmac_secret`, anyone who can reach the port can make the agent run, and pick what it reads. The trigger logs a warning at start-up in that case. Always set a secret (and ideally `allowed_sources`) when `webhook_host` isn't loopback.

**Unsigned loopback webhooks refuse browser requests.** A webhook on a loopback address can also be reached by any web page open in a browser on the same machine (a cross-site form post or `fetch`, or DNS rebinding). Without `hmac_secret`, a loopback webhook therefore answers `421` to a request whose `Host` header is not a loopback name and `403` to one with a cross-site `Origin` header, like the [control API](../api.md#browsers-cant-drive-a-local-api). Local senders such as `curl`, scripts and other services send a loopback `Host` and no `Origin`, so they are unaffected. A signed webhook skips this check: the signature already proves the sender.

**Agent-added webhooks.** A webhook trigger an open-mode agent adds (`add_trigger`), or gives a process it spawns, must listen on loopback unless `open_mode.allow_public_webhooks` is on, and then needs an `hmac_secret` (see [Open Mode](../meta-tools.md#security)).

`allowed_sources` is checked against the TCP peer address. Behind a reverse proxy that is the proxy's address, so list the proxy and do the provider IP check there.

### Direct instantiation

```python
from promptise.runtime.triggers.webhook import WebhookTrigger

trigger = WebhookTrigger(
    path="/webhook",
    port=9090,
    host="127.0.0.1",
    hmac_secret="my-secret",
    signature_scheme="generic",
    max_body_bytes=256 * 1024,
)

await trigger.start()
# HTTP server now listening at http://127.0.0.1:9090/webhook

event = await trigger.wait_for_next()
print(event.payload)   # POST body (JSON or text)
print(event.metadata)  # {"method": "POST", "path": "/webhook", ...}

await trigger.stop()
```

`event_filter=` takes a callable (the `TriggerEvent` in, a bool out); `TriggerConfig.filter_expression` is compiled into it. `WebhookTrigger` raises `ValueError` for an unknown `signature_scheme` or a `max_body_bytes` below 1.

### How requests are handled

1. A POST request arrives at the configured path.
2. The client address is checked against `allowed_sources` → `403` if not allowed. Without `hmac_secret` on a loopback bind, a non-loopback `Host` header → `421` and a cross-site `Origin` → `403`.
3. The body size is checked against `webhook_max_body_bytes` → `413` if larger. The `Content-Length` header is checked before the body is read; a chunked body without one is cut off at the limit while it is read.
4. With `hmac_secret` set, the signature is verified → `401` if missing or wrong (or a `stripe` replay).
5. If the owning process can't take the event (it is `FAILED`, stopping or stopped, or its queue is full) → `503` with `Retry-After: 30`.
6. The body is parsed as JSON (falling back to plain text) and a `TriggerEvent` is created with the body as `payload` and request details in `metadata`.
7. If a `filter_expression` is set and doesn't match → `200 {"status": "ignored", "event_id": ...}`; the agent doesn't run.
8. The event is queued → `202 {"status": "accepted", "event_id": ...}`. If the webhook's own queue is full at that moment → `503`.

A suspended process still accepts events: they wait in the queue until it resumes, and a process suspended by `idle_timeout` resumes by itself.

| Status | Body | Meaning |
|---|---|---|
| `202` | `{"status": "accepted", "event_id": …}` | Queued for the agent |
| `200` | `{"status": "ignored", "event_id": …}` | Didn't match `filter_expression` |
| `401` | `{"status": "error", "message": "Missing or invalid signature"}` | Missing, wrong or replayed signature |
| `403` | `{"status": "error", "message": "Source not allowed"}` | Source address not in `allowed_sources` |
| `403` | `{"status": "error", "message": "Cross-origin request refused"}` | Unsigned loopback webhook, cross-site `Origin` |
| `421` | `{"status": "error", "message": "Misdirected request"}` | Unsigned loopback webhook, non-loopback `Host` (DNS rebinding) |
| `404` | (aiohttp) | Wrong path |
| `405` | (aiohttp) | Method other than POST on the webhook path |
| `413` | `{"status": "error", "message": "Request body too large (limit … bytes)"}` | Body larger than `webhook_max_body_bytes` |
| `500` | `{"status": "error", "message": "internal error"}` | Unexpected error while handling the request; the event was not queued |
| `503` | `{"status": "unavailable", "message": …}` + `Retry-After: 30` | `message` is `process failed`, `process stopped`, `queue full` or `webhook stopped`; retry later |

The `202` is sent when the event is queued, before the agent runs. If the run later fails, the sender is not told; the event ends up in the process's [dead letters](index.md#retries-and-dead-letters). Stripe retries `5xx` answers automatically (for up to three days in live mode); GitHub doesn't retry failed deliveries by itself, but you can redeliver them from the webhook's settings page.

Sensitive headers (`Authorization`, `Proxy-Authorization`, `Cookie`, `Set-Cookie`) and the signature header are stripped from the metadata.

### Health check endpoint

The webhook server also exposes `GET /health` on the same port. It returns `200` while events are accepted and `503` when they aren't:

```json
{
    "status": "healthy",
    "reason": null,
    "trigger_id": "webhook-1f0c9e2a7b",
    "queue_size": 0
}
```

When unavailable, `status` is `"unavailable"` and `reason` is the same text the POST route returns in `message`.

### Webhook payload

The `payload` field contains the raw POST body:

- If the body is valid JSON, it is stored as parsed JSON (usually a dict).
- Otherwise, it is stored as a string.

The agent sees it inside a delimited untrusted-data block; see [Trigger payloads are untrusted input](index.md#trigger-payloads-are-untrusted-input).

### Webhook metadata

| Field | Description |
|---|---|
| `method` | HTTP method (always `POST`) |
| `path` | Request path |
| `query` | Query parameters dict |
| `headers` | Safe headers (auth and signature headers excluded) |
| `remote` | Client IP address |
| `signature_verified` | `True` when the request passed the HMAC check |

The agent sees only the payload, not the metadata.

---

## Shared Patterns

### Queue-based decoupling

All three triggers use `asyncio.Queue` to buffer events:

- `EventTrigger`: maxsize=100 (drops new events with a warning when full)
- `MessageTrigger`: maxsize=100 (drops new events with a warning when full)
- `WebhookTrigger`: maxsize=1000 (answers `503` when full)

### Graceful stop

When `stop()` is called, each trigger:

1. Unsubscribes from its event source (event bus, broker), or shuts down the HTTP server.
2. Enqueues a sentinel event with `metadata={"_stop": True}`.
3. Any `wait_for_next()` call receiving the sentinel raises `asyncio.CancelledError`.

Events still in the trigger's queue are returned by `drain_pending()`. A process calls it when it stops (the events are dead-lettered with reason `process stopped`) or restarts (they move to the process queue), so an accepted `202` webhook event is not lost.

---

## API Summary

| Class | Description |
|---|---|
| `EventTrigger(event_bus, event_type, *, source_filter, trigger_id)` | Event-bus trigger |
| `MessageTrigger(broker, topic, *, trigger_id)` | Message-broker trigger |
| `WebhookTrigger(path, port, host, hmac_secret, *, signature_scheme, signature_header, signature_tolerance, allowed_sources, event_filter, max_body_bytes)` | HTTP webhook trigger |

All implement the `BaseTrigger` protocol: `start()`, `stop()`, `wait_for_next()`. `WebhookTrigger.set_availability_check(callback)` installs the check behind the `503` answers; `AgentProcess` sets it for you.

---

## Common pitfalls

- **Large payloads.** Bodies over `webhook_max_body_bytes` (1 MiB by default) get `413`, and a retry of the same body gets `413` again. Raise the limit for providers that send big payloads.
- **Two webhooks on one port.** Each webhook trigger binds its own server, so a second trigger on the same port fails to start (address already in use). Give each one its own port, or use one webhook with a `filter_expression` per event kind.
- **Reverse proxy and `allowed_sources`.** Behind a proxy the peer address is the proxy's. List the proxy, or filter by IP in the proxy.
- **`200 ignored` is final.** Senders treat a `2xx` as delivered. A filtered request is not retried later, even if you change the filter.
- **A `202` is not a successful run.** Watch dead letters and the process status; the sender only knows the event was queued.
- **Event bus not shared.** `EventTrigger` and `MessageTrigger` only see events emitted on the same bus or broker instance you passed to `AgentRuntime`.

!!! tip "Use EventTrigger for inter-process coordination"
    When one process needs to trigger another, use `EventTrigger` with a shared event bus. This is simpler than a webhook for in-process communication.

!!! tip "Webhook security"
    The webhook server binds to `127.0.0.1` by default, so only local clients can reach it. When you bind `0.0.0.0`, set `hmac_secret` (and `allowed_sources` where the sender publishes its IP ranges), and put a reverse proxy (nginx, Caddy) in front for TLS. The trigger doesn't terminate TLS itself.

---

## What's Next

- [Triggers Overview](index.md) -- stable trigger IDs, live trigger changes and the event queue
- [Cron Trigger](cron.md) -- time-based scheduling
- [File Watch Trigger](file-watch.md) -- file-system monitoring
- [Runtime Manager](../runtime-manager.md) -- sharing an event bus and broker between processes
- [Configuration](../configuration.md) -- full `TriggerConfig` reference

!!! tip "Learn the concept"

    - [Event-Driven AI Agents: Webhook, Cron and File Triggers](https://www.promptise.com/guides/view/event-driven-ai-agents-webhook-cron-triggers) -- Signed webhooks and prompt-injection tests for event-driven agents.
    - [AI Agent Webhook Trigger: Run an Agent on HTTP Events](https://www.promptise.com/guides/view/ai-agent-webhook-trigger) -- HMAC signatures for GitHub and Stripe, replay protection and response codes.
