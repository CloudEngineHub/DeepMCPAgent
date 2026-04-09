# Agent Manifests

Define agent processes declaratively using `.agent` YAML manifest files. Manifests let you version-control agent configurations, validate them before deployment, and start processes from the CLI without writing Python.

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
```

```bash
promptise runtime validate watcher.agent
promptise runtime start watcher.agent
```

---

## Manifest Schema

The `.agent` format is a YAML file validated against `AgentManifestSchema`. All sections except `version` and `name` are optional.

### Top-Level Fields

| Field | Type | Required | Description |
|---|---|---|---|
| `version` | `str` | yes | Schema version. Must be `"1.0"` |
| `name` | `str` | yes | Unique process name |
| `model` | `str` | no | LLM model ID (default: `"openai:gpt-5-mini"`) |
| `instructions` | `str` | no | System prompt for the agent |
| `servers` | `dict` | no | MCP server specifications |
| `triggers` | `list` | no | Trigger configurations |
| `world` | `dict` | no | Initial world state (key-value pairs) |
| `memory` | `dict` | no | Memory provider configuration |
| `journal` | `dict` | no | Journal (audit log) configuration |
| `config` | `dict` | no | Additional `ProcessConfig` overrides |
| `execution_mode` | `str` | no | `"strict"` (default) or `"open"` |
| `open_mode` | `dict` | no | Open mode guardrails |
| `entrypoint` | `str` | no | Python module for custom hooks |

### `servers` Section

MCP server specifications, same format as `.superagent` files:

```yaml
servers:
  my_tools:
    type: http
    url: http://localhost:8000/mcp
  local_tools:
    type: stdio
    command: python
    args: ["-m", "mytools.server"]
```

### `triggers` Section

A list of trigger configurations. Each entry must have a `type` field:

```yaml
triggers:
  - type: cron
    cron_expression: "*/5 * * * *"
  - type: webhook
    webhook_path: /events
    webhook_port: 9090
  - type: file_watch
    watch_path: /data/inbox
    watch_patterns: ["*.csv", "*.json"]
  - type: event
    event_type: pipeline.degraded
```

### `world` Section

Initial key-value state injected into `AgentContext`:

```yaml
world:
  pipeline_status: unknown
  last_check: null
  alerts_created: 0
```

### `memory` Section

Configure the long-term memory provider:

```yaml
memory:
  provider: in_memory     # "in_memory", "chroma", or "mem0"
  auto_store: false       # Auto-store exchanges in memory
  max: 5                  # Max memories injected per invocation
  min_score: 0.0          # Min relevance score
  # ChromaDB-specific:
  collection: agent_memory
  persist_directory: .promptise/chroma
  # Mem0-specific:
  user_id: default
```

### `journal` Section

Durable audit log configuration:

```yaml
journal:
  level: full             # "none", "checkpoint", or "full"
  backend: file           # "file" or "memory"
  path: .promptise/journal
```

### `config` Section

Any additional `ProcessConfig` fields not covered by other sections:

```yaml
config:
  concurrency: 2
  heartbeat_interval: 30
  idle_timeout: 3600
  max_consecutive_failures: 5
  restart_policy: on_failure
  max_restarts: 5
```

### Open Mode

Enable self-modifying agent behavior with execution mode and guardrails:

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

---

## Full Example

This is the `pipeline_watcher.agent` manifest from the examples directory:

```yaml
version: "1.0"
name: pipeline-watcher
model: openai:gpt-5-mini
instructions: |
  You are a Data Pipeline Watchdog. When triggered, run a
  system_health_check to assess infrastructure status. If any
  pipeline is degraded, get detailed metrics and create an alert.

servers:
  pipeline_tools:
    type: http
    url: http://127.0.0.1:8200/mcp

triggers:
  - type: cron
    cron_expression: "*/5 * * * *"

world:
  pipeline_status: unknown
  last_check: null
  alerts_created: 0

memory:
  provider: in_memory
  auto_store: false
  max: 5

journal:
  level: full
  backend: file
  path: .promptise/journal

config:
  concurrency: 1
  heartbeat_interval: 30
  idle_timeout: 3600
  max_consecutive_failures: 5
  restart_policy: on_failure
```

---

## API Reference

### `load_manifest(path)`

Load and validate a `.agent` manifest file.

```python
from promptise.runtime.manifest import load_manifest

manifest = load_manifest("agents/watcher.agent")
print(manifest.name)        # "pipeline-watcher"
print(manifest.model)       # "openai:gpt-5-mini"
print(len(manifest.triggers))  # 1
```

**Returns:** `AgentManifestSchema`
**Raises:** `ManifestError` (file/parse errors), `ManifestValidationError` (schema errors)

### `validate_manifest(path)`

Validate a manifest file and return a list of warnings.

```python
from promptise.runtime.manifest import validate_manifest

warnings = validate_manifest("agents/watcher.agent")
# ["No triggers defined -- agent will only respond to manual events"]
```

**Returns:** `list[str]` -- warning messages (empty if fully valid)

### `manifest_to_process_config(manifest)`

Convert a validated manifest into a `ProcessConfig` for use with `AgentProcess`.

```python
from promptise.runtime.manifest import load_manifest, manifest_to_process_config
from promptise.runtime import AgentProcess

manifest = load_manifest("agents/watcher.agent")
config = manifest_to_process_config(manifest)
process = AgentProcess(name=manifest.name, config=config)
```

**Returns:** `ProcessConfig`

### `save_manifest(manifest, path)`

Write a manifest to a YAML file.

```python
from promptise.runtime.manifest import save_manifest

save_manifest(manifest, "agents/watcher-copy.agent")
```

---

## CLI Commands

### Validate a Manifest

```bash
promptise runtime validate agents/watcher.agent
```

Checks schema validity and prints a summary table with name, model, servers, and triggers.

### Start from a Manifest

```bash
# Start a single manifest
promptise runtime start agents/watcher.agent

# Start all manifests in a directory
promptise runtime start agents/

# Start with the live dashboard
promptise runtime start agents/watcher.agent --dashboard

# Override the process name
promptise runtime start agents/watcher.agent --name custom-watcher
```

### Generate a Template

```bash
# Basic template
promptise runtime init -o my-agent.agent

# Cron-based template
promptise runtime init --template cron -o watcher.agent

# Full-featured template
promptise runtime init --template full -o production.agent
```

Available templates: `basic`, `cron`, `webhook`, `full`.

---

!!! tip "Environment variable resolution"
    Manifests support environment variable resolution. If the `promptise.env_resolver` module is available, `${VAR_NAME}` patterns in YAML values are replaced with their environment variable values at load time.

---

## What's Next?

- [Agent Processes](processes.md) -- `ProcessConfig` fields and trigger types in detail
- [Runtime Manager](runtime-manager.md) -- multi-process management with `AgentRuntime`
- [CLI Reference](../core/cli.md) -- full CLI command reference
