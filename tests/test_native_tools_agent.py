"""Native ``@tool`` functions in a running agent (scripted chat models, no API key).

The model is offered the tool's schema, calls it, and reads its result;
``ToolError``, invalid arguments, other exceptions and timeouts reach the
model as tool errors.  Approval policies, guardrails, tracing, events,
streaming, cross-agent delegation and code-action work with native tools as
they do with LangChain tools.
"""

from __future__ import annotations

import asyncio
import json
from typing import Annotated, Any

import pytest
from _scripted_model import Scripted
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.language_models.fake_chat_models import GenericFakeChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool as lc_tool
from langchain_core.utils.function_calling import convert_to_openai_tool
from pydantic import BaseModel, Field, PrivateAttr

from promptise import (
    ApprovalDecision,
    ApprovalPolicy,
    CallbackSink,
    CallerContext,
    EventNotifier,
    ToolError,
    build_agent,
    tool,
)
from promptise.config import HTTPServerSpec
from promptise.cross_agent import CrossAgent
from promptise.engine.code_action import CodeActionNode
from promptise.engine.state import GraphState, NodeResult
from promptise.events import AgentEvent
from promptise.function_tools import FunctionTool
from promptise.streaming import ToolEndEvent, ToolStartEvent

TIMEOUT = 15.0


# ---------------------------------------------------------------------------
# Scripted model
# ---------------------------------------------------------------------------


class _Model(BaseChatModel):
    """Emits the scripted tool calls (one turn each), then answers ``done``.

    Records the tools it was bound to and the messages of every turn.
    """

    _script: list[list[dict[str, Any]]] = PrivateAttr()
    _seen: list[list[BaseMessage]] = PrivateAttr()
    _bound: list[Any] = PrivateAttr()
    _bindings: list[list[str]] = PrivateAttr()

    def __init__(self, *turns: list[dict[str, Any]]) -> None:
        super().__init__()
        self._script = list(turns)
        self._seen = []
        self._bound = []
        self._bindings = []

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> _Model:
        self._bound = list(tools)
        self._bindings.append([convert_to_openai_tool(t)["function"]["name"] for t in tools])
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        self._seen.append(list(messages))
        if self._script:
            message = AIMessage(content="", tool_calls=self._script.pop(0))
        else:
            message = AIMessage(content="done")
        return ChatResult(generations=[ChatGeneration(message=message)])

    def offered(self, name: str) -> dict[str, Any]:
        """The function definition the model was given for tool *name*."""
        for t in self._bound:
            spec = convert_to_openai_tool(t)["function"]
            if spec["name"] == name:
                return spec
        raise KeyError(name)

    def tool_results(self) -> list[ToolMessage]:
        last = self._seen[-1] if self._seen else []
        return [m for m in last if isinstance(m, ToolMessage)]


_ids = iter(range(10_000))


def _turn(name: str, args: dict[str, Any]) -> list[dict[str, Any]]:
    return [{"name": name, "args": args, "id": f"call_{next(_ids)}"}]


async def _run(agent: Any, **kwargs: Any) -> Any:
    try:
        return await asyncio.wait_for(
            agent.ainvoke({"messages": [HumanMessage(content="go")]}, **kwargs), TIMEOUT
        )
    finally:
        await agent.shutdown()


# ---------------------------------------------------------------------------
# Tools
# ---------------------------------------------------------------------------

calls: list[tuple[str, Any]] = []


class Line(BaseModel):
    sku: str
    qty: int = Field(ge=1)


@tool
async def get_order(order_id: str, caller: CallerContext) -> dict:
    """Look up an order by its ID.

    Args:
        order_id: The order ID, for example "A-1001".
    """
    calls.append(("get_order", order_id))
    if order_id == "A-404":
        raise ToolError(f"No order {order_id}.")
    return {"order_id": order_id, "status": "shipped", "owner": caller.user_id}


@tool(name="refund_order", timeout=5)
def refund(
    order_id: str,
    amount: Annotated[float, Field(gt=0, description="Amount to refund.")],
    caller: CallerContext,
) -> str:
    """Refund part of an order."""
    calls.append(("refund_order", amount))
    return f"Refunded {amount:.2f} on {order_id} for {caller.user_id}"


@tool
def add_lines(order_id: str, lines: list[Line]) -> dict:
    """Add lines to an order."""
    return {"order_id": order_id, "skus": [line.sku for line in lines]}


@tool
def explode(order_id: str) -> str:
    """Always fails."""
    raise RuntimeError("database exploded")


@tool(timeout=0.1)
async def too_slow() -> str:
    """Never finishes in time."""
    await asyncio.sleep(5)
    return "late"


def plain_status(order_id: str) -> str:
    """The status of an order (a plain function, no decorator)."""
    return f"{order_id}: shipped"


@pytest.fixture(autouse=True)
def _clear_calls():
    calls.clear()


# ---------------------------------------------------------------------------
# The model calls the tool and reads the result
# ---------------------------------------------------------------------------


class TestAgentRun:
    async def test_model_is_offered_the_schema(self):
        model = _Model()
        agent = await build_agent(model=model, extra_tools=[get_order, refund])
        await _run(agent)
        spec = model.offered("get_order")
        assert spec["description"] == "Look up an order by its ID."
        assert spec["parameters"] == {
            "properties": {
                "order_id": {"description": 'The order ID, for example "A-1001".', "type": "string"}
            },
            "required": ["order_id"],
            "type": "object",
        }
        refund_spec = model.offered("refund_order")
        assert refund_spec["parameters"]["properties"]["amount"] == {
            "description": "Amount to refund.",
            "exclusiveMinimum": 0,
            "type": "number",
        }
        assert "caller" not in json.dumps(refund_spec)

    async def test_model_calls_the_tool_and_reads_the_result(self):
        model = _Model(_turn("get_order", {"order_id": "A-1001"}))
        agent = await build_agent(model=model, extra_tools=[get_order])
        result = await _run(agent, caller=CallerContext(user_id="alice"))
        [msg] = model.tool_results()
        assert msg.status == "success"
        assert json.loads(msg.content) == {
            "order_id": "A-1001",
            "status": "shipped",
            "owner": "alice",
        }
        assert result["messages"][-1].content == "done"
        assert calls == [("get_order", "A-1001")]

    async def test_sync_tool_with_caller_and_coercion(self):
        model = _Model(_turn("refund_order", {"order_id": "A-1", "amount": "12.5"}))
        agent = await build_agent(model=model, extra_tools=[refund])
        await _run(agent, caller=CallerContext(user_id="bob"))
        [msg] = model.tool_results()
        assert msg.content == "Refunded 12.50 on A-1 for bob"

    async def test_nested_models(self):
        model = _Model(
            _turn("add_lines", {"order_id": "A-1", "lines": [{"sku": "X", "qty": 1}, {"sku": "Y"}]})
        )
        agent = await build_agent(model=model, extra_tools=[add_lines])
        await _run(agent)
        [msg] = model.tool_results()
        assert msg.status == "error"
        assert "- lines.1.qty: Field required" in msg.content

    async def test_plain_function_in_extra_tools(self):
        model = _Model(_turn("plain_status", {"order_id": "A-7"}))
        agent = await build_agent(model=model, extra_tools=[plain_status])
        assert isinstance(agent.tools[0], FunctionTool)
        await _run(agent)
        assert model.offered("plain_status")["description"] == (
            "The status of an order (a plain function, no decorator)."
        )
        assert model.tool_results()[0].content == "A-7: shipped"

    async def test_native_and_langchain_tools_together(self):
        @lc_tool
        def legacy(order_id: str) -> str:
            """A LangChain tool."""
            return f"legacy {order_id}"

        model = _Model(
            _turn("legacy", {"order_id": "A-1"}), _turn("plain_status", {"order_id": "A-2"})
        )
        agent = await build_agent(model=model, extra_tools=[legacy, plain_status, get_order])
        assert agent.tool_names == ["legacy", "plain_status", "get_order"]
        await _run(agent)
        contents = [m.content for m in model._seen[-1] if isinstance(m, ToolMessage)]
        assert contents == ["legacy A-1", "A-2: shipped"]

    async def test_semantic_tool_selection_covers_native_tools(self):
        pytest.importorskip("sentence_transformers")
        from promptise import OptimizationLevel, ToolOptimizationConfig

        model = _Model(_turn("refund_order", {"order_id": "A-1", "amount": 5}))
        agent = await build_agent(
            model=model,
            extra_tools=[get_order, refund, get_payment, list_skus, describe, plain_status],
            optimize_tools=ToolOptimizationConfig(
                level=OptimizationLevel.SEMANTIC, semantic_top_k=2
            ),
        )
        try:
            await asyncio.wait_for(
                agent.ainvoke(
                    {"messages": [HumanMessage(content="Refund 5 EUR on order A-1, please.")]},
                    caller=CallerContext(user_id="hana"),
                ),
                TIMEOUT,
            )
        finally:
            await agent.shutdown()
        first = model._bindings[0]
        assert "refund_order" in first
        assert len(first) <= 3  # the two best matches and the request_more_tools fallback
        assert model.tool_results()[0].content == "Refunded 5.00 on A-1 for hana"

    async def test_invalid_extra_tools_fail_before_connecting(self):
        unreachable = {"orders": HTTPServerSpec(url="http://127.0.0.1:9/mcp")}
        with pytest.raises(TypeError, match=r"extra_tools\[1\] must be a function"):
            await build_agent(model=_Model(), servers=unreachable, extra_tools=[get_order, 42])


# ---------------------------------------------------------------------------
# Errors reach the model
# ---------------------------------------------------------------------------


class TestErrorsReachTheModel:
    async def test_tool_error(self):
        model = _Model(_turn("get_order", {"order_id": "A-404"}))
        agent = await build_agent(model=model, extra_tools=[get_order])
        await _run(agent)
        [msg] = model.tool_results()
        assert msg.status == "error"
        assert json.loads(msg.content) == {
            "error": {"code": "TOOL_ERROR", "message": "No order A-404.", "retryable": False}
        }

    async def test_invalid_arguments(self):
        model = _Model(_turn("refund_order", {"order_id": "A-1", "amount": -3}))
        agent = await build_agent(model=model, extra_tools=[refund])
        await _run(agent)
        [msg] = model.tool_results()
        assert msg.status == "error"
        assert msg.content == (
            "Invalid arguments for tool 'refund_order':\n"
            "- amount: Input should be greater than 0 (got -3)\n"
            "Correct the arguments and call the tool again."
        )
        assert calls == []  # the function never ran

    async def test_other_exception(self):
        model = _Model(_turn("explode", {"order_id": "A-1"}))
        agent = await build_agent(model=model, extra_tools=[explode])
        await _run(agent)
        [msg] = model.tool_results()
        assert msg.status == "error"
        assert msg.content == "Error: RuntimeError: database exploded"

    async def test_timeout(self):
        model = _Model(_turn("too_slow", {}))
        agent = await build_agent(model=model, extra_tools=[too_slow])
        await _run(agent)
        [msg] = model.tool_results()
        assert msg.status == "error"
        error = json.loads(msg.content)["error"]
        assert error["code"] == "TIMEOUT"
        # Not marked read-only or idempotent: the model must not just retry.
        assert error["retryable"] is False
        assert "outcome is unknown" in error["message"]

    async def test_an_unknown_tool_name_lists_the_real_ones(self):
        model = _Model(_turn("refund_ordr", {"order_id": "A-1"}))
        agent = await build_agent(model=model, extra_tools=[refund, explode])
        await _run(agent)
        [msg] = model.tool_results()
        assert msg.content == (
            "Error: Unknown tool 'refund_ordr'. Did you mean 'refund_order'? "
            "Call one of the available tools: refund_order, explode."
        )

    async def test_model_corrects_the_call(self):
        model = _Model(
            _turn("refund_order", {"order_id": "A-1", "amount": 0}),
            _turn("refund_order", {"order_id": "A-1", "amount": 5}),
        )
        agent = await build_agent(model=model, extra_tools=[refund])
        await _run(agent, caller=CallerContext(user_id="carol"))
        results = [m for m in model._seen[-1] if isinstance(m, ToolMessage)]
        assert [m.status for m in results] == ["error", "success"]
        assert results[1].content == "Refunded 5.00 on A-1 for carol"


# ---------------------------------------------------------------------------
# Tracing, events and streaming
# ---------------------------------------------------------------------------


class _Streaming(GenericFakeChatModel):
    def bind_tools(self, tools: Any, **kwargs: Any) -> _Streaming:
        return self


def _streaming(*turns: Any) -> _Streaming:
    messages = []
    for i, turn in enumerate(turns):
        if isinstance(turn, tuple):
            name, args = turn
            messages.append(
                AIMessage(content="", tool_calls=[{"id": f"s{i}", "name": name, "args": args}])
            )
        else:
            messages.append(AIMessage(content=turn))
    return _Streaming(messages=iter(messages), disable_streaming=True)


class TestObservability:
    async def test_trace_tools(self, capsys):
        model = _Model(
            _turn("get_order", {"order_id": "A-1"}), _turn("get_order", {"order_id": "A-404"})
        )
        agent = await build_agent(model=model, extra_tools=[get_order], trace_tools=True)
        await _run(agent, caller=CallerContext(user_id="dan"))
        out = capsys.readouterr().out
        assert "→ Invoking tool: get_order with {'order_id': 'A-1'}" in out
        assert '✔ Tool result from get_order: {"order_id": "A-1"' in out
        assert "✖ get_order error: TOOL_ERROR: No order A-404." in out

    async def test_tool_error_event(self):
        seen: list[AgentEvent] = []
        notifier = EventNotifier(sinks=[CallbackSink(seen.append)])
        model = _streaming(("get_order", {"order_id": "A-404"}), "done")
        agent = await build_agent(model=model, extra_tools=[get_order], events=notifier)
        await _run(agent)
        [event] = [e for e in seen if e.event_type == "tool.error"]
        assert event.data["tool_name"] == "get_order"
        assert event.data["code"] == "TOOL_ERROR"
        assert event.data["error"] == "No order A-404."
        assert event.data["retryable"] is False

    async def test_streaming_tool_events(self):
        model = _streaming(
            ("get_order", {"order_id": "A-1"}), ("get_order", {"order_id": "A-404"}), "done"
        )
        agent = await build_agent(model=model, extra_tools=[get_order])
        try:
            events = [
                e
                async for e in agent.astream_with_tools(
                    {"messages": [HumanMessage(content="go")]},
                    caller=CallerContext(user_id="eve"),
                )
            ]
        finally:
            await agent.shutdown()
        starts = [e for e in events if isinstance(e, ToolStartEvent)]
        ends = [e for e in events if isinstance(e, ToolEndEvent)]
        assert [s.arguments for s in starts] == [{"order_id": "A-1"}, {"order_id": "A-404"}]
        assert [e.success for e in ends] == [True, False]
        assert ends[1].tool_summary == "No order A-404."


# ---------------------------------------------------------------------------
# Approval and guardrails
# ---------------------------------------------------------------------------


class TestApproval:
    async def test_denied_call_never_runs(self):
        requests = []

        async def deny(request):
            requests.append(request)
            return ApprovalDecision(approved=False, reason="Refunds need a manager.")

        model = _Model(_turn("refund_order", {"order_id": "A-1", "amount": 20}))
        agent = await build_agent(
            model=model,
            extra_tools=[refund, get_order],
            approval=ApprovalPolicy(tools=["refund_order"], handler=deny),
        )
        await _run(agent)
        [request] = requests
        assert request.tool_name == "refund_order"
        assert request.arguments == {"order_id": "A-1", "amount": 20.0}
        assert model.tool_results()[0].content == "DENIED: Refunds need a manager."
        assert calls == []

    async def test_approved_call_runs_with_the_reviewers_changes(self):
        async def lower(request):
            return ApprovalDecision(approved=True, modified_arguments={"amount": 5})

        model = _Model(_turn("refund_order", {"order_id": "A-1", "amount": 20}))
        agent = await build_agent(
            model=model,
            extra_tools=[refund],
            approval=ApprovalPolicy(tools=["refund_order"], handler=lower),
            trace_tools=True,
        )
        await _run(agent, caller=CallerContext(user_id="fred"))
        assert calls == [("refund_order", 5.0)]
        content = model.tool_results()[0].content
        assert "A reviewer changed amount: 20.0 -> 5." in content
        assert content.endswith("Refunded 5.00 on A-1 for fred")

    async def test_invalid_call_is_rejected_before_asking(self):
        requests = []

        async def approve(request):
            requests.append(request)
            return ApprovalDecision(approved=True)

        model = _Model(_turn("refund_order", {"order_id": "A-1", "amount": -1}))
        agent = await build_agent(
            model=model,
            extra_tools=[refund],
            approval=ApprovalPolicy(tools=["refund_order"], handler=approve),
        )
        await _run(agent)
        assert requests == []
        assert model.tool_results()[0].content.startswith("Invalid arguments for tool")

    async def test_other_tools_are_not_gated(self):
        async def deny(request):
            return ApprovalDecision(approved=False)

        model = _Model(_turn("get_order", {"order_id": "A-1"}))
        agent = await build_agent(
            model=model,
            extra_tools=[refund, get_order],
            approval=ApprovalPolicy(tools=["refund_*"], handler=deny),
        )
        await _run(agent)
        assert model.tool_results()[0].status == "success"


class _CardRedactor:
    """A guard that redacts card numbers from tool results."""

    scan_tool_results = True

    def __init__(self) -> None:
        self.scanned: list[tuple[str, str]] = []

    async def check_input(self, text: str) -> str:
        return text

    async def check_output(self, output: Any) -> Any:
        return output

    async def check_tool_result(self, tool_name: str, text: str) -> str:
        self.scanned.append((tool_name, text))
        return text.replace("4111 1111 1111 1111", "[CARD]")


@tool
def get_payment(order_id: str) -> dict:
    """The payment of an order."""
    return {"order_id": order_id, "card": "4111 1111 1111 1111"}


class TestGuardrails:
    async def test_tool_result_is_scanned_before_the_model_reads_it(self):
        guard = _CardRedactor()
        model = _Model(_turn("get_payment", {"order_id": "A-1"}))
        agent = await build_agent(model=model, extra_tools=[get_payment], guardrails=guard)
        await _run(agent)
        assert guard.scanned == [
            ("get_payment", '{"order_id": "A-1", "card": "4111 1111 1111 1111"}')
        ]
        [msg] = model.tool_results()
        assert msg.content == '{"order_id": "A-1", "card": "[CARD]"}'


# ---------------------------------------------------------------------------
# Cross-agent delegation and code-action
# ---------------------------------------------------------------------------


@tool
def whose_account(customer_id: str, caller: CallerContext) -> str:
    """Say whose account is being looked at."""
    return f"{customer_id} for {caller.user_id}"


class TestCrossAgent:
    async def test_peer_tool_sees_the_original_caller(self):
        peer = await build_agent(
            model=Scripted(mode="tool:whose_account"), extra_tools=[whose_account]
        )
        main = await build_agent(
            model=Scripted(mode="tool:ask_agent_support"),
            cross_agents={"support": CrossAgent(agent=peer, description="Support")},
        )
        try:
            result = await asyncio.wait_for(
                main.ainvoke(
                    {"messages": [HumanMessage(content="go")]},
                    caller=CallerContext(user_id="grace"),
                ),
                TIMEOUT,
            )
        finally:
            await main.shutdown()
            await peer.shutdown()
        assert result["messages"][-1].content == "ANSWER: ANSWER: acme for grace"


@tool
def list_skus(prefix: str) -> list:
    """SKUs that start with a prefix."""
    return [f"{prefix}-1", f"{prefix}-2"]


@tool
def describe(sku: str) -> str:
    """Describe a SKU."""
    return "42"


class TestCodeAction:
    async def test_program_gets_the_returned_data(self):
        node = CodeActionNode("reason")
        state, result = GraphState(messages=[]), NodeResult()
        tools = {"list_skus": list_skus, "describe": describe, "get_order": get_order}
        assert await node._invoke_tool(tools, state, {}, "list_skus", {"prefix": "X"}, result) == {
            "result": ["X-1", "X-2"]
        }
        # A string stays a string, even when it looks like JSON.
        assert await node._invoke_tool(tools, state, {}, "describe", {"sku": "X-1"}, result) == {
            "result": "42"
        }
        assert result.tool_calls[0]["result"] == '["X-1", "X-2"]'

    async def test_program_gets_tool_errors(self):
        node = CodeActionNode("reason")
        response = await node._invoke_tool(
            {"get_order": get_order},
            GraphState(messages=[]),
            {},
            "get_order",
            {"order_id": "A-404"},
            NodeResult(),
        )
        assert "No order A-404." in response["error"]
