"""Tests for promptise.prompts.inspector — prompt assembly tracing."""

from __future__ import annotations

from promptise.prompts.blocks import (
    Identity,
    PromptAssembler,
    Rules,
)
from promptise.prompts.inspector import (
    GraphTrace,
    NodeTrace,
    PromptInspector,
    PromptTrace,
)

# ---------------------------------------------------------------------------
# PromptTrace
# ---------------------------------------------------------------------------


class TestPromptTrace:
    def test_defaults(self):
        trace = PromptTrace(
            timestamp=1.0,
            prompt_name="test",
            model="openai:gpt-5-mini",
        )
        assert trace.blocks == []
        assert trace.total_tokens_estimated == 0
        assert trace.input_text == ""
        assert trace.output_text == ""
        assert trace.latency_ms == 0.0
        assert trace.guards_passed == []
        assert trace.guards_failed == []
        assert trace.flow_phase == ""
        assert trace.flow_turn == 0


# ---------------------------------------------------------------------------
# PromptInspector
# ---------------------------------------------------------------------------


class TestPromptInspector:
    def test_record_assembly(self):
        inspector = PromptInspector()
        assembled = PromptAssembler(Identity("Agent")).assemble()

        trace = inspector.record_assembly(
            assembled, prompt_name="analyze", model="openai:gpt-5-mini"
        )
        assert isinstance(trace, PromptTrace)
        assert trace.prompt_name == "analyze"
        assert trace.model == "openai:gpt-5-mini"
        assert "Agent" in trace.input_text
        assert "identity" in trace.blocks_included
        assert len(inspector.traces) == 1

    def test_record_execution(self):
        inspector = PromptInspector()
        assembled = PromptAssembler(Identity("Agent")).assemble()
        trace = inspector.record_assembly(assembled, prompt_name="test", model="test")

        inspector.record_execution(trace, output="result text", latency_ms=42.0)
        assert trace.output_text == "result text"
        assert trace.latency_ms == 42.0

    def test_record_context(self):
        inspector = PromptInspector()
        assembled = PromptAssembler(Identity("x")).assemble()
        trace = inspector.record_assembly(assembled, "test", "test")

        inspector.record_context(
            trace,
            provider_name="UserContext",
            chars_injected=150,
            render_time_ms=2.5,
        )
        assert len(trace.context_providers) == 1
        assert trace.context_providers[0].provider_name == "UserContext"
        assert trace.context_providers[0].chars_injected == 150

    def test_record_guard(self):
        inspector = PromptInspector()
        assembled = PromptAssembler(Identity("x")).assemble()
        trace = inspector.record_assembly(assembled, "test", "test")

        inspector.record_guard(trace, "content_filter", passed=True)
        inspector.record_guard(trace, "length", passed=False)
        assert "content_filter" in trace.guards_passed
        assert "length" in trace.guards_failed

    def test_last(self):
        inspector = PromptInspector()
        assert inspector.last() is None

        a1 = PromptAssembler(Identity("First")).assemble()
        inspector.record_assembly(a1, "first", "m1")

        a2 = PromptAssembler(Identity("Second")).assemble()
        inspector.record_assembly(a2, "second", "m2")

        last = inspector.last()
        assert last is not None
        assert last.prompt_name == "second"

    def test_clear(self):
        inspector = PromptInspector()
        a = PromptAssembler(Identity("x")).assemble()
        inspector.record_assembly(a, "test", "test")
        assert len(inspector.traces) == 1

        inspector.clear()
        assert len(inspector.traces) == 0
        assert inspector.last() is None

    def test_summary_empty(self):
        inspector = PromptInspector()
        assert inspector.summary() == "No traces recorded."

    def test_summary_with_traces(self):
        inspector = PromptInspector()
        a = PromptAssembler(Identity("Agent"), Rules(["Be precise"])).assemble()
        trace = inspector.record_assembly(a, "analyze", "openai:gpt-5-mini")
        inspector.record_execution(trace, "output", latency_ms=100.0)

        summary = inspector.summary()
        assert "Prompt Traces" in summary
        assert "analyze" in summary
        assert "openai:gpt-5-mini" in summary
        assert "100.0ms" in summary
        assert "identity" in summary

    def test_repr(self):
        inspector = PromptInspector()
        r = repr(inspector)
        assert "PromptInspector" in r
        assert "traces=0" in r


# ---------------------------------------------------------------------------
# GraphTrace
# ---------------------------------------------------------------------------


class TestGraphTrace:
    def test_record_graph(self):
        inspector = PromptInspector()
        node_traces = [
            NodeTrace(
                node_id="step1",
                node_type="function",
                duration_ms=10.0,
                status="success",
            ),
            NodeTrace(
                node_id="step2",
                node_type="prompt",
                duration_ms=50.0,
                status="success",
            ),
        ]

        gt = inspector.record_graph(
            graph_name="pipeline",
            node_traces=node_traces,
            total_duration_ms=60.0,
            path=["step1", "step2"],
            iterations=2,
            final_state={"result": "done"},
        )
        assert isinstance(gt, GraphTrace)
        assert gt.graph_name == "pipeline"
        assert len(gt.node_traces) == 2
        assert gt.total_duration_ms == 60.0
        assert gt.iterations == 2

    def test_last_graph(self):
        inspector = PromptInspector()
        assert inspector.last_graph() is None

        inspector.record_graph("g1", [], 0, [], 0, {})
        inspector.record_graph("g2", [], 0, [], 0, {})

        last = inspector.last_graph()
        assert last is not None
        assert last.graph_name == "g2"

    def test_summary_with_graph(self):
        inspector = PromptInspector()
        nt = NodeTrace(
            node_id="classify",
            node_type="function",
            duration_ms=5.0,
            status="success",
        )
        inspector.record_graph("analyzer", [nt], 5.0, ["classify"], 1, {})

        summary = inspector.summary()
        assert "Graph Traces" in summary
        assert "analyzer" in summary
        assert "classify" in summary

    def test_summary_with_error_node(self):
        inspector = PromptInspector()
        nt = NodeTrace(
            node_id="broken",
            node_type="function",
            duration_ms=1.0,
            status="error",
            error="connection failed",
        )
        inspector.record_graph("test", [nt], 1.0, ["broken"], 1, {})

        summary = inspector.summary()
        assert "connection failed" in summary
        assert "[x]" in summary
