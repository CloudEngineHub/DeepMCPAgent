# Reasoning Patterns

Every Promptise agent is powered by a Reasoning Graph. By default, `build_agent()` creates a ReAct graph (single node with tools). You can replace this with any of the 7 built-in patterns, or build your own.

```mermaid
graph TD
    BA[build_agent] -->|'react'| R1[ReAct]
    BA -->|'peoatr'| R2[PEOATR]
    BA -->|'research'| R3[Research]
    BA -->|'autonomous'| R4[Autonomous]
    BA -->|'deliberate'| R5[Deliberate]
    BA -->|'debate'| R6[Debate]
    BA -->|'pipeline'| R7[Pipeline]
    BA -->|PromptGraph| R8[Custom]

    subgraph Agent Wrapper
        M[Memory] --- G[Guardrails] --- C[Cache]
        G --- O[Observability] --- E[Events]
    end

    R1 --> Agent Wrapper
    R4 --> Agent Wrapper
    R8 --> Agent Wrapper

    style BA fill:#1e3a5f,stroke:#60a5fa,color:#fff
    style R1 fill:#1a2e1a,stroke:#4ade80,color:#fff
    style R2 fill:#2d1b4e,stroke:#c084fc,color:#fff
    style R3 fill:#3a2a0a,stroke:#fbbf24,color:#fff
    style R4 fill:#3a1a1a,stroke:#f87171,color:#fff
    style R5 fill:#2d1b4e,stroke:#c084fc,color:#fff
    style R6 fill:#3a1a1a,stroke:#f87171,color:#fff
    style R7 fill:#1a2e1a,stroke:#4ade80,color:#fff
    style R8 fill:#3a2a0a,stroke:#fbbf24,color:#fff
```

The Reasoning Graph replaces only the inner loop. All other features (memory, guardrails, cache, observability, events, approval, streaming) stay the same regardless of pattern.

## Quick Reference

```python
from promptise import build_agent

# Default — single node with tools
agent = await build_agent(model="openai:gpt-5-mini", servers=my_servers)

# Built-in patterns
agent = await build_agent(..., agent_pattern="react")       # Tool-calling loop
agent = await build_agent(..., agent_pattern="peoatr")      # Plan → Act → Think → Reflect
agent = await build_agent(..., agent_pattern="research")    # Search → Verify → Synthesize
agent = await build_agent(..., agent_pattern="autonomous")  # Agent builds own path
agent = await build_agent(..., agent_pattern="deliberate")  # Think → Plan → Act → Observe → Reflect
agent = await build_agent(..., agent_pattern="debate")      # Proposer ↔ Critic → Judge
agent = await build_agent(..., agent_pattern="pipeline")    # Sequential chain

# Custom graph
agent = await build_agent(..., agent_pattern=my_graph)

# Node pool (autonomous mode)
agent = await build_agent(..., node_pool=[PlanNode("plan", is_entry=True), ...])
```

## Built-in Patterns

### ReAct (Default)

Single PromptNode with tools. The LLM decides when to call tools and when to produce a final answer. Simplest and fastest for most use cases.

```
reason ──→ (tool calls) ──→ reason ──→ ... ──→ final answer
```

**Best for:** Simple tool-calling agents, Q&A, most general tasks.

### PEOATR

Four specialized stages: Plan subgoals → Act with tools → Think about results → Reflect on progress. The reflect stage decides whether to continue, replan, or answer.

```
plan ──→ act ──→ think ──→ reflect ──→ (continue/replan/answer)
```

**Best for:** Complex multi-step tasks, research, tasks requiring self-correction.

### Research

Three-stage pipeline: Search gathers information, Verify cross-checks for accuracy, Synthesize produces the final output. Verification loops back to search if quality is low.

```
search ──→ verify ──→ synthesize
              ↓ (fail)
           search
```

**Best for:** Fact-checking, research reports, tasks requiring verified information.

### Autonomous

The agent receives a pool of reasoning nodes and dynamically decides which to execute at each step. No static edges — the LLM builds its own execution path.

```
[think, plan, search, analyze, synthesize] → Agent chooses → Agent chooses → ...
```

**Best for:** Open-ended tasks, exploration, tasks where the optimal reasoning path isn't known ahead of time.

### Deliberate

Five-stage deep reasoning: Think before acting, plan the approach, act with tools, observe results carefully, then reflect. Slower but produces higher-quality results.

```
think ──→ plan ──→ act ──→ observe ──→ reflect ──→ (continue/replan/answer)
```

**Best for:** High-stakes decisions, complex analysis, tasks where accuracy matters more than speed.

### Debate

Adversarial two-agent debate. A proposer generates an answer, a critic challenges it, and they alternate until a judge renders the final verdict.

```
proposer ──→ critic ──→ (severity high) ──→ proposer
                    ──→ (severity low)  ──→ judge ──→ done
```

**Best for:** Controversial topics, decision-making, generating robust arguments.

### Pipeline

Simple sequential chain. Each node runs once in order. No loops, no conditions. Use when you need a fixed sequence of processing steps.

```
step1 ──→ step2 ──→ step3 ──→ done
```

**Best for:** Data processing, ETL, fixed multi-step workflows.

## Building Custom Graphs

### With Reasoning Nodes

Pre-built building bricks — fully configured with instructions, context management, and default flags:

```python
from promptise import build_agent
from promptise.engine import PromptGraph, PromptNode
from promptise.engine.reasoning_nodes import (
    PlanNode, ThinkNode, ReflectNode, SynthesizeNode,
)

graph = PromptGraph("my-agent", nodes=[
    PlanNode("plan", is_entry=True),
    PromptNode("act", inject_tools=True),
    ThinkNode("think"),
    ReflectNode("reflect"),
    SynthesizeNode("answer", is_terminal=True),
])

agent = await build_agent(
    model="openai:gpt-5-mini",
    servers=my_servers,
    agent_pattern=graph,
)
```

In autonomous mode (default), the agent decides which node to execute next. No edges needed.

### With Static Edges

Wire nodes explicitly for a fixed topology:

```python
graph = PromptGraph("pipeline", mode="static")

graph.add_node(PlanNode("plan"))
graph.add_node(PromptNode("execute", inject_tools=True))
graph.add_node(SynthesizeNode("answer"))

graph.sequential("plan", "execute", "answer")
graph.set_entry("plan")

agent = await build_agent(..., agent_pattern=graph)
```

### Per-Node Model Override

Use cheaper models for lightweight tasks, powerful models for complex reasoning:

```python
from promptise.engine import PromptNode, NodeFlag
from promptise.engine.reasoning_nodes import ThinkNode

graph = PromptGraph("cost-optimized", nodes=[
    # Cheap model for routing and simple analysis
    ThinkNode("think", model_override="openai:gpt-4o-mini"),

    # Main model for tool calling (uses build_agent's model)
    PromptNode("act", inject_tools=True),

    # Powerful model for final synthesis
    SynthesizeNode("answer", model_override="openai:gpt-4o", is_terminal=True),
])
```

### Node Flags

Control execution behavior with typed flags:

```python
from promptise.engine import PromptNode, NodeFlag

# Abort the entire graph if this node fails
PromptNode("critical_step", flags={NodeFlag.CRITICAL})

# Retry on failure with exponential backoff
PromptNode("flaky_api", flags={NodeFlag.RETRYABLE})

# Skip this node if the previous one errored
PromptNode("optional_enrichment", flags={NodeFlag.SKIP_ON_ERROR})

# Cache results — same inputs return cached output
PromptNode("expensive_analysis", flags={NodeFlag.CACHEABLE})

# Don't pass conversation history to this node
PromptNode("stateless_classifier", flags={NodeFlag.NO_HISTORY})

# Isolate context — node only sees its input_keys
PromptNode("isolated", flags={NodeFlag.ISOLATED_CONTEXT}, input_keys=["query"])
```

See [Node Flags](../engine-flags.md) for all 16 flags.

### Data Flow Between Nodes

```python
# Node A writes output to state
PromptNode("search", output_key="search_data", inject_tools=True)

# Node B reads specific keys from state
PromptNode("analyze", input_keys=["search_data"])

# Node B inherits the previous node's full output
PromptNode("analyze", inherit_context_from="search")
```

### Processors

Transform data before/after the LLM call:

```python
from promptise.engine.processors import (
    json_extractor, confidence_scorer,
    chain_postprocessors,
)

PromptNode("analyze",
    postprocessor=chain_postprocessors(
        json_extractor(keys=["answer", "confidence"]),
        confidence_scorer(),
    ),
)
```

See [Processors](../engine-processors.md) for all built-in processors.

## How It Integrates

When you pass `agent_pattern=` to `build_agent()`:

1. MCP tools are discovered from servers (as usual)
2. The graph is used instead of the default ReAct graph
3. Nodes with `inject_tools=True` receive the discovered tools
4. All other agent features work unchanged:
    - Memory injection (before graph execution)
    - Input/output guardrails (before/after graph execution)
    - Semantic cache (before/after graph execution)
    - Conversation persistence (`chat()` method)
    - Observability (callbacks propagated through graph)
    - Events and notifications
    - CallerContext per-request identity

## See Also

- [Reasoning Graph Overview](../engine.md) — Architecture and engine details
- [All 20 Node Types](../engine-nodes.md) — Full parameter reference
- [Node Flags](../engine-flags.md) — 16 typed execution flags
- [Prebuilt Patterns](../engine-prebuilts.md) — Pattern factory functions
- [Skills Library](../engine-skills.md) — 15 pre-configured node factories
- [Building Custom Reasoning Guide](../../guides/custom-reasoning.md) — Step-by-step examples
