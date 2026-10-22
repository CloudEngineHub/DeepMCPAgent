---
seo_title: "Reasoning Graph Nodes: Built-in and Custom"
description: "The 21 built-in reasoning graph node types, from ThinkNode and PlanNode to ToolNode, RouterNode, AutonomousNode and CodeActionNode, and how to write custom nodes."
---

# Reasoning Graph Nodes

Every node in a Reasoning Graph is a complete processing pipeline. Promptise ships with **21 built-in node types**, plus three ways to create custom ones.

## Reasoning Nodes (Pre-built Building Bricks)

These come configured with instructions, context injection settings and default flags. All reasoning nodes inherit from `PromptNode` (except RetryNode and FanOutNode which inherit from `BaseNode`), so every [`PromptNode` parameter](#promptnode) works on them and overrides the preset (the presets are defaults, set with `setdefault`).

!!! warning "Give a reasoning node an `output_schema` when it should route or update state"
    A reasoning node's instructions ask the model for fields such as `route`,
    `mistake`, `subgoals`, `severity` or `passes`, but the node only reads
    them from a **structured** output. Without an `output_schema` the model
    answers in plain text, `NodeResult.output` stays `None`, and the extra
    behaviour listed for each node below (storing reflections, updating
    `state.plan`, severity or pass/fail routing, writing `output_key`) does
    not happen. The text still lands in `state.context["{name}_output"]` and
    `NodeResult.raw_output`. The prebuilt patterns pass schemas such as
    `PeoatrPlan` and `PeoatrReflection` (in `promptise.engine.prebuilts`) for
    this reason:

    ```python
    from promptise.engine import ReflectNode
    from promptise.engine.prebuilts import PeoatrReflection

    reflect = ReflectNode(
        "reflect",
        output_schema=PeoatrReflection,   # mistake, correction, confidence, route
        transitions={"answer": "answer", "replan": "plan", "continue": "act"},
    )
    ```

### ThinkNode

Gap analysis and next-step reasoning. No tools, pure analytical reasoning.

```python
ThinkNode("think")
ThinkNode("think", focus_areas=["data quality", "missing sources"])
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | `"think"` | Node identifier |
| `focus_areas` | `list[str] \| None` | `None` | Optional areas to focus analysis on |
| `**kwargs` | | | All PromptNode parameters |

**Pre-configured behavior:**

- Instructions: analyze the state, name the gaps, rate confidence (1-5), recommend the next step; `focus_areas` are appended as "Focus on: ..."
- Injects observations and plan (`include_observations=True`, `include_plan=True`, `include_reflections=False`); no tools
- Output key: `think_output`
- Default flags: `READONLY`, `LIGHTWEIGHT` (`LIGHTWEIGHT` only switches model when the engine has a `lightweight_model`; `build_agent` sets none)

### ReflectNode

Self-evaluation and mistake correction. Auto-stores reflections in state.

```python
ReflectNode("reflect")
ReflectNode("reflect", review_depth=5)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | `"reflect"` | Node identifier |
| `review_depth` | `int` | `3` | Stored (and saved by `save_graph`); 1.6.0 does not use it in the prompt |
| `**kwargs` | | | All PromptNode parameters |

**Pre-configured behavior:**

- Instructions: assess progress, name mistakes and corrections, rate confidence, pick a route: `continue`, `replan` or `answer` (the route is followed through the node's `transitions`)
- With a structured output that has a non-empty `mistake`, stores `{iteration, mistake, correction, confidence, stage: "reflect"}` in `state.reflections` (the last 5 are kept)
- Injects observations, plan and past reflections
- Output key: `reflection`
- Default flags: `STATEFUL`, `OBSERVABLE`

### ObserveNode

Interprets tool results into structured data. Enriches state with extracted entities and facts.

```python
ObserveNode("observe")
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | `"observe"` | Node identifier |
| `**kwargs` | | | All PromptNode parameters |

**Pre-configured behavior:**

- Instructions: summarize the tool results, extract entities, factual claims and key findings
- With a structured output, appends its `entities` list to `state.context["extracted_entities"]` and its `facts` list to `state.context["extracted_facts"]` (values that are not lists are ignored)
- Injects observations only
- Output key: `observation`
- Default flags: `STATEFUL`

### PlanNode

Structured planning with subgoal management and quality self-evaluation.

```python
PlanNode("plan", max_subgoals=4, quality_threshold=3)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | `"plan"` | Node identifier |
| `max_subgoals` | `int` | `4` | Maximum number of subgoals |
| `quality_threshold` | `int` | `3` | Plans below this quality score (1-5) trigger re-planning |
| `**kwargs` | | | All PromptNode parameters |

**Pre-configured behavior:**

- Instructions: break the task into at most `max_subgoals` subgoals, pick the first, rate the plan 1-5
- With a structured output, sets `state.plan` to its `subgoals` list (cut to `max_subgoals`). `state.completed` is not touched; mark progress with `state.complete_subgoal()` in your own node
- Reads `quality_score` (or `quality`, default 3): below `quality_threshold` it follows a `replan` transition, else runs itself again. Give the node a small `max_iterations` and an `"error"` transition so weak plans cannot loop (the PEOATR pattern uses `max_iterations=3`, `transitions={"error": "act"}`)
- Injects past reflections only
- Output key: `plan_output`
- Default flags: `STATEFUL`, `OBSERVABLE`

### SynthesizeNode

Combines all gathered data into a comprehensive final answer.

```python
SynthesizeNode("answer", is_terminal=True)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | `"synthesize"` | Node identifier |
| `**kwargs` | | | All PromptNode parameters |

**Pre-configured behavior:**

- Injects observations (the last 5), reflections and plan progress
- Instructions: answer the original question, support claims with evidence, cite sources
- `default_next="__end__"`, so the run ends after it unless an edge or transition says otherwise. `is_terminal=True` only matters to an `AutonomousNode` pool
- Output key: `synthesis`
- Default flags: `OBSERVABLE`

### CritiqueNode

Adversarial self-review with severity-based routing.

```python
CritiqueNode("critique", severity_threshold=0.5)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | `"critique"` | Node identifier |
| `severity_threshold` | `float` | `0.5` | Severity above this routes to revision (0.0 = perfect, 1.0 = flawed) |
| `**kwargs` | | | All PromptNode parameters |

**Pre-configured behavior:**

- Instructions: name weaknesses, counter-arguments and improvements, rate severity from 0.0 to 1.0
- With a structured output whose `severity` is above `severity_threshold`, routes to the first of these transitions the node has: `revise`, `improve`, `retry`, `replan`. Otherwise the normal [transition resolution](engine-edges.md#transition-resolution) applies
- Output key: `critique`
- Default flags: `READONLY`, `OBSERVABLE`

### JustifyNode

Chain-of-thought justification for audit trail.

```python
JustifyNode("justify")
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | `"justify"` | Node identifier |
| `**kwargs` | | | All PromptNode parameters |

**Pre-configured behavior:**

- Instructions: explain why the last action was taken, step by step, with evidence, a conclusion and a confidence
- With a structured output, appends `{iteration, reasoning, conclusion, confidence}` (from `reasoning_chain`, `conclusion`, `confidence`) to `state.context["justifications"]` (the last 5 are kept)
- Output key: `justification`
- Default flags: `READONLY`, `OBSERVABLE`, `VERBOSE`

### ValidateNode

LLM-powered quality validation with pass/fail routing.

```python
ValidateNode("validate",
    criteria=["Be accurate", "Cite sources"],
    on_pass="deliver",
    on_fail="revise",
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | `"validate"` | Node identifier |
| `criteria` | `list[str] \| None` | `None` | Validation criteria (defaults to accuracy, evidence, no factual errors) |
| `on_pass` | `str` | `"__end__"` | Node to transition to on pass |
| `on_fail` | `str \| None` | `None` | Node to transition to on fail |
| `**kwargs` | | | All PromptNode parameters |

**Pre-configured behavior:**

- Instructions: check the current output against the criteria, list issues and suggestions
- With a structured output, reads `passes` (or `pass`): true routes to `on_pass`; false routes to `on_fail` when set, else the normal transition resolution applies
- Output key: `validation`
- Default flags: `READONLY`, `VALIDATE_OUTPUT` (the flag checks the output against `output_schema`, so it does nothing without one)

```python
from pydantic import BaseModel

from promptise.engine import ValidateNode


class Verdict(BaseModel):
    passes: bool
    issues: list[str]


ValidateNode("validate", criteria=["Cites a source"], output_schema=Verdict,
             on_pass="deliver", on_fail="revise", max_iterations=3)
```

### RetryNode

Wraps another node with retry logic and error enrichment. Extends BaseNode (not PromptNode).

```python
RetryNode("safe_search", wrapped_node=search_node, max_retries=3, backoff_factor=0.5)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | *required* | Node identifier |
| `wrapped_node` | `BaseNode` | *required* | The node to wrap with retry logic |
| `max_retries` | `int` | `3` | Retries after the first attempt, so up to `max_retries + 1` attempts |
| `backoff_factor` | `float` | `0.5` | Wait before retry *n* (0-based): `min(backoff_factor * 2**n, 8)` seconds |
| `**kwargs` | | | All BaseNode parameters (`description` is set for you) |

**Behavior:**

- Runs `wrapped_node.execute()`; a result with an `error` or a raised exception counts as a failure
- Between retries, sets `state.context["_retry_attempt"]`, `["_retry_error"]` and `["_retry_node"]` so the wrapped node can adapt
- A success comes back under the `RetryNode`'s name; after the last failure the result's `error` is `"All N attempts failed. Last error: ..."`
- Default flags: `RETRYABLE`

!!! warning "The engine retries a `RetryNode` again"
    In 1.6.0 the `RETRYABLE` flag that `RetryNode` adds makes the engine
    [retry the node itself](engine-flags.md#execution-control), up to its
    `max_iterations` (default 10) attempts. The attempts multiply: with the
    defaults a failing wrapped node runs up to 40 times. Pass a small
    `max_iterations` (for example `RetryNode(..., max_iterations=1)`), or
    remove the flag (`retry_node.flags.discard(NodeFlag.RETRYABLE)`) when
    the `RetryNode`'s own retries are enough.

### FanOutNode

Sends different sub-questions to parallel children with state overrides. Extends BaseNode.

```python
FanOutNode("gather", branches=[
    (PromptNode("web", tools=[web_search]), {"focus": "news"}),
    (PromptNode("docs", tools=[doc_search]), {"focus": "technical"}),
])
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | *required* | Node identifier |
| `branches` | `list[tuple[BaseNode, dict]]` | `None` | List of (node, state_overrides) tuples |
| `merge_strategy` | `str` | `"dict"` | Stored and saved; 1.6.0 always merges into a dict |
| `**kwargs` | | | All BaseNode parameters (`description` is set for you) |

**Behavior:**

- Each branch gets its own `GraphState` with a copy of the messages and observations and `{**state.context, **overrides}` as context; changes a branch makes to its state are not copied back
- All branches run concurrently via `asyncio.gather`; each runs `execute()` once, so a branch that calls tools runs them once and does not see their results (there is no tool loop inside a fan-out)
- `output` is `{branch_name: branch_output}`; a branch that raised gets `{"error": "..."}` there instead, and the node itself does not fail
- Tool calls and tokens of the branches are added to the node's result
- Default flags: `PARALLEL_SAFE`

---

## Standard Nodes

### PromptNode

The core LLM reasoning node. Full prompt-assembly pipeline with tool calling, guards, strategies, and context management.

```python
from pydantic import BaseModel

from promptise.prompts import chain_of_thought
from promptise.prompts.blocks import Identity, Rules
from promptise.prompts.guards import SchemaStrictGuard


class AnalysisOutput(BaseModel):
    summary: str
    complete: bool


PromptNode("analyze",
    instructions="Analyze the data.",
    blocks=[Identity("Analyst"), Rules(["Cite sources"])],
    strategy=chain_of_thought,
    tools=my_tools,
    inject_tools=True,
    output_key="analysis",
    input_keys=["raw_data"],
    inherit_context_from="search",
    preprocessor=enrich_fn,
    postprocessor=format_fn,
    guards=[SchemaStrictGuard()],
    output_schema=AnalysisOutput,
    transitions={"complete": "report", "need_data": "search"},
    model_override="openai:gpt-5-mini",
)
```

A minimal node you can run on its own, through `PromptGraphEngine`:

```python
from pydantic import BaseModel

from promptise import resolve_model
from promptise.engine import PromptGraph, PromptGraphEngine, PromptNode


class Sentiment(BaseModel):
    label: str
    confidence: float


class PrintOutput:
    async def post_node(self, node, result, state):
        print(result.output)                # {'label': 'positive', 'confidence': 0.95}
        print(state.context["sentiment"])   # the same dict, under output_key
        return result


graph = PromptGraph("classify", nodes=[
    PromptNode("classify", instructions="Classify the sentiment of the message.",
               output_schema=Sentiment, output_key="sentiment",
               is_entry=True, default_next="__end__"),
])
engine = PromptGraphEngine(graph, resolve_model("openai:gpt-5-mini"), hooks=[PrintOutput()])
await engine.ainvoke({"messages": [{"role": "user", "content": "I love this library!"}]})
```

A node with an `output_schema` adds no message to the conversation (the model's
reply is a parsed object, not an `AIMessage`). End a graph that an agent runs on
a node that writes text, so the reply is an answer.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | *required* | Node identifier |
| `instructions` | `str` | `""` | Natural-language instructions injected into the system prompt |
| `blocks` | `list[Any]` | `None` | PromptBlock objects (Identity, Rules, OutputFormat, etc.) appended to system prompt |
| `strategy` | `Any` | `None` | A strategy such as `chain_of_thought` or `self_critique` (`promptise.prompts`): its `wrap()` rewrites the system prompt, its `parse()` extracts the answer from the reply (when the text changes, `NodeResult.output` is the extracted answer) |
| `perspective` | `Any` | `None` | A perspective such as `analyst` or `critic` (`promptise.prompts`): its `apply()` reframes the system prompt; an object with a `framing` string is put first instead |
| `tools` | `list[BaseTool]` | `None` | Tools bound to the model for this node (`@tool` functions, MCP tools) |
| `tool_choice` | `str` | `"auto"` | Stored and saved; 1.6.0 binds tools without a `tool_choice`, so the provider default (`"auto"`) applies |
| `inject_tools` | `bool` | `False` | If True, receives the tools `build_agent()` discovered (or `PromptGraphEngine(tools=...)`) at runtime, merged with `tools` |
| `tool_summary` | `str` | `None` | An "Available tools" block in the system prompt, on top of the tool definitions the model receives anyway: `"off"`, `"names"` (one line of names) or `"full"` (parameters and the first 120 characters of each description). `None` uses the engine's setting (`PromptGraphEngine(tool_summary=...)` / `build_agent(tool_summary=...)`), which is `"off"`, see [The tool summary](tool-optimization.md#the-tool-summary-in-the-system-prompt) |
| `output_schema` | `type` | `None` | Pydantic model or `TypedDict` for structured output via `with_structured_output()`. The output is stored as a plain dict (a Pydantic model is dumped), so transitions, edge conditions, `route` and `output_key` read it the same way |
| `guards` | `list[Any]` | `None` | Guards with a `check_output(text)` method (`ContentFilterGuard`, `LengthGuard`, ... in `promptise.prompts.guards`), run on the reply text. A guard that raises is recorded in `NodeResult.guards_failed` (an [`on_guard_fail`](engine-edges.md#on_guard_fail-guard-failure-routing) edge can route on it); the node does not retry |
| `model_override` | `Any` | `None` | Per-node model: a chat model, a `Model`, or a model id string such as `"openai:gpt-5-mini"` (resolved like `build_agent(model=...)`) |
| `context_layers` | `dict[str, int]` | `None` | Names of `state.context` keys to add to the system prompt as `name:\nvalue` blocks; the priority values are not used in 1.6.0 |
| `max_tokens` | `int` | `4096` | Stored and saved; 1.6.0 does not pass it to the model. Set limits on the model (`Model(...)` or the chat model) instead |
| `temperature` | `float` | `0.0` | Stored and saved; 1.6.0 does not pass it to the model. Set it on the model instead |
| `input_keys` | `list[str]` | `None` | Keys from `state.context` added to the system prompt as "Input data" (`key: value`; missing keys are skipped) |
| `output_key` | `str` | `None` | Write `NodeResult.output` to `state.context[output_key]`. Only a parsed output is written (an `output_schema` result, a strategy's extracted answer, or a postprocessor's return value); a plain-text reply leaves `output` as `None`, so use `state.context["{name}_output"]` or `inherit_context_from` for text |
| `inherit_context_from` | `str` | `None` | Inject another node's output into the prompt: reads `state.context["{name}_output"]`, which every `PromptNode` writes (its reply text; for an `output_schema` node, the output as JSON text) |
| `context_scope` | `str` | `"full"` | Context lifecycle mode: `"auto"` (full→ledger automatically; the ReAct default), `"full"` (whole transcript), `"scoped"` (bounded working set), or `"ledger"` (deduplicated facts ledger), see [Context scope](#context-scope) |
| `auto_ledger_after` | `int` | `None` | For `context_scope="auto"`: number of accumulated tool results after which the node switches from the full transcript to the compacted view (default 6, or the agent's `context_compaction` setting) |
| `compaction` | `ContextCompaction \| bool \| int` | `None` | This node's [compaction settings](../guides/context-lifecycle.md#tune-it-or-turn-it-off); wins over `build_agent(context_compaction=...)`. `False` keeps an `"auto"` node on the full transcript |
| `preprocessor` | `Callable` | `None` | Runs before the LLM call: `fn(state, config) -> None`, sync or async. An exception is logged as a warning and the node goes on |
| `postprocessor` | `Callable` | `None` | Runs after: `fn(output, state, config) -> Any`, sync or async. It receives `NodeResult.output` (`None` for a plain-text reply); a non-`None` return value replaces the output. An exception is logged and ignored |
| `include_observations` | `bool` | `True` | Auto-inject recent tool results from `state.observations` |
| `include_plan` | `bool` | `True` | Auto-inject current plan/subgoals from `state.plan` |
| `include_reflections` | `bool` | `True` | Auto-inject past learnings from `state.reflections` |
| `transitions` | `dict[str, str]` | `None` | Map output keys to next-node names (e.g. `{"proceed": "act"}`). With two or more keys (other than `"error"`) on a node without tools, the prompt lists them and asks for a `route` field; see [Dynamic LLM routing](engine-edges.md#dynamic-llm-routing) |
| `default_next` | `str` | `None` | Fallback node if no transition matches |
| `max_iterations` | `int` | `10` | Max times this node can execute in one graph run; each tool-loop round counts. The engine's `max_node_iterations` (25) caps it. Once used up, the node is not run again: the engine follows its `"error"` transition, else an `__error__` node, else ends the run. A run ended this way in the middle of a tool loop (the model never saw its last tool results) raises `GraphIterationLimitError` |
| `flags` | `set[NodeFlag]` | `None` | Typed flags controlling engine behavior |
| `is_entry` | `bool` | `False` | Shorthand for adding `NodeFlag.ENTRY` |
| `is_terminal` | `bool` | `False` | Shorthand for adding `NodeFlag.TERMINAL` |

**Execution pipeline.** One execution of a `PromptNode` is one model call:

1. Model: `model_override` if set, else the engine's model (the `lightweight_model` for a `LIGHTWEIGHT` node, when the engine has one)
2. Preprocessor
3. System prompt assembly, in this order: `instructions`, the optional tool summary, `input_keys` ("Input data"), `inherit_context_from`, `context_layers`, `blocks`, recent observations (skipped on a node with tools: the results are already in the conversation), plan, past reflections, and the list of route choices
4. Strategy `wrap()`, then perspective
5. Messages: the conversation with the node's system prompt. When the conversation starts with the agent's prompt, the two are merged into one system message; otherwise the node's prompt is inserted after the first system message (or first)
6. Tool binding (`bind_tools()`) and structured output (`with_structured_output()`); the binding is reused on the next execution of the node in the same run while the model, tool set and schema are unchanged
7. Context scope: `"scoped"` / `"ledger"` (and `"auto"` past its threshold) replace the transcript with the compacted view
8. The model call (`astream()` when the run is streamed and the node has no `output_schema`, else `ainvoke()`)
9. Tool calls: all calls of the reply run (in parallel when there are two or more), their `ToolMessage`s are appended, and the engine runs the node again so the model sees the results
10. Transitions read from a structured output, then guards, strategy `parse()`, postprocessor
11. `state.context[output_key]` (parsed output only) and `state.context["{name}_output"]`

The system prompt is rebuilt on every execution, because `input_keys`, the
plan, reflections, observations and the offered tools can change between
steps. Tool definitions reach the model through `bind_tools()`; the
"Available tools" block in the prompt is off unless `tool_summary` asks for it.

#### Context scope

`context_scope` controls **how much accumulated history the node sees on each LLM
call**, the single most important lever for context-lifecycle management on long
or multi-stage tasks. The full transcript grows on every tool call; left
unbounded it inflates token cost super-linearly and the model starts losing track
of facts it already gathered.

The **default ReAct pattern uses `"auto"`**, context handling is automatic, with
no pattern to choose. The four modes:

| Mode | What the node sees | Use it for |
|------|--------------------|------------|
| `"auto"` *(ReAct default)* | `"full"` while the tool loop is short, then `"ledger"` once it grows past `auto_ledger_after` (default 6) tool results | The smart default, simple tasks unchanged, deep tool loops bounded automatically |
| `"full"` | The whole accumulated transcript | When every prior message must always be visible |
| `"scoped"` | The node's system prompt (carrying any inherited/injected distilled state) + the input's system messages + the **current** user question + **only this node's own in-progress tool loop** | Multi-stage reasoning graphs, drops the verbose intermediate messages produced by *other* stages so tokens don't grow across stages |
| `"ledger"` | System prompt + the input's system messages + a short note on earlier conversation + the **current** user question + the **most recent** assistant turn and its tool results + a compact **deduplicated "facts gathered" ledger** of older results | Long single-node tool loops over an interconnected dataset, where the model otherwise re-queries the same facts dozens of times |

```python
# Default agents already get this: no configuration needed:
PromptNode("reason", inject_tools=True, context_scope="auto", auto_ledger_after=6)

# Multi-stage graph: each stage only sees its own working set.
PromptNode("analyze", instructions="...", context_scope="scoped")

# Force the ledger from turn 0 (deep tool loop you know will be long):
PromptNode("reason", inject_tools=True, context_scope="ledger")
```

!!! note "Honest scope of `auto`"
    `auto` is an **efficiency / context-bounding** default: it keeps tokens and
    context manageable on deep loops at **zero change to short tasks** (below the
    threshold it is byte-for-byte `"full"`). It does **not** by itself make the
    model aggregate gathered facts more accurately; for that, change the action
    space with [code-action](engine-prebuilts.md#code-action-one-sandboxed-program).

**How `"ledger"` works:**

- The input's system messages (yours and runtime-injected ones such as `[Context State]`) and the **current** user question are always sent. Dict messages (`{"role": "user", ...}`) are converted to LangChain messages first, so every input form works; with chat history, the question is the *last* user message and earlier turns become a short note.
- The ledger is built from the tool calls in the transcript older than the latest exchange, one line per `tool(args) = result`, **last value wins** per `(tool, args)` so duplicates collapse. Results longer than `keep_result_chars` (2,000) are cut to an excerpt that says how much was cut and names the call; a call the model made more than once is shown in full (it fetched it again for the full text), and is the last to shrink under `max_tokens`; results shown in the latest exchange are not repeated.
- It is placed **last**, immediately before the model's turn, where it is most salient, so the model consults it instead of re-calling a tool.
- The most recent assistant turn and its tool results (a whole parallel batch) are kept *in-flow* so the model doesn't lose continuity and loop.
- Tool execution is unchanged: a repeated `(tool, args)` call **runs the tool again**, because its result may have changed (a job's status, a health check). The ledger, not a cache, is what keeps the model from re-fetching facts it already has. For tools whose result cannot change during a run, `ContextCompaction(reuse_results=[...])` answers a repeated call from the latest earlier **successful** result instead of running the tool; such a call is reported to `trace_tools`, the `observer`, callbacks, hooks (`on_tool_reused`) and stream events, marked as reused, and has `"reused": True` in `NodeResult.tool_calls`. See [Repeated calls run the tool](../guides/context-lifecycle.md#repeated-calls-run-the-tool).

This is the mechanism behind the [`managed` prebuilt pattern](engine-prebuilts.md#managed-context-managed-tool-loop). It is an **efficiency primitive**, it bounds token growth and tells the model not to re-fetch what it has; how many calls that saves depends on the model (see [the note on `managed`](engine-prebuilts.md#managed-context-managed-tool-loop)).

### ToolNode

Explicit tool execution with input validation and deduplication.

```python
ToolNode("search",
    tools=[search_tool, wiki_tool],
    validate_inputs=True,
    deduplicate=True,
    max_result_chars=4000,
    tool_selector=my_selector_fn,
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | *required* | Node identifier |
| `tools` | `list[BaseTool]` | `None` | Available tools |
| `validate_inputs` | `bool` | `True` | Check each call's arguments against the tool's parameter schema before calling it; invalid arguments do not reach the tool (see below) |
| `deduplicate` | `bool` | `True` | Run identical `(tool, args)` calls of one model turn once (see below) |
| `max_result_chars` | `int` | `4000` | Truncate successful tool results to this length |
| `tool_selector` | `Callable` | `None` | Custom function `(state) -> (tool_name, args)` to pick which tool to call. Without one, the node runs the tool calls of the most recent assistant message that has tool calls |
| `**kwargs` | | | All BaseNode parameters |

**When to use it.** A `PromptNode` runs the tool calls its model asks for
itself, so a `ToolNode` placed after one finds those calls already answered
and runs nothing. Use a `ToolNode` with a `tool_selector` for a call your code
decides (no model turn), or after a custom node that appends an `AIMessage`
with tool calls. A `ToolNode` uses only its own `tools`; it does not receive
injected tools.

```python
from promptise import tool
from promptise.engine import PromptGraph, PromptGraphEngine, ToolNode


@tool
def get_weather(city: str) -> str:
    """Current weather for a city."""
    return f"Sunny in {city}"


graph = PromptGraph("lookup", nodes=[
    ToolNode("weather", tools=[get_weather], is_entry=True, default_next="__end__",
             tool_selector=lambda state: ("get_weather", {"city": state.context.get("city", "Berlin")})),
])
engine = PromptGraphEngine(graph, model=None)    # no model call
result = await engine.ainvoke({"messages": []})
print(result["messages"][-1].content)            # Sunny in Berlin
```

**A call runs as it does in a `PromptNode`.** Whichever node runs a tool, the
call goes through the same steps and is reported the same way:

- The engine hooks' [`pre_tool`](engine-hooks.md#tool-hooks) run first and may
  change the arguments; a `pre_tool` hook that raises blocks the call. Then the
  tool runs, then `post_tool` hooks may change the result.
- The tool is invoked as a tool call, so a tool with an
  `InjectedToolCallId` argument gets its id, and a tool that reports an error
  counts as failed: the model gets an MCP server's own error envelope
  (`{"error": {"code": "TOOL_ERROR", "message": ...}}`) in a `ToolMessage` with
  `status="error"`. A tool that raises gives `"Error: <type>: <message>"`.
  Each `ToolMessage` carries the tool's `name`.
- Each call streams: `astream_events()` yields `on_tool_start` and then
  `on_tool_end` (`status`, `duration_ms`, `reused`) or `on_tool_error`, and
  `astream_with_tools()` turns them into `ToolStartEvent` / `ToolEndEvent` with
  the call's `tool_index`, `duration_ms`, `success` and `reused`
  ([Streaming](streaming.md#what-a-streamed-run-does)). A `ToolNode` runs one
  turn's calls one after another, in order, so each call's `tool_end` comes
  before the next call's `tool_start`.

**What `validate_inputs=True` checks.** After the `pre_tool` hooks, the
arguments are validated against the schema the model is offered for the tool
(its `args_schema` without injected arguments). As for MCP tools, an explicit
`null` for an optional parameter that cannot be null counts as not given and is
left out. When the arguments do not match, the tool does not run; the call's
`ToolMessage` (`status="error"`) is the same message a
`promptise.tools.ToolArgumentError` gives, which lists each problem by argument
path and tells the model to correct the call:

```text
Invalid arguments for tool 'create_order':
- qty: Input should be a valid integer, unable to parse string as an integer (got "two")
Correct the arguments and call the tool again.
```

The call counts as failed (`tool_calls_failed`, `success: false` in the
stream), `post_tool` hooks see it, and callbacks get its start and an
`on_tool_error` with the `ToolArgumentError` (code `INVALID_ARGUMENTS`). An MCP
tool (also behind an approval gate or guardrails) checks its own arguments the
same way and gives the same message, so the node leaves the check to it, and
`trace_tools` and the `observer` show the rejected call as for any MCP call. A
tool whose schema is a JSON-schema dict rather than a pydantic model is not
checked by the node; it validates its own input when it runs. With
`validate_inputs=False` the arguments go to the tool as they are.

**What `deduplicate=True` covers.** Only the calls of **one model turn** (the
tool calls of one assistant message). When the model asks for the same tool
with the same arguments twice in one turn, the tool runs once and each repeat
gets its own `ToolMessage` with that result, so every `tool_call_id` has a
result. The repeat is reported as reused, like a call answered by
[`ContextCompaction(reuse_results=...)`](../guides/context-lifecycle.md#repeated-calls-run-the-tool):
`trace_tools` prints `↺ Reusing an earlier result for tool: ...`, the
`observer` records it with `reused: true`, LangChain callbacks get its start and
end with `metadata["promptise_reused"]`, engine hooks get `on_tool_reused`
(instead of `pre_tool` / `post_tool`), stream events have `reused: true`, and
`NodeResult.tool_calls` has it with `"reused": True` (`tool_calls_deduplicated`
counts it). The repeat gets the first call's result as the model saw it, after
`post_tool` hooks. A failed call, including one a `pre_tool` hook blocked or
one with invalid arguments, is never reused.

A call in a **later** model turn, a later pass of a `tool_selector`, or another
run always runs the tool, so a status or health check returns the current
value. The record of a turn's calls belongs to the run, not to the node: runs
of the same graph, one after another or concurrent, never see each other's
calls. If the graph reaches the node again before the model's next turn, calls
that already have a result are not run again.

### RouterNode

LLM decides which path to take from a set of named routes.

```python
from promptise import resolve_model
from promptise.engine import RouterNode
from promptise.prompts.blocks import Rules

RouterNode("route",
    routes={"search": "search_node", "answer": "synthesize", "clarify": "ask_user"},
    model_override=resolve_model("openai:gpt-5-mini"),
    context_blocks=[Rules(["Choose the most relevant path"])],
    default_next="synthesize",
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | *required* | Node identifier |
| `routes` | `dict[str, str]` | `None` | Map of route labels to target node names |
| `context_blocks` | `list[Any]` | `None` | Blocks rendered into the routing prompt (after `instructions`) |
| `model_override` | `BaseChatModel` | `None` | A chat model object for routing. Unlike `PromptNode`, a model id string is not resolved here (the call fails); pass `resolve_model("...")` |
| `**kwargs` | | | All BaseNode parameters |

**How it routes.** The node sends the conversation plus one system message
("Choose ONE of these routes: [...] Respond with ONLY the route name"), takes
the reply in lower case and picks the first route label (in `routes` order)
that appears in it, as a substring. No match goes to `default_next`; with no
`default_next` the node fails (`"No route matched ..."`). Keep labels distinct
(`"search"` also matches a reply of `"research"`). It makes one model call and
binds no tools.

### GuardNode

Programmatic validation and gating with pass/fail routing.

```python
from promptise.prompts.guards import ContentFilterGuard, LengthGuard

GuardNode("check_quality",
    guards=[LengthGuard(min_length=100), ContentFilterGuard(blocked=["todo"])],
    target_key="draft",
    on_pass="publish",
    on_fail="revise",
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | *required* | Node identifier |
| `guards` | `list[Any]` | `None` | Objects with an async `check_output(value)` that raises to fail, or plain callables `fn(value) -> bool` (sync or async; falsy fails) |
| `target_key` | `str` | `None` | Key in `state.context` to check. `None` checks the content of the last message that has content |
| `on_pass` | `str` | `"__end__"` | Next node when every guard passes (also when there are no guards) |
| `on_fail` | `str \| None` | `None` | Next node when any guard fails; `None` falls back to `default_next`, then to the graph's edges |
| `**kwargs` | | | All BaseNode parameters |

No model call. Every guard runs (a failure does not stop the others); the
results land in `NodeResult.guards_passed` / `guards_failed`. A failed guard is
routing, not an error: the run does not raise. A `check_output` that returns
`False` without raising counts as a pass.

### ParallelNode

Run multiple child nodes concurrently and merge results.

```python
ParallelNode("parallel_search",
    nodes=[search_node, wiki_node, docs_node],
    merge_strategy="concatenate",
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | *required* | Node identifier |
| `nodes` | `list[BaseNode]` | `None` | Child nodes to run concurrently |
| `merge_strategy` | `str` | `"concatenate"` | `"custom"` passes the outputs to `merge_fn`; any other value (`"concatenate"`, `"dict"`) gives `{child_name: child_output}` |
| `merge_fn` | `Callable` | `None` | `fn(outputs: dict) -> Any`, used only with `merge_strategy="custom"` |
| `**kwargs` | | | All BaseNode parameters |

**What the children get.** Each child runs `execute()` once on its own
`GraphState` with copies of the messages, context and observations. A child
`PromptNode` that calls tools runs them once but does not see their results
(no tool loop inside a `ParallelNode`): the successful calls are added to the
parent's `state.observations`, so a following node without tools sees them in
its prompt. Context a child writes is not copied back; read the merged
`output` instead. A child that raises adds `"name: error"` to the node's
`error`, which fails the node unless an edge or hook handles it.

### LoopNode

Repeat a body node until a condition is met.

```python
LoopNode("refine",
    body_node=edit_node,
    condition=lambda state: state.context.get("quality_score", 0) >= 4,
    max_loop_iterations=5,
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | *required* | Node identifier |
| `body_node` | `BaseNode` | `None` | The node to execute on each iteration (it runs on the graph's own state) |
| `condition` | `Callable` | `None` | `(state) -> bool`, checked **before** each iteration; the loop exits when it returns `True` |
| `max_loop_iterations` | `int` | `5` | Maximum iterations |
| `**kwargs` | | | All BaseNode parameters |

The whole loop is one execution of the `LoopNode` for the engine. A body result
with an `error` stops the loop and becomes the node's error. The node's
`output` is `state.context["loop_result"]`, so have the body write its result
there. For a loop over graph nodes (with edges in between), use
[`loop_until`](engine-edges.md#loop_until-conditional-loop-with-exit) instead.

### HumanNode

Pause for human approval with timeout.

```python
HumanNode("approve",
    prompt_template="Approve sending this email?",
    timeout=300.0,
    on_approve="send",
    on_deny="revise",
    on_timeout="cancel",
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | *required* | Node identifier |
| `prompt_template` | `str` | `"Approve this action?"` | Prompt for the human, formatted with `state.context` (`"Send {draft_id}?"`); a placeholder missing from the context fails the node |
| `timeout` | `float` | `300.0` | Seconds to wait for the handler |
| `on_approve` | `str` | `"__end__"` | Next node on approval |
| `on_deny` | `str \| None` | `None` | Next node on denial; `None` falls back to `default_next` |
| `on_timeout` | `str` | `"__end__"` | Next node when the handler does not answer in time |
| `**kwargs` | | | All BaseNode parameters |

The decision comes from a handler in the run's config:
`async handler(prompt: str, state: GraphState) -> bool`, passed as
`config={"_human_handler": handler}` to `PromptGraphEngine.ainvoke()`. A truthy
return approves.

!!! warning "No handler means approved"
    Without a `_human_handler` in the config, `HumanNode` approves at once
    (`transition_reason` is `"No human handler configured, auto-approved"`).
    `build_agent` does not set one. For tool approval in an agent, use
    [`build_agent(approval=...)`](approval.md) instead.

```python
from promptise.engine import GraphState, HumanNode, PromptGraph, PromptGraphEngine, TransformNode


async def ask(prompt: str, state: GraphState) -> bool:
    print(prompt)                      # Send draft 42?
    return False                       # deny


graph = PromptGraph("approve", mode="static")
graph.add_node(TransformNode("prepare", transform=lambda state: state.context.update(draft_id=42),
                             default_next="approve"))
graph.add_node(HumanNode("approve", prompt_template="Send draft {draft_id}?",
                         on_approve="__end__", on_deny="__end__"))
graph.set_entry("prepare")

engine = PromptGraphEngine(graph, model=None)   # no model call in this graph
await engine.ainvoke({"messages": []}, config={"_human_handler": ask})
print(engine.last_report.nodes_visited)        # ['prepare', 'approve']
```

### TransformNode

Pure data transformation without an LLM call.

```python
TransformNode("format",
    transform=lambda state: {"formatted": state.context.get("raw", "").upper()},
    output_key="formatted_data",
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | *required* | Node identifier |
| `transform` | `Callable` | `None` | `fn(state) -> Any`, sync or async (it gets the state only, no config). Without one the node fails |
| `output_key` | `str` | `"transform_result"` | Where to store the return value in `state.context`; it is also `NodeResult.output` |
| `**kwargs` | | | All BaseNode parameters |

An exception in `transform` fails the node (an `on_error` edge or `"error"` handling can route it).

### SubgraphNode

Embed a complete sub-graph as a single node.

```python
SubgraphNode("research_pipeline",
    subgraph=research_graph,
    inherit_state=True,
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | *required* | Node identifier |
| `subgraph` | `PromptGraph` | `None` | The sub-graph to execute |
| `inherit_state` | `bool` | `True` | `True`: the sub-graph starts from a copy of the parent's messages. `False`: it starts from no messages, and its final messages are appended to the parent's |
| `**kwargs` | | | All BaseNode parameters |

The node runs the sub-graph with a new `PromptGraphEngine` on the parent's
model and `max_iterations`, and the same run config (tools, compaction); the
parent's hooks, `max_node_iterations` and `lightweight_model` are not passed on.
The sub-graph gets its own `GraphState`: it does not see or change the parent's
`context`, plan or observations. Its result is `NodeResult.output`
(`{"messages": [...]}`), and its tokens count toward the parent's.

!!! warning "With `inherit_state=True`, the sub-graph's reply is not added to the conversation"
    In 1.6.0 the default `inherit_state=True` copies the parent's messages in
    but does not copy the sub-graph's new messages back, so a graph that ends
    on a `SubgraphNode` returns the conversation without the sub-graph's
    answer. Use `inherit_state=False` when the sub-graph writes the reply, or
    read `NodeResult.output["messages"]` in a following node or hook.

### AutonomousNode

Agent dynamically picks which node to execute from a pool.

```python
AutonomousNode("agent",
    node_pool=[think_node, search_node, answer_node],
    planner_instructions="Choose the most productive next step.",
    max_steps=15,
    entry_node="think",
    terminal_nodes=["answer"],
)
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | `"autonomous"` | Node identifier |
| `node_pool` | `list[BaseNode]` | `None` | Available nodes the agent can choose from |
| `planner_instructions` | `str` | `""` | Extra instructions for the planning LLM |
| `allow_repeat` | `bool` | `True` | Allow the agent to pick the same node multiple times |
| `max_steps` | `int` | `15` | Maximum autonomous steps |
| `entry_node` | `str \| None` | `None` | Force a specific first node |
| `terminal_nodes` | `list[str]` | `None` | Nodes that end the autonomous loop once they succeed |
| `answer_node` | `str \| None` | `None` | Node that writes the final answer when the agent finishes without a terminal node having run last. Defaults to the first terminal node; with none, the node asks the model for the final answer itself |

| `**kwargs` | | | All BaseNode parameters |

A chosen node runs its whole tool loop (re-run while its model keeps calling tools, up to its `max_iterations`) before the agent picks again. The routing call's prompt and JSON reply (`{"next_node": "...", "reason": "..."}`) never enter `state.messages`; a tool name given as the next step runs the pool node that has that tool; `"__done__"`, `"__end__"`, `"done"` or `"finish"` ends the loop, and so does a reply with no JSON object. The run always ends with a written answer. `NodeResult.output` is `{"steps": [...], "total": n}`.

The whole autonomous loop is **one** node execution for the engine, so engine
hooks (`pre_node` / `post_node`), flags and `max_iterations` apply to the
`AutonomousNode`, not to each pool step; `max_steps` bounds the steps. Tool hooks
(`pre_tool` / `post_tool`) still run for every tool call.

You rarely build an `AutonomousNode` yourself: `PromptGraph.from_pool(nodes)`,
`build_agent(node_pool=[...])`, and `build_agent(agent_pattern=PromptGraph("x", nodes=[...]))`
with two or more nodes and no edges all wrap the pool in one named
`"orchestrator"`, taking `entry_node` from the node with `is_entry=True` and
`terminal_nodes` from the nodes with `is_terminal=True`.

### CodeActionNode

Solves a task by writing **one** Python program that calls the tools, running it in
the [sandbox](sandbox.md) and returning the line the program prints after
`RESULT:`. This is the node behind `agent_pattern="code-action"`; the
[Code-Action guide](../guides/code-action.md) explains when it beats a tool loop.

```python
from promptise.engine import CodeActionNode, PromptGraph

graph = PromptGraph("coder", nodes=[
    CodeActionNode("solve", is_entry=True, is_terminal=True, exec_timeout=60, max_repairs=2),
])
```

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | *required* | Node identifier |
| `tools` | `list` | `None` | Tools the program may call (bridged to the host). Empty: the engine's runtime tools |
| `system_prompt` | `str` | `""` | Instructions put before the code-writing prompt |
| `blocks` | `list` | `None` | Accepted for parity with other nodes; not used in the program prompt |
| `sandbox_factory` | `async () -> SandboxSession` | `None` | Called once per program (the first and each repair), so every program runs in a fresh session, cleaned up afterwards. `build_agent(agent_pattern="code-action")` sets it from `sandbox=`. Without one the node fails with "code-action requires a sandbox" |
| `model_override` | chat model | `None` | A chat model object for this node (not resolved from a string) |
| `max_repairs` | `int` | `1` | How often a failed program (a crash, or one that hit `max_tool_calls`) is sent back for a fix |
| `result_marker` | `str` | `"RESULT:"` | Prefix of the stdout line that holds the answer (the last such line wins; without one, the last non-empty stdout line is the answer) |
| `exec_timeout` | `int` | `120` | Seconds the program may run in the sandbox |
| `max_tool_calls` | `int` | `50` | Most bridged tool calls per program. Reaching it fails the program; with no repairs left the engine raises `CodeActionBudgetError`. `0` disables the cap |
| `**kwargs` | | | All BaseNode parameters |

From `build_agent`, set `exec_timeout`, `max_repairs` and `max_tool_calls` with
`code_action={...}` or a `CodeActionConfig`; see
[Configuration](../guides/code-action.md#configuration).

`CodeActionConfig` (a frozen pydantic model, unknown keys rejected):

| Field | Type | Default | Constraint |
|-------|------|---------|-----------|
| `exec_timeout` | `int` | `120` | `> 0`, `<= 3600` seconds |
| `max_repairs` | `int` | `1` | `0` to `10` |
| `max_tool_calls` | `int` | `50` | `>= 0` (`0` disables the cap) |

The node writes the answer as an `AIMessage`, its `NodeResult.output`, and
`raw_output`. Its prompt holds `system_prompt`, the system messages of the
conversation (memory, flow prompts) and earlier user/assistant turns; tool
traffic from earlier turns is left out. It does not stream model tokens.

---

## Custom Nodes

### @node decorator

Turn any async function into a graph node.

```python
from promptise.engine import node, GraphState, NodeResult, NodeFlag

@node("fetch_weather", default_next="respond", flags={NodeFlag.RETRYABLE})
async def fetch_weather(state: GraphState) -> NodeResult:
    weather = await api.get(state.context.get("city", "Berlin"))
    state.context["weather"] = weather
    return NodeResult(node_name="fetch_weather", output=weather)

graph.add_node(fetch_weather)
```

`node(name, *, instructions="", transitions=None, default_next=None, max_iterations=10, metadata=None, flags=None, is_entry=False, is_terminal=False)` takes the BaseNode parameters except `description`, which comes from the first line of the function's docstring.

- The function must be `async` (a plain function raises `TypeError`), and can accept `(state)`, `(state, config)`, or `()`.
- Return a `NodeResult` to control everything (an empty `node_name` is filled in); any other return value becomes `NodeResult(output=value)`.
- The decorator returns a node object, not a function: add it with `graph.add_node(fetch_weather)`.
- An exception the function raises fails the node; the engine records it as the node's `error`.

### BaseNode subclass

For full control, subclass BaseNode directly.

```python
from promptise.engine import BaseNode, NodeResult, NodeFlag

class DatabaseNode(BaseNode):
    def __init__(self, name, *, query_template, **kwargs):
        super().__init__(name, flags={NodeFlag.RETRYABLE, NodeFlag.STATEFUL}, **kwargs)
        self.template = query_template

    async def execute(self, state, config):
        result = await db.query(self.template.format(**state.context))
        state.context[f"{self.name}_output"] = result
        return NodeResult(node_name=self.name, output=result)
```

When the graph is streamed (`astream()`, `astream_events()`,
`agent.astream_with_tools()`), the engine calls the node's `stream()`. The
default runs `execute()` once and yields an `on_node_end` event with the result,
so a subclass only overrides `stream()` to emit events of its own, and then
must end with `NodeEvent(event="on_node_end", node_name=self.name,
data={"result": result})`. A stream that ends without the result fails the run;
the engine never executes a node twice to get one.

### BaseNode Parameters

All nodes inherit these parameters from BaseNode:

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `name` | `str` | *required* | Unique identifier within the graph |
| `instructions` | `str` | `""` | Description of what the node does |
| `description` | `str` | `""` | Short description for visualization (defaults to first 80 chars of instructions) |
| `transitions` | `dict[str, str]` | `None` | Output key to next-node mapping |
| `default_next` | `str` | `None` | Fallback transition |
| `max_iterations` | `int` | `10` | Max executions per graph run, enforced by the engine; when used up, the `"error"` transition is followed (else `__error__`, else the run ends) |
| `metadata` | `dict` | `None` | Arbitrary metadata for hooks and observability |
| `is_entry` | `bool` | `False` | Adds `NodeFlag.ENTRY` |
| `is_terminal` | `bool` | `False` | Adds `NodeFlag.TERMINAL` |
| `flags` | `set[NodeFlag]` | `None` | Typed flags controlling engine behavior |

Every node also has `has_flag(flag)`, the `is_entry` / `is_terminal`
properties (backed by the `ENTRY` / `TERMINAL` flags), `execute(state, config)`
(raises `NotImplementedError` on `BaseNode`) and `stream(state, config)`.

`NodeProtocol` (`from promptise.engine import NodeProtocol`) is the minimal
runtime-checkable protocol: a `name` attribute plus async `execute()` and
`stream()`. The engine also reads `max_iterations`, `transitions`,
`default_next`, `flags` and `has_flag()`, so subclass `BaseNode` rather than
implementing the protocol from scratch.

---

## Node Flags

Nodes declare their capabilities through typed `NodeFlag` enums. The engine processes these flags at runtime to control execution, caching, error handling, and observability.

```python
from promptise.engine import NodeFlag

PlanNode("plan", is_entry=True)                                    # ENTRY flag
PromptNode("act", inject_tools=True)                               # INJECT_TOOLS flag
SynthesizeNode("answer", is_terminal=True)                         # TERMINAL flag
PromptNode("critical_step", flags={NodeFlag.CRITICAL})             # Abort on error
PromptNode("optional", flags={NodeFlag.SKIP_ON_ERROR, NodeFlag.LIGHTWEIGHT})
```

18 built-in flags cover execution control, context isolation, model selection, observability, and output processing. See [Node Flags](engine-flags.md) for the full reference.

---

## Skills Library

Pre-configured node factories for common tasks:

```python
from promptise.engine.skills import (
    # Standard skills
    web_researcher, code_reviewer, data_analyst, fact_checker,
    summarizer, planner, decision_router, formatter,
    # Reasoning skills
    thinker, reflector, critic, justifier, synthesizer,
    validator_node, observer_node,
)

graph.add_node(planner("plan", is_entry=True))
graph.add_node(web_researcher("search", tools=search_tools))
graph.add_node(reflector("reflect"))
graph.add_node(synthesizer("conclude", is_terminal=True))
```

See [Skills Library](engine-skills.md) for all 15 skill factories with parameters.
