"""Tool calls whose arguments are not valid JSON (``invalid_tool_calls``).

LangChain keeps such a call in ``AIMessage.invalid_tool_calls``.  The
built-in nodes answer it with an error the model can act on instead of
ending the run on that message: the tool loop goes on, the call counts as a
failed one, and every call id of the message gets exactly one tool result.
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.callbacks import AsyncCallbackHandler
from langchain_core.messages import AIMessage, HumanMessage, ToolMessage

from promptise import build_agent, tool
from promptise.engine import (
    GraphIterationLimitError,
    GraphState,
    NodeResult,
    PromptGraph,
    PromptGraphEngine,
    ToolNode,
    node,
)
from promptise.engine.nodes import _argument_problem, _invalid_call_message
from promptise.fallback import _as_chunk
from promptise.testing import Malformed, ScriptedModel, ToolCall


@tool
def get_order(order_id: str, include_items: bool = False) -> dict:
    """Look up an order by its ID."""
    return {"order_id": order_id, "status": "shipped"}


def _ask(text: str) -> dict[str, Any]:
    return {"messages": [{"role": "user", "content": text}]}


def _tool_messages(messages: list[Any]) -> list[ToolMessage]:
    return [m for m in messages if isinstance(m, ToolMessage)]


def _assert_paired(messages: list[Any]) -> None:
    """Every assistant tool call id (valid or not) has exactly one tool result."""
    asked = [
        call["id"]
        for m in messages
        if isinstance(m, AIMessage)
        for call in [*m.tool_calls, *m.invalid_tool_calls]
    ]
    answered = [m.tool_call_id for m in messages if isinstance(m, ToolMessage)]
    assert sorted(asked) == sorted(answered)
    assert len(set(answered)) == len(answered)


class _Results:
    """Engine hook that keeps every NodeResult."""

    def __init__(self) -> None:
        self.results: list[NodeResult] = []

    async def post_node(self, node: Any, result: NodeResult, state: GraphState) -> NodeResult:
        self.results.append(result)
        return result


class _ToolErrors(AsyncCallbackHandler):
    def __init__(self) -> None:
        self.started: list[str] = []
        self.errors: list[tuple[str, str]] = []

    async def on_tool_start(self, serialized: dict[str, Any], input_str: str, **kw: Any) -> None:
        self.started.append(serialized["name"])

    async def on_tool_error(self, error: BaseException, **kw: Any) -> None:
        self.errors.append((type(error).__name__, str(error)))


class TestMessage:
    def test_names_the_problem_and_the_parameters(self) -> None:
        text = _invalid_call_message("get_order", "{'order_id': 'A-1001'}", get_order)
        assert text == (
            "Error: The arguments for 'get_order' were not valid JSON (Expecting property "
            "name enclosed in double quotes at line 1 column 2). Call the tool again with a "
            "JSON object that matches its parameters: order_id (required), include_items."
        )

    @pytest.mark.parametrize(
        ("raw", "problem"),
        [
            ("", "no arguments were sent"),
            (None, "no arguments were sent"),
            ('{"order_id": ', "Expecting value at line 1 column 14"),
            ('["A-1001"]', "they are an array, not an object"),
            ('"A-1001"', "they are a string, not an object"),
            ("12", "they are a number, not an object"),
            ("null", "they are null, not an object"),
        ],
    )
    def test_argument_problem(self, raw: Any, problem: str) -> None:
        assert _argument_problem(raw) == problem


class TestAgentRuns:
    async def test_the_model_gets_an_error_and_calls_again(self) -> None:
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

        assert result["messages"][-1].content == "Order A-1001 has shipped."
        error = model.calls[1].messages[-1]
        assert isinstance(error, ToolMessage)
        assert error.status == "error"
        assert error.tool_call_id == "call_1"
        assert error.name == "get_order"
        assert error.content.startswith("Error: The arguments for 'get_order' were not valid JSON")
        assert error.content.endswith("parameters: order_id (required), include_items.")
        _assert_paired(result["messages"])
        assert len(model.calls) == 3

    async def test_streamed_run_recovers_the_same_way(self) -> None:
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
        tool_events = [(e.type, getattr(e, "success", None)) for e in events if "tool" in e.type]
        assert tool_events == [
            ("tool_start", None),
            ("tool_end", False),
            ("tool_start", None),
            ("tool_end", True),
        ]
        assert events[-1].type == "done"
        assert events[-1].full_response == "Order A-1001 has shipped."  # type: ignore[attr-defined]
        error = model.calls[1].messages[-1]
        assert isinstance(error, ToolMessage) and error.status == "error"
        assert "were not valid JSON" in str(error.content)

    async def test_unknown_tool_with_invalid_arguments(self) -> None:
        model = ScriptedModel(
            [
                Malformed.unparseable_arguments("lookup_order"),
                ToolCall("get_order", {"order_id": "A-1001"}),
                "Shipped.",
            ]
        )
        agent = await build_agent(model=model, extra_tools=[get_order])
        try:
            result = await agent.ainvoke(_ask("Where is A-1001?"))
        finally:
            await agent.shutdown()
        first = _tool_messages(result["messages"])[0]
        assert first.content == (
            "Error: Unknown tool 'lookup_order'. Call one of the available tools: get_order."
        )
        assert first.status == "error"
        assert result["messages"][-1].content == "Shipped."

    async def test_valid_and_invalid_calls_in_one_turn(self) -> None:
        reply = AIMessage(
            content="",
            tool_calls=[{"name": "get_order", "args": {"order_id": "A-1"}, "id": "ok_1"}],
            invalid_tool_calls=[
                {
                    "type": "invalid_tool_call",
                    "name": "get_order",
                    "args": "{order_id: A-2}",
                    "id": "bad_1",
                    "error": None,
                },
                # No id: one is given to the call and its result.
                {
                    "type": "invalid_tool_call",
                    "name": "get_order",
                    "args": "{",
                    "id": None,
                    "error": None,
                },
            ],
        )
        model = ScriptedModel([reply, "Done."])
        agent = await build_agent(model=model, extra_tools=[get_order])
        try:
            result = await agent.ainvoke(_ask("A-1 and A-2?"))
        finally:
            await agent.shutdown()
        results = _tool_messages(result["messages"])
        assert [m.status for m in results] == ["success", "error", "error"]
        assert results[1].tool_call_id == "bad_1"
        _assert_paired(result["messages"])
        # The id given to the call without one is on the assistant message too.
        sent = model.calls[1].messages
        ai = next(m for m in sent if isinstance(m, AIMessage))
        assert ai.invalid_tool_calls[1]["id"] == results[2].tool_call_id

    async def test_iteration_limit_still_applies(self) -> None:
        model = ScriptedModel(
            [Malformed.unparseable_arguments("get_order")], on_exhausted="repeat_last"
        )
        agent = await build_agent(model=model, extra_tools=[get_order], max_agent_iterations=3)
        try:
            with pytest.raises(GraphIterationLimitError) as info:
                await agent.ainvoke(_ask("Where is A-1001?"))
        finally:
            await agent.shutdown()
        assert len(model.calls) == 3
        assert info.value.pending_tools == ["get_order"]


class TestEngine:
    async def test_node_result_and_callbacks(self) -> None:
        model = ScriptedModel(
            [Malformed.unparseable_arguments("get_order", "{'order_id': 1}"), "Sorry."]
        )
        recorder = _Results()
        engine = PromptGraphEngine(
            graph=PromptGraph.react(tools=[get_order]), model=model, hooks=[recorder]
        )
        handler = _ToolErrors()
        await engine.ainvoke(
            {"messages": [HumanMessage(content="Where is A-1001?")]},
            config={"callbacks": [handler]},
        )
        first = recorder.results[0]
        assert first.tool_calls_failed == 1
        assert first.transition_reason == "tool_calls_present"
        (record,) = first.tool_calls
        assert record["name"] == "get_order"
        assert record["success"] is False
        assert record["invalid"] is True
        assert record["args"] == {}
        assert record["raw_args"] == "{'order_id': 1}"
        assert record["result"].startswith("Error: The arguments for 'get_order'")
        assert handler.started == ["get_order"]
        assert handler.errors[0][0] == "ToolArgumentError"
        assert engine.last_report is not None
        assert engine.last_report.error is None

    async def test_tool_node_answers_invalid_calls(self) -> None:
        @node("agent")
        async def agent_step(state: GraphState, config: dict[str, Any]) -> NodeResult:
            reply = await config["_engine_model"].ainvoke(state.messages)
            state.messages.append(reply)
            asks = reply.tool_calls or reply.invalid_tool_calls
            return NodeResult(
                node_name="agent",
                messages_added=[reply],
                next_node="tools" if asks else "__end__",
            )

        graph = PromptGraph("agent-tools", mode="static")
        graph.add_node(agent_step)
        graph.add_node(ToolNode("tools", tools=[get_order], default_next="agent"))
        graph.set_entry("agent")

        model = ScriptedModel(
            [
                Malformed.unparseable_arguments("get_order"),
                ToolCall("get_order", {"order_id": "A-1001"}),
                "Done.",
            ]
        )
        agent = await build_agent(model=model, agent_pattern=graph)
        try:
            result = await agent.ainvoke(_ask("Where is A-1001?"))
        finally:
            await agent.shutdown()
        first, second = _tool_messages(result["messages"])
        assert first.status == "error"
        assert first.content.startswith("Error: The arguments for 'get_order' were not valid JSON")
        assert second.status == "success"
        assert result["messages"][-1].content == "Done."
        _assert_paired(result["messages"])


class TestFallbackChunk:
    def test_invalid_calls_survive_the_chunk(self) -> None:
        message = AIMessage(
            content="",
            invalid_tool_calls=[
                {
                    "type": "invalid_tool_call",
                    "name": "get_order",
                    "args": "{'order_id': 'A-1'}",
                    "id": "bad_1",
                    "error": None,
                }
            ],
        )
        chunk = _as_chunk(message)
        assert chunk.invalid_tool_calls[0]["id"] == "bad_1"  # type: ignore[attr-defined]
        assert chunk.invalid_tool_calls[0]["args"] == "{'order_id': 'A-1'}"  # type: ignore[attr-defined]


class TestCompaction:
    def test_the_error_stays_in_the_compacted_view(self) -> None:
        from promptise.engine.compaction import ContextCompaction, build_compacted_view

        question = HumanMessage(content="Where is A-1001?")
        ask = AIMessage(
            content="",
            invalid_tool_calls=[
                {
                    "type": "invalid_tool_call",
                    "name": "get_order",
                    "args": "{'order_id': 'A-1001'}",
                    "id": "bad_1",
                    "error": None,
                }
            ],
        )
        answer = ToolMessage(content="Error: ...", tool_call_id="bad_1", name="get_order")
        for scoped in (False, True):
            view = build_compacted_view(
                [question, ask, answer],
                question=question,
                head=[question],
                settings=ContextCompaction(),
                scoped=scoped,
            )
            assert view[-2:] == [ask, answer], scoped
