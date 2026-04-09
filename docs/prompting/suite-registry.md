# Suite & Registry

Group related prompts with shared configuration via `PromptSuite`, and version-control prompts with `PromptRegistry`.

## Quick Example

```python
from promptise.prompts import prompt
from promptise.prompts.suite import PromptSuite
from promptise.prompts.strategies import structured_reasoning, critic

class SecurityAudit(PromptSuite):
    default_strategy = structured_reasoning
    default_perspective = critic
    default_constraints = ["Reference OWASP categories"]

    @prompt(model="openai:gpt-5-mini")
    async def scan_code(self, code: str, language: str) -> list[str]:
        """Scan for security vulnerabilities in this {language} code:
        {code}"""

    @prompt(model="openai:gpt-5-mini")
    async def suggest_fixes(self, vulnerabilities: list[str]) -> str:
        """Suggest fixes for: {vulnerabilities}"""

suite = SecurityAudit()
results = await suite.scan_code(code="def login(): ...", language="python")
```

## PromptSuite Concepts

A `PromptSuite` bundles related prompts that share a common configuration. When you define a suite subclass:

1. Set **class attributes** for shared defaults (strategy, perspective, constraints, guards, context providers, world contexts).
2. Decorate methods with `@prompt(...)` to define individual prompts.
3. Suite defaults merge with per-prompt configuration -- per-prompt settings override suite defaults.

The merge rules are:

- **Context providers**: suite providers come first, then prompt-level providers.
- **Strategy / Perspective**: prompt-level wins if set; otherwise suite default applies.
- **Constraints**: suite constraints are prepended to prompt-level constraints.
- **Guards**: suite guards are prepended to prompt-level guards.
- **World contexts**: suite world is the base; prompt-level world entries override matching keys.

### Suite Class Attributes

| Attribute | Type | Description |
|-----------|------|-------------|
| `context_providers` | `list[ContextProvider]` | Shared context providers |
| `default_strategy` | `Strategy \| None` | Default reasoning strategy |
| `default_perspective` | `Perspective \| None` | Default cognitive perspective |
| `default_constraints` | `list[str]` | Default constraint strings |
| `default_guards` | `list[Guard]` | Default guards |
| `default_world` | `dict[str, BaseContext]` | Default world contexts |

### Full Suite Example

```python
from promptise.prompts import prompt
from promptise.prompts.suite import PromptSuite
from promptise.prompts.context import tool_context, memory_context, BaseContext
from promptise.prompts.strategies import chain_of_thought, analyst
from promptise.prompts.guards import content_filter

class DataAnalysisSuite(PromptSuite):
    context_providers = [tool_context(), memory_context()]
    default_strategy = chain_of_thought
    default_perspective = analyst
    default_constraints = ["Include confidence scores", "Cite data sources"]
    default_guards = [content_filter(blocked=["confidential"])]
    default_world = {
        "project": BaseContext(name="Q1 Report", deadline="March 2026"),
    }

    @prompt(model="openai:gpt-5-mini")
    async def summarize(self, data: str) -> str:
        """Summarize this data: {data}"""

    @prompt(model="openai:gpt-5-mini")
    async def forecast(self, data: str, horizon: str) -> str:
        """Based on this data, forecast trends for {horizon}: {data}"""

suite = DataAnalysisSuite()
summary = await suite.summarize(data="Revenue: $2.3M, Growth: 15%...")
forecast = await suite.forecast(data="...", horizon="next quarter")
```

### Discovering Prompts in a Suite

The `prompts` property returns a dict mapping prompt names to `Prompt` instances:

```python
suite = DataAnalysisSuite()

for name, p in suite.prompts.items():
    print(f"{name}: {p}")
# summarize: <Prompt summarize>
# forecast: <Prompt forecast>
```

### Rendering the Suite

Render a combined system prompt from all suite prompts, or render asynchronously with a context:

```python
# Static rendering (no context providers)
system = suite.system_prompt()

# Async rendering with context providers
from promptise.prompts.context import PromptContext
rendered = await suite.render_async(PromptContext())
```

### Suite Methods

| Method | Returns | Description |
|--------|---------|-------------|
| `system_prompt()` | `str` | Render a combined system prompt from all suite prompts |
| `render_async(ctx)` | `str` | Async render all prompts with context providers |
| `prompts` | `dict[str, Prompt]` | All `Prompt` instances on this suite (property) |

---

## PromptRegistry Concepts

The `PromptRegistry` stores versioned prompts keyed by `(name, version)`. It supports:

- **Registration** of prompts at specific semantic versions.
- **Retrieval** by name (latest) or by name + version.
- **Rollback** to remove the latest version and revert to the previous one.
- **Listing** all registered prompts and their versions.

A module-level singleton `registry` is available for convenience. The `@version()` decorator auto-registers prompts into this singleton.

### Versioning Prompts

```python
from promptise.prompts import prompt
from promptise.prompts.registry import registry, version

@version("1.0.0")
@prompt(model="openai:gpt-5-mini")
async def summarize(text: str) -> str:
    """Summarize: {text}"""

@version("2.0.0")
@prompt(model="openai:gpt-5-mini")
async def summarize(text: str) -> str:
    """Provide a concise summary with key takeaways: {text}"""
```

### Retrieving Prompts

```python
# Get the latest version
latest = registry.get("summarize")

# Get a specific version
v1 = registry.get("summarize", "1.0.0")

# Check the latest version string
ver = registry.latest_version("summarize")  # "2.0.0"
```

### Rolling Back

Remove the latest version and revert to the previous one:

```python
rolled_back = registry.rollback("summarize")
# Removes v2.0.0, returns the v1.0.0 Prompt
```

!!! warning "Rollback requires at least two versions"
    Calling `rollback()` when only one version is registered raises `KeyError`.

### Listing All Prompts

```python
registry.list()
# {"summarize": ["1.0.0", "2.0.0"]}
```

### Using a Custom Registry

You can create isolated registries instead of the global singleton:

```python
from promptise.prompts.registry import PromptRegistry

team_registry = PromptRegistry()
team_registry.register("analyze", "1.0.0", my_prompt)
team_registry.register("analyze", "1.1.0", improved_prompt)

latest = team_registry.get("analyze")
```

## API Summary

### PromptSuite

| Attribute / Method | Type / Returns | Description |
|--------------------|----------------|-------------|
| `context_providers` | `list[ContextProvider]` | Shared context providers |
| `default_strategy` | `Strategy \| None` | Default reasoning strategy |
| `default_perspective` | `Perspective \| None` | Default cognitive perspective |
| `default_constraints` | `list[str]` | Default constraints |
| `default_guards` | `list[Guard]` | Default guards |
| `default_world` | `dict[str, BaseContext]` | Default world contexts |
| `prompts` | `dict[str, Prompt]` | All prompts in the suite |
| `system_prompt()` | `str` | Render combined system prompt |
| `render_async(ctx)` | `str` | Async render all prompts |

### PromptRegistry

| Method | Returns | Description |
|--------|---------|-------------|
| `register(name, ver, prompt)` | `None` | Register a prompt at a version |
| `get(name, ver=None)` | `Prompt` | Retrieve by name; `ver=None` returns latest |
| `latest_version(name)` | `str` | Get the latest version string |
| `rollback(name)` | `Prompt` | Remove latest version, return new latest |
| `list()` | `dict[str, list[str]]` | List all prompts and their versions |
| `clear()` | `None` | Remove all registered prompts |

### Decorators

| Decorator | Description |
|-----------|-------------|
| `@version(ver)` | Register a prompt in the global `registry` at version `ver` |

!!! tip "Suite + Registry together"
    Define your prompts in a `PromptSuite` for shared configuration, then register each suite prompt in the `PromptRegistry` for version management. Use `suite.prompts` to iterate and register.

!!! tip "Duplicate version protection"
    Registering the same name + version twice raises `ValueError`. This prevents accidental overwrites in production.

## What's Next

- [Loader & Templates](loader-templates.md) -- Load prompts from YAML files
- [Guards](guards.md) -- Input/output validation
- [Context System](context.md) -- Dynamic context injection
