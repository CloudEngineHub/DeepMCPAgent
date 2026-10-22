"""``promptise.testing.ScriptedModel``: agents run on scripted replies.

Covers the reply forms, tool binding and the ``calls`` record, sync, async
and streamed calls, running out of replies, an agent run end to end with
``@tool`` functions, and the malformed replies.  The error a model gets for a
tool name the agent doesn't have is covered in ``test_unknown_tool_hint.py``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from promptise import build_agent, tool
from promptise.engine import GraphExecutionError
from promptise.testing import (
    Malformed,
    ModelCall,
    ScriptedModel,
    ScriptExhaustedError,
    ToolCall,
)

ORDERS = {"A-1001": {"order_id": "A-1001", "status": "shipped"}}


@tool
def get_order(order_id: str) -> dict:
    """Look up an order by its ID.

    Args:
        order_id: The order ID, for example "A-1001".
    """
    return ORDERS[order_id]


@tool
async def refund_order(order_id: str, amount: float) -> str:
    """Refund part of an order."""
    return f"Refunded {amount:.2f} EUR on {order_id}."


def _ask(text: str) -> dict[str, Any]:
    return {"messages": [{"role": "user", "content": text}]}


def _tool_results(messages: list[Any]) -> list[str]:
    return [str(m.content) for m in messages if isinstance(m, ToolMessage)]


# ---------------------------------------------------------------------------
# The model on its own
# ---------------------------------------------------------------------------


class TestReplies:
    async def test_replies_come_in_order(self) -> None:
        model = ScriptedModel(["one", "two"])
        assert (await model.ainvoke("a")).content == "one"
        assert model.invoke("b").content == "two"
        assert model.remaining == 0

    def test_replies_keyword(self) -> None:
        model = ScriptedModel(replies=["hi"])
        assert model.invoke("x").content == "hi"

    def test_tool_call_reply(self) -> None:
        model = ScriptedModel([ToolCall("get_order", {"order_id": "A-1001"})])
        reply = model.invoke("x")
        assert reply.tool_calls == [
            {
                "name": "get_order",
                "args": {"order_id": "A-1001"},
                "id": "call_1",
                "type": "tool_call",
            }
        ]

    def test_list_reply_with_text_and_several_calls(self) -> None:
        model = ScriptedModel(
            [
                [
                    "Checking both.",
                    ToolCall("get_order", {"order_id": "A-1"}),
                    ToolCall("get_order", {"order_id": "A-2"}, id="mine"),
                ]
            ]
        )
        reply = model.invoke("x")
        assert reply.content == "Checking both."
        assert [tc["id"] for tc in reply.tool_calls] == ["call_1", "mine"]

    def test_ai_message_reply_is_copied(self) -> None:
        message = AIMessage(content="as is")
        model = ScriptedModel([message], on_exhausted="repeat_last")
        first, second = model.invoke("x"), model.invoke("y")
        assert first.content == second.content == "as is"
        assert first is not message

    def test_rejects_a_reply_it_cannot_send(self) -> None:
        with pytest.raises(TypeError, match="reply 1"):
            ScriptedModel(["ok", 42])  # type: ignore[list-item]
        with pytest.raises(TypeError, match="wrap a single reply"):
            ScriptedModel(replies="ok")
        with pytest.raises(TypeError, match="list reply"):
            ScriptedModel([[ToolCall("a"), 3]])  # type: ignore[list-item]

    def test_tool_call_as_text_styles(self) -> None:
        body = '{"name": "get_order", "arguments": {"order_id": "A-1"}}'
        args = {"order_id": "A-1"}
        assert Malformed.tool_call_as_text("get_order", args).text == body
        assert Malformed.tool_call_as_text("get_order", args, style="tagged").text == (
            f"<tool_call>\n{body}\n</tool_call>"
        )
        assert Malformed.tool_call_as_text("get_order", args, style="fenced").text == (
            f"```json\n{body}\n```"
        )
        with pytest.raises(ValueError, match="style"):
            Malformed.tool_call_as_text("get_order", style="xml")  # type: ignore[arg-type]

    def test_unparseable_arguments_are_an_invalid_tool_call(self) -> None:
        model = ScriptedModel([Malformed.unparseable_arguments("get_order", '{"order_id": ')])
        reply = model.invoke("x")
        assert reply.tool_calls == []
        assert reply.invalid_tool_calls[0]["name"] == "get_order"
        assert reply.invalid_tool_calls[0]["args"] == '{"order_id": '
        assert reply.invalid_tool_calls[0]["id"] == "call_1"


class TestExhausted:
    async def test_raises_by_default(self) -> None:
        model = ScriptedModel(["only"])
        await model.ainvoke("a")
        with pytest.raises(ScriptExhaustedError, match="model call 2: the script has 1 reply"):
            await model.ainvoke("b")
        # The request is still recorded, with no reply.
        assert len(model.calls) == 2
        assert model.calls[1].reply is None

    def test_repeat_last(self) -> None:
        model = ScriptedModel(["a", "b"], on_exhausted="repeat_last")
        assert [model.invoke("x").content for _ in range(4)] == ["a", "b", "b", "b"]

    def test_empty_script_raises_even_with_repeat_last(self) -> None:
        with pytest.raises(ScriptExhaustedError, match="the script has 0 replies"):
            ScriptedModel([], on_exhausted="repeat_last").invoke("x")

    async def test_agent_run_fails_with_the_cause(self) -> None:
        model = ScriptedModel([ToolCall("get_order", {"order_id": "A-1001"})])
        agent = await build_agent(model=model, extra_tools=[get_order])
        try:
            with pytest.raises(GraphExecutionError) as caught:
                await agent.ainvoke(_ask("Where is A-1001?"))
        finally:
            await agent.shutdown()
        assert isinstance(caught.value.__cause__, ScriptExhaustedError)


class TestCallsRecord:
    def test_records_messages_and_bound_tools(self) -> None:
        model = ScriptedModel(["ok"])
        bound = model.bind_tools([get_order, refund_order])
        bound.invoke([HumanMessage(content="hello")])
        (call,) = model.calls
        assert isinstance(call, ModelCall)
        assert [m.content for m in call.messages] == ["hello"]
        assert call.tools == ["get_order", "refund_order"]
        schema = call.tool_schemas[0]["function"]
        assert schema["name"] == "get_order"
        assert schema["parameters"]["properties"]["order_id"]["type"] == "string"
        assert call.reply is not None and call.reply.content == "ok"

    def test_unbound_call_has_no_tools(self) -> None:
        model = ScriptedModel(["ok"])
        model.invoke("x")
        assert model.calls[0].tools == []
        assert model.calls[0].tool_schemas == []


class TestStreaming:
    async def test_astream_aggregates_to_the_reply(self) -> None:
        model = ScriptedModel(
            [["Let me check.", ToolCall("get_order", {"order_id": "A-1"})], "It has shipped."]
        )
        chunks = [chunk async for chunk in model.astream("x")]
        merged = chunks[0]
        for chunk in chunks[1:]:
            merged = merged + chunk
        assert merged.content == "Let me check."
        assert merged.tool_calls[0]["name"] == "get_order"
        assert merged.tool_calls[0]["args"] == {"order_id": "A-1"}

        words = [c.content for c in model.stream("y") if c.content]
        assert words == ["It ", "has ", "shipped."]

    async def test_streamed_unparseable_arguments_stay_invalid(self) -> None:
        model = ScriptedModel(
            [
                Malformed.unparseable_arguments("get_order"),
                # A cut-off object is read as far as it goes, as LangChain
                # reads a provider's stream.
                Malformed.unparseable_arguments("get_order", '{"order_id"'),
            ]
        )
        chunks = [chunk async for chunk in model.astream("x")]
        merged = chunks[0]
        for chunk in chunks[1:]:
            merged = merged + chunk
        assert merged.tool_calls == []
        assert merged.invalid_tool_calls[0]["name"] == "get_order"
        assert merged.invalid_tool_calls[0]["args"] == "{'arg': 'value'}"

        chunks = [chunk async for chunk in model.astream("y")]
        merged = chunks[0]
        for chunk in chunks[1:]:
            merged = merged + chunk
        assert merged.tool_calls[0]["args"] == {}


# ---------------------------------------------------------------------------
# Agents on a scripted model
# ---------------------------------------------------------------------------


class TestAgentRuns:
    async def test_tool_calling_agent_end_to_end(self) -> None:
        model = ScriptedModel(
            [
                ToolCall("get_order", {"order_id": "A-1001"}),
                ToolCall("refund_order", {"order_id": "A-1001", "amount": 20}),
                "Refunded 20 EUR on A-1001.",
            ]
        )
        agent = await build_agent(model=model, extra_tools=[get_order, refund_order])
        try:
            result = await agent.ainvoke(_ask("Refund 20 EUR on A-1001."))
        finally:
            await agent.shutdown()

        assert result["messages"][-1].content == "Refunded 20 EUR on A-1001."
        assert _tool_results(result["messages"]) == [
            json.dumps(ORDERS["A-1001"]),
            "Refunded 20.00 EUR on A-1001.",
        ]
        assert len(model.calls) == 3
        assert all(c.tools == ["get_order", "refund_order"] for c in model.calls)
        # The second request carries the first tool result.
        last = model.calls[1].messages[-1]
        assert isinstance(last, ToolMessage)
        assert last.tool_call_id == "call_1"
        assert model.remaining == 0

    async def test_streamed_agent_run(self) -> None:
        model = ScriptedModel(
            [ToolCall("get_order", {"order_id": "A-1001"}), "Order A-1001 has shipped."]
        )
        agent = await build_agent(model=model, extra_tools=[get_order])
        try:
            events = [e async for e in agent.astream_with_tools(_ask("Where is A-1001?"))]
        finally:
            await agent.shutdown()
        types = [e.type for e in events]
        assert types[:2] == ["tool_start", "tool_end"]
        assert types[-1] == "done"
        assert events[-1].full_response == "Order A-1001 has shipped."  # type: ignore[attr-defined]

    async def test_unknown_tool_then_the_model_corrects_itself(self) -> None:
        model = ScriptedModel(
            [
                ToolCall("lookup_order", {"order_id": "A-1001"}),
                ToolCall("get_order", {"order_id": "A-1001"}),
                "Order A-1001 has shipped.",
            ]
        )
        agent = await build_agent(model=model, extra_tools=[get_order, refund_order])
        try:
            result = await agent.ainvoke(_ask("Where is A-1001?"))
        finally:
            await agent.shutdown()

        error = model.calls[1].messages[-1]
        assert isinstance(error, ToolMessage)
        assert error.status == "error"
        assert error.content == (
            "Error: Unknown tool 'lookup_order'. "
            "Call one of the available tools: get_order, refund_order."
        )
        assert result["messages"][-1].content == "Order A-1001 has shipped."

    async def test_invalid_arguments_then_the_model_corrects_itself(self) -> None:
        model = ScriptedModel(
            [
                ToolCall("get_order", {"id": "A-1001"}),
                ToolCall("get_order", {"order_id": "A-1001"}),
                "Order A-1001 has shipped.",
            ]
        )
        agent = await build_agent(model=model, extra_tools=[get_order])
        try:
            result = await agent.ainvoke(_ask("Where is A-1001?"))
        finally:
            await agent.shutdown()

        first, second = _tool_results(result["messages"])
        assert first.startswith("Invalid arguments for tool 'get_order':")
        assert "order_id" in first
        assert second == json.dumps(ORDERS["A-1001"])
        assert result["messages"][-1].content == "Order A-1001 has shipped."

    async def test_unparseable_arguments_then_the_model_corrects_itself(self) -> None:
        model = ScriptedModel(
            [
                Malformed.unparseable_arguments("get_order", "{'order_id': 'A-1001'}"),
                ToolCall("get_order", {"order_id": "A-1001"}),
                "Order A-1001 has shipped.",
            ]
        )
        agent = await build_agent(model=model, extra_tools=[get_order])
        try:
            result = await agent.ainvoke(_ask("Where is A-1001?"))
        finally:
            await agent.shutdown()

        # The call that can't be read doesn't run: the model gets an error
        # for it (answering its call id) and calls the tool again.
        error = model.calls[1].messages[-1]
        assert isinstance(error, ToolMessage)
        assert error.status == "error"
        assert error.tool_call_id == "call_1"
        assert error.content == (
            "Error: The arguments for 'get_order' were not valid JSON (Expecting property "
            "name enclosed in double quotes at line 1 column 2). Call the tool again with "
            "a JSON object that matches its parameters: order_id (required)."
        )
        assert _tool_results(result["messages"])[1] == json.dumps(ORDERS["A-1001"])
        assert result["messages"][-1].content == "Order A-1001 has shipped."

    async def test_streamed_unparseable_arguments_then_the_model_corrects_itself(self) -> None:
        model = ScriptedModel(
            [
                Malformed.unparseable_arguments("get_order"),
                ToolCall("get_order", {"order_id": "A-1001"}),
                "Order A-1001 has shipped.",
            ]
        )
        agent = await build_agent(model=model, extra_tools=[get_order])
        try:
            events = [e async for e in agent.astream_with_tools(_ask("Where is A-1001?"))]
        finally:
            await agent.shutdown()
        ends = [e for e in events if e.type == "tool_end"]
        assert [e.success for e in ends] == [False, True]  # type: ignore[attr-defined]
        assert events[-1].full_response == "Order A-1001 has shipped."  # type: ignore[attr-defined]

    async def test_tool_call_as_text_ends_the_run_as_the_answer(self) -> None:
        reply = Malformed.tool_call_as_text("get_order", {"order_id": "A-1001"})
        model = ScriptedModel([reply])
        agent = await build_agent(model=model, extra_tools=[get_order])
        try:
            result = await agent.ainvoke(_ask("Where is A-1001?"))
        finally:
            await agent.shutdown()
        # No tool ran: the text is the run's answer.
        assert _tool_results(result["messages"]) == []
        assert result["messages"][-1].content == reply.text
        assert len(model.calls) == 1
