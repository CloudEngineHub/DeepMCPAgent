---
seo_title: "PromptBuilder: Build Prompts at Runtime"
description: "Construct prompts at runtime with the fluent PromptBuilder API: templates, model, context providers, strategy, perspective, guards, hooks and output types."
---

# PromptBuilder

Fluent API for constructing prompts programmatically at runtime. Use `PromptBuilder` when prompt configuration depends on runtime conditions: user input, feature flags, A/B tests, or dynamic model selection.

## Quick Example

```python
from promptise.prompts.builder import PromptBuilder
from promptise.prompts.context import UserContext, BaseContext, user_context, world_context
from promptise.prompts.strategies import chain_of_thought, analyst
from promptise.prompts.guards import content_filter, length

prompt = (
    PromptBuilder("analyze")
    .system("You are an expert data analyst.")
    .user(UserContext(expertise_level="expert"))
    .world(project=BaseContext(name="Alpha", deadline="March 2026"))
    .context(user_context(), world_context("project"))
    .strategy(chain_of_thought)
    .perspective(analyst)
    .constraint("Must include confidence scores")
    .guard(content_filter(blocked=["secret"]), length(max_length=5000))
    .template("Analyze in under 150 words: {data}")
    .model("openai:gpt-5-mini")
    .build()
)

result = await prompt(data="quarterly figures...")
```

## Concepts

The `PromptBuilder` provides a fluent (chainable) API where every method returns `self`. This lets you incrementally configure a prompt, then call `.build()` to produce the final `Prompt` instance.

Key differences from the `@prompt` decorator:

- **Runtime configuration**: you can build prompts based on runtime conditions instead of static decoration.
- **Dynamic templates**: set templates from variables, databases, or user input.
- **Programmatic composition**: loop through configurations, conditionally add guards, etc.

The builder extracts `{variable}` names from the system text and template, in order of first appearance, and creates a synthetic async function with one parameter per name. Every parameter is a `str` that defaults to `""`, so the prompt can be called positionally or with keywords, and a missing argument renders as an empty string. With no variables, the function accepts any keyword arguments and ignores them. The resulting `Prompt` behaves like one created with `@prompt`.

`build()` raises `ValueError` when the builder name or a variable name is not a valid Python identifier (for example `PromptBuilder("my-prompt")`).

## Detailed Walkthrough

### Step 1: Create the Builder

```python
from promptise.prompts.builder import PromptBuilder

builder = PromptBuilder("my_prompt")
```

The name is used for logging, observability traces, and the `PromptInspector`.

### Step 2: Set System Text and Template

```python
builder.system("You are a senior financial analyst.")
builder.template("Analyze the following report:\n{report}\nFocus: {focus}")
```

The system text and the template are joined with a blank line into one template; the result is sent as a single system message. Both support `{variable}` placeholders and the [template syntax](loader-templates.md#template-engine). Calling `.system()` or `.template()` again replaces the earlier text.

### Step 3: Configure the Model

```python
builder.model("openai:gpt-5-mini")
```

The default model is `openai:gpt-5-mini` if not explicitly set.

### Step 4: Add World Context

```python
from promptise.prompts.context import UserContext, EnvironmentContext, BaseContext

builder.user(UserContext(name="Alice", expertise_level="expert"))
builder.env(EnvironmentContext(platform="linux", timezone="UTC"))
builder.world(
    project=BaseContext(name="Q1 Report", deadline="March 2026"),
    team=BaseContext(size=5, department="analytics"),
)
```

### Step 5: Add Context Providers

```python
from promptise.prompts.context import user_context, world_context

builder.context(user_context(), world_context("project", header="## Project"))
```

World contexts are only the data. A provider such as `user_context()` or `world_context(key)` turns them into prompt text (see [Context](context.md)).

### Step 6: Set Strategy and Perspective

```python
from promptise.prompts.strategies import chain_of_thought, analyst

builder.strategy(chain_of_thought)
builder.perspective(analyst)
```

### Step 7: Add Constraints and Guards

```python
from promptise.prompts.guards import content_filter, length

builder.constraint("Must include confidence scores", "Cite data sources")
builder.guard(content_filter(blocked=["confidential"]), length(max_length=5000))
```

### Step 8: Add Lifecycle Hooks

```python
async def log_before(ctx):
    print(f"About to call: {ctx.prompt_name}")

async def log_after(ctx, result):
    print(f"Got result: {len(str(result))} chars")

async def handle_error(ctx, error):
    print(f"Error: {error}")

builder.on_before(log_before)
builder.on_after(log_after)
builder.on_error(handle_error)
```

### Step 9: Set Output Type

```python
from pydantic import BaseModel

class AnalysisResult(BaseModel):
    summary: str
    confidence: float
    recommendations: list[str]

builder.output_type(AnalysisResult)
```

### Step 10: Build and Use

```python
prompt = builder.build()
result = await prompt(report="Revenue grew 15%...", focus="trends")
```

## Chaining Everything

Since all builder methods return `self`, you can chain the entire configuration:

```python
from promptise.prompts.builder import PromptBuilder
from promptise.prompts.strategies import chain_of_thought
from promptise.prompts.guards import length

prompt = (
    PromptBuilder("quick_summary")
    .system("Expert summarizer")
    .template("Summarize in {max_words} words: {text}")
    .model("openai:gpt-5-mini")
    .strategy(chain_of_thought)
    .constraint("Be concise")
    .guard(length(max_length=1000))
    .build()
)

result = await prompt(text="long article...", max_words="100")
```

## Dynamic Prompt Construction

The real power of `PromptBuilder` is in dynamic, conditional construction:

```python
from promptise.prompts.builder import PromptBuilder
from promptise.prompts.strategies import chain_of_thought, self_critique
from promptise.prompts.guards import content_filter, length

def create_analysis_prompt(user_level: str, strict_mode: bool) -> "Prompt":
    builder = (
        PromptBuilder("analysis")
        .template("Analyze: {data}")
        .model("openai:gpt-5-mini")
    )

    # Adjust strategy based on user level
    if user_level == "expert":
        builder.strategy(chain_of_thought + self_critique)
        builder.constraint("Include statistical confidence intervals")
    else:
        builder.strategy(chain_of_thought)
        builder.constraint("Explain in simple terms")

    # Add guards in strict mode
    if strict_mode:
        builder.guard(
            content_filter(blocked=["confidential", "internal"]),
            length(max_length=2000),
        )

    return builder.build()

# Usage
prompt = create_analysis_prompt(user_level="expert", strict_mode=True)
result = await prompt(data="quarterly revenue figures...")
```

## Observability

`.observe()` makes the built prompt record every call with a [`PromptObserver`](inspector.md#promptobserver): start, end, errors, guard blocks and context providers, as timeline events. `.observe(True)` records to the prompt's own collector; pass an `ObservabilityCollector` (or a `PromptObserver`) to record somewhere shared:

```python
from promptise.observability import ObservabilityCollector

collector = ObservabilityCollector()
prompt = (
    PromptBuilder("tracked_prompt")
    .template("Process: {input}")
    .model("openai:gpt-5-mini")
    .observe(collector)
    .build()
)
# After a call: [e.event_type for e in collector.get_timeline()]
```

## API Summary

| Method | Returns | Description |
|--------|---------|-------------|
| `PromptBuilder(name="builder_prompt")` | `PromptBuilder` | Create a new builder; `name` becomes the prompt's `name` |
| `.system(text)` | `self` | Set system/instruction text |
| `.template(text)` | `self` | Set prompt template with `{variable}` placeholders |
| `.model(name)` | `self` | Set the LLM model identifier |
| `.observe(enabled=True)` | `self` | Record calls with a `PromptObserver` (`True`, a collector or an observer; `False` turns it off) |
| `.output_type(t)` | `self` | Set output type for structured parsing |
| `.user(user_ctx)` | `self` | Set `world["user"]` |
| `.env(env_ctx)` | `self` | Set `world["environment"]` |
| `.world(**contexts)` | `self` | Add named world contexts |
| `.context(*providers)` | `self` | Add context providers |
| `.strategy(s)` | `self` | Set reasoning strategy |
| `.perspective(p)` | `self` | Set cognitive perspective |
| `.constraint(*texts)` | `self` | Add constraint strings |
| `.guard(*guards)` | `self` | Add input/output guards |
| `.on_before(fn)` | `self` | Set pre-execution hook |
| `.on_after(fn)` | `self` | Set post-execution hook |
| `.on_error(fn)` | `self` | Set error-handling hook |
| `.build()` | `Prompt` | Construct the final `Prompt` instance |

!!! tip "Builder vs. decorator"
    Use `@prompt` for static, declarative prompt definitions. Use `PromptBuilder` when you need to construct prompts based on runtime conditions. Both produce `Prompt` objects with the same methods.

!!! note "Blocks, inspector and description"
    The builder has no methods for blocks, an inspector or a description. Add them to the built prompt: `builder.build().with_blocks(Identity("Analyst")).with_inspector(inspector)`. `.observe()` is the builder's way to attach a [PromptObserver](inspector.md#promptobserver); `.with_observer(...)` works on the built prompt too.

!!! warning "Template is required"
    If neither `.system()` nor `.template()` is called, the template is "Respond to the user's request." Always set an explicit template for production prompts.

!!! tip "Default model"
    If `.model()` is not called, the builder defaults to `openai:gpt-5-mini`. This keeps quick prototyping fast without specifying a model every time.

## What's Next

- [Context System](context.md): Dynamic context injection
- [Guards](guards.md): Input/output validation
- [Strategies](strategies.md): Reasoning strategies and perspectives
