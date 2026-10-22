---
seo_title: "Reasoning Graph Node Flags Reference"
description: "Typed node flags that control execution, caching, context, model selection, observability and output processing in a reasoning graph, and how to add your own."
---

# Node Flags

Every node in a Reasoning Graph declares its capabilities and behaviors through **typed flags**. Flags are checked by the engine at runtime to control execution flow, caching, error handling, and observability.

## Using Flags

```python
from promptise.engine import GraphState, NodeFlag, NodeResult, PromptNode, node

# Set flags on any node
PromptNode("analyze",
    instructions="Analyze the data.",
    flags={NodeFlag.CRITICAL, NodeFlag.OBSERVABLE},
)

# Functional nodes support flags too
@node("validate", flags={NodeFlag.RETRYABLE})
async def validate(state: GraphState) -> NodeResult:
    return NodeResult(output={"ok": True})

# Check flags at runtime (@node returns a node object)
if validate.has_flag(NodeFlag.RETRYABLE):
    ...
```

## Flag Reference

### Execution Control

| Flag | Engine Behavior |
|------|----------------|
| `ENTRY` | Marks the starting node. Set via `is_entry=True`. Read by `PromptGraph(nodes=[...])` (the first such node becomes the entry) and by `from_pool()` / `build_agent(node_pool=...)` (the pool's first step). A node added later with `add_node()` needs `set_entry()`. |
| `TERMINAL` | Set via `is_terminal=True`. Only an `AutonomousNode` pool reads it: a terminal step that succeeds ends the autonomous loop and writes the answer. In a static graph it does nothing; end a path with `default_next="__end__"` or an edge to `"__end__"`. |
| `CRITICAL` | If this node errors, the engine **aborts the run** right away: post-node hooks, mutations and transitions are skipped, the stream gets `on_node_error` with `critical: True`, `ExecutionReport.error` is set, and `ainvoke()` raises `GraphExecutionError` (the node's exception is its `__cause__`). |
| `SKIP_ON_ERROR` | If the **previous** node's result has an `error` (including a note a hook wrote, such as a `TimingHook` overrun), this node does not run: its result is empty with `transition_reason="skipped_on_error"`, and the transitions are resolved from that. Useful for optional enrichment steps. |
| `RETRYABLE` | On failure (a result with an `error`, or an exception), the engine runs the node again, up to `max_iterations` attempts in total (default 10), waiting `min(0.5 * 2**n, 8)` seconds between attempts (0.5s, 1s, 2s, 4s, 8s, 8s...). Before each retry `state.context["_retry_attempt"]` and `["_retry_error"]` are set, so the node can adapt; both are removed afterwards. The attempts run through `execute()`, so a streamed run gets no token chunks from a `RETRYABLE` node. A [`RetryNode`](engine-nodes.md#retrynode) is never retried this way (it retries itself). |
| `REQUIRES_HUMAN` | Sets `state.context["_awaiting_human"]` to the node name and awaits `on_human_required(node_name, state)` on every hook that has it, before the node runs. The node still executes; the flag does not pause anything by itself. An exception in `on_human_required` is logged and the run goes on (before 1.7.0 it ended the run). For a real approval step, use [`HumanNode`](engine-nodes.md#humannode). |

### Context & Memory

| Flag | Engine Behavior |
|------|----------------|
| `NO_HISTORY` | Before the node runs, the engine keeps only the first message when it is a system message (no messages otherwise). Afterwards the original messages are restored, plus the messages the node added. The node then sees no user question, so give it what it needs through `input_keys` or `inherit_context_from`. |
| `ISOLATED_CONTEXT` | The node runs with a `state.context` holding only its `input_keys` (empty when it has none). Afterwards the original context is restored, and only the node's `output_key` and `{name}_output` values are merged back. |
| `CACHEABLE` | Within one run, a second visit to the node with the same `input_keys` values returns the first visit's result (output, raw output and `next_node`; no new messages or tool calls) without executing. The key is the node name plus the `input_keys` values (only the name for a node without `input_keys`). The cache lives in `state.context["_node_cache"]`, holds up to 128 results, skips results with an error, and starts empty on every run, so it never carries results between runs. |

### Model Selection

| Flag | Engine Behavior |
|------|----------------|
| `INJECT_TOOLS` | A `PromptNode` receives the engine's tools at runtime (the tools `build_agent()` discovered, or `PromptGraphEngine(tools=...)`) on top of its own. Set via `inject_tools=True`. Other node types ignore it. |
| `LIGHTWEIGHT` | If the node has no `model_override` and the engine was created with a `lightweight_model`, the engine swaps to that model for this node. `build_agent` creates the engine without one, so in an agent the flag has no effect; use `model_override` there. `ThinkNode` has this flag by default. |

### Observability

| Flag | Engine Behavior |
|------|----------------|
| `OBSERVABLE` | After the node runs, awaits `on_observable_event(event, state)` on every hook that has it, with a `NodeEvent(event="on_node_metrics", data={node_name, duration_ms, total_tokens, tool_calls, error})`. `duration_ms` is the node's own measurement (the engine's timing is set afterwards). An exception in the hook is logged and the run goes on (before 1.7.0 it ended the run). |
| `VERBOSE` | Logs the first 2,000 characters of the raw output at DEBUG level on the `promptise.engine` logger. |

### Output Processing

| Flag | Engine Behavior |
|------|----------------|
| `SUMMARIZE_OUTPUT` | If `raw_output` is longer than 1,000 characters, the engine makes one extra model call (the engine's model) to summarize it and replaces `NodeResult.raw_output` with the summary. The conversation, `output` and `state.context["{name}_output"]` keep the full text, so it mainly shortens what hooks and reports see. A failed summary call is logged and ignored. |
| `VALIDATE_OUTPUT` | Validates `result.output` against the node's `output_schema` (a Pydantic model, or a dict whose keys must be present). Problems are added to `result.guards_failed` (an `on_guard_fail` edge can route on them); a pass adds `"output_schema_validation"` to `guards_passed`. Does nothing without an `output_schema` or with a `None` output. |

### Concurrency & State

| Flag | Engine Behavior |
|------|----------------|
| `READONLY` | Declares that this node only reads state. Informational: the engine does not act on it (hooks and your own code can, with `node.has_flag()`). |
| `STATEFUL` | Declares that this node writes `state.context`. Informational. |
| `PARALLEL_SAFE` | Declares that this node can run concurrently. Informational: `ParallelNode` and `FanOutNode` run their children concurrently whatever their flags. |

## Pre-built Reasoning Node Flags

Each reasoning node ships with sensible default flags:

| Node | Default Flags | Rationale |
|------|--------------|-----------|
| ThinkNode | `READONLY`, `LIGHTWEIGHT` | Pure reasoning, no state writes, smaller model sufficient |
| ReflectNode | `STATEFUL`, `OBSERVABLE` | Writes reflections to state, worth logging |
| ObserveNode | `STATEFUL` | Writes entities/facts to state.context |
| JustifyNode | `READONLY`, `OBSERVABLE`, `VERBOSE` | Audit trail, full logging matters |
| CritiqueNode | `READONLY`, `OBSERVABLE` | Pure analysis, severity worth logging |
| PlanNode | `STATEFUL`, `OBSERVABLE` | Writes to state.plan |
| SynthesizeNode | `OBSERVABLE` | Final output, important to capture |
| ValidateNode | `READONLY`, `VALIDATE_OUTPUT` | Quality gate, validates own output |
| RetryNode | none | Retries itself; the engine does not retry it again |
| FanOutNode | `PARALLEL_SAFE` | Designed for concurrent execution |

## Custom Flags

The flag set accepts any hashable value. You can define domain-specific flags:

```python
class MyFlags:
    AUDIT_REQUIRED = "audit_required"
    PII_SENSITIVE = "pii_sensitive"

PromptNode("handle_user_data",
    flags={MyFlags.PII_SENSITIVE, NodeFlag.OBSERVABLE},
)

# Check in hooks
class AuditHook:
    async def post_node(self, node, result, state):
        if node.has_flag(MyFlags.AUDIT_REQUIRED):
            await audit_log.record(node.name, result)
        return result
```

## Engine Configuration

```python
from promptise.engine import PromptGraphEngine

engine = PromptGraphEngine(
    graph=graph,
    model=main_model,
    lightweight_model=small_model,  # Used for LIGHTWEIGHT nodes
)
```

Flags are processed in this order around a node: `SKIP_ON_ERROR`, `CACHEABLE`
(lookup), `NO_HISTORY`, `ISOLATED_CONTEXT`, `LIGHTWEIGHT`, `REQUIRES_HUMAN`;
then the node runs (with `RETRYABLE` retries); then the restores, `CACHEABLE`
(store), `VERBOSE`, `OBSERVABLE`, `SUMMARIZE_OUTPUT`, `VALIDATE_OUTPUT`;
then `CRITICAL`. A skipped or cached node gets none of the post-execute steps.
