# Secret Scoping

Secret scoping gives each agent process its own credentials, with expiry, rotation without a restart, an access log and revocation. Environment variables are shared by every agent on a host; a secret scope holds only the secrets one process needs, keeps them encrypted in memory, and makes sure their values never reach the journal, checkpoints, the conversation history or API responses in plain text.

```python
import os

from promptise.runtime import AgentProcess, ProcessConfig, SecretScopeConfig
from promptise.runtime.config import JournalConfig

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Process payments.",
    secrets=SecretScopeConfig(
        enabled=True,
        secrets={
            "stripe_key": "${STRIPE_API_KEY}",                 # resolved from the environment at start
            "db_url": "postgres://app:${DB_PASSWORD}@db/app",  # references inside text work too
        },
        default_ttl=3600,           # every secret expires after 1 hour...
        ttls={"stripe_key": 1800},  # ...this one after 30 minutes
    ),
    journal=JournalConfig(path=".promptise/journal"),  # access log
)

process = AgentProcess("payments", config)
await process.start()  # resolves the references; a missing variable fails the start
```

Operators rotate and revoke a running process's secrets [over the API](#over-the-api). The scope itself is a `SecretScope`, which you can also use on its own:

```python
from promptise.runtime.secrets import SecretScope

scope = SecretScope(config.secrets, process_id="payments")
await scope.resolve_initial()
key = scope.get("stripe_key")                              # None once expired
await scope.rotate("stripe_key", "sk_live_new", ttl=1800)  # new value, new 30-minute lifetime
print(scope.list_metadata())                               # names, sources, expiry, counts: never values
print(scope.sanitize_text(f"charged with {key}"))          # "charged with [REDACTED]"
```

The same in a `.agent` manifest:

```yaml
name: payments
model: openai:gpt-5-mini
instructions: Process payments.
secrets:
  enabled: true
  secrets:
    stripe_key: "${STRIPE_API_KEY}"
    db_url: "postgres://app:${DB_PASSWORD}@db/app"
  default_ttl: 3600
  ttls:
    stripe_key: 1800
  revoke_on_stop: true
journal:
  level: checkpoint
```

---

## Where values come from

Each value in `secrets` is either a literal or contains environment references:

| Form | Example | Resolved to |
|---|---|---|
| Reference | `"${STRIPE_API_KEY}"` | The variable's value |
| Reference with default | `"${DB_PASSWORD:-devpw}"` | The variable, or `devpw` when it is unset |
| Text with references | `"postgres://app:${DB_PASSWORD}@db/app"` | The text with each reference replaced |
| Literal | `"tok-abc123"` | The text as written |

- References are resolved when the process **starts** (`SecretScope.resolve_initial()`), not when the config is built, so a `ProcessConfig` or a loaded manifest holds the reference, never the value. Loading a manifest checks that the referenced variables exist and fails early when one is missing.
- A variable that is unset (and has no default) when the process starts raises `SecretResolutionError` (a `KeyError`) naming the variable and the secret; the process ends up `failed`.
- A process deployed through the [control API](../api.md#environment-references) may only reference the variables the operator allowed (`OrchestrationAPI(allowed_env_vars=...)` or `allow_env_references=True`): the API refuses other references with `403 ENV_REFERENCES_NOT_ALLOWED`, and the process's scope won't resolve them either. Its resolution errors are generic (they don't say whether a variable exists), so a token holder can't probe the host's environment.
- Values are stored as `pydantic.SecretStr`: `repr()` shows `SecretStr('**********')`, and a JSON dump (`model_dump(mode="json")`) shows `**********` for literal values and references as written. Passing the masked placeholder `**********` back as a value is refused, so a dumped config with literal values can't be loaded again by mistake.
- Every name in `ttls` must be a key of `secrets`, and every TTL must be positive.

For production, deliver secrets as environment variables (from your secret manager, Kubernetes or Docker secrets) and reference them; avoid literal values in configs and manifests.

---

## Lifecycle

1. **Resolve.** Every `start()` resolves all secrets from the config again and starts their TTLs. A value set by a rotation is replaced by the configured value on the next start.
2. **Access.** `scope.get(name)` returns the value, or `None` when the secret doesn't exist or has expired. Every successful access is logged (name only).
3. **Expiry.** A secret with a TTL expires that many seconds after it was resolved or rotated (monotonic clock). An expired secret is removed on the next access or listing, and its value stays redacted.
4. **Rotation.** `await scope.rotate(name, new_value, ttl=None)` replaces a value at once, without a restart. `ttl` sets a new lifetime from now; without it the secret keeps its current expiry. An expired secret can be rotated: it becomes active again, with `ttl` or else its configured TTL. A name that isn't configured raises `SecretNotFoundError` (a `KeyError`); an empty value or a TTL `<= 0` raises `ValueError`.
5. **Revocation.** `await scope.revoke_all()` overwrites every value with null bytes, removes all secrets, forgets rotated values and replaces the encryption key. It runs on `stop()` when `revoke_on_stop=True` (the default), always on `close()` (and so on `AgentRuntime.remove_process()`), and on `DELETE /api/v1/processes/{name}/secrets`. With `revoke_on_stop=False`, a stopped process keeps its secrets until it is closed; the next `start()` resolves them again either way.

Values are kept Fernet-encrypted in memory with a key per scope. Python strings can't be wiped, so zero-filling is a best effort: copies may stay in the interpreter's memory until they are garbage-collected.

---

## Access log

With a journal, the scope writes a `secret_access` entry for every access, rotation, expiry and revocation:

```json
{"entry_type": "secret_access", "data": {"action": "access", "secret_name": "stripe_key"}}
```

`action` is `"access"`, `"rotation"`, `"expired"` or `"revoke_all"` (with `secret_name` `"*"`). The value is never logged. Without a journal (`JournalConfig(level="none")`, the `ProcessConfig` default) there is no access log.

---

## Redaction

Secret values are replaced by `[REDACTED]` wherever the runtime stores or shows text:

| Where | What is redacted |
|---|---|
| Journal entries and checkpoints | Every string in the entry data and the checkpoint state, including tool arguments and results (`level="full"`), invocation results, context updates and the conversation snapshot. The result of the `get_secret` tool is always journaled as `[REDACTED]` |
| Conversation history | The stored trigger message and the agent's reply, so an echoed secret never reaches a later prompt |
| Lifecycle hooks | The data passed to hooks, including the prompt a `user_prompt_submit` hook sees |
| Inbox answers | The answer to a question (`ANSWER Qn:` in the reply), so `POST /ask` and `process.ask()` return `[REDACTED]` instead of a value the agent was talked into repeating; `GET /inbox` too |
| Control API | Responses never contain values; a failed rotation answers `ROTATION_FAILED` without echoing the error |

Redaction covers the current values and values that were rotated away or expired (the last 10 per secret), longest first. It is an exact text match: a value that was encoded, split or reformatted is not recognized.

Two things are not redacted: the live context state (the agent's `[Context State]` prompt block and `GET /context` show values as stored; a value recovered from the journal comes back as `[REDACTED]`), and what the model itself receives, such as the `get_secret` result. Keep secrets out of the context state.

`scope.sanitize_text(text)` and `scope.sanitize(value)` (nested dicts, lists and tuples; returns a copy) do the same replacement for your own logs and output.

---

## Open mode: `get_secret`

When secret scoping is enabled and the process runs in [open mode](../meta-tools.md), the agent gets a `get_secret(name)` meta-tool. It returns the value (or an error text when the secret is missing or expired), so the agent can pass it to the tool that needs it; the tool's description tells the agent never to repeat it. Each call is access-logged. The agent never sees secrets in its system prompt.

Prefer giving credentials to the tool instead of the model: put tokens in the MCP server spec (`bearer_token`, `api_key`, or `env` of a stdio server), where the model never sees them.

---

## Over the API

| Method | Path | What it does |
|---|---|---|
| `GET` | `/api/v1/processes/{name}/secrets` | `{"name", "secrets": [...], "enabled": true}`; `{"secrets": [], "enabled": false}` without secret scoping |
| `PATCH` | `/api/v1/processes/{name}/secrets/{secret_name}` | Rotate: body `{"value": "...", "ttl": <seconds, optional>}`. Answers `{"name", "secret_rotated", "secret": <metadata>}` |
| `DELETE` | `/api/v1/processes/{name}/secrets` | Revoke all: `{"name", "secrets_revoked": true}` |

Each item of the listing (and `secret` in the rotation answer) is the secret's metadata plus `expires_at`:

```json
{"name": "stripe_key", "active": true, "source": "rotation", "ttl": 60,
 "expires_in": 59.999, "rotation_count": 1, "rotated_at": "2026-10-19T20:23:26.797891+00:00",
 "access_count": 0, "expires_at": 1792441466.79}
```

| Error | When |
|---|---|
| `400 SECRETS_DISABLED` | Rotate or revoke on a process without secret scoping |
| `404 SECRET_NOT_FOUND` | Rotating a name the process has no secret for |
| `422 MISSING_FIELD` / `INVALID_FIELD` | `value` missing or not a string; `ttl` not a number `> 0` |
| `422 UNKNOWN_FIELD` | A body field other than `value` and `ttl` |
| `400 ROTATION_FAILED` | The rotation failed for another reason (the message never contains the value) |

The Python client has `list_secrets(name)`, `rotate_secret(name, secret_name, value, *, ttl=None)` and `revoke_secrets(name)`. `GET /api/v1/processes/{name}` shows `active_secrets`, the number of active secrets.

---

## API reference

### `SecretScopeConfig`

| Field | Type | Default | Description |
|---|---|---|---|
| `enabled` | `bool` | `False` | Enable secret scoping |
| `secrets` | `dict[str, SecretStr]` | `{}` | Secret name to literal value or `${VAR}` reference |
| `default_ttl` | `float \| None` | `None` | TTL in seconds for every secret (`None` = no expiry; must be `> 0`) |
| `ttls` | `dict[str, float]` | `{}` | Per-secret TTL overrides (names must be in `secrets`) |
| `revoke_on_stop` | `bool` | `True` | Revoke all secrets on `stop()` |

Unknown keys are ignored with a `FutureWarning`.

### `SecretScope`

`SecretScope(config, process_id, journal=None, *, source="operator", env_references=True)` from `promptise.runtime.secrets`. `env_references` limits which variables `${VAR}` may read (`True` any, `False` none, or a set of names); `source="api"` makes resolution errors generic. A process with `secrets.enabled` creates its own (it is not a public attribute of the process); reach a running process's secrets over the API.

| Member | Description |
|---|---|
| `await .resolve_initial(*, missing_only=False)` | Resolve every secret from the config and start its TTL (again on each call). `missing_only=True` resolves only the secrets the scope doesn't hold, keeping rotated values (what a process start uses). Raises `SecretResolutionError` |
| `.get(name)` | The value, or `None` when missing or expired (access-logged) |
| `await .rotate(name, new_value, *, ttl=None)` | Replace a value now; `SecretNotFoundError` for an unknown name |
| `await .revoke_all()` | Zero-fill and remove all secrets, forget rotated values, new encryption key |
| `.list_metadata()` | One dict per configured secret: `name`, `active`, `source` (`"env"`, `"literal"` or `"rotation"`), `ttl`, `expires_in` (seconds left, `None` = no expiry or not active), `rotation_count`, `rotated_at` (ISO UTC or `None`), `access_count`. Never a value |
| `.list_names()` | Names of the active secrets |
| `.active_secret_names` / `.active_secrets` | Names of the non-expired secrets (`active_secrets` is an alias) |
| `.active_secret_count` | Number of non-expired secrets |
| `.sanitize_text(text)` | `text` with known values replaced by `[REDACTED]` |
| `.sanitize(value)` | The same for every string in a nested value (a copy) |

`SecretNotFoundError` subclasses `KeyError` and has `.name` and `.known` (the configured names).

---

## Common pitfalls

- **A rotation lasts until the next stop.** With the default `revoke_on_stop=True`, `stop()` (and so a restart) revokes every secret and the next start resolves them from the config again. With `revoke_on_stop=False` a start only resolves missing secrets, so the rotated value survives. Rotate the value at its source (the environment variable or your secret manager) as well.
- **The model sees what `get_secret` returns.** Redaction keeps the value out of storage, not out of the model's context. Prefer server-side credentials in MCP server specs.
- **Secrets in the context state come back as `[REDACTED]`.** The journal stores the redacted value, so recovery restores the placeholder.
- **No journal, no access log.** The `ProcessConfig` default journal level is `none`.
- **A missing environment variable fails the start.** The process goes to `failed` with a `KeyError` naming the variable; set it or give a default (`${VAR:-default}`).
- **Masked dumps can't be reloaded.** A JSON dump shows literal values as `**********`, which is refused as a value. Use references for anything you serialize.
- **Redaction is exact-match.** A value the agent encodes or splits is not recognized.

---

## What's next

- [Orchestration API](../api.md) -- rotate and revoke over HTTP
- [Agent Context](../context.md) -- why secrets don't belong in the state
- [Conversation](../conversation.md) -- the redacted history
- [Journal](../journal/index.md) -- where the access log goes
- [Meta-Tools](../meta-tools.md) -- `get_secret` and the other open-mode tools

!!! tip "Learn the concept"

    - [AI Agent Secrets Management: Scoped, Expiring Credentials](https://www.promptise.com/guides/view/ai-agent-secrets-management) -- Scope, expire, rotate and audit an agent's credentials.
    - [Secrets in Prompts: System Prompt Leakage and Safe Patterns](https://www.promptise.com/guides/view/secrets-in-prompts-the-silent-killer) -- Why secrets in prompts leak and safer patterns.
