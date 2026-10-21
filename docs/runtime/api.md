---
seo_title: "Agent Orchestration REST API and Client"
description: "Control a running agent runtime over HTTP: deploy and stop processes, change instructions, budgets and triggers, send messages, rotate secrets, read journals."
---

# Orchestration API

`OrchestrationAPI` is a REST API for a running `AgentRuntime`. Use it to deploy, start, stop and remove agent processes, change their instructions, budgets and triggers while they run, send them messages, rotate their secrets and read their journals, all over HTTP. `OrchestrationClient` is the matching typed Python client. `promptise runtime start` serves this API for you (see [CLI Commands](cli.md)); in your own program you start it yourself.

```python
import asyncio

from promptise.runtime import AgentRuntime, OrchestrationAPI, OrchestrationClient


async def main():
    runtime = AgentRuntime()
    api = OrchestrationAPI(runtime, port=9100, auth_token="change-me")
    await api.start()

    async with OrchestrationClient("http://127.0.0.1:9100", token="change-me") as client:
        await client.deploy(
            "monitor",
            {
                "model": "openai:gpt-5-mini",
                "instructions": "Watch the data pipelines and report problems.",
                "triggers": [{"type": "cron", "cron_expression": "*/5 * * * *"}],
                "inbox": {"enabled": True},
            },
            start=True,
        )
        status = await client.get_process("monitor")
        print(status["state"])  # running

        await client.send_message(
            "monitor", "Ignore staging alerts for 1 hour",
            message_type="directive", priority="high", ttl=3600,
        )

    await api.stop()
    await runtime.stop_all()


asyncio.run(main())
```

---

## Creating the server

```python
OrchestrationAPI(
    runtime,
    *,
    host="127.0.0.1",
    port=9100,
    auth_token=None,
    on_shutdown=None,
    allow_command_deploys=False,
    allow_env_references=False,
    allowed_env_vars=None,
    data_dir=None,
)
```

| Argument | Default | Description |
|---|---|---|
| `runtime` | required | The `AgentRuntime` to manage |
| `host` | `"127.0.0.1"` | Bind address |
| `port` | `9100` | Port. `0` lets the OS pick a free port; read it from `api.port` after `start()` |
| `auth_token` | `None` | Bearer token required on every route except `GET /api/v1/health`. Required when `host` is not a loopback address. `"${VAR}"` reads it from the environment |
| `on_shutdown` | `None` | Sync or async callable without arguments. When set, `POST /api/v1/runtime/shutdown` exists: it answers `202` and then calls the callable (for example `runtime.request_shutdown`). Without it the route answers `404` |
| `allow_command_deploys` | `False` | Let `POST /api/v1/processes` deploy configs that run programs or code on the host (stdio MCP servers, command hooks, open mode with `allow_tool_creation`). See [Deploys that run programs](#deploys-that-run-programs) |
| `allow_env_references` | `False` | Let deployed configs use `${VAR}` references to any environment variable of the host. See [Environment references](#environment-references) |
| `allowed_env_vars` | `None` | Names of the variables deployed configs may reference when `allow_env_references` is off, e.g. `{"SEARCH_API_KEY"}` |
| `data_dir` | `./.promptise` | The directory file system paths in deployed configs must lie in. See [File system paths](#file-system-paths) |

`await api.start()` binds the port and serves; `await api.stop()` closes the server. The constructor raises `ValueError` when `auth_token` is an empty string, names an unset environment variable, or is missing for a non-loopback `host`, and `TypeError` when `on_shutdown` is not callable or `allowed_env_vars` is a single string.

---

## Authentication

When `auth_token` is set, every request except `GET /api/v1/health` needs the token, including requests to unknown paths:

```
Authorization: Bearer <your-token>
```

- **Loopback** (`127.0.0.1`, `localhost`, `::1`): the token is optional.
- **Anything else**: the token is **required**. The constructor refuses to build the API without one.
- The comparison is timing-safe.
- `auth_token="${ORCHESTRATION_API_TOKEN}"` reads the token from the environment; an unset variable raises `ValueError`.

A missing or wrong token gets `401 UNAUTHORIZED`.

### Browsers can't drive a local API

A loopback bind is reachable by any web page open in a browser on the same machine. On a loopback bind the API therefore refuses:

| Request | Status | Why |
|---|---|---|
| `Host` header that isn't a loopback name | `421` `MISDIRECTED_REQUEST` | DNS rebinding: a page whose own host name resolves to `127.0.0.1` |
| `Origin` header from a non-loopback site | `403` `CROSS_ORIGIN_REFUSED` | A cross-site `fetch` or form post from a page you visit |

`curl`, scripts and the Promptise clients send a loopback `Host` and no `Origin`, so they are unaffected. A local web UI served from `http://localhost:<port>` is allowed. Set an `auth_token` even on loopback if other users share the machine. Behind a reverse proxy, keep the proxy's upstream `Host` on a loopback name, or bind the API to a non-loopback address with a token.

### Request bodies

Request bodies must be JSON objects. Anything else (a list, a string, invalid JSON) gets `400 INVALID_JSON`; routes that take no body accept an empty one. A body over 1 MiB gets `413 PAYLOAD_TOO_LARGE`.

---

## Securing the control API

Anyone with the token can deploy agents, so treat a token holder as someone who writes process configs on your machine. The API keeps such a config from reaching further than the agent itself:

| Setting | Default | What it protects |
|---|---|---|
| `auth_token` | random with `promptise runtime start`, else `None` | Every route except `GET /health`. Required off loopback |
| Loopback bind (`host="127.0.0.1"`) | on | Only local clients; browser pages are refused (`421` / `403`) |
| `allow_command_deploys` | `False` | Deploys can't start programs or run agent-written code on the host |
| `allow_env_references` / `allowed_env_vars` | off / none | Deploys can't read the host's environment through `${VAR}` |
| `data_dir` | `./.promptise` | Deploys can't write journals, memory or watch files elsewhere |
| `open_mode.allowed_mcp_urls` | empty = **no** server for API deploys | A deployed open-mode agent can't connect MCP servers you didn't list |

```python
api = OrchestrationAPI(
    runtime,
    host="127.0.0.1",
    auth_token="${ORCHESTRATION_API_TOKEN}",
    allowed_env_vars={"SEARCH_API_KEY"},  # the only variable deploys may reference
    data_dir="/var/lib/promptise",
)
```

The same switches exist on the CLI: `promptise runtime start --allow-command-deploys --allow-env-references --allow-env-var NAME`. Processes you load from manifests on the host are yours and none of these checks apply to them. The process keeps the limits it was deployed with (secrets, MCP allowlist, paths) and passes them on to the processes it spawns.

### Deploys that run programs

A config that starts a program, or runs agent-written code, on the runtime's host can't be deployed over the API by default. `POST /api/v1/processes` answers `403 COMMANDS_NOT_ALLOWED` when the config has:

- a stdio MCP server (a server entry with a `command`, or `type`/`transport` `"stdio"`),
- a command hook (a `hooks` entry with a `command`), or
- open mode with `open_mode.allow_tool_creation` (the default in open mode). The agent's `create_tool` runs model-written Python inside the runtime's process, and its restricted builtins are not a sandbox, so it amounts to running commands. Deploy open-mode agents with `"open_mode": {"allow_tool_creation": false}`.

Anyone with the token could otherwise run any command as the runtime's user. Use HTTP MCP servers instead of stdio ones and leave out command hooks (a deploy is JSON, so it can't carry a Python callback hook either; register hooks on the host, in a manifest or in code), load such processes from manifests on the host, or, if every token holder may run commands there, enable it:

```python
api = OrchestrationAPI(runtime, auth_token="${ORCHESTRATION_API_TOKEN}", allow_command_deploys=True)
```

```bash
promptise runtime start agents/ --allow-command-deploys
```

The check applies only to deploys. Processes loaded from manifests by the operator are not checked, and the other routes that change a process (triggers, `PATCH` sections, context, instructions) have no field that runs a program.

### Environment references

`${VAR}` references are resolved from the host's environment when a process starts (for example in `secrets`), and an agent can read a secret back with `get_secret`. So a deploy, a `POST .../triggers` or a `PATCH` of `budget`, `health` or `mission` whose body contains a `${VAR}` reference anywhere gets `403 ENV_REFERENCES_NOT_ALLOWED`, naming where the reference is. `instructions` are never resolved, so text there isn't checked. To allow references:

- `allowed_env_vars={"SEARCH_API_KEY"}` (`--allow-env-var SEARCH_API_KEY`, repeatable): only these variables, or
- `allow_env_references=True` (`--allow-env-references`): any variable.

Otherwise send the value itself as a literal secret. As a backstop, an API-deployed process's secret scope resolves only the allowed variables too. A reference that can't be resolved fails the start with a generic `START_FAILED` message that doesn't say whether the variable exists (the server log has the detail).

### File system paths

Paths in a deployed config must lie inside `data_dir` (default `./.promptise` under the runtime's working directory, where the journal goes by default). Relative paths are resolved against the working directory, as the process uses them. `403 PATH_NOT_ALLOWED` answers a deploy whose

- `journal.path` (for a file journal that is on),
- `context.memory_persist_directory`, or
- a file-watch trigger's `watch_path`

is outside it, uses `..` or `~`, or is a symlink that leads out. `POST .../triggers` checks `watch_path` the same way, and the deployed agent's own `add_trigger` (open mode) is held to `data_dir` too.

---

## Route reference

Every route the server registers. Paths start with `/api/v1`. All routes except `GET /health` need the bearer token when one is set; `{name}` is the process name.

### Runtime

| Method | Path | What it does | Success |
|---|---|---|---|
| `GET` | `/health` | Load-balancer probe, no auth: `{"status": "healthy", "process_count"}` | `200` |
| `GET` | `/runtime/status` | `{"process_count", "processes": {name: status}}` (plus `cluster` in distributed mode) | `200` |
| `POST` | `/runtime/start-all` | Start every process: `{"status": "started"}` | `200` |
| `POST` | `/runtime/stop-all` | Stop every process: `{"status": "stopped"}` | `200` |
| `POST` | `/runtime/shutdown` | Only with `on_shutdown`: answers `{"status": "shutting_down"}`, then calls `on_shutdown` | `202` |

### Processes

| Method | Path | What it does | Success |
|---|---|---|---|
| `GET` | `/processes` | `{"processes": [{"name", "state", "process_id"}], "total"}` | `200` |
| `POST` | `/processes` | Deploy: body `{"name", "config", "start"}` (see [Deploying a process](#deploying-a-process)) | `201` |
| `GET` | `/processes/{name}` | Process status: state, counters, triggers, budget, health, mission state, active secret count | `200` |
| `DELETE` | `/processes/{name}` | Stop and close the process, then remove it (memory closed, secrets revoked, conversation cleared) | `204` |
| `POST` | `/processes/{name}/start` | Start: `{"name", "state"}` | `200` |
| `POST` | `/processes/{name}/stop` | Stop: `{"name", "state"}` | `200` |
| `POST` | `/processes/{name}/restart` | Stop (if running), then start: `{"name", "state"}` | `200` |
| `POST` | `/processes/{name}/suspend` | Pause trigger processing: `{"name", "suspended": true, "state"}` | `200` |
| `POST` | `/processes/{name}/resume` | Resume a suspended process: `{"name", "resumed": true, "state"}` | `200` |
| `GET` | `/processes/{name}/metrics` | Same body as `GET /processes/{name}` | `200` |

### Live configuration

| Method | Path | What it does | Takes effect | Success |
|---|---|---|---|---|
| `PATCH` | `/processes/{name}/instructions` | Replace the instructions: `{"instructions": "..."}` | Next run (`effective_from: "next_invocation"`) | `200` |
| `PATCH` | `/processes/{name}/budget` | Change `BudgetConfig` fields | Next budget check (`"next_budget_check"`) | `200` |
| `PATCH` | `/processes/{name}/health` | Change `HealthConfig` fields | Next check (`"immediate"`) | `200` |
| `PATCH` | `/processes/{name}/mission` | Change `MissionConfig` fields | Next evaluation and prompt (`"immediate"`) | `200` |
| `GET` | `/processes/{name}/triggers` | Config and agent-added triggers with stable IDs; `hmac_secret`, credential-like `custom_config` values and URL user info masked | -- | `200` |
| `POST` | `/processes/{name}/triggers` | Add a trigger (body: a `TriggerConfig`) | At once on an active process, else at the next start | `201` |
| `DELETE` | `/processes/{name}/triggers/{trigger_id}` | Stop and remove a trigger | At once on an active process, else at the next start | `200` |

### Messages

| Method | Path | What it does | Success |
|---|---|---|---|
| `POST` | `/processes/{name}/messages` | Queue a message: `{"message_id", "status": "queued", "expires_at"}` | `201` |
| `POST` | `/processes/{name}/ask` | Ask a question and wait for the answer (long poll; secret values in the answer are `[REDACTED]`) | `200` |
| `GET` | `/processes/{name}/inbox` | Pending messages and questions (redacted like the journal) | `200` |
| `DELETE` | `/processes/{name}/inbox` | Remove every message and cancel pending questions | `204` |

### State, secrets, mission, health

| Method | Path | What it does | Success |
|---|---|---|---|
| `GET` | `/processes/{name}/context` | `{"state", "writable_keys", "all_keys_writable"}` | `200` |
| `PATCH` | `/processes/{name}/context` | Write state keys, all or none: `{"state": {...}}` | `200` |
| `GET` | `/processes/{name}/secrets` | Secret metadata, never values | `200` |
| `PATCH` | `/processes/{name}/secrets/{secret_name}` | Rotate one secret: `{"value", "ttl"?}` | `200` |
| `DELETE` | `/processes/{name}/secrets` | Revoke every secret (zero-fill): `{"name", "secrets_revoked": true}` | `200` |
| `GET` | `/processes/{name}/mission` | Mission state and evaluation history | `200` |
| `POST` | `/processes/{name}/mission/fail` | Fail an `active` or `paused` mission: `{"reason"?}` | `200` |
| `POST` | `/processes/{name}/mission/pause` | Pause an `active` mission | `200` |
| `POST` | `/processes/{name}/mission/resume` | Resume a `paused` mission | `200` |
| `GET` | `/processes/{name}/health/anomalies` | Anomaly history: `type`, `description`, `timestamp` | `200` |
| `DELETE` | `/processes/{name}/health/anomalies` | Clear the history and cooldowns: `{"anomalies_cleared": n}` | `200` |

### Journal and rewind

| Method | Path | What it does | Success |
|---|---|---|---|
| `GET` | `/processes/{name}/journal` | Newest entries, oldest first. `?limit=` 1-500 (default 50), `?type=` filters by entry type | `200` |
| `GET` | `/processes/{name}/rewind/plan` | Preview a rewind: `?target_entry_id=` (required), `?mode=` (default `both`). Changes nothing | `200` |
| `POST` | `/processes/{name}/rewind` | Rewind to just before a journal entry: `{"target_entry_id", "mode"?, "actor"?}` | `200` |

Each journal entry is `{"entry_id", "entry_type", "timestamp", "data"}`; the `entry_id` is a rewind target. Without a journal the route answers `{"name", "entries": [], "journal_enabled": false}`, and the rewind routes answer `400 JOURNAL_DISABLED`. Rewind modes are `both`, `conversation_only`, `code_only`, `summarize` and `cancel`; `actor` (default `"api"`) is recorded in the journal. A running process is suspended for the rewind and resumed afterwards, and a run still in flight when the rewind times out gives `409 REWIND_BUSY`. See [Rewind Engine](journal/rewind.md) for what each mode restores.

---

## Deploying a process

```bash
curl -X POST http://localhost:9100/api/v1/processes \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{
    "name": "incident-handler",
    "config": {
        "model": "openai:gpt-5-mini",
        "instructions": "Handle infrastructure incidents.",
        "triggers": [{"type": "webhook", "webhook_path": "/incidents", "webhook_port": 9091}],
        "budget": {"enabled": true, "max_tool_calls_per_day": 500}
    },
    "start": true
  }'
```

- `name` must match `^[a-zA-Z][a-zA-Z0-9_-]{0,63}$` (`422 INVALID_NAME`); a name already registered gets `409 PROCESS_EXISTS`.
- `config` is a `ProcessConfig` as JSON (`422 INVALID_CONFIG` when it doesn't validate). A valid config that still can't be turned into a process gets `422 DEPLOY_FAILED`. The [security checks](#securing-the-control-api) answer `403` with `COMMANDS_NOT_ALLOWED`, `ENV_REFERENCES_NOT_ALLOWED` or `PATH_NOT_ALLOWED`.
- `start` (default `false`) must be a boolean. When the start fails the answer is `500 START_FAILED` and the process **stays deployed**: fix the cause and `POST .../start`, or `DELETE` it. A process whose start fails because of the process itself (a secret that can't be resolved, for example) is `500 START_FAILED` on `POST .../start` too, never `404`.
- The answer is `201` with `{"name", "process_id", "state", "created_at"}`.

The `config` object follows `ProcessConfig`:

```json
{
    "model": "openai:gpt-5-mini",
    "instructions": "Your agent instructions here.",
    "servers": {
        "tools": {"url": "http://tools:8080/mcp", "transport": "streamable-http"}
    },
    "triggers": [{"type": "cron", "cron_expression": "*/5 * * * *"}],
    "inbox": {"enabled": true, "max_messages": 50},
    "budget": {"enabled": true, "max_tool_calls_per_day": 500},
    "health": {"enabled": true, "stuck_threshold": 5},
    "mission": {
        "enabled": true,
        "objective": "Keep pipelines healthy",
        "success_criteria": "Zero critical alerts for 24h"
    }
}
```

---

## Changing a running process

### Instructions

`PATCH /instructions` with `{"instructions": "..."}` (a non-empty string) replaces the process's instructions, including any an open-mode agent set for itself. The next run rebuilds the agent with them before it runs, keeping the connected MCP servers, the conversation history and the context state; a run already in progress finishes with the old instructions. A process that is not running uses them when it starts. In Python: `process.update_instructions(text)`.

### Budget, health and mission

<a id="configuration-updates-hot-reload"></a>

The body of a `budget`, `health` or `mission` patch holds the fields to change, named as in [`BudgetConfig`](governance/budget.md), [`HealthConfig`](governance/health.md) and [`MissionConfig`](governance/mission.md).

```bash
curl -X PATCH http://localhost:9100/api/v1/processes/monitor/budget \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"max_cost_per_day": 500.0}'
```

```json
{"name": "monitor", "budget_updated": true, "effective_from": "next_budget_check", "live": true}
```

- The patched section is validated as a whole before anything changes, with the same limits as at deploy time and strict JSON types (`"5"` is not a number, `1` is not `true`). A field the model doesn't declare, including inside `escalation` or `tool_costs`, gets `422 UNKNOWN_FIELD`; an invalid value gets `422 INVALID_CONFIG`; an empty body gets `422 MISSING_FIELD`. In every case nothing changes. `null` clears an optional field such as `max_cost_per_day`.
- The process applies the change at once (`live: true`). Budget limits apply at the next budget check (the next tool call, LLM turn or run start) and the usage counted so far is kept. Health thresholds and window sizes apply at the next check, keeping the recorded history. A mission change applies to the next evaluation and the next `[Mission]` prompt block, keeping the mission's progress.
- `enabled` works live too: `{"enabled": true}` builds the subsystem on a process that was deployed without it, and `{"enabled": false}` removes it.

The response always says when the change applies. `live` is `false` with `effective_from: "redeploy"`, `pending_fields` and a `note` only for a process object that can't apply governance changes itself (a custom process class without `apply_governance_config()`).

### Triggers

Config triggers have **stable IDs**: `<type>-<10-character digest of the trigger's config>`, for example `cron-6d904fd75c`. An ID doesn't depend on the trigger's position, so removing one trigger leaves the other IDs unchanged, and the IDs are the same after a restart. Identical configs get `-2`, `-3` suffixes. The ID is also the `trigger_id` of the events the trigger produces.

```bash
# List (config triggers include their config; agent-added ones have source "dynamic")
curl http://localhost:9100/api/v1/processes/monitor/triggers -H "Authorization: Bearer $TOKEN"

# Add
curl -X POST http://localhost:9100/api/v1/processes/monitor/triggers \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"type": "cron", "cron_expression": "0 9 * * 1-5"}'
# {"name": "monitor", "trigger_added": true, "trigger_id": "cron-fb9fa7b826",
#  "trigger_type": "cron", "live": true, "effective_from": "immediate"}

# Remove by ID
curl -X DELETE http://localhost:9100/api/v1/processes/monitor/triggers/cron-6d904fd75c \
  -H "Authorization: Bearer $TOKEN"
# {"name": "monitor", "trigger_removed": "cron-6d904fd75c", "live": true, "effective_from": "immediate"}
```

| Operation | Process starting, running, suspended or awaiting | Process created, stopped or failed |
|---|---|---|
| Add | Created and started at once: `live: true`, `effective_from: "immediate"` | Added to the config: `live: false`, `effective_from: "next_start"` |
| Remove a config trigger | Stopped at once and removed from the config: `live: true`, `"immediate"` | Removed from the config: `live: true`, `"next_start"` |
| Remove an agent-added trigger | Stopped at once: `live: true`, `"immediate"` | -- |

Adding a trigger identical to one already configured gets `409 TRIGGER_EXISTS`; an invalid config or one that can't start gets `422 INVALID_CONFIG`; a `${VAR}` reference gets `403 ENV_REFERENCES_NOT_ALLOWED` and a `watch_path` outside `data_dir` `403 PATH_NOT_ALLOWED`; an unknown ID gets `404 TRIGGER_NOT_FOUND`. In the listing, `hmac_secret` is masked, and so are the values of `custom_config` keys that look like credentials (names containing `secret`, `token`, `password`, `api_key`, `auth`, `credential`, `signature` or `cookie`) and the user info of any URL value (`postgres://app:pw@db/x` is listed as `postgres://**********@db/x`). The old position-based IDs (`static_0`, `static_1`, ...) are still accepted on `DELETE`; the response then carries a `warning` naming the stable ID to use instead.

### Context state

`PATCH /context` with `{"state": {key: value}}` writes all keys or none: if any key is not writable the answer is `403 WRITE_DENIED` naming them, and nothing is written. Writes are recorded with source `"api"` and journaled. Other top-level fields get `422 UNKNOWN_FIELD`. See [Agent Context](context.md).

### Secrets

`GET /secrets` lists metadata, `PATCH /secrets/{secret_name}` rotates one value (`404 SECRET_NOT_FOUND` for a name the process doesn't have), `DELETE /secrets` revokes all. Values never appear in a response. See [Secret Scoping](governance/secrets.md#over-the-api).

### Mission

`GET /mission` returns the state, objective, invocation count and evaluations; `fail`, `pause` and `resume` answer `409 MISSION_STATE` when the mission is not in a state that allows them. See [Mission Model](governance/mission.md#over-the-api).

---

## Messages and questions

A process with an inbox takes messages from people while it runs. The agent sees them in its prompt on its next run. Enable it in the process config:

```json
{"inbox": {"enabled": true, "max_messages": 50, "default_ttl": 3600}}
```

| `InboxConfig` field | Default | Description |
|---|---|---|
| `enabled` | `false` | Enable the inbox |
| `max_messages` | `50` | Most messages held at once (never exceeded) |
| `max_message_length` | `2000` | Longer content is truncated to this many characters |
| `default_ttl` | `3600` | Seconds a message lives when it has no `ttl` (`0` = no expiry) |
| `max_ttl` | `86400` | Cap on any message's lifetime (`0` = no cap). `default_ttl` may not exceed it |
| `rate_limit_per_sender` | `20` | Messages per `sender_id` per hour (messages without a `sender_id` are not limited) |
| `scan_with_guardrails` | `true` | Scan messages for prompt injection when they arrive (see [Guardrail scanning](#guardrail-scanning)) |

Without an inbox, `/messages` and `/ask` answer `400 INBOX_DISABLED` and `GET /inbox` answers `{"enabled": false, ...}`. A stopped or failed process gets `409 PROCESS_NOT_RUNNING`.

### Message types and priorities

| Type | Use case | Example |
|---|---|---|
| `directive` | Tell the agent to do or stop doing something | "Do not restart any services today" |
| `context` | Information the agent should know (the default for `/messages`) | "Traffic spike is from a marketing campaign" |
| `question` | Ask the agent something; it answers in its reply | "What's the current health status?" |
| `correction` | Tell the agent it was wrong | "That alert was a false positive" |

Priorities are `low`, `normal` (default), `high` and `critical`. The agent sees messages ordered by priority, then oldest first.

### Sending a message

```bash
curl -X POST http://localhost:9100/api/v1/processes/monitor/messages \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"content": "Ignore staging alerts for 1 hour", "message_type": "directive",
       "priority": "high", "sender_id": "alice", "ttl": 3600}'
# 201 {"message_id": "msg_...", "status": "queued", "expires_at": 1792445004.98}
```

Fields: `content` (required, non-empty string), `message_type`, `priority`, `sender_id` (string), `ttl` (seconds, `>= 0`; `0` or missing uses `default_ttl`, and `max_ttl` caps it) and `metadata` (object, stored with the message). An unknown `message_type` gets `422 INVALID_TYPE`; another bad field gets `422 INVALID_FIELD`.

### Asking a question

```bash
curl -X POST http://localhost:9100/api/v1/processes/monitor/ask \
  -H "Authorization: Bearer $TOKEN" \
  -d '{"content": "How many alerts did you skip?", "timeout": 120}'
```

The request waits until the agent answers or `timeout` seconds pass (default `120`, at most `3600`). It always answers `200`:

```json
{"question_id": "msg_...", "status": "answered", "response": "Three staging alerts skipped.",
 "answered_at": 1792441406.1, "invocation_id": "2"}
```

or, when no answer came in time, `{"question_id", "status": "timeout", "response": null}`. The question stays in the inbox after a timeout (it lives for `timeout` + 60 seconds, capped by `max_ttl`), so a later run can still answer it, but nobody is waiting for that answer. The agent sees the question on its **next run**: if no trigger fires within `timeout`, it can't answer. `priority`, `sender_id` and `metadata` work as for `/messages`.

### What the agent sees

Before each run, the pending messages are added to the prompt as one system message, after the context state, budget and mission blocks and before the conversation history:

```
=== OPERATOR MESSAGES ===

[HIGH DIRECTIVE] (from: alice, just now, expires in 59 min)
Ignore staging alerts for 1 hour

[QUESTION] (just now, expires in 1 min)
How many alerts did you skip?
→ Please include your answer prefixed with "ANSWER Q1:"

=== END OPERATOR MESSAGES ===
```

Each message is a `[<PRIORITY> <TYPE>]` label (the priority is left out for `normal`) with the sender when known, its age (`just now`, `<n> min ago`, `<n>h ago`) and the time left before it expires, followed by the content. Questions are numbered `Q1`, `Q2`, ... in the order they appear in the block.

### Answers and consumption

After every run, the runtime:

1. Reads `ANSWER Q<n>: <text>` parts from the agent's final reply and delivers each one to the question it numbers (the waiting `/ask` call returns it). Text after the marker up to the next `ANSWER Q` marker is the answer. Secret values in it are replaced by `[REDACTED]`, as in the journal, so a question can't make the agent hand out a secret.
2. Removes the messages that run saw: directives, context and corrections are gone after one run, and answered questions are removed. Unanswered questions stay until they are answered or expire.

So a directive is shown once. Send it again, or store the fact in the [context state](context.md), when the agent must keep following it on later runs.

### Limits and refusals

| Situation | API answer | Python (`process.send_message`) |
|---|---|---|
| Empty content, unknown priority or type | `422 INVALID_FIELD` / `422 INVALID_TYPE` | `InvalidInboxMessage` |
| Sender over `rate_limit_per_sender` this hour | `429 RATE_LIMITED` | `InboxRateLimitError` |
| Inbox full and nothing can be evicted | `409 INBOX_FULL` | `InboxFullError` |
| Flagged by the guardrail scan | `422 MESSAGE_REJECTED` | `InboxMessageRejected` (with `.reason`) |

All four exception classes are in `promptise.runtime.inbox` and subclass `InboxError`, a `ValueError`. Refused messages are not counted against the sender's rate limit.

When the inbox holds `max_messages` messages, a new message first replaces an answered question if there is one; otherwise it evicts the **oldest message of the lowest priority** present. Critical messages are never evicted, and a message never evicts one of higher priority: if every message is critical, or the new message has a lower priority than every evictable one, the new message is refused with `INBOX_FULL`. The inbox never holds more than `max_messages`. An `/ask` call waiting on an evicted question ends with `410 QUESTION_REMOVED`.

### Guardrail scanning

With `scan_with_guardrails: true` (the default), each message is scanned when it arrives, in overlapping 500-character windows, and a flagged message is refused (`422 MESSAGE_REJECTED`). The scanner is, in order:

1. `ProcessConfig.guardrails`, when it can scan text (a `PromptiseSecurityScanner` or any object with `scan_text()` or `check_input()`).
2. The trigger payload scanner, when `trigger_delivery.scan_payloads` is on. It is loaded when the process starts; messages that arrived before (a created process already takes messages) are scanned then, and flagged ones are removed (an `/ask` waiting on one gets `422 MESSAGE_REJECTED`).

With neither, messages are **not scanned**: the process logs a one-time warning and accepts them. Set `guardrails` (for example `PromptiseSecurityScanner(detectors=[InjectionDetector()])`) or `trigger_delivery.scan_payloads=True` to scan them, or `scan_with_guardrails: false` to accept messages unscanned without the warning. Only give inbox access to people you trust with the agent's instructions: a message lands in the agent's prompt.

### From Python

```python
msg_id = await process.send_message(
    "Ignore staging alerts for 1 hour",
    message_type="directive", priority="high", sender_id="alice", ttl=3600,
)
response = await process.ask("How many alerts did you skip?", timeout=60)
print(response.content)  # an InboxResponse: question_id, content, answered_at, invocation_id
```

`send_message` returns the message ID. `ask` returns an `InboxResponse` and raises `asyncio.TimeoutError` when no answer arrives in time. Both raise `RuntimeError` when the process has no inbox or is stopped or failed, and `ValueError` (an `InboxError`) for a refused message.

---

## Python client

`OrchestrationClient` has a typed method for every route. It uses `httpx` and is an async context manager.

```python
from promptise.runtime import OrchestrationAPIError, OrchestrationClient

async with OrchestrationClient("http://localhost:9100", token="my-token") as client:
    await client.deploy("monitor", {"model": "openai:gpt-5-mini", "instructions": "Monitor pipelines."}, start=True)

    answer = await client.ask("monitor", "What anomalies have you found?", timeout=60)
    print(answer)  # the answer text, or None if the agent didn't answer in time

    result = await client.update_budget("monitor", max_cost_per_day=500.0)
    print(result["live"], result["effective_from"])  # True next_budget_check

    try:
        await client.rotate_secret("monitor", "api_key", "sk-new-key", ttl=3600)
    except OrchestrationAPIError as exc:
        print(exc.status_code, exc.code, exc.error_message)  # 404 SECRET_NOT_FOUND ...
```

| Argument | Default | Description |
|---|---|---|
| `base_url` | required | For example `"http://localhost:9100"` |
| `token` | `None` | The server's `auth_token`, sent as `Authorization: Bearer` |
| `auth_token` | `None` | Alias of `token` (passing both with different values raises `ValueError`) |
| `timeout` | `30.0` | Default request timeout in seconds |

Use it with `async with`, or call `await client.aclose()` when done.

**Errors.** A `4xx` or `5xx` answer raises `OrchestrationAPIError`, a subclass of `httpx.HTTPStatusError`, with `status_code`, `code` (the API error code, or `None` when the body was not an API error) and `error_message`. Network failures raise the usual `httpx` exceptions.

**`204` answers.** `remove_process()` and `clear_inbox()` return `{}`.

| Route | Method | Returns |
|---|---|---|
| `GET /health` | `health()` | `dict` |
| `GET /runtime/status` | `runtime_status()` | `dict` |
| `POST /runtime/start-all` | `start_all()` | `dict` |
| `POST /runtime/stop-all` | `stop_all()` | `dict` |
| `POST /runtime/shutdown` | `shutdown()` | `{"status": "shutting_down"}`; `404` error without `on_shutdown` |
| `GET /processes` | `list_processes()` | `list[dict]` |
| `POST /processes` | `deploy(name, config, *, start=False)` | `dict` |
| `GET /processes/{name}` | `get_process(name)` | `dict` |
| `DELETE /processes/{name}` | `remove_process(name)` | `{}` |
| `POST .../start` | `start_process(name)` | `dict` |
| `POST .../stop` | `stop_process(name)` | `dict` |
| `POST .../restart` | `restart_process(name)` | `dict` |
| `POST .../suspend` | `suspend_process(name)` | `dict` |
| `POST .../resume` | `resume_process(name)` | `dict` |
| `PATCH .../instructions` | `update_instructions(name, instructions)` | `dict` |
| `PATCH .../budget` | `update_budget(name, **fields)` | `dict` |
| `PATCH .../health` | `update_health(name, **fields)` | `dict` |
| `PATCH .../mission` | `update_mission(name, **fields)` | `dict` |
| `GET .../triggers` | `list_triggers(name)` | `list[dict]` |
| `POST .../triggers` | `add_trigger(name, trigger_config)` | `dict` |
| `DELETE .../triggers/{id}` | `remove_trigger(name, trigger_id)` | `dict` |
| `GET .../secrets` | `list_secrets(name)` | `list[dict]` |
| `PATCH .../secrets/{secret}` | `rotate_secret(name, secret_name, value, *, ttl=None)` | `dict` |
| `DELETE .../secrets` | `revoke_secrets(name)` | `dict` |
| `GET .../journal` | `get_journal(name, *, limit=50, entry_type=None)` | `list[dict]` |
| `GET .../rewind/plan` | `rewind_plan(name, target_entry_id, *, mode="both")` | `dict` (the plan) |
| `POST .../rewind` | `rewind(name, target_entry_id, *, mode="both", actor=None)` | `dict` |
| `GET .../mission` | `get_mission(name)` | `dict` |
| `POST .../mission/fail` | `fail_mission(name, reason="Manually failed via client")` | `dict` |
| `POST .../mission/pause` | `pause_mission(name)` | `dict` |
| `POST .../mission/resume` | `resume_mission(name)` | `dict` |
| `GET .../health/anomalies` | `get_anomalies(name)` | `list[dict]` |
| `DELETE .../health/anomalies` | `clear_anomalies(name)` | `dict` |
| `POST .../messages` | `send_message(name, content, *, message_type="context", priority="normal", sender_id=None, ttl=None, metadata=None)` | `dict` |
| `POST .../ask` | `ask(name, question, *, timeout=120, sender_id=None, priority="normal")` | `str \| None` |
| `GET .../inbox` | `get_inbox(name)` | `dict` |
| `DELETE .../inbox` | `clear_inbox(name)` | `{}` |
| `GET .../context` | `get_context(name)` | `dict` |
| `PATCH .../context` | `update_context(name, state)` | `dict` |
| `GET .../metrics` | `get_metrics(name)` | `dict` |

`ask()` returns the answer text, or `None` when the server answered `status: "timeout"`; its HTTP timeout is `timeout` + 10 seconds, so the server's long poll ends first.

---

## Audit log

Every mutating request (`POST`, `PATCH`, `PUT`, `DELETE`) that passes the token and Host/Origin checks, successful or not, is logged at `INFO` to the `promptise.runtime.api` logger with its method, route, path parameters (process, trigger and secret **names**), status and client address. Requests those checks refuse (`401`, `403 CROSS_ORIGIN_REFUSED`, `421`) are logged as warnings, as are refused command deploys.

A successful mutating request is also written to the process's journal (when it has one) as an `api_request` entry:

```json
{"method": "DELETE", "route": "/api/v1/processes/{name}/triggers/{trigger_id}",
 "params": {"name": "monitor", "trigger_id": "cron-6d904fd75c"}, "status": 200, "client": "127.0.0.1"}
```

Request bodies are never logged: they can hold secret values and message text.

---

## Error responses

Errors are JSON objects:

```json
{
    "error": {
        "code": "PROCESS_NOT_FOUND",
        "message": "Process 'unknown-agent' not found"
    }
}
```

The codes come from `promptise.runtime.api.ERROR_CODES` (`code -> (status, meaning)`):

| Status | Code | Meaning |
|---|---|---|
| `400` | `HEALTH_DISABLED` | Health operation on a process without health monitoring |
| `400` | `INBOX_DISABLED` | Inbox operation on a process without an inbox |
| `400` | `INVALID_JSON` | Request body is not a JSON object |
| `400` | `JOURNAL_DISABLED` | Rewind on a process without a journal |
| `400` | `MISSION_DISABLED` | Mission operation on a process without a mission |
| `400` | `RESUME_FAILED` | The process can't be resumed in its current state |
| `400` | `ROTATION_FAILED` | The secret could not be rotated |
| `400` | `SECRETS_DISABLED` | Secret operation on a process without secret scoping |
| `400` | `SUSPEND_FAILED` | The process can't be suspended in its current state |
| `401` | `UNAUTHORIZED` | Missing or wrong bearer token |
| `403` | `COMMANDS_NOT_ALLOWED` | Deploy with a stdio MCP server, a command hook or open-mode tool creation while allow_command_deploys is off |
| `403` | `CROSS_ORIGIN_REFUSED` | Cross-site Origin on a loopback bind |
| `403` | `ENV_REFERENCES_NOT_ALLOWED` | Config with a `${VAR}` reference the API doesn't allow (allow_env_references / allowed_env_vars) |
| `403` | `PATH_NOT_ALLOWED` | Journal, memory or watch path outside the API's data_dir (or with '..') |
| `403` | `WRITE_DENIED` | Context key(s) not writable (nothing was written) |
| `404` | `ENTRY_NOT_FOUND` | Journal entry ID doesn't exist (rewind target) |
| `404` | `NOT_FOUND` | No such route |
| `404` | `PROCESS_NOT_FOUND` | Process name doesn't exist |
| `404` | `SECRET_NOT_FOUND` | The process has no secret of that name |
| `404` | `TRIGGER_NOT_FOUND` | Trigger ID doesn't exist |
| `405` | `METHOD_NOT_ALLOWED` | The route doesn't accept this method |
| `409` | `INBOX_FULL` | The inbox is full and nothing can be evicted for the message |
| `409` | `MISSION_STATE` | Mission fail/pause/resume not valid in the mission's state |
| `409` | `PROCESS_EXISTS` | Deploy with a name that's already registered |
| `409` | `PROCESS_NOT_RUNNING` | Message or question to a stopped or failed process |
| `409` | `REWIND_BUSY` | A run was still in flight when the rewind timed out |
| `409` | `TRIGGER_EXISTS` | Adding a trigger identical to an existing one |
| `410` | `QUESTION_REMOVED` | `/ask`: the question was cleared or evicted from the inbox before it was answered |
| `413` | `PAYLOAD_TOO_LARGE` | Request body over the server's size limit (1 MiB) |
| `421` | `MISDIRECTED_REQUEST` | Non-loopback Host on a loopback bind (DNS rebinding) |
| `422` | `DEPLOY_FAILED` | The process could not be created from a valid config |
| `422` | `INVALID_CONFIG` | Config fails validation (deploy, trigger, PATCH sections) |
| `422` | `INVALID_FIELD` | A field has the wrong type or an out-of-range value |
| `422` | `INVALID_NAME` | Process name doesn't match `^[a-zA-Z][a-zA-Z0-9_-]{0,63}$` |
| `422` | `INVALID_TYPE` | Unknown message_type |
| `422` | `MESSAGE_REJECTED` | The inbox's guardrail scan flagged the message |
| `422` | `MISSING_FIELD` | Required field missing from the request |
| `422` | `UNKNOWN_FIELD` | PATCH names a field the config model doesn't declare |
| `429` | `RATE_LIMITED` | Inbox rate limit exceeded |
| varies | `HTTP_ERROR` | Another HTTP error from the server (`400`, `408`, `411`, `414`, `431`, ...); the response status says which |
| `500` | `INTERNAL_ERROR` | Unexpected server error (details in the server log) |
| `500` | `RESTART_FAILED` | The process failed to restart |
| `500` | `START_FAILED` | The process failed to start |
| `500` | `STOP_FAILED` | The process failed to stop |

`HTTP_ERROR` has no fixed status (its `ERROR_CODES` status is `None`): it carries the status of the underlying HTTP error. An `INTERNAL_ERROR` message is generic; the details go to the server log.

When an error is caused by an exception inside the runtime (a failed deploy, start, stop, restart, suspend, resume or secret rotation, an inbox refusal, a rewind that failed, an unexpected error), the response never carries the exception's text, which can hold file paths, internal state or secret values. It carries a fixed `message` and an `error_id`, and the exception with its traceback is logged on `promptise.runtime.api` under the same id (`ERROR` for a `5xx`, `WARNING` otherwise). `OrchestrationAPIError.error_id` holds it on the client side:

```json
{
    "error": {
        "code": "START_FAILED",
        "message": "Process 'worker' failed to start",
        "error_id": "3f9c0a1b2d4e"
    }
}
```

Validation errors (`INVALID_CONFIG` from a config pydantic rejects) still list each offending field and what was expected of it.

---

## Security

- Bearer token required on non-loopback binds, compared in constant time; `GET /api/v1/health` is the only route without auth (for Kubernetes probes).
- On loopback binds, DNS-rebinding (`421`) and cross-site (`403`) requests are refused.
- Deploys that would run programs or agent-written code on the host are refused unless `allow_command_deploys=True`.
- `${VAR}` references in deployed configs are refused unless allowed, and an API-deployed process resolves only the allowed variables; a failed resolution doesn't reveal whether a variable exists.
- Journal, memory and watch paths in deployed configs stay inside `data_dir`.
- An API-deployed open-mode agent connects only the MCP servers in `allowed_mcp_urls` (none when it is empty), and agent-connected servers never receive the process identity's credential (see [Open Mode](meta-tools.md#security)).
- Process names are validated, and configs are validated before a process is created or changed; invalid changes change nothing.
- Secret values never appear in responses (including `/ask` answers), error messages, the audit log or the journal.
- Every mutating request is audited.

---

## Common pitfalls

- **An unanswered `/ask` is not an error.** It answers `200` with `status: "timeout"`, and `client.ask()` returns `None`. The agent only sees the question on its next run, so a process whose triggers don't fire within `timeout` can't answer. Inject an event or pick a trigger that runs often enough.
- **Directives are shown once.** After the run that saw it, a directive is removed from the inbox. Put lasting rules in the instructions (`PATCH /instructions`) or the context state.
- **Messages are unscanned without guardrails.** `scan_with_guardrails` defaults to `true`, but it needs a scanner; without `guardrails` or `trigger_delivery.scan_payloads` the process only logs a warning.
- **`DELETE /inbox` ends waiting questions.** It removes pending questions; an `/ask` call still waiting on one answers `410 QUESTION_REMOVED` (`process.ask()` raises `InboxQuestionRemoved`). Wait for open questions before clearing the inbox.
- **`START_FAILED` on deploy leaves the process registered.** A retry with the same name gets `409 PROCESS_EXISTS`; start it or delete it instead.
- **Secret rotations don't survive a restart** (with the default `revoke_on_stop=True`). A restart revokes the secrets and resolves them from the config again; rotate the source value as well (see [Secret Scoping](governance/secrets.md)).
- **An open-mode deploy gets `403 COMMANDS_NOT_ALLOWED`.** Open mode allows `create_tool` by default, which runs agent-written code on the host. Send `"open_mode": {"allow_tool_creation": false}`, or start the API with `allow_command_deploys=True` if every token holder may run code there.
- **A `${VAR}` in a deploy gets `403 ENV_REFERENCES_NOT_ALLOWED`.** Send the value, or ask the operator to allow that variable (`--allow-env-var NAME`).
- **Use stable trigger IDs.** Take the `id` from `GET /triggers`; position-based `static_<n>` IDs are deprecated.

---

## What's next?

- [Agent Processes](processes.md) -- process lifecycle and configuration
- [Agent Context](context.md) -- the state behind `GET/PATCH /context`
- [Mission Model](governance/mission.md), [Autonomy Budget](governance/budget.md), [Behavioral Health](governance/health.md) and [Secret Scoping](governance/secrets.md) -- the sections you can patch
- [Rewind Engine](journal/rewind.md) -- what the rewind routes restore
- [Triggers](triggers/index.md) -- cron, webhook, file watch, event and message triggers
- [CLI Commands](cli.md) -- `promptise runtime start` serves this API

!!! tip "Learn the concept"

    - [AI Agent Control API: Manage Running Agents Over HTTP](https://www.promptise.com/guides/view/ai-agent-control-api) -- Every route group, auth and the loopback guard, with real calls.
    - [Message a Running AI Agent: Inbox, Directives and Ask](https://www.promptise.com/guides/view/message-running-ai-agent) -- Send directives and questions to an agent while it runs.
    - [Autonomous AI Agent Runtime: Budgets, Crash Recovery, API](https://www.promptise.com/guides/view/autonomous-ai-agent-runtime) -- How the API fits with budgets and recovery in a long-running runtime.
