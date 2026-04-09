# PromptBuilder

Fluent API for constructing prompts programmatically at runtime. Use `PromptBuilder` when prompt configuration depends on runtime conditions -- user input, feature flags, A/B tests, or dynamic model selection.

## Quick Example

```python
from promptise.prompts.builder import PromptBuilder
from promptise.prompts.context import UserContext, BaseContext, tool_context
from promptise.prompts.strategies import chain_of_thought, analyst
from promptise.prompts.guards import content_filter, length

prompt = (
    PromptBuilder("analyze")
    .system("Expert data analyst")
    .user(UserContext(expertise_level="expert"))
    .world(project=BaseContext(name="Alpha", deadline="March 2026"))
    .context(tool_context())
    .strategy(chain_of_thought)
    .perspective(analyst)
    .constraint("Must include confidence scores")
    .guard(content_filter(blocked=["secret"]), length(max_length=5000))
    .template("Analyze: {data}")
    .model("openai:gpt-5-mini")
    .build()
)

result = await prompt(data="quarterly figures...")
```

## Concepts

The `PromptBuilder` provides a fluent (chainable) API where every method returns `self`. This lets you incrementally configure a prompt, then call `.build()` to produce the final `Prompt` instance.

Key differences from the `@prompt` decorator:

- **Runtime configuration** -- you can build prompts based on runtime conditions instead of static decoration.
- **Dynamic templates** -- set templates from variables, databases, or user input.
- **Programmatic composition** -- loop through configurations, conditionally add guards, etc.

The builder extracts `{variable}` names from the template text and creates a synthetic async function with matching parameters. The resulting `Prompt` behaves identically to one created with `@prompt`.

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

The system text is prepended to the template. Both support `{variable}` placeholders.

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
from promptise.prompts.context import tool_context, memory_context

builder.context(tool_context(), memory_context(limit=10))
```

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

Enable observability recording on the built prompt:

```python
prompt = (
    PromptBuilder("tracked_prompt")
    .template("Process: {input}")
    .model("openai:gpt-5-mini")
    .observe(True)
    .build()
)
```

## API Summary

| Method | Returns | Description |
|--------|---------|-------------|
| `PromptBuilder(name)` | `PromptBuilder` | Create a new builder |
| `.system(text)` | `self` | Set system/instruction text |
| `.template(text)` | `self` | Set prompt template with `{variable}` placeholders |
| `.model(name)` | `self` | Set the LLM model identifier |
| `.observe(enabled)` | `self` | Enable/disable observability recording |
| `.output_type(t)` | `self` | Set output type for structured parsing |
| `.user(user_ctx)` | `self` | Set user context |
| `.env(env_ctx)` | `self` | Set environment context |
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
    Use `@prompt` for static, declarative prompt definitions. Use `PromptBuilder` when you need to construct prompts based on runtime conditions. Both produce identical `Prompt` objects.

!!! warning "Template is required"
    If neither `.system()` nor `.template()` is called, the builder uses a default placeholder text. Always set an explicit template for production prompts.

!!! tip "Default model"
    If `.model()` is not called, the builder defaults to `openai:gpt-5-mini`. This keeps quick prototyping fast without specifying a model every time.

## What's Next

- [Context System](context.md) -- Dynamic context injection
- [Guards](guards.md) -- Input/output validation
- [Strategies](strategies.md) -- Reasoning strategies and perspectives
