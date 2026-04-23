"""Tests for reasoning node execution with mock LLMs — state mutations, routing, type coercion."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from langchain_core.messages import AIMessage, HumanMessage

from promptise.engine import (
    GraphState,
    NodeFlag,
    NodeResult,
    PromptGraph,
    PromptGraphEngine,
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
    ThinkNode,
    ValidateNode,
)


def _make_model(content: str = "mock", structured: dict | None = None):
    """Mock model. If structured is given, returns it as structured output."""
    if structured:
        model = MagicMock(spec=["ainvoke", "bind_tools", "with_structured_output"])
        model.ainvoke = AsyncMock(return_value=structured)
        model.bind_tools = MagicMock(return_value=model)
        model.with_structured_output = MagicMock(return_value=model)
        return model
    msg = AIMessage(content=content)
    model = MagicMock(spec=["ainvoke", "bind_tools", "with_structured_output"])
    model.ainvoke = AsyncMock(return_value=msg)
    model.bind_tools = MagicMock(return_value=model)
    return model


def _state_with_message(text="go"):
    return GraphState(messages=[HumanMessage(content=text)])


# ═══════════════════════════════════════════════════════════════════════════════
# ThinkNode
# ═══════════════════════════════════════════════════════════════════════════════


class TestThinkNode:
    @pytest.mark.asyncio
    async def test_executes_and_writes_output(self):
        n = ThinkNode("think")
        state = _state_with_message()
        config = {"_engine_model": _make_model("Gap: need more data. Confidence: 3.")}

        result = await n.execute(state, config)

        assert result.error is None
        assert "think_output" in state.context

    def test_default_flags(self):
        n = ThinkNode("t")
        assert NodeFlag.READONLY in n.flags
        assert NodeFlag.LIGHTWEIGHT in n.flags

    @pytest.mark.asyncio
    async def test_focus_areas_in_instructions(self):
        n = ThinkNode("think", focus_areas=["data quality", "completeness"])
        assert "data quality" in n.instructions
        assert "completeness" in n.instructions


# ═══════════════════════════════════════════════════════════════════════════════
# ReflectNode
# ═══════════════════════════════════════════════════════════════════════════════


class TestReflectNode:
    @pytest.mark.asyncio
    async def test_stores_reflection(self):
        n = ReflectNode("reflect")
        state = _state_with_message()
        model = _make_model(
            structured={"mistake": "wrong tool", "correction": "use search", "confidence": 4}
        )
        config = {"_engine_model": model}

        result = await n.execute(state, config)

        assert result.error is None
        assert len(state.reflections) == 1
        assert state.reflections[0]["mistake"] == "wrong tool"

    @pytest.mark.asyncio
    async def test_confidence_string_coercion(self):
        """String confidence like 'high' should not crash — falls back to 0.5."""
        n = ReflectNode("reflect")
        state = _state_with_message()
        model = _make_model(
            structured={"mistake": "bad", "correction": "fix", "confidence": "high"}
        )
        config = {"_engine_model": model}

        result = await n.execute(state, config)

        # Should not crash
        assert result.error is None
        if state.reflections:
            assert state.reflections[0]["confidence"] == 0.5

    @pytest.mark.asyncio
    async def test_error_returns_early(self):
        """If super().execute() errors, state should not be mutated."""
        n = ReflectNode("reflect")
        state = _state_with_message()
        config = {}  # No model → error

        result = await n.execute(state, config)

        assert result.error is not None
        assert len(state.reflections) == 0


# ═══════════════════════════════════════════════════════════════════════════════
# PlanNode
# ═══════════════════════════════════════════════════════════════════════════════


class TestPlanNode:
    @pytest.mark.asyncio
    async def test_updates_state_plan(self):
        n = PlanNode("plan")
        state = _state_with_message()
        model = _make_model(structured={"subgoals": ["gather", "analyze"], "quality_score": 4})
        config = {"_engine_model": model}

        result = await n.execute(state, config)

        assert result.error is None
        assert state.plan == ["gather", "analyze"]

    @pytest.mark.asyncio
    async def test_replans_on_low_quality(self):
        n = PlanNode("plan", quality_threshold=3)
        n.transitions = {"replan": "plan"}
        state = _state_with_message()
        state.graph = MagicMock()
        state.graph.nodes = {"plan": n}
        model = _make_model(structured={"subgoals": ["a"], "quality_score": 1})
        config = {"_engine_model": model}

        result = await n.execute(state, config)

        # Low quality should trigger re-plan
        assert result.next_node == "plan"

    @pytest.mark.asyncio
    async def test_subgoals_not_list_handled(self):
        """If LLM returns subgoals as a string, should not crash."""
        n = PlanNode("plan")
        state = _state_with_message()
        model = _make_model(structured={"subgoals": "just one thing", "quality_score": 4})
        config = {"_engine_model": model}

        result = await n.execute(state, config)

        assert result.error is None
        # subgoals is not a list — should be ignored
        assert state.plan == []


# ═══════════════════════════════════════════════════════════════════════════════
# CritiqueNode
# ═══════════════════════════════════════════════════════════════════════════════


class TestCritiqueNode:
    @pytest.mark.asyncio
    async def test_routes_on_high_severity(self):
        n = CritiqueNode("critique", severity_threshold=0.5)
        n.transitions = {"revise": "fix_node"}
        state = _state_with_message()
        model = _make_model(structured={"severity": 0.8, "weaknesses": ["incomplete"]})
        config = {"_engine_model": model}

        result = await n.execute(state, config)

        assert result.next_node == "fix_node"

    @pytest.mark.asyncio
    async def test_passes_on_low_severity(self):
        n = CritiqueNode("critique", severity_threshold=0.5)
        state = _state_with_message()
        model = _make_model(structured={"severity": 0.2, "weaknesses": []})
        config = {"_engine_model": model}

        result = await n.execute(state, config)

        assert result.next_node is None  # No routing override

    @pytest.mark.asyncio
    async def test_severity_string_coercion(self):
        """String severity like '0.9' should be coerced to float."""
        n = CritiqueNode("critique", severity_threshold=0.5)
        n.transitions = {"revise": "fix"}
        state = _state_with_message()
        model = _make_model(structured={"severity": "0.9"})
        config = {"_engine_model": model}

        result = await n.execute(state, config)

        assert result.next_node == "fix"


# ═══════════════════════════════════════════════════════════════════════════════
# ValidateNode
# ═══════════════════════════════════════════════════════════════════════════════


class TestValidateNode:
    @pytest.mark.asyncio
    async def test_routes_on_pass(self):
        n = ValidateNode("validate", on_pass="deliver", on_fail="revise")
        state = _state_with_message()
        model = _make_model(structured={"passes": True, "issues": []})
        config = {"_engine_model": model}

        result = await n.execute(state, config)

        assert result.next_node == "deliver"
        assert "passed" in result.transition_reason

    @pytest.mark.asyncio
    async def test_routes_on_fail(self):
        n = ValidateNode("validate", on_pass="deliver", on_fail="revise")
        state = _state_with_message()
        model = _make_model(structured={"passes": False, "issues": ["missing sources"]})
        config = {"_engine_model": model}

        result = await n.execute(state, config)

        assert result.next_node == "revise"


# ═══════════════════════════════════════════════════════════════════════════════
# ObserveNode
# ═══════════════════════════════════════════════════════════════════════════════


class TestObserveNode:
    @pytest.mark.asyncio
    async def test_merges_entities(self):
        n = ObserveNode("observe")
        state = _state_with_message()
        model = _make_model(structured={"entities": ["Alice", "Bob"], "facts": ["sky is blue"]})
        config = {"_engine_model": model}

        result = await n.execute(state, config)

        assert result.error is None
        assert state.context.get("extracted_entities") == ["Alice", "Bob"]
        assert state.context.get("extracted_facts") == ["sky is blue"]

    @pytest.mark.asyncio
    async def test_string_entities_not_merged(self):
        """String entities (not list) should be silently ignored."""
        n = ObserveNode("observe")
        state = _state_with_message()
        model = _make_model(structured={"entities": "Alice and Bob", "facts": "one fact"})
        config = {"_engine_model": model}

        result = await n.execute(state, config)

        assert result.error is None
        assert "extracted_entities" not in state.context


# ═══════════════════════════════════════════════════════════════════════════════
# JustifyNode
# ═══════════════════════════════════════════════════════════════════════════════


class TestJustifyNode:
    @pytest.mark.asyncio
    async def test_stores_justification(self):
        n = JustifyNode("justify")
        state = _state_with_message()
        model = _make_model(
            structured={
                "reasoning_chain": ["step1", "step2"],
                "conclusion": "therefore yes",
                "confidence": 4,
            }
        )
        config = {"_engine_model": model}

        result = await n.execute(state, config)

        assert result.error is None
        justifications = state.context.get("justifications", [])
        assert len(justifications) == 1
        assert justifications[0]["conclusion"] == "therefore yes"


# ═══════════════════════════════════════════════════════════════════════════════
# RetryNode
# ═══════════════════════════════════════════════════════════════════════════════


class TestRetryNode:
    @pytest.mark.asyncio
    async def test_retries_and_succeeds(self):
        attempt = 0

        @node("inner")
        async def inner(state: GraphState) -> NodeResult:
            nonlocal attempt
            attempt += 1
            if attempt < 3:
                return NodeResult(node_name="inner", error=f"fail {attempt}")
            return NodeResult(node_name="inner", output="success")

        n = RetryNode("retry", wrapped_node=inner, max_retries=3, backoff_factor=0.01)
        state = _state_with_message()
        config = {}

        result = await n.execute(state, config)

        assert attempt == 3
        assert result.error is None or "success" in str(result.output)

    @pytest.mark.asyncio
    async def test_backoff_capped(self):
        """Backoff should be capped — not sleep for absurd amounts."""

        @node("inner")
        async def inner(state: GraphState) -> NodeResult:
            return NodeResult(node_name="inner", error="always fail")

        n = RetryNode("retry", wrapped_node=inner, max_retries=2, backoff_factor=100)
        state = _state_with_message()

        with patch("asyncio.sleep", new_callable=AsyncMock) as mock_sleep:
            await n.execute(state, {})
            # backoff capped at 8s
            for call in mock_sleep.call_args_list:
                assert call[0][0] <= 8.0


# ═══════════════════════════════════════════════════════════════════════════════
# FanOutNode
# ═══════════════════════════════════════════════════════════════════════════════


class TestFanOutNode:
    @pytest.mark.asyncio
    async def test_parallel_execution(self):
        @node("branch_a")
        async def branch_a(state: GraphState) -> NodeResult:
            return NodeResult(node_name="branch_a", output="result_a")

        @node("branch_b")
        async def branch_b(state: GraphState) -> NodeResult:
            return NodeResult(node_name="branch_b", output="result_b")

        n = FanOutNode(
            "fanout",
            branches=[
                (branch_a, {"focus": "topic_a"}),
                (branch_b, {"focus": "topic_b"}),
            ],
        )
        state = _state_with_message()

        result = await n.execute(state, {})

        assert result.error is None
        assert "branch_a" in result.output
        assert "branch_b" in result.output
        assert result.output["branch_a"] == "result_a"
        assert result.output["branch_b"] == "result_b"

    @pytest.mark.asyncio
    async def test_branch_exception_handled(self):
        @node("good")
        async def good(state: GraphState) -> NodeResult:
            return NodeResult(node_name="good", output="ok")

        @node("bad")
        async def bad(state: GraphState) -> NodeResult:
            raise RuntimeError("branch exploded")

        n = FanOutNode(
            "fanout",
            branches=[
                (good, {}),
                (bad, {}),
            ],
        )
        state = _state_with_message()

        result = await n.execute(state, {})

        assert result.output["good"] == "ok"
        assert "error" in result.output["bad"]


# ═══════════════════════════════════════════════════════════════════════════════
# Full Pipeline — Engine-level test with reasoning nodes
# ═══════════════════════════════════════════════════════════════════════════════


class TestFullPipeline:
    @pytest.mark.asyncio
    async def test_think_plan_synthesize(self):
        """Run a 3-node reasoning graph through the engine."""

        @node("think", default_next="plan")
        async def think(state: GraphState) -> NodeResult:
            state.context["think_output"] = "Identified 2 gaps"
            return NodeResult(node_name="think", output="gaps identified")

        @node("plan", default_next="answer")
        async def plan(state: GraphState) -> NodeResult:
            state.plan = ["gather data", "analyze"]
            return NodeResult(node_name="plan", output="plan created")

        @node("answer", default_next="__end__")
        async def answer(state: GraphState) -> NodeResult:
            return NodeResult(
                node_name="answer",
                output=f"Based on {state.context.get('think_output', '?')}: final answer",
            )

        graph = PromptGraph("pipeline", mode="static")
        graph.add_node(think)
        graph.add_node(plan)
        graph.add_node(answer)
        graph.set_entry("think")
        graph.add_edge("think", "plan")
        graph.add_edge("plan", "answer")

        engine = PromptGraphEngine(graph=graph, model=_make_model())
        await engine.ainvoke({"messages": []})

        report = engine.last_report
        assert report.nodes_visited == ["think", "plan", "answer"]
        assert report.total_iterations == 3

    @pytest.mark.asyncio
    async def test_critique_revision_loop(self):
        """CritiqueNode routes back on high severity, then passes on second attempt."""
        attempts = 0

        @node("draft", default_next="critique")
        async def draft(state: GraphState) -> NodeResult:
            nonlocal attempts
            attempts += 1
            return NodeResult(node_name="draft", output=f"draft v{attempts}")

        @node("critique")
        async def critique(state: GraphState) -> NodeResult:
            if attempts < 2:
                return NodeResult(node_name="critique", next_node="draft")
            return NodeResult(node_name="critique", next_node="__end__")

        graph = PromptGraph("test", mode="static")
        graph.add_node(draft)
        graph.add_node(critique)
        graph.set_entry("draft")
        graph.add_edge("draft", "critique")

        engine = PromptGraphEngine(graph=graph, model=_make_model())
        await engine.ainvoke({"messages": []})

        assert attempts == 2
        assert "draft" in engine.last_report.nodes_visited
        assert "critique" in engine.last_report.nodes_visited
