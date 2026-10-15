"""Run limits: ``max_agent_iterations`` counts exactly, and a hook that ends a
run in the middle of a tool loop raises instead of returning a tool result.

Scripted models only: no network.
"""

from __future__ import annotations

import itertools
from typing import Any

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage, BaseMessage, HumanMessage, ToolMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from langchain_core.tools import tool

from promptise import build_agent
from promptise.engine import (
    BudgetHook,
    GraphBudgetExceededError,
    GraphExecutionError,
    GraphIterationLimitError,
    GraphStoppedError,
    PromptGraph,
    PromptGraphEngine,
)
from promptise.engine.base import node
from promptise.engine.state import GraphState, NodeResult

_ids = itertools.count()


class Pager(BaseChatModel):
    """Calls ``next_page`` on every turn until ``answer_after`` tool results
    are in the conversation (``0`` = never answers).  1,000 tokens a turn."""

    answer_after: int = 0
    turns: list[int] = []

    @property
    def _llm_type(self) -> str:
        return "pager"

    def bind_tools(self, tools: Any, **kwargs: Any) -> Pager:  # type: ignore[override]
        return self

    def _generate(self, messages: list[BaseMessage], stop: Any = None, **kw: Any) -> ChatResult:
        raise NotImplementedError("async only")

    async def _agenerate(
        self, messages: list[BaseMessage], stop: Any = None, run_manager: Any = None, **kw: Any
    ) -> ChatResult:
        self.turns.append(1)
        seen = sum(isinstance(m, ToolMessage) for m in messages)
        usage = {"input_tokens": 900, "output_tokens": 100, "total_tokens": 1000}
        if self.answer_after and seen >= self.answer_after:
            msg = AIMessage(content=f"read {seen} pages", usage_metadata=usage)
        else:
            msg = AIMessage(
                content="",
                tool_calls=[{"name": "next_page", "args": {"cursor": "x"}, "id": f"p{next(_ids)}"}],
                usage_metadata=usage,
            )
        return ChatResult(generations=[ChatGeneration(message=msg)])


def _pager_tool() -> tuple[Any, list[str]]:
    calls: list[str] = []

    @tool
    def next_page(cursor: str) -> dict:
        """Return the next page of items."""
        calls.append(cursor)
        return {"items": [1, 2], "next_cursor": f"c{len(calls)}"}

    return next_page, calls


def _question() -> dict[str, Any]:
    return {"messages": [HumanMessage(content="Read every page.")]}


# ── max_agent_iterations ────────────────────────────────────────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("n", [1, 2, 3])
async def test_max_agent_iterations_allows_exactly_n_model_calls(n: int) -> None:
    next_page, calls = _pager_tool()
    model = Pager(turns=[])
    agent = await build_agent(
        servers={}, model=model, extra_tools=[next_page], max_agent_iterations=n
    )
    try:
        with pytest.raises(GraphIterationLimitError) as info:
            await agent.ainvoke(_question())
    finally:
        await agent.shutdown()
    assert len(model.turns) == n
    assert len(calls) == n
    assert info.value.limit == n
    assert info.value.pending_tools == ["next_page"]


@pytest.mark.asyncio
async def test_max_agent_iterations_answer_on_the_last_allowed_call() -> None:
    next_page, calls = _pager_tool()
    model = Pager(turns=[], answer_after=2)
    agent = await build_agent(
        servers={}, model=model, extra_tools=[next_page], max_agent_iterations=3
    )
    try:
        result = await agent.ainvoke(_question())
    finally:
        await agent.shutdown()
    assert result["messages"][-1].content == "read 2 pages"
    assert len(model.turns) == 3
    assert len(calls) == 2


@pytest.mark.asyncio
async def test_engine_max_iterations_counts_node_executions() -> None:
    runs = 0

    @node("loop")
    async def loop(state: GraphState) -> NodeResult:
        nonlocal runs
        runs += 1
        return NodeResult(node_name="loop", next_node="loop")

    graph = PromptGraph("g", mode="static")
    graph.add_node(loop)
    graph.set_entry("loop")
    engine = PromptGraphEngine(
        graph=graph, model=Pager(turns=[]), max_iterations=4, max_node_iterations=100
    )
    await engine.ainvoke({"messages": []})
    assert runs == 4
    assert engine.last_report is not None
    assert engine.last_report.total_iterations == 4


# ── BudgetHook ──────────────────────────────────────────────────────────────


def _engine(model: Pager, hook: Any) -> tuple[PromptGraphEngine, list[str]]:
    next_page, calls = _pager_tool()
    graph = PromptGraph.react(tools=[next_page], system_prompt="Read pages.")
    return PromptGraphEngine(graph=graph, model=model, hooks=[hook], max_iterations=50), calls


@pytest.mark.asyncio
async def test_budget_stop_mid_tool_loop_raises_instead_of_returning_a_tool_result() -> None:
    hook = BudgetHook(max_tokens=2500)
    engine, calls = _engine(Pager(turns=[]), hook)
    with pytest.raises(GraphBudgetExceededError) as info:
        await engine.ainvoke(_question())
    exc = info.value
    assert isinstance(exc, GraphStoppedError)
    assert isinstance(exc, GraphExecutionError)
    assert exc.stopped_by == "BudgetHook"
    assert exc.reason == "Token budget exceeded: 3000/2500 tokens"
    assert exc.pending_tools == ["next_page"]
    assert "Token budget exceeded" in str(exc)
    assert engine.last_report is not None
    assert engine.last_report.error == exc.error
    assert len(calls) == 3


@pytest.mark.asyncio
async def test_budget_is_per_run_on_a_shared_hook() -> None:
    hook = BudgetHook(max_tokens=2500)
    engine, calls = _engine(Pager(turns=[]), hook)
    for _ in range(2):
        calls.clear()
        with pytest.raises(GraphBudgetExceededError):
            await engine.ainvoke(_question())
        # Every run gets the whole budget, not what an earlier run left.
        assert len(calls) == 3
        assert hook.tokens_used == 3000


@pytest.mark.asyncio
async def test_budget_exceeded_by_the_answer_returns_the_answer() -> None:
    hook = BudgetHook(max_tokens=1500)
    engine, calls = _engine(Pager(turns=[], answer_after=1), hook)
    result = await engine.ainvoke(_question())
    assert result["messages"][-1].content == "read 1 pages"
    assert hook.tokens_used == 2000


@pytest.mark.asyncio
async def test_cost_budget_raises_budget_error() -> None:
    hook = BudgetHook(max_cost_usd=0.003, cost_per_1k_tokens=0.002)
    engine, _calls = _engine(Pager(turns=[]), hook)
    with pytest.raises(GraphBudgetExceededError) as info:
        await engine.ainvoke(_question())
    assert info.value.reason.startswith("Cost budget exceeded")


@pytest.mark.asyncio
async def test_custom_hook_ending_a_tool_loop_raises_stopped_error() -> None:
    class StopAfterOne:
        async def post_node(self, node: Any, result: NodeResult, state: GraphState) -> NodeResult:
            state.current_node = "__end__"
            return result

    engine, calls = _engine(Pager(turns=[]), StopAfterOne())
    with pytest.raises(GraphStoppedError) as info:
        await engine.ainvoke(_question())
    assert not isinstance(info.value, GraphBudgetExceededError)
    assert info.value.stopped_by == "StopAfterOne"
    assert info.value.reason == "StopAfterOne ended the run"
    assert len(calls) == 1
