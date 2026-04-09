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
        confidence_threshold=0.7,  # Escalate below this
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

Each mission moves through states:

| State | Meaning |
|-------|---------|
| `pending` | Mission defined but not yet started |
| `in_progress` | Agent is actively working |
| `completed` | Success criteria met |
| `failed` | Timeout, max invocations, or manual failure |

### Evaluation cycle

Every `eval_every` invocations, the runtime:

1. Takes the recent conversation history
2. Calls a separate LLM (the `eval_model`) with the objective, success criteria, and conversation
3. Receives a structured `MissionEvaluation`: achieved (bool), confidence (0.0–1.0), reasoning, progress summary
4. If achieved → mission completes, process stops (if `auto_complete=True`)
5. If confidence < `confidence_threshold` → fires escalation, the team can intervene

### Context injection

Before every invocation the agent sees its mission context in the system prompt:

```
[Mission] State: in_progress | Objective: Migrate all database tables to v2 schema
Evaluations: 2 | Last confidence: 0.82 | Last summary: 6 of 8 tables migrated
```

The agent can also call `check_mission` in open mode to inspect full evaluation history.

## API reference

### `MissionConfig`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `bool` | `False` | Enable mission tracking |
| `objective` | `str` | `""` | What the agent is trying to achieve |
| `success_criteria` | `str` | `""` | How to judge completion |
| `eval_model` | `str \| None` | `None` | LLM for evaluation (defaults to process model) |
| `eval_every` | `int` | `5` | Evaluate every N invocations |
| `confidence_threshold` | `float` | `0.7` | Below this → escalate |
| `timeout_hours` | `float` | `0` | Max hours (0 = no timeout) |
| `max_invocations` | `int` | `0` | Max invocations (0 = unlimited) |
| `auto_complete` | `bool` | `True` | Stop process when achieved |
| `escalation` | `EscalationTarget \| None` | `None` | Where to notify on low confidence |

### `MissionTracker`

Created automatically when `mission.enabled=True`.

| Method | Description |
|--------|-------------|
| `.state` | Current `MissionState` |
| `.should_evaluate()` | Whether eval is due this invocation |
| `.evaluate(conversation, model)` | Run LLM-as-judge evaluation |
| `.context_summary()` | One-line string for agent context injection |
| `.is_timed_out()` | Whether the mission has exceeded `timeout_hours` |
| `.fail(reason)` | Manually fail the mission |

### `MissionEvaluation`

Returned by `.evaluate()`:

| Field | Type | Description |
|-------|------|-------------|
| `achieved` | `bool` | Whether the mission is complete |
| `confidence` | `float` | 0.0–1.0 confidence in the assessment |
| `reasoning` | `str` | Why the evaluator made this judgment |
| `progress_summary` | `str` | Current progress description |
| `invocation_number` | `int` | Which invocation this evaluation covers |
