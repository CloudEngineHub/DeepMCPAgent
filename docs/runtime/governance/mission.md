---
seo_title: "Mission-Driven Agents with an LLM Judge"
description: "Give an agent process an objective and success criteria; an LLM judge or your own check decides when the mission is achieved, with run limits and escalation."
---

# Mission-Oriented Process Model

A mission turns an agent process from "run when triggered" into "work toward a goal until it is done". You give the process an objective and success criteria; after its runs, an LLM judge (or your own check) decides whether the mission is achieved, how confident it is, and whether to pause and escalate. The process completes, fails on a time or run limit, or is failed by an operator, and the mission's progress survives restarts when the process has a journal.

```python
from promptise.runtime import AgentProcess, EscalationTarget, MissionConfig, ProcessConfig
from promptise.runtime.config import JournalConfig, TriggerConfig

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Migrate all tables to the new schema, one per run.",
    triggers=[TriggerConfig(type="cron", cron_expression="*/10 * * * *")],
    mission=MissionConfig(
        enabled=True,
        objective="Migrate all database tables to the v2 schema",
        success_criteria="All tables pass v2 schema validation with zero errors",
        eval_every=3,              # judge after every 3rd successful run
        confidence_threshold=0.7,  # below this: pause and escalate
        timeout_hours=24,          # fail after 24 hours
        max_invocations=50,        # fail after 50 successful runs
        eval_model="openai:gpt-5-mini",
        escalation=EscalationTarget(webhook_url="https://hooks.slack.com/..."),
    ),
    journal=JournalConfig(path=".promptise/journal"),  # mission survives restarts
)

process = AgentProcess("schema-migrator", config)
await process.start()
```

The same in a `.agent` manifest:

```yaml
name: schema-migrator
model: openai:gpt-5-mini
instructions: Migrate all tables to the new schema, one per run.
triggers:
  - type: cron
    cron_expression: "*/10 * * * *"
mission:
  enabled: true
  objective: "Migrate all database tables to the v2 schema"
  success_criteria: "All tables pass v2 schema validation with zero errors"
  eval_every: 3
  confidence_threshold: 0.7
  timeout_hours: 24
  max_invocations: 50
  escalation:
    webhook_url: "https://hooks.slack.com/..."
journal:
  level: checkpoint
```

---

## How it works

### States

A mission (`MissionState`) is in one of four states:

| State | Meaning |
|---|---|
| `active` | The agent works on the mission and evaluations run (the initial state) |
| `paused` | An evaluation was below `confidence_threshold`, or an operator paused it. No evaluations run until it is resumed; the process keeps running its triggers |
| `completed` | Achieved. **Final**: later runs are skipped |
| `failed` | `timeout_hours` or `max_invocations` reached, or failed by an operator. **Final**: later runs are skipped |

Evaluations never change a `completed` or `failed` mission: a judge verdict that arrives after an operator failed the mission doesn't complete it, and a failed mission can't be completed. The API fails only `active` or `paused` missions. Only an `active` mission can be paused, and only a `paused` one resumed.

### Evaluation cycle

Every `eval_every` successful runs (after each one by default), while the mission is `active`, the process:

1. Collects the evidence (`MissionEvidence`): the conversation, the context state, the run's tool calls (recorded when [health monitoring](health.md) is on) and the trigger event.
2. Runs your `success_check` first, when you set one (see [Programmatic success checks](#programmatic-success-checks)).
3. Otherwise asks the judge: a tool-less agent built with `eval_model` (or the process's model) that gets the objective, the success criteria and the evidence (the state as JSON up to 1,000 characters, the last 20 tool calls, the trigger up to 300 characters, the last 10 conversation messages up to 500 characters each) and must answer with JSON: `achieved`, `confidence` (0.0-1.0, clamped), `reasoning`, `progress_summary`.
4. Applies the verdict:
    - **Achieved**: the mission is `completed`, a `mission.complete` event is emitted, and with `auto_complete=True` (the default) the process stops.
    - **Not achieved, confidence below `confidence_threshold`**: the mission is `paused`, a `mission.progress` event is emitted, and when `escalation` is set it receives `{"type": "low_confidence", "process_id", "process_name", "confidence", "reasoning"}`.
    - **Otherwise**: a `mission.progress` event; the mission stays `active`.

If the judge call fails or its reply is not valid JSON, the evaluation counts as not achieved with confidence `0.5` and reasoning starting with `Evaluation failed:`, so with the default threshold it pauses the mission. Every evaluation is journaled as a `mission_evaluation` entry (`achieved`, `confidence`, `reasoning`, `progress_summary`, `mission_state`, `invocation_number`, `source`).

### Events

With an event notifier on the process or runtime:

| Event | Severity | Data |
|---|---|---|
| `mission.complete` | `info` | `process_name`, `confidence`, `invocations` |
| `mission.progress` | `info` | `process_name`, `confidence`, `achieved` (`false`), `invocations` |
| `mission.failed` | `critical` | `process_name`, `process_id`, `reason`, `message`, `objective`, `invocation_count`, `previous_state`, `failed_at` |

In `mission.failed`, `reason` says who failed it (`"timeout"`, `"max_invocations"` or `"manual"`) and `message` says why (for example `"Mission timeout reached"`, `"Mission reached max_invocations (50)"`, or the operator's reason). `failed_at` is ISO UTC.

### Limits

`timeout_hours` and `max_invocations` fail the mission with a `mission.failed` event. Both are checked before each run and again after it, once its evaluation has run, so `max_invocations=50` lets 50 runs happen and the mission is `failed` right after the 50th unless that one completed it. Only successful runs count. The timeout clock starts when the process object is created and, after a restart with a journal, continues where it stopped. A failed or completed mission doesn't stop the process: its later runs are skipped.

### Failing a mission

An operator can fail an `active` or `paused` mission with a reason:

```python
data = process.fail_mission("Requirements changed, migration cancelled")
# {"process_name": "schema-migrator", "process_id": "...", "reason": "manual",
#  "message": "Requirements changed, migration cancelled", "objective": "...",
#  "invocation_count": 6, "previous_state": "active", "failed_at": "2026-10-19T20:23:26+00:00"}
```

`fail_mission()` stores the reason with the mission (`MissionTracker.failure_reason`, the `[Mission]` prompt block, checkpoints), journals a `mission_failed` entry, emits `mission.failed` and returns the event data. It raises `RuntimeError` when the process has no mission, and `ValueError` when the mission is already completed or failed (`The mission is already failed; a failed mission can't be failed`). Over the API: `POST /mission/fail` (see [Over the API](#over-the-api)).

### Context injection

Before every run the agent sees its mission as a system message:

```
[Mission] Objective: Migrate all database tables to the v2 schema
Success Criteria: All tables pass v2 schema validation with zero errors
Status: active
Progress: 6 invocations/50, 2.3/24.0 hours
Last Evaluation: confidence=0.82 — Tables users..orders migrated; invoices and payments remain.
Progress Summary: 6 of 8 tables migrated
```

`/50` and the hours appear only when `max_invocations` and `timeout_hours` are set; the last two lines appear after the first evaluation. A failed mission adds a `Failure Reason:` line after `Status`.

### Evaluation history

The tracker keeps the latest 100 evaluations (`MissionTracker.MAX_EVALUATIONS`), oldest first, in `tracker.evaluations`; `evaluation_count` counts all of them. In [open mode](../meta-tools.md) the agent can call `check_mission`, which returns the mission block above followed by the latest 10 evaluations:

```
Evaluation History (1):
- #3 [llm] not achieved, confidence=0.90: Two tables remain.
```

`GET /api/v1/processes/{name}/mission` returns the kept evaluations.

### Restore after a restart

With a [journal](../journal/index.md), the mission is part of every checkpoint: its state, invocation count, elapsed time, failure reason and evaluation history. The first `start()` of a new process object with the same name restores it (`JournalConfig.recover_on_start`, default `True`), and `process.recovered["mission_restored"]` is `True`. A snapshot made for a different `objective` is not restored (a warning is logged) and the mission starts fresh. A completed or failed mission stays completed or failed after a restart.

---

## Programmatic success checks

When success can be measured in code, set `success_check` in Python. It is called with the `MissionEvidence` before the judge:

```python
from promptise.runtime import MissionConfig
from promptise.runtime.mission import MissionEvidence


def migration_done(evidence: MissionEvidence) -> bool | None:
    left = evidence.state.get("tables_left")
    if left == 0:
        return True    # achieved: no judge call
    if left is None:
        return False   # not achieved: no judge call
    return None        # inconclusive: ask the LLM judge


mission = MissionConfig(
    enabled=True,
    objective="Migrate all tables",
    success_criteria="tables_left is 0",
    success_check=migration_done,
)
```

`True` completes the mission and `False` records "not achieved", both with confidence `1.0` and `source: "programmatic"`, without a judge call. `None` falls through to the judge. A check that raises is logged and the judge decides. `success_check` is Python only: it can't be set in a manifest or over the API, and it is never serialized.

---

## Over the API

| Method | Path | What it does |
|---|---|---|
| `GET` | `/api/v1/processes/{name}/mission` | `{"name", "mission_enabled": true, "mission": {"state", "objective", "invocation_count", "evaluations": [...]}}`; `{"mission_enabled": false}` without a mission |
| `POST` | `/api/v1/processes/{name}/mission/fail` | Fail an `active` or `paused` mission: body `{"reason": "..."}` (optional, default `"Manually failed via API"`). Emits `mission.failed` with `reason: "manual"` |
| `POST` | `/api/v1/processes/{name}/mission/pause` | Pause an `active` mission |
| `POST` | `/api/v1/processes/{name}/mission/resume` | Resume a `paused` mission |
| `PATCH` | `/api/v1/processes/{name}/mission` | Change `MissionConfig` fields; applies to the next evaluation and prompt |

Each evaluation in `GET /mission` has `achieved`, `confidence`, `reasoning`, `progress_summary`, `invocation_number`, `source` and `timestamp`. `fail`, `pause` and `resume` answer `409 MISSION_STATE` when the mission's state doesn't allow them (for example pausing a completed mission) and `400 MISSION_DISABLED` without a mission. The Python client has `get_mission`, `fail_mission`, `pause_mission`, `resume_mission` and `update_mission`.

```bash
curl -X POST http://localhost:9100/api/v1/processes/schema-migrator/mission/fail \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"reason": "Requirements changed, migration cancelled"}'
# {"name": "schema-migrator", "mission_failed": true, "reason": "Requirements changed, migration cancelled"}
```

---

## API reference

### `MissionConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `False` | Enable mission tracking |
| `objective` | `str` | `""` | What the agent is trying to achieve |
| `success_criteria` | `str` | `""` | How the judge decides completion |
| `eval_model` | `str \| None` | `None` | Model for the judge (`None` = the process model) |
| `eval_every` | `int` | `1` | Evaluate every N successful runs (`>= 1`) |
| `confidence_threshold` | `float` | `0.7` | Below this (not achieved): pause and escalate (0-1) |
| `timeout_hours` | `float` | `0.0` | Fail after this many hours (`0` = no timeout) |
| `max_invocations` | `int` | `0` | Fail after this many successful runs (`0` = unlimited) |
| `auto_complete` | `bool` | `True` | Stop the process when the mission is achieved. `False` keeps it running (its later runs are skipped) |
| `on_complete` | `"stop" \| "continue" \| "suspend" \| None` | `None` | **Deprecated, ignored.** Setting it emits a `FutureWarning`; use `auto_complete` |
| `escalation` | `EscalationTarget \| None` | `None` | Where to notify on low confidence (`webhook_url`, `event_type`) |
| `success_check` | `Callable[[MissionEvidence], bool \| None] \| None` | `None` | Programmatic check run before the judge. Python only, never serialized |

Unknown keys are ignored with a `FutureWarning` (manifests and the API reject them).

### `MissionTracker`

Created by the process when `mission.enabled=True` (or when a `PATCH /mission` enables it).

| Member | Description |
|---|---|
| `.state` | Current `MissionState` |
| `.evaluations` | Kept evaluations, oldest first (at most `MAX_EVALUATIONS` = 100) |
| `.evaluation_count` | Total evaluations, including ones no longer kept |
| `.evaluation_history(limit=10)` | The latest `limit` evaluations |
| `.history_summary(limit=10)` | `context_summary()` plus the latest evaluations (what `check_mission` returns) |
| `.invocation_count` | Successful runs counted |
| `.failure_reason` | Why the mission failed, or `None` |
| `.should_evaluate()` | Whether an evaluation is due now |
| `await .evaluate(evidence, model)` | Run `success_check` and/or the judge; `model` is used when `eval_model` is not set |
| `.context_summary()` | The `[Mission]` block text |
| `.is_timed_out()` / `.is_over_limit()` | The `timeout_hours` / `max_invocations` checks |
| `.fail(reason, *, source="manual")` | Fail the mission; returns the `mission.failed` data (without `process_name`). On a process, use `process.fail_mission()`, which also emits the event |
| `.pause()` / `.resume()` / `.complete()` | State changes (no effect from a state that doesn't allow them) |
| `.to_dict()` / `.restore(data)` / `MissionTracker.from_dict(data, config)` | Snapshot and restore (`restore` raises `ValueError` for a snapshot with another objective) |

### `MissionEvaluation`

| Field | Type | Description |
|---|---|---|
| `achieved` | `bool` | Whether the mission is complete |
| `confidence` | `float` | 0.0-1.0 confidence in the verdict |
| `reasoning` | `str` | Why the evaluator decided this |
| `progress_summary` | `str` | Current progress |
| `timestamp` | `datetime` | When the evaluation ran (UTC) |
| `invocation_number` | `int` | The run count it covers |
| `source` | `str` | `"llm"` or `"programmatic"` |

### `MissionEvidence`

| Field | Type | Description |
|---|---|---|
| `conversation` | `list[dict]` | The conversation buffer |
| `state` | `dict` | The context state |
| `tool_calls` | `list[tuple[str, str]]` | Tool calls of the run as `(name, args_hash)` (with health monitoring on) |
| `trigger_event` | `dict` | `{"type", "payload"}` of the trigger event |
| `invocation_count` | `int` | Successful runs of the process so far |

---

## Common pitfalls

- **Every evaluation is an LLM call.** With `eval_every=1` the judge runs after every run. Raise `eval_every`, use a cheaper `eval_model`, or add a `success_check` that answers without the judge.
- **A failed judge call pauses the mission.** It scores confidence `0.5`, below the default threshold. A paused mission stays paused until someone calls `resume`; check the `Evaluation failed:` reasoning first.
- **Paused doesn't mean idle.** A paused mission skips evaluations, but the process keeps running its triggers (and spending budget). Suspend the process if it should stop working.
- **Completed and failed are final.** Restarting the process doesn't reopen the mission when a journal restores it; its runs keep being skipped. Use a new `objective`, a new journal, or `recover_on_start=False` to start a new mission.
- **`on_complete` does nothing.** Use `auto_complete`.
- **`success_check` only exists in Python.** A manifest or API deploy can't set it; those missions always use the judge.
- **Tool calls are only evidence with health monitoring on.** Without `health.enabled`, the judge sees state, conversation and trigger only.

---

## What's next

- [Autonomy Budget](budget.md): cap what the mission may spend
- [Behavioral Health](health.md): detect a stuck agent, and record tool calls as evidence
- [Orchestration API](../api.md): the mission routes and the rest of the control API
- [Journal](../journal/index.md): checkpoints that carry the mission across restarts
- [Meta-Tools](../meta-tools.md): `check_mission` in open mode

!!! tip "Learn the concept"

    - [Goal-Driven AI Agents: Missions With an LLM Judge](https://www.promptise.com/guides/view/goal-driven-ai-agent-mission): Objectives, success criteria, thresholds and judge costs on real runs.
    - [Autonomous AI Agent Runtime: Budgets, Crash Recovery, API](https://www.promptise.com/guides/view/autonomous-ai-agent-runtime): Missions, budgets and recovery tested on real runs.
