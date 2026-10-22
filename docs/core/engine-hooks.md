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

    async def on_observable_event(self, event: NodeEvent, state: GraphState) -> None:
        """Called for nodes with the OBSERVABLE flag after execution."""
        pass

    async def on_human_required(self, node_name: str, state: GraphState) -> None:
        """Called for nodes with the REQUIRES_HUMAN flag."""
        pass

    async def on_error(self, node: BaseNode, error: Exception, state: GraphState) -> str | None:
        """Called when a node fails. Return a node name (or "__end__") to go
        there next, or None for the usual handling."""
        return None

    async def on_graph_mutation(self, mutation: GraphMutation, state: GraphState) -> bool:
        """Called before a graph mutation is applied. Return False to block it."""
        return True
```

Only implement the methods you need, the engine checks `hasattr()` before calling. The `Hook` protocol (`from promptise.engine import Hook`) describes them for type checking.

`on_error` gets the exception behind the failure (a `RuntimeError` with the
error text when the node recorded none, for example a `RouterNode` with no
matching route). The first hook that returns the name of a node in the graph,
or `"__end__"`, decides where the run goes next, ahead of edges and
transitions; returning `None` leaves the usual handling ([`on_error`
edges](engine-edges.md#on_error-error-routing), then the run raises
`GraphExecutionError` if it ends on the failure). Routing to `"__end__"` ends
the run on the failure, so it still raises. A `CRITICAL` node aborts the run
whatever the hooks return. Before 1.7.0 the engine never called `on_error` or
`on_graph_mutation`.

When each method runs:

| Method | When | Return value | If it raises |
|--------|------|--------------|--------------|
| `pre_node` | Before every node execution (after the node budget check, before flags) | The state to use | Logged, the run goes on |
| `post_node` | After every node execution (not after a `CRITICAL` failure), before the next node is chosen | The result to use | Logged, the run goes on |
| `pre_tool` | Before each tool call of a `PromptNode`, `ToolNode` or `CodeActionNode` program | The args to use | The call fails (see below) |
| `post_tool` | After each tool call | The result text to use | The call fails |
| `on_tool_reused` | Instead of the two above, for a reused result | ignored | Logged |
| `on_observable_event` | After an `OBSERVABLE` node runs | ignored | Logged, the run goes on |
| `on_human_required` | Before a `REQUIRES_HUMAN` node runs | ignored | Logged, the run goes on |
| `on_error` | After a node fails (it raised or its result has an `error`), before `post_node` and routing | A node name or `"__end__"` to route there; `None` for the usual handling | Logged, the next hook is asked |
| `on_graph_mutation` | Before each mutation: a node's `graph_mutations` and the LLM's `add_node` / `add_edge` actions | `False` blocks the mutation (it does not count against `max_mutations_per_run`) | The mutation is blocked (fail closed) |

A hook can end a run by setting `state.current_node = "__end__"` in `pre_node`
or `post_node`. The node being processed still finishes (from `pre_node`, it
still runs once), then the run ends; mid tool loop that raises
`GraphStoppedError`. `post_node` runs before `NodeResult.duration_ms` is final
for nodes that do not set it themselves, and before the engine adds the
node's tokens to `state.total_tokens`.

## Usage

```python
import logging

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

Logs on the `promptise.engine.hooks` logger, so enable that logger (or its parent `promptise`) at the chosen level to see the lines.

**Log output format:**

```
▶ Node 'plan' (iter=0, type=PlanNode)
◀ Node 'plan' → (resolve) (150ms, tokens=340)
◀ Node 'act' → (resolve) (900ms, tokens=1210, tools=2)
```

The arrow shows `NodeResult.next_node` when the node set one, else `(resolve)`
(the engine picks the next node after the hooks). `tools=` and `error=` appear
only when there are tool calls or an error.

### TimingHook

Enforces per-node time budgets. It measures from `pre_node` to `post_node` (the node's whole execution, tool calls included). If a node took longer than its budget, the hook logs a warning and, when the result has no error yet, sets `result.error = "Time budget exceeded: 12034ms > 10000ms"`. It does not interrupt the slow node, and it does not fail the run: the error is a note that an [`on_error` edge](engine-edges.md#on_error-error-routing), `SKIP_ON_ERROR` on the next node and `MetricsHook` (when it runs after `TimingHook`) all see. For a hard stop, set timeouts on the model and tools.

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

Example: If visited nodes are `[A, B, C, A, B, C, A, B, C]`, the pattern `[A, B, C]` has repeated 3 times, the hook sets `state.current_node = "__end__"`. It checks in `pre_node`, where `visited` already includes the node about to run; the engine respects a `pre_node` stop, so that node does not run (it is not in `ExecutionReport.nodes_visited`).

A node that the engine re-enters for its own tool loop (it called tools and runs again with their results) counts as one step, so a ReAct `reason` node making many tool calls is not a cycle; bound those with `max_iterations` / `max_agent_iterations`. A node that routes back to itself without calling tools still counts every visit. Before 1.7.0 the stopped node ran once more, and a single-node tool loop was ended after 9 model calls with `GraphStoppedError`.

### MetricsHook

Collects per-node execution metrics in its `metrics` dict. Access them via `summary()` (a copy) after the run. Metrics add up across runs until you call `reset()`; with concurrent runs on one engine, they mix.

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

Every run (`ainvoke()`, `astream()`, `astream_events()`) produces an `ExecutionReport`:

```python
result = await engine.ainvoke({"messages": [{"role": "user", "content": "Summarize Q3"}]})
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
| `error` | `str \| None` | Why the run failed or stopped: the error of the node failure the run ended on (any unrecovered failure, not only `CRITICAL`), or the iteration-limit / hook-stop message of a run stopped mid tool loop. `None` for a run that answered |

`summary()` returns the report as readable text. The report is also on every
engine exception as `exc.report`. `tool_calls` counts calls recorded as
observations (`PromptNode`, `ToolNode`, `CodeActionNode` programs, and the
children of a `ParallelNode`), failed and reused calls included.

### NodeResult

Every node execution produces a `NodeResult` (27 fields, all with defaults) for full traceability:

| Category | Fields |
|----------|--------|
| **Identity** | `node_name`, `node_type`, `iteration` |
| **Timing** | `duration_ms`, `llm_duration_ms`, `tool_duration_ms` |
| **Tokens** | `prompt_tokens`, `completion_tokens`, `total_tokens` |
| **Tool Activity** | `tool_calls` (list of dicts), `tool_calls_deduplicated`, `tool_calls_failed` |
| **LLM Output** | `output` (the parsed object, or the reply text), `raw_output` (string), `next_node`, `transition_reason` |
| **Messages** | `messages_added` (messages appended to state) |
| **Prompt Trace** | `blocks_used`, `blocks_dropped`, `strategy_applied`, `perspective_applied` |
| **Guards** | `guards_passed`, `guards_failed`, `guard_retries` (not set by the built-in nodes; stays 0) |
| **Mutations** | `graph_mutations` (list of GraphMutation) |
| **Error** | `error`, `error_recovered` |

All `NodeResult` objects are stored in `state.node_history` for full execution replay. `tool_calls` entries are dicts with `name`, `args`, `result`, `success` (and `reused: True` for a reused result). `error_recovered=True`, set by a hook, makes the engine treat a failed node as recovered.
