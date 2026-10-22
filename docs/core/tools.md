---
seo_title: "Tools: @tool Functions, MCP Tool Discovery and Schemas"
description: "Give a Promptise agent a Python function as a tool with @tool: schema from type hints and docstrings, validation, CallerContext, ToolError and timeouts. Plus how MCP tools are discovered and converted."
---

# Tools

An agent calls two kinds of tools: Python functions you declare with `@tool`
and pass to `build_agent(extra_tools=[...])`, and the tools of the MCP servers
it connects to.  This page covers both: first how to declare a tool, then how
MCP tools are discovered and their JSON Schemas converted.

**Source:** `src/promptise/function_tools.py` (`@tool`), `src/promptise/tools.py` (MCP tools)

## Declare tools with `@tool`

Decorate a function with `@tool` and give it to the agent:

```python
import asyncio
from typing import Annotated

from pydantic import Field

from promptise import CallerContext, ToolError, build_agent, tool

ORDERS = {"A-1001": {"order_id": "A-1001", "status": "shipped", "total": 120.0}}


@tool
async def get_order(order_id: str, include_lines: bool = False) -> dict:
    """Look up an order by its ID.

    Args:
        order_id: The order ID, for example "A-1001".
        include_lines: Also return the order lines.
    """
    order = ORDERS.get(order_id)
    if order is None:
        raise ToolError(f"No order {order_id}. Check the ID with the customer.")
    return order


@tool(name="refund_order", timeout=10)
def refund(
    order_id: str,
    amount: Annotated[float, Field(gt=0, description="Amount to refund, in EUR.")],
    caller: CallerContext,
) -> str:
    """Refund part of an order."""
    return f"Refunded {amount:.2f} EUR on {order_id} for {caller.user_id}."


async def main():
    agent = await build_agent(model="openai:gpt-5-mini", extra_tools=[get_order, refund])
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Refund 20 EUR on order A-1001."}]},
        caller=CallerContext(user_id="alice"),
    )
    print(result["messages"][-1].content)
    await agent.shutdown()


asyncio.run(main())
```

`@tool` works bare, with options (`@tool(name=..., description=..., timeout=...)`)
and with the name alone (`@tool("get_order")`), on `async def` and plain `def`
functions.  `extra_tools` also takes a plain function as it is (it is made a
tool as if decorated) and LangChain tools.

### What the model sees

The tool's **name** is the function's name.  Its **description** is the
docstring without the parameter section; the parameter section describes the
parameters instead.  The **parameters** come from the type hints, defaults
and `Field` constraints.  For `get_order` the model is offered:

```json
{
  "name": "get_order",
  "description": "Look up an order by its ID.",
  "parameters": {
    "type": "object",
    "properties": {
      "order_id": {"type": "string", "description": "The order ID, for example \"A-1001\"."},
      "include_lines": {"type": "boolean", "default": false, "description": "Also return the order lines."}
    },
    "required": ["order_id"]
  }
}
```

`refund_order` takes `order_id` (a string) and `amount`, both required:

```json
{"type": "number", "exclusiveMinimum": 0, "description": "Amount to refund, in EUR."}
```

`caller` is not in the schema: the agent fills it in (see
[The caller](#the-caller)).  A function decorated with `@server.tool()` on a
Promptise MCP server gets the same parameter schema, built by the same code,
so a tool keeps its parameters when it moves between your agent and an MCP
server.  (An MCP server describes the tool with the docstring's first
paragraph only.)

### Describing parameters

A parameter's description comes from the first of these that is present:

| Source | Example |
|---|---|
| `Field(description=...)` in `Annotated` or as the default | `amount: Annotated[float, Field(gt=0, description="Amount to refund.")]` |
| A string in `Annotated` | `reason: Annotated[str, "Why the customer wants a refund."]` |
| The docstring, Google style | `Args:` then `order_id: The order ID.` |
| The docstring, NumPy style | `Parameters` / `----------` then `order_id : str` and an indented description |
| The docstring, Sphinx style | `:param order_id: The order ID.` |

Pass `description=` to `@tool` to send the model a different tool
description.  A function without a docstring is described by its name, so
write one: the model decides when to call a tool from its description.

### Types and validation

Every type Pydantic understands works: `str`, `int`, `float`, `bool`,
`Optional[...]` / `X | None`, `Literal[...]`, `Enum`, `list[...]`,
`dict[str, ...]`, `datetime` / `date`, nested Pydantic models (inlined into
the schema, without `$ref`), and constraints such as `Field(ge=1, le=100)`,
`max_length` or `pattern`.  A parameter without a type hint is a string.

The arguments are validated before the function runs.  The function gets
them in its own types (a nested model as an instance, an enum as its member,
defaults filled in), and lax conversions apply (`"12.5"` for a `float`).  An
explicit `null` for an optional parameter that can't be `None` counts as not
given, and a nested object the model sent as JSON text is decoded.  When the
arguments don't fit, the function does not run and the model gets a tool
error it can correct:

```text
Invalid arguments for tool 'refund_order':
- amount: Input should be greater than 0 (got -20)
Correct the arguments and call the tool again.
```

Arguments that are not JSON at all (a Python-style dict with single quotes, a
cut-off object) never reach the function.  The agent answers the call with an
error that says what is wrong and names the tool's parameters, and the model
can call again:

```text
Error: The arguments for 'refund_order' were not valid JSON (Expecting property name enclosed in double quotes at line 1 column 2). Call the tool again with a JSON object that matches its parameters: order_id (required), amount (required).
```

### The caller

A parameter annotated `CallerContext` receives the caller of the current
`agent.ainvoke(..., caller=...)` or `agent.chat(..., caller=...)`: the user's
id, tenant, roles, scopes and metadata, to scope a query or check a
permission.  It is never part of the schema, so the model can't set it.
Without a caller the parameter gets an empty `CallerContext()` (no user, no
roles); annotate it `CallerContext | None` to get `None` instead.  It works
in sync and async tools, and in a peer agent the caller is the one the first
agent was invoked with.

```python
@tool
async def my_orders(status: str, caller: CallerContext) -> list:
    """List the caller's orders with a status."""
    if "support" not in caller.roles:
        raise ToolError("Only support staff can list orders.")
    return await db.orders.list(owner=caller.user_id, status=status)
```

### Errors

Raise `ToolError` (from `promptise`, the same class MCP server tools raise) to
give the model an error it can act on.  The model gets the error envelope a
Promptise MCP server sends, and the call counts as failed in events,
observability and streaming:

```json
{"error": {"code": "TOOL_ERROR", "message": "No order A-9999. Check the ID with the customer.", "retryable": false}}
```

`ToolError` takes `code=`, `retryable=` and `suggestion=` as well, and the
other `MCPError` subclasses (`RateLimitError`, `ValidationError` ...) are
reported the same way.  Any other exception is a tool that failed, as for
every tool: the model reads `Error: <type>: <message>`, so don't put secrets
in exception messages.

With `timeout=` a call that runs longer is answered with a `TIMEOUT` error
(`Tool 'refund_order' did not finish within 10s, so its outcome is unknown.`).
An async function is cancelled; a sync function's thread can't be stopped, so
it finishes in the background and its result is dropped (the error says it is
still running). The call may have taken effect before it was stopped, so the
error is `retryable` only for a tool marked `@tool(read_only=True)` or
`@tool(idempotent=True)`; for any other tool it tells the model to check
whether the call took effect before calling it again. `read_only` and
`idempotent` are the MCP `readOnlyHint` and `idempotentHint` annotations, so
the semantic cache and approval rules read them too.

### Results

| The function returns | The model reads |
|---|---|
| `str` | The string as it is |
| `None` | An empty result |
| `dict`, `list`, `tuple`, numbers, `bool` | JSON (`{"status": "shipped"}`, `["a", "b"]`, `42`, `true`) |
| A Pydantic model or dataclass | Its fields as JSON |
| `datetime`, `Enum`, `set` inside the value | ISO strings, the enum's value, a list |

Non-ASCII text stays readable (`"Zürich"`, not `"Z\u00fcrich"`).  With
`agent_pattern="code-action"` the program gets the value back as data (a
dict, a list), not as JSON text.

### Sync and async functions

An `async def` tool runs on the event loop.  A plain `def` tool runs in a
worker thread, so a blocking call (a database driver, `requests`) never
stalls the agent or other tool calls running in parallel.

### Testing a tool

A tool stays callable like the function it was made from, so unit tests call
it directly, without validation or an agent:

```python
assert (await get_order("A-1001"))["status"] == "shipped"
assert refund("A-1001", 20.0, CallerContext(user_id="alice")).startswith("Refunded")
```

`await get_order.ainvoke({"order_id": "A-1001"})` runs it the way the agent
does (validated, with the caller injected) and returns the text the model
reads.

To test the agent that calls your tools without a model provider, script the
model's tool calls with `ScriptedModel` (see [Testing Agents](testing.md)).

### Approval, guardrails and tracing

A `@tool` is a LangChain `BaseTool`, so everything that applies to tools
applies to it, by its name: `ApprovalPolicy(tools=["refund_*"])` gates it
(invalid arguments are rejected before a reviewer is asked), guardrails with
`scan_tool_results=True` scan its result before the model reads it, and
`trace_tools`, `observe`, `events` and `optimize_tools` include it.

!!! note "LangChain tools still work"
    `extra_tools` accepts LangChain tools as well (`@tool` from
    `langchain_core.tools`, `StructuredTool`, any `BaseTool`), so existing
    code keeps working.  Switching a simple tool to `from promptise import
    tool` changes nothing the model sees: the name, description and
    parameter schema are the same.  For a docstring with an `Args:` section
    the parameter descriptions move from the tool description into the
    parameters, as with LangChain's `parse_docstring=True`.

## MCP tools

The `tools` module discovers MCP tools and converts them into fully-typed
LangChain `BaseTool` instances.  Its centrepiece is the recursive JSON Schema
to Pydantic converter that ensures LLMs see rich, described parameter types
-- including nested objects, arrays, unions, enums, and `$ref`/`$defs` -- so
they can generate correct tool calls on the first attempt.

### Discovering MCP tools

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

### Concepts

#### ToolInfo

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

#### MCPClientError

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

#### JSON Schema to Pydantic conversion

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

#### MCPToolAdapter

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

#### Callback hooks

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

### API summary

#### ToolInfo

| Attribute | Type | Description |
|-----------|------|-------------|
| `server_guess` | `str` | Best-guess server name the tool belongs to |
| `name` | `str` | Tool name |
| `description` | `str` | Human-readable tool description |
| `input_schema` | `dict[str, Any]` | Raw JSON Schema for the tool's input |

#### MCPClientError

| Base class | Description |
|------------|-------------|
| `RuntimeError` | Raised when MCP client communication fails |

#### MCPToolAdapter

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

#### Callback types

| Type | Signature |
|------|-----------|
| `OnBefore` | `Callable[[str, dict[str, Any]], None]` |
| `OnAfter` | `Callable[[str, Any], None]` |
| `OnError` | `Callable[[str, Exception], None]` |

### Tips and gotchas

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
    a tool with the same name, the server listed last in `servers` wins:
    the agent gets only that server's copy, and a warning is logged.  Use
    server-specific tool name prefixes on your MCP servers to avoid this.

## What's next

- [Testing Agents](testing.md) -- run an agent and its tools on scripted model replies
- [Approval (HITL)](approval.md) -- gate tool calls behind a human decision
- [Guardrails](guardrails.md) -- scan tool results before the model reads them
- [CallerContext: Agent to MCP Identity](../guides/multi-user-identity.md) -- per-request identity end to end
- [Server Fundamentals](../mcp/server/building-servers.md) -- serve the same functions to any MCP client
- [MCP Tool Adapter](../mcp/client/tool-adapter.md) -- the newer adapter using the Promptise MCP Client
- [Config & Server Specs](config.md) -- define the servers that tools are discovered from
- [Callback Handler](callback-handler.md) -- observe tool calls via the observability pipeline
