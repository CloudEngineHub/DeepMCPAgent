# Environment Resolver

The environment resolver lets you embed `${VAR_NAME}` placeholders in
configuration strings and nested dictionaries.  At load time, these references
are replaced with the corresponding environment variable values -- keeping
secrets like API keys and tokens out of source code.

**Source:** `src/promptise/env_resolver.py`

## Quick example

```python
import os
from promptise.env_resolver import resolve_env_var

os.environ["API_KEY"] = "secret123"

resolved = resolve_env_var("Bearer ${API_KEY}")
print(resolved)  # "Bearer secret123"

# With a default value
resolved = resolve_env_var("${MISSING_VAR:-fallback}")
print(resolved)  # "fallback"
```

## Concepts

### Placeholder syntax

Two placeholder forms are supported:

| Syntax | Behaviour |
|--------|-----------|
| `${VAR_NAME}` | **Required** -- raises `EnvVarNotFoundError` if the variable is not set |
| `${VAR_NAME:-default_value}` | **Optional** -- uses `default_value` when the variable is not set |

The placeholder pattern matches variable names that follow Python/shell
conventions: letters, digits, and underscores, starting with a letter or
underscore.

```python
import os
from promptise.env_resolver import resolve_env_var

os.environ["DB_HOST"] = "db.example.com"

# Required variable
resolve_env_var("postgres://${DB_HOST}:5432/app")
# -> "postgres://db.example.com:5432/app"

# Optional with default
resolve_env_var("${DB_PORT:-5432}")
# -> "5432"  (if DB_PORT is not set)

# Multiple placeholders in one string
os.environ["DB_USER"] = "admin"
resolve_env_var("${DB_USER}@${DB_HOST}")
# -> "admin@db.example.com"
```

### Resolving nested dictionaries

`resolve_env_in_dict` walks an entire nested dictionary and resolves
placeholders in all string values -- including strings inside lists and
sub-dictionaries.

```python
import os
from promptise.env_resolver import resolve_env_in_dict

os.environ["TOKEN"] = "abc123"
os.environ["API_URL"] = "https://api.example.com"

config = {
    "auth": "${TOKEN}",
    "server": {
        "url": "${API_URL}",
        "port": 8080,           # Non-strings are passed through unchanged
    },
    "tags": ["${TOKEN}", "static"],
}

resolved = resolve_env_in_dict(config)
# {
#     "auth": "abc123",
#     "server": {"url": "https://api.example.com", "port": 8080},
#     "tags": ["abc123", "static"],
# }
```

### Pre-validation

Before attempting to resolve all variables, you can check which ones are
missing using `validate_all_env_vars_available`.  This is useful for
fail-fast startup validation.

```python
from promptise.env_resolver import validate_all_env_vars_available

config = {
    "api_key": "${OPENAI_API_KEY}",
    "url": "${API_URL:-http://localhost:8080}",
}

missing = validate_all_env_vars_available(config)
if missing:
    print(f"Missing env vars: {', '.join(missing)}")
    # Only lists truly required variables -- those with defaults are excluded
```

### The regex pattern

The raw pattern used for matching is exposed as `ENV_VAR_PATTERN`:

```python
from promptise.env_resolver import ENV_VAR_PATTERN

# re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*?)(?::-(.*?))?\}")
```

This can be useful if you need to scan configuration files for placeholder
references in custom tooling.

## API summary

### resolve_env_var

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `value` | `str` | *required* | String potentially containing `${VAR}` references |
| `context` | `str \| None` | `None` | Optional context for error messages (e.g. `"servers.math.url"`) |
| `allow_missing` | `bool` | `False` | If `True`, leave unresolved vars as-is instead of raising |
| **Returns** | `str` | | String with all environment variables resolved |
| **Raises** | `EnvVarNotFoundError` | | If a required variable is not found and `allow_missing` is `False` |

### resolve_env_in_dict

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `data` | `dict[str, Any]` | *required* | Dictionary potentially containing env var references |
| `context_prefix` | `str` | `""` | Prefix for error context (e.g. `"servers.math"`) |
| **Returns** | `dict[str, Any]` | | New dictionary with all env vars resolved |
| **Raises** | `EnvVarNotFoundError` | | If any required variable is not found |

### validate_all_env_vars_available

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `data` | `dict[str, Any]` | *required* | Parsed configuration data (dict from YAML/JSON) |
| **Returns** | `list[str]` | | Sorted list of missing variable names (empty if all available) |

### ENV_VAR_PATTERN

| Export | Type | Description |
|--------|------|-------------|
| `ENV_VAR_PATTERN` | `re.Pattern` | Compiled regex matching `${VAR}` and `${VAR:-default}` syntax |

## Tips and gotchas

!!! tip
    Use `validate_all_env_vars_available` at application startup to detect
    missing environment variables early, before any agent or server starts up.

!!! warning
    The `allow_missing` flag in `resolve_env_var` leaves unresolved placeholders
    as literal `${VAR}` strings.  This is useful for multi-stage resolution but
    can cause subtle bugs if you forget to resolve them later.

!!! tip
    The `context` parameter (in `resolve_env_var`) and `context_prefix`
    (in `resolve_env_in_dict`) produce clearer error messages.  When a variable
    is missing, the error will say something like:
    `Environment variable 'API_KEY' not found (referenced in: servers.math.url)`.

!!! warning
    `resolve_env_in_dict` returns a **new** dictionary -- it does not mutate the
    input.  Non-string, non-dict, non-list values are passed through unchanged.

## What's next

- [Config & Server Specs](config.md) -- use resolved values in server specifications
- [Exceptions](exceptions.md) -- details on `EnvVarNotFoundError`
- [SuperAgent files](agents/superagent-files.md) -- YAML-based configuration that uses env resolution
