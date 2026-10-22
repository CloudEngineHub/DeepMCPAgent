"""Reasoning and composite nodes act on what their children and models return.

- reasoning nodes read their fields from a text reply without an output_schema
- an output_schema still decides when one is given
- RetryNode runs ``max_retries + 1`` attempts, not that times the engine's retries
- ParallelNode / FanOutNode merge strategies differ
- SubgraphNode(inherit_state=True) hands the sub-graph's reply to the parent
- HumanNode never approves without a handler
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage
from pydantic import BaseModel

from promptise.engine import (
    GraphExecutionError,
    GraphState,
    HumanNode,
    NodeFlag,
    NodeResult,
    ParallelNode,
    PromptGraph,
    PromptGraphEngine,
    PromptNode,
    SubgraphNode,
    TransformNode,
    node,
)
from promptise.engine.reasoning_nodes import (
    CritiqueNode,
    FanOutNode,
    JustifyNode,
    ObserveNode,
    PlanNode,
    ReflectNode,
    RetryNode,
    SynthesizeNode,
    ThinkNode,
    ValidateNode,
)
from promptise.testing import ScriptedModel, ToolCall

QUESTION = {"messages": [{"role": "user", "content": "hi"}]}


async def _run(graph: PromptGraph, model: Any, **engine_kwargs: Any) -> tuple[Any, Any]:
    engine = PromptGraphEngine(graph, model, **engine_kwargs)
    captured: dict[str, Any] = {}

    class Capture:
        async def post_node(self, n, result, state):
            captured["state"] = state
            captured.setdefault("results", []).append(result)
            return result

    engine.hooks = [*engine.hooks, Capture()]
    out = await engine.ainvoke(QUESTION)
    return out, captured


def _single(n: Any) -> PromptGraph:
    graph = PromptGraph("g", nodes=[n])
    graph.set_entry(n.name)
    return graph


class TestReasoningNodesWithoutSchema:
    @pytest.mark.asyncio
    async def test_reflect_node_stores_a_reflection_from_text(self):
        reply = (
            "I searched the wrong year.\n"
            '{"progress": "half done", "mistake": "used 2023 data", '
            '"correction": "use 2024", "confidence": 3, "route": "continue"}'
        )
        model = ScriptedModel([reply])
        _, captured = await _run(_single(ReflectNode("reflect", default_next="__end__")), model)
        state = captured["state"]
        assert state.reflections[0]["mistake"] == "used 2023 data"
        assert state.reflections[0]["correction"] == "use 2024"
        assert state.context["reflection"]["route"] == "continue"
        # The node asked for the fields.
        system = model.calls[0].messages[0].content
        assert "End your reply with a JSON object" in system
        assert "- mistake:" in system

    @pytest.mark.asyncio
    async def test_reflect_route_from_text_follows_the_transition(self):
        reply = (
            '{"progress": "p", "mistake": "", "correction": "", "confidence": 4, "route": "answer"}'
        )
        graph = PromptGraph(
            "g",
            nodes=[
                ReflectNode("reflect", transitions={"answer": "final", "continue": "reflect"}),
                PromptNode("final", default_next="__end__"),
            ],
        )
        graph.set_entry("reflect")
        engine = PromptGraphEngine(graph, ScriptedModel([reply, "the answer"]))
        out = await engine.ainvoke(QUESTION)
        assert engine.last_report.nodes_visited == ["reflect", "final"]
        assert out["messages"][-1].content == "the answer"

    @pytest.mark.asyncio
    async def test_plan_node_sets_the_plan_from_text(self):
        reply = 'Plan:\n{"subgoals": ["find A", "find B"], "active_subgoal": "find A", "quality_score": 4}'
        _, captured = await _run(
            _single(PlanNode("plan", default_next="__end__")), ScriptedModel([reply])
        )
        assert captured["state"].plan == ["find A", "find B"]

    @pytest.mark.asyncio
    async def test_critique_severity_from_text_routes_to_revision(self):
        reply = '{"weaknesses": ["no source"], "counter_arguments": [], "improvements": ["cite"], "severity": 0.9}'
        graph = PromptGraph(
            "g",
            nodes=[
                CritiqueNode("critic", transitions={"revise": "fix"}, default_next="__end__"),
                PromptNode("fix", default_next="__end__"),
            ],
        )
        graph.set_entry("critic")
        engine = PromptGraphEngine(graph, ScriptedModel([reply, "revised"]))
        await engine.ainvoke(QUESTION)
        assert engine.last_report.nodes_visited == ["critic", "fix"]

    @pytest.mark.asyncio
    async def test_validate_node_routes_on_a_text_verdict(self):
        reply = '{"passes": false, "issues": ["missing total"], "suggestions": [], "confidence": 4}'
        graph = PromptGraph(
            "g",
            nodes=[
                ValidateNode("validate", on_pass="__end__", on_fail="redo"),
                PromptNode("redo", default_next="__end__"),
            ],
        )
        graph.set_entry("validate")
        engine = PromptGraphEngine(graph, ScriptedModel([reply, "redone"]))
        await engine.ainvoke(QUESTION)
        assert engine.last_report.nodes_visited == ["validate", "redo"]

    @pytest.mark.asyncio
    async def test_observe_and_justify_store_their_fields(self):
        observe = '{"summary": "s", "entities": ["ACME"], "facts": ["f1"], "key_findings": []}'
        justify = (
            '{"reasoning_chain": ["a", "b"], "evidence": [], "conclusion": "c", "confidence": 5}'
        )
        graph = PromptGraph(
            "g",
            nodes=[
                ObserveNode("observe", default_next="justify"),
                JustifyNode("justify", default_next="__end__"),
            ],
        )
        graph.set_entry("observe")
        _, captured = await _run(graph, ScriptedModel([observe, justify]))
        context = captured["state"].context
        assert context["extracted_entities"] == ["ACME"]
        assert context["extracted_facts"] == ["f1"]
        assert context["justifications"][0]["conclusion"] == "c"

    @pytest.mark.asyncio
    async def test_text_without_json_stays_text(self):
        _, captured = await _run(
            _single(ReflectNode("reflect", default_next="__end__")),
            ScriptedModel(["no structured part"]),
        )
        assert captured["state"].reflections == []
        assert captured["state"].context["reflection"] == "no structured part"

    @pytest.mark.asyncio
    async def test_think_and_synthesize_nodes_stay_plain_text(self):
        model = ScriptedModel(["gap: none", "final answer"])
        graph = PromptGraph(
            "g",
            nodes=[
                ThinkNode("think", default_next="answer"),
                SynthesizeNode("answer"),
            ],
        )
        graph.set_entry("think")
        _, captured = await _run(graph, model)
        assert captured["state"].context["think_output"] == "gap: none"
        assert captured["state"].context["synthesis"] == "final answer"
        assert all(
            "End your reply with a JSON object" not in str(m.content)
            for call in model.calls
            for m in call.messages
        )

    @pytest.mark.asyncio
    async def test_a_schema_still_decides(self):
        class Reflection(BaseModel):
            mistake: str
            correction: str

        model = ScriptedModel([ToolCall("Reflection", {"mistake": "m", "correction": "c"})])
        _, captured = await _run(
            _single(ReflectNode("reflect", output_schema=Reflection, default_next="__end__")),
            model,
        )
        assert captured["state"].reflections[0]["mistake"] == "m"
        system = model.calls[0].messages[0].content
        assert "End your reply with a JSON object" not in system


class TestRetryNode:
    @pytest.mark.asyncio
    async def test_engine_does_not_retry_a_retry_node_again(self):
        attempts: list[int] = []

        @node("flaky")
        async def flaky(state: GraphState) -> NodeResult:
            attempts.append(1)
            raise RuntimeError("down")

        retry = RetryNode(
            "retry", wrapped_node=flaky, max_retries=1, backoff_factor=0.0, max_iterations=2
        )
        with pytest.raises(GraphExecutionError) as excinfo:
            await PromptGraphEngine(_single(retry), ScriptedModel([])).ainvoke(QUESTION)
        assert len(attempts) == 2
        assert "All 2 attempts failed" in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, RuntimeError)

    def test_retry_node_is_not_engine_retryable(self):
        inner = TransformNode("inner", transform=lambda s: 1)
        assert NodeFlag.RETRYABLE not in RetryNode("r", wrapped_node=inner).flags

    @pytest.mark.asyncio
    async def test_saved_retryable_flag_is_not_applied_twice(self):
        attempts: list[int] = []

        @node("flaky")
        async def flaky(state: GraphState) -> NodeResult:
            attempts.append(1)
            return NodeResult(node_name="flaky", error="nope")

        retry = RetryNode(
            "retry",
            wrapped_node=flaky,
            max_retries=0,
            backoff_factor=0.0,
            flags={NodeFlag.RETRYABLE},
            max_iterations=3,
        )
        with pytest.raises(GraphExecutionError):
            await PromptGraphEngine(_single(retry), ScriptedModel([])).ainvoke(QUESTION)
        assert len(attempts) == 1


def _child(name: str, output: Any) -> Any:
    @node(name)
    async def child(state: GraphState) -> NodeResult:
        return NodeResult(node_name=name, output=output)

    return child


class TestMergeStrategies:
    @pytest.mark.asyncio
    async def test_parallel_concatenate_differs_from_dict(self):
        children = [_child("a", "alpha"), _child("b", {"k": 1})]
        as_dict = await ParallelNode("p", nodes=children, merge_strategy="dict").execute(
            GraphState(), {}
        )
        joined = await ParallelNode("p", nodes=children, merge_strategy="concatenate").execute(
            GraphState(), {}
        )
        assert as_dict.output == {"a": "alpha", "b": {"k": 1}}
        assert joined.output == '[a]\nalpha\n\n[b]\n{"k": 1}'
        assert joined.raw_output == joined.output

    @pytest.mark.asyncio
    async def test_parallel_default_is_dict(self):
        result = await ParallelNode("p", nodes=[_child("a", "x")]).execute(GraphState(), {})
        assert result.output == {"a": "x"}

    def test_unknown_or_incomplete_strategies_are_rejected(self):
        with pytest.raises(ValueError, match="merge_strategy must be one of"):
            ParallelNode("p", merge_strategy="first")
        with pytest.raises(ValueError, match="needs a merge_fn"):
            ParallelNode("p", merge_strategy="custom")
        with pytest.raises(ValueError, match="merge_strategy must be one of"):
            FanOutNode("f", merge_strategy="first")

    @pytest.mark.asyncio
    async def test_fan_out_applies_its_merge_strategy(self):
        branches = [(_child("a", "one"), {}), (_child("b", "two"), {})]
        joined = await FanOutNode("f", branches=branches, merge_strategy="concatenate").execute(
            GraphState(), {}
        )
        custom = await FanOutNode(
            "f",
            branches=branches,
            merge_strategy="custom",
            merge_fn=lambda outputs: sorted(outputs.values()),
        ).execute(GraphState(), {})
        assert joined.output == "[a]\none\n\n[b]\ntwo"
        assert custom.output == ["one", "two"]


class TestSubgraphNode:
    @pytest.mark.asyncio
    async def test_inherited_subgraph_reply_reaches_the_parent(self):
        sub = _single(PromptNode("inner", default_next="__end__"))
        parent = _single(
            SubgraphNode("sub", subgraph=sub, inherit_state=True, default_next="__end__")
        )
        out = await PromptGraphEngine(parent, ScriptedModel(["sub answer"])).ainvoke(QUESTION)
        assert isinstance(out["messages"][-1], AIMessage)
        assert out["messages"][-1].content == "sub answer"
        # The question is not duplicated.
        assert [m.type for m in out["messages"]] == ["human", "ai"]

    @pytest.mark.asyncio
    async def test_isolated_subgraph_still_appends_its_messages(self):
        sub = _single(PromptNode("inner", default_next="__end__"))
        parent = _single(
            SubgraphNode("sub", subgraph=sub, inherit_state=False, default_next="__end__")
        )
        out = await PromptGraphEngine(parent, ScriptedModel(["isolated"])).ainvoke(QUESTION)
        assert out["messages"][-1].content == "isolated"


class TestHumanNode:
    @pytest.mark.asyncio
    async def test_no_handler_fails_instead_of_approving(self):
        reached: list[str] = []

        @node("act")
        async def act(state: GraphState) -> NodeResult:
            reached.append("act")
            return NodeResult(node_name="act")

        graph = PromptGraph("g", nodes=[HumanNode("approve", on_approve="act"), act])
        graph.set_entry("approve")
        with pytest.raises(GraphExecutionError, match="has no human handler"):
            await PromptGraphEngine(graph, ScriptedModel([])).ainvoke(QUESTION)
        assert reached == []

    @pytest.mark.asyncio
    async def test_engine_human_handler_is_used(self):
        asked: list[str] = []

        async def handler(prompt: str, state: GraphState) -> bool:
            asked.append(prompt)
            return False

        graph = PromptGraph(
            "g",
            nodes=[
                HumanNode(
                    "approve", prompt_template="Ship it?", on_approve="__end__", on_deny="no"
                ),
                _child("no", "denied"),
            ],
        )
        graph.set_entry("approve")
        engine = PromptGraphEngine(graph, ScriptedModel([]), human_handler=handler)
        await engine.ainvoke(QUESTION)
        assert asked == ["Ship it?"]
        assert engine.last_report.nodes_visited == ["approve", "no"]

    @pytest.mark.asyncio
    async def test_a_denied_decision_object_is_a_denial(self):
        class Decision:
            approved = False

        async def handler(prompt: str, state: GraphState) -> Any:
            return Decision()

        human = HumanNode("approve", on_approve="yes", on_deny="no")
        result = await human.execute(GraphState(), {"_human_handler": handler})
        assert result.next_node == "no"
