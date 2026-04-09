# Runtime Tool Injection

When you use `build_agent(servers=...)`, MCP tools are discovered at startup. For the default ReAct pattern, these tools are automatically available. For custom graphs, you control which nodes get tools.

## How It Works

1. `build_agent()` discovers tools from MCP servers
2. Tools are converted to LangChain `BaseTool` instances
3. The engine collects all tools and passes them via `config["_engine_tools"]`
4. Nodes with `inject_tools=True` receive all discovered tools at runtime
5. Injected tools merge with any explicitly configured tools (no duplicates)

## Usage

```python
from promptise.engine import PromptGraph, PromptNode

graph = PromptGraph("my-agent")

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
    pattern=graph,
)
```

## When to Use

| Scenario | `inject_tools` | Why |
|----------|---------------|-----|
| Tool-calling node | `True` | Needs MCP tools to do work |
| Pure reasoning | `False` | No tools — just thinking |
| Routing decision | `False` | Lightweight, no tool overhead |
| Guard/validation | `False` | No LLM call, no tools needed |
| Mixed (custom + MCP) | `True` + explicit `tools` | Both available |
