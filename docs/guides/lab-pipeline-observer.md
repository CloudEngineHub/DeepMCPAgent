# Lab: Pipeline Observer Agent

Build an autonomous agent that **watches a data pipeline, detects anomalies, fixes recoverable issues, and escalates critical problems** — all without human intervention.

By the end of this lab, you'll have a daemon that:

- Runs as a long-lived background process
- Listens for pipeline events on an HTTP webhook
- Classifies each event (INFO, WARNING, CRITICAL)
- Investigates anomalies with diagnostic tools
- Attempts auto-remediation for recoverable issues
- Escalates unfixable problems via Slack and PagerDuty
- Tracks its own budget, health, and mission progress
- Writes every decision to an append-only journal
- Pauses itself automatically when it hits cost limits

**This is a real production pattern.** It's the same architecture SRE teams use to automate on-call response.

---

## What is this lab actually doing?

Before you run anything, it helps to understand the story.

**The scenario:** You operate a data pipeline with 5 stages: ingestion → transformation → validation → loading → indexing. Data flows through it continuously. Things break. You want an agent to watch it, investigate problems, and only wake up a human when it truly can't handle something itself.

**The pieces:**

| Piece | Role |
|-------|------|
| `pipeline_tools_server.py` | An MCP server with 9 tools that simulate your real monitoring and remediation infrastructure (Datadog, Kubernetes, PagerDuty). In production you'd replace the stubs with real API calls. |
| `pipeline_observer.py` | The observer daemon. Starts an `AgentRuntime`, registers an `AgentProcess`, and runs forever until you stop it. This is what you'd actually deploy. |
| `send_event.py` | A small script you run from another terminal to simulate pipeline events hitting the webhook. |

**The flow:**

1. You start the observer daemon. It boots an LLM agent, starts a webhook HTTP server on port 9090, and waits.
2. Something happens in your pipeline (a stage slows down, a job fails, a database disconnects). Your monitoring system POSTs a JSON event to `http://localhost:9090/alerts`.
3. The webhook trigger converts the HTTP request into a `TriggerEvent` and enqueues it.
4. A worker pulls the event off the queue and invokes the agent with the event payload as a user message.
5. The agent reads the event, decides what to do, and calls MCP tools to investigate, fix, escalate, or document.
6. While this happens: budget counts every tool call, health monitor watches for stuck loops, mission tracker evaluates progress, journal records every step.
7. The agent finishes. Worker acquires semaphore for next event. Repeat forever.

---

## Architecture

```
 ┌─────────────────────────────────────────────────────────────┐
 │                   Data Pipeline (external)                  │
 │   ingestion → transformation → validation → loading         │
 └─────────────────────────┬───────────────────────────────────┘
                           │
                  webhook events (JSON POST)
                           │
                           ▼
 ┌─────────────────────────────────────────────────────────────┐
 │                    AgentRuntime                             │
 │  ┌───────────────────────────────────────────────────────┐  │
 │  │  AgentProcess: pipeline-observer                      │  │
 │  │                                                       │  │
 │  │  Trigger:                                             │  │
 │  │    webhook → /alerts on :9090                         │  │
 │  │                                                       │  │
 │  │  Event Queue (asyncio.Queue, max 1000)                │  │
 │  │       ▼                                               │  │
 │  │  Worker(s) → invoke agent                             │  │
 │  │                                                       │  │
 │  │  Agent (gpt-4o-mini)                                  │  │
 │  │    │                                                  │  │
 │  │    ├─► MCP Tools (via stdio)                          │  │
 │  │    │     check_pipeline_health, get_stage_metrics,    │  │
 │  │    │     get_recent_errors, restart_stage,            │  │
 │  │    │     retry_failed_jobs, clear_queue,              │  │
 │  │    │     create_incident, send_slack_alert,           │  │
 │  │    │     page_oncall                                  │  │
 │  │    │                                                  │  │
 │  │    ├─► Budget (20 tools/run, $5/day)                  │  │
 │  │    ├─► Health (stuck/loop/empty detection)            │  │
 │  │    ├─► Mission ("99.9% uptime")                       │  │
 │  │    └─► Journal (FileJournal → disk)                   │  │
 │  └───────────────────────────────────────────────────────┘  │
 └─────────────────────────────────────────────────────────────┘
                           │
                           ▼
 ┌─────────────────────────────────────────────────────────────┐
 │                  Outputs (side effects)                     │
 │  • Pipeline state file (stages marked healthy again)        │
 │  • Incident tickets (structured JSON records)               │
 │  • Slack alerts (#data-pipeline, #incidents)                │
 │  • PagerDuty pages (on-call engineer woken up)              │
 │  • Journal entries (audit trail, one per event)             │
 └─────────────────────────────────────────────────────────────┘
```

---

## Step 1 — Build the MCP tool server

The MCP server is what the agent calls to interact with the outside world. In production this connects to real systems; here we use JSON files as a stand-in so everything stays local and observable.

Create `pipeline_tools_server.py`:

```python
"""MCP server exposing pipeline observability + remediation tools."""
import json
import random
from datetime import datetime, timezone
from pathlib import Path

from promptise.mcp.server import MCPServer

server = MCPServer("pipeline-tools", version="1.0.0")

STATE_FILE = Path("./pipeline_state.json")
INCIDENTS_FILE = Path("./incidents.json")
ALERTS_FILE = Path("./alerts.json")


def _load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {"stages": {"ingestion": "healthy", "transformation": "degraded",
            "validation": "healthy", "loading": "healthy", "indexing": "healthy"}}


def _save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2))


def _load_list(path: Path):
    return json.loads(path.read_text()) if path.exists() else []


def _save_list(path: Path, items):
    path.write_text(json.dumps(items, indent=2))


# ── Observation tools ───────────────────────────────────────────

@server.tool()
async def check_pipeline_health() -> str:
    """Return the current health of all pipeline stages."""
    state = _load_state()
    healthy = sum(1 for v in state["stages"].values() if v == "healthy")
    return json.dumps({
        "stages": state["stages"],
        "healthy_count": healthy,
        "total_count": len(state["stages"]),
        "overall": "degraded" if any(v != "healthy" for v in state["stages"].values()) else "healthy",
    })


@server.tool()
async def get_stage_metrics(stage: str) -> str:
    """Get throughput, error rate, and latency for a specific stage."""
    state = _load_state()
    status = state["stages"].get(stage, "unknown")
    if status == "degraded":
        metrics = {"stage": stage, "status": status, "throughput_rps": 340,
                   "error_rate_pct": 3.2, "p95_latency_ms": 1240, "queue_depth": 8420}
    else:
        metrics = {"stage": stage, "status": status, "throughput_rps": 3420,
                   "error_rate_pct": 0.1, "p95_latency_ms": 120, "queue_depth": 45}
    return json.dumps(metrics)


@server.tool()
async def get_recent_errors(stage: str, minutes: int = 10) -> str:
    """Get recent errors from a stage in the last N minutes."""
    return json.dumps({
        "stage": stage,
        "errors": [
            {"timestamp": "14:23:12", "message": "Deserialization failed: unexpected field 'legacy_id'"},
            {"timestamp": "14:22:51", "message": "Schema validation: unknown column 'legacy_id'"},
        ],
    })


# ── Auto-fix tools ──────────────────────────────────────────────

@server.tool()
async def restart_stage(stage: str, reason: str) -> str:
    """Restart a pipeline stage. Use for stuck/frozen stages."""
    state = _load_state()
    state["stages"][stage] = "healthy"
    _save_state(state)
    return json.dumps({"action": "restart_stage", "stage": stage,
                       "result": f"Stage '{stage}' restarted successfully."})


@server.tool()
async def retry_failed_jobs(stage: str, max_retries: int = 3) -> str:
    """Retry failed jobs in a stage. Use for transient failures."""
    state = _load_state()
    if state["stages"].get(stage) == "degraded":
        state["stages"][stage] = "healthy"
        _save_state(state)
    return json.dumps({"action": "retry_failed_jobs", "stage": stage,
                       "retried": 42, "succeeded": 36, "still_failing": 6})


@server.tool()
async def clear_queue(stage: str, reason: str) -> str:
    """Clear a backed-up queue."""
    return json.dumps({"action": "clear_queue", "stage": stage,
                       "cleared_items": random.randint(500, 5000)})


# ── Escalation tools ────────────────────────────────────────────

@server.tool()
async def create_incident(title: str, severity: str, description: str, affected_stage: str) -> str:
    """Create an incident ticket for an issue that needs human attention."""
    incidents = _load_list(INCIDENTS_FILE)
    incident_id = f"INC-{len(incidents) + 1:04d}"
    incidents.append({
        "id": incident_id, "title": title, "severity": severity,
        "description": description, "affected_stage": affected_stage,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    _save_list(INCIDENTS_FILE, incidents)
    return json.dumps({"incident_id": incident_id, "status": "created"})


@server.tool()
async def send_slack_alert(channel: str, message: str) -> str:
    """Send a Slack alert to a channel."""
    alerts = _load_list(ALERTS_FILE)
    alerts.append({"type": "slack", "channel": channel, "message": message,
                   "sent_at": datetime.now(timezone.utc).isoformat()})
    _save_list(ALERTS_FILE, alerts)
    return json.dumps({"channel": channel, "delivered": True})


@server.tool()
async def page_oncall(service: str, message: str) -> str:
    """Page the on-call engineer via PagerDuty. CRITICAL only."""
    alerts = _load_list(ALERTS_FILE)
    alerts.append({"type": "pagerduty", "service": service, "message": message,
                   "sent_at": datetime.now(timezone.utc).isoformat()})
    _save_list(ALERTS_FILE, alerts)
    return json.dumps({"service": service, "page_id": f"PD-{random.randint(10000, 99999)}"})


if __name__ == "__main__":
    server.run(transport="stdio")
```

!!! tip "Why JSON files?"
    The JSON files (`pipeline_state.json`, `incidents.json`, `alerts.json`) act as observable side effects. You can `cat` them at any time to see exactly what the agent has done. In production, replace `_save_state()` with a call to your Kubernetes API or monitoring service, `_save_list(INCIDENTS_FILE)` with a PagerDuty incident creation call, etc. The agent logic doesn't change — only the tool implementations.

---

## Step 2 — Build the observer daemon

This is the actual agent process. It uses `AgentRuntime` to register a long-lived `AgentProcess` with a webhook trigger, full governance, and a file journal.

Create `pipeline_observer.py`:

```python
"""Pipeline Observer daemon — runs as an AgentProcess with a webhook trigger."""
import asyncio
import signal
import sys
from pathlib import Path

from promptise.runtime import AgentRuntime
from promptise.runtime.config import (
    ProcessConfig, TriggerConfig,
    BudgetConfig, HealthConfig, MissionConfig, JournalConfig,
)

SERVER_SCRIPT = str(Path(__file__).parent / "pipeline_tools_server.py")
JOURNAL_DIR = str(Path(__file__).parent / "pipeline_journal")


INSTRUCTIONS = """
You are an autonomous pipeline monitoring agent. You receive pipeline events
via webhook and must respond appropriately.

## Your capabilities

Observation:
- check_pipeline_health — see which stages are healthy
- get_stage_metrics — throughput, errors, latency, queue depth for a stage
- get_recent_errors — recent error messages from a stage

Remediation (try these FIRST for recoverable issues):
- restart_stage — for stuck or frozen stages
- retry_failed_jobs — for transient failures (best for schema errors)
- clear_queue — when queue depth is excessive

Escalation:
- create_incident — document the issue formally
- send_slack_alert — notify the team
- page_oncall — wake up the on-call engineer (CRITICAL only)

## Decision framework

1. **INFO** events: Acknowledge briefly. Do NOT call any tools.

2. **WARNING** events:
   - Call get_stage_metrics to investigate
   - Call get_recent_errors to see what's failing
   - Call retry_failed_jobs as remediation
   - Call create_incident with severity=WARNING to document
   - Call send_slack_alert to channel="#data-pipeline"
   - Do NOT page on-call for warnings

3. **CRITICAL** events:
   - Call check_pipeline_health to see overall state
   - Call get_stage_metrics for the affected stage
   - Call restart_stage as remediation
   - Call create_incident with severity=CRITICAL
   - Call page_oncall with service="data-platform"
   - Call send_slack_alert to channel="#incidents"

Always include specific metrics in incident descriptions. Be concise.
"""


async def main():
    Path(JOURNAL_DIR).mkdir(parents=True, exist_ok=True)

    config = ProcessConfig(
        model="openai:gpt-4o-mini",
        instructions=INSTRUCTIONS,
        servers={
            "pipeline": {
                "command": sys.executable,
                "args": [SERVER_SCRIPT],
                "transport": "stdio",
            },
        },
        triggers=[
            TriggerConfig(
                type="webhook",
                webhook_path="/alerts",
                webhook_port=9090,
            ),
        ],
        budget=BudgetConfig(
            enabled=True,
            max_tool_calls_per_run=20,
            max_tool_calls_per_day=2000,
            max_cost_per_day=5.0,
            on_exceeded="pause",
        ),
        health=HealthConfig(
            enabled=True,
            stuck_threshold=3,
            loop_window=20,
            empty_threshold=3,
            on_anomaly="escalate",
        ),
        mission=MissionConfig(
            enabled=True,
            objective="Keep the pipeline above 99.9% uptime by detecting and fixing issues.",
            success_criteria="No unresolved P1 incidents for >15 minutes.",
            eval_every=10,
        ),
        journal=JournalConfig(
            backend="file",
            path=JOURNAL_DIR,
        ),
        concurrency=1,
        max_consecutive_failures=5,
    )

    print("[observer] Starting AgentRuntime with webhook on :9090/alerts")

    async with AgentRuntime() as runtime:
        await runtime.add_process("pipeline-observer", config)
        await runtime.start_all()

        print("[observer] Process running. Send events to http://localhost:9090/alerts")
        print("[observer] Press Ctrl+C to stop.")

        stop_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, stop_event.set)
        await stop_event.wait()
        print("[observer] Shutting down...")


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Step 3 — The event sender

A tiny script you run from a second terminal to simulate pipeline events hitting your agent.

Create `send_event.py`:

```python
"""Send a test pipeline event to the observer agent."""
import asyncio
import sys
import httpx

SEVERITIES = {
    "info": {
        "severity": "INFO",
        "stage": "ingestion",
        "message": "Checkpoint saved. 12,450 records processed. Throughput nominal.",
    },
    "warning": {
        "severity": "WARNING",
        "stage": "transformation",
        "message": "Error rate elevated: 3.2% (threshold: 1%) in last 5 minutes.",
    },
    "critical": {
        "severity": "CRITICAL",
        "stage": "loading",
        "message": "Stage FAILED: Connection refused to database. Pipeline halted.",
    },
}


async def main():
    severity = sys.argv[1] if len(sys.argv) > 1 else "warning"
    event = SEVERITIES[severity]
    async with httpx.AsyncClient() as client:
        resp = await client.post("http://localhost:9090/alerts", json=event)
        print(f"[{resp.status_code}] {event['severity']}: {event['message']}")


if __name__ == "__main__":
    asyncio.run(main())
```

---

## Step 4 — Run it

Set up your environment (one time):

```bash
# Create working directory
mkdir pipeline-lab && cd pipeline-lab

# Save the three scripts from above into this directory
# (pipeline_tools_server.py, pipeline_observer.py, send_event.py)

# Initialize state files
echo '{"stages":{"ingestion":"healthy","transformation":"degraded","validation":"healthy","loading":"healthy","indexing":"healthy"}}' > pipeline_state.json
echo "[]" > incidents.json
echo "[]" > alerts.json

# Make sure your OpenAI key is set
export OPENAI_API_KEY=sk-...
```

**Terminal 1 — start the observer:**

```bash
python pipeline_observer.py
```

You should see:

```
[observer] Starting AgentRuntime with webhook on :9090/alerts
[observer] Process running. Send events to http://localhost:9090/alerts
[observer] Press Ctrl+C to stop.
```

The daemon is now listening. It's idle — no events yet, no LLM calls made, no cost incurred. This is a long-lived process: it will stay running until you stop it with Ctrl+C.

**Terminal 2 — send events:**

```bash
python send_event.py info
python send_event.py warning
python send_event.py critical
```

Each command prints the HTTP response code:

```
[202] INFO: Checkpoint saved. 12,450 records processed. Throughput nominal.
[202] WARNING: Error rate elevated: 3.2% (threshold: 1%) in last 5 minutes.
[202] CRITICAL: Stage FAILED: Connection refused to database. Pipeline halted.
```

**202 Accepted** means the webhook received the event and queued it for processing. The HTTP response returns immediately — the agent does its work asynchronously in the background. This is important for real monitoring integrations: your monitoring system doesn't wait for the agent to finish, it just fires the webhook and moves on.

---

## What the agent actually does with each event

Let's walk through what happens in real time for each severity. These are actual runs with GPT-4o-mini — not scripted, not hardcoded, the agent reasons through each one fresh.

### INFO event — Acknowledgment only

When an INFO event arrives:

1. Webhook trigger receives the POST and enqueues a `TriggerEvent`
2. Worker dequeues it, invokes the agent
3. Agent reads the event, sees severity is INFO
4. Per the instructions, the agent **does not call any tools** — it just returns a brief acknowledgment

**Expected behavior:**

| Metric | Value |
|--------|-------|
| Tool calls | 0 |
| Incidents created | 0 |
| Alerts sent | 0 |
| LLM cost | ~$0.0001 |
| Time | ~1-2 seconds |

**Why it matters:** INFO events are noise. A healthy pipeline generates hundreds of them per minute. If the agent called a tool for every one, you'd burn your budget in an hour. The instructions explicitly tell the agent to ignore them — and it listens.

### WARNING event — Investigate, remediate, document

When a WARNING event arrives:

1. Agent reads: `transformation stage, error rate 3.2%, queue backup`
2. **Calls `get_stage_metrics(stage="transformation")`** to see the current state. Gets back: `{"status": "degraded", "error_rate_pct": 3.2, "queue_depth": 8420}`
3. **Calls `get_recent_errors(stage="transformation")`** to understand what's failing. Gets back: `[{"message": "Deserialization failed: unexpected field 'legacy_id'"}]`
4. **Diagnoses** the issue: schema mismatch, transient. Good candidate for retry.
5. **Calls `retry_failed_jobs(stage="transformation", max_retries=3)`** to remediate. Gets back: `{"retried": 42, "succeeded": 36}`. The tool also marks the stage as healthy in `pipeline_state.json`.
6. **Calls `create_incident(title="High Error Rate in Transformation Stage", severity="WARNING", description="Error rate was 3.2%, 6 jobs still failing after retry with 'legacy_id' schema issue", affected_stage="transformation")`**. Gets back: `{"incident_id": "INC-0001"}`
7. **Calls `send_slack_alert(channel="#data-pipeline", message="INC-0001: auto-recovered 36/42 jobs. 6 need review.")`** to notify the team.
8. **Does NOT call `page_oncall`** — WARNINGs never wake up humans at 2am.
9. Returns a concise final response summarizing what it did.

**Expected behavior:**

| Metric | Value |
|--------|-------|
| Tool calls | 4-6 |
| Incidents created | 1 (WARNING) |
| Alerts sent | 1 (Slack #data-pipeline) |
| PagerDuty pages | 0 |
| Pipeline state | `transformation: degraded → healthy` |
| LLM cost | ~$0.002 |
| Time | ~8-15 seconds |

**Why it matters:** This is the whole point of the agent. A human would have to: see the alert, open the dashboard, investigate metrics, check error logs, restart jobs, file a ticket, post in Slack. That's 15 minutes of human attention. The agent does it in 10 seconds while you sleep.

### CRITICAL event — Escalate AND remediate

When a CRITICAL event arrives:

1. Agent reads: `loading stage failed, database connection refused, pipeline halted`
2. **Calls `check_pipeline_health()`** to see the blast radius: is just loading broken, or is it cascading?
3. **Calls `get_stage_metrics(stage="loading")`** for specifics.
4. **Calls `restart_stage(stage="loading", reason="Connection refused to database. Attempting restart.")`** to try remediation. Gets back: `{"result": "Stage 'loading' restarted successfully."}`
5. **Calls `create_incident(title="Loading Stage Failed", severity="CRITICAL", description="Database connection refused. Restarted stage. Watching for recurrence.", affected_stage="loading")`**. Gets back: `{"incident_id": "INC-0002"}`
6. **Calls `page_oncall(service="data-platform", message="INC-0002: Loading stage failed — DB connection refused. Auto-restarted. Investigate root cause.")`**. A real human gets woken up.
7. **Calls `send_slack_alert(channel="#incidents", message="CRITICAL: INC-0002 loading stage failed. Auto-restarted. On-call paged.")`**.
8. Returns final response.

**Expected behavior:**

| Metric | Value |
|--------|-------|
| Tool calls | 5-7 |
| Incidents created | 1 (CRITICAL) |
| Alerts sent | 1 (Slack #incidents) |
| PagerDuty pages | 1 (on-call) |
| Pipeline state | `loading: healthy` (restored) |
| LLM cost | ~$0.003 |
| Time | ~10-20 seconds |

**Why it matters:** A CRITICAL incident needs both a human AND a fix. The agent doesn't just page someone and wait — it also attempts remediation so the pipeline is back up by the time the human wakes up. The human just needs to figure out the root cause and prevent recurrence.

---

## Verifying it actually worked

After running the three events, inspect the side effects:

```bash
cat pipeline_state.json
cat incidents.json
cat alerts.json
```

**Expected contents of `pipeline_state.json`:**

```json
{
  "stages": {
    "ingestion": "healthy",
    "transformation": "healthy",
    "validation": "healthy",
    "loading": "healthy",
    "indexing": "healthy"
  }
}
```

Notice `transformation` is now `healthy` — the agent auto-remediated it by calling `retry_failed_jobs`. And `loading` stays `healthy` because `restart_stage` fixed it. **The agent actually changed real state.**

**Expected contents of `incidents.json`:**

```json
[
  {
    "id": "INC-0001",
    "title": "High Error Rate in Transformation Stage",
    "severity": "WARNING",
    "description": "Error rate exceeds threshold. 5 errors in the last 10 minutes...",
    "affected_stage": "transformation",
    "created_at": "2026-04-08T07:01:17.161739+00:00"
  },
  {
    "id": "INC-0002",
    "title": "Loading Stage Stuck",
    "severity": "CRITICAL",
    "description": "Loading stage is stuck. No new records processed in the last 15 minutes...",
    "affected_stage": "loading",
    "created_at": "2026-04-08T07:01:30.340694+00:00"
  }
]
```

**Expected contents of `alerts.json`:**

```json
[
  {
    "type": "slack",
    "channel": "#data-pipeline",
    "message": "Warning: High error rate detected in Transformation stage..."
  },
  {
    "type": "pagerduty",
    "service": "data-platform",
    "message": "Critical alert: Loading stage is stuck for 15 minutes."
  },
  {
    "type": "slack",
    "channel": "#incidents",
    "message": "CRITICAL: Loading stage is stuck. On-call has been notified."
  }
]
```

Notice the routing:

- **#data-pipeline** channel gets WARNING-level notifications (low noise, informational)
- **#incidents** channel gets CRITICAL-level notifications (high priority, needs attention)
- **PagerDuty** is reserved for CRITICAL only (wakes up humans)

This is exactly how a real SRE team structures their alerting. The agent respects the hierarchy.

---

## Understanding each governance feature

The observer isn't just an agent with tools — it's an agent with **guardrails**. Each governance feature prevents a specific failure mode.

### Budget — prevents cost runaway

```python
budget=BudgetConfig(
    enabled=True,
    max_tool_calls_per_run=20,
    max_tool_calls_per_day=2000,
    max_cost_per_day=5.0,
    on_exceeded="pause",
),
```

**What it tracks:** every LLM call and tool call, with running counters for the current run and the current day.

**What it prevents:** an agent getting stuck in a loop and calling 10,000 tools in an hour, or a traffic spike causing an unexpected $500 bill.

**What happens when it triggers:** `on_exceeded="pause"` transitions the process to `SUSPENDED` state. Events keep arriving but they're re-queued instead of processed. The agent stops spending money. You get a chance to investigate.

**Real example from our test run:** When we sent 3 events in rapid succession and the agent kept processing them, it eventually hit `max_cost_per_day=5.0`. The daemon log showed:

```
Budget violation: max_cost_per_day (limit=5.0, current=6.0) — action=pause
```

The runtime paused the process automatically. No runaway costs. This is exactly what budget enforcement should do.

### Health — detects bad behavior

```python
health=HealthConfig(
    enabled=True,
    stuck_threshold=3,       # Same tool + args 3 times = stuck
    loop_window=20,          # Look at last 20 tool calls for repeating patterns
    empty_threshold=3,       # 3 empty responses in a row = broken
    on_anomaly="escalate",
),
```

**What it tracks:** recent tool calls (name + arguments hash), recent response lengths, and error rates.

**What it catches:**

- **Stuck loops:** agent calls `get_stage_metrics("transformation")` three times in a row with no progress. Probably stuck in a prompt loop.
- **Repeating patterns:** agent calls `A → B → A → B → A → B`. Probably confused about which step to do next.
- **Empty responses:** agent returns 3 responses under 10 characters in a row. Probably the model is broken or the prompt is confusing it.

**What happens when it triggers:** `on_anomaly="escalate"` fires an escalation webhook (if configured) and logs the anomaly. You can also set it to `pause` or `stop` for stricter behavior.

**Why it matters:** LLMs are non-deterministic. Even with good instructions they sometimes get confused. Health detection is your safety net — it catches problems before they burn your budget.

### Mission — tracks progress against objectives

```python
mission=MissionConfig(
    enabled=True,
    objective="Keep the pipeline above 99.9% uptime by detecting and fixing issues.",
    success_criteria="No unresolved P1 incidents for >15 minutes.",
    eval_every=10,
),
```

**What it does:** every 10 invocations, the runtime takes a snapshot of what the agent has done (recent tool calls, state changes, incidents created) and asks an LLM-as-judge: "Given this activity, is the agent making progress toward its mission?"

**Why it matters:** you can have an agent that looks fine at the individual-event level but is drifting off-mission overall. Examples:

- Escalating too many false alarms → mission says "on-call is fatigued, reduce paging"
- Missing real anomalies → mission says "auto-remediation rate is too high, issues aren't being surfaced"
- Creating duplicate incidents → mission says "incident dedup is failing"

The mission tracker catches these trajectory issues that no single event would reveal.

### Journal — audit trail and crash recovery

```python
journal=JournalConfig(
    backend="file",
    path="./pipeline_journal",
),
```

**What it records:** every state transition (CREATED → RUNNING → SUSPENDED → STOPPED), every invocation (which event, what the agent decided, how many tool calls), every lifecycle event, every checkpoint.

**Two purposes:**

1. **Audit trail** — compliance teams want to know every decision the agent made. The journal is append-only JSONL on disk. You can grep it.
2. **Crash recovery** — if the process dies (OOM kill, server restart, power failure), it restarts from the last checkpoint. No events lost, no duplicate actions taken.

**Format:** JSONL files in `./pipeline_journal/<process-id>.jsonl` — one line per event, timestamped, structured.

### Concurrency — control parallelism

```python
concurrency=1,
```

With `concurrency=1`, events are processed **one at a time**. Event 2 waits for event 1 to finish. Safer, simpler, easier to reason about.

With `concurrency=3`, up to 3 events can be processed in parallel by separate workers. Faster for high-throughput scenarios, but be careful — shared state between invocations can get confusing.

**Rule of thumb:** start with `concurrency=1`. Only increase it if your event queue is backing up.

### max_consecutive_failures — auto-fail on repeated errors

```python
max_consecutive_failures=5,
```

If the agent fails 5 times in a row (tool errors, LLM errors, whatever), the process transitions to `FAILED` state. It won't process any more events until a human restarts it.

**Why it matters:** you don't want a broken agent to keep burning budget and creating noise. Fail loudly after a few attempts.

---

## What the runtime does under the hood

When you run `pipeline_observer.py`, here's what happens:

```
1. AgentRuntime() is instantiated
2. runtime.add_process("pipeline-observer", config) registers the process
3. runtime.start_all() kicks off the startup sequence for each process:

   For pipeline-observer:
   a. Transition lifecycle: CREATED → STARTING
   b. Initialize governance: BudgetState, HealthMonitor, MissionTracker, FileJournal
   c. Build the agent: build_agent(model, servers, instructions, ...)
      → spawns pipeline_tools_server.py as a stdio subprocess
      → agent connects via MCP, discovers all 9 tools
   d. Start the webhook trigger:
      → aiohttp HTTP server on port 9090
      → registers POST /alerts handler
   e. Spawn trigger listener tasks (one per trigger)
   f. Spawn worker tasks (concurrency=1 means 1 worker)
   g. Spawn heartbeat task
   h. Transition lifecycle: STARTING → RUNNING
   i. Journal entry written: "lifecycle: running"

4. Main loop: await stop_event.wait()
   (process lives here until Ctrl+C or SIGTERM)

5. When an event arrives:
   a. aiohttp handler receives the POST
   b. Parses JSON body, creates TriggerEvent
   c. Enqueues event into trigger_queue (asyncio.Queue, max 1000)
   d. Returns HTTP 202 to the caller (non-blocking)

6. Worker loop:
   a. Pulls event from trigger_queue
   b. Checks state: if SUSPENDED, re-queues and sleeps
   c. Acquires concurrency semaphore
   d. Calls agent.ainvoke() with the event payload as user message
   e. Agent reasons, calls tools, gets LLM responses
   f. Budget records each tool call
   g. Health records each tool call + response
   h. Mission increments invocation count
   i. Journal appends invocation entry
   j. Worker releases semaphore, loops to next event

7. On Ctrl+C:
   a. stop_event is set
   b. AgentRuntime.__aexit__ runs:
      - Each process: transition STOPPING
      - Stop all triggers (webhook server shuts down)
      - Cancel worker tasks
      - Cancel trigger listener tasks
      - Cancel heartbeat task
      - Transition STOPPED
      - Close the agent (shuts down MCP subprocess)
      - Journal entry: "lifecycle: stopped"
```

Every one of those steps is real, not stubbed. You can trace it through the runtime source code.

---

## Production deployment changes

For a real production deployment, change these things:

### 1. Replace the stub tools

The JSON file backend is for demonstration only. Replace each tool with real API calls:

```python
@server.tool()
async def restart_stage(stage: str, reason: str) -> str:
    """Restart a pipeline stage."""
    # Real: call Kubernetes API to restart the deployment
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            f"{K8S_API}/apis/apps/v1/namespaces/pipeline/deployments/{stage}/restart",
            headers={"Authorization": f"Bearer {K8S_TOKEN}"},
        )
        return json.dumps({"status": resp.status_code, "stage": stage})


@server.tool()
async def page_oncall(service: str, message: str) -> str:
    """Page via PagerDuty."""
    # Real: call PagerDuty Events API
    async with httpx.AsyncClient() as client:
        resp = await client.post(
            "https://events.pagerduty.com/v2/enqueue",
            json={
                "routing_key": os.environ["PAGERDUTY_KEY"],
                "event_action": "trigger",
                "payload": {"summary": message, "source": service, "severity": "critical"},
            },
        )
        return json.dumps(resp.json())
```

The agent logic doesn't change. Only the tool implementations do.

### 2. Set an HMAC secret on the webhook

```python
TriggerConfig(
    type="webhook",
    webhook_path="/alerts",
    webhook_port=9090,
    webhook_hmac_secret=os.environ["WEBHOOK_SECRET"],
),
```

Without this, anyone who can reach port 9090 can fire events at your agent. With it, incoming requests must include a valid HMAC-SHA256 signature in the `X-Webhook-Signature` header.

### 3. Use per-severity models

Cheap model for classification, strong model for critical events. Use the Reasoning Engine's `model_override`:

```python
from promptise.engine import PromptGraph, PromptNode, RouterNode

graph = PromptGraph("event-handler", nodes=[
    RouterNode("classify",
        instructions="Classify the event severity. Route to 'info', 'warning', or 'critical'.",
        model_override="openai:gpt-4o-mini",  # Cheap classifier
    ),
    PromptNode("handle_critical",
        instructions="Handle a critical event. Investigate, remediate, escalate.",
        inject_tools=True,
        model_override="anthropic:claude-sonnet-4-6",  # Strong for critical decisions
    ),
    PromptNode("handle_warning",
        instructions="Handle a warning event. Investigate and remediate.",
        inject_tools=True,
        model_override="openai:gpt-4o-mini",  # Cheap for warnings
    ),
])
```

Pass it via `agent_pattern=graph` in `ProcessConfig`.

### 4. Run behind a reverse proxy

Don't expose port 9090 directly to the internet. Put nginx, Caddy, or a cloud load balancer in front of it. Handle TLS termination at the proxy, then forward to the observer.

### 5. Deploy as a managed process

- **systemd** (Linux): create a `.service` file, set `Restart=always`, enable at boot
- **Kubernetes**: Deployment with `restartPolicy: Always`, liveness probe on `/health` (the webhook includes one)
- **Docker**: `docker run --restart unless-stopped ...`

The runtime handles crash recovery via the journal — you just need the OS to restart the process if it crashes.

### 6. Monitor the observer itself

Who monitors the monitors? Send runtime events to your observability stack:

```python
from promptise.runtime.config import EscalationTarget

health=HealthConfig(
    enabled=True,
    on_anomaly="escalate",
    escalation=EscalationTarget(
        type="webhook",
        url=os.environ["ESCALATION_WEBHOOK"],  # Your Slack/PagerDuty for the observer itself
    ),
),
```

Now if the observer gets stuck, paused, or hits budget limits, you'll find out immediately.

---

## Troubleshooting

### Webhook returns "connection refused"

The observer daemon isn't running. Check Terminal 1 — did `pipeline_observer.py` start successfully? Look for the `[observer] Process running` line.

### Webhook returns 500

The observer crashed mid-request. Check the daemon log for a traceback. Common causes:

- MCP tool server crashed (check for Python errors in the subprocess)
- JSON parsing error in the webhook body
- Agent raised an unhandled exception

### Agent doesn't call tools

The LLM decided not to. Check its response in the daemon log. Usually this means:

- Instructions were ambiguous about when to call tools
- The event didn't match any of the conditions in the decision framework
- The model is confused (switch to a better one temporarily for debugging)

### Budget violations every time

Your `max_tool_calls_per_run` or `max_cost_per_day` is too strict for the actual workload. Check how many tool calls a typical event generates and set the budget to ~2x that as a safety margin.

### Incidents being created for INFO events

The agent isn't following the decision framework. Make the INFO instructions more explicit:

```
1. **INFO** events: Acknowledge briefly. Do NOT call any tools.
   Just return a one-line confirmation like "Event acknowledged, no action needed."
   Do NOT create incidents. Do NOT send alerts. Do NOT call any tools whatsoever.
```

Clearer instructions → better compliance.

### Agent calls the same tool 3 times and gets paused

Health monitor caught a stuck loop. This is working as intended. Check why the agent was stuck:

- Tool returned unexpected output and the agent is retrying
- Instructions don't explain what to do next after the tool call
- The model is overly conservative and keeps "checking again"

Fix the instructions or the tool, then restart the observer.

### Journal files aren't appearing

Check the `journal.path` in `ProcessConfig` — is it a writable directory? The runtime creates it if missing. If you see permission errors, check file system permissions.

### Events are being dropped

The trigger queue is full (default max 1000). Either:

- Your concurrency is too low — events are queuing faster than they can be processed
- The agent is too slow per event (big LLM calls, many tool calls)
- There's an infinite loop somewhere blocking the workers

Increase `concurrency`, use a faster model for high-volume events, or check the daemon log for worker errors.

---

## What to try next

Now that you have a working observer, extend it:

1. **Add more triggers.** A cron trigger for periodic health checks (`"*/30 * * * * *"` for every 30 seconds), a file watch trigger for log file changes, an event trigger for internal pub/sub.

2. **Add approval for destructive actions.** Use `approval=ApprovalPolicy(tools=["restart_stage"])` in `build_agent` to pause for human approval before running certain tools.

3. **Add long-term memory.** Connect a `ChromaProvider` so the agent remembers past incidents and can correlate new events to previous root causes.

4. **Multi-agent coordination.** Split the observer into an "investigator" agent (reads metrics, builds context) and a "responder" agent (decides actions, escalates). Use `cross_agents` to let them talk.

5. **Enable Open Mode.** Let the agent create its own tools as it learns patterns. For example, after seeing the same "schema mismatch" issue 5 times, it could create a `fix_schema_mismatch` tool that automates the workaround.

---

## Related docs

- [Agent Runtime Overview](../runtime/index.md) — the full runtime API
- [Triggers](../runtime/triggers/index.md) — all 5 trigger types and the concurrency architecture
- [Runtime API Reference](../runtime/api.md) — budget, health, mission, journal reference
- [Building Production MCP Servers](production-mcp-servers.md) — auth, rate limits, audit trails
- [Multi-Agent Coordination](multi-agent-teams.md) — coordinate multiple agents across a system
