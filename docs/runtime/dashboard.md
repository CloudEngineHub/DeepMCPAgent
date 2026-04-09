# Runtime Dashboard

The `RuntimeDashboard` provides a live, full-screen terminal UI for monitoring running agent processes in real time. Built on `rich`, it offers tabbed views covering every aspect of your runtime -- from process states and trigger activity to interactive commands.

```bash
# Start from the CLI
promptise runtime start agents/ --dashboard
```

```python
# Or programmatically
from promptise.runtime import (
    RuntimeDashboard, RuntimeDashboardState, RuntimeDataCollector,
)

state = RuntimeDashboardState(
    runtime_name="my-runtime",
    manifest_names=["data-watcher", "alert-handler"],
)
collector = RuntimeDataCollector(runtime, state, interval=0.5)
collector.start()

dashboard = RuntimeDashboard(state, runtime=runtime)
dashboard.start()
```

---

## Concepts

The dashboard replaces static CLI status output with a continuously refreshing terminal display. It consists of three layers:

1. **RuntimeDashboardState** -- a shared data structure that holds all display state (process snapshots, trigger info, logs, events).
2. **RuntimeDataCollector** -- a background thread that periodically reads from the `AgentRuntime` and updates the state.
3. **RuntimeDashboard** -- the terminal renderer that uses `rich.Live` to draw tabbed panels from the state.

---

## Dashboard Tabs

Switch between tabs using the number keys (1-7), arrow keys, or Tab.

| # | Tab | Description |
|---|---|---|
| 1 | **Overview** | Runtime summary: process count, uptime, global metrics, logo |
| 2 | **Processes** | Per-process details: state, invocations, queue depth, uptime, concurrency |
| 3 | **Triggers** | All triggers with type, configuration summary, and fire count |
| 4 | **Context** | World state keys/values, writable keys, and audit trail |
| 5 | **Logs** | Journal entries from all processes (most recent first) |
| 6 | **Events** | Trigger event log: received, processed, and queued events |
| 7 | **Commands** | Interactive command panel for process control |

---

## Starting the Dashboard

### Via CLI

The simplest way to use the dashboard is through the CLI:

```bash
# Start a single agent with the dashboard
promptise runtime start agents/watcher.agent --dashboard

# Start a directory of agents with the dashboard
promptise runtime start agents/ --dashboard
```

The `--dashboard` flag is incompatible with `--detach` (background mode).

### Via Python

For programmatic control, create the dashboard components manually:

```python
import asyncio
from promptise.runtime import AgentRuntime, ProcessConfig, TriggerConfig
from promptise.runtime._dashboard import (
    RuntimeDashboard,
    RuntimeDashboardState,
    RuntimeDataCollector,
)

async def main():
    runtime = AgentRuntime()
    await runtime.add_process("watcher", ProcessConfig(
        model="openai:gpt-5-mini",
        instructions="Monitor pipelines.",
        triggers=[TriggerConfig(type="cron", cron_expression="*/5 * * * *")],
    ))
    await runtime.start_all()

    # Create dashboard state
    state = RuntimeDashboardState(
        runtime_name="production",
        manifest_names=["watcher"],
    )

    # Start data collector (reads from runtime every 0.5s)
    collector = RuntimeDataCollector(runtime, state, interval=0.5)
    collector.start()

    # Start dashboard (blocks the terminal)
    dashboard = RuntimeDashboard(state, runtime=runtime)
    dashboard.start()

    try:
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        dashboard.stop()
        collector.stop()
        await runtime.stop_all()

asyncio.run(main())
```

---

## Data Types

The dashboard uses several snapshot dataclasses for display:

### ProcessSnapshot

| Field | Type | Description |
|---|---|---|
| `name` | `str` | Process name |
| `process_id` | `str` | Unique process ID |
| `state` | `str` | Current lifecycle state |
| `model` | `str` | LLM model identifier |
| `invocation_count` | `int` | Total invocations |
| `consecutive_failures` | `int` | Current failure streak |
| `trigger_count` | `int` | Number of configured triggers |
| `queue_size` | `int` | Pending events in queue |
| `uptime_seconds` | `float \| None` | Time since process started |
| `concurrency` | `int` | Max concurrent invocations |
| `heartbeat_interval` | `float` | Heartbeat period |

### TriggerInfo

| Field | Type | Description |
|---|---|---|
| `process_name` | `str` | Owning process |
| `trigger_id` | `str` | Trigger identifier |
| `trigger_type` | `str` | `cron`, `webhook`, `event`, etc. |
| `config_summary` | `str` | Human-readable config |
| `fire_count` | `int` | Times fired |
| `last_fired` | `float \| None` | Last fire timestamp |

---

## State Colors

Process states are color-coded in the dashboard:

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

## RuntimeDashboardState

The shared data structure that holds all display information:

```python
from promptise.runtime._dashboard import RuntimeDashboardState

state = RuntimeDashboardState(
    runtime_name="my-runtime",
    manifest_names=["watcher", "handler"],
)
```

The `RuntimeDataCollector` populates this state periodically, and the `RuntimeDashboard` reads from it to render the display.

---

## RuntimeDataCollector

A background thread that reads from the `AgentRuntime` and updates the `RuntimeDashboardState`:

```python
from promptise.runtime._dashboard import RuntimeDataCollector

collector = RuntimeDataCollector(runtime, state, interval=0.5)
collector.start()
# ... dashboard runs ...
collector.stop()
```

The `interval` parameter controls the refresh rate in seconds (default: 0.5).

---

## API Summary

| Class | Description |
|---|---|
| `RuntimeDashboardState` | Shared data container for all dashboard display state |
| `RuntimeDataCollector` | Background thread that populates state from the runtime |
| `RuntimeDashboard` | Terminal renderer using `rich.Live` |
| `ProcessSnapshot` | Process status snapshot for display |
| `TriggerInfo` | Trigger configuration and fire status |
| `InvocationLog` | Record of a single agent invocation |
| `EventLog` | Record of a trigger event received |
| `CommandResult` | Record of a command executed via the dashboard |

---

## Tips and Gotchas

!!! tip "Requires rich>=13"
    The dashboard depends on `rich`, which is already a framework dependency. No extra installation is needed.

!!! tip "Adjust collector interval"
    For high-throughput runtimes, use a shorter interval (e.g., `0.25`). For low-activity runtimes, a longer interval (e.g., `2.0`) reduces overhead.

!!! warning "Dashboard blocks the terminal"
    The dashboard takes over the terminal with `rich.Live`. You cannot use the same terminal for other commands while it is running. Use `Ctrl+C` to exit.

!!! warning "Not compatible with --detach"
    The `--dashboard` flag requires a foreground terminal. It cannot be combined with `--detach` for background operation.

---

## What's Next

- [CLI Commands](cli.md) -- all runtime CLI commands including `--dashboard`
- [Runtime Manager](runtime-manager.md) -- the `AgentRuntime` that powers the dashboard
