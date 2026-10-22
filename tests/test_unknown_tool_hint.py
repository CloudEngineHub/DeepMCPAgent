"""The error a model gets for a call to a tool the agent doesn't have.

It names the tools the agent can call, closest names first and at most 20
of them, so a model that invented or misspelled a name can correct itself.
"""

from __future__ import annotations

from typing import Any

from langchain_core.messages import ToolMessage

from promptise import build_agent, tool
from promptise.engine import GraphState, NodeResult, PromptGraph, ToolNode, node
from promptise.engine.nodes import _unknown_tool_message
from promptise.testing import ScriptedModel, ToolCall


@tool
def get_order(order_id: str) -> dict:
    """Look up an order by its ID."""
    return {"order_id": order_id, "status": "shipped"}


@tool
def refund_order(order_id: str, amount: float) -> str:
    """Refund part of an order."""
    return f"Refunded {amount:.2f} EUR on {order_id}."


def _ask(text: str) -> dict[str, Any]:
    return {"messages": [{"role": "user", "content": text}]}


def _tool_results(messages: list[Any]) -> list[str]:
    return [str(m.content) for m in messages if isinstance(m, ToolMessage)]


class TestUnknownToolMessage:
    def test_close_name_first_with_a_suggestion(self) -> None:
        text = _unknown_tool_message("get_ordr", ["refund_order", "get_order"])
        assert text == (
            "Error: Unknown tool 'get_ordr'. Did you mean 'get_order'? "
            "Call one of the available tools: get_order, refund_order."
        )

    def test_suggests_a_misspelling_ignoring_case(self) -> None:
        for called in ("getOrder", "GET_ORDER", "get-order", "get_orders"):
            text = _unknown_tool_message(called, ["refund_order", "get_order"])
            assert "Did you mean 'get_order'?" in text, called

    def test_does_not_suggest_a_tool_that_only_shares_a_word(self) -> None:
        # cancel_order is not a misspelling of get_order: no suggestion, but
        # the closer names still come first.
        text = _unknown_tool_message("cancel_order", ["list_users", "get_order"])
        assert text == (
            "Error: Unknown tool 'cancel_order'. "
            "Call one of the available tools: get_order, list_users."
        )

    def test_caps_the_list(self) -> None:
        names = [f"tool_{i:02d}" for i in range(30)]
        text = _unknown_tool_message("zzz", names)
        assert "tool_19" in text and "tool_20" not in text
        assert text.endswith("tool_19 (and 10 more).")

    def test_a_close_name_past_the_cap_is_listed(self) -> None:
        names = [f"alpha_{i:02d}" for i in range(30)] + ["search_orders"]
        text = _unknown_tool_message("search_order", names)
        assert "Did you mean 'search_orders'?" in text
        assert "tools: search_orders, alpha_00" in text
        assert text.endswith("(and 11 more).")

    def test_no_tools(self) -> None:
        assert _unknown_tool_message("x", []) == (
            "Error: Unknown tool 'x'. No tools are available; answer without one."
        )


class TestInAgentRuns:
    async def test_prompt_node_lists_the_agent_tools(self) -> None:
        model = ScriptedModel([ToolCall("cancel_order", {"order_id": "A-1001"}), "Sorry."])
        agent = await build_agent(model=model, extra_tools=[get_order, refund_order])
        try:
            result = await agent.ainvoke(_ask("Cancel A-1001."))
        finally:
            await agent.shutdown()
        assert _tool_results(result["messages"]) == [
            "Error: Unknown tool 'cancel_order'. "
            "Call one of the available tools: get_order, refund_order."
        ]

    async def test_tool_node_lists_its_tools(self) -> None:
        @node("agent")
        async def agent_step(state: GraphState, config: dict[str, Any]) -> NodeResult:
            reply = await config["_engine_model"].ainvoke(state.messages)
            state.messages.append(reply)
            return NodeResult(
                node_name="agent",
                messages_added=[reply],
                next_node="tools" if reply.tool_calls else "__end__",
            )

        graph = PromptGraph("agent-tools", mode="static")
        graph.add_node(agent_step)
        graph.add_node(ToolNode("tools", tools=[get_order], default_next="agent"))
        graph.set_entry("agent")

        model = ScriptedModel([ToolCall("fetch_order", {"order_id": "A-1001"}), "Done."])
        agent = await build_agent(model=model, agent_pattern=graph)
        try:
            result = await agent.ainvoke(_ask("Where is A-1001?"))
        finally:
            await agent.shutdown()
        assert _tool_results(result["messages"]) == [
            "Error: Unknown tool 'fetch_order'. Call one of the available tools: get_order."
        ]
