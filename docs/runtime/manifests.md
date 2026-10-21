---
seo_title: "Agent Manifests: YAML for Agent Processes"
description: "Declare an agent process in a .agent YAML manifest: model, instructions, MCP servers, triggers, state, governance and hooks, with strict validation and a CLI."
---

# Agent Manifests

A `.agent` manifest is a YAML file that declares one agent process: model, instructions, MCP servers, triggers, initial state, governance, hooks and process settings. Manifests let you review and version an agent's definition like any other config, validate it before deploying, and start it from the CLI without writing Python. The loader is strict: an unknown key anywhere fails with the list of valid keys, so a typo never silently keeps a default.

```yaml
# watcher.agent
version: "1.0"
name: data-watcher
model: openai:gpt-5-mini
instructions: |
  You monitor data pipelines and alert on anomalies.

servers:
  data_tools:
    type: http
    url: http://localhost:8000/mcp

triggers:
  - type: cron
    cron_expression: "*/5 * * * *"

config:
  restart_policy: on_failure
```

```bash
promptise runtime validate watcher.agent
promptise runtime start watcher.agent
```

---

## Top-level keys

`AgentManifestSchema` accepts exactly these keys. Only `name` is required.

| Key | Type | Default | Becomes |
|---|---|---|---|
| `version` | `"1.0"` | `"1.0"` | Schema version; any other value is rejected |
| `name` | `str` | required | The process name |
| `model` | `str` | `"openai:gpt-5-mini"` | `ProcessConfig.model` |
| `instructions` | `str` | none | `ProcessConfig.instructions` |
| `identity` | mapping | none | `ProcessConfig.identity` (see [identity](#identity)) |
| `servers` | mapping | `{}` | `ProcessConfig.servers` |
| `triggers` | list | `[]` | `ProcessConfig.triggers` (each a `TriggerConfig`) |
| `world` | mapping | `{}` | `context.initial_state` |
| `memory` | mapping | none | `context.memory_*` fields |
| `context` | mapping | none | Any `ContextConfig` field |
| `journal` | mapping | `JournalConfig()` | `ProcessConfig.journal` (see [journal](#journal)) |
| `execution_mode` | `"strict"` or `"open"` | `"strict"` | `ProcessConfig.execution_mode` |
| `open_mode` | mapping | defaults | `OpenModeConfig` |
| `secrets` | mapping | disabled | `SecretScopeConfig` |
| `budget` | mapping | disabled | `BudgetConfig` |
| `health` | mapping | disabled | `HealthConfig` |
| `mission` | mapping | disabled | `MissionConfig` |
| `hooks` | list | `[]` | `ProcessConfig.hooks` (command hooks) |
| `config` | mapping | `{}` | Any other `ProcessConfig` field |
| `entrypoint` | `str` | none | Reserved and not read; setting it emits a `FutureWarning` |

Every field of every section is listed on the [Configuration](configuration.md) page.

---

## Strict validation

`load_manifest()` rejects:

| Problem | Error message (inside `ManifestValidationError`) |
|---|---|
| Unknown top-level key (`journal_level: full`) | `manifest: unknown key(s) ['journal_level']; valid keys are [...] (did you mean 'journal_level' -> 'journal'?)` |
| Unknown key in a section (`health:`, `context:`, `journal:`, `open_mode:`, `secrets:`, `budget:`, `mission:`) | `health: unknown key(s) ['error_windw']; valid keys are ['cooldown', ..., 'stuck_threshold']` |
| Unknown key in `memory:` | `memory: unknown key(s) ['provder']; valid keys are ['auto_store', 'collection', 'max', 'min_score', 'persist_directory', 'provider', 'timeout', 'user_id'] (did you mean 'provder' -> 'provider'?)` |
| Unknown key in `config:` | `config: unknown key(s) ['concurency']; valid keys are ['adaptive', 'approval', ...] (did you mean 'concurency' -> 'concurrency'?)` |
| A `config:` key that has its own section | `config: ['instructions'] also set by the manifest's own section(s); set each option in one place (the top-level section)` |
| `world:` and `context: initial_state` both set | `Initial state is set twice, in 'world:' and in 'context: initial_state'; use one of them` |
| The same option in `memory:` and `context:` | `'memory: provider' and 'context: memory_provider' set the same option; use one of them` |
| Unknown key in a server entry | `servers.tools: timout: Extra inputs are not permitted` |
| A server without `url` or `command` | `servers.tools: needs 'url' (an HTTP server) or 'command' (a stdio server)` |
| A trigger without `type` | `Trigger at index 0 is missing required 'type' field` |
| An invalid trigger (an unknown key, a missing `cron_expression`, a bad time zone) | `triggers[0] (cron): unknown key(s) ['cron']; valid keys are [...]` |
| A hook with an unknown event | `hooks[0]: ... unknown hook event 'on_start'; valid events are ['session_start', ...]` |
| A hook with `callback:` | `hooks[0]: a manifest hook runs a 'command'; Python callbacks are registered in code (process.hooks)` |
| Invalid `execution_mode` | `execution_mode: 'auto' is not one of ['strict', 'open']` |

The exception message starts with `Manifest validation failed:`; `exc.errors` holds the Pydantic error details and `exc.file_path` the file.

Trigger entries are checked in full (unknown keys, the fields their type needs, a bad time zone) when the manifest is loaded, so `load_manifest()`, `promptise runtime validate` and `promptise runtime start` all report a bad trigger. Unknown keys, here and in every other section, come with "did you mean" hints.

A `${VAR}` that is not set raises `ManifestEnvVarError`, which is both a `ManifestError` and an `EnvVarNotFoundError`. `promptise runtime validate` reports it for that file and goes on with the next one; `promptise runtime start` checks every manifest first and starts nothing when one fails.

---

## Sections

### identity

Gives the process a stable, traceable [Agent Identity](../identity/overview.md). Same shape as the [`.superagent` identity block](../core/agents/superagent-files.md#identity): a local identity tags the process's actions for attribution; a verifiable identity (Entra, AWS, GCP, SPIFFE, OIDC, or `auto`) also presents a signed credential, scoped to each server's `audience`, to the MCP servers the process calls.

```yaml
identity:
  provider: oidc                 # local|entra|aws|gcp|spiffe|oidc|auto
  agent_id: data-watcher
  owner: platform
  issuer: https://gitlab.com
  token_env_var: CI_JOB_JWT_V2
```

For `provider: local` only `agent_id` is required; for `provider: oidc`, `issuer` plus exactly one of `token_file` / `token_env_var`. See the [`.superagent` reference](../core/agents/superagent-files.md#identity) for every field and the [Agent Identity guide](../identity/guide.md) for the end-to-end flow.

### servers

MCP servers, the same format as `.superagent` files. `type` is `http`, `streamable-http`, `sse` or `stdio`; an HTTP server needs `url`, a stdio server `command`. Every key is passed to the server spec, and an unknown one fails.

```yaml
servers:
  my_tools:
    type: http
    url: http://localhost:8000/mcp
    audience: api://my-tools     # optional: identity credential audience
  local_tools:
    type: stdio
    command: python
    args: ["-m", "mytools.server"]
```

### triggers

A list of trigger configurations, each with a `type` and the fields of that type (see [Triggers](triggers/index.md)):

```yaml
triggers:
  - type: cron
    cron_expression: "0 9 * * 1-5"
    cron_timezone: Europe/Zurich
  - type: webhook
    webhook_path: /events
    webhook_port: 9090
    hmac_secret: ${WEBHOOK_SECRET}
  - type: file_watch
    watch_path: /data/inbox
    watch_patterns: ["*.csv", "*.json"]
    watch_backend: polling
  - type: event
    event_type: pipeline.degraded
```

### world and context

`world:` is the initial key-value state of the process's [`AgentContext`](context.md). `context:` accepts any `ContextConfig` field, including `initial_state`; use one of `world:` or `context: initial_state`, not both. Keys recovered from the journal on start replace these initial values.

```yaml
world:
  pipeline_status: unknown
  alerts_created: 0

context:
  writable_keys: [pipeline_status, alerts_created]
  conversation_max_messages: 50
  state_tool: true          # gives the agent an update_state(key, value) tool
  env_prefix: PIPELINE_
```

Equivalent, with the state inside `context:`:

```yaml
context:
  initial_state:
    pipeline_status: unknown
    alerts_created: 0
  writable_keys: [pipeline_status, alerts_created]
```

### memory

Short names for the long-term memory fields of `ContextConfig`:

| `memory:` key | Sets |
|---|---|
| `provider` | `memory_provider` (`in_memory`, `chroma`, `mem0`) |
| `auto_store` | `memory_auto_store` |
| `max` | `memory_max` |
| `min_score` | `memory_min_score` |
| `timeout` | `memory_timeout` |
| `collection` | `memory_collection` |
| `persist_directory` | `memory_persist_directory` |
| `user_id` | `memory_user_id` |

```yaml
memory:
  provider: chroma
  collection: agent_memory
  persist_directory: .promptise/chroma
  max: 5
```

You can set the same options as `memory_*` keys under `context:` instead, but not both ways for one option.

### journal

```yaml
journal:
  level: full             # none | checkpoint | full
  backend: file           # file | memory
  path: .promptise/journal
  recover_on_start: true
```

A manifest without a `journal:` section gets `JournalConfig()`: `level: checkpoint`, files under `.promptise/journal`, and recovery on start. A process loaded from a manifest therefore journals and, when started again under the same name, continues from its last checkpoint. Set `level: none` to turn the journal off, or `recover_on_start: false` to keep the journal but always start fresh. (A `ProcessConfig` built in Python has no journal unless you set one.)

### Governance

`secrets:`, `budget:`, `health:` and `mission:` take the fields of `SecretScopeConfig`, `BudgetConfig`, `HealthConfig` and `MissionConfig`. Each needs `enabled: true`.

```yaml
secrets:
  enabled: true
  secrets:
    api_key: ${MONITORING_API_KEY}
  default_ttl: 3600

budget:
  enabled: true
  max_tool_calls_per_run: 20
  max_cost_per_day: 50
  on_exceeded: pause

health:
  enabled: true
  on_anomaly: escalate
  escalation:
    webhook_url: https://hooks.example.com/agent-alerts

mission:
  enabled: true
  objective: Keep every pipeline healthy.
  success_criteria: No pipeline degraded for more than 15 minutes.
  eval_every: 10
```

`MissionConfig.success_check` is Python only and can't be set in a manifest.

### hooks

A list of [lifecycle hooks](hooks.md) that run an external command. Each entry takes the `HookConfig` fields for a command hook: `event`, `command`, `timeout`, `priority`, `once`, `tag`, `fail_closed`, `cwd`, `env`, `env_passthrough`, `max_output_bytes`. The event arrives on the command's stdin as JSON; no shell runs it.

```yaml
hooks:
  - event: user_prompt_submit
    command: ["./hooks/audit.sh"]
    timeout: 5
    fail_closed: true
  - event: session_end
    command: "python hooks/notify.py --channel ops"
```

Python callbacks are registered in code: `process.hooks.on(...)` or `HookConfig(callback=...)`.

### Open mode

```yaml
execution_mode: open

open_mode:
  allow_identity_change: true
  allow_tool_creation: true
  allow_mcp_connect: false
  allow_trigger_management: true
  allow_memory_management: true
  max_custom_tools: 5
  max_rebuilds: 10
  sandbox_custom_tools: true
```

`allow_tool_creation: true` lets the model run Python in the runtime's process; restricted builtins don't contain it. Keep it off unless that is acceptable. See [Meta-Tools](meta-tools.md#security).

### config

Any `ProcessConfig` field that has no section of its own, for example:

```yaml
config:
  concurrency: 2
  heartbeat_interval: 30
  idle_timeout: 3600
  max_lifetime: 86400
  max_consecutive_failures: 5
  restart_policy: on_failure
  max_restarts: 5
  restart_backoff: 2
  max_invocation_time: 300
  inbox:
    enabled: true
  trigger_delivery:
    max_retries: 2
    dead_letter_size: 200
```

A key that has its own section (`model`, `instructions`, `identity`, `execution_mode`, `open_mode`, `servers`, `triggers`, `journal`, `context`, `secrets`, `budget`, `health`, `mission`, `hooks`) is refused in `config:` when that section is also set; set each option in one place.

---

## Environment variables and secrets

`${VAR}` and `${VAR:-default}` references are resolved from the environment when the manifest is loaded, in every value of the file, with one exception: the values under `secrets.secrets`. Those stay references in the loaded config and are resolved by the process's [secret scope](governance/secrets.md) when it starts, so the config, `repr()` and any dump of it never hold the secret value. A reference to an unset variable without a default fails at load time in both cases:

```text
ManifestEnvVarError: agents/monitor.agent: Environment variable 'MONITORING_API_KEY' not found (referenced in: secrets.secrets.api_key)
```

To keep a credential out of plain config, put it in `secrets.secrets`. In [open mode](meta-tools.md) the agent reads it with the `get_secret` tool; a strict-mode process has no such tool, so pass the credential to your own code or MCP server instead (see [Secret scoping](governance/secrets.md)). Other secret-bearing values, such as a webhook's `hmac_secret` or a server's credential headers, are resolved at load time; they are stored as `SecretStr` (masked in `repr()` and JSON dumps) but held in memory in plain form.

```yaml
secrets:
  enabled: true
  secrets:
    db_password: ${DB_PASSWORD}             # resolved when the process starts
    region: ${AWS_REGION:-eu-central-1}     # with a default
```

---

## Full example

```yaml
version: "1.0"
name: pipeline-watcher
model: openai:gpt-5-mini
instructions: |
  You are a data pipeline watchdog. When triggered, run a
  system_health_check. If a pipeline is degraded, get its metrics
  and create an alert.

servers:
  pipeline_tools:
    type: http
    url: http://127.0.0.1:8200/mcp

triggers:
  - type: cron
    cron_expression: "*/5 * * * *"
  - type: webhook
    webhook_path: /alerts
    webhook_port: 9091
    hmac_secret: ${WEBHOOK_SECRET}

world:
  pipeline_status: unknown
  alerts_created: 0

memory:
  provider: in_memory
  max: 5

journal:
  level: full
  path: .promptise/journal

budget:
  enabled: true
  max_tool_calls_per_run: 20
  on_exceeded: pause

hooks:
  - event: session_start
    command: ["./hooks/announce.sh"]

config:
  heartbeat_interval: 30
  idle_timeout: 3600
  max_consecutive_failures: 5
  restart_policy: on_failure
  inbox:
    enabled: true
```

---

## Python API

```python
from promptise.runtime import (
    AgentProcess, load_manifest, manifest_to_process_config, save_manifest, validate_manifest,
)

manifest = load_manifest("agents/watcher.agent")       # AgentManifestSchema
config = manifest_to_process_config(manifest)          # ProcessConfig
process = AgentProcess(name=manifest.name, config=config)

warnings = validate_manifest("agents/watcher.agent")
# ['No MCP servers configured — agent has no tools']

save_manifest(manifest, "agents/watcher-copy.agent")
```

| Function | Returns | Raises |
|---|---|---|
| `load_manifest(path)` | `AgentManifestSchema` | `ManifestError` (missing file, unreadable, invalid YAML, not a mapping), `ManifestValidationError` (schema), `ManifestEnvVarError` (unset `${VAR}`; also an `EnvVarNotFoundError`) |
| `validate_manifest(path)` | `list[str]` of warnings: no instructions, no triggers, no MCP servers | as `load_manifest()` |
| `manifest_to_process_config(manifest)` | `ProcessConfig` | `pydantic.ValidationError` (for example an invalid trigger) |
| `save_manifest(manifest, path)` | `None` | |

`save_manifest()` writes the loaded manifest, so `${VAR}` references outside `secrets.secrets` are written with their resolved values. `AgentRuntime.load_manifest(path, name_override=None)` and `AgentRuntime.load_directory(dir)` load and register in one step (see [Runtime Manager](runtime-manager.md#loading-manifests)).

---

## CLI

```bash
# Validate: schema check plus a summary table and warnings (a file or a directory)
promptise runtime validate agents/watcher.agent
promptise runtime validate agents/

# Start one manifest, or every *.agent file in a directory
promptise runtime start agents/watcher.agent
promptise runtime start agents/
promptise runtime start agents/watcher.agent --name custom-watcher
promptise runtime start agents/watcher.agent --dashboard
promptise runtime start agents/ --detach

# Generate a template: basic, cron, webhook or full
promptise runtime init --template cron -o watcher.agent
```

See [CLI Commands](cli.md) for every option.

---

## Common pitfalls

- **Expecting `get_secret` in strict mode.** The agent's `get_secret` tool exists only in open mode; in strict mode `secrets.secrets` holds values for your code, not for the model.
- **A manifest process that "remembers" old state.** Without a `journal:` section the process journals to `.promptise/journal` and recovers on start. Set `journal: {level: none}` or `recover_on_start: false` if each start should be fresh.
- **Putting an option in two places.** `config: {instructions: ...}` next to a top-level `instructions:` fails; so do `world:` together with `context: initial_state`.
- **Secrets in plain values.** Only `secrets.secrets` keeps `${VAR}` references until start. A literal API key anywhere else is in the file and in the loaded config.
- **Old keys from earlier examples.** `journal_level`, `restart_on_failure` and similar keys don't exist; the loader rejects them with the list of valid keys.
- **Using `entrypoint`.** It is not read. Declare command hooks in `hooks:` and register Python hooks in code.

---

## What's next

- [Configuration](configuration.md) -- every field of every section
- [Agent Processes](processes.md) -- what the process does with its config
- [Runtime Manager](runtime-manager.md) -- load many manifests into one runtime
- [Runtime Hooks](hooks.md) -- hook events and their data
- [CLI Commands](cli.md) -- `promptise runtime validate`, `start` and `init`

!!! tip "Learn the concept"

    - [AI Agent YAML: Define and Run an Agent From a Config File](https://www.promptise.com/guides/view/ai-agent-yaml-config-file) -- Keep an agent's definition in a reviewed config file.
    - [Run Multiple AI Agents: One Runtime, Many Processes](https://www.promptise.com/guides/view/run-multiple-ai-agents) -- Load a directory of manifests into one runtime.
    - [AI Agent Secrets Management: Scoped, Expiring Credentials](https://www.promptise.com/guides/view/ai-agent-secrets-management) -- Keep credentials out of config with scoped secrets.
