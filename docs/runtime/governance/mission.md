# Mission-Oriented Process Model

Transforms agents from task-runners into mission-driven processes with LLM-as-judge evaluation, confidence thresholds, escalation, and automatic completion.

## When you need it

Standard agents run on a trigger, do something, stop. A mission-oriented agent runs until a goal is achieved — checking database migrations, monitoring a deployment, researching a topic until a quality bar is met. The agent accumulates context across invocations and the runtime evaluates whether the mission is complete.

## Configuration

```python
from promptise.runtime import ProcessConfig, MissionConfig, EscalationTarget

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Migrate all tables to the new schema.",
    mission=MissionConfig(
        enabled=True,
        objective="Migrate all database tables to v2 schema",
        success_criteria="All tables pass v2 schema validation with zero errors",
        eval_every=3,              # Evaluate every 3 invocations
        confidence_threshold=0.7,  # Pause + escalate below this
        timeout_hours=24,          # Fail after 24h
        max_invocations=50,        # Fail after 50 invocations
        auto_complete=True,        # Stop on success
        eval_model="openai:gpt-5-mini",  # Separate model for eval
        escalation=EscalationTarget(
            webhook_url="https://hooks.slack.com/...",
        ),
    ),
)
```

Or in a `.agent` manifest:

```yaml
name: schema-migrator
model: openai:gpt-5-mini
instructions: Migrate all tables to the new schema.
mission:
  enabled: true
  objective: "Migrate all database tables to v2 schema"
  success_criteria: "All tables pass v2 schema validation"
  eval_every: 3
  confidence_threshold: 0.7
  timeout_hours: 24
  auto_complete: true
  escalation:
    webhook_url: "https://hooks.slack.com/..."
```

## How it works

### State machine

Each mission (`MissionState`) is in one of four states:

| State | Meaning |
|-------|---------|
| `active` | The agent is working on the mission and evaluations run (the initial state) |
| `paused` | An evaluation was below `confidence_threshold`: no further evaluations until the mission is resumed (`POST /api/v1/processes/{name}/mission/resume`); the process keeps running its invocations |
| `completed` | Success criteria met; further invocations are skipped |
| `failed` | `timeout_hours` or `max_invocations` reached, or `fail()` called; further invocations are skipped |

### Evaluation cycle

Every `eval_every` successful invocations (after each one by default), the runtime:

1. Collects the evidence: recent conversation, context state, recent tool calls (with health monitoring on) and the trigger event
2. Calls a separate LLM (the `eval_model`, or the process model) with the objective, success criteria, and evidence
3. Receives a structured `MissionEvaluation`: achieved (bool), confidence (0.0–1.0), reasoning, progress summary
4. If achieved → mission `completed`, `mission.complete` event, process stops (if `auto_complete=True`)
5. If not achieved and confidence < `confidence_threshold` → mission `paused`, escalation fires (`{"type": "low_confidence", "process_id", "confidence", "reasoning"}`), the team can intervene
6. Otherwise → `mission.progress` event, the mission stays `active`

If the judge call fails or its reply is not valid JSON, the evaluation counts as not achieved with confidence 0.5 and its reasoning starts with `Evaluation failed:`, so with the default threshold it pauses the mission.

### Limits

`timeout_hours` and `max_invocations` fail the mission (`mission.failed` event with `reason` `"timeout"` or `"max_invocations"`). Both are checked before each invocation and again after it, once its evaluation has run, so `max_invocations=50` lets 50 invocations run and the mission is `failed` right after the 50th unless that one completed it. Only successful invocations count. A failed or completed mission does not stop the process: its later invocations are skipped.

### Context injection

Before every invocation the agent sees its mission context as a system message:

```
[Mission] Objective: Migrate all database tables to v2 schema
Success Criteria: All tables pass v2 schema validation with zero errors
Status: active
Progress: 6 invocations/50, 2.3/24.0 hours
Last Evaluation: confidence=0.82 — Tables users..orders migrated; invoices and payments remain.
Progress Summary: 6 of 8 tables migrated
```

`/50` and the hours appear only when `max_invocations` and `timeout_hours` are set; the last two lines appear after the first evaluation.

The agent can also call `check_mission` in open mode to inspect full evaluation history.

## API reference

### `MissionConfig`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `bool` | `False` | Enable mission tracking |
| `objective` | `str` | `""` | What the agent is trying to achieve |
| `success_criteria` | `str` | `""` | How to judge completion |
| `eval_model` | `str \| None` | `None` | LLM for evaluation (defaults to process model) |
| `eval_every` | `int` | `1` | Evaluate every N invocations |
| `confidence_threshold` | `float` | `0.7` | Below this → pause the mission and escalate |
| `timeout_hours` | `float` | `0` | Max hours (0 = no timeout) |
| `max_invocations` | `int` | `0` | Fail the mission after this many invocations (0 = unlimited) |
| `auto_complete` | `bool` | `True` | Stop the process when achieved. `False` keeps it running (its later invocations are skipped: the mission is completed) |
| `on_complete` | `str \| None` | `None` | **Deprecated, ignored.** It was never read; setting it emits a `FutureWarning`. Use `auto_complete` |
| `escalation` | `EscalationTarget \| None` | `None` | Where to notify on low confidence |

### `MissionTracker`

Created automatically when `mission.enabled=True`.

| Method | Description |
|--------|-------------|
| `.state` | Current `MissionState` |
| `.should_evaluate()` | Whether eval is due this invocation |
| `.evaluate(evidence, model)` | Run the evaluation on a `MissionEvidence` bundle; `model` (an ID or chat model) is used when `eval_model` is not set |
| `.context_summary()` | The multi-line mission block for agent context injection |
| `.is_timed_out()` | Whether the mission has exceeded `timeout_hours` |
| `.is_over_limit()` | Whether the mission has reached `max_invocations` |
| `.fail(reason)` | Manually fail the mission |
| `.pause()` / `.resume()` | Pause the mission, or resume a paused one |

### `MissionEvaluation`

Returned by `.evaluate()`:

| Field | Type | Description |
|-------|------|-------------|
| `achieved` | `bool` | Whether the mission is complete |
| `confidence` | `float` | 0.0–1.0 confidence in the assessment |
| `reasoning` | `str` | Why the evaluator made this judgment |
| `progress_summary` | `str` | Current progress description |
| `invocation_number` | `int` | Which invocation this evaluation covers |

!!! tip "Learn the concept"

    - [Autonomous AI Agent Runtime: Budgets, Crash Recovery, API](https://www.promptise.com/guides/view/autonomous-ai-agent-runtime) -- Missions, budgets and recovery tested on real runs.
