# Behavioral Health Monitoring

Lightweight anomaly detection that identifies stuck agents, infinite loops, empty responses, and high error rates — without making any LLM calls. All detection is pure pattern matching on tool call and response history.

## When you need it

System monitoring watches CPU and memory. Nobody watches whether the agent is actually doing what it should be doing. A stuck agent calling the same tool in a loop burns compute silently until it hits context limits. Behavioral health catches that.

## Configuration

```python
from promptise.runtime import ProcessConfig, HealthConfig, EscalationTarget

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Monitor data pipelines.",
    health=HealthConfig(
        enabled=True,
        stuck_threshold=3,       # 3 identical calls = stuck
        loop_window=20,          # Check last 20 tool calls for loops
        loop_min_repeats=2,      # 2+ repeats of a pattern = loop
        empty_threshold=3,       # 3 consecutive short responses = anomaly
        empty_max_chars=10,      # Below 10 chars = trivial response
        error_rate_threshold=0.5, # 50%+ error rate = anomaly
        on_anomaly="escalate",   # "log", "pause" or "escalate"
        cooldown=300,            # 5 min before the same anomaly is reported again
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
  on_anomaly: escalate
  cooldown: 300
  escalation:
    webhook_url: "https://hooks.slack.com/..."
```

## Detectors

### Stuck detection

Tracks `(tool_name, hash(arguments))` tuples. When the agent calls the same tool with the same arguments `stuck_threshold` times consecutively **within one invocation**, it's stuck.

Tool history starts fresh at every invocation (stuck and loop detection look for repetition inside a run). A scheduled agent that checks the same queue once per cron tick is behaving normally and is not reported as stuck.

### Loop detection

Examines the last `loop_window` tool calls for repeating patterns of any length. If a sequence like `search → read → analyze → search → read → analyze` repeats `loop_min_repeats` times, it's a loop.

### Empty response detection

Counts consecutive **final replies** (the agent's answer at the end of an invocation) shorter than `empty_max_chars`. After `empty_threshold` consecutive trivial replies, the agent likely isn't making progress. Tool results are not replies: a tool that returns `[]` because a queue is empty does not count. Reply lengths carry over between invocations.

### Recovery

After an anomaly, the next successful invocation emits a single `health.recovered` event. Further successes do not emit it again until another anomaly fires.

### Error rate detection

Sliding window error rate over the last `error_window` invocations. An invocation counts as failed when it raises and has used up its retries (`trigger_delivery.max_retries`). When the fraction of failed invocations reaches `error_rate_threshold`, something is systematically wrong.

## Enforcement actions

| Action | Behavior |
|--------|----------|
| `"log"` | Log the anomaly, continue execution |
| `"pause"` | Suspend the process |
| `"escalate"` | Fire escalation notification, then suspend |

The action runs when the invocation that produced the anomaly ends, whether the run succeeded or raised (a stuck tool loop often ends in `GraphIterationLimitError`), and however long the run went on after the anomaly was detected. An anomaly from a failed run that is only detected once the failure is recorded (`high_error_rate`) is acted on right then. Each anomaly is acted on once: a later healthy run does not pause the process again for it. Every new anomaly also emits a `health.anomaly` event (`process_name`, `anomaly_type`, `details`).

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

A suspended process keeps queueing its trigger events; `resume()` (or `POST /api/v1/processes/{name}/resume`) lets it continue. To stop a process outright on a hard limit, use the [Autonomy Budget](budget.md) with `on_exceeded="stop"`.

## API reference

### `HealthConfig`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `bool` | `False` | Enable health monitoring |
| `stuck_threshold` | `int` | `3` | Consecutive identical calls to trigger |
| `loop_window` | `int` | `20` | Tool calls to examine for loops |
| `loop_min_repeats` | `int` | `2` | Min pattern repeats to trigger |
| `empty_threshold` | `int` | `3` | Consecutive short responses to trigger |
| `empty_max_chars` | `int` | `10` | Max chars for a "trivial" response |
| `error_rate_threshold` | `float` | `0.5` | Error rate to trigger |
| `on_anomaly` | `str` | `"log"` | Action when anomaly detected |
| `cooldown` | `float` | `300` | Seconds before the same anomaly (same kind and evidence) is reported again; `0` reports every repeat (in-run guards) |
| `escalation` | `EscalationTarget \| None` | `None` | Where to notify |

### `HealthMonitor`

| Method | Description |
|--------|-------------|
| `.record_tool_call(tool_name, args)` | Record a call; return the anomaly it completes, unless the same anomaly was reported less than `cooldown` seconds ago |
| `.record_response(text)` | Record the agent's final reply for empty detection (not tool results) |
| `.record_success()` | Record a successful invocation; returns `True` once, on the first success after an anomaly |
| `.begin_invocation()` | Start a new invocation: clears the tool history used by stuck/loop detection (the runtime calls it) |
| `.record_error()` | Record a failed invocation for error-rate detection |
| `.take_new_anomalies()` | Anomalies detected since the previous call, each returned once (the runtime calls it when a run ends to apply `on_anomaly`) |
| `.anomalies` | List of all detected anomalies |
| `.latest_anomaly` | Most recent anomaly (or None) |
| `.health_status()` | Summary dict used by `process.status()` |

### `AnomalyType`

| Value | Description |
|-------|-------------|
| `STUCK` | Same tool + args repeated consecutively |
| `LOOP` | Repeating pattern in tool call sequence |
| `EMPTY_RESPONSE` | Consecutive trivial responses |
| `HIGH_ERROR_RATE` | Error rate above threshold |
