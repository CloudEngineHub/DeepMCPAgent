---
seo_title: "Agent Processes: Long-Running AI Agents"
description: "AgentProcess turns an agent into a long-running process: it starts triggers, runs the agent per event, keeps state between runs and restarts after failures."
---

# Agent Processes

`AgentProcess` turns an agent into a long-running process. It builds the agent, connects its MCP servers, starts its triggers, runs the agent once per trigger event, and keeps state, memory and conversation history between runs. It also restarts itself after failures when you ask it to. It is the core unit of the Agent Runtime; [`AgentRuntime`](runtime-manager.md) manages many of them.

```python
import asyncio

from promptise.runtime import AgentProcess, ProcessConfig, TriggerConfig, TriggerEvent


async def main():
    process = AgentProcess(
        name="health-checker",
        config=ProcessConfig(
            model="openai:gpt-5-mini",
            instructions="You monitor infrastructure health.",
            triggers=[TriggerConfig(type="cron", cron_expression="*/5 * * * *")],
        ),
    )
    await process.start()  # runs on the cron schedule from now on

    # Run it once now, without waiting for the schedule
    await process.inject(TriggerEvent(
        trigger_id="manual", trigger_type="manual", payload={"reason": "ad-hoc check"},
    ))
    await asyncio.sleep(5)

    status = process.status()
    print(status["state"], status["invocation_count"])  # running 1
    await process.stop()


asyncio.run(main())
```

---

## Creating a process

```python
AgentProcess(
    name,                  # unique process name; the journal files entries under it
    config,                # ProcessConfig
    *,
    process_id=None,       # auto-generated UUID if omitted
    event_bus=None,        # shared bus for "event" triggers
    broker=None,           # shared broker for "message" triggers
    runtime=None,          # parent AgentRuntime (open mode's spawn_process needs it)
    event_notifier=None,   # EventNotifier for process.* and agent events
    hooks=None,            # shared HookManager, dispatched after the process's own hooks
    extra_tools=None,      # local tools, added after config.extra_tools
    model=None,            # the agent's model, instead of config.model
)
```

The constructor checks each entry of `config.servers` and raises `ValueError` for one it can't use. Every setting lives in `ProcessConfig`; see [Configuration](configuration.md#processconfig) for all fields and defaults. Inside an `AgentRuntime`, use `runtime.add_process(name, config)` instead: it passes the runtime's bus, broker, notifier and hooks.

### Local tools

Python functions the agent can call need no MCP server: pass them in `extra_tools`, on `ProcessConfig` or the `AgentProcess` constructor (or both; the constructor's come after the config's). They take what `build_agent(extra_tools=...)` takes: functions made tools with `@tool`, plain functions (made tools as if decorated) and LangChain tools.

```python
from promptise import tool
from promptise.runtime import AgentProcess, BudgetConfig, ProcessConfig

@tool
def lookup_order(order_id: str) -> str:
    """Look up an order's status."""
    return f"{order_id}: shipped"

process = AgentProcess(
    "orders",
    ProcessConfig(
        instructions="Answer questions about orders.",
        extra_tools=[lookup_order],
        budget=BudgetConfig(enabled=True, max_tool_calls_per_run=10),
    ),
)
```

They sit next to the MCP servers' tools, and budget, approval, health and the journal treat them the same way. Every agent the process builds has them: after a restart, a hot reload or new instructions. Anything that is not a tool raises `TypeError` when the config or process is created.

They are code only, like a hook's `callback`: `model_dump()` and `RuntimeConfig.to_dict()` leave them out, and a manifest or YAML config can't name them (serve such tools from an MCP server instead). Processes spawned in open mode don't get them, and the agent can't create a tool with the name of one.

### The agent's model

`ProcessConfig.model` is usually a model id such as `"openai:gpt-5-mini"`. In code it can also be a model object, as `build_agent(model=...)` takes it: a LangChain chat model you configured yourself, a [`promptise.Model`](../core/agents/models.md) with its provider settings, a [`FallbackChain`](../core/fallback.md), or a [`ScriptedModel`](../core/testing.md) in a test. `AgentProcess(..., model=...)` does the same for one process and wins over `config.model`. Every agent the process builds uses it (after a restart, a hot reload or new instructions), and so does the mission evaluator.

```python
import asyncio

from promptise import tool
from promptise.runtime import AgentProcess, ProcessConfig, TriggerEvent
from promptise.testing import ScriptedModel, ToolCall


@tool
def lookup_order(order_id: str) -> str:
    """Look up an order's status."""
    return f"{order_id}: shipped"


async def main():
    model = ScriptedModel([
        ToolCall("lookup_order", {"order_id": "A-1001"}),
        "Order A-1001 has shipped.",
    ])
    process = AgentProcess(
        "orders",
        ProcessConfig(model=model, instructions="Answer questions about orders.", extra_tools=[lookup_order]),
    )
    await process.start()
    await process.inject(TriggerEvent(trigger_id="manual", trigger_type="manual", payload={"order": "A-1001"}))
    while process.status()["invocation_count"] < 1:
        await asyncio.sleep(0.05)
    await process.stop()

    print(len(model.calls), model.calls[1].messages[-1].content)


asyncio.run(main())
```

Output:

```
2 A-1001: shipped
```

A model object is code only, like `extra_tools`: `model_dump()` and `RuntimeConfig.to_dict()` leave `model` out, so a config read back from them has the default model id, and a manifest or YAML config names a model by its id. Events, the dashboard and the open-mode `list_capabilities` tool show a model object by its class and model name, such as `ChatOpenAI(gpt-5-mini)`; a `promptise.Model` is shown without its key. Processes spawned in open mode run on the parent's model unless the agent names another one. Anything that isn't a model id or a model object raises a `ValidationError` (`ValueError` from the `AgentProcess` constructor).

---

## Lifecycle methods

| Method | Transition | What it does |
|---|---|---|
| `await start()` | `CREATED`, `STOPPED` or `FAILED` → `STARTING` → `RUNNING` | Recovers from the journal (first start only), resolves secrets, builds the agent and connects MCP servers, starts triggers, workers and the heartbeat |
| `await stop(*, drain_timeout=0.0)` | → `STOPPING` → `STOPPED` (from `CREATED`: straight to `STOPPED`) | Cancels workers and runs in flight (with `drain_timeout`: lets runs in flight finish first, see [Graceful stop](#graceful-stop)), stops triggers, dead-letters queued events, writes a journal checkpoint (with a journal), closes the agent and its MCP connections, revokes secrets |
| `await close()` | as `stop()` | `stop()`, then closes long-term memory, revokes secrets and clears the conversation history |
| `await suspend(reason="manual")` | `RUNNING` → `SUSPENDED` | Pauses processing; triggers keep queueing events |
| `await resume(reason="manual")` | `SUSPENDED` → `RUNNING` | Runs the queued events in order; restarts the idle clock |
| `async with process:` | | `start()` on enter, `close()` on exit |

The [Lifecycle](lifecycle.md) page has the full state diagram, which transitions raise `StateError`, and every `process.*` event.

### start()

`start()` works from `CREATED`, `STOPPED` and `FAILED`. On `FAILED` it first tears down what the failed run left (trigger listeners, triggers, heartbeat, agent) and cancels a pending automatic restart, so nothing runs twice. If startup raises, the process goes to `FAILED`, `process.failed` is emitted, and `start()` raises the error to you; your own `start()` call is never retried automatically.

Calling `start()` on a process that is already `STARTING`, `RUNNING` or `SUSPENDED` raises `StateError`.

### stop()

`stop()` works from every state:

- `RUNNING`, `SUSPENDED`, `FAILED`: `STOPPING` → `STOPPED`, then `process.stopped`.
- `CREATED` (never started): straight to `STOPPED`, no event.
- `STOPPED` or `STOPPING`: does nothing.

An explicit `stop()` never triggers an automatic restart and cancels one that is pending. A run in flight is cancelled, and every event that hasn't run goes to the [dead letters](#dead-letters) with reason `"process stopped"`: events still in the queue, events a suspended worker or a trigger listener was holding, events still inside a trigger (a file watch's backlog, a webhook's accepted requests), and the cancelled run's event (with error `"run cancelled"`). An automatic restart keeps the queue instead, and adds the triggers' leftover events to it.

#### Graceful stop

`stop(drain_timeout=N)` lets the runs in flight finish before the process stops. From the moment it is called, no new event starts: trigger events and `inject()` go to the dead letters (`"process stopped"`), a webhook answers 503, and queued events are not picked up. The runs in flight get up to `N` seconds; only the ones still running then are cancelled. `drain_timeout=None` uses `ProcessConfig.drain_timeout` (default 5 seconds), and `process.abort_drain()` ends the wait early. A plain `stop()` keeps cancelling at once.

```python
await process.stop(drain_timeout=30)    # wait up to 30 s for the current run
await process.stop(drain_timeout=None)  # wait up to config.drain_timeout
await process.stop()                    # cancel at once
```

`AgentRuntime.run_forever()` stops this way on `SIGTERM` and Ctrl+C (see [Runtime Manager](runtime-manager.md#run_forever)).

A cancelled run may have called tools that already took effect, so it is not dropped silently: the process logs a warning, writes a `run_interrupted` journal entry (at every journal level) with the event, and keeps the event in its journal checkpoint. A new process object with the same name and journal lists it in `dead_letters` again after `start()` (and counts it in `recovered["interrupted_runs"]`), so you can inspect it and call `redeliver_dead_letters()` if it should run again. Once redelivered or cleared, it is no longer carried over.

### suspend() and resume()

`suspend()` pauses the process without tearing anything down. Triggers keep firing; their events wait in the queue in arrival order, and a run in flight finishes. `resume()` runs them in that order and restarts the idle clock, so `idle_timeout` counts from the resume.

A process suspended by `idle_timeout` resumes by itself when the next trigger event arrives (or you call `inject()`); you don't need to call `resume()`. Budget, health and manual suspensions wait for `resume()`. `process.suspend_reason` tells them apart: `"manual"`, `"idle"`, `"budget"`, `"health"` or `"rewind"`.

```python
await process.suspend()  # e.g. during a maintenance window
print(process.suspend_reason)  # "manual"
await process.resume()
```

### close()

`close()` stops the process and releases what lives as long as the process object: it closes the long-term memory provider, revokes secrets (whatever `revoke_on_stop` says) and clears the conversation history. `async with process:` and `AgentRuntime.remove_process()` call it. A closed process can be started again; `start()` opens a new memory provider, and an `in_memory` one starts empty.

---

## What survives stop and start

`stop()` and `start()` on the same object (including automatic restarts) keep the process's state, so the next run continues where the last one stopped. `close()` releases it.

| Item | After `stop()` / `start()` | After `close()` | In a new process object |
|---|---|---|---|
| Context state | Kept | Kept | Restored from the journal (see [Recovery on start](#recovery-on-start)) |
| Conversation history | Kept (cleared if `context.conversation_clear_on_stop=True`) | Cleared | Restored from the journal |
| Long-term memory | Provider stays open | Provider closed; `start()` opens a new one | Whatever the provider persists (Chroma, Mem0) |
| Secrets | Revoked on stop (unless `secrets.revoke_on_stop=False`), resolved again on start | Revoked | Resolved on start |
| Invocation count, budget counters, mission | Kept | Kept | Restored from the journal |
| Dead letters | Kept | Kept | Runs a stop cancelled are restored (with a journal); the others are not |
| Agent-added triggers (open mode) | Stopped and dropped | Dropped | Not restored |

---

## Recovery on start

With a journal, a new `AgentProcess` continues from the last checkpoint of the same process **name**. On the first `start()` of a process object, when `JournalConfig.recover_on_start` is `True` (the default) and the journal holds state for that name, the process restores the context state (the last checkpoint plus the `context_update` entries written after it; without a checkpoint, for a crash before the first run ended, the `context_update` entries alone), the mission, the budget counters (a restart can't reset the daily limits), the conversation history and the invocation count. Recovered keys replace the same keys of `context.initial_state`.

The process writes a checkpoint after every run, on `stop()` and after a rewind, so this works after a clean stop and after a crash.

```python
from promptise.runtime import AgentProcess, JournalConfig, ProcessConfig

config = ProcessConfig(
    model="openai:gpt-5-mini",
    journal=JournalConfig(path=".promptise/journal"),  # level="checkpoint"
)
process = AgentProcess("pipeline-watcher", config)
await process.start()
print(process.recovered)
# {'lifecycle_state': 'stopped', 'entries_replayed': 1, 'context_keys': ['counter'],
#  'mission_restored': False, 'budget_restored': False,
#  'conversation_messages': 2, 'invocation_count': 1}
```

`process.recovered` is `None` when nothing was restored. `lifecycle_state` is the last state the journal recorded: `"stopped"` after a clean stop, `"running"` after a crash. A journal that can't be read is logged and the process starts fresh. Set `recover_on_start=False` to always start fresh. `ProcessConfig` has no journal by default (`level="none"`); see [Journal](journal/index.md) and [Replay Engine](journal/replay.md).

---

## Trigger events and the queue

Each trigger's events go into one queue per process (1000 events). `concurrency` workers take events from it in arrival order and run the agent once per event; with `concurrency=1` (the default) runs never overlap.

```python
ProcessConfig(model="openai:gpt-5-mini", concurrency=3)  # up to 3 runs in parallel
```

`await process.inject(event)` puts a `TriggerEvent` into the queue by hand, as if a trigger had fired: it resumes an idle-suspended process, passes the `file_changed` / `task_created` hooks, and is dead-lettered (`"queue full"`) when the queue is full.

A run that raises is retried when `trigger_delivery.max_retries` is above 0 (default 0); an event that has used up its retries counts as one consecutive failure. Events blocked by the payload scan, the guardrail scanner or a `user_prompt_submit` hook are dead-lettered without counting as failures. See [Triggers](triggers/index.md) for trigger types, filters and payload handling.

### Dead letters

`process.dead_letters` lists the events that were not processed, oldest first. Each entry has `event` (the `TriggerEvent`), `reason`, `error`, `attempts` and `dead_lettered_at`. The list keeps the last `trigger_delivery.dead_letter_size` entries (default 100).

| Reason | When |
|---|---|
| `"process stopped"` | Not run yet at `stop()` (queued, held, running or still inside a trigger), or arriving while the process is stopping or stopped. A run that finished (its `task_completed` hooks may still be running) is not dead-lettered |
| `"budget: max_runs_per_day"` | The run was refused because `max_runs_per_day` was reached; redeliver it after the daily reset |
| `"process restarting"` | Held by a worker, or waiting for a retry, when an automatic restart or recycle tears the process down |
| `"trigger removed"` | Still buffered in a trigger, or held by its listener, when the trigger was removed (`remove_trigger()`, `DELETE .../triggers/{id}`, the agent's `remove_trigger`) |
| `"process failed"` | Queued when the process failed, or arriving while it is `FAILED` |
| `"retries exhausted"` | The run raised and used up `trigger_delivery.max_retries` |
| `"queue full"` | `inject()` or a retry found the queue full |
| `"trigger buffer full"` | An event or message trigger's 100-event buffer was full (also counted in the trigger's `dropped_events`) |
| `"flagged by payload scan"` | `trigger_delivery.scan_payloads` flagged the payload |
| `"blocked by guardrails"` | The guardrail scanner blocked the run |
| `"blocked by hook"` | A `user_prompt_submit` hook refused the run |

```python
for entry in process.dead_letters:
    print(entry["reason"], entry["event"].payload)

await process.start()
requeued = await process.redeliver_dead_letters()  # or event_ids=[...]
process.clear_dead_letters()
```

`redeliver_dead_letters(event_ids=None)` puts the events back on the queue and returns how many it re-queued; events that don't fit stay in the list. Like `inject()`, it wakes a process suspended for `idle_timeout`.

---

## Status

`process.status()` returns a JSON-serializable dict:

| Key | Description |
|---|---|
| `name`, `process_id` | Identity |
| `state` | Lifecycle state value, e.g. `"running"` |
| `suspend_reason` | `"manual"`, `"idle"`, `"budget"`, `"health"`, `"rewind"`, or `None` |
| `execution_mode` | `"strict"` or `"open"` |
| `invocation_count` | Completed runs (restored by journal recovery) |
| `consecutive_failures` | Current streak of failed events |
| `restart_count` | Automatic restart attempts in the current failure streak |
| `journal_enabled` | Whether the process has a journal |
| `trigger_count`, `dynamic_trigger_count` | Running config triggers, agent-added triggers |
| `triggers` | One dict per trigger: `id`, `type`, `source` (`"config"` or `"dynamic"`), for a file watch `dropped_events` and `pending_events`, and for an event or message trigger `dropped_events` |
| `custom_tool_count`, `rebuild_count`, `spawned_process_count` | Open-mode counters |
| `conversation_messages` | Messages in the conversation buffer |
| `has_memory` | Whether a long-term memory provider is configured |
| `queue_size` | Events waiting in the queue |
| `dead_letter_count` | Entries in `dead_letters` |
| `filtered_count` | Events skipped by a trigger's `filter_expression` |
| `pending_retries` | Events waiting for a retry |
| `uptime_seconds` | Seconds since the last `start()` while the process is up; `None` before the first start and while it is stopped or failed |
| `hook_count` | Hooks registered on `process.hooks` |
| `recovered` | Only after a journal recovery: the `recovered` dict |
| `budget` | Only with a budget: remaining limits |
| `health_anomalies`, `health` | Only with health monitoring: anomaly count and health status |
| `mission_state` | Only with a mission: its state |
| `active_secrets` | Only with secret scoping: number of active secrets |

Other read-only properties: `state` (`ProcessState`), `context` (the [`AgentContext`](context.md)), `lifecycle` (the [`ProcessLifecycle`](lifecycle.md)), `recovered`, `suspend_reason`, `dead_letters`, `spawned_processes` and `hooks`.

---

## Restart policies

When a running process enters `FAILED` (after `max_consecutive_failures` failed events in a row), `restart_policy` decides what happens next.

| Policy | Behavior |
|---|---|
| `"never"` | The process stays `FAILED` (default) |
| `"on_failure"` | Tear the process down and start it again, up to `max_restarts` attempts in a row |
| `"always"` | Same as `"on_failure"`, and a process that reaches `max_lifetime` is recycled (stopped and started again) instead of stopped |

```python
ProcessConfig(
    model="openai:gpt-5-mini",
    restart_policy="on_failure",
    max_restarts=5,
    restart_backoff=2.0,          # 2 s, 4 s, 8 s, ... (max 60 s)
    max_consecutive_failures=3,
)
```

How restarts behave:

- The first attempt waits `restart_backoff` seconds; each further attempt in a row doubles the wait, capped at 60 s. A restart whose `start()` fails counts as an attempt.
- The attempt count (`restart_count`) resets after a successful run, so `max_restarts` limits a streak of failures, not the process's lifetime. A restarted process also starts a fresh `max_consecutive_failures` streak. `max_lifetime` recycling doesn't count as an attempt.
- When `max_restarts` is used up, the process stays `FAILED`: a `restart_exhausted` journal entry is written and `process.restart_exhausted` is emitted.
- An explicit `stop()` never triggers a restart and cancels a pending one. Stops the process makes on purpose (budget `on_exceeded="stop"`, health `on_anomaly="stop"`, mission completion, `max_lifetime` without `"always"`) are not restarted either.
- A restart keeps the context state, conversation history, invocation count and `process_id`. Each restart shuts the old agent down and closes its MCP connections (stdio servers exit) before starting again.
- A failure of your own `start()` call is raised to you and not retried.
- Each attempt emits `process.restarted` (`attempt`, `max_restarts`); each failure emits `process.failed` with the error.

Only an event whose run raised (after its retries) counts as a failure. A tool that raises does not fail the run: the model sees the error and carries on.

---

## Live triggers

`add_trigger()` and `remove_trigger()` change a process's config triggers while it runs:

```python
trigger_id = await process.add_trigger({"type": "cron", "cron_expression": "0 * * * *"})
await process.remove_trigger(trigger_id)
```

On a `RUNNING` or `SUSPENDED` process the new trigger starts at once (its events queue while suspended); otherwise it starts with the next `start()`. Trigger IDs are stable (`<type>-<digest of the config>`), so removing one trigger leaves the others' IDs unchanged. An invalid config raises `ValueError` and nothing is added; an unknown ID raises `KeyError`. See [Triggers](triggers/index.md).

---

## Instructions, messages and hooks

- `process.update_instructions(text)` replaces the system prompt from the next run on. The agent is rebuilt before that run, reusing the connected MCP tools; no restart.
- `await process.send_message(content, ...)` and `await process.ask(content, timeout=120)` reach the agent through its inbox (`InboxConfig(enabled=True)`); both raise `RuntimeError` on a stopped or failed process. See [Orchestration API](api.md).
- `process.hooks` is the process's [`HookManager`](hooks.md). It holds the hooks from `ProcessConfig.hooks` (or a manifest's `hooks:` section); register more in code:

```python
from promptise.runtime import HookEvent

@process.hooks.on(HookEvent.SESSION_START)
async def announce(ctx):
    print(ctx.process_id, "started; restart:", ctx.data["restart"])
```

---

## Rewind

With a journal, `rewind()` rolls the context state and/or conversation history back to just before a journal entry. `rewind_plan()` previews it without changing anything.

```python
plan = await process.rewind_plan(entry_id, mode="both")
print(plan.entries_affected)
result = await process.rewind(entry_id, mode="both")  # also: conversation_only, code_only, summarize, cancel
```

A running process is suspended (reason `"rewind"`) for the duration, waits for a run in flight, and is resumed afterwards. Long-term memory, budget counters, the mission, secrets and the outside effects of tool calls are never rolled back. Without a journal it raises `RuntimeError`. From the command line: `promptise runtime rewind <process> [<entry_id>] [--mode both|conversation_only|code_only|summarize] [--plan]`. See [Rewind Engine](journal/rewind.md).

---

## Governance

Budget, health, mission, secrets and the inbox are opt-in and add no overhead while disabled. Each has its own config object on `ProcessConfig`:

| Field | Config | Page |
|---|---|---|
| `budget` | `BudgetConfig(enabled=True, ...)` | [Autonomy Budget](governance/budget.md) |
| `health` | `HealthConfig(enabled=True, ...)` | [Behavioral Health](governance/health.md) |
| `mission` | `MissionConfig(enabled=True, ...)` | [Mission Model](governance/mission.md) |
| `secrets` | `SecretScopeConfig(enabled=True, ...)` | [Secret Scoping](governance/secrets.md) |
| `inbox` | `InboxConfig(enabled=True, ...)` | [Orchestration API](api.md) |

```python
from promptise.runtime import (
    BudgetConfig, HealthConfig, JournalConfig, MissionConfig, ProcessConfig,
    SecretScopeConfig, ToolCostAnnotation, TriggerConfig,
)

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Monitor the data pipeline and respond to incidents.",
    servers={"tools": {"type": "http", "url": "http://localhost:8000/mcp"}},
    triggers=[
        TriggerConfig(type="webhook", webhook_path="/alerts", webhook_port=9090),
        TriggerConfig(type="cron", cron_expression="*/5 * * * *"),
    ],
    concurrency=2,
    restart_policy="on_failure",
    journal=JournalConfig(path="./pipeline-journal"),
    budget=BudgetConfig(
        enabled=True,
        max_tool_calls_per_run=20,
        max_cost_per_day=50.0,
        on_exceeded="pause",
        tool_costs={"restart_service": ToolCostAnnotation(cost_weight=5.0, irreversible=True)},
    ),
    health=HealthConfig(enabled=True, stuck_threshold=3, on_anomaly="escalate"),
    mission=MissionConfig(
        enabled=True,
        objective="Keep all services above 99.9% uptime.",
        success_criteria="No unresolved P1 incidents for more than 15 minutes.",
        eval_every=10,
    ),
    secrets=SecretScopeConfig(
        enabled=True,
        secrets={"api_key": "${MONITORING_API_KEY}"},
        default_ttl=3600,
    ),
)
```

!!! tip "Start with governance disabled"
    For local development, leave governance off. Turn it on one subsystem at a time as you move toward production: budget first (runaway cost), then health (stuck agents), then mission (long-running goals).

---

## Common pitfalls

- **Forgetting `enabled=True`.** Every governance config defaults to `enabled=False`; `SecretScopeConfig(secrets={...})` without `enabled=True` resolves no secrets.
- **Expecting a new process object to start empty.** With a journal, a process with the same name continues from its last checkpoint. Use a new journal path, a new name, or `recover_on_start=False` for a clean start.
- **Losing events at stop.** Events that were queued when you stopped are in `dead_letters`, not lost. Call `redeliver_dead_letters()` after `start()` if they should still run.
- **Calling `resume()` for an idle process.** Not needed: the next trigger event wakes it. `resume()` is for manual, budget and health suspensions.
- **Relying on `stop()` to free memory.** `stop()` keeps long-term memory and the conversation for the next `start()`. Use `close()` (or `async with`) when you are done with the process.
- **Leaving `restart_policy` at `"never"` in production.** A failed process then stays `FAILED` until someone restarts it.

---

## What's next

- [Process Lifecycle](lifecycle.md) -- states, transitions, `StateError` and `process.*` events
- [Runtime Manager](runtime-manager.md) -- run many processes with `AgentRuntime`
- [Configuration](configuration.md) -- every `ProcessConfig` field
- [Agent Manifests](manifests.md) -- declare a process in a `.agent` YAML file
- [Triggers](triggers/index.md) -- cron, webhook, file watch, event and message triggers
- [Runtime Hooks](hooks.md) -- run code at lifecycle points
- [Journal](journal/index.md) -- recovery and rewind

!!! tip "Learn the concept"

    - [Deploy AI Agents in Production: Long-Running Python Agents](https://www.promptise.com/guides/view/deploy-ai-agents-in-production) -- Run an agent as a long-lived process with state between runs.
    - [AI Agent Auto-Restart: Restart Policies and Timeouts](https://www.promptise.com/guides/view/ai-agent-auto-restart) -- Restart policies, backoff, restart limits, run timeouts and lifetime recycling.
    - [Pause and Resume an AI Agent: Lifecycle States Explained](https://www.promptise.com/guides/view/ai-agent-pause-resume) -- Suspend and resume a running agent and what happens to queued work.
    - [Durable AI Agents: Crash Recovery and Safe Resume](https://www.promptise.com/guides/view/durable-ai-agent-crash-recovery) -- Continue from the last checkpoint after a crash or redeploy.
    - [Autonomous AI Agent Runtime: Budgets, Crash Recovery, API](https://www.promptise.com/guides/view/autonomous-ai-agent-runtime) -- What a runtime adds around an agent: budgets, restarts and recovery.
