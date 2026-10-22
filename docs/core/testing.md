---
seo_title: "Test AI Agents Without API Calls: ScriptedModel"
description: "Test a Promptise agent without API calls: script the model's replies and tool calls with ScriptedModel, assert on its requests, and test malformed tool calls."
---

# Testing Agents

An agent's behavior depends on what the model answers, and a real model costs
money, needs an API key and answers differently from run to run.
`promptise.testing.ScriptedModel` takes the model's place: you write its
replies in advance, the agent runs exactly as it would with a provider (tools
are bound and called, results go back to the model, streaming works), and the
model records every request it received so you can assert on it.

Nothing leaves the process, so these tests run in CI without secrets, and
you don't import LangChain to write them.

**Source:** `src/promptise/testing.py`

## A first test

Script one tool call and the final answer, run the agent, and check both the
answer and what the model was sent:

```python
import asyncio
import json

from promptise import build_agent, tool
from promptise.testing import ScriptedModel, ToolCall

ORDERS = {"A-1001": {"order_id": "A-1001", "status": "shipped"}}


@tool
def get_order(order_id: str) -> dict:
    """Look up an order by its ID.

    Args:
        order_id: The order ID, for example "A-1001".
    """
    return ORDERS[order_id]


async def ask(model: ScriptedModel, question: str) -> str:
    agent = await build_agent(model=model, extra_tools=[get_order])
    try:
        result = await agent.ainvoke({"messages": [{"role": "user", "content": question}]})
    finally:
        await agent.shutdown()
    return result["messages"][-1].content


def test_agent_looks_up_the_order():
    model = ScriptedModel([
        ToolCall("get_order", {"order_id": "A-1001"}),
        "Order A-1001 has shipped.",
    ])

    answer = asyncio.run(ask(model, "Where is order A-1001?"))

    assert answer == "Order A-1001 has shipped."
    # The model was called twice, with the get_order tool bound both times.
    assert [call.tools for call in model.calls] == [["get_order"], ["get_order"]]
    # The second call carried the tool's result.
    assert model.calls[1].messages[-1].content == json.dumps(ORDERS["A-1001"])
```

Run it with `pytest`.  The test wraps the agent in `asyncio.run()`, so it
needs no plugin; with `pytest-asyncio` you can write `async def` tests and
`await` the agent directly.

Each model call takes the next reply.  The first reply asks for `get_order`,
the agent runs the real tool, and the second reply is the answer.  The tool
itself is not mocked: only the model is scripted.

## Replies

A reply is one model turn.  It can be:

| Reply | The model's turn |
|---|---|
| `"text"` | Text with no tool calls: the agent's answer |
| `ToolCall(name, args)` | One tool call |
| `[ToolCall(...), ToolCall(...)]` | Several tool calls in one turn (the agent runs them in parallel); a string in the list is the turn's text |
| `Malformed.tool_call_as_text(...)` | A tool call written into the text instead of sent as one |
| `Malformed.unparseable_arguments(...)` | A tool call whose arguments are not valid JSON |
| an `AIMessage` | That LangChain message, as it is |

`ToolCall(name, args, id=None)` takes the tool's name and arguments.  Leave
`id` out and the model numbers its calls `call_1`, `call_2`, ... in the order
it sends them, so the ids in a test are always the same.

A reply that isn't one of these raises `TypeError` when the model is created,
so a typo in a script fails at once rather than in the middle of a run.

## Assert on what the model received

`model.calls` holds a `ModelCall` for every request, in order:

| Attribute | What it holds |
|---|---|
| `messages` | The messages the model was sent (system prompt, conversation, tool results) |
| `tools` | The names of the tools bound for that call |
| `tool_schemas` | Those tools as the model was offered them (OpenAI-style function definitions) |
| `reply` | The message the model answered with (`None` when it had no reply left) |

`model.remaining` is the number of replies not yet sent.  Checking it at the
end of a test shows that the agent made as many model calls as you expected:

```python
import asyncio

from promptise import build_agent, tool
from promptise.testing import ScriptedModel, ToolCall


@tool
def get_order(order_id: str) -> dict:
    """Look up an order by its ID."""
    return {"order_id": order_id, "status": "shipped"}


async def main():
    model = ScriptedModel([ToolCall("get_order", {"order_id": "A-1001"}), "Shipped."])
    agent = await build_agent(
        model=model,
        instructions="You answer questions about orders.",
        extra_tools=[get_order],
    )
    await agent.ainvoke({"messages": [{"role": "user", "content": "Where is A-1001?"}]})
    await agent.shutdown()

    first = model.calls[0]
    print(first.messages[0].type, "|", first.messages[0].content[:33])
    print(first.messages[-1].type, "|", first.messages[-1].content)
    print(first.tool_schemas[0]["function"]["parameters"]["required"])
    print(model.calls[1].messages[-1].type, model.calls[1].messages[-1].tool_call_id)
    print("replies left:", model.remaining)


asyncio.run(main())
```

Output:

```
system | You answer questions about orders
human | Where is A-1001?
['order_id']
tool call_1
replies left: 0
```

The messages are LangChain message objects: `.type` is `"system"`, `"human"`,
`"ai"` or `"tool"`, and `.content` is the text.  A tool result also has
`.tool_call_id` and `.status` (`"success"` or `"error"`).

## Test failure handling

Small and local models get tool calling wrong more often than large hosted
ones: they invent tool names, send arguments that don't match the schema or
aren't JSON, or write the tool call into their text.  Scripting these replies tests what your
agent does when it happens, without waiting for a model to make the mistake.

### A tool name the agent doesn't have

Script a `ToolCall` with a name the agent has no tool for.  The agent doesn't
fail: the model gets an error result that names the tools it can call, and its
next turn can use the right one.

```python
import asyncio

from promptise import build_agent, tool
from promptise.testing import ScriptedModel, ToolCall


@tool
def get_order(order_id: str) -> dict:
    """Look up an order by its ID."""
    return {"order_id": order_id, "status": "shipped"}


@tool
def refund_order(order_id: str, amount: float) -> str:
    """Refund part of an order."""
    return f"Refunded {amount:.2f} EUR on {order_id}."


async def main():
    model = ScriptedModel([
        ToolCall("get_order_status", {"order_id": "A-1001"}),  # invented name
        ToolCall("get_order", {"order_id": "A-1001"}),         # the model corrects it
        "Order A-1001 has shipped.",
    ])
    agent = await build_agent(model=model, extra_tools=[get_order, refund_order])
    result = await agent.ainvoke({"messages": [{"role": "user", "content": "Where is A-1001?"}]})
    await agent.shutdown()

    error = model.calls[1].messages[-1]
    print(error.status, "|", error.content)
    print(result["messages"][-1].content)


asyncio.run(main())
```

Output:

```
error | Error: Unknown tool 'get_order_status'. Call one of the available tools: get_order, refund_order.
Order A-1001 has shipped.
```

The error lists the agent's tools with the names closest to the one called
first, at most 20 of them, followed by how many more there are.  When the
name looks like a misspelling of a tool's name (`get_ordr`, `getOrder`), the
error also asks "Did you mean 'get_order'?".  A name that only shares a word
with a tool, such as `cancel_order`, gets no suggestion, so the model isn't
pointed at a tool that does something else.

### Invalid arguments

Arguments that don't match the tool's schema are a `ToolCall` too.  A
[`@tool`](tools.md) function checks its arguments before it runs, and the
model gets the problems to correct:

```python
import asyncio

from promptise import build_agent, tool
from promptise.testing import ScriptedModel, ToolCall


@tool
def get_order(order_id: str) -> dict:
    """Look up an order by its ID."""
    return {"order_id": order_id, "status": "shipped"}


async def main():
    model = ScriptedModel([
        ToolCall("get_order", {"id": "A-1001"}),        # wrong argument name
        ToolCall("get_order", {"order_id": "A-1001"}),
        "Order A-1001 has shipped.",
    ])
    agent = await build_agent(model=model, extra_tools=[get_order])
    await agent.ainvoke({"messages": [{"role": "user", "content": "Where is A-1001?"}]})
    await agent.shutdown()

    print(model.calls[1].messages[-1].content)


asyncio.run(main())
```

Output:

```
Invalid arguments for tool 'get_order':
- order_id: Field required
Correct the arguments and call the tool again.
```

### A tool call written as text

Some models answer with the tool call as JSON in their text instead of as a
tool call.  `Malformed.tool_call_as_text(name, args)` sends that reply, in one
of three styles:

| `style` | Text of the reply |
|---|---|
| `"json"` (default) | `{"name": "get_order", "arguments": {"order_id": "A-1001"}}` |
| `"tagged"` | The same object inside `<tool_call>` and `</tool_call>` lines |
| `"fenced"` | The same object in a Markdown `json` code block |

The reply has no tool call, so the agent takes the text as the answer and no
tool runs.  A test can check that your code catches such an answer, for
example a check that rejects answers that look like JSON:

```python
import asyncio

from promptise import build_agent, tool
from promptise.testing import Malformed, ScriptedModel


@tool
def get_order(order_id: str) -> dict:
    """Look up an order by its ID."""
    return {"order_id": order_id, "status": "shipped"}


def looks_like_a_tool_call(answer: str) -> bool:
    text = answer.strip().strip("`").removeprefix("json").strip()
    return text.startswith(("{", "<tool_call>")) and '"name"' in text


async def main():
    for style in ("json", "tagged", "fenced"):
        model = ScriptedModel([
            Malformed.tool_call_as_text("get_order", {"order_id": "A-1001"}, style=style),
        ])
        agent = await build_agent(model=model, extra_tools=[get_order])
        result = await agent.ainvoke({"messages": [{"role": "user", "content": "Where is A-1001?"}]})
        await agent.shutdown()
        answer = result["messages"][-1].content
        print(style, looks_like_a_tool_call(answer))


asyncio.run(main())
```

Output:

```
json True
tagged True
fenced True
```

### Arguments that aren't JSON

`Malformed.unparseable_arguments(name, raw_args)` sends a tool call whose
argument text can't be parsed, such as a Python-style dict with single
quotes (the default).  LangChain keeps such a call in the message's
`invalid_tool_calls`, not its `tool_calls`, as it does for a provider's reply.
The agent doesn't run a call it can't read.  It answers the call with a tool
error (`status="error"`, the call's own `tool_call_id`) that says what is
wrong and names the tool's parameters, and the loop goes on, so the model can
call the tool again:

```python
import asyncio

from promptise import build_agent, tool
from promptise.testing import Malformed, ScriptedModel, ToolCall


@tool
def get_order(order_id: str) -> dict:
    """Look up an order by its ID."""
    return {"order_id": order_id, "status": "shipped"}


async def main():
    model = ScriptedModel([
        Malformed.unparseable_arguments("get_order", "{'order_id': 'A-1001'}"),
        ToolCall("get_order", {"order_id": "A-1001"}),  # the model calls again
        "Order A-1001 has shipped.",
    ])
    agent = await build_agent(model=model, extra_tools=[get_order])
    result = await agent.ainvoke({"messages": [{"role": "user", "content": "Where is A-1001?"}]})
    await agent.shutdown()

    error = model.calls[1].messages[-1]
    print(error.status, error.tool_call_id)
    print(error.content)
    print(result["messages"][-1].content)


asyncio.run(main())
```

Output:

```
error call_1
Error: The arguments for 'get_order' were not valid JSON (Expecting property name enclosed in double quotes at line 1 column 2). Call the tool again with a JSON object that matches its parameters: order_id (required).
Order A-1001 has shipped.
```

Such a call counts as a failed tool call: `NodeResult.tool_calls` records it
with `success: False`, `invalid: True` and the text the model sent under
`raw_args`, callbacks get its start and an `on_tool_error`
(`ToolArgumentError`), and `astream_with_tools()` yields a `ToolEndEvent`
with `success=False`.  Each retry is a model call, so `max_agent_iterations`
still ends a model that never sends valid arguments.  A call that names a tool
the agent doesn't have gets the
[unknown-tool error](#a-tool-name-the-agent-doesnt-have) instead.

When this reply is streamed, LangChain reads the arguments as a partial JSON
object, as it does for a provider's stream: a cut-off object such as
`'{"order_id"'` becomes a tool call with the arguments read so far (`{}`),
while text that can't start a JSON object stays an invalid call and gets the
same error.  A streamed run (`astream_with_tools()`) recovers the same way.

## When the replies run out

A model call after the last reply raises `ScriptExhaustedError`, which says
which call it was and how many replies the script has.  In an agent run it
fails the run: `ainvoke()` raises `GraphExecutionError`, whose `__cause__` is
the `ScriptExhaustedError`.  An agent that makes more model calls than you
scripted is usually the bug a test is looking for, so this is the default.

```python
import asyncio

from promptise import build_agent
from promptise.engine import GraphExecutionError
from promptise.testing import ScriptedModel, ScriptExhaustedError


async def main():
    model = ScriptedModel(["Hello!"])
    agent = await build_agent(model=model)
    ask = {"messages": [{"role": "user", "content": "Hi"}]}
    await agent.ainvoke(ask)
    try:
        await agent.ainvoke(ask)
    except GraphExecutionError as exc:
        assert isinstance(exc.__cause__, ScriptExhaustedError)
        print(exc.__cause__)
    finally:
        await agent.shutdown()


asyncio.run(main())
```

Output:

```
ScriptedModel has no reply for model call 2: the script has 1 reply. Add a reply, or pass on_exhausted='repeat_last' to repeat the last one.
```

The call is still recorded in `model.calls`, with `reply=None`, so you can
see what the agent asked for.

`ScriptedModel(replies, on_exhausted="repeat_last")` answers every call after
the last reply with the last reply again, for a test that calls the agent
many times with the same answer.  A script with no replies raises either way.

## Streaming and sync calls

The model streams too.  Its text comes word by word and each tool call as one
chunk, so `astream_with_tools()` and `astream()` emit the same events as with
a provider:

```python
import asyncio

from promptise import build_agent, tool
from promptise.testing import ScriptedModel, ToolCall


@tool
def get_order(order_id: str) -> dict:
    """Look up an order by its ID."""
    return {"order_id": order_id, "status": "shipped"}


async def main():
    model = ScriptedModel([
        ToolCall("get_order", {"order_id": "A-1001"}),
        "Order A-1001 has shipped.",
    ])
    agent = await build_agent(model=model, extra_tools=[get_order])
    async for event in agent.astream_with_tools(
        {"messages": [{"role": "user", "content": "Where is A-1001?"}]}
    ):
        if event.type == "token":
            print(f"token {event.text!r}")
        else:
            print(event.type)
    await agent.shutdown()


asyncio.run(main())
```

Output:

```
tool_start
tool_end
token 'Order '
token 'A-1001 '
token 'has '
token 'shipped.'
done
```

`model.invoke()` and `model.stream()` work as well, for code that calls a
chat model synchronously:

```python
from promptise.testing import ScriptedModel

model = ScriptedModel(["Bonjour", "Hello there"])
print(model.invoke("Say hello in French").content)
print([chunk.content for chunk in model.stream("Now in English") if chunk.content])
```

Output:

```
Bonjour
['Hello ', 'there']
```

## Where to use it

`ScriptedModel` is a LangChain chat model, so it goes wherever a model
instance is accepted: `build_agent(model=...)`, every agent pattern
(`agent_pattern="react"`, `"peoatr"`, your own `PromptGraph`), a
[`FallbackChain`](fallback.md) and a graph node's model.  Tools are bound
with `bind_tools()` as for a provider model, and `calls` records the tools of
each call.

Things to know:

- **The replies don't depend on the input.**  A script fixes the order of
  the model's turns, not what it reads.  Assert on `model.calls` to check
  what the agent sent; to make a reply depend on the conversation, script an
  `AIMessage` you build in the test, or subclass the model.
- **There is no token usage.**  Replies carry no usage data, so budgets and
  cost reports see zero tokens.
- **`AgentProcess` takes a model id.**  `ProcessConfig.model` is a string
  such as `"openai:gpt-5-mini"`, so a process can't run on a
  `ScriptedModel`.  Test the agent a process runs with `build_agent()` and
  the same tools and instructions.
- **Concurrent runs share the script.**  Each call takes the next reply
  whichever run makes it, so give each concurrent run its own model.

## API summary

| Name | What it is |
|---|---|
| `ScriptedModel(replies, *, on_exhausted="raise")` | The chat model; `calls`, `remaining` |
| `ToolCall(name, args={}, id=None)` | A tool call in a reply |
| `Malformed.tool_call_as_text(name, args=None, *, style="json")` | A tool call written as text |
| `Malformed.unparseable_arguments(name, raw_args="{'arg': 'value'}", *, id=None)` | A tool call whose arguments are not JSON |
| `ModelCall` | One recorded request: `messages`, `tools`, `tool_schemas`, `reply` |
| `ScriptExhaustedError` | Raised by a call after the last reply (a `RuntimeError`) |

All of them are imported from `promptise.testing`.

## What's next

- [Tools (@tool)](tools.md) -- declare the tools your agent calls
- [Building Agents](agents/building-agents.md) -- everything `build_agent()` takes
- [Streaming](streaming.md) -- the events `astream_with_tools()` emits
- [Custom Reasoning Patterns](agents/reasoning-patterns.md) -- graphs you can test the same way
