---
title: "Event, Webhook & File-Watch Triggered Agents"
description: "Beyond cron: how to wire agents to real-world events. Compares the four reactive trigger types — HMAC-verified WebhookTrigger, EventBus EventTrigger, topic…"
keywords: "event-driven AI agent, webhook triggered AI agent, file watch AI agent, reactive AI agent, pub/sub agent trigger, trigger-driven agent"
date: 2026-07-16
slug: event-driven-ai-agent
categories:
  - Runtime
---

# Event, Webhook & File-Watch Triggered Agents

An event-driven AI agent wakes up when something happens in the real world — a webhook fires, a file lands, another service publishes a message — instead of waiting for you to call it or for a clock to tick. Most "autonomous agent" tutorials stop at a cron schedule, which is fine for periodic work but useless when the trigger is external and unpredictable. By the end of this post you'll know the four reactive trigger types Promptise Foundry ships, how to compose several of them on a single process, and how to verify a webhook with HMAC so only trusted callers can wake your agent.

<!-- more -->

## Beyond cron: what makes an agent reactive

A cron trigger answers "run every five minutes." A reactive AI agent answers a harder question: "run *now*, because this specific thing just occurred." That distinction matters because most production work is event-shaped. A CI pipeline fails. A customer uploads a CSV. A monitoring system crosses a threshold. None of those happen on a schedule, and polling for them wastes tokens and latency.

In the runtime, triggers are the activation mechanism. Each one produces a `TriggerEvent` that wakes an `AgentProcess` and injects the event payload into the agent's context for that invocation. A cron trigger is just one type; the reactive types are what turn a scheduled batch job into a trigger-driven agent that responds to the world. If you're new to the process model behind all of this, start with [What Is an Autonomous AI Agent Runtime?](autonomous-ai-agent-runtime.md) — this post assumes you know what an `AgentProcess` is and focuses on the triggers that feed it.

## The four reactive trigger types

Alongside `CronTrigger`, the runtime ships four reactive triggers. Each satisfies the same `BaseTrigger` protocol (`start()`, `stop()`, `wait_for_next()`), so they're interchangeable from the process's point of view — it just enqueues whatever event arrives.

- **`WebhookTrigger`** — an `aiohttp` HTTP server that fires on `POST`. This is the bridge from external systems: GitHub, Stripe, CI/CD, monitoring, any service that can send an HTTP call. Optional HMAC signature verification.
- **`EventTrigger`** — subscribes to the framework's internal `EventBus`. Use it when one agent process needs to react to something another process emitted, without a network hop.
- **`MessageTrigger`** — subscribes to a `MessageBroker` topic. This is your in-process pub/sub agent trigger, and topics support wildcards (`reports.*`) so one subscription can catch a whole family of messages.
- **`FileWatchTrigger`** — watches a directory with glob patterns and fires when files are created or modified. Perfect for the "someone dropped a file, go process it" workflow.

The full reference for every field and payload shape lives in the [triggers guide](../../runtime/triggers/index.md). The key idea is that these aren't four separate frameworks — they're four sources feeding one queue.

## Compose triggers on one AgentProcess

Here's the payoff, and the part most tutorials never show: a single process can carry any number of triggers at once. Each runs its own listener task in parallel, and all of them feed the same bounded queue, so a webhook request never blocks a file-watch event, and vice versa. Worker tasks pull from that queue and invoke the agent, with `concurrency` controlling how many invocations run in parallel.

This example wires one incident-response agent to a webhook, a file drop, an internal event, and a wildcard message topic — four different ways to wake the same process:

```python
import asyncio
from promptise.runtime import (
    AgentRuntime,
    ProcessConfig,
    TriggerConfig,
    JournalConfig,
)


async def main():
    async with AgentRuntime() as runtime:
        await runtime.add_process(
            "ops-responder",
            ProcessConfig(
                model="openai:gpt-5-mini",
                instructions=(
                    "You are an operations responder. For each event, decide "
                    "whether it needs escalation and summarize what happened."
                ),
                triggers=[
                    # 1. External HTTP POST (CI/CD, monitoring, third-party APIs)
                    TriggerConfig(type="webhook", webhook_path="/events", webhook_port=9090),
                    # 2. A file lands in the inbox
                    TriggerConfig(
                        type="file_watch",
                        watch_path="/data/inbox",
                        watch_patterns=["*.csv", "*.json"],
                        watch_events=["created"],
                    ),
                    # 3. Another process emits an internal event
                    TriggerConfig(type="event", event_type="pipeline.error", event_source="etl"),
                    # 4. Any message on a wildcard pub/sub topic
                    TriggerConfig(type="message", topic="alerts.*"),
                ],
                # Every event and invocation is journaled for crash recovery.
                journal=JournalConfig(backend="file", path="./ops-journal"),
                concurrency=3,  # up to 3 events handled in parallel
            ),
        )

        await runtime.start_all()          # CREATED -> STARTING -> RUNNING
        print(runtime.status())            # state, invocation counts, queue depth

        # The process now reacts on its own. POST to :9090/events, drop a CSV
        # in /data/inbox, or publish to alerts.* — any of them wakes the agent.
        await asyncio.sleep(3600)

        await runtime.stop_all()           # RUNNING -> STOPPING -> STOPPED


asyncio.run(main())
```

Notice that the agent's logic — the `model` and `instructions` — is identical to what you'd pass to `build_agent()`. The triggers are pure configuration. Add or remove one and the agent code doesn't change. For the broader picture of how processes, the runtime manager, and governance fit together, the [Agent Runtime overview](../../runtime/index.md) is the map.

## Webhook triggered AI agent, verified with HMAC

A webhook that anyone can `POST` to is a way for anyone to run up your LLM bill. Promptise's `WebhookTrigger` supports HMAC-SHA256 signature verification: give the trigger a shared secret, and every request must carry a matching `X-Webhook-Signature` header or it's rejected with `401` before the agent is ever invoked.

HMAC verification is a constructor option on the trigger itself, so you instantiate `WebhookTrigger` directly to enable it:

```python
from promptise.runtime.triggers.webhook import WebhookTrigger

trigger = WebhookTrigger(
    path="/github",
    port=9090,
    host="0.0.0.0",
    hmac_secret="your-shared-secret",  # every POST must be signed with this
)
await trigger.start()
```

The caller signs the raw request body with the same secret and sends the digest, prefixed with `sha256=`:

```bash
body='{"event":"deploy.failed","service":"checkout"}'
sig=$(printf '%s' "$body" | openssl dgst -sha256 -hmac "your-shared-secret" | awk '{print $2}')

curl -X POST http://localhost:9090/github \
  -H "X-Webhook-Signature: sha256=$sig" \
  -d "$body"
```

Requests with a missing or mismatched signature get `401` and never reach the agent. The trigger also strips sensitive headers (`Authorization`, `Cookie`) from the event metadata, so secrets don't leak into the journal.

One honest caveat: a webhook defined purely through `TriggerConfig` binds without request authentication — HMAC is enabled by constructing the trigger directly with `hmac_secret`, or by putting the endpoint behind a reverse proxy (nginx, Caddy) that handles TLS and auth. Don't expose an unauthenticated webhook to the public internet.

## File watch AI agent: react to a folder

For batch and data workflows, a file watch AI agent is often the cleanest design. Instead of a script that polls a directory on a timer, `FileWatchTrigger` fires the instant a matching file appears. The event payload carries the `path`, `filename`, and `event_type`, which the agent sees in its context:

```python
TriggerConfig(
    type="file_watch",
    watch_path="/data/inbox",
    watch_patterns=["*.csv", "*.parquet"],
    watch_events=["created", "modified"],
)
```

Under the hood it uses `watchdog` when available and falls back to polling otherwise, so it works the same across platforms. Glob patterns keep it focused — a `*.csv` watcher ignores the temp files your uploader writes alongside the real data.

## Every event lands in the journal

Reactivity is only trustworthy if you can see what happened and recover when a process dies mid-event. That's the journal's job. Every trigger firing, state transition, and invocation result is written as a `JournalEntry`, so after a crash the replay engine reconstructs the process's last known state and the restart policy brings it back — no lost events, no manual replay. It's also your audit trail: exactly which webhook, at which timestamp, caused which invocation. The [journal system](../../runtime/journal/index.md) documents the detail levels (`none`, `checkpoint`, `full`) so you can trade storage for observability.

One thing to keep in mind: the trigger queues are bounded (webhook capacity is 1000; event and message queues 100). If events arrive faster than the agent can process them, the oldest are dropped with a warning. Raise `concurrency` or add backpressure upstream for genuinely high-throughput streams.

## When cron — or a real queue — is the better fit

Reactive triggers aren't always the right answer. If your work truly is periodic — a nightly report, an hourly sync — a `CronTrigger` is simpler and more predictable than rigging an event to fire on a schedule. And if you need durable, replayable, fan-out messaging with delivery guarantees, a dedicated broker like Kafka, SQS, or NATS is the better fit; the built-in `EventBus` and `MessageBroker` are in-process and lose buffered events if the queue overflows or the process exits. A clean pattern is to let the durable broker own delivery and put a thin `WebhookTrigger` in front of your agent. Use the built-in reactive triggers for a self-contained, single-process system with zero external infrastructure; reach for a real queue when durability and scale-out are hard requirements.

## Frequently asked questions

### What is an event-driven AI agent?

An event-driven AI agent is one that invokes itself in response to an external or internal event — an HTTP webhook, a file appearing on disk, a pub/sub message, or an event from another process — rather than on a fixed schedule or a manual call. In Promptise Foundry, each event source is a trigger that wakes an `AgentProcess` and passes the event payload into the agent's context for that run.

### Can one agent react to multiple event sources at once?

Yes. A single `AgentProcess` can declare any number of triggers, and each runs its own listener task in parallel. All of them feed one shared, bounded queue, and worker tasks (sized by `concurrency`) pull events and invoke the agent. So the same process can react to a webhook, a file drop, and a message topic simultaneously.

### How do I stop untrusted callers from triggering my webhook?

Construct the `WebhookTrigger` with an `hmac_secret`. Every `POST` must then include an `X-Webhook-Signature: sha256=<digest>` header computed from the request body and the shared secret; requests without a valid signature are rejected with `401` before the agent runs. For public endpoints, also terminate TLS and authenticate at a reverse proxy.

## Next steps

Point a `WebhookTrigger` at your agent and fire your first event-driven invocation in under 15 minutes: start with the [Quick Start](../../getting-started/quickstart.md) to get an agent running, then follow the [triggers guide](../../runtime/triggers/index.md) to compose webhook, file-watch, event, and message triggers on one process. When you're ready to add journaling and governance on top, [How to Build a Long-Running AI Agent](long-running-ai-agent.md) walks through the full production build.
