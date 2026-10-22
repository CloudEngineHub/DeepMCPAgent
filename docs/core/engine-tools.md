---
seo_title: "Inject MCP Tools into Graph Nodes"
description: "How MCP tools found by build_agent reach reasoning graph nodes at runtime with inject_tools, how they merge with node tools, and how to pass them yourself."
---

# Runtime Tool Injection

When you use `build_agent(servers=...)`, MCP tools are discovered at startup. For the default ReAct pattern, these tools are automatically available. For custom graphs, you control which nodes get tools.

## How It Works

1. `build_agent()` discovers tools from MCP servers (plus `extra_tools`, cross-agent and sandbox tools)
2. Tools are converted to LangChain `BaseTool` instances
3. `build_agent()` hands them to the engine: `PromptGraphEngine(graph, model, tools=discovered)`
4. Nodes with `inject_tools=True` receive those tools at runtime, together with every tool another node of the graph declares
5. Injected tools merge with the node's own `tools` (no duplicates, the node's own tool wins on a name clash)

Nodes without `inject_tools=True` see only their own `tools`. `agent.tool_names` lists what was discovered; it does not mean every node can call them.

Which node types use injected tools:

| Node | Tools it can call |
|------|-------------------|
| `PromptNode` (and the reasoning nodes) with `inject_tools=True` | Its own `tools` plus the engine's tools |
| `PromptNode` without it | Its own `tools` only |
| `ToolNode` | Its own `tools` only |
| `CodeActionNode` | Its own `tools`; with none, all of the engine's tools |
| `AutonomousNode` | None itself; each pool step uses its own rule above, and the step menu lists each step's tools |

With `build_agent(optimize_tools="semantic")`, a tool selector narrows the
tools *offered* on each model call to the relevant ones; a call to a tool that
was not offered on that step still runs, since every candidate stays callable.

## Usage

```python
from promptise import build_agent
from promptise.config import HTTPServerSpec
from promptise.engine import PromptGraph, PromptNode

graph = PromptGraph("my-agent", mode="static")

# This node gets ALL MCP tools at runtime
graph.add_node(PromptNode("search",
    instructions="Search for information.",
    inject_tools=True,
))

# This node gets NO tools (pure reasoning)
graph.add_node(PromptNode("think",
    instructions="Analyze the results.",
    inject_tools=False,
))

# This node gets ONLY its explicit tools + MCP tools
graph.add_node(PromptNode("enhanced",
    tools=[my_custom_calculator],
    inject_tools=True,   # MCP tools ALSO added
))

graph.always("search", "think")
graph.always("think", "__end__")
graph.set_entry("search")

agent = await build_agent(
    model="openai:gpt-5-mini",
    servers={"tools": HTTPServerSpec(url="http://localhost:8000/mcp")},
    agent_pattern=graph,
)
```

## When to Use

| Scenario | `inject_tools` | Why |
|----------|---------------|-----|
| Tool-calling node | `True` | Needs MCP tools to do work |
| Pure reasoning | `False` | No tools, just thinking |
| Routing decision | `False` | Lightweight, no tool overhead |
| Guard/validation | `False` | No LLM call, no tools needed |
| Mixed (custom + MCP) | `True` + explicit `tools` | Both available |

## Without build_agent

Running a graph directly, pass the tools to the engine yourself:

```python
from promptise import resolve_model, tool
from promptise.engine import PromptGraph, PromptGraphEngine, PromptNode


@tool
def lookup_order(order_id: str) -> str:
    """Status of an order."""
    return f"Order {order_id}: shipped"


graph = PromptGraph("support", nodes=[
    PromptNode("answer", instructions="Answer with the tools.", inject_tools=True,
               is_entry=True, default_next="__end__"),
])
engine = PromptGraphEngine(graph, resolve_model("openai:gpt-5-mini"), tools=[lookup_order])
result = await engine.ainvoke({"messages": [{"role": "user", "content": "Where is order A-17?"}]})
print(result["messages"][-1].content)
```
