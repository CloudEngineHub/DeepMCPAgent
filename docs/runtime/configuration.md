---
seo_title: "Agent Runtime Configuration Reference"
description: "Every runtime config field with its type and default: RuntimeConfig, ProcessConfig, triggers, journal, context, hooks, secrets, budget, health and mission."
---

# Runtime Configuration

All runtime configuration is Pydantic models with defaults for every optional field. Small section configs (`TriggerConfig`, `JournalConfig`, `BudgetConfig`, ...) are composed into a `ProcessConfig` for one process, and `ProcessConfig`s into a `RuntimeConfig` for an [`AgentRuntime`](runtime-manager.md). This page lists every field with its exact type and default. You only set what you want to change.

```python
from promptise.runtime import JournalConfig, ProcessConfig, RuntimeConfig, TriggerConfig

config = RuntimeConfig(
    processes={
        "watcher": ProcessConfig(
            model="openai:gpt-5-mini",
            instructions="Monitor data pipelines.",
            triggers=[TriggerConfig(type="cron", cron_expression="*/5 * * * *")],
            journal=JournalConfig(path=".promptise/journal"),
            restart_policy="on_failure",
        ),
    },
)
```

Every class on this page is importable from `promptise.runtime`.

---

## Structure

```
RuntimeConfig
  +-- processes: dict[str, ProcessConfig]
  |     +-- open_mode: OpenModeConfig
  |     +-- triggers: list[TriggerConfig]
  |     +-- trigger_delivery: TriggerDeliveryConfig
  |     +-- journal: JournalConfig
  |     +-- context: ContextConfig
  |     +-- hooks: list[HookConfig]
  |     +-- secrets: SecretScopeConfig
  |     +-- budget: BudgetConfig        (tool_costs: ToolCostAnnotation, escalation: EscalationTarget)
  |     +-- health: HealthConfig        (escalation: EscalationTarget)
  |     +-- mission: MissionConfig      (escalation: EscalationTarget)
  |     +-- inbox: InboxConfig
  +-- distributed: DistributedConfig
```

---

## Validation and unknown keys

Values are validated when the config is created: types, ranges, required trigger fields, cron expressions, time zones and cross-field rules raise `pydantic.ValidationError` at construction, not when the process first runs. What happens to a key the model doesn't know depends on the model:

| Models | Unknown key |
|---|---|
| `ProcessConfig`, `TriggerConfig`, `TriggerDeliveryConfig`, `InboxConfig`, `HookConfig` | Rejected: `ValidationError` ("Extra inputs are not permitted") |
| `RuntimeConfig`, `OpenModeConfig`, `JournalConfig`, `ContextConfig`, `SecretScopeConfig`, `BudgetConfig`, `HealthConfig`, `MissionConfig`, `ToolCostAnnotation`, `EscalationTarget`, `DistributedConfig` | Ignored with a `FutureWarning` naming the key and the valid keys; a future release will reject them |

```python
HealthConfig(error_windw=5)
# FutureWarning: HealthConfig: unknown key(s) ['error_windw'] are ignored; valid keys are
# ['cooldown', 'empty_max_chars', ..., 'stuck_threshold']. Unknown keys will be rejected
# in a future release.
```

[Manifests](manifests.md) reject unknown keys in every section, and so does the [Orchestration API](api.md). To catch typos in Python too, turn the warning into an error: `warnings.simplefilter("error", FutureWarning)`.

### Secret fields

These fields are `pydantic.SecretStr`: `TriggerConfig.hmac_secret`, the values of `SecretScopeConfig.secrets`, the values of `HookConfig.env` and `DistributedConfig.auth_token`. You pass plain strings; `repr()` shows `SecretStr('**********')`, and code reads the value with `.get_secret_value()`.

`model_dump(mode="json")` and `RuntimeConfig.to_dict()` write literal values as `"**********"`. For `SecretScopeConfig.secrets`, `HookConfig.env` and `DistributedConfig.auth_token`, a `${VAR}` reference carries no secret and is written as is. Loading a dict that holds the `"**********"` placeholder raises a `ValidationError`, so a dumped config can't silently start with a wrong secret; supply the real values (or references) again.

```python
s = SecretScopeConfig(enabled=True, secrets={"api": "sk-live-123", "db": "${DB_PASSWORD}"})
s.model_dump(mode="json")["secrets"]
# {'api': '**********', 'db': '${DB_PASSWORD}'}
```

---

## RuntimeConfig

| Field | Type | Default | Description |
|---|---|---|---|
| `processes` | `dict[str, ProcessConfig]` | `{}` | Processes to register, by name. `AgentRuntime(config)` registers them at construction. |
| `distributed` | `DistributedConfig` | `DistributedConfig()` | Distributed mode (off by default) |

```python
data = config.to_dict()                  # JSON-compatible; secrets masked
restored = RuntimeConfig.from_dict(data) # model_validate
```

### Presets

`DEFAULT_DEVELOPMENT_CONFIG` and `DEFAULT_PRODUCTION_CONFIG` are identical: no processes and `distributed.enabled=False`. Production hardening is per process (`journal`, `restart_policy`, `health`, `budget`, `trigger_delivery`), and distributed mode needs your cluster's coordinator and token, so no preset turns either on.

```python
from promptise.runtime import DEFAULT_DEVELOPMENT_CONFIG, DEFAULT_PRODUCTION_CONFIG
```

---

## ProcessConfig

Configuration for one agent process. Unknown keys are rejected.

| Field | Type | Default | Description |
|---|---|---|---|
| `model` | `str` | `"openai:gpt-5-mini"` | LLM model ID |
| `instructions` | `str \| None` | `None` | System prompt |
| `execution_mode` | `ExecutionMode` | `ExecutionMode.STRICT` | `"strict"` or `"open"` (self-modifying, see [Meta-Tools](meta-tools.md)) |
| `open_mode` | `OpenModeConfig` | `OpenModeConfig()` | Open-mode limits; ignored in strict mode |
| `servers` | `dict[str, Any]` | `{}` | MCP servers: a dict per server (`url` for HTTP, `command` for stdio) or a server spec object |
| `triggers` | `list[TriggerConfig]` | `[]` | What wakes the process |
| `journal` | `JournalConfig` | `JournalConfig(level="none")` | Off unless set. `JournalConfig()` itself defaults to `level="checkpoint"`. |
| `context` | `ContextConfig` | `ContextConfig()` | State, memory, conversation, environment |
| `concurrency` | `int` | `1` | Runs in parallel (1-100) |
| `heartbeat_interval` | `float` | `10.0` | Seconds between idle-timeout and lifetime checks (> 0) |
| `idle_timeout` | `float` | `0.0` | Seconds without a run before the process suspends with reason `"idle"` (0 = never). The next trigger event resumes it. |
| `max_lifetime` | `float` | `0.0` | Seconds after start at which the process stops, or is recycled with `restart_policy="always"` (0 = unlimited) |
| `max_consecutive_failures` | `int` | `3` | Failed events in a row before `FAILED` (> 0) |
| `restart_policy` | `Literal["always", "on_failure", "never"]` | `"never"` | See [Restart policies](processes.md#restart-policies) |
| `max_restarts` | `int` | `3` | Automatic restart attempts in a row; the count resets after a successful run (>= 0) |
| `restart_backoff` | `float` | `1.0` | Seconds before the first restart; doubles per attempt, capped at 60 s (>= 0) |
| `secrets` | `SecretScopeConfig` | `SecretScopeConfig()` | Disabled |
| `budget` | `BudgetConfig` | `BudgetConfig()` | Disabled |
| `health` | `HealthConfig` | `HealthConfig()` | Disabled |
| `mission` | `MissionConfig` | `MissionConfig()` | Disabled |
| `inbox` | `InboxConfig` | `InboxConfig()` | Disabled |
| `approval` | `Any \| None` | `None` | `ApprovalPolicy` (or dict) for human approval of tool calls |
| `identity` | `Any \| None` | `None` | `AgentIdentity`: attributes the process's actions and presents its credential to MCP servers |
| `trigger_delivery` | `TriggerDeliveryConfig` | `TriggerDeliveryConfig()` | Retries, dead letters, payload scanning |
| `guardrails` | `Any \| None` | `None` | Passed to `build_agent()` (e.g. a `PromptiseSecurityScanner`) |
| `observe` | `Any \| None` | `None` | Passed to `build_agent()`: `True` or an `ObservabilityConfig` |
| `cache` | `Any \| None` | `None` | Passed to `build_agent()`: a `SemanticCache` |
| `optimize_tools` | `Any \| None` | `None` | Passed to `build_agent()`: semantic tool selection |
| `adaptive` | `Any \| None` | `None` | Passed to `build_agent()`: adaptive strategy |
| `max_invocation_time` | `float` | `0` | Per-run timeout in seconds, passed to `build_agent()` (0 = none) |
| `hooks` | `list[HookConfig]` | `[]` | [Lifecycle hooks](hooks.md), registered on `process.hooks` when the process is created |

---

## ExecutionMode

| Value | Description |
|---|---|
| `ExecutionMode.STRICT` (`"strict"`) | The agent can't change itself. Default. |
| `ExecutionMode.OPEN` (`"open"`) | The agent gets [meta-tools](meta-tools.md) to change its instructions, tools, MCP servers and triggers, and to spawn processes, within `OpenModeConfig`. |

---

## OpenModeConfig

Limits for open mode. Unknown keys are ignored with a `FutureWarning`. Open mode lets the model change its own setup; see [Meta-Tools: Security](meta-tools.md#security) before you turn capabilities on.

| Field | Type | Default | Description |
|---|---|---|---|
| `allow_identity_change` | `bool` | `True` | `modify_instructions` tool |
| `allow_tool_creation` | `bool` | `True` | `create_tool` tool. Runs model-written Python in the runtime's process (not a sandbox); the control API refuses it without `allow_command_deploys` |
| `allow_mcp_connect` | `bool` | `True` | `connect_mcp_server` tool |
| `allow_trigger_management` | `bool` | `True` | `add_trigger` / `remove_trigger` tools |
| `allow_memory_management` | `bool` | `True` | `store_memory` / `search_memory` / `forget_memory` tools |
| `max_custom_tools` | `int` | `20` | Agent-created tools (>= 0) |
| `max_dynamic_triggers` | `int` | `10` | Agent-added triggers (>= 0) |
| `max_instruction_length` | `int` | `10000` | Characters of agent-written instructions (>= 100) |
| `max_rebuilds` | `int \| None` | `None` | Hot reloads per lifetime of the process object; restarts don't reset it. A refused change is not applied, now or after a restart. `rollback()` is not refused but counts. `None` = unlimited. |
| `allowed_mcp_urls` | `list[str]` | `[]` | URLs `connect_mcp_server` may use. Empty: any URL whose host resolves to public addresses (warning at start); for a process deployed through the control API, none |
| `identity_mcp_urls` | `list[str]` | `[]` | Agent-connected server URLs that may receive the process identity's credential and a caller's token (empty = none) |
| `sandbox_custom_tools` | `bool` | `True` | Run agent-written tools with restricted builtins and no imports (`False`: a fixed import allowlist). Not a security boundary either way |
| `allow_process_spawn` | `bool` | `False` | `spawn_process` / `list_processes` tools |
| `max_spawned_processes` | `int` | `3` | Processes this agent may spawn (>= 0) |
| `allow_public_webhooks` | `bool` | `False` | Webhook triggers the agent adds (or gives a spawned process) may bind a non-loopback `webhook_host`, with an `hmac_secret` |

---

## TriggerConfig

One trigger. Each built-in `type` reads its own subset of fields; setting a field of another built-in type (for example `watch_path` on a `cron` trigger) to a non-default value is an error, because the trigger would ignore it. Custom types (registered with `register_trigger_type()`) receive the whole config and use `custom_config`. Unknown keys are rejected. See [Triggers](triggers/index.md).

| Field | Type | Default | Applies to | Description |
|---|---|---|---|---|
| `type` | `str` | required | all | `"cron"`, `"webhook"`, `"file_watch"`, `"event"`, `"message"` or a registered custom type |
| `cron_expression` | `str \| None` | `None` | cron | Required for `cron`. Five fields, or six with a trailing seconds field |
| `cron_timezone` | `str \| None` | `None` | cron | IANA time zone (`None` = UTC) |
| `webhook_path` | `str` | `"/webhook"` | webhook | URL path |
| `webhook_port` | `int` | `9090` | webhook | Listen port (1025-65535) |
| `webhook_host` | `str` | `"127.0.0.1"` | webhook | Bind address (`"0.0.0.0"` = all interfaces) |
| `hmac_secret` | `SecretStr \| None` | `None` | webhook | Signature secret; unsigned or badly signed requests get 401 |
| `signature_scheme` | `Literal["generic", "github", "stripe"]` | `"generic"` | webhook | Signature format; a non-default value needs `hmac_secret` |
| `signature_header` | `str \| None` | `None` | webhook | Header with the signature (default: the scheme's header); needs `hmac_secret` |
| `signature_tolerance` | `int` | `300` | webhook | Max age in seconds of a `stripe` signature timestamp (> 0) |
| `allowed_sources` | `list[str]` | `[]` | webhook | Client IPs / CIDR ranges allowed (empty = any); others get 403 |
| `webhook_max_body_bytes` | `int` | `1048576` | webhook | Largest request body (1 MiB; 1 to 1 GiB); larger bodies get 413 |
| `watch_path` | `str \| None` | `None` | file_watch | Required for `file_watch`: the directory to watch |
| `watch_patterns` | `list[str]` | `["*"]` | file_watch | Glob patterns matched against the file name |
| `watch_events` | `list[str]` | `["created", "modified"]` | file_watch | Any of `created`, `modified`, `deleted`, `moved` (at least one) |
| `watch_debounce_seconds` | `float` | `0.5` | file_watch | Events for one file within this window merge into one (>= 0) |
| `watch_recursive` | `bool` | `True` | file_watch | Also watch subdirectories |
| `watch_backend` | `Literal["auto", "watchdog", "polling"]` | `"auto"` | file_watch | `auto`: OS notifications through `watchdog` when installed, else polling. `watchdog` fails if it isn't installed. `polling` scans the directory (for network shares and bind mounts) |
| `watch_poll_interval` | `float` | `1.0` | file_watch | Seconds between scans with polling (> 0, <= 3600) |
| `watch_max_pending_events` | `int` | `10000` | file_watch | Changed files held while the process is busy; past that, new files are dropped and counted in `dropped_events` (>= 1) |
| `event_type` | `str \| None` | `None` | event | Required for `event`: the event type to subscribe to |
| `event_source` | `str \| None` | `None` | event | Only events from this source |
| `topic` | `str \| None` | `None` | message | Required for `message`: the broker topic (wildcards allowed) |
| `custom_config` | `dict[str, Any]` | `{}` | all | Settings for custom trigger types |
| `filter_expression` | `str \| Callable \| None` | `None` | all | Safe expression string or a callable taking the `TriggerEvent`; events that don't match skip the agent ([syntax](triggers/index.md#filtering-events-before-the-agent-runs)) |

---

## TriggerDeliveryConfig

How a process handles trigger events it can't process (`ProcessConfig.trigger_delivery`). Unknown keys are rejected.

| Field | Type | Default | Description |
|---|---|---|---|
| `max_retries` | `int` | `0` | Extra attempts for an event whose run raised (0-10). A retry re-runs the whole agent turn, so tools with side effects may run twice. |
| `retry_backoff` | `float` | `2.0` | Seconds before the first retry; doubles per attempt (> 0) |
| `retry_backoff_max` | `float` | `60.0` | Upper bound for the retry delay (> 0) |
| `dead_letter_size` | `int` | `100` | Entries kept in `process.dead_letters`, oldest dropped first (>= 0) |
| `scan_payloads` | `bool` | `False` | Prompt-injection scan of each payload before the agent sees it; flagged events are dead-lettered. Uses `guardrails` when it is a scanner, otherwise an injection-only `PromptiseSecurityScanner` (needs `transformers`) |
| `max_payload_chars` | `int` | `20000` | Longest payload text placed in the prompt; longer payloads are truncated (>= 200). The payload scan reads exactly this text |

---

## JournalConfig

The durable audit log and recovery source (`ProcessConfig.journal`). Unknown keys warn. See [Journal](journal/index.md).

| Field | Type | Default | Description |
|---|---|---|---|
| `level` | `Literal["none", "checkpoint", "full"]` | `"checkpoint"` | `"none"` disables the journal. `"checkpoint"` records transitions, run results, context updates and checkpoints. `"full"` also records trigger events and tool calls. |
| `backend` | `Literal["file", "memory"]` | `"file"` | Append-only JSONL files, or in memory (tests) |
| `path` | `str` | `".promptise/journal"` | Base directory of the file backend |
| `recover_on_start` | `bool` | `True` | On the first `start()` of a process object, restore context state, mission, budget counters, conversation history and invocation count from the last checkpoint of the same process name. `False` always starts fresh. |

`ProcessConfig()` uses `JournalConfig(level="none")`: no journal unless you set one. A [manifest](manifests.md) without a `journal:` section gets `JournalConfig()`, which journals at `"checkpoint"` level.

---

## ContextConfig

State, memory, conversation and environment (`ProcessConfig.context`). Unknown keys warn. See [Context & State](context.md).

| Field | Type | Default | Description |
|---|---|---|---|
| `writable_keys` | `list[str]` | `[]` | State keys the agent may write (empty = all) |
| `memory_provider` | `Literal["in_memory", "chroma", "mem0"] \| None` | `None` | Long-term memory provider (`None` = off) |
| `memory_max` | `int` | `5` | Memories injected per run (>= 1) |
| `memory_min_score` | `float` | `0.0` | Minimum relevance score (0-1) |
| `memory_timeout` | `float` | `5.0` | Seconds to wait for a memory search or auto-store (> 0) |
| `memory_auto_store` | `bool` | `False` | Store each exchange in long-term memory |
| `memory_collection` | `str` | `"agent_memory"` | ChromaDB collection |
| `memory_persist_directory` | `str \| None` | `None` | ChromaDB persist directory |
| `memory_user_id` | `str` | `"default"` | Mem0 user scope |
| `conversation_max_messages` | `int` | `100` | Messages kept in the conversation buffer (**0 = unlimited**) |
| `conversation_history` | `bool` | `True` | `False`: every run sees only its trigger event |
| `conversation_clear_on_stop` | `bool` | `False` | Clear the conversation history on `stop()`. By default it survives `stop()` / `start()` and restarts; `close()` always clears it. |
| `state_tool` | `bool` | `False` | Give the agent an `update_state(key, value)` tool (limited to `writable_keys` when set) |
| `file_mounts` | `dict[str, str]` | `{}` | Logical name to filesystem path |
| `env_prefix` | `str` | `"AGENT_"` | Only environment variables with this prefix are exposed. Must not be empty (an empty prefix would expose the whole environment) |
| `initial_state` | `dict[str, Any]` | `{}` | State at the first start. Keys recovered from the journal replace these. |

---

## HookConfig

One [lifecycle hook](hooks.md) (`ProcessConfig.hooks`): either an external `command` or an async Python `callback`, never both. Unknown keys are rejected.

| Field | Type | Default | Description |
|---|---|---|---|
| `event` | `str` | required | A `HookEvent` value, e.g. `"user_prompt_submit"`; an unknown event is rejected with the list of valid ones |
| `command` | `str \| list[str] \| None` | `None` | Program and arguments (a string is split like a shell would; no shell runs it). The event arrives on stdin as JSON. |
| `callback` | `Any \| None` | `None` | Async callable taking a `HookContext` (code only; never serialized) |
| `timeout` | `float \| None` | `None` | Seconds the hook may run: 5 for a command, 30 for a callback when unset (> 0, <= 3600) |
| `priority` | `int` | `0` | Higher runs first |
| `once` | `bool` | `False` | Run on the first matching event only |
| `tag` | `str \| None` | `None` | Group name for `HookManager.remove_by_tag()` |
| `fail_closed` | `bool` | `False` | An error or timeout of this hook blocks the action (blockable events only) |
| `cwd` | `str \| None` | `None` | Working directory of the command (command only) |
| `env` | `dict[str, SecretStr]` | `{}` | Extra environment variables for the command; `${VAR}` values are resolved when the hook runs (command only) |
| `env_passthrough` | `list[str]` | `[]` | Host variables passed through; the command otherwise gets a minimal environment (command only) |
| `max_output_bytes` | `int` | `65536` | Largest stdout accepted from the command (> 0, <= 10 MiB) |

---

## SecretScopeConfig

Per-process secrets (`ProcessConfig.secrets`). Unknown keys warn. See [Secret Scoping](governance/secrets.md).

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `False` | Turn secret scoping on |
| `secrets` | `dict[str, SecretStr]` | `{}` | Name to value or `${VAR}` / `${VAR:-default}` reference. References are resolved from the environment when the process starts, so the config holds no secret value. |
| `default_ttl` | `float \| None` | `None` | Seconds until a secret expires (`None` = never; > 0) |
| `ttls` | `dict[str, float]` | `{}` | Per-secret TTLs; every name must be in `secrets`, every value positive |
| `revoke_on_stop` | `bool` | `True` | Zero-fill and drop the secrets on `stop()`; `close()` always revokes them |

---

## BudgetConfig

Autonomy limits (`ProcessConfig.budget`). Unknown keys warn. See [Autonomy Budget](governance/budget.md).

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `False` | Turn budget tracking on |
| `max_tool_calls_per_run` | `int \| None` | `None` | Tool calls per run (>= 1) |
| `max_llm_turns_per_run` | `int \| None` | `None` | LLM turns per run (>= 1) |
| `max_cost_per_run` | `float \| None` | `None` | Cost units per run (> 0) |
| `max_irreversible_per_run` | `int \| None` | `None` | Irreversible tool calls per run (>= 0) |
| `max_tool_calls_per_day` | `int \| None` | `None` | Tool calls per day (>= 1) |
| `max_runs_per_day` | `int \| None` | `None` | Runs per day (>= 1) |
| `max_cost_per_day` | `float \| None` | `None` | Cost units per day (> 0) |
| `tool_costs` | `dict[str, ToolCostAnnotation]` | `{}` | Per-tool cost and irreversibility |
| `on_exceeded` | `Literal["pause", "stop", "escalate"]` | `"pause"` | `pause` suspends (reason `"budget"`), `stop` stops, `escalate` notifies `escalation` and suspends |
| `escalation` | `EscalationTarget \| None` | `None` | Where `escalate` sends its notification |
| `daily_reset_hour_utc` | `int` | `0` | UTC hour at which the daily counters reset (0-23) |
| `inject_remaining` | `bool` | `True` | Show the remaining budget in the agent's context |
| `warning_threshold` | `float` (0-1, above 0) | `0.8` | Fraction of a limit at which `budget.warning` is emitted |

### ToolCostAnnotation

| Field | Type | Default | Description |
|---|---|---|---|
| `cost_weight` | `float` | `1.0` | Cost units per call (> 0) |
| `irreversible` | `bool` | `False` | Counts toward `max_irreversible_per_run` |

### EscalationTarget

Used by `BudgetConfig`, `HealthConfig` and `MissionConfig`.

| Field | Type | Default | Description |
|---|---|---|---|
| `webhook_url` | `str \| None` | `None` | `http://` or `https://` URL that receives a JSON POST |
| `event_type` | `str \| None` | `None` | Event type emitted on the runtime's event bus. Needs `AgentRuntime(event_bus=...)` (`promptise runtime start` has an in-process bus); without a bus, or with no subscriber on an `InProcessEventBus`, a warning is logged and nothing is delivered |

---

## HealthConfig

Behavioral anomaly detection (`ProcessConfig.health`). Unknown keys warn. See [Behavioral Health](governance/health.md).

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `False` | Turn health monitoring on |
| `stuck_threshold` | `int` | `3` | Same tool with the same arguments this many times in a row is "stuck" (>= 2) |
| `loop_window` | `int` | `20` | Recent tool calls examined for loops (>= 4) |
| `loop_min_repeats` | `int` | `2` | Pattern repeats that count as a loop (>= 2) |
| `empty_threshold` | `int` | `3` | Trivial final replies in a row that count as an anomaly (>= 2) |
| `empty_max_chars` | `int` | `10` | A final reply of at most this many characters (stripped) is trivial; 0 counts only blank replies |
| `error_window` | `int` | `10` | Recent runs the error rate is computed over (>= 2) |
| `error_rate_threshold` | `float` | `0.5` | Error rate (in a full window) that counts as an anomaly (> 0, <= 1) |
| `on_anomaly` | `Literal["log", "pause", "escalate", "stop"]` | `"log"` | `log` only logs, `pause` suspends (reason `"health"`), `escalate` notifies `escalation` (when set) and suspends, `stop` stops the process. A `health.anomaly` event is emitted in every case. |
| `cooldown` | `float` | `300.0` | Seconds before the same anomaly is reported again (>= 0) |
| `escalation` | `EscalationTarget \| None` | `None` | Where `escalate` sends its notification |

---

## MissionConfig

Goal tracking with an LLM judge (`ProcessConfig.mission`). Unknown keys warn. See [Mission Model](governance/mission.md).

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `False` | Turn mission tracking on |
| `objective` | `str` | `""` | What the agent is trying to achieve |
| `success_criteria` | `str` | `""` | How completion is judged |
| `eval_model` | `str \| None` | `None` | Model for the judge (`None` = the process model) |
| `eval_every` | `int` | `1` | Evaluate every N runs (>= 1) |
| `confidence_threshold` | `float` | `0.7` | A verdict below this confidence pauses the mission (not the process) and notifies `escalation` when set (0-1) |
| `timeout_hours` | `float` | `0.0` | Hours before the mission fails (0 = no timeout) |
| `max_invocations` | `int` | `0` | Runs before the mission fails (0 = unlimited) |
| `auto_complete` | `bool` | `True` | Stop the process when the mission is achieved; `False` keeps it running and skips later runs |
| `on_complete` | `Literal["stop", "continue", "suspend"] \| None` | `None` | Deprecated and ignored (emits a `FutureWarning`); use `auto_complete` |
| `escalation` | `EscalationTarget \| None` | `None` | Where low-confidence escalations go |
| `success_check` | `Callable \| None` | `None` | Python check called with the `MissionEvidence` before the judge: `True` / `False` decide, `None` falls through. Code only; never serialized. |

---

## InboxConfig

The human-to-agent message inbox (`ProcessConfig.inbox`). Unknown keys are rejected. See [Orchestration API](api.md).

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `False` | Turn the inbox on |
| `max_messages` | `int` | `50` | Messages held at once (> 0) |
| `max_message_length` | `int` | `2000` | Characters per message (> 0) |
| `default_ttl` | `float` | `3600` | Seconds a message lives (0 = no expiry; >= 0); must not exceed `max_ttl` |
| `max_ttl` | `float` | `86400` | Longest TTL a sender may ask for (>= 0) |
| `rate_limit_per_sender` | `int` | `20` | Messages per sender per hour (> 0) |
| `scan_with_guardrails` | `bool` | `True` | Scan messages for prompt injection; flagged messages are refused |

---

## DistributedConfig

Distributed mode (`RuntimeConfig.distributed`). With `enabled=True`, `AgentRuntime.start_all()` starts this node's management transport and, depending on the role, the coordinator or the registration with one. Unknown keys warn. Inconsistent combinations raise a `ValidationError` that says what to change. See [Distributed Coordinator](distributed/coordinator.md).

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `False` | Start the transport (and coordination) with the runtime |
| `coordinator` | `bool` | `False` | This runtime hosts the cluster coordinator (not together with `coordinator_url`) |
| `coordinator_url` | `str \| None` | `None` | Coordinator to register with (`http(s)://` URL) |
| `node_id` | `str \| None` | `None` | This node's ID (default `<hostname>-<port>`) |
| `transport_host` | `str` | `"127.0.0.1"` | Interface the transport binds; a non-loopback address needs `auth_token` (or `allow_unauthenticated`) |
| `transport_port` | `int` | `9100` | Transport port; 0 picks a free one; 1-1024 are rejected |
| `advertise_url` | `str \| None` | `None` | URL other nodes use; required with a wildcard `transport_host` and a coordinator role |
| `auth_token` | `SecretStr \| None` | `None` | Cluster bearer token, or a `${VAR}` reference resolved when the transport starts |
| `allow_unauthenticated` | `bool` | `False` | Allow a non-loopback transport or coordinator without `auth_token` |
| `discovery_method` | `Literal["registry", "static", "multicast"]` | `"registry"` | `registry` (nodes register) or `static` (`static_nodes`). `multicast` is rejected: not implemented. |
| `static_nodes` | `dict[str, str]` | `{}` | `node_id` to URL, for a coordinator with `discovery_method="static"` |
| `allowed_node_hosts` | `list[str]` | `[]` | Hosts a registering node's URL may name (`*.example.com` matches subdomains); others get `403`. Empty: any host |
| `heartbeat_interval` | `float` | `15.0` | Seconds between health checks and re-registrations (> 0) |
| `node_timeout` | `float \| None` | `None` | Seconds of silence before a node is unhealthy or dropped (default 3 x `heartbeat_interval`) |

---

## Common pitfalls

- **A typo in a section config.** `HealthConfig(error_windw=5)` or `OpenModeConfig(max_rebuild=3)` only warns and keeps the default. Check warnings in tests, or define the config in a manifest, which rejects unknown keys.
- **Governance without `enabled=True`.** `BudgetConfig(max_runs_per_day=10)` alone tracks nothing.
- **`JournalConfig()` vs `ProcessConfig()`.** A `JournalConfig` you create journals at `"checkpoint"` level, but a `ProcessConfig` without one has no journal.
- **Round-tripping secrets.** `to_dict()` masks literal secrets; `from_dict()` of that dict raises until you put the real values (or `${VAR}` references) back.
- **`conversation_max_messages=0`.** It means unlimited, not disabled. Use `conversation_history=False` to turn short-term memory off.
- **Expecting `DEFAULT_PRODUCTION_CONFIG` to harden anything.** It is the same as the development preset.

---

## What's next

- [Agent Processes](processes.md): what the process does with these settings
- [Agent Manifests](manifests.md): the same settings in a `.agent` YAML file
- [Runtime Manager](runtime-manager.md): `AgentRuntime` and `RuntimeConfig`
- [Triggers Overview](triggers/index.md): trigger types in detail
- [Context](context.md): the state layer `ContextConfig` configures

!!! tip "Learn the concept"

    - [Autonomous AI Agent Runtime: Budgets, Crash Recovery, API](https://www.promptise.com/guides/view/autonomous-ai-agent-runtime): Which settings turn an agent into a bounded, recoverable process.
    - [AI Agent Auto-Restart: Restart Policies and Timeouts](https://www.promptise.com/guides/view/ai-agent-auto-restart): `restart_policy`, `max_restarts`, `restart_backoff`, `max_lifetime` and run timeouts in practice.
    - [Run Multiple AI Agents: One Runtime, Many Processes](https://www.promptise.com/guides/view/run-multiple-ai-agents): `RuntimeConfig` with several processes in one runtime.
