# Secret Scoping

Per-process secrets with TTL, rotation, access logging, and zero-retention revocation. Secret values live only in memory and are never serialised to journal, checkpoint, or status output.

## When you need it

Environment variables are shared across all agents on the same host. When agent A needs a Stripe key and agent B needs a GitHub token, both see both secrets. Secret scoping gives each process its own isolated credential context with automatic expiry and access logging.

## Configuration

```python
from promptise.runtime import ProcessConfig, SecretScopeConfig

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Process payments.",
    secrets=SecretScopeConfig(
        enabled=True,
        secrets={
            "stripe_key": "${STRIPE_API_KEY}",    # Resolved from env
            "db_password": "${DB_PASSWORD}",
            "static_token": "tok-abc123",          # Literal value
        },
        default_ttl=3600,         # 1 hour default
        ttls={
            "stripe_key": 1800,   # 30 min for payment key
        },
        revoke_on_stop=True,      # Zero-fill on process stop
    ),
)
```

Or in a `.agent` manifest:

```yaml
name: payment-processor
secrets:
  enabled: true
  secrets:
    stripe_key: "${STRIPE_API_KEY}"
    db_password: "${DB_PASSWORD}"
  default_ttl: 3600
  ttls:
    stripe_key: 1800
  revoke_on_stop: true
```

## How it works

### Lifecycle

1. **Resolve** — On `process.start()`, `${ENV_VAR}` references are resolved from the environment. Missing vars raise errors.
2. **Access** — During execution, `scope.get(name)` returns the secret value. Every access is logged to the journal.
3. **Expiry** — Secrets with TTL expire automatically. Accessing an expired secret returns `None`.
4. **Rotation** — `scope.rotate(name, new_value)` replaces a secret without restart. Logged.
5. **Revocation** — On `process.stop()`, all secrets are overwritten with null bytes and removed.

### Access logging

Every `get()` and `rotate()` call creates a journal entry:

```json
{
  "entry_type": "secret_access",
  "process_id": "proc-1",
  "data": {"action": "access", "secret_name": "stripe_key"}
}
```

The secret **value** is never logged — only the name and action.

### Sanitization

`scope.sanitize_text(text)` replaces any secret values found in a string with `[REDACTED]`. Used internally to prevent secrets from leaking into conversation buffers or status output.

### Open mode

When secret scoping is enabled and the agent runs in open mode, it gets a `get_secret` meta-tool:

```
Agent → get_secret(name="stripe_key") → "sk-live-..."
```

Access is logged. The agent never sees secrets in its system prompt — it must explicitly request them.

## API reference

### `SecretScopeConfig`

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `enabled` | `bool` | `False` | Enable secret scoping |
| `secrets` | `dict[str, str]` | `{}` | Secret name → value or `${ENV_VAR}` |
| `default_ttl` | `float \| None` | `None` | Default TTL in seconds |
| `ttls` | `dict[str, float]` | `{}` | Per-secret TTL overrides |
| `revoke_on_stop` | `bool` | `True` | Zero-fill on stop |

### `SecretScope`

| Method | Description |
|--------|-------------|
| `.resolve_initial()` | Resolve `${ENV_VAR}` references and start TTL timers |
| `.get(name)` | Get a secret value (access-logged, returns None if expired) |
| `.rotate(name, new_value, ttl=)` | Replace a secret value |
| `.revoke_all()` | Overwrite and remove all secrets |
| `.sanitize_text(text)` | Replace secret values with `[REDACTED]` |
| `.active_secrets` | List of non-expired secret names |
