"""Tests for engine hooks — LoggingHook, TimingHook, CycleDetectionHook, MetricsHook, BudgetHook."""

from __future__ import annotations

import asyncio
import logging
from unittest.mock import AsyncMock, MagicMock

import pytest

from promptise.engine import (
    BudgetHook,
    CycleDetectionHook,
    GraphState,
    LoggingHook,
    MetricsHook,
    NodeResult,
    PromptGraph,
    PromptGraphEngine,
    TimingHook,
    node,
)


def _make_mock_model():
    from langchain_core.messages import AIMessage

    model = MagicMock(spec=["ainvoke", "bind_tools", "with_structured_output"])
    model.ainvoke = AsyncMock(return_value=AIMessage(content="mock"))
    model.bind_tools = MagicMock(return_value=model)
    return model


# ═══════════════════════════════════════════════════════════════════════════════
# LoggingHook
# ═══════════════════════════════════════════════════════════════════════════════


class TestLoggingHook:
    @pytest.mark.asyncio
    async def test_logs_node_entry_exit(self, caplog):
        @node("step", default_next="__end__")
        async def step(state: GraphState) -> NodeResult:
            return NodeResult(node_name="step", output="done")

        graph = PromptGraph("test", mode="static")
        graph.add_node(step)
        graph.set_entry("step")

        with caplog.at_level(logging.INFO, logger="promptise.engine"):
            engine = PromptGraphEngine(
                graph=graph, model=_make_mock_model(), hooks=[LoggingHook()]
            )
            await engine.ainvoke({"messages": []})

        # LoggingHook should have logged pre_node and post_node
        log_text = caplog.text
        assert "step" in log_text


# ═══════════════════════════════════════════════════════════════════════════════
# TimingHook
# ═══════════════════════════════════════════════════════════════════════════════


class TestTimingHook:
    @pytest.mark.asyncio
    async def test_within_budget_no_error(self):
        @node("fast", default_next="__end__")
        async def fast(state: GraphState) -> NodeResult:
            return NodeResult(node_name="fast", output="ok")

        graph = PromptGraph("test", mode="static")
        graph.add_node(fast)
        graph.set_entry("fast")

        hook = TimingHook(default_budget_ms=30000)
        engine = PromptGraphEngine(graph=graph, model=_make_mock_model(), hooks=[hook])
        await engine.ainvoke({"messages": []})

        # No error — completed well within budget
        report = engine.last_report
        assert report is not None
        assert report.error is None

    @pytest.mark.asyncio
    async def test_custom_per_node_budget(self):
        @node("slow", default_next="__end__")
        async def slow(state: GraphState) -> NodeResult:
            await asyncio.sleep(0.05)  # 50ms
            return NodeResult(node_name="slow", output="done", total_tokens=10)

        graph = PromptGraph("test", mode="static")
        graph.add_node(slow)
        graph.set_entry("slow")

        hook = TimingHook(default_budget_ms=30000, per_node_budgets={"slow": 1})  # 1ms budget
        engine = PromptGraphEngine(graph=graph, model=_make_mock_model(), hooks=[hook])
        await engine.ainvoke({"messages": []})

        # The hook sets an error on the result but doesn't abort
        history = engine.last_report.nodes_visited
        assert "slow" in history


# ═══════════════════════════════════════════════════════════════════════════════
# CycleDetectionHook
# ═══════════════════════════════════════════════════════════════════════════════


class TestCycleDetectionHook:
    @pytest.mark.asyncio
    async def test_detects_cycle_and_ends(self):
        @node("a", default_next="b")
        async def a(state: GraphState) -> NodeResult:
            return NodeResult(node_name="a", output="ok")

        @node("b", default_next="a")
        async def b(state: GraphState) -> NodeResult:
            return NodeResult(node_name="b", output="ok")

        graph = PromptGraph("test", mode="static")
        graph.add_node(a)
        graph.add_node(b)
        graph.set_entry("a")
        graph.add_edge("a", "b")
        graph.add_edge("b", "a")

        hook = CycleDetectionHook(sequence_length=2, max_repeats=2)
        engine = PromptGraphEngine(
            graph=graph, model=_make_mock_model(), hooks=[hook], max_iterations=50
        )
        await engine.ainvoke({"messages": []})

        # Should have stopped before max_iterations
        assert engine.last_report.total_iterations < 50

    @pytest.mark.asyncio
    async def test_no_false_positive(self):
        @node("a", default_next="b")
        async def a(state: GraphState) -> NodeResult:
            return NodeResult(node_name="a", output="ok")

        @node("b", default_next="c")
        async def b(state: GraphState) -> NodeResult:
            return NodeResult(node_name="b", output="ok")

        @node("c", default_next="__end__")
        async def c(state: GraphState) -> NodeResult:
            return NodeResult(node_name="c", output="ok")

        graph = PromptGraph("test", mode="static")
        graph.add_node(a)
        graph.add_node(b)
        graph.add_node(c)
        graph.set_entry("a")
        graph.add_edge("a", "b")
        graph.add_edge("b", "c")

        hook = CycleDetectionHook(sequence_length=2, max_repeats=3)
        engine = PromptGraphEngine(graph=graph, model=_make_mock_model(), hooks=[hook])
        await engine.ainvoke({"messages": []})

        assert engine.last_report.nodes_visited == ["a", "b", "c"]


# ═══════════════════════════════════════════════════════════════════════════════
# MetricsHook
# ═══════════════════════════════════════════════════════════════════════════════


class TestMetricsHook:
    @pytest.mark.asyncio
    async def test_collects_per_node_metrics(self):
        @node("step1", default_next="step2")
        async def step1(state: GraphState) -> NodeResult:
            return NodeResult(node_name="step1", output="ok", total_tokens=100)

        @node("step2", default_next="__end__")
        async def step2(state: GraphState) -> NodeResult:
            return NodeResult(node_name="step2", output="ok", total_tokens=200)

        graph = PromptGraph("test", mode="static")
        graph.add_node(step1)
        graph.add_node(step2)
        graph.set_entry("step1")
        graph.add_edge("step1", "step2")

        hook = MetricsHook()
        engine = PromptGraphEngine(graph=graph, model=_make_mock_model(), hooks=[hook])
        await engine.ainvoke({"messages": []})

        summary = hook.summary()
        assert "step1" in summary
        assert "step2" in summary
        assert summary["step1"]["calls"] == 1
        assert summary["step2"]["calls"] == 1

    @pytest.mark.asyncio
    async def test_reset_clears(self):
        @node("x", default_next="__end__")
        async def x(state: GraphState) -> NodeResult:
            return NodeResult(node_name="x", output="ok")

        graph = PromptGraph("test", mode="static")
        graph.add_node(x)
        graph.set_entry("x")

        hook = MetricsHook()
        engine = PromptGraphEngine(graph=graph, model=_make_mock_model(), hooks=[hook])
        await engine.ainvoke({"messages": []})
        assert len(hook.summary()) > 0

        hook.reset()
        assert len(hook.summary()) == 0

    @pytest.mark.asyncio
    async def test_multiple_visits_accumulate(self):
        visit_count = 0

        @node("loop_node", default_next="__end__")
        async def loop_node(state: GraphState) -> NodeResult:
            nonlocal visit_count
            visit_count += 1
            if visit_count < 3:
                return NodeResult(node_name="loop_node", next_node="loop_node", total_tokens=50)
            return NodeResult(node_name="loop_node", output="done", total_tokens=50)

        graph = PromptGraph("test", mode="static")
        graph.add_node(loop_node)
        graph.set_entry("loop_node")

        hook = MetricsHook()
        engine = PromptGraphEngine(graph=graph, model=_make_mock_model(), hooks=[hook])
        await engine.ainvoke({"messages": []})

        summary = hook.summary()
        assert summary["loop_node"]["calls"] == 3


# ═══════════════════════════════════════════════════════════════════════════════
# BudgetHook
# ═══════════════════════════════════════════════════════════════════════════════


class TestBudgetHook:
    @pytest.mark.asyncio
    async def test_stops_on_token_budget(self):
        visit_count = 0

        @node("expensive")
        async def expensive(state: GraphState) -> NodeResult:
            nonlocal visit_count
            visit_count += 1
            if visit_count < 10:
                return NodeResult(
                    node_name="expensive", total_tokens=200, next_node="expensive"
                )
            return NodeResult(node_name="expensive", output="done", total_tokens=200)

        graph = PromptGraph("test", mode="static")
        graph.add_node(expensive)
        graph.set_entry("expensive")

        hook = BudgetHook(max_tokens=500)
        engine = PromptGraphEngine(
            graph=graph, model=_make_mock_model(), hooks=[hook], max_iterations=20
        )
        await engine.ainvoke({"messages": []})

        # Should have stopped before all 10 iterations
        assert visit_count < 10
        assert hook.tokens_used > 0

    @pytest.mark.asyncio
    async def test_tracks_cost(self):
        @node("step", default_next="__end__")
        async def step(state: GraphState) -> NodeResult:
            return NodeResult(node_name="step", output="done", total_tokens=1000)

        graph = PromptGraph("test", mode="static")
        graph.add_node(step)
        graph.set_entry("step")

        hook = BudgetHook(max_tokens=50000, cost_per_1k_tokens=0.01)
        engine = PromptGraphEngine(graph=graph, model=_make_mock_model(), hooks=[hook])
        await engine.ainvoke({"messages": []})

        assert hook.tokens_used == 1000
        assert hook.cost_used == pytest.approx(0.01, abs=0.001)


# ═══════════════════════════════════════════════════════════════════════════════
# Hook Error Handling
# ═══════════════════════════════════════════════════════════════════════════════


class TestHookErrorHandling:
    @pytest.mark.asyncio
    async def test_failing_pre_hook_doesnt_crash(self):
        class BadPreHook:
            async def pre_node(self, node, state):
                raise RuntimeError("hook exploded")
                return state

        @node("step", default_next="__end__")
        async def step(state: GraphState) -> NodeResult:
            return NodeResult(node_name="step", output="ok")

        graph = PromptGraph("test", mode="static")
        graph.add_node(step)
        graph.set_entry("step")

        engine = PromptGraphEngine(
            graph=graph, model=_make_mock_model(), hooks=[BadPreHook()]
        )
        # Should not crash despite hook failure
        await engine.ainvoke({"messages": []})
        assert "step" in engine.last_report.nodes_visited

    @pytest.mark.asyncio
    async def test_failing_post_hook_doesnt_crash(self):
        class BadPostHook:
            async def post_node(self, node, result, state):
                raise RuntimeError("post hook exploded")
                return result

        @node("step", default_next="__end__")
        async def step(state: GraphState) -> NodeResult:
            return NodeResult(node_name="step", output="ok")

        graph = PromptGraph("test", mode="static")
        graph.add_node(step)
        graph.set_entry("step")

        engine = PromptGraphEngine(
            graph=graph, model=_make_mock_model(), hooks=[BadPostHook()]
        )
        await engine.ainvoke({"messages": []})
        assert "step" in engine.last_report.nodes_visited


# ═══════════════════════════════════════════════════════════════════════════════
# Hook Interactions
# ═══════════════════════════════════════════════════════════════════════════════


class TestHookInteractions:
    @pytest.mark.asyncio
    async def test_multiple_hooks_all_called(self):
        calls = []

        class Hook1:
            async def pre_node(self, node, state):
                calls.append("h1_pre")
                return state

            async def post_node(self, node, result, state):
                calls.append("h1_post")
                return result

        class Hook2:
            async def pre_node(self, node, state):
                calls.append("h2_pre")
                return state

            async def post_node(self, node, result, state):
                calls.append("h2_post")
                return result

        @node("step", default_next="__end__")
        async def step(state: GraphState) -> NodeResult:
            return NodeResult(node_name="step", output="ok")

        graph = PromptGraph("test", mode="static")
        graph.add_node(step)
        graph.set_entry("step")

        engine = PromptGraphEngine(
            graph=graph, model=_make_mock_model(), hooks=[Hook1(), Hook2()]
        )
        await engine.ainvoke({"messages": []})

        assert "h1_pre" in calls
        assert "h2_pre" in calls
        assert "h1_post" in calls
        assert "h2_post" in calls

    @pytest.mark.asyncio
    async def test_hook_order_preserved(self):
        order = []

        class HookA:
            async def pre_node(self, n, s):
                order.append("A")
                return s

        class HookB:
            async def pre_node(self, n, s):
                order.append("B")
                return s

        class HookC:
            async def pre_node(self, n, s):
                order.append("C")
                return s

        @node("step", default_next="__end__")
        async def step(state: GraphState) -> NodeResult:
            return NodeResult(node_name="step", output="ok")

        graph = PromptGraph("test", mode="static")
        graph.add_node(step)
        graph.set_entry("step")

        engine = PromptGraphEngine(
            graph=graph, model=_make_mock_model(), hooks=[HookA(), HookB(), HookC()]
        )
        await engine.ainvoke({"messages": []})

        assert order == ["A", "B", "C"]
