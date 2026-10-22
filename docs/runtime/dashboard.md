---
seo_title: "Agent Runtime Dashboard in the Terminal"
description: "Watch a running agent runtime in a terminal UI: process states, triggers, context state and logs, started from the CLI or Python, with its data collector."
---

# Runtime Dashboard

`RuntimeDashboard` is a full-screen terminal UI for watching a running `AgentRuntime`: process states and counters, configured triggers, each process's context state, and captured log output, refreshed several times a second. It is built on `rich` and is meant for local development and demos; for remote or scripted control use the [Orchestration API](api.md) or the [CLI](cli.md).

```bash
promptise runtime start agents/ --dashboard
```

```python
from promptise.runtime import RuntimeDashboard, RuntimeDashboardState, RuntimeDataCollector

state = RuntimeDashboardState(runtime_name="my-runtime", manifest_names=["data-watcher"])
collector = RuntimeDataCollector(runtime, state, interval=0.5)  # runtime: your AgentRuntime
collector.start()

dashboard = RuntimeDashboard(state, runtime=runtime)
dashboard.start()   # takes over the terminal
...
dashboard.stop()    # restores the terminal
collector.stop()
```

---

## Concepts

The dashboard has three parts:

1. **`RuntimeDashboardState`**: the data the dashboard shows: process, trigger and context snapshots, plus ring buffers for invocations, events, commands and journal entries.
2. **`RuntimeDataCollector`**: a background thread that reads the `AgentRuntime` every `interval` seconds and refreshes the process, trigger and context snapshots in the state. It also registers `task_created` / `task_completed` [hooks](hooks.md) on every process (for the event and run panels and the trigger fire counts) and reads each process's journal for the Logs tab.
3. **`RuntimeDashboard`**: the renderer. It draws the state with `rich.Live` in the terminal's alternate screen, reads keys in a background thread, and captures Python log output for the Logs tab.

The collector fills every panel. Trigger events and finished runs arrive through the hooks it registers (removed again by `collector.stop()`); journal entries are read from each process's journal every fourth snapshot, on the runtime's event loop. Context values are shown with secret values `[REDACTED]`, as in the journal. Create the collector on the runtime's loop (or pass `loop=`): without a loop the journal panel stays empty. `state.record_invocation()`, `state.record_event()` and `state.record_journal()` remain available to add records of your own.

---

## Tabs

Switch tabs with the number keys `1`-`7`, the left and right arrow keys, or `Tab`.

| # | Tab | What it shows |
|---|---|---|
| 1 | **Overview** | Counters (processes, running, failed, triggers, invocations, errors, throughput, uptime), a table of processes, and recent invocations (from `state.invocations`) |
| 2 | **Processes** | Per process: name, state, process ID, model, invocations, failures, triggers, queue size, concurrency, heartbeat interval, uptime. On a narrow terminal the least important columns are left out first (heartbeat, process ID, concurrency, uptime...); name, state and model always show |
| 3 | **Triggers** | Every config trigger: process, type, a configuration summary (cron expression, webhook port and path, watched path, event type or topic), fire count and last fire time (counted from `task_created`) |
| 4 | **Context** | Per process: each state key, its value (secret values redacted, cut at 50 characters), whether it is writable and how many writes it has had, plus the number of environment variables, file mounts and whether memory is on |
| 5 | **Logs** | Captured Python log records (runtime logs) and `state.journal_entries`, each in its own panel with a share of the height (newest first) |
| 6 | **Events** | `state.events` (trigger events, with a one-line payload preview) and `state.invocations` (invocation results) |
| 7 | **Commands** | The command line and the history of commands run |

Process states are color-coded:

| State | Color |
|---|---|
| `running` | Bold green |
| `starting` | Yellow |
| `created` | Dim |
| `stopped` | Red |
| `failed` | Bold red |
| `suspended` | Yellow |
| `stopping` | Yellow |
| `awaiting` | Cyan |

---

## Commands

Press `:` or `/` to type a command (the Commands tab opens), `Enter` to run it, `Esc` to cancel, `Backspace` to delete.

| Command | What it does |
|---|---|
| `help` | List the commands |
| `list` | Names of the processes in the dashboard state |
| `status` | Process count and how many are running |
| `status <name>` | One process's state, invocations and queue size |
| `start <name>`, `stop <name>`, `restart <name>`, `suspend <name>`, `resume <name>` | Lifecycle commands, run on the runtime's event loop (`restart` is `AgentRuntime.restart_process()`) |
| `inject <name> [payload_json]` | Inject a `manual` trigger event with the payload (non-JSON text becomes `{"raw": "..."}`) |

Lifecycle and inject commands are submitted to the runtime's event loop, which `dashboard.start()` captures, so call `start()` from that loop (as `promptise runtime start --dashboard` does). The command history shows `Stopping <name>...` at once; if the action then fails, a `(result)` entry with the error follows. Without a loop (or without `runtime=`), the command answers with an error and nothing runs.

---

## Starting the dashboard

### Via the CLI

```bash
# One manifest
promptise runtime start agents/watcher.agent --dashboard

# A directory of manifests
promptise runtime start agents/ --dashboard
```

The dashboard's title is the manifest file or directory name. Everything else `promptise runtime start` does still happens: the control API is served on loopback and the runtime is recorded, so `promptise runtime status` and `promptise runtime stop` work from another terminal. Press `Ctrl+C` to stop all processes and leave the dashboard. `--dashboard` can't be combined with `--detach`: the dashboard needs a terminal.

### Via Python

```python
import asyncio

from promptise.runtime import (
    AgentRuntime,
    ProcessConfig,
    RuntimeDashboard,
    RuntimeDashboardState,
    RuntimeDataCollector,
    TriggerConfig,
)


async def main():
    runtime = AgentRuntime()
    await runtime.add_process("watcher", ProcessConfig(
        model="openai:gpt-5-mini",
        instructions="Monitor pipelines.",
        triggers=[TriggerConfig(type="cron", cron_expression="*/5 * * * *")],
    ))

    state = RuntimeDashboardState(runtime_name="production", manifest_names=["watcher"])
    collector = RuntimeDataCollector(runtime, state, interval=0.5)
    dashboard = RuntimeDashboard(state, runtime=runtime)

    collector.start()
    dashboard.start()
    try:
        await runtime.run_forever()  # starts all processes; returns on Ctrl+C or SIGTERM
    finally:
        dashboard.stop()
        collector.stop()


asyncio.run(main())
```

`runtime.run_forever()` starts every process and, on `Ctrl+C` or `SIGTERM`, stops and closes them before it returns.

---

## Data types

All of these are dataclasses in `promptise.runtime._dashboard`; the three main classes are also exported from `promptise.runtime`.

### RuntimeDashboardState

| Field | Default | Description |
|---|---|---|
| `runtime_name` | `"Agent Runtime"` | Title shown in the header |
| `started_at` | now | Unix time the dashboard state was created (uptime) |
| `processes` | `{}` | `ProcessSnapshot` per process name |
| `triggers` | `[]` | `TriggerInfo` list |
| `contexts` | `{}` | `ContextSnapshot` per process name |
| `invocations` | ring buffer (200) | `InvocationLog` records |
| `events` | ring buffer (200) | `EventLog` records |
| `commands` | ring buffer (100) | `CommandResult` records |
| `journal_entries` | ring buffer (500) | Journal entry dicts |
| `total_invocations`, `total_errors`, `total_events` | `0` | Counters updated by the `record_*` methods |
| `manifest_names` | `[]` | Names of the loaded manifests |

Methods: `record_invocation(log)` (also counts errors), `record_event(event)`, `record_command(result)`, `record_journal(entry)`.

### ProcessSnapshot

| Field | Type | Description |
|---|---|---|
| `name` | `str` | Process name |
| `process_id` | `str` | Unique process ID |
| `state` | `str` | Current lifecycle state |
| `model` | `str` | LLM model identifier |
| `invocation_count` | `int` | Total invocations |
| `consecutive_failures` | `int` | Current failure streak |
| `trigger_count` | `int` | Number of running config triggers |
| `queue_size` | `int` | Pending events in the queue |
| `uptime_seconds` | `float \| None` | Time since the process started; `None` while it is not up (created, stopped, failed) |
| `concurrency` | `int` | Max concurrent invocations |
| `heartbeat_interval` | `float` | Heartbeat period |

### TriggerInfo

| Field | Type | Description |
|---|---|---|
| `process_name` | `str` | Owning process |
| `trigger_id` | `str` | The trigger's stable ID, the same as the API's (for example `cron-2721f2b845`) |
| `trigger_type` | `str` | `cron`, `webhook`, `file_watch`, `event`, `message`, ... |
| `config_summary` | `str` | Short configuration text, or `-` |
| `fire_count` | `int` | Times fired (default `0`) |
| `last_fired` | `float \| None` | Last fire time (default `None`) |

### ContextSnapshot

| Field | Type | Description |
|---|---|---|
| `process_name` | `str` | Owning process |
| `state` | `dict` | The context state |
| `writable_keys` | `list[str] \| None` | Writable keys, `None` when all are writable |
| `env_count` | `int` | Environment variables exposed to the context |
| `file_mount_count` | `int` | File mounts |
| `has_memory` | `bool` | Whether a memory provider is set |
| `history_counts` | `dict[str, int]` | Writes per key |

### Log records

| Class | Fields |
|---|---|
| `InvocationLog` | `timestamp`, `process_name`, `trigger_type`, `trigger_id`, `success`, `duration_ms`, `error` |
| `EventLog` | `timestamp`, `process_name`, `trigger_type`, `trigger_id`, `event_id`, `payload_summary` |
| `CommandResult` | `timestamp`, `command`, `result`, `success` |

---

## RuntimeDataCollector

```python
collector = RuntimeDataCollector(runtime, state, interval=0.5)
collector.start()   # daemon thread
collector.stop()    # ends the loop after the current sleep
```

Every `interval` seconds (default `0.5`) it calls `status()` on each process of `runtime.processes`, reads its config and context, rebuilds the trigger list from the config triggers (with their stable IDs and the fire counts its hooks counted), and drops snapshots of processes that are no longer in the runtime. On each new process it registers a `task_created` and a `task_completed` hook (priority `-1000`, tag `promptise-dashboard`); every fourth snapshot it reads the newest 100 journal entries of each process. `stop()` ends the loop after the current sleep and removes the hooks.

---

## API summary

| Class | Description |
|---|---|
| `RuntimeDashboardState` | Data container for everything the dashboard shows |
| `RuntimeDataCollector(runtime, state, interval=0.5, loop=None)` | Background thread that refreshes the snapshots from the runtime, registers the event/run hooks and reads the journals. `loop` defaults to the running loop when it is created |
| `RuntimeDashboard(state, runtime=None)` | Terminal renderer: `start()` (call it on the runtime's loop) takes over the terminal, `stop()` restores it. `runtime` is used by the commands |
| `ProcessSnapshot`, `TriggerInfo`, `ContextSnapshot` | Snapshots for display |
| `InvocationLog`, `EventLog`, `CommandResult` | Records for the ring buffers |

---

## Common pitfalls

- **The dashboard takes over the terminal.** It draws in the alternate screen and reads keys directly; you can't use the same terminal for other commands. Always call `dashboard.stop()` (in a `finally`) so the terminal is restored.
- **Log output moves into the Logs tab.** While the dashboard runs, the root logger is set to `DEBUG` and existing console handlers of the root, `promptise.runtime`, `httpx`, `uvicorn` and `asyncio` loggers are muted; `stop()` restores them.
- **An empty journal panel.** The collector reads the journal on the runtime's event loop; create it there (or pass `loop=`), and give the process a `journal`.
- **Starting the dashboard off the runtime's loop.** Commands then can't run and the journal panel stays empty; create the collector and call `dashboard.start()` inside the coroutine that runs the runtime.
- **Shorter intervals cost more.** The collector polls every process each `interval`; `2.0` is plenty for a quiet runtime.
- **Not with `--detach`.** The dashboard needs a foreground terminal.

---

## What's next

- [CLI Commands](cli.md): `promptise runtime start --dashboard` and the other commands
- [Runtime Manager](runtime-manager.md): the `AgentRuntime` the dashboard watches
- [Orchestration API](api.md): control the runtime while you watch it
- [Agent Context](context.md): what the Context tab shows

!!! tip "Learn the concept"

    - [Run Multiple AI Agents: One Runtime, Many Processes](https://www.promptise.com/guides/view/run-multiple-ai-agents): Run and watch several agent processes in one runtime.
    - [AI Agent Health Monitoring: Catch Silent Failures](https://www.promptise.com/guides/view/ai-agent-health-monitoring): What to watch in a running agent besides its state.
