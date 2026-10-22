---
title: Build an Autonomous Agent Runtime in Python with Promptise Foundry
seo_title: "Build an Autonomous Agent Runtime in Python"
description: "Build an autonomous agent system step by step: triggers, persistent state, crash recovery, budget, health, mission and secret governance, and multi-node runs."
keywords: autonomous agent tutorial, agent runtime guide, build autonomous AI agent, cron AI agent, AI agent crash recovery, agent governance Python
---

# Building Agentic Runtime Systems

## What You'll Build

A production-grade autonomous agent system that monitors data pipelines, reacts to events, persists state across invocations, recovers from crashes, enforces governance policies, and scales across multiple machines. You'll start with a single agent and progressively add capabilities until you have a governed, mission-driven, distributed multi-agent operations center.

## Concepts

An **agentic runtime** is the infrastructure that turns a stateless LLM into a persistent, autonomous process. Without a runtime, an agent exists only for the duration of a single request: you call it, get a response, and it disappears. A runtime wraps that same agent in a long-running process with scheduling, event handling, persistent memory, crash recovery, and governance.

Think of the relationship like a web application versus a web framework: you could handle HTTP requests manually, but a framework gives you routing, middleware, sessions, and error handling. The Agent Runtime does the same for AI agents: it provides the infrastructure so you can focus on what the agent should do, not how to keep it running.

The runtime is built in layers. Each layer adds capabilities without requiring changes to the layer below:

```
Layer 5: Governance                  Budget, health, mission, secrets
         |
Layer 4: Distributed Coordination    Multi-node clusters with transport & discovery
         |
Layer 3: AgentRuntime                Multi-process manager with lifecycle control
         |
Layer 2: AgentProcess                Single autonomous agent with triggers & state
         |
Layer 1: build_agent()          Stateless LLM agent with MCP tool access
```

You can stop at any layer. Most projects need Layer 2 (a single autonomous agent). Add Layer 3 when you need multiple agents working together. Layer 4 is for production deployments across multiple machines. Layer 5 is for autonomous agents that need guardrails.

---

## Step 1: Single Autonomous Agent

Start with an `AgentProcess` wrapping a stateless agent in a persistent process.

```python
import asyncio

from promptise.runtime import AgentProcess, ProcessConfig, TriggerConfig

process = AgentProcess(
    name="pipeline-monitor",
    config=ProcessConfig(
        model="openai:gpt-5-mini",
        instructions=(
            "You monitor data pipelines. When triggered, check pipeline "
            "health and report any anomalies."
        ),
        servers={
            "pipeline_api": {
                "url": "http://localhost:8080/mcp",
                "transport": "streamable-http",
            },
        },
        triggers=[
            TriggerConfig(type="cron", cron_expression="*/5 * * * *"),
        ],
    ),
)

async with process:
    # Process runs autonomously, waking on every cron tick
    await asyncio.sleep(3600)  # Run for 1 hour
```

The process handles the full lifecycle: build the agent, connect MCP servers, discover tools, start triggers, and invoke the agent on each trigger event.

---

## Step 2: Add Triggers

Triggers wake the agent in response to events. Compose multiple triggers to create responsive agents.

```python
config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="You respond to pipeline events and scheduled checks.",
    triggers=[
        # Wake every 5 minutes for health checks
        TriggerConfig(type="cron", cron_expression="*/5 * * * *"),

        # Wake on incoming webhooks (e.g., from CI/CD)
        TriggerConfig(
            type="webhook",
            webhook_path="/pipeline-events",
            webhook_port=9090,
        ),

        # Wake when config files change
        TriggerConfig(
            type="file_watch",
            watch_path="/etc/pipeline/config",
            watch_patterns=["*.yaml", "*.json"],
            watch_events=["modified"],
        ),

        # Wake on events from other agents
        TriggerConfig(
            type="event",
            event_type="alert.critical",
        ),

        # Wake on inter-agent messages
        TriggerConfig(
            type="message",
            topic="pipeline.alerts.*",
        ),
    ],
)
```

Each trigger type fires a `TriggerEvent` with type-specific payload data that the agent receives as context.

| Trigger | Wakes On | Payload |
|---------|----------|---------|
| `cron` | Schedule expression | `scheduled_time`, `cron_expression` |
| `webhook` | HTTP POST request | Request body (JSON or text) |
| `file_watch` | File system changes | `path`, `filename`, `event_type` |
| `event` | EventBus events | `event_type`, `source`, `data` |
| `message` | MessageBroker messages | `topic`, `sender`, `content` |

---

## Step 3: Persistent State and Context

`AgentContext` gives each process a persistent key-value state with audit trail, long-term memory, environment variables, and file mounts.

```python
from promptise.runtime.config import ContextConfig

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="You track pipeline health over time.",
    context=ContextConfig(
        # Pre-populate state
        initial_state={
            "alert_count": 0,
            "last_healthy": None,
            "known_issues": [],
        },

        # Give the agent an update_state(key, value) tool, limited to these keys
        state_tool=True,
        writable_keys=["alert_count", "last_healthy", "known_issues"],

        # Long-term memory (persists across restarts)
        memory_provider="chroma",
        memory_collection="pipeline_memory",
        memory_persist_directory=".promptise/memory",
        memory_auto_store=True,

        # Short-term conversation buffer
        conversation_max_messages=50,

        # Expose environment variables matching prefix
        env_prefix="PIPELINE_",

        # Logical names for paths your tools look up (no file I/O by the context)
        file_mounts={
            "config": "/etc/pipeline/config.yaml",
            "runbook": "/docs/runbook.md",
        },
    ),
    triggers=[
        TriggerConfig(type="cron", cron_expression="*/5 * * * *"),
    ],
)
```

The context state is injected into every agent invocation as a system message. With `state_tool=True` the agent can also write the keys in `writable_keys` through its `update_state` tool; without it, only your code, triggers and the API write state. See [Agent Context](../runtime/context.md).

**State access in code:**

```python
process = AgentProcess(name="monitor", config=config)
await process.start()

# Read state programmatically
ctx = process.context
alert_count = ctx.get("alert_count", 0)
snapshot = ctx.state_snapshot()

# Write state with audit trail
ctx.put("alert_count", alert_count + 1, source="external")

# View audit history for a key
history = ctx.state_history("alert_count")
for entry in history:
    print(f"  {entry.timestamp}: {entry.value} (by {entry.source})")
```

---

## Step 4: Crash Recovery with Journals

The journal records every state transition, context write and run result (and, at `level="full"`, every trigger event and tool call). When a process with the same name starts again after a crash or a redeploy, it restores its context state, conversation, budget counters and mission from the last checkpoint by itself (`JournalConfig.recover_on_start`, on by default).

```python
from promptise.runtime.config import JournalConfig

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Critical monitoring agent.",
    journal=JournalConfig(
        level="full",                          # "none", "checkpoint", or "full"
        backend="file",                        # "file" or "memory"
        path=".promptise/journal",             # Persist to disk
    ),
    max_consecutive_failures=3,              # FAILED after 3 failed runs in a row
    restart_policy="on_failure",             # Auto-restart on failure
    max_restarts=5,                          # Max restart attempts
    triggers=[
        TriggerConfig(type="cron", cron_expression="*/5 * * * *"),
    ],
)
```

**Recovery flow:** the process does this on its first `start()`, and `process.recovered` reports what came back. To look at what a process would recover without starting it, use the `ReplayEngine` yourself. Entries are filed under the process **name**:

```python
from promptise.runtime.journal import FileJournal, ReplayEngine

journal = FileJournal(base_path=".promptise/journal")
engine = ReplayEngine(journal)

recovered = await engine.recover(process_id="monitor")
print(f"Recovered state: {recovered['context_state']}")
print(f"Last lifecycle state: {recovered['lifecycle_state']}")
print(f"Entries replayed: {recovered['entries_replayed']}")
```

With `restart_policy="on_failure"`, the runtime automatically attempts to restart failed processes up to `max_restarts` times.

---

## Step 5: Governance (Autonomy Budget)

Without a budget, an autonomous agent can loop indefinitely, call expensive APIs without limit, or take irreversible actions at 3am. The autonomy budget defines the envelope within which the agent operates freely.

```python
from promptise.runtime import ProcessConfig, BudgetConfig, EscalationTarget, ToolCostAnnotation

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Process customer support tickets.",
    budget=BudgetConfig(
        enabled=True,

        # Per-invocation limits
        max_tool_calls_per_run=20,
        max_llm_turns_per_run=10,
        max_cost_per_run=25.0,
        max_irreversible_per_run=2,

        # Daily limits (reset at midnight UTC)
        max_tool_calls_per_day=500,
        max_cost_per_day=100.0,

        # Per-tool cost annotations
        tool_costs={
            "stripe_charge": ToolCostAnnotation(cost_weight=5.0, irreversible=True),
            "send_email": ToolCostAnnotation(cost_weight=2.0, irreversible=True),
            "search": ToolCostAnnotation(cost_weight=0.5),
        },

        # What happens when a limit is hit
        on_exceeded="escalate",   # "pause", "stop", or "escalate"
        inject_remaining=True,     # Show budget in agent context

        escalation=EscalationTarget(
            webhook_url="https://hooks.slack.com/services/...",
        ),
    ),
    triggers=[
        TriggerConfig(type="cron", cron_expression="*/5 * * * *"),
    ],
)
```

When `inject_remaining=True`, the agent sees its remaining budget before every turn, so it can prioritize actions and avoid hitting limits.

!!! warning "cost_weight is abstract, not dollars"
    `cost_weight` values are **budget units you define**, not real money. The framework does not track LLM API costs (token pricing). `max_cost_per_day=100.0` with `cost_weight=5.0` on `stripe_charge` means "the agent can make 20 Stripe charges per day", not "$100/day." Use `max_llm_turns_per_run` to limit LLM usage. See [Autonomy Budget](../runtime/governance/budget.md) for full details on designing your cost scale.

---

## Step 6: Governance (Behavioral Health)

System monitoring watches CPU and memory. Nobody watches whether the agent is actually doing what it should be doing. Behavioral health catches stuck agents, infinite loops, empty responses, and high error rates, without making any LLM calls.

```python
from promptise.runtime import ProcessConfig, HealthConfig, EscalationTarget

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Monitor data pipelines.",
    health=HealthConfig(
        enabled=True,
        stuck_threshold=3,         # 3 identical calls = stuck
        loop_window=20,            # Check last 20 tool calls for loops
        loop_min_repeats=2,        # 2+ repeats of a pattern = loop
        empty_threshold=3,         # 3 consecutive short responses = anomaly
        empty_max_chars=10,        # At most 10 chars = trivial response
        error_rate_threshold=0.5,  # 50%+ failed runs = anomaly
        on_anomaly="pause",        # "log", "pause", "escalate" or "stop"
        cooldown=300,              # 5 min before the same anomaly is reported again
        escalation=EscalationTarget(
            webhook_url="https://hooks.slack.com/services/...",
        ),
    ),
    triggers=[
        TriggerConfig(type="cron", cron_expression="*/5 * * * *"),
    ],
)
```

Four anomaly detectors run automatically:

| Anomaly | Detection | Example |
|---------|-----------|---------|
| **Stuck** | The same tool with the same arguments N times in a row within one run | Agent calling `get_status("pipeline-3")` 5 times in a row |
| **Loop** | Repeating subsequence in tool call history | Agent cycling through check → fix → check → fix endlessly |
| **Empty response** | N consecutive final replies of at most `empty_max_chars` characters | Agent returning `""` or `"ok"` repeatedly |
| **High error rate** | Share of failed runs in the last `error_window` runs at or above the threshold | 6 of the last 10 runs raised |

---

## Step 7: Governance (Mission-Oriented Execution)

Standard agents run on a trigger, do something, stop. A mission-oriented agent runs until a goal is achieved, accumulating context across invocations, with LLM-as-judge evaluation and automatic completion.

```python
from promptise.runtime import ProcessConfig, MissionConfig, EscalationTarget

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Migrate all database tables to v2 schema.",
    mission=MissionConfig(
        enabled=True,
        objective="Migrate all database tables to v2 schema",
        success_criteria="All tables pass v2 schema validation with zero errors",
        eval_every=3,               # Evaluate after every 3rd successful run
        confidence_threshold=0.7,   # Below this: pause the mission and escalate
        timeout_hours=24,           # Fail after 24 hours
        max_invocations=50,         # Fail after 50 successful runs
        auto_complete=True,         # Stop when mission succeeds
        eval_model="openai:gpt-5-mini",  # Separate model for evaluation
        escalation=EscalationTarget(
            webhook_url="https://hooks.slack.com/services/...",
        ),
    ),
    triggers=[
        TriggerConfig(type="cron", cron_expression="*/10 * * * *"),
    ],
)
```

The mission tracker:

1. Prepends the objective and progress into every agent invocation context
2. Every N successful runs, runs an LLM-as-judge evaluation against the success criteria (or your own `success_check` first)
3. If `achieved=True` and `auto_complete=True`, stops the process
4. If the mission is not achieved and `confidence < confidence_threshold`, pauses the **mission** (the process keeps running its triggers) and notifies `escalation`
5. If timed out or over the run limit, fails the mission; later runs are skipped

---

## Step 8: Governance (Scoped Secrets)

Environment variables are shared across all agents on the same host. Secret scoping gives each process its own isolated credential context with automatic expiry and access logging.

```python
from promptise.runtime import ProcessConfig, SecretScopeConfig

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Process payments.",
    secrets=SecretScopeConfig(
        enabled=True,
        secrets={
            "stripe_key": "${STRIPE_API_KEY}",    # Resolved from env at startup
            "db_password": "${DB_PASSWORD}",
            "static_token": "tok-abc123",          # Literal value
        },
        default_ttl=3600,           # 1 hour default
        ttls={
            "stripe_key": 1800,     # 30 min for payment credentials
        },
        revoke_on_stop=True,        # Zero-fill on process stop
    ),
    triggers=[
        TriggerConfig(type="cron", cron_expression="*/5 * * * *"),
    ],
)
```

Key security properties:

- **Values live only in memory**: encrypted, and redacted (`[REDACTED]`) from the journal, checkpoints, conversation history, hook data and API responses
- **TTL-based expiry**: secrets become inaccessible after their TTL expires
- **Access logging**: every access is logged in the journal (name only, never the value)
- **Zero-fill revocation**: on process stop, all values are overwritten with zeros and removed
- **Crash recovery**: secrets are resolved from the environment again on start, never from the journal

---

## Step 9: Multi-Agent Runtime

`AgentRuntime` manages multiple `AgentProcess` instances with centralized lifecycle control.

```python
import asyncio

from promptise.runtime import AgentRuntime, InProcessEventBus, ProcessConfig, TriggerConfig

# Shared channels: InProcessEventBus for "event" triggers. A "message" trigger
# needs a broker object you provide (subscribe(topic, handler) / unsubscribe(id)).
event_bus = InProcessEventBus()
broker = MyMessageBroker()

async with AgentRuntime(event_bus=event_bus, broker=broker) as runtime:
    # Register agents
    await runtime.add_process("monitor", ProcessConfig(
        model="openai:gpt-5-mini",
        instructions="Monitor pipelines and report failures.",
        triggers=[
            TriggerConfig(type="cron", cron_expression="*/5 * * * *"),
        ],
    ))

    await runtime.add_process("analyst", ProcessConfig(
        model="openai:gpt-5-mini",
        instructions="Analyze alerts and determine root cause.",
        triggers=[
            TriggerConfig(type="event", event_type="alert.critical"),
        ],
    ))

    await runtime.add_process("responder", ProcessConfig(
        model="openai:gpt-5-mini",
        instructions="Execute remediation based on analysis.",
        triggers=[
            TriggerConfig(type="message", topic="analysis.complete"),
        ],
    ))

    # Start all agents
    await runtime.start_all()

    # Monitor status
    status = runtime.status()
    for name, proc_status in status["processes"].items():
        print(f"  {name}: {proc_status['state']}")

    # Run until interrupted
    await asyncio.sleep(3600)
```

**Agent-to-agent communication** happens through two channels:

- **Event bus**: broadcast events that any listening process receives. Use `TriggerConfig(type="event", event_type="...")` to subscribe.
- **Message broker**: topic-based messaging (wildcards are up to your broker). Use `TriggerConfig(type="message", topic="...")` to subscribe.

An agent has no built-in tool to publish on the bus or the broker. The events come from your code (a web handler, a scheduler), from an MCP tool you give the agent, or from a runtime hook that publishes when a run finishes; see the staged pipeline in [Multi-Agent Coordination](multi-agent-teams.md#5-a-staged-pipeline).

**Loading from manifests:**

```python
# Load all .agent files from a directory
loaded = await runtime.load_directory("agents/")
print(f"Loaded: {loaded}")  # ["monitor", "analyst", "responder"]

await runtime.start_all()
```

---

## Step 10: Distributed Coordination

For multi-node deployments, `RuntimeTransport` exposes each node's runtime over HTTP, and `RuntimeCoordinator` manages cluster membership.

```python
import os

from promptise.runtime import AgentRuntime
from promptise.runtime.distributed import RuntimeTransport, RuntimeCoordinator

TOKEN = os.environ["PROMPTISE_NODE_TOKEN"]  # required for a non-loopback bind

# Node 1 -- primary
runtime_1 = AgentRuntime()
transport_1 = RuntimeTransport(
    runtime_1,
    host="0.0.0.0",
    port=9100,
    node_id="node-1",
    auth_token=TOKEN,
)
await transport_1.start()

# Node 2 -- secondary
runtime_2 = AgentRuntime()
transport_2 = RuntimeTransport(
    runtime_2,
    host="0.0.0.0",
    port=9101,
    node_id="node-2",
    auth_token=TOKEN,
)
await transport_2.start()

# Coordinator tracks all nodes
coordinator = RuntimeCoordinator(
    health_check_interval=15.0,
    node_timeout=45.0,
    auth_token=TOKEN,  # sent as a bearer token to every node
)
coordinator.register_node("node-1", "http://node-1:9100")
coordinator.register_node("node-2", "http://node-2:9101")
```

Each node exposes a REST API for remote management. Every endpoint except `/health` requires `Authorization: Bearer <token>`; binding a non-loopback address without `auth_token` raises `ValueError` (see [Discovery and Transport](../runtime/distributed/discovery-transport.md#security)).

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/health` | GET | Health check with process count |
| `/status` | GET | Full runtime status |
| `/processes` | GET | List all processes |
| `/processes/{name}/start` | POST | Start a process |
| `/processes/{name}/stop` | POST | Stop a process |
| `/processes/{name}/event` | POST | Inject a trigger event |

---

## Step 11: Production Hardening

Combine governance, failure handling, and resource limits for a production deployment:

```python
config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Production agent.",

    # Concurrency control
    concurrency=3,                    # Up to 3 parallel invocations

    # Health monitoring
    heartbeat_interval=10.0,          # Idle and lifetime checks every 10s
    idle_timeout=300.0,               # Suspend after 5 min idle; the next event resumes it
    max_lifetime=86400.0,             # Stop after 24h (recycled instead with restart_policy="always")

    # Failure handling
    max_consecutive_failures=3,       # FAILED after 3 failed runs in a row
    restart_policy="on_failure",      # Auto-restart on failure
    max_restarts=5,                   # Max 5 restart attempts

    # Governance
    budget=BudgetConfig(
        enabled=True,
        max_tool_calls_per_day=1000,
        on_exceeded="pause",
    ),
    health=HealthConfig(
        enabled=True,
        on_anomaly="escalate",
        escalation=EscalationTarget(webhook_url="https://hooks.slack.com/..."),
    ),
    mission=MissionConfig(
        enabled=True,
        objective="Keep all pipelines healthy",
        success_criteria="Zero critical alerts for 24 consecutive hours",
        eval_every=10,
        timeout_hours=48,
    ),
    secrets=SecretScopeConfig(
        enabled=True,
        secrets={"api_key": "${PIPELINE_API_KEY}"},
        default_ttl=3600,
        revoke_on_stop=True,
    ),
)
```

**Monitoring:**

```python
# Global runtime status
status = runtime.status()
print(f"Processes: {status['process_count']}")

# Per-process status (includes governance info)
for info in runtime.list_processes():
    print(f"  {info['name']}: {info['state']}")

# Detailed process status
detail = runtime.process_status("monitor")
print(f"  Invocations: {detail['invocation_count']}")
print(f"  Failures: {detail['consecutive_failures']}")
print(f"  Uptime: {detail['uptime_seconds']:.0f}s")
```

---

## Complete Example

A three-agent system with full governance: a monitor watches pipelines with a budget and health checks, an analyst diagnoses issues, and a responder executes fixes with a mission objective. Each agent records its result with its state tool, and one runtime hook passes that result on as the next agent's event.

```python
import asyncio

from promptise.runtime import (
    AgentRuntime, BudgetConfig, EscalationTarget, HealthConfig, HookContext, HookEvent,
    HookManager, InProcessEventBus, MissionConfig, ProcessConfig, SecretScopeConfig, TriggerConfig,
)
from promptise.runtime.config import ContextConfig, JournalConfig

PIPELINE = {"pipeline": {"url": "http://localhost:8080/mcp", "transport": "streamable-http"}}
# Which state key a process fills, and which event announces it
HAND_OFF = {"monitor": ("alert", "alert.critical"), "analyst": ("analysis", "analysis.complete")}


async def main():
    bus = InProcessEventBus()
    hooks = HookManager()
    slack = EscalationTarget(webhook_url="https://hooks.slack.com/services/...")
    runtime = AgentRuntime(event_bus=bus, hooks=hooks)

    @hooks.on(HookEvent.TASK_COMPLETED)
    async def hand_off(ctx: HookContext) -> None:
        if ctx.data["outcome"] != "success" or ctx.process_id not in HAND_OFF:
            return
        key, event_type = HAND_OFF[ctx.process_id]
        context = runtime.get_process(ctx.process_id).context
        if context.get(key):
            asyncio.create_task(bus.emit(event_type, {key: context.get(key)}, source=ctx.process_id))
            context.put(key, None, source="hand_off")   # announce each result once

    await runtime.add_process("monitor", ProcessConfig(
        model="openai:gpt-5-mini",
        instructions=(
            "You monitor data pipelines. If a pipeline is unhealthy, describe the problem "
            "with update_state('alert', ...)."
        ),
        servers=PIPELINE,
        triggers=[TriggerConfig(type="cron", cron_expression="*/5 * * * *")],
        context=ContextConfig(
            initial_state={"alert": None},
            state_tool=True,
            writable_keys=["alert"],
            memory_provider="chroma",
            memory_auto_store=True,
        ),
        journal=JournalConfig(level="full", backend="file"),
        budget=BudgetConfig(enabled=True, max_tool_calls_per_day=500, on_exceeded="pause"),
        health=HealthConfig(enabled=True, stuck_threshold=5, on_anomaly="escalate", escalation=slack),
    ))

    await runtime.add_process("analyst", ProcessConfig(
        model="openai:gpt-5-mini",
        instructions="Find the root cause of the alert. Save it with update_state('analysis', ...).",
        triggers=[TriggerConfig(type="event", event_type="alert.critical", event_source="monitor")],
        context=ContextConfig(state_tool=True, writable_keys=["analysis"]),
    ))

    await runtime.add_process("responder", ProcessConfig(
        model="openai:gpt-5-mini",
        instructions="Execute the pipeline remediation the analysis calls for.",
        servers=PIPELINE,
        triggers=[TriggerConfig(type="event", event_type="analysis.complete", event_source="analyst")],
        restart_policy="on_failure",
        mission=MissionConfig(
            enabled=True,
            objective="Restore all pipelines to healthy status",
            success_criteria="All pipeline stages pass health checks for 3 consecutive runs",
            eval_every=3,
            confidence_threshold=0.6,
            timeout_hours=4,
            auto_complete=True,
            escalation=slack,
        ),
        secrets=SecretScopeConfig(
            enabled=True,
            secrets={"pipeline_admin_key": "${PIPELINE_ADMIN_KEY}"},
            default_ttl=1800,
            revoke_on_stop=True,
        ),
    ))

    await runtime.run_forever()   # starts all three; Ctrl+C or SIGTERM stops them cleanly


if __name__ == "__main__":
    asyncio.run(main())
```

`run_forever()` starts every process and, on shutdown, stops and closes them. Check `runtime.status()` (or `promptise runtime status` when you start it from manifests) to see each process's state, counters and governance status.

---

## What's Next

**Reference documentation:**

- [Agent Processes](../runtime/processes.md): Lifecycle methods, triggers, and `ProcessConfig`
- [Runtime Manager](../runtime/runtime-manager.md): Multi-process runtimes with `AgentRuntime`
- [Context & State](../runtime/context.md): Persistent state, memory, and environment
- [Triggers](../runtime/triggers/index.md): All five trigger types

**Governance:**

- [Autonomy Budget](../runtime/governance/budget.md): Per-run and daily limits, tool cost annotations
- [Behavioral Health](../runtime/governance/health.md): Stuck, loop, empty, and error rate detection
- [Mission Model](../runtime/governance/mission.md): LLM-as-judge evaluation, confidence thresholds
- [Secret Scoping](../runtime/governance/secrets.md): Per-process credentials with TTL and rotation

**Infrastructure:**

- [Journal & Recovery](../runtime/journal/index.md): Crash recovery with journals and replay
- [Distributed](../runtime/distributed/coordinator.md): Multi-node coordination
- [Agent Manifests](../runtime/manifests.md): `.agent` YAML files for declarative deployment

**Other guides:**

- [Building Production MCP Servers](production-mcp-servers.md): Build the tool servers your agents connect to
- [Building AI Agents](building-agents.md): The core agent that powers every process
- [Prompt Engineering](prompt-engineering.md): Build reliable, testable system prompts

!!! tip "Learn the concept"

    - [Autonomous AI Agent Runtime: Budgets, Crash Recovery, API](https://www.promptise.com/guides/view/autonomous-ai-agent-runtime): Budgets, crash recovery and missions for agents that run on their own.
    - [Topic hub: Runtime, triggers and deployment](https://www.promptise.com/guides/topics/runtime-deployment): Every guide on triggers, durable runs and deployment.
