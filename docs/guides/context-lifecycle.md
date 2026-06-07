# Context Lifecycle Management

The single biggest reason long-running agents get slow, expensive, and *wrong*
is **context bloat**. Every tool call appends its request and result to the
transcript. On a deep task the model ends up re-reading a growing wall of its
own past calls — it loses the thread, re-queries facts it already has, and pays
for thousands of redundant tokens on every turn.

Promptise gives you three opt-in levers to control exactly how much history a
reasoning node sees, so context stays bounded as the work gets deep. This guide
shows the problem, the three modes of `context_scope`, the two ready-made
patterns built on them, and a decision table for picking the right one.

!!! info "Runnable example"
    Everything here is demonstrated end-to-end in
    [`examples/reasoning/verify_and_managed.py`](https://github.com/promptise-com/foundry/blob/main/examples/reasoning/verify_and_managed.py)
    — real LLM calls, just set `OPENAI_API_KEY`.

## The problem: transcripts grow, models drown

A naive tool-calling loop feeds the model the **entire** conversation on every
turn:

```
turn 1:  [system, user]
turn 2:  [system, user, ai→tool, tool_result]
turn 3:  [system, user, ai→tool, tool_result, ai→tool, tool_result]
...
turn 12: [system, user, + 22 more messages]   ← the model re-reads ALL of this
```

For a task that needs ~13 distinct facts, a naive loop can make **dozens** of
tool calls — repeatedly looking up the same employee or record because the
relevant result is buried far back in the transcript. Tokens grow
super-linearly, latency climbs, and accuracy can *drop* as the signal gets lost
in the middle.

## The lever: `context_scope` on `PromptNode`

Every [`PromptNode`](../core/engine-nodes.md#promptnode) accepts a
`context_scope` argument controlling what it sees on each LLM call. It is fully
opt-in — the default preserves today's behavior exactly.

| Mode | What the node sees | Use it for |
|------|--------------------|------------|
| `"full"` *(default)* | The whole accumulated transcript | Short tasks, or when every prior message matters |
| `"scoped"` | Its system prompt (with any inherited/distilled state) + the original task + **only its own in-progress tool loop** | Multi-stage reasoning graphs — drops the verbose output of *other* stages so tokens don't grow across stages |
| `"ledger"` | System prompt + task + the **most recent** exchange + a compact **deduplicated "facts gathered" ledger** | Long single-node tool loops that gather many facts then aggregate |

```python
from promptise.engine import PromptNode

# Multi-stage graph: each stage only sees its own working set.
PromptNode("analyze", instructions="...", context_scope="scoped")

# Deep tool loop: replace the growing transcript with a facts ledger.
PromptNode("reason", inject_tools=True, context_scope="ledger")
```

### How `"ledger"` works

Instead of an ever-growing transcript, the node sees a compact ledger built
from the tool results so far:

- One line per `tool(args) = result`, **last value wins** per `(tool, args)` —
  duplicates collapse automatically.
- The ledger is placed **last**, right before the model's turn, where it is
  most salient, so the model consults it instead of re-calling a tool.
- The most recent assistant turn and its tool results are kept *in-flow* so the
  model doesn't lose continuity.
- Tool execution is **cache-served**: a repeated `(tool, args)` call returns the
  cached result instead of re-executing.

See [Context scope](../core/engine-nodes.md#context-scope) for the full
mechanism.

## Two ready-made patterns

You rarely need to wire a node by hand — two built-in `agent_pattern` values
package these levers for the common cases.

### `verify` — accuracy via a one-turn self-check

A single node that must **plan, solve, and re-check its own answer** within one
generation. You get the accuracy benefit of an explicit verification step at
one-turn latency — no multi-call pipeline.

```python
from promptise import build_agent

agent = await build_agent(
    servers={},                      # no tools needed for pure reasoning
    model="openai:gpt-5-mini",
    agent_pattern="verify",
    instructions="Give only the final answer at the end.",
)

result = await agent.ainvoke({"messages": [
    {"role": "user", "content":
     "A bat and a ball cost $1.10. The bat costs $1.00 more than the ball. "
     "How much is the ball?"}
]})
# The VERIFY step catches the intuitive-but-wrong $0.10 and corrects to $0.05.
```

!!! note "Honest scope"
    `verify` lifts accuracy on **weak and mainstream** models where a forced
    self-check recovers careless errors. A frontier model that already reasons
    internally is usually at its ceiling with a plain prompt, so `verify` there
    is a cheap safety net, not a step change. On a capable model it is
    *comparable to* a well-prompted single pass.

### `managed` — efficiency for deep tool chains

A single tool-using node run with `context_scope="ledger"`. Best for traversing
a database or graph: gather many facts, then aggregate.

```python
from promptise import build_agent

agent = await build_agent(
    servers={"company": my_server_spec},  # or pass extra_tools=[...]
    model="openai:gpt-5-mini",
    agent_pattern="managed",
    instructions=(
        "Answer by calling tools. A ledger of facts you already gathered is "
        "provided each turn — consult it and never re-fetch a fact you have."
    ),
    max_agent_iterations=30,          # deep chains make many calls
)
```

!!! note "Honest scope"
    `managed` is an **efficiency primitive**. On long chains it cuts redundant
    tool calls and bounds token growth at **equal accuracy** — a real cost and
    latency win. It does **not** by itself make the model's final answer more
    correct; if your bottleneck is the model mis-aggregating gathered facts,
    that is a model-capability limit, not a context one.

## Which one should I use?

| Situation | Reach for |
|---|---|
| Short Q&A, every message matters | Default `react` (`context_scope="full"`) |
| One question that's easy to get *subtly* wrong | `verify` |
| A long tool chain over a dataset (gather → aggregate) | `managed` |
| A multi-stage custom graph where stages pile up tokens | A custom graph with `context_scope="scoped"` on each stage |
| You need both bounded context *and* a custom topology | Build a [custom graph](../core/agents/reasoning-patterns.md#building-custom-graphs) and set `context_scope` per node |

## Composing it yourself

`context_scope` is a node-level primitive — drop it onto any node in a custom
graph, mixing modes per stage:

```python
from promptise.engine import PromptGraph, PromptNode

graph = PromptGraph("research", mode="static")
graph.add_node(PromptNode("gather", inject_tools=True, context_scope="ledger"))
graph.add_node(PromptNode("write", context_scope="scoped",
                          inherit_context_from="gather"))
graph.sequential("gather", "write")
graph.set_entry("gather")

agent = await build_agent(servers=my_servers, model="openai:gpt-5-mini",
                          agent_pattern=graph)
```

Here `gather` runs a bounded tool loop (ledger), then `write` sees only the
distilled output it inherits plus the task (scoped) — neither stage drowns in
the other's raw messages.

## Key takeaways

- **Context is a resource to manage, not a side effect.** On deep tasks it is
  the deciding factor for cost, latency, and reliability.
- **`context_scope` is opt-in and zero-regression** — `"full"` stays the
  default; reach for `"scoped"` or `"ledger"` only where the chain gets long.
- **`verify` is the accuracy lever; `managed` is the efficiency lever.** Be
  honest about which problem you have — they solve different ones.

## See also

- [Reasoning Patterns](../core/agents/reasoning-patterns.md) — all 9 built-in patterns
- [Nodes reference: Context scope](../core/engine-nodes.md#context-scope) — the full mechanism
- [Prebuilt Patterns](../core/engine-prebuilts.md) — `verify` and `managed` factories
- [`examples/reasoning/verify_and_managed.py`](https://github.com/promptise-com/foundry/blob/main/examples/reasoning/verify_and_managed.py) — runnable demo
