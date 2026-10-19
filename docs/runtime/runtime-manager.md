# Runtime Manager

`AgentRuntime` runs several agent processes together in one Python process. It keeps a registry of named `AgentProcess` instances, starts and stops them together or one by one, shares an event notifier, hooks and an event bus between them, and shuts everything down cleanly on `SIGTERM` or Ctrl+C. Use it when you have more than one agent, or when one agent should run as a long-lived service.

```python
import asyncio

from promptise.runtime import AgentRuntime, ProcessConfig, RuntimeConfig, TriggerConfig

config = RuntimeConfig(
    processes={
        "watcher": ProcessConfig(
            model="openai:gpt-5-mini",
            instructions="You monitor data pipelines and report anomalies.",
            triggers=[TriggerConfig(type="cron", cron_expression="*/5 * * * *")],
            restart_policy="on_failure",
        ),
        "responder": ProcessConfig(
            model="openai:gpt-5-mini",
            instructions="You handle alerts.",
        ),
    },
)


async def main():
    runtime = AgentRuntime(config)  # registers "watcher" and "responder"
    await runtime.run_forever()     # starts both; stops cleanly on SIGTERM / Ctrl+C


asyncio.run(main())
```

---

## Creating a runtime

```python
AgentRuntime(
    config=None,           # RuntimeConfig: processes to register, distributed settings
    *,
    event_bus=None,        # shared bus for "event" triggers
    broker=None,           # shared broker for "message" triggers
    event_notifier=None,   # EventNotifier shared by every process
    hooks=None,            # HookManager whose hooks fire for every process
)
```

The processes in `config.processes` are registered when the runtime is created, in `CREATED` state; nothing starts until you call `start_all()`, `start_process()` or `run_forever()`. `await AgentRuntime.from_config(config, ...)` takes the same arguments and is equivalent.

```python
runtime = AgentRuntime(config)
print(runtime.list_processes())
# [{'name': 'watcher', 'state': 'created', 'process_id': '67b6...'},
#  {'name': 'responder', 'state': 'created', 'process_id': '86e2...'}]
```

### Shared resources

- **`event_notifier`**: every process emits its `process.*`, `invocation.*` and governance events through it. A process never stops a shared notifier; `stop_all()` drains it after the last process has stopped, so every `process.stopped` is delivered before `stop_all()` returns.
- **`hooks`**: a [`HookManager`](hooks.md) whose hooks run for every process's lifecycle events, after each process's own `hooks`. `ctx.process_id` names the process.
- **`event_bus`** and **`broker`**: objects you provide; the runtime does not create them. `event` triggers call `await event_bus.subscribe(event_type, handler)` and `await event_bus.unsubscribe(event_type, handler)`; `message` triggers call `await broker.subscribe(topic, handler)` (which returns a subscription ID) and `await broker.unsubscribe(subscription_id)`. See [Event & Webhook triggers](triggers/event-webhook.md).

---

## Registering processes

```python
from promptise.runtime import ProcessConfig, TriggerConfig

process = await runtime.add_process(
    "data-watcher",
    ProcessConfig(
        model="openai:gpt-5-mini",
        instructions="You monitor data pipelines.",
        triggers=[TriggerConfig(type="cron", cron_expression="*/5 * * * *")],
    ),
)
```

`add_process(name, config, *, process_id=None)` returns the new `AgentProcess` (in `CREATED` state). Names are unique; a duplicate raises `RuntimeBaseError`:

```text
Process 'data-watcher' already exists. Use remove_process() first or choose a different name.
```

`await runtime.remove_process(name)` works in every state. It stops the process (a `FAILED` one too, so its triggers, listeners and heartbeat end), closes it (long-term memory closed, secrets revoked, conversation cleared) and removes it from the registry. An unknown name raises `KeyError`.

### Loading manifests

```python
process = await runtime.load_manifest("agents/watcher.agent")
process = await runtime.load_manifest("agents/watcher.agent", name_override="watcher-2")

names = await runtime.load_directory("agents/")  # every *.agent file, in name order
```

`load_directory()` raises `ManifestError` when the path is not a directory, and stops at the first manifest that fails to load (re-raising its error); the manifests loaded before it stay registered. See [Agent Manifests](manifests.md).

---

## Starting and stopping

| Method | What it does |
|---|---|
| `await start_all()` | Starts every `CREATED`, `STOPPED` and `FAILED` process. A process that fails to start is logged and skipped. With `distributed.enabled`, the cluster node starts first, and its failure raises. |
| `await stop_all()` | Stops every process that is not `STOPPED` or `CREATED` (`FAILED` and `SUSPENDED` ones too), then drains the shared event notifier |
| `await start_process(name)` | `get_process(name).start()` |
| `await stop_process(name)` | `get_process(name).stop()` |
| `await restart_process(name)` | Stops a running or suspended process, then starts it. A `FAILED` process is torn down by `start()` itself (old triggers, listeners and heartbeat end first), so nothing fires twice. Context state, long-term memory and conversation history carry over. |
| `await shutdown()` | `stop_all()`, then closes every process (memory, secrets, conversation), then stops the cluster node. The processes stay registered. |

Each method that takes a name raises `KeyError` for an unknown one.

### run_forever()

`run_forever()` is the entry point for a service. It starts every process, waits until shutdown is requested, then calls `shutdown()`, so every process gets its `process.stopped` event, a final journal checkpoint and its dead letters before the program exits.

```python
await runtime.run_forever()                               # handles SIGTERM and SIGINT
await runtime.run_forever(install_signal_handlers=False)  # you handle signals
```

It returns after:

- `SIGTERM` (`docker stop`, Kubernetes, systemd) or `SIGINT` (Ctrl+C), when `install_signal_handlers=True` (the default). The handlers are installed on the running loop for the duration of the call and removed afterwards. Where the loop can't install them (Windows, or a thread other than the main one) they are skipped.
- `runtime.request_shutdown()`, which is safe to call from your own signal handler or another task.
- Cancellation of the task running it.

```text
AgentRuntime: received SIGTERM, shutting down
```

### async with

`async with AgentRuntime(...) as runtime:` calls `shutdown()` on exit, even when the block raises. It does not install signal handlers; a `SIGTERM` kills the program without a clean stop unless you use `run_forever()` or call `request_shutdown()` from your own handler.

```python
async with AgentRuntime() as runtime:
    await runtime.add_process("watcher", config)
    await runtime.start_all()
    await asyncio.sleep(60)
# every process is stopped and closed here
```

From the command line, `promptise runtime start agents/` runs a runtime with the same clean shutdown on `SIGTERM` and Ctrl+C, and `--detach` runs it in the background. See [CLI Commands](cli.md).

---

## Running several processes

Each process has its own agent, triggers, queue, context, conversation and journal entries. One process failing, restarting or being suspended does not affect the others, and `start_all()` keeps starting the rest when one fails to start.

All processes share one asyncio event loop. Agent runs and tool calls are async, but a blocking call (a synchronous HTTP request, `time.sleep()`) in a tool or callback stalls every process until it returns. Each webhook trigger opens its own HTTP server, so give each one its own port.

```python
for name, info in runtime.status()["processes"].items():
    print(name, info["state"], info["invocation_count"], info["restart_count"])
```

---

## Status and monitoring

```python
runtime.status()
# {"process_count": 2,
#  "processes": {"watcher": {...process status...}, "responder": {...}},
#  "cluster": {...}}            # only in distributed mode

runtime.process_status("watcher")  # one process's status() dict
runtime.list_processes()           # [{"name", "state", "process_id"}, ...]
runtime.get_process("watcher")     # the AgentProcess (KeyError if unknown)
runtime.processes                  # dict copy: name -> AgentProcess
print(runtime)                     # AgentRuntime(processes=2, 2 running)
```

The per-process fields are listed under [Status](processes.md#status).

---

## Distributed mode and presets

With `RuntimeConfig(distributed=DistributedConfig(enabled=True, ...))`, `start_all()` and `run_forever()` first start this node's management transport and then, depending on the config, the cluster coordinator or the registration with one. `shutdown()` stops them, and `runtime.cluster` is the running `ClusterNode` (or `None`). See [Distributed Coordinator](distributed/coordinator.md) and [Discovery & Transport](distributed/discovery-transport.md).

`DEFAULT_DEVELOPMENT_CONFIG` and `DEFAULT_PRODUCTION_CONFIG` are identical: no processes, distributed mode off. Production hardening is set per process (`journal`, `restart_policy`, `health`, `budget`, `trigger_delivery`), and distributed mode needs your cluster's coordinator and token, so no preset turns either on.

---

## API summary

| Method / property | Description |
|---|---|
| `AgentRuntime(config=None, *, event_bus, broker, event_notifier, hooks)` | Create a runtime; registers `config.processes` |
| `await AgentRuntime.from_config(config, ...)` | Same as the constructor |
| `await add_process(name, config, *, process_id=None)` | Register a process; returns it |
| `await remove_process(name)` | Stop, close and unregister a process |
| `get_process(name)` | The `AgentProcess` |
| `processes` | Dict copy of all processes |
| `list_processes()` | `name`, `state`, `process_id` per process |
| `await load_manifest(path, *, name_override=None)` | Register a process from a `.agent` file |
| `await load_directory(path)` | Register every `*.agent` file in a directory |
| `await start_all()` / `await stop_all()` | Start / stop every process |
| `await start_process(name)` / `await stop_process(name)` / `await restart_process(name)` | Control one process |
| `await shutdown()` | Stop and close every process, stop the cluster node |
| `await run_forever(*, install_signal_handlers=True)` | Start everything and run until a signal or `request_shutdown()` |
| `request_shutdown()` | Make `run_forever()` return |
| `status()` / `process_status(name)` | Status dicts |
| `cluster` | The running `ClusterNode`, or `None` |
| `await start_distributed()` / `await stop_distributed()` | Start / stop the cluster node (`start_all()` and `shutdown()` call them) |

---

## Common pitfalls

- **Not checking after `start_all()`.** A process that fails to start is only logged. Check `status()` (or watch for `process.failed`) to see that every process reached `running`.
- **Using `async with` in a container.** It does not handle `SIGTERM`; `docker stop` then kills the program mid-run. Use `run_forever()`.
- **Reusing a name after a failure.** `add_process()` with an existing name raises. `remove_process()` first, or use `restart_process()`.
- **Blocking the event loop.** A synchronous call in one agent's tool pauses every process in the runtime.
- **Two webhook triggers on one port.** Each webhook trigger binds its own server, so two of them on the same port conflict.
- **Expecting the production preset to change behavior.** `DEFAULT_PRODUCTION_CONFIG` is the same as the development preset; configure each process instead.

---

## What's next

- [Agent Processes](processes.md) -- what each process does on start, stop and restart
- [Process Lifecycle](lifecycle.md) -- states, transitions and `process.*` events
- [Configuration](configuration.md) -- `RuntimeConfig`, `ProcessConfig` and every sub-config
- [Orchestration API](api.md) -- manage a running runtime over HTTP
- [CLI Commands](cli.md) -- `promptise runtime start`, `status`, `stop` and `restart`

!!! tip "Learn the concept"

    - [Run Multiple AI Agents: One Runtime, Many Processes](https://www.promptise.com/guides/view/run-multiple-ai-agents) -- Add and remove processes, load manifests, contain failures and shut down cleanly on SIGTERM.
    - [AI Agent Auto-Restart: Restart Policies and Timeouts](https://www.promptise.com/guides/view/ai-agent-auto-restart) -- Restart policies, backoff and an alert when the runtime gives up.
    - [Autonomous AI Agent Runtime: Budgets, Crash Recovery, API](https://www.promptise.com/guides/view/autonomous-ai-agent-runtime) -- What a runtime adds around an agent: budgets, restarts and crash recovery.
