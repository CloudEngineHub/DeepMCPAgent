# Inspector & Observability

See exactly how your prompts are assembled, which blocks made it in, token estimates, what was dropped, guard results, and the exact text sent to the LLM. No other framework gives you this level of visibility.

## Quick Example

```python
from promptise.prompts.inspector import PromptInspector
from promptise.prompts import prompt
from promptise.prompts.blocks import blocks, Identity, Rules

inspector = PromptInspector()

@prompt(model="openai:gpt-5-mini", inspect=inspector)
@blocks(Identity("Analyst"), Rules(["Be precise"]))
async def analyze(text: str) -> str:
    """Analyze: {text}"""

result = await analyze("quarterly data")

trace = inspector.last()
print(trace.prompt_name)            # "analyze"
print(trace.blocks_included)        # ["identity", "rules"]
print(trace.total_tokens_estimated) # 45
print(trace.input_text)             # Full prompt sent to LLM
print(trace.latency_ms)             # Execution time in ms
```

## Concepts

The prompt framework has two observability tools:

- **`PromptInspector`** -- local debugging and tracing. Records detailed traces of every prompt assembly and execution. Designed for development and testing.
- **`PromptObserver`** -- production telemetry. Records structured events to a pluggable `ObservabilityCollector`. Designed for metrics, monitoring, and dashboards.

Both can be used simultaneously. The inspector records everything locally; the observer emits events to your telemetry pipeline.

## PromptInspector

### Recording Traces

Attach the inspector to a prompt via the `inspect` parameter:

```python
from promptise.prompts.inspector import PromptInspector

inspector = PromptInspector()

@prompt(model="openai:gpt-5-mini", inspect=inspector)
async def summarize(text: str) -> str:
    """Summarize: {text}"""

result = await summarize("long article text...")
```

Every invocation records a `PromptTrace` with full assembly and execution details.

### Reading Traces

```python
# Most recent trace
trace = inspector.last()

# All recorded traces
all_traces = inspector.traces

# Human-readable summary
print(inspector.summary())
```

### PromptTrace Fields

| Field | Type | Description |
|-------|------|-------------|
| `prompt_name` | `str` | Name of the prompt |
| `model` | `str` | Model used |
| `timestamp` | `float` | Unix timestamp |
| `blocks` | `list[BlockTrace]` | Per-block details |
| `total_tokens_estimated` | `int` | Estimated token count |
| `blocks_included` | `list[str]` | Blocks included in assembly |
| `blocks_excluded` | `list[str]` | Blocks not included |
| `context_providers` | `list[ContextTrace]` | Per-provider details |
| `input_text` | `str` | Final text sent to LLM |
| `output_text` | `str` | LLM response |
| `latency_ms` | `float` | Execution time in milliseconds |
| `guards_passed` | `list[str]` | Guard names that passed |
| `guards_failed` | `list[str]` | Guard names that failed |
| `flow_phase` | `str` | Conversation flow phase (if used) |
| `flow_turn` | `int` | Conversation flow turn number |

### ContextTrace Fields

| Field | Type | Description |
|-------|------|-------------|
| `provider_name` | `str` | Name of the context provider |
| `chars_injected` | `int` | Characters injected into the prompt |
| `render_time_ms` | `float` | Time to render the context |

### Summary Report

```python
print(inspector.summary())
```

Produces a human-readable report covering all recorded prompt traces, including blocks, context providers, and guards.

### Inspector API

| Method / Property | Returns | Description |
|-------------------|---------|-------------|
| `last()` | `PromptTrace \| None` | Most recent prompt trace |
| `traces` | `list[PromptTrace]` | All recorded prompt traces |
| `summary()` | `str` | Human-readable summary report |
| `clear()` | `None` | Discard all recorded traces |

Recording methods (auto-called by `@prompt`; available for custom integrations):

| Method | Description |
|--------|-------------|
| `record_assembly(assembled, prompt_name, model)` | Record how the prompt was assembled |
| `record_execution(trace, output, latency_ms)` | Record LLM output and latency |
| `record_context(trace, provider_name, chars, time)` | Record a context provider contribution |
| `record_guard(trace, guard_name, passed)` | Record guard pass/fail |

---

## PromptObserver

`PromptObserver` is the production telemetry bridge. It wraps the `ObservabilityCollector` and emits structured events for prompt lifecycle stages.

### Setup

```python
from promptise.prompts.observe import PromptObserver
from promptise.observability import ObservabilityCollector

collector = ObservabilityCollector()
observer = PromptObserver(collector)
```

Attach to prompts via the `observer` parameter or `observe=True`:

```python
@prompt(model="openai:gpt-5-mini", observer=observer)
async def analyze(text: str) -> str:
    """Analyze: {text}"""
```

### Events Recorded

| Method | Event | Data |
|--------|-------|------|
| `record_start(prompt_name, model, input_text)` | `PROMPT_START` | prompt name, model, input length |
| `record_end(prompt_name, model, latency_ms, output_length)` | `PROMPT_END` | prompt name, model, latency, output length |
| `record_error(prompt_name, error)` | `PROMPT_ERROR` | prompt name, error type, error message |
| `record_guard_block(prompt_name, guard_name, reason)` | `PROMPT_GUARD_BLOCK` | prompt name, guard name, reason |
| `record_context(prompt_name, provider_name, chars_injected)` | `PROMPT_CONTEXT` | prompt name, provider, chars injected |

### Observer API

| Method | Description |
|--------|-------------|
| `record_start(prompt_name, model, input_text)` | Record prompt execution start |
| `record_end(prompt_name, model, latency_ms, output_length)` | Record prompt execution end |
| `record_error(prompt_name, error)` | Record prompt execution error |
| `record_guard_block(prompt_name, guard_name, reason)` | Record a guard blocking execution |
| `record_context(prompt_name, provider_name, chars_injected)` | Record context provider execution |

!!! tip "Inspector for debugging, Observer for production"
    Use `PromptInspector` during development to understand exactly how your prompts are assembled. Use `PromptObserver` in production to feed metrics into your monitoring pipeline. They serve different purposes and can run simultaneously.

!!! warning "Inspector stores traces in memory"
    The inspector keeps all traces in memory. Call `inspector.clear()` periodically in long-running processes to avoid unbounded memory growth.

## What's Next

- [Guards](guards.md) -- Input/output validation
- [Strategies](strategies.md) -- Control how the agent reasons
