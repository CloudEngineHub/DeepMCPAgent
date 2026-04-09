# Exceptions

Promptise defines a focused hierarchy of custom exceptions for configuration
loading and validation errors.  All exceptions extend `RuntimeError` so they can
propagate naturally through async code without special handling.

**Source:** `src/promptise/exceptions.py`

## Quick example

```python
from promptise.exceptions import (
    SuperAgentError,
    SuperAgentValidationError,
    EnvVarNotFoundError,
)

# Catch all Promptise config errors with a single base class
try:
    # ... load a .superagent file or resolve env vars ...
    pass
except SuperAgentError as exc:
    print(f"Configuration error: {exc}")
```

## Concepts

### Exception hierarchy

```
RuntimeError
  └── SuperAgentError                  # Base for all Promptise config errors
        ├── SuperAgentValidationError  # Schema validation failed
        └── EnvVarNotFoundError        # Missing environment variable
```

All three exceptions are importable from the top-level package:

```python
from promptise import SuperAgentError, SuperAgentValidationError, EnvVarNotFoundError
```

### SuperAgentError

The base exception for all SuperAgent-related errors.  Catch this to handle
any Promptise configuration problem in a single `except` block.

```python
from promptise.exceptions import SuperAgentError

try:
    # Load and validate configuration
    pass
except SuperAgentError as exc:
    print(f"Something went wrong: {exc}")
```

### SuperAgentValidationError

Raised when a `.superagent` file fails Pydantic schema validation.  It carries
structured error details to help users fix the configuration.

```python
from promptise.exceptions import SuperAgentValidationError

try:
    # Load a malformed .superagent file
    pass
except SuperAgentValidationError as exc:
    print(exc)             # Formatted message with file path + field errors
    print(exc.file_path)   # "/path/to/agent.superagent"
    print(exc.errors)      # [{"loc": ["agent", "model"], "msg": "field required"}]
```

The `__str__` method produces a human-readable report:

```
Schema validation failed
File: /path/to/agent.superagent

Validation errors:
  agent -> model: field required
  servers -> math -> command: field required
```

### EnvVarNotFoundError

Raised when a configuration string references `${VAR_NAME}` but the variable
is not set in the environment and no default is provided.

```python
import os
from promptise.env_resolver import resolve_env_var
from promptise.exceptions import EnvVarNotFoundError

# Ensure the var is NOT set
os.environ.pop("SECRET_KEY", None)

try:
    resolve_env_var("token=${SECRET_KEY}", context="servers.auth.token")
except EnvVarNotFoundError as exc:
    print(exc)
    # "Environment variable 'SECRET_KEY' not found (referenced in: servers.auth.token)"
    print(exc.var_name)  # "SECRET_KEY"
    print(exc.context)   # "servers.auth.token"
```

## API summary

### SuperAgentError

| Attribute | Type | Description |
|-----------|------|-------------|
| *(inherits from `RuntimeError`)* | | Base exception -- no extra attributes |

### SuperAgentValidationError

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `message` | `str` | *required* | Human-readable error message |
| `errors` | `list[dict[str, Any]] \| None` | `None` | Pydantic validation error dicts (from `ValidationError.errors()`) |
| `file_path` | `str \| None` | `None` | Path to the configuration file that failed |

| Attribute | Type | Description |
|-----------|------|-------------|
| `errors` | `list[dict[str, Any]]` | List of validation error dicts |
| `file_path` | `str \| None` | Path to the failed configuration file |

### EnvVarNotFoundError

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `var_name` | `str` | *required* | Name of the missing environment variable |
| `context` | `str \| None` | `None` | Where the variable was referenced (e.g. `"servers.math.url"`) |

| Attribute | Type | Description |
|-----------|------|-------------|
| `var_name` | `str` | The missing variable name |
| `context` | `str \| None` | Context string for the reference location |

## Tips and gotchas

!!! tip
    Catch `SuperAgentError` at your application's top level for a single
    error-handling path that covers both schema validation failures and
    missing environment variables.

!!! warning
    `SuperAgentValidationError.errors` contains the raw Pydantic error dicts.
    Each dict has `"loc"` (a tuple of field path segments) and `"msg"` (the
    error message).  The `__str__` method formats these automatically, but
    if you need programmatic access, iterate over `exc.errors` directly.

!!! tip
    Always pass a `context` string when raising `EnvVarNotFoundError` (or when
    calling `resolve_env_var` with a `context` kwarg).  The context appears in
    the error message and makes debugging much faster -- users immediately see
    *which* configuration field references the missing variable.

## What's next

- [Environment Resolver](env-resolver.md) -- the module that raises `EnvVarNotFoundError`
- [Config & Server Specs](config.md) -- configuration objects that trigger validation errors
- [SuperAgent files](agents/superagent-files.md) -- YAML configuration that uses these exceptions
