# Hooks & Observability

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
        """Called before each tool call inside PromptNode. Can modify args."""
        return args

    async def post_tool(self, tool_name: str, result: str, args: dict, state: GraphState) -> str:
        """Called after each tool call. Can modify result string."""
        return result

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

Enforces a total token and/or cost budget across the entire graph run. When exceeded, forces the graph to end immediately.

```python
hook = BudgetHook(
    max_tokens=50000,
    max_cost_usd=0.50,
    cost_per_1k_tokens=0.002,
)
engine = PromptGraphEngine(graph=graph, model=model, hooks=[hook])
await engine.ainvoke({"messages": [...]})

print(f"Tokens used: {hook.tokens_used}")
print(f"Cost: ${hook.cost_used:.4f}")
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `max_tokens` | `int \| None` | `None` | Maximum total tokens across all nodes |
| `max_cost_usd` | `float \| None` | `None` | Maximum estimated cost (USD) |
| `cost_per_1k_tokens` | `float` | `0.002` | Cost per 1,000 tokens for estimation |

**Attributes after execution:**

| Attribute | Type | Description |
|-----------|------|-------------|
| `tokens_used` | `int` | Total tokens consumed |
| `cost_used` | `float` | Estimated cost (USD) |

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
