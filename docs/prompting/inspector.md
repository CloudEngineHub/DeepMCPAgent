---
seo_title: "Prompt Inspector: Debug Prompt Assembly"
description: "See how a prompt was assembled: blocks included, token estimates, what was dropped, guard results and the exact text sent, plus PromptObserver telemetry."
---

# Prompt Inspector & Observability

See how your prompts are assembled: which blocks made it in, token estimates, what was dropped, guard results, and the exact text sent to the LLM.

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
print(trace.total_tokens_estimated) # estimate for the assembled blocks
print(trace.input_text)             # Full prompt sent to LLM
print(trace.latency_ms)             # Execution time in ms
```

## Concepts

The prompt framework has two observability tools:

- **`PromptInspector`**: local debugging and tracing. Records detailed traces of every prompt assembly and execution. Designed for development and testing.
- **`PromptObserver`**: production telemetry. Records structured events to a pluggable `ObservabilityCollector`. Designed for metrics, monitoring, and dashboards.

Both can be used simultaneously. The inspector records everything locally; the observer emits events to your telemetry pipeline.

## PromptInspector

### Recording Traces

Attach the inspector to a prompt with `@prompt(inspect=...)` or `prompt.with_inspector(inspector)`. A [`ConversationFlow`](flows.md#inspecting-a-flow) takes `inspector=` too:

```python
from promptise.prompts.inspector import PromptInspector

inspector = PromptInspector()

@prompt(model="openai:gpt-5-mini", inspect=inspector)
async def summarize(text: str) -> str:
    """Summarize: {text}"""

result = await summarize("long article text...")
```

Each standalone call, each `render_async()` (also when the prompt is an agent's `instructions`), and each flow `start()` / `next_turn()` / `transition()` records one `PromptTrace`. One inspector can be shared by many prompts.

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
| `total_tokens_estimated` | `int` | Estimated tokens of the assembled **blocks** (not the whole prompt) |
| `blocks_included` | `list[str]` | Blocks included in assembly |
| `blocks_excluded` | `list[str]` | Blocks not included |
| `context_providers` | `list[ContextTrace]` | One entry per context provider that ran: class name, characters it added, time it took |
| `input_text` | `str` | Final text sent to the LLM (after input guards) |
| `output_text` | `str` | Raw LLM response after the strategy's `parse()` (standalone calls only) |
| `latency_ms` | `float` | Execution time in milliseconds (standalone calls only) |
| `guards_passed` | `list[str]` | Guards that passed every check they ran, each listed once (a guard attached with `@guard` or `with_guards()` checks both the input and the output) |
| `guards_failed` | `list[str]` | Guards that rejected the input or output |
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
| `traces` | `list[PromptTrace]` | All recorded prompt traces (a copy) |
| `last_graph()` | `GraphTrace \| None` | Most recent graph trace |
| `graph_traces` | `list[GraphTrace]` | All recorded graph traces (a copy) |
| `summary()` | `str` | Human-readable summary report |
| `clear()` | `None` | Discard all recorded traces |

Recording methods (auto-called by `@prompt`; available for custom integrations):

| Method | Description |
|--------|-------------|
| `record_assembly(assembled, prompt_name, model)` | Record how the prompt was assembled |
| `record_execution(trace, output, latency_ms)` | Record LLM output and latency |
| `record_context(trace, provider_name, chars_injected, render_time_ms)` | Record a context provider contribution |
| `record_guard(trace, guard_name, passed)` | Record guard pass/fail |
| `record_graph(graph_name, node_traces, total_duration_ms, path, iterations, final_state)` | Record a graph run as a `GraphTrace` (with `NodeTrace` entries) |

Guard names in traces are the guard's `name` attribute, or its class name (`ContentFilterGuard`, `LengthGuard`, ...).

---

## PromptObserver

`PromptObserver(collector)` turns prompt lifecycle events into timeline events on an [`ObservabilityCollector`](../core/observability.md). Recording failures are logged at debug level and never raise.

### Setup

```python
from promptise.prompts.observe import PromptObserver
from promptise.observability import ObservabilityCollector

collector = ObservabilityCollector()
observer = PromptObserver(collector)
```

A prompt records its calls with an observer when you ask for it: `@prompt(observe=...)`, `PromptBuilder.observe(...)`, `observe: true` in a YAML prompt file, or `with_observer(...)` on any prompt:

```python
from promptise.observability import ObservabilityCollector
from promptise.prompts import prompt

collector = ObservabilityCollector()

@prompt(model="openai:gpt-5-mini", observe=collector)   # or observe=observer
async def analyze(text: str) -> str:
    """Analyze: {text}"""

result = await analyze("Q3 revenue $3.2M")
print([e.event_type for e in collector.get_timeline()])
# [<TimelineEventType.PROMPT_START: 'prompt.start'>, <TimelineEventType.PROMPT_END: 'prompt.end'>]
```

| `observe=` | Events go to |
|------------|--------------|
| `False` (default) | Nowhere; `prompt.observer` is `None` |
| `True` | The prompt's own `ObservabilityCollector`: `analyze.observer.collector.get_timeline()` |
| An `ObservabilityCollector` | That collector (share one between prompts, or with an agent's collector) |
| A `PromptObserver` | That observer |

Each standalone call (`await analyze(...)`) records `PROMPT_START` when it starts, `PROMPT_CONTEXT` for each context provider, `PROMPT_GUARD_BLOCK` when a guard rejects the input or output, then `PROMPT_END` on success or `PROMPT_ERROR` on failure. Copies made with `with_*()` record to the same observer. Rendering without an LLM call (`render_async()`, a prompt used as agent `instructions`) records nothing; the agent's own [observability](../core/observability.md) covers those runs. An observer that raises is logged at debug level and never fails the call.

You can also call the observer yourself, for example from lifecycle hooks, with the methods below.

### Events Recorded

| Method | Event | Data |
|--------|-------|------|
| `record_start(prompt_name, model, input_text)` | `PROMPT_START` | `prompt_name`, `model`, `input_length` |
| `record_end(prompt_name, model, latency_ms, output_length)` | `PROMPT_END` | `prompt_name`, `model`, `latency_ms`, `output_length` |
| `record_error(prompt_name, error)` | `PROMPT_ERROR` | `prompt_name`, `error_type`, `error_message` |
| `record_guard_block(prompt_name, guard_name, reason)` | `PROMPT_GUARD_BLOCK` | `prompt_name`, `guard_name`, `reason` |
| `record_context(prompt_name, provider_name, chars_injected)` | `PROMPT_CONTEXT` | `prompt_name`, `provider`, `chars_injected` |

Each event uses the prompt name as its `agent_id`.

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

- [Guards](guards.md): Input/output validation
- [Strategies](strategies.md): Control how the agent reasons
