---
title: "Tool Calling in Python: Connect an LLM to Tools"
description: "Explains tool/function calling conceptually, then shows the shortcut readers won't find elsewhere: instead of writing JSON schemas by hand for every…"
keywords: "tool calling in python, connect an llm to tools, llm tool calling python, function calling python agent, give an llm tools python"
date: 2026-07-16
slug: tool-calling-in-python
categories:
  - Building Agents
---

# Tool Calling in Python: Connect an LLM to Tools

Tool calling in Python is the mechanism that turns a language model from a text generator into something that can actually *do* things — look up an order, query a database, call an internal API. The concept is simple, but the standard tutorials bury you in JSON Schema boilerplate: for every function you want to expose, you hand-write a schema describing its name, parameters, and types, and keep that schema in sync with the code forever. By the end of this post you'll understand exactly how tool calling works under the hood, and you'll see the shortcut most guides skip — pointing your agent at an MCP server so tools appear auto-discovered, with schemas derived straight from your Python type hints.

## What tool calling in Python actually is

At its core, tool calling (also called function calling) is a loop between your code and the model:

1. You send the model a list of tools, each described as a schema: a name, a description, and a typed set of parameters.
2. The model reads the user's request and, if a tool would help, responds with a structured **tool call** — a function name plus JSON arguments — instead of plain text.
3. *Your* code runs the function and feeds the result back into the conversation.
4. The model uses that result to write its final answer.

The important thing to internalize: the model never executes anything. It only *decides* which tool to call and with what arguments. Execution stays entirely in your Python process. That's what makes tool calling safe to reason about — you own every side effect.

This loop is how an LLM tool calling Python setup answers a question like "What's the weather in Berlin?" The model can't know the weather, but if you've given it a `get_weather` tool, it emits a call to that tool, you run it, and it composes the answer from the result.

## The boilerplate tax: hand-writing function schemas

Here's what the "old way" looks like. To expose a single function to the model, you write the function *and* a schema that describes it:

```python
# The manual way — a schema you write and maintain by hand.
tools = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "Get the current weather for a city.",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {"type": "string", "description": "City name"},
                },
                "required": ["city"],
            },
        },
    }
]
```

That's ~15 lines to describe *one* function that has *one* parameter. Now multiply it. A real agent has a dozen tools, each with several parameters, optional fields, enums, and nested objects. You end up maintaining two representations of the same truth — the Python function and its JSON schema — and every time a signature changes, you have to remember to update both. Forget once, and the model calls your tool with arguments that no longer match, and you get a runtime error that's painful to trace.

This is the tax that makes a function calling Python agent tedious to grow. It's not hard, it's just repetitive and fragile, and it scales badly.

## Connect an LLM to tools with MCP auto-discovery

Promptise Foundry takes a different route. Instead of hand-writing schemas, you expose your tools from a [Model Context Protocol](what-is-mcp.md) (MCP) server and point your agent at it. The agent calls the server's discovery endpoint, reads every tool definition, converts each one into a typed tool the model can call, and starts using them — no manual wiring.

The entire "give an LLM tools Python" step becomes one server reference:

```python
import asyncio
from promptise import build_agent
from promptise.config import StdioServerSpec


async def main():
    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={
            "weather": StdioServerSpec(command="python", args=["weather_server.py"]),
        },
        instructions="You are a helpful assistant. Use tools when they help.",
    )

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "What's the weather in Berlin?"}]}
    )
    print(result["messages"][-1].content)

    await agent.shutdown()


asyncio.run(main())
```

There's no `tools=[...]` list here. You never described `get_weather` to the model — the agent discovered it from the server, generated the schema, handed it to the model, executed the tool call, and fed the result back. That's the full tool-calling loop from the first section, but the boilerplate is gone. Adding a tenth tool to the server means it shows up automatically on the next run; you change nothing in the agent code.

You can point at more than one server at a time — a local stdio process and a remote HTTP service, for example — and the agent merges every discovered tool into one catalog. The [Building Agents](../../guides/building-agents.md) guide walks through mixing server types and passing per-request identity with `CallerContext`.

## How schemas get generated from your type hints

The magic isn't magic — it's the server side doing the work you used to do by hand. Here's the `weather_server.py` the agent above connects to:

```python
from promptise.mcp.server import MCPServer

server = MCPServer("weather-tools")


@server.tool()
async def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"Sunny in {city}"
```

That's the whole tool. The `@server.tool()` decorator inspects the function's signature and generates the JSON schema for you:

- The parameter name `city` and its type `str` become a typed, required string property.
- The docstring becomes the tool's description — the text the model reads to decide *when* to call it.
- The return annotation shapes how the result is serialized back.

Your type hints *are* the schema. There is exactly one source of truth — the function — and it can't drift out of sync with a hand-maintained copy, because there is no copy. Add a parameter with a default, and it becomes optional automatically. Use an enum or a Pydantic-style typed object, and the schema reflects it. This is the concrete payoff of the brief's feature: **MCP tool auto-discovery with schemas generated from type hints, zero manual wiring** — the 40 lines of boilerplate collapse into a decorated function.

Once the server is written, you serve it over stdio or HTTP with a single command:

```bash
promptise serve weather_server:server --transport http --port 8080
```

Any MCP-compatible client — your Promptise agent, Claude Desktop, an internal bot — can now discover and call the same tools. You defined `get_weather` once, and every consumer reads the same definition.

## Keeping prompt tokens flat as your tool count grows

There's an honest catch to auto-discovery: the more tools you expose, the more schemas get sent to the model on every request. With 50 tools, that's thousands of tokens of definitions before the user even asks anything. Promptise addresses this with **semantic tool optimization** — the agent uses local embeddings to select only the tools relevant to each query, cutting **40–70% fewer tokens** for tool definitions without you pruning the catalog by hand. You keep one big, discoverable tool surface, and each individual request only pays for the tools it actually needs. If you want to go deeper on building a full agent around this, the [How to Build an AI Agent in Python: The Complete Guide](how-to-build-an-ai-agent-in-python.md) post ties tool calling into memory, guardrails, and persistence.

## When hand-written tool calling is the better fit

Auto-discovery is a clear win once you have real tools, but be honest about the crossover point. If you're shipping a one-file script with two or three functions that only this agent will ever call, running an MCP server is machinery you don't need yet. Plain inline function calling — tools defined as schemas in the same process, no server, no transport — is genuinely simpler for that case. The overhead of MCP earns its keep when *any* of these becomes true:

- Tools live behind a server or belong to another team.
- More than one agent (or a third-party client) needs the same tools.
- You want auth, roles, rate limits, or audit enforced on the tools themselves.
- The catalog is growing past what you can hold in your head.

Until then, hand-written schemas for a handful of tools are perfectly reasonable, and Promptise won't pretend otherwise. The value shows up exactly when maintaining those schemas by hand starts to hurt.

## Frequently asked questions

### What is the difference between tool calling and function calling in Python?

They're two names for the same thing. "Function calling" is the term OpenAI popularized; "tool calling" is the more general, provider-neutral phrasing. Both describe the loop where a model returns a structured request to invoke a named function with arguments, and your code executes it. In Promptise, the pattern is model-agnostic — the same agent code works whether you point `build_agent` at `openai:gpt-5-mini`, `anthropic:claude-sonnet-4.5`, or a local `ollama` model.

### Do I have to write a JSON schema for every tool?

No. If you write your tools by hand for a single model API, you maintain the schema yourself. If you expose tools from a Promptise MCP server, the `@server.tool()` decorator generates the schema from your function's type hints and docstring, and the agent discovers it automatically. Your Python signature is the single source of truth, so the schema can't drift out of sync with the code.

### How does the model know which tool to call?

It reads each tool's name, description, and parameter schema, then matches them against the user's request. That's why the docstring matters — it's the description the model uses to decide *when* a tool is relevant. Clear, specific docstrings lead to better tool selection, and semantic tool optimization further narrows the choices to the tools most relevant to the current query.

## Next steps

Grab the copy-paste "connect tools" recipe from the [Cookbook](../../getting-started/cookbook.md) and adapt the server above to your own functions — it's the fastest path from concept to a working agent that calls real tools. From there, the [Quick Start](../../getting-started/quickstart.md) gets a full agent running end to end in a few minutes, so you can connect an LLM to tools in your own project today.
