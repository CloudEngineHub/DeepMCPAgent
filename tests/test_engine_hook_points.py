"""Engine control points: LLM graph actions, hook call sites, cycle detection,
and the answer a structured-output node leaves at the end of a run."""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.messages import AIMessage
from langchain_core.tools import tool
from pydantic import BaseModel

from promptise.engine import (
    CycleDetectionHook,
    GraphExecutionError,
    GraphMutation,
    GraphState,
    NodeFlag,
    NodeResult,
    PromptGraph,
    PromptGraphEngine,
    PromptNode,
    ValidateNode,
    node,
)
from promptise.testing import ScriptedModel, ToolCall

QUESTION = {"messages": [{"role": "user", "content": "hi"}]}


def _emit(name: str, output: Any = None, **kwargs: Any) -> Any:
    @node(name, **kwargs)
    async def step(state: GraphState) -> NodeResult:
        return NodeResult(node_name=name, output=output)

    return step


class TestLLMGraphActions:
    @pytest.mark.asyncio
    async def test_skip_to_jumps_over_the_default_next(self):
        graph = PromptGraph(
            "g",
            nodes=[
                _emit("a", {"_graph_action": "skip_to", "_target": "c"}, default_next="b"),
                _emit("b", default_next="__end__"),
                _emit("c", default_next="__end__"),
            ],
        )
        graph.set_entry("a")
        engine = PromptGraphEngine(graph, ScriptedModel([]))
        await engine.ainvoke(QUESTION)
        assert engine.last_report.nodes_visited == ["a", "c"]

    @pytest.mark.asyncio
    async def test_skip_to_end_is_a_normal_finish(self):
        graph = PromptGraph(
            "g",
            nodes=[
                _emit("a", {"_graph_action": "skip_to", "_target": "__end__"}, default_next="b"),
                _emit("b", default_next="__end__"),
            ],
        )
        graph.set_entry("a")
        engine = PromptGraphEngine(graph, ScriptedModel([]))
        await engine.ainvoke(QUESTION)  # no GraphStoppedError
        assert engine.last_report.nodes_visited == ["a"]
        assert engine.last_report.error is None

    @pytest.mark.asyncio
    async def test_retry_with_updates_context_and_runs_the_node_again(self):
        seen: list[Any] = []

        @node("a", default_next="__end__")
        async def a(state: GraphState) -> NodeResult:
            seen.append(state.context.get("hint"))
            if state.context.get("hint") is None:
                return NodeResult(
                    node_name="a",
                    output={"_graph_action": "retry_with", "_context_update": {"hint": "x"}},
                )
            return NodeResult(node_name="a", output="done")

        graph = PromptGraph("g", nodes=[a])
        graph.set_entry("a")
        engine = PromptGraphEngine(graph, ScriptedModel([]))
        await engine.ainvoke(QUESTION)
        assert seen == [None, "x"]
        assert engine.last_report.nodes_visited == ["a", "a"]


class TestOnErrorHook:
    @pytest.mark.asyncio
    async def test_on_error_is_called_and_can_redirect(self):
        calls: list[tuple[str, str]] = []

        class Recover:
            async def on_error(self, n, error, state):
                calls.append((n.name, type(error).__name__))
                return "fallback"

        @node("a", default_next="__end__")
        async def a(state: GraphState) -> NodeResult:
            raise ValueError("bad input")

        graph = PromptGraph("g", nodes=[a, _emit("fallback", "recovered", default_next="__end__")])
        graph.set_entry("a")
        engine = PromptGraphEngine(graph, ScriptedModel([]), hooks=[Recover()])
        await engine.ainvoke(QUESTION)
        assert calls == [("a", "ValueError")]
        assert engine.last_report.nodes_visited == ["a", "fallback"]

    @pytest.mark.asyncio
    async def test_on_error_returning_none_keeps_the_failure(self):
        class Observe:
            seen: list[str] = []

            async def on_error(self, n, error, state):
                self.seen.append(str(error))
                return None

        hook = Observe()
        graph = PromptGraph("g", nodes=[_emit_error("a", "it broke")])
        graph.set_entry("a")
        with pytest.raises(GraphExecutionError, match="it broke"):
            await PromptGraphEngine(graph, ScriptedModel([]), hooks=[hook]).ainvoke(QUESTION)
        assert hook.seen == ["it broke"]

    @pytest.mark.asyncio
    async def test_a_raising_on_error_hook_is_logged_not_fatal(self):
        class Broken:
            async def on_error(self, n, error, state):
                raise RuntimeError("hook bug")

        graph = PromptGraph("g", nodes=[_emit_error("a", "it broke")])
        graph.set_entry("a")
        with pytest.raises(GraphExecutionError, match="it broke"):
            await PromptGraphEngine(graph, ScriptedModel([]), hooks=[Broken()]).ainvoke(QUESTION)


def _emit_error(name: str, error: str) -> Any:
    @node(name, default_next="__end__")
    async def step(state: GraphState) -> NodeResult:
        return NodeResult(node_name=name, error=error)

    return step


class TestOnGraphMutationHook:
    @pytest.mark.asyncio
    async def test_hook_sees_and_can_block_mutations(self):
        seen: list[str] = []

        class Gate:
            async def on_graph_mutation(self, mutation, state):
                seen.append(mutation.action)
                return mutation.action != "add_edge"

        @node("a", default_next="__end__")
        async def a(state: GraphState) -> NodeResult:
            return NodeResult(
                node_name="a",
                graph_mutations=[GraphMutation(action="add_edge", from_node="a", to_node="b")],
            )

        graph = PromptGraph("g", nodes=[a, _emit("b", default_next="__end__")])
        graph.set_entry("a")
        engine = PromptGraphEngine(graph, ScriptedModel([]), hooks=[Gate()])
        await engine.ainvoke(QUESTION)
        assert seen == ["add_edge"]
        # Blocked: the edge to b was never added.
        assert engine.last_report.nodes_visited == ["a"]
        assert engine.last_report.graph_mutations == 0

    @pytest.mark.asyncio
    async def test_allowed_mutation_is_applied(self):
        class Allow:
            async def on_graph_mutation(self, mutation, state):
                return True

        graph = PromptGraph(
            "g",
            nodes=[
                _emit("a", {"_graph_action": "add_edge", "_from": "a", "_to": "b"}),
                _emit("b", default_next="__end__"),
            ],
        )
        graph.set_entry("a")
        engine = PromptGraphEngine(graph, ScriptedModel([]), hooks=[Allow()])
        await engine.ainvoke(QUESTION)
        assert engine.last_report.nodes_visited == ["a", "b"]

    @pytest.mark.asyncio
    async def test_a_raising_gate_blocks(self):
        class Broken:
            async def on_graph_mutation(self, mutation, state):
                raise RuntimeError("gate bug")

        graph = PromptGraph(
            "g",
            nodes=[
                _emit("a", {"_graph_action": "add_edge", "_from": "a", "_to": "b"}),
                _emit("b", default_next="__end__"),
            ],
        )
        graph.set_entry("a")
        engine = PromptGraphEngine(graph, ScriptedModel([]), hooks=[Broken()])
        await engine.ainvoke(QUESTION)
        assert engine.last_report.nodes_visited == ["a"]


class TestFlagHookErrors:
    @pytest.mark.asyncio
    async def test_raising_observable_and_human_hooks_do_not_end_the_run(self):
        class Broken:
            async def on_observable_event(self, event, state):
                raise RuntimeError("metrics sink down")

            async def on_human_required(self, name, state):
                raise RuntimeError("pager down")

        graph = PromptGraph(
            "g",
            nodes=[
                _emit(
                    "a",
                    "ok",
                    default_next="__end__",
                    flags={NodeFlag.OBSERVABLE, NodeFlag.REQUIRES_HUMAN},
                )
            ],
        )
        graph.set_entry("a")
        engine = PromptGraphEngine(graph, ScriptedModel([]), hooks=[Broken()])
        await engine.ainvoke(QUESTION)
        assert engine.last_report.nodes_visited == ["a"]
        assert engine.last_report.error is None


@tool
def ping(x: int) -> str:
    """Ping."""
    return f"pong {x}"


class TestCycleDetection:
    @pytest.mark.asyncio
    async def test_a_single_node_tool_loop_is_not_a_cycle(self):
        replies: list[Any] = [ToolCall("ping", {"x": i}) for i in range(12)] + ["answer"]
        model = ScriptedModel(replies)
        graph = PromptGraph(
            "g",
            nodes=[PromptNode("reason", tools=[ping], default_next="__end__", max_iterations=20)],
        )
        graph.set_entry("reason")
        engine = PromptGraphEngine(graph, model, hooks=[CycleDetectionHook()])
        out = await engine.ainvoke(QUESTION)
        assert out["messages"][-1].content == "answer"
        assert len(model.calls) == 13

    @pytest.mark.asyncio
    async def test_cycle_stop_happens_before_the_node_runs(self):
        runs: list[str] = []

        @node("a", default_next="b")
        async def a(state: GraphState) -> NodeResult:
            runs.append("a")
            return NodeResult(node_name="a", output="ok")

        @node("b", default_next="a")
        async def b(state: GraphState) -> NodeResult:
            runs.append("b")
            return NodeResult(node_name="b", output="ok")

        graph = PromptGraph("g", nodes=[a, b])
        graph.set_entry("a")
        engine = PromptGraphEngine(
            graph, ScriptedModel([]), hooks=[CycleDetectionHook(sequence_length=2, max_repeats=2)]
        )
        await engine.ainvoke(QUESTION)
        # a b a, then the second "b" would complete the repeat: the hook
        # stops the run and that "b" never runs.
        assert runs == ["a", "b", "a"]
        assert engine.last_report.nodes_visited == ["a", "b", "a"]


class TestStructuredAnswer:
    @pytest.mark.asyncio
    async def test_schema_node_at_the_end_leaves_an_answer_message(self):
        class Verdict(BaseModel):
            passes: bool
            reason: str

        model = ScriptedModel([ToolCall("Verdict", {"passes": True, "reason": "fine"})])
        graph = PromptGraph(
            "g", nodes=[PromptNode("judge", output_schema=Verdict, default_next="__end__")]
        )
        graph.set_entry("judge")
        out = await PromptGraphEngine(graph, model).ainvoke(QUESTION)
        last = out["messages"][-1]
        assert isinstance(last, AIMessage)
        assert last.content == '{"passes":true,"reason":"fine"}'

    @pytest.mark.asyncio
    async def test_schema_node_in_the_middle_adds_no_message(self):
        class Plan(BaseModel):
            steps: list[str]

        model = ScriptedModel([ToolCall("Plan", {"steps": ["a"]}), "final"])
        graph = PromptGraph(
            "g",
            nodes=[
                PromptNode("plan", output_schema=Plan, default_next="answer"),
                PromptNode("answer", default_next="__end__"),
            ],
        )
        graph.set_entry("plan")
        out = await PromptGraphEngine(graph, model).ainvoke(QUESTION)
        assert [m.type for m in out["messages"]] == ["human", "ai"]
        assert out["messages"][-1].content == "final"

    @pytest.mark.asyncio
    async def test_stream_ends_with_the_answer_message_too(self):
        class Verdict(BaseModel):
            passes: bool

        model = ScriptedModel([ToolCall("Verdict", {"passes": False})])
        graph = PromptGraph(
            "g", nodes=[PromptNode("judge", output_schema=Verdict, default_next="__end__")]
        )
        graph.set_entry("judge")
        chunks = [c async for c in PromptGraphEngine(graph, model).astream(QUESTION)]
        assert chunks[-1]["messages"][-1].content == '{"passes":false}'

    @pytest.mark.asyncio
    async def test_validate_node_at_the_end_keeps_the_answer_it_checked(self):
        class Verdict(BaseModel):
            passes: bool
            issues: list[str]

        seen: dict[str, Any] = {}

        class Capture:
            async def post_node(self, node, result, state):
                seen.update(state.context)
                return result

        model = ScriptedModel(
            ["North missed.", ToolCall("Verdict", {"passes": True, "issues": []})]
        )
        graph = PromptGraph(
            "g",
            nodes=[
                PromptNode("act", default_next="verify"),
                ValidateNode("verify", output_schema=Verdict, on_pass="__end__", on_fail="act"),
            ],
        )
        graph.set_entry("act")
        out = await PromptGraphEngine(graph, model, hooks=[Capture()]).ainvoke(QUESTION)
        assert [m.type for m in out["messages"]] == ["human", "ai"]
        assert out["messages"][-1].content == "North missed."
        # The verdict stays under the node's output_key.
        assert seen["validation"] == {"passes": True, "issues": []}

    @pytest.mark.asyncio
    async def test_validate_node_with_no_answer_before_it_leaves_its_verdict(self):
        class Verdict(BaseModel):
            passes: bool

        model = ScriptedModel([ToolCall("Verdict", {"passes": True})])
        graph = PromptGraph("g", nodes=[ValidateNode("verify", output_schema=Verdict)])
        graph.set_entry("verify")
        out = await PromptGraphEngine(graph, model).ainvoke(QUESTION)
        assert out["messages"][-1].content == '{"passes":true}'

    @pytest.mark.asyncio
    async def test_schema_prompt_node_after_an_answer_still_leaves_its_output(self):
        class Invoice(BaseModel):
            total: int

        model = ScriptedModel(["The total is 12.", ToolCall("Invoice", {"total": 12})])
        graph = PromptGraph(
            "g",
            nodes=[
                PromptNode("draft", default_next="extract"),
                PromptNode("extract", output_schema=Invoice, default_next="__end__"),
            ],
        )
        graph.set_entry("draft")
        out = await PromptGraphEngine(graph, model).ainvoke(QUESTION)
        assert out["messages"][-1].content == '{"total":12}'
