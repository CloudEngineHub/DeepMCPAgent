---
seo_title: "Reasoning Graph Hooks and Tool Hooks"
description: "Hook into the reasoning engine's execution loop for logging, timing, cycle detection, metrics, budgets and tool calls, and read the execution report."
---

# Graph Hooks & Observability

Hooks intercept the engine's execution loop at defined points. Use them for logging, metrics, budget enforcement, cycle detection, and custom behavior.

## Hook Protocol

Hooks implement any subset of these async methods:

```python
class MyHook:
    async def pre_node(self, node: BaseNode, state: GraphState) -> GraphState:
        """Called before each node executes. Can modify state."""
        return state

    async def post_node(self, node: BaseNode, result: NodeResult, state: GraphState) -> NodeResult:
        """Called after each node executes. Can modify result."""
        return result

    async def pre_tool(self, tool_name: str, args: dict, state: GraphState) -> dict:
        """Called before each tool call of a PromptNode or ToolNode. Returns the
        args to call the tool with. Raise to block the call."""
        return args

    async def post_tool(self, tool_name: str, result: str, args: dict, state: GraphState) -> str:
        """Called after each tool call. Returns the result the model sees."""
        return result

    async def on_tool_reused(self, tool_name: str, args: dict, result: str, state: GraphState) -> None:
        """Called instead of pre_tool/post_tool when a call is answered from an
        earlier result without running the tool (tools opted in with
        ContextCompaction(reuse_results=...), and a repeat of an identical call
        in one model turn of a ToolNode(deduplicate=True))."""
        pass

    async def on_error(self, node: BaseNode, error: Exception, state: GraphState) -> None:
        """Called when a node raises an exception."""
        pass

    async def on_graph_mutation(self, mutation: GraphMutation, state: GraphState) -> None:
        """Called when the graph is mutated at runtime."""
        pass

    async def on_observable_event(self, event: NodeEvent, state: GraphState) -> None:
        """Called for nodes with the OBSERVABLE flag after execution."""
        pass

    async def on_human_required(self, node_name: str, state: GraphState) -> None:
        """Called for nodes with the REQUIRES_HUMAN flag."""
        pass
```

Only implement the methods you need — the engine checks `hasattr()` before calling.

## Usage

```python
from promptise.engine import PromptGraphEngine, LoggingHook, MetricsHook, BudgetHook

engine = PromptGraphEngine(
    graph=graph,
    model=model,
    hooks=[
        LoggingHook(level=logging.DEBUG),
        MetricsHook(),
        BudgetHook(max_tokens=50000),
    ],
)
```

## Tool Hooks

`pre_tool` and `post_tool` run around every tool call of a `PromptNode` and of a
`ToolNode`, the same way in both:

1. Each hook's `pre_tool(tool_name, args, state)`, in registration order. It
   returns the arguments to use, so a hook can change them (the next hook and
   the tool get what it returned).
2. The tool runs with those arguments (after a `ToolNode(validate_inputs=True)`
   has checked them against the tool's schema).
3. Each hook's `post_tool(tool_name, result, args, state)`, in registration
   order, with the arguments the tool got. It returns the result to use: what
   it returns is what the model sees in the call's `ToolMessage`.

**Blocking a call.** A `pre_tool` hook blocks a call by raising. The tool does
not run and no `post_tool` hook is called for it; the model gets
`"Error: <exception message>"` as the call's result (`status="error"`), the call
counts as failed (`NodeResult.tool_calls_failed`), and the stream reports it as
`on_tool_error` (`ToolEndEvent(success=False, tool_summary="Tool call failed")`
in `astream_with_tools()`). The model can then answer, or call something else.
A `post_tool` hook that raises makes the call fail the same way, after the tool
ran.

```python
class ReadOnlyHook:
    async def pre_tool(self, tool_name, args, state):
        if tool_name.startswith(("delete_", "drop_")):
            raise PermissionError(f"{tool_name} is not allowed in read-only mode")
        return args
```

A call answered from an earlier result without running the tool
(`ContextCompaction(reuse_results=...)`, or a repeat in one model turn of a
`ToolNode(deduplicate=True)`) gets `on_tool_reused` instead of `pre_tool` /
`post_tool`; its result is the earlier call's, as `post_tool` left it.

## Built-in Hooks

### LoggingHook

Logs every node execution with timing, token counts, tool calls, and errors.

```python
LoggingHook(level=logging.INFO)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `level` | `int` | `logging.INFO` | Python logging level |

**Log output format:**

```
▶ Node 'plan' (iter=1, type=PlanNode)
◀ Node 'plan' → act (150ms, 340 tokens, 0 tools, 0 errors)
```

### TimingHook

Enforces per-node time budgets. If a node exceeds its budget, the hook sets an error on the result (but does NOT abort the graph — use the `CRITICAL` flag for that).

```python
TimingHook(
    default_budget_ms=30000,
    per_node_budgets={"search": 10000, "synthesize": 60000},
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `default_budget_ms` | `float` | `30000` | Default time budget per node (30s) |
| `per_node_budgets` | `dict[str, float]` | `None` | Per-node time budget overrides (ms) |

### CycleDetectionHook

Detects infinite loops by tracking patterns in visited node sequences. Forces graph termination when a repeating pattern is detected.

```python
CycleDetectionHook(sequence_length=3, max_repeats=3)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `sequence_length` | `int` | `3` | Length of node sequence to check for repetition |
| `max_repeats` | `int` | `3` | Max times a sequence can repeat before forcing end |

Example: If visited nodes are `[A, B, C, A, B, C, A, B, C]`, the pattern `[A, B, C]` has repeated 3 times — the hook sets `state.current_node = "__end__"`.

### MetricsHook

Collects per-node execution metrics. Access collected data via `summary()` after the run.

```python
hook = MetricsHook()
engine = PromptGraphEngine(graph=graph, model=model, hooks=[hook])
await engine.ainvoke({"messages": [...]})

metrics = hook.summary()
# {
#     "plan": {"calls": 1, "total_tokens": 450, "total_duration_ms": 1200, "tool_calls": 0, "errors": 0},
#     "act":  {"calls": 3, "total_tokens": 2100, "total_duration_ms": 8500, "tool_calls": 5, "errors": 0},
# }

hook.reset()  # Clear for next run
```

**Tracked metrics per node:**

| Metric | Description |
|--------|-------------|
| `calls` | Number of times this node executed |
| `total_tokens` | Cumulative token usage |
| `total_duration_ms` | Cumulative execution time |
| `tool_calls` | Total tool calls made |
| `errors` | Number of errors encountered |

### BudgetHook

Enforces a token and/or cost budget for each graph run. After every node it adds the node's tokens to the run's total; when the run goes over `max_tokens` or `max_cost_usd`, the hook ends the run and writes the reason (`"Token budget exceeded: 5078/5000 tokens"`) to that node's `NodeResult.error`. The check runs after a node, so a run can go over the budget by up to one node's tokens.

The budget is per run: the total comes from the run's own state, so one hook on an engine caps every run separately (concurrent runs included) and never carries tokens over to the next run.

How the run ends depends on where the budget ran out:

- **In the middle of a tool loop** (the model asked for tools and has not seen their results): there is no answer, so the engine raises `GraphBudgetExceededError` instead of returning the last tool result. It is a `GraphStoppedError` and a `GraphExecutionError`, with `reason`, `stopped_by` (`"BudgetHook"`), `pending_tools` and `report`. `engine.last_report.error` holds the same message.
- **On the node that answered**: the run returns that answer as usual.

```python
from promptise.engine import BudgetHook, GraphBudgetExceededError, PromptGraphEngine

hook = BudgetHook(
    max_tokens=50000,
    max_cost_usd=0.50,
    cost_per_1k_tokens=0.002,
)
engine = PromptGraphEngine(graph=graph, model=model, hooks=[hook])
try:
    result = await engine.ainvoke({"messages": [...]})
except GraphBudgetExceededError as exc:
    print(exc.reason)          # Token budget exceeded: 50412/50000 tokens
    print(exc.pending_tools)   # ['search']

print(f"Tokens used: {hook.tokens_used}")
print(f"Cost: ${hook.cost_used:.4f}")
```

Any hook that ends a run the same way (`state.current_node = "__end__"`, e.g. `CycleDetectionHook`) in the middle of a tool loop raises `GraphStoppedError`; `reason` is the `NodeResult.error` the hook wrote, if any.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `max_tokens` | `int \| None` | `None` | Maximum tokens per run, summed over all nodes |
| `max_cost_usd` | `float \| None` | `None` | Maximum estimated cost (USD) |
| `cost_per_1k_tokens` | `float` | `0.002` | Cost per 1,000 tokens for estimation |

**Attributes after execution:**

| Attribute | Type | Description |
|-----------|------|-------------|
| `tokens_used` | `int` | Tokens of the latest run |
| `cost_used` | `float` | Estimated cost (USD) of the latest run |

## Custom Hook Examples

### Webhook Notifier

```python
class WebhookNotifier:
    def __init__(self, url: str):
        self.url = url

    async def post_node(self, node, result, state):
        import httpx
        async with httpx.AsyncClient() as client:
            await client.post(self.url, json={
                "node": node.name,
                "duration_ms": result.duration_ms,
                "tokens": result.total_tokens,
                "error": result.error,
            })
        return result
```

### Audit Logger

```python
class AuditHook:
    async def pre_tool(self, tool_name, args, state):
        if tool_name == "delete_user":
            log.warning("AUDIT: delete_user called with %s", args)
        return args

    async def post_tool(self, tool_name, result, args, state):
        audit_log.record(tool_name, args, result)
        return result
```

### Human Approval Integration

```python
class HumanApprovalHook:
    async def on_human_required(self, node_name, state):
        approval = await my_approval_system.request(
            node=node_name,
            context=state.context,
        )
        state.context["_human_approved"] = approval
```

## Observability

### ExecutionReport

Every `ainvoke()` call produces an `ExecutionReport`:

```python
result = await engine.ainvoke(input)
report = engine.last_report
print(report.summary())
```

| Field | Type | Description |
|-------|------|-------------|
| `total_iterations` | `int` | Total node executions |
| `total_tokens` | `int` | Cumulative token usage |
| `total_duration_ms` | `float` | Total wall-clock time |
| `nodes_visited` | `list[str]` | Ordered node names visited |
| `tool_calls` | `int` | Total tool calls across all nodes |
| `graph_mutations` | `int` | Number of runtime graph mutations |
| `guards_passed` | `int` | Total guards passed across all nodes |
| `guards_failed` | `int` | Total guards failed across all nodes |
| `error` | `str \| None` | Error message if a CRITICAL node failed |

### NodeResult

Every node execution produces a `NodeResult` with 30+ fields for full traceability:

| Category | Fields |
|----------|--------|
| **Identity** | `node_name`, `node_type`, `iteration` |
| **Timing** | `duration_ms`, `llm_duration_ms`, `tool_duration_ms` |
| **Tokens** | `prompt_tokens`, `completion_tokens`, `total_tokens` |
| **Tool Activity** | `tool_calls` (list of dicts), `tool_calls_deduplicated`, `tool_calls_failed` |
| **LLM Output** | `output` (parsed), `raw_output` (string), `next_node`, `transition_reason` |
| **Messages** | `messages_added` (messages appended to state) |
| **Prompt Trace** | `blocks_used`, `blocks_dropped`, `strategy_applied`, `perspective_applied` |
| **Guards** | `guards_passed`, `guards_failed`, `guard_retries` |
| **Mutations** | `graph_mutations` (list of GraphMutation) |
| **Error** | `error`, `error_recovered` |

All `NodeResult` objects are stored in `state.node_history` for full execution replay.
