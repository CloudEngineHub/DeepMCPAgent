"""build_agent graph options: node budgets, graph_blocks and HumanNode approval."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.tools import tool

from promptise import build_agent
from promptise.approval import ApprovalDecision
from promptise.engine import (
    GraphExecutionError,
    GraphIterationLimitError,
    GraphState,
    HumanNode,
    NodeResult,
    PromptGraph,
    PromptGraphEngine,
    PromptNode,
    node,
)
from promptise.testing import ScriptedModel, ToolCall

QUESTION = {"messages": [{"role": "user", "content": "go"}]}


def _poll_tool() -> tuple[Any, list[int]]:
    calls: list[int] = []

    @tool
    def poll(n: int) -> str:
        """Poll a job."""
        calls.append(n)
        return "still running"

    return poll, calls


def _polling_model() -> ScriptedModel:
    return ScriptedModel([ToolCall("poll", {"n": 1})], on_exhausted="repeat_last")


class _Block:
    name = "house_rules"

    def render(self, ctx: Any) -> str:
        return "HOUSE RULES: answer in French."


class TestNodeBudgets:
    @pytest.mark.asyncio
    @pytest.mark.parametrize("pattern", ["managed", "react"])
    async def test_max_agent_iterations_governs_single_node_patterns(self, pattern: str):
        poll, calls = _poll_tool()
        model = _polling_model()
        agent = await build_agent(
            model=model,
            servers={},
            extra_tools=[poll],
            agent_pattern=pattern,
            max_agent_iterations=40,
        )
        try:
            with pytest.raises(GraphIterationLimitError) as info:
                await agent.ainvoke(QUESTION)
        finally:
            await agent.shutdown()
        assert len(model.calls) == 40
        assert len(calls) == 40
        assert info.value.limit == 40

    @pytest.mark.asyncio
    async def test_managed_graph_uses_its_own_budget_in_a_plain_engine(self):
        poll, calls = _poll_tool()
        model = _polling_model()
        engine = PromptGraphEngine(PromptGraph.managed(tools=[poll]), model)
        with pytest.raises(GraphIterationLimitError) as info:
            await engine.ainvoke(QUESTION)
        assert len(model.calls) == 30
        assert info.value.limit == 30

    @pytest.mark.asyncio
    async def test_an_explicit_engine_ceiling_still_caps_nodes(self):
        poll, _ = _poll_tool()
        model = _polling_model()
        engine = PromptGraphEngine(PromptGraph.managed(tools=[poll]), model, max_node_iterations=5)
        with pytest.raises(GraphIterationLimitError) as info:
            await engine.ainvoke(QUESTION)
        assert len(model.calls) == 5
        assert info.value.limit == 5


class TestGraphBlocks:
    @pytest.mark.asyncio
    async def test_graph_blocks_reach_the_default_pattern(self):
        model = ScriptedModel(["bonjour"])
        agent = await build_agent(model=model, servers={}, graph_blocks=[_Block()])
        try:
            await agent.ainvoke(QUESTION)
        finally:
            await agent.shutdown()
        system = str(model.calls[0].messages[0].content)
        assert "HOUSE RULES: answer in French." in system

    @pytest.mark.asyncio
    async def test_graph_blocks_reach_node_pool_nodes_without_changing_them(self):
        answer = PromptNode("answer", is_entry=True, is_terminal=True)
        model = ScriptedModel(["bonjour"], on_exhausted="repeat_last")
        agent = await build_agent(
            model=model, servers={}, node_pool=[answer], graph_blocks=[_Block()]
        )
        try:
            await agent.ainvoke(QUESTION)
        finally:
            await agent.shutdown()
        prompts = [str(m.content) for call in model.calls for m in call.messages]
        assert any("HOUSE RULES: answer in French." in p for p in prompts)
        assert answer.blocks == []


def _approval_graph(reached: list[str]) -> PromptGraph:
    @node("act", default_next="__end__")
    async def act(state: GraphState) -> NodeResult:
        reached.append("act")
        return NodeResult(node_name="act", output="done")

    graph = PromptGraph(
        "g",
        mode="static",
        nodes=[HumanNode("approve", prompt_template="Deploy?", on_approve="act"), act],
    )
    graph.set_entry("approve")
    return graph


class TestHumanNodeApproval:
    @pytest.mark.asyncio
    async def test_build_agent_approval_answers_human_nodes(self):
        asked: list[Any] = []

        async def reviewer(request: Any) -> ApprovalDecision:
            asked.append(request)
            return ApprovalDecision(approved=True)

        reached: list[str] = []
        agent = await build_agent(
            model=ScriptedModel([]),
            servers={},
            agent_pattern=_approval_graph(reached),
            approval=reviewer,
        )
        try:
            await agent.ainvoke(QUESTION)
        finally:
            await agent.shutdown()
        assert reached == ["act"]
        assert asked[0].context_summary == "Deploy?"
        assert asked[0].metadata["source"] == "human_node"
        assert asked[0].tool_name == "approve"

    @pytest.mark.asyncio
    async def test_a_denial_does_not_approve(self):
        async def reviewer(request: Any) -> ApprovalDecision:
            return ApprovalDecision(approved=False)

        reached: list[str] = []
        agent = await build_agent(
            model=ScriptedModel([]),
            servers={},
            agent_pattern=_approval_graph(reached),
            approval=reviewer,
        )
        try:
            await agent.ainvoke(QUESTION)
        finally:
            await agent.shutdown()
        assert reached == []

    @pytest.mark.asyncio
    async def test_without_approval_the_human_node_fails(self):
        reached: list[str] = []
        agent = await build_agent(
            model=ScriptedModel([]), servers={}, agent_pattern=_approval_graph(reached)
        )
        try:
            with pytest.raises(GraphExecutionError, match="has no human handler"):
                await agent.ainvoke(QUESTION)
        finally:
            await agent.shutdown()
        assert reached == []
