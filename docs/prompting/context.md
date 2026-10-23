---
seo_title: "Prompt Context Providers and Variables"
description: "Give prompts awareness of their world: BaseContext and predefined user, environment and error contexts, PromptContext and context providers that inject text."
---

# Prompt Context System

The context system gives your prompts dynamic awareness of their world: who the user is, what tools are available, what errors occurred previously, and any custom data you need. Context providers fire on every invocation and inject relevant text into the prompt automatically.

## Quick Example

```python
from promptise.prompts import prompt
from promptise.prompts.context import context, user_context, env_context, UserContext, EnvironmentContext

@prompt(model="openai:gpt-5-mini")
@context(user_context(), env_context())
async def analyze(text: str) -> str:
    """Based on available context, analyze: {text}"""

personalized = analyze.with_world(
    user=UserContext(name="Alice", expertise_level="expert"),
    environment=EnvironmentContext(timezone="Europe/Zurich"),
)
print(await personalized.render_async(text="quarterly revenue figures"))   # no LLM call
result = await personalized("quarterly revenue figures")
```

The providers read the **world** (`with_world()`, `PromptBuilder.world()`, a suite's `default_world`, or a YAML file's `world:`) and append a Markdown section such as `## User Context` to the prompt. World data with no provider that reads it is not added to the prompt.

## Concepts

The context system has three layers:

1. **`BaseContext`**: an extensible key-value container. Predefined subclasses (`UserContext`, `EnvironmentContext`, etc.) add typed fields but never restrict what you can store.
2. **`PromptContext`**: the complete world available during prompt execution. It carries the world dict (containing `BaseContext` instances), input args, agent references, and extensible state.
3. **`ContextProvider`**: a pluggable protocol that generates context text at runtime. Providers receive the `PromptContext` and return a markdown string to inject into the prompt.

## BaseContext

The foundation for all context data. Accepts arbitrary keyword arguments.

### Creating Contexts

```python
from promptise.prompts.context import BaseContext, UserContext, EnvironmentContext

# Use a predefined subclass with typed fields
user = UserContext(user_id="123", name="Alice", expertise_level="expert")

# Add custom fields -- no subclassing needed
user = UserContext(user_id="123", department="eng", clearance="high")

# Entirely custom context
project = BaseContext(sprint="2026-Q1", budget=50000, deadline="March 2026")
```

### Accessing Data

```python
project = BaseContext(sprint="2026-Q1", budget=50000)

# Attribute access
project.sprint          # "2026-Q1"

# Dict-style access
project["budget"]       # 50000

# Safe access with default
project.get("missing")  # None
project.get("missing", "fallback")  # "fallback"

# Extend after creation
project.deadline = "March 2026"
```

### Merging Contexts

Merge two contexts into a new one. Values from the second context override the first:

```python
base = BaseContext(sprint="2026-Q1", budget=50000)
override = BaseContext(budget=75000, team_size=5)

merged = base.merge(override)
# merged.sprint == "2026-Q1"
# merged.budget == 75000
# merged.team_size == 5
```

### Serialization

```python
data = project.to_dict()   # {"sprint": "2026-Q1", "budget": 50000}
keys = project.keys()      # dict_keys(["sprint", "budget"])
```

### BaseContext Methods

| Method | Returns | Description |
|--------|---------|-------------|
| `get(key, default=None)` | `Any` | Retrieve a value by key with optional default |
| `to_dict()` | `dict` | Shallow copy of stored data |
| `merge(other)` | `BaseContext` | New context with merged data (other overrides self) |
| `keys()` | `KeysView` | All context keys |

`ctx.missing` returns `None` for an unknown key, while `ctx["missing"]` raises `KeyError`; `"key" in ctx` tests membership, and an empty context is falsy. Field names starting with `_` can't be read or set as attributes. `merge()` keeps the class of the left-hand context.

## Predefined Context Classes

All predefined classes accept additional `**kwargs` for custom fields.

### UserContext

Who the agent is serving.

```python
from promptise.prompts.context import UserContext

user = UserContext(
    user_id="u-123",
    name="Alice",
    preferences={"format": "markdown"},
    expertise_level="expert",  # "beginner", "intermediate", "expert"
    language="english",
    # Custom fields
    department="engineering",
    clearance="high",
)
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `user_id` | `str` | `""` | Unique user identifier |
| `name` | `str` | `""` | Display name |
| `preferences` | `dict` | `{}` | User preference dict |
| `expertise_level` | `str` | `"intermediate"` | Beginner, intermediate, or expert |
| `language` | `str` | `"english"` | Preferred language |

### EnvironmentContext

Runtime environment information.

```python
from promptise.prompts.context import EnvironmentContext

env = EnvironmentContext(
    timezone="America/New_York",
    platform="linux",
    available_apis=["openai", "anthropic"],
)
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `timestamp` | `float` | `time.time()` | Epoch timestamp |
| `timezone` | `str` | `""` | IANA timezone string |
| `platform` | `str` | `""` | OS platform |
| `available_apis` | `list[str]` | `[]` | Available API identifiers |

### ConversationContext

Conversation history and state.

```python
from promptise.prompts.context import ConversationContext

conv = ConversationContext(
    messages=[
        {"role": "user", "content": "Analyze Q1 data"},
        {"role": "assistant", "content": "Here is my analysis..."},
    ],
    turn_count=2,
    summary="User asked for Q1 analysis.",
)
```

| Field | Type | Default | Description |
|-------|------|---------|-------------|
| `messages` | `list[dict]` | `[]` | Message history |
| `turn_count` | `int` | `0` | Number of turns |
| `summary` | `str` | `""` | Compressed summary |

### TeamContext

Other agents in the team.

```python
from promptise.prompts.context import TeamContext

team = TeamContext(
    agents=[
        {"name": "researcher", "role": "data collection", "capabilities": ["web_search"]},
        {"name": "writer", "role": "content creation", "capabilities": ["markdown"]},
    ],
    completed_tasks=[
        {"agent": "researcher", "task": "gather Q1 data", "result_preview": "Revenue: $2.3M"},
    ],
)
```

### ErrorContext

Previous errors for retry awareness.

```python
from promptise.prompts.context import ErrorContext

errors = ErrorContext(
    errors=[{"type": "ValidationError", "message": "Missing field: revenue"}],
    retry_count=1,
    last_error="Missing field: revenue",
)
```

### OutputContext

Expected output format and constraints.

```python
from promptise.prompts.context import OutputContext

output = OutputContext(
    format="json",
    schema_description="Object with summary, confidence, and recommendations fields",
    examples=[{"summary": "...", "confidence": 0.9}],
    constraints=["Must include confidence score"],
)
```

## PromptContext

The complete world available during prompt execution. Every context provider, strategy, perspective, guard, and hook receives this object.

```python
from promptise.prompts.context import PromptContext, UserContext, BaseContext

ctx = PromptContext(
    prompt_name="analyze",
    model="openai:gpt-5-mini",
    input_args={"text": "quarterly data"},
    world={
        "user": UserContext(name="Alice"),
        "project": BaseContext(sprint="2026-Q1"),
    },
)

# Convenience properties
ctx.user          # UserContext if "user" key exists in world
ctx.environment   # EnvironmentContext if "environment" key exists
ctx.conversation  # ConversationContext if "conversation" key exists
ctx.team          # TeamContext if "team" key exists
ctx.errors        # ErrorContext if "errors" key exists
ctx.output        # OutputContext if "output" key exists
```

### PromptContext Fields

| Field | Type | Description |
|-------|------|-------------|
| `prompt_name` | `str` | Name of the prompt |
| `prompt_version` | `str` | Version string |
| `model` | `str` | LLM model identifier |
| `input_args` | `dict` | Arguments passed to the prompt |
| `rendered_text` | `str` | Rendered prompt text |
| `agent` | `Any` | The agent, set when the prompt is an agent's `instructions`; `None` in a standalone call |
| `task` | `Any` | A task object for `task_context()`; `None` unless you build the context yourself |
| `blackboard` | `Any` | A blackboard with `get_section(prefix)` for `blackboard_context()`; `None` unless you build the context yourself |
| `world` | `dict[str, BaseContext]` | Named context instances |
| `state` | `dict` | Free-form state for your providers and hooks (a new, empty dict on every call) |

All fields have defaults (`prompt_version="0.0.0"`, everything else empty or `None`). A standalone call builds a new `PromptContext` from the arguments and the prompt's world on every call, so nothing in it carries over to the next call.

## Context Providers

### The ContextProvider Protocol

Implement a single async method to create a custom provider:

```python
from promptise.prompts.context import ContextProvider, PromptContext

class WeatherContextProvider:
    async def provide(self, ctx: PromptContext) -> str:
        # Return empty string to skip injection
        if not ctx.user:
            return ""
        return "## Current Weather\nSunny, 22C in New York"
```

### Built-In Providers

| Provider | Factory | Injects |
|----------|---------|---------|
| `UserContextProvider` | `user_context()` | `## User Context`: name, expertise, language (when not English), preferences and custom fields of `world["user"]` (a `UserContext`) |
| `EnvironmentContextProvider` | `env_context()` | `## Environment`: platform, timezone, available APIs and custom fields of `world["environment"]` (an `EnvironmentContext`) |
| `ConversationContextProvider` | `conversation_context(last_n=10)` | `## Conversation History`: summary and the last `last_n` messages of `world["conversation"]` |
| `TeamContextProvider` | `team_context()` | `## Team Context`: members and the last 5 completed tasks of `world["team"]` |
| `ErrorContextProvider` | `error_context()` | `## Previous Errors`: retry count, last error and the last 3 errors of `world["errors"]` |
| `OutputContextProvider` | `output_context()` | `## Expected Output`: format, schema, first 3 examples and constraints of `world["output"]` |
| `WorldContextProvider` | `world_context(key, header=None)` | Every non-empty field of `world[key]`, under `header` (default `## <Key>`) |
| `StaticContextProvider` | `static_context(text, header=None)` | Fixed text block |
| `CallableContextProvider` | `callable_context(fn, header=None)` | The string your sync or async `fn(ctx)` returns (nothing when it returns an empty value) |
| `ConditionalContextProvider` | `conditional_context(condition, provider)` | The inner provider's text when `condition(ctx)` is true |
| `MemoryContextProvider` | `memory_context(limit=5, min_score=0.3)` | `## Relevant Context`: memories from `ctx.agent.provider` scoring at least `min_score`; only when the prompt is an agent's `instructions` and the agent has a memory provider |
| `TaskContextProvider` | `task_context()` | `## Task Context`: `goal` (or `name`), `description`, `expected_output`, `priority` of `ctx.task` |
| `BlackboardContextProvider` | `blackboard_context(prefix="result.")` | `## Prior Results`: entries of `ctx.blackboard.get_section(prefix)` |
| `ToolContextProvider` | `tool_context(include_schemas=False)` | Nothing. A placeholder kept for compatibility: tools reach the model as tool definitions. `include_schemas` is ignored |

The typed providers check the class: `user_context()` reads `world["user"]` only when it is a `UserContext` (likewise `EnvironmentContext`, `ConversationContext`, `TeamContext`, `ErrorContext`, `OutputContext`). For a plain `BaseContext`, use `world_context(key)`.

### Using the `@context` Decorator

Attach providers to a prompt with the `@context()` decorator:

```python
from promptise.prompts import prompt
from promptise.prompts.context import context, tool_context, memory_context

@prompt(model="openai:gpt-5-mini")
@context(tool_context(), memory_context())
async def analyze(text: str) -> str:
    """Based on available context, analyze: {text}"""
```

### StaticContextProvider

Inject a fixed text block on every invocation:

```python
from promptise.prompts.context import static_context

rules = static_context(
    "Always respond in JSON format.\nNever include PII.",
    header="## Output Rules",
)
```

### CallableContextProvider

Wrap any async callable as a context provider:

```python
from promptise.prompts.context import callable_context

async def fetch_latest_data(ctx):
    # Fetch from database, API, etc.
    return f"Latest revenue: $2.3M"

data_provider = callable_context(fetch_latest_data, header="## Latest Data")
```

Sync callables also work:

```python
def get_timestamp(ctx):
    import time
    return f"Current time: {time.strftime('%Y-%m-%d %H:%M')}"

time_provider = callable_context(get_timestamp)
```

### ConditionalContextProvider

Only run the inner provider when a condition is met:

```python
from promptise.prompts.context import conditional_context, memory_context

# Only search memories when the prompt runs inside an agent
smart_memory = conditional_context(
    condition=lambda ctx: ctx.agent is not None,
    provider=memory_context(limit=10),
)
```

### WorldContextProvider

Read any custom `BaseContext` from the world dict and format it as markdown:

```python
from promptise.prompts.context import world_context

project_provider = world_context("project", header="## Project Info")
```

This reads `ctx.world["project"]` and formats all its fields into a markdown section.

## API Summary

### Context Classes

| Class | Description |
|-------|-------------|
| `BaseContext(**kwargs)` | Extensible key-value container |
| `UserContext(...)` | User identity and preferences |
| `EnvironmentContext(...)` | Runtime environment |
| `ConversationContext(...)` | Conversation history |
| `TeamContext(...)` | Team composition |
| `ErrorContext(...)` | Previous errors |
| `OutputContext(...)` | Expected output format |
| `PromptContext(...)` | Complete world during execution |

### Provider Factories

| Factory | Description |
|---------|-------------|
| `tool_context(include_schemas=False)` | No-op placeholder |
| `memory_context(limit=5, min_score=0.3)` | Relevant memories (agent instructions only) |
| `task_context()` | Current task metadata |
| `blackboard_context(prefix="result.")` | Prior results from a blackboard |
| `user_context()` | User identity |
| `env_context()` | Environment information |
| `conversation_context(last_n=10)` | Conversation messages |
| `team_context()` | Team members and completed tasks |
| `error_context()` | Previous errors |
| `output_context()` | Output format |
| `static_context(text, header=None)` | Fixed text |
| `callable_context(fn, header=None)` | Any callable |
| `conditional_context(condition, provider)` | Conditional |
| `world_context(key, header=None)` | Custom world context |

### Decorators

| Decorator | Description |
|-----------|-------------|
| `@context(*providers)` | Attach context providers to a prompt (below or above `@prompt`; both add) |

`Prompt.with_context(*providers)` and `PromptBuilder.context(*providers)` add providers without the decorator.

!!! tip "Empty string skips injection"
    If a context provider returns an empty string, nothing is injected. This lets providers gracefully handle missing data without cluttering the prompt.

!!! tip "Custom fields without subclassing"
    You never need to subclass `BaseContext`. Just pass extra keyword arguments: `UserContext(user_id="123", department="eng", clearance="high")`. All fields are accessible via attribute access, dict-style access, and `get()`.

!!! warning "Provider ordering"
    Providers execute in the order they are passed to `@context()`, and their sections appear in that order after the template text. In a [`PromptSuite`](suite-registry.md), suite-level providers run first; a suite provider is skipped when the prompt already has a provider of the same class.

!!! note "As agent instructions, providers run on every call"
    With `build_agent(instructions=my_prompt)`, the agent renders the prompt for each request, so each provider runs once per call and sees that call's user message in `ctx.rendered_text`. The model gets the rendered prompt once, as the start of its system message.

!!! warning "Providers that need an agent"
    `memory_context()` returns nothing in a standalone call because `ctx.agent` is `None`. It works when the prompt is passed as `build_agent(instructions=...)` and the agent has a memory provider. `task_context()` and `blackboard_context()` need `ctx.task` / `ctx.blackboard`, which only a `PromptContext` you build yourself and pass to `render_async(ctx)` sets.

## What's Next

- [PromptBuilder](builder.md): Fluent API for runtime prompt construction
- [Guards](guards.md): Input/output validation
- [Prompt Chaining](chaining.md): Compose prompts into pipelines
