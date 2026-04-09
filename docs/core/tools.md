# Tools & Schema Helpers

The `tools` module is responsible for discovering MCP tools and converting them
into fully-typed LangChain `BaseTool` instances.  Its centrepiece is the
recursive JSON Schema to Pydantic converter that ensures LLMs see rich,
described parameter types -- including nested objects, arrays, unions, enums,
and `$ref`/`$defs` -- so they can generate correct tool calls on the first
attempt.

**Source:** `src/promptise/tools.py`

## Quick example

```python
import asyncio
from promptise import build_agent, StdioServerSpec

async def main():
    # build_agent discovers and converts tools internally
    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={
            "math": StdioServerSpec(command="python", args=["-m", "math_server"]),
        },
    )
    # Agent has discovered all tools from connected MCP servers
    for tool in agent.tools:
        print(f"{tool.name}: {tool.description}")

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "What is 123 * 456?"}]}
    )
    print(result["messages"][-1].content)

asyncio.run(main())
```

## Concepts

### ToolInfo

A frozen dataclass that holds human-friendly metadata about a discovered MCP
tool.  Useful for introspection, debugging, and building tool registries.

```python
from promptise.tools import ToolInfo

info = ToolInfo(
    server_guess="math",
    name="add",
    description="Add two numbers",
    input_schema={
        "type": "object",
        "properties": {
            "a": {"type": "number", "description": "First operand"},
            "b": {"type": "number", "description": "Second operand"},
        },
        "required": ["a", "b"],
    },
)
print(info.name)          # "add"
print(info.description)   # "Add two numbers"
```

### MCPClientError

Raised when communication with the MCP client fails -- for example, when
listing tools or calling a tool over a broken connection.

```python
from promptise.tools import MCPClientError

try:
    # ... attempt to call an MCP tool ...
    pass
except MCPClientError as exc:
    print(f"MCP communication failed: {exc}")
```

### JSON Schema to Pydantic conversion

The core of the module is the `_jsonschema_to_pydantic` function (internal) that
recursively converts a JSON Schema dict into a Pydantic model.  This is critical
for tool-calling accuracy: without proper nested models, the LLM only sees `dict`
and has to guess the structure.

The converter handles:

| JSON Schema feature | Python/Pydantic result |
|---------------------|----------------------|
| `"type": "string"` | `str` |
| `"type": "integer"` | `int` |
| `"type": "number"` | `float` |
| `"type": "boolean"` | `bool` |
| `"type": "array"` | `list` or `list[T]` with typed items |
| `"type": "object"` with `properties` | Nested Pydantic `BaseModel` |
| `"enum": [...]` | `Literal["a", "b", "c"]` |
| `"anyOf"` / `"oneOf"` | `Union[...]` or `Optional[...]` |
| `"allOf"` | Merged properties |
| `"$ref"` / `"$defs"` | Resolved and inlined |
| `"default"` | Pydantic `Field(default=...)` |
| `"description"` | Pydantic `Field(description=...)` |

Example of a complex schema being converted:

```python
# MCP tool exposes this JSON Schema:
schema = {
    "type": "object",
    "properties": {
        "query": {"type": "string", "description": "Search query"},
        "filters": {
            "type": "object",
            "properties": {
                "date_from": {"type": "string", "description": "ISO date"},
                "limit": {"type": "integer", "default": 10},
            },
        },
        "tags": {
            "type": "array",
            "items": {"type": "string"},
        },
    },
    "required": ["query"],
}

# Promptise converts this to a Pydantic model equivalent to:
# class Filters(BaseModel):
#     date_from: str | None = Field(None, description="ISO date")
#     limit: int = Field(10)
#
# class SearchArgs(BaseModel):
#     query: str = Field(..., description="Search query")
#     filters: Filters | None = None
#     tags: list[str] | None = None
```

### MCPToolAdapter

For direct tool discovery and conversion outside of `build_agent`, use
`MCPToolAdapter` from the Promptise MCP Client.  It connects to MCP servers
via `MCPMultiClient`, discovers all available tools, converts their schemas
to Pydantic models, and wraps each tool in a LangChain `BaseTool`.

```python
import asyncio
from promptise import MCPClient, MCPMultiClient, MCPToolAdapter

async def main():
    multi = MCPMultiClient({
        "math": MCPClient(url="http://localhost:8080/mcp"),
    })

    async with multi:
        adapter = MCPToolAdapter(multi)
        tools = await adapter.as_langchain_tools()

        for tool in tools:
            print(f"Tool: {tool.name} -- {tool.description}")

        # For debugging: get tool metadata without creating BaseTool wrappers
        infos = await adapter.list_tool_info()
        for info in infos:
            print(f"{info.server_guess}/{info.name}: {info.input_schema}")

asyncio.run(main())
```

### Callback hooks

`MCPToolAdapter` supports three callback hooks for tracing tool calls:

```python
def on_before(tool_name: str, arguments: dict) -> None:
    """Called before each tool invocation."""
    print(f"Calling {tool_name} with {arguments}")

def on_after(tool_name: str, result: Any) -> None:
    """Called after each tool invocation."""
    print(f"{tool_name} returned: {result}")

def on_error(tool_name: str, error: Exception) -> None:
    """Called when a tool invocation fails."""
    print(f"{tool_name} failed: {error}")

adapter = MCPToolAdapter(
    multi,
    on_before=on_before,
    on_after=on_after,
    on_error=on_error,
)
```

## API summary

### ToolInfo

| Attribute | Type | Description |
|-----------|------|-------------|
| `server_guess` | `str` | Best-guess server name the tool belongs to |
| `name` | `str` | Tool name |
| `description` | `str` | Human-readable tool description |
| `input_schema` | `dict[str, Any]` | Raw JSON Schema for the tool's input |

### MCPClientError

| Base class | Description |
|------------|-------------|
| `RuntimeError` | Raised when MCP client communication fails |

### MCPToolAdapter

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `multi` | `MCPMultiClient` | *required* | Connected multi-server client |
| `on_before` | `OnBefore \| None` | `None` | Callback before each tool call |
| `on_after` | `OnAfter \| None` | `None` | Callback after each tool call |
| `on_error` | `OnError \| None` | `None` | Callback on tool errors |

| Method | Returns | Description |
|--------|---------|-------------|
| `as_langchain_tools()` | `list[BaseTool]` | Discover all tools and return as LangChain tools |
| `list_tool_info()` | `list[ToolInfo]` | Return tool metadata for introspection |

### Callback types

| Type | Signature |
|------|-----------|
| `OnBefore` | `Callable[[str, dict[str, Any]], None]` |
| `OnAfter` | `Callable[[str, Any], None]` |
| `OnError` | `Callable[[str, Exception], None]` |

## Tips and gotchas

!!! tip
    You rarely need to use `MCPToolAdapter` directly.  `build_agent` handles
    tool discovery internally.  The returned `PromptiseAgent` contains all
    discovered tools.

!!! warning
    If an MCP server is unreachable during tool discovery, `MCPToolAdapter`
    raises `MCPClientError` with diagnostic information.  Check server URLs,
    network connectivity, and authentication headers.

!!! tip
    Use `list_tool_info()` for debugging tool discovery without creating
    actual `BaseTool` wrappers.  It returns lightweight `ToolInfo` dataclass
    instances.

!!! warning
    Tool name collisions across servers are possible.  If two servers expose
    a tool with the same name, the last-discovered server wins.  Use
    server-specific tool name prefixes on your MCP servers to avoid this.

## What's next

- [MCP Tool Adapter](../mcp/client/tool-adapter.md) -- the newer adapter using the Promptise MCP Client
- [Config & Server Specs](config.md) -- define the servers that tools are discovered from
- [Callback Handler](callback-handler.md) -- observe tool calls via the observability pipeline
