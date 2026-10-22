---
seo_title: "Behavioral Health Monitoring for AI Agents"
description: "Behavioral health monitoring catches agents that are stuck, looping, giving empty replies or failing repeatedly, using pattern matching, with no LLM calls."
---

# Behavioral Health Monitoring

Behavioral health monitoring catches an agent that runs but no longer does useful work: stuck on the same tool call, looping through the same sequence, giving empty replies, or failing run after run. Detection is pure pattern matching on the tool calls, final replies and run outcomes the process records; it makes no LLM calls. When it finds an anomaly, the process logs it, pauses, escalates or stops, as you configure.

```python
from promptise.runtime import ProcessConfig, HealthConfig

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Monitor data pipelines.",
    health=HealthConfig(enabled=True, on_anomaly="pause"),
)
```

## When you need it

System monitoring watches CPU and memory. Nobody watches whether the agent is actually doing what it should be doing. A stuck agent calling the same tool in a loop burns compute silently until it hits context limits, and a scheduled agent that answers "ok" to every run looks healthy from the outside. Behavioral health catches both.

## Configuration

```python
from promptise.runtime import ProcessConfig, HealthConfig, EscalationTarget

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Monitor data pipelines.",
    health=HealthConfig(
        enabled=True,
        stuck_threshold=3,        # 3 identical calls in a row = stuck
        loop_window=20,           # check the last 20 tool calls for loops
        loop_min_repeats=2,       # a sequence repeated 2+ times = loop
        empty_threshold=3,        # 3 trivial final replies in a row = anomaly
        empty_max_chars=10,       # a reply of at most 10 chars is trivial
        error_window=10,          # error rate over the last 10 runs
        error_rate_threshold=0.5, # 50%+ failed runs = anomaly
        on_anomaly="escalate",    # "log", "pause", "escalate" or "stop"
        cooldown=300,             # 5 min before the same anomaly is reported again
        escalation=EscalationTarget(
            webhook_url="https://hooks.slack.com/...",
        ),
    ),
)
```

Or in a `.agent` manifest:

```yaml
name: pipeline-monitor
health:
  enabled: true
  stuck_threshold: 3
  loop_window: 20
  error_window: 10
  on_anomaly: escalate
  cooldown: 300
  escalation:
    webhook_url: "https://hooks.slack.com/..."
```

An unknown key under `health:` is rejected in a manifest; in Python, `HealthConfig` ignores it with a `FutureWarning`.

## How the process feeds the monitor

| Signal | Recorded when |
|---|---|
| Tool call (name + hash of the arguments) | Each tool call starts, through the process's callback handler |
| Final reply length | A run finishes without raising; the agent's final answer, not tool results |
| Run success | A run finishes without raising |
| Run failure | A run raised and used up its retries (`trigger_delivery.max_retries`) |

Tool history starts fresh at every run (stuck and loop detection look for repetition inside a run). Reply lengths and run outcomes carry over between runs. Runs blocked by a guardrail, a hook or the payload scan are not recorded.

## Detectors

### Stuck detection

When the agent calls the same tool with the same arguments `stuck_threshold` times in a row **within one run**, it's stuck. A scheduled agent that checks the same queue once per cron tick is behaving normally and is not reported.

`details`: `tool_name`, `consecutive_count`.

### Loop detection

Examines the last `loop_window` tool calls of the run (at least 4) for a repeating sequence of two or more calls. If a sequence like `search → read → analyze → search → read → analyze` repeats `loop_min_repeats` times back to back, it's a loop.

`details`: `pattern` (tool names), `repeat_count`, `pattern_length`.

### Empty response detection

Counts consecutive **final replies** of at most `empty_max_chars` characters after stripping whitespace. With the default 10, a 10-character reply counts as empty and an 11-character one doesn't; `0` counts only blank replies. After `empty_threshold` such replies in a row, the anomaly fires. Tool results are not replies: a tool that returns `[]` because a queue is empty does not count.

`details`: `consecutive_count`, `max_chars`, `lengths`.

### Error rate detection

Once `error_window` run outcomes are recorded, the error rate is the share of failed runs among the last `error_window`. It fires when the rate is at or above `error_rate_threshold`. Before the window is full, nothing fires, so with the defaults a process needs 10 recorded runs first.

`details`: `error_rate`, `error_count`, `window_size`, `threshold`.

## Enforcement actions

| `on_anomaly` | Behavior |
|---|---|
| `"log"` (default) | Log the anomaly and emit `health.anomaly`; the process keeps running |
| `"pause"` | Suspend the process (`suspend_reason == "health"`) |
| `"escalate"` | Send the escalation notification when `escalation` is set, then suspend the process. Without `escalation` it still suspends |
| `"stop"` | Stop the process |

The action runs once, when the run that produced the anomaly ends, whether the run succeeded or raised (a stuck tool loop often ends in `GraphIterationLimitError`), and however long the run went on after the anomaly was detected. An anomaly detected when a failure is recorded (`high_error_rate`) is acted on right then. Each anomaly is handled once: a later healthy run does not pause the process again for it. `pause` and `escalate` suspend only a `RUNNING` process.

A process suspended for health does not resume by itself. Its trigger events keep queueing; `resume()` (or `POST /api/v1/processes/{name}/resume`) lets it continue in order. To stop a process outright on a hard limit, use `on_anomaly="stop"` or the [Autonomy Budget](budget.md) with `on_exceeded="stop"`.

The escalation is a POST to `escalation.webhook_url` and/or an `escalation.event_type` event on the runtime's event bus, with the latest anomaly of the run:

```json
{
  "type": "health_anomaly",
  "process_id": "0c8e5d0a-6b8f-4c56-9a52-0f4f3c1e9b7d",
  "process_name": "pipeline-monitor",
  "anomaly_type": "stuck",
  "details": {"tool_name": "search", "consecutive_count": 3}
}
```

The webhook URL must resolve to a public address; see [escalation delivery](budget.md#enforcement-actions) for the SSRF rules, which apply here too.

## Events

| Event | Severity | Payload |
|---|---|---|
| `health.anomaly` | warning | `process_name`, `anomaly_type` (`stuck`, `loop`, `empty_response`, `high_error_rate`), `details` |
| `health.recovered` | info | `process_name` |

Events reach the runtime's [event notifier](../../core/events.md) sinks; their metadata carries `process_name`, `process_id` and the model. `health.anomaly` is emitted for every new anomaly, whatever `on_anomaly` is.

`health.recovered` is emitted once per incident: on the first run after an anomaly that finishes without raising. That can be the same run that produced a stuck or loop anomaly, if that run then finishes cleanly; the `health.recovered` event is then emitted before the `health.anomaly` event of that run. The reply of the recovering run may itself still be empty.

## Cooldown

`cooldown` (default 300 s) stops the same anomaly from being reported over
and over, so a stuck agent does not send an escalation per call. "The same"
means the same kind **and** the same evidence:

| Anomaly | Same anomaly when |
|---------|-------------------|
| `STUCK` | the same tool with the same arguments |
| `LOOP` | the same repeating sequence, whichever call it is caught at (`A, B` and `B, A` are one loop) |
| `EMPTY_RESPONSE`, `HIGH_ERROR_RATE` | always (one signal per process) |

A different stuck call or a different loop is a new anomaly and is reported
at once, also within the cooldown and in the same run. The cooldown carries
over between invocations: a scheduled agent that gets stuck on the same call
in every run reports it once per `cooldown`.

`clear_anomalies()` (and `DELETE /api/v1/processes/{name}/health/anomalies`) forgets the anomaly history **and resets the cooldowns**: after a clear, the next occurrence of any anomaly is reported and acted on at once.

## Reading health status

```python
process.status()["health"]
# {'is_healthy': False,
#  'anomaly_count': 1,
#  'anomalies_by_type': {'stuck': 1, 'loop': 0, 'empty_response': 0, 'high_error_rate': 0},
#  'latest_anomaly': {'type': 'stuck',
#                     'description': "Agent stuck: tool 'search' called 3 times consecutively with identical arguments",
#                     'timestamp': '2026-10-19T20:21:57.600765+00:00'}}

process.status()["health_anomalies"]  # 1: anomalies in the history
```

`status()["health"]` is `HealthMonitor.health_status()`:

| Key | Meaning |
|---|---|
| `is_healthy` | `True` when no anomaly was reported within the last `cooldown` seconds |
| `anomaly_count` | Anomalies reported since the start or the last clear |
| `anomalies_by_type` | The same count per type |
| `latest_anomaly` | `type`, `description` and ISO `timestamp` of the newest one, or `None` |

Both keys are present only when health monitoring is enabled. `GET /api/v1/processes/{name}` returns the same status over HTTP.

The anomaly history keeps the latest 1000 anomalies (`HealthMonitor.MAX_ANOMALIES`), oldest dropped first; `anomaly_count` counts all of them.

Over the [orchestration API](../api.md):

| Route | Response |
|---|---|
| `GET /api/v1/processes/{name}/health/anomalies` | `{"name", "health_enabled": true, "anomalies": [{"type", "description", "timestamp"}], "count"}`; `{"health_enabled": false, "anomalies": []}` when monitoring is off |
| `DELETE /api/v1/processes/{name}/health/anomalies` | `{"name", "anomalies_cleared": n}`; `400 HEALTH_DISABLED` when monitoring is off |
| `PATCH /api/v1/processes/{name}/health` | `{"name", "health_updated": true, "live": true, "effective_from": "immediate"}` |

## Changing the configuration while it runs

Every `HealthConfig` field can change on a running process, through `PATCH /api/v1/processes/{name}/health` or the client:

```python
async with OrchestrationClient("http://127.0.0.1:9100", token=TOKEN) as client:
    await client.update_health("pipeline-monitor", error_window=20, on_anomaly="pause")
```

The patch is validated as a whole before anything changes (`422 UNKNOWN_FIELD` or `422 INVALID_CONFIG` otherwise) and applies at the next recorded signal:

- **Thresholds and actions** apply to the next check.
- **Window sizes** (`error_window`, `loop_window`, `stuck_threshold`, `empty_threshold`) resize the monitor's windows and keep the most recent data. Growing `error_window` from 10 to 20 keeps the 10 outcomes recorded so far; the error rate is checked again once 20 are recorded.
- **`enabled`** builds the monitor (`true`) or drops it with its history (`false`).

In Python, change `process.config.health` and call `process.apply_governance_config("health")`, or give a standalone monitor a new config with `monitor.apply_config(config)`. A monitor also picks up window-size changes made directly on the config object it reads.

The monitor's history lives in memory. It survives `stop()` / `start()` and `restart_process()` of the same process, but it is not written to the journal, so a new process object (a new runtime, a redeploy) starts with an empty history.

## As an in-run guard

`HealthMonitor` also works on its own, outside `AgentProcess`, as a guard
inside a run: a `pre_tool` [engine hook](../../core/engine-hooks.md) records
every call and blocks the one that keeps the agent stuck. Set `cooldown=0`
for this. With the default cooldown, `record_tool_call()` returns the anomaly
only the first time, so the guard would block the third identical call and
let every later one through.

```python
from promptise.runtime import HealthConfig, HealthMonitor


class LoopGuard:
    """Blocks a tool call while the agent is repeating itself."""

    def __init__(self) -> None:
        # cooldown=0: report every call that continues the anomaly
        self.monitor = HealthMonitor(
            HealthConfig(enabled=True, stuck_threshold=3, cooldown=0),
            process_id="my-agent",
        )

    async def pre_tool(self, tool_name, args, state):
        anomaly = await self.monitor.record_tool_call(tool_name, args)
        if anomaly:
            raise RuntimeError(
                f"{anomaly.description}. The result will not change in this run: "
                "stop calling it and continue with what you have."
            )
        return args
```

Pass it in `hooks=[LoopGuard()]` of a `PromptGraphEngine`. The error goes back
to the model as the tool's result. Use one guard per run, or call
`monitor.begin_invocation()` at the start of each run, because stuck and loop
detection look at the calls of one run.

## Common pitfalls

- **`on_anomaly="escalate"` with no target.** The process is suspended, but no one is notified. Set `escalation` with a `webhook_url` or an `event_type`.
- **A suspended process doesn't resume itself.** After `pause` or `escalate`, resume it once you have looked at the cause; until then its events queue up.
- **Sparse schedules and the error window.** An agent that runs twice a day needs five days to fill the default `error_window=10`. Use a smaller window for rare runs.
- **Short legitimate replies.** An agent whose normal answer is "OK" or "none" trips the empty-response detector. Lower `empty_max_chars` (or set `0`) for such agents.
- **`cooldown=0` in a process.** Every repeat is reported, so `is_healthy` turns `True` again right away and a stuck agent sends one escalation per run.
- **Clearing anomalies resets cooldowns.** After `DELETE /health/anomalies`, an ongoing problem is reported (and acted on) again on its next occurrence.

## API reference

### `HealthConfig`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `bool` | `False` | Enable health monitoring |
| `stuck_threshold` | `int` (`>= 2`) | `3` | Consecutive identical calls (same tool and arguments) that count as stuck |
| `loop_window` | `int` (`>= 4`) | `20` | Recent tool calls examined for loops |
| `loop_min_repeats` | `int` (`>= 2`) | `2` | Back-to-back repeats of a sequence that count as a loop |
| `empty_threshold` | `int` (`>= 2`) | `3` | Consecutive trivial final replies that count as an anomaly |
| `empty_max_chars` | `int` (`>= 0`) | `10` | A final reply of at most this many characters (after stripping whitespace) is trivial |
| `error_window` | `int` (`>= 2`) | `10` | Recent runs the error rate is computed over; checked once the window is full |
| `error_rate_threshold` | `float` (`> 0`, `<= 1`) | `0.5` | Error rate at or above which the anomaly fires |
| `on_anomaly` | `"log" \| "pause" \| "escalate" \| "stop"` | `"log"` | Action when a run produced an anomaly |
| `cooldown` | `float` (`>= 0`) | `300.0` | Seconds before the same anomaly (same kind and evidence) is reported again; `0` reports every repeat (in-run guards) |
| `escalation` | `EscalationTarget \| None` | `None` | Where `"escalate"` sends its notification |

### `HealthMonitor`

`HealthMonitor(config, process_id)`

| Member | Description |
|--------|-------------|
| `.record_tool_call(tool_name, args)` | Record a call; return the anomaly it completes, unless the same anomaly was reported less than `cooldown` seconds ago |
| `.record_response(text)` | Record the agent's final reply for empty detection (not tool results) |
| `.record_success()` | Record a successful run; returns `True` once, on the first success after an anomaly |
| `.record_error()` | Record a failed run for error-rate detection; returns the anomaly, if any |
| `.begin_invocation()` | Start a new run: clears the tool history used by stuck/loop detection (the runtime calls it) |
| `.take_new_anomalies()` | Anomalies detected since the previous call, each returned once (the runtime calls it when a run ends to apply `on_anomaly`) |
| `.apply_config(config)` | Switch to another `HealthConfig`, resizing the windows and keeping data, history and cooldowns |
| `.clear_anomalies()` | Forget the history, counts and cooldowns; returns how many anomalies were cleared |
| `.health_status()` | The summary dict shown as `process.status()["health"]` |
| `.config` | The `HealthConfig` the monitor reads |
| `.anomalies` | Detected anomalies, oldest first (the latest `MAX_ANOMALIES`) |
| `.latest_anomaly` | Most recent anomaly, or `None` |
| `.tool_history` | The run's recorded calls as `(tool_name, args_hash)` |
| `.to_dict()` / `HealthMonitor.from_dict(data, config)` | Serialize and restore the windows and counts |
| `MAX_ANOMALIES` | `1000`, the size of the anomaly history |

### `Anomaly`

| Field | Description |
|-------|-------------|
| `anomaly_type` | An `AnomalyType` |
| `description` | Human-readable explanation |
| `timestamp` | When it was detected (UTC) |
| `details` | Evidence; keys per detector above |

### `AnomalyType`

A `str` enum; the value is what events and the API show.

| Member | Value | Description |
|-------|-------|-------------|
| `STUCK` | `"stuck"` | Same tool + args repeated consecutively |
| `LOOP` | `"loop"` | Repeating pattern in the tool call sequence |
| `EMPTY_RESPONSE` | `"empty_response"` | Consecutive trivial final replies |
| `HIGH_ERROR_RATE` | `"high_error_rate"` | Error rate at or above the threshold |

## See also

- [Autonomy Budget](budget.md): hard limits on tool calls, runs and cost
- [Mission](mission.md): goal tracking with an LLM judge
- [Orchestration API](../api.md): read anomalies and patch the config over HTTP
- [Events & Notifications](../../core/events.md): deliver `health.*` events to webhooks and logs
- [Triggers Overview](../triggers/index.md#what-happens-when-the-process-is-suspended): what happens to events while a process is suspended

!!! tip "Learn the concept"

    - [AI Agent Stuck in a Loop? How to Detect and Stop It](https://www.promptise.com/guides/view/ai-agent-stuck-in-loop): How agent loops get stuck and how to stop them.
    - [AI Agent Health Monitoring: Catch Silent Failures](https://www.promptise.com/guides/view/ai-agent-health-monitoring): Empty replies, rising error rates and when to log, pause or escalate.
