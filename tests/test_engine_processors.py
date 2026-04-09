"""Tests for engine processors — pre/post processing pipeline functions."""

from __future__ import annotations

import pytest

from promptise.engine.processors import (
    chain_postprocessors,
    chain_preprocessors,
    confidence_scorer,
    context_enricher,
    input_validator,
    json_extractor,
    output_truncator,
    state_summarizer,
    state_writer,
)
from promptise.engine.state import GraphState


class TestContextEnricher:
    def test_adds_timestamp(self):
        state = GraphState(iteration=3)
        fn = context_enricher(include_timestamp=True, include_iteration=False)
        fn(state, {})
        assert "_timestamp" in state.context
        assert "T" in state.context["_timestamp"]  # ISO format

    def test_adds_iteration_info(self):
        state = GraphState(iteration=5)
        state.tool_calls_made = 3
        fn = context_enricher(include_timestamp=False, include_iteration=True)
        fn(state, {})
        assert state.context["_iteration"] == 5
        assert state.context["_tools_called"] == 3

    def test_both_disabled(self):
        state = GraphState()
        fn = context_enricher(include_timestamp=False, include_iteration=False)
        fn(state, {})
        assert "_timestamp" not in state.context
        assert "_iteration" not in state.context


class TestStateSummarizer:
    def test_truncates_long_values(self):
        state = GraphState(context={"data": "x" * 3000})
        fn = state_summarizer(max_context_chars=100)
        fn(state, {})
        assert len(state.context["data"]) < 200
        assert "truncated" in state.context["data"]

    def test_leaves_short_values(self):
        state = GraphState(context={"data": "short"})
        fn = state_summarizer(max_context_chars=100)
        fn(state, {})
        assert state.context["data"] == "short"

    def test_leaves_non_string_values(self):
        state = GraphState(context={"count": 42, "items": [1, 2, 3]})
        fn = state_summarizer(max_context_chars=10)
        fn(state, {})
        assert state.context["count"] == 42
        assert state.context["items"] == [1, 2, 3]


class TestInputValidator:
    def test_passes_when_keys_present(self):
        state = GraphState(context={"query": "test", "source": "web"})
        fn = input_validator(required_keys=["query", "source"])
        fn(state, {})  # Should not raise

    def test_raises_when_keys_missing(self):
        state = GraphState(context={"query": "test"})
        fn = input_validator(required_keys=["query", "source"])
        with pytest.raises(ValueError, match="source"):
            fn(state, {})


class TestJsonExtractor:
    def test_extracts_from_string(self):
        fn = json_extractor()
        result = fn('Here is the answer: {"key": "value", "num": 42}', GraphState(), {})
        assert result == {"key": "value", "num": 42}

    def test_nested_braces(self):
        fn = json_extractor()
        text = 'Result: {"outer": {"inner": "deep"}, "flat": 1}'
        result = fn(text, GraphState(), {})
        assert result["outer"]["inner"] == "deep"
        assert result["flat"] == 1

    def test_with_key_filter(self):
        fn = json_extractor(keys=["answer"])
        result = fn('{"answer": "yes", "confidence": 0.9}', GraphState(), {})
        assert result == {"answer": "yes"}
        assert "confidence" not in result

    def test_no_json_returns_original(self):
        fn = json_extractor()
        result = fn("just plain text", GraphState(), {})
        assert result == "just plain text"

    def test_dict_input_passed_through(self):
        fn = json_extractor()
        data = {"already": "parsed"}
        result = fn(data, GraphState(), {})
        assert result == {"already": "parsed"}

    def test_dict_input_filtered(self):
        fn = json_extractor(keys=["a"])
        result = fn({"a": 1, "b": 2, "c": 3}, GraphState(), {})
        assert result == {"a": 1}


class TestConfidenceScorer:
    def test_high_confidence(self):
        fn = confidence_scorer()
        result = fn("The answer is definitively 42.", GraphState(), {})
        assert result["_confidence"] >= 0.5

    def test_low_confidence(self):
        fn = confidence_scorer()
        result = fn(
            "I think maybe it might possibly be unclear, perhaps approximately 42, "
            "but I'm not sure and it could be something else.",
            GraphState(),
            {},
        )
        assert result["_confidence"] < 0.5

    def test_dict_input_preserves_fields(self):
        fn = confidence_scorer()
        result = fn({"answer": "42", "source": "math"}, GraphState(), {})
        assert "answer" in result
        assert "_confidence" in result


class TestStateWriter:
    def test_writes_fields(self):
        state = GraphState()
        fn = state_writer(fields={"answer": "final_answer", "score": "quality"})
        fn({"answer": "yes", "score": 4.5, "extra": "ignored"}, state, {})
        assert state.context["final_answer"] == "yes"
        assert state.context["quality"] == 4.5
        assert "extra" not in state.context

    def test_missing_keys_skipped(self):
        state = GraphState()
        fn = state_writer(fields={"answer": "final", "missing": "nowhere"})
        fn({"answer": "yes"}, state, {})
        assert state.context["final"] == "yes"
        assert "nowhere" not in state.context

    def test_non_dict_ignored(self):
        state = GraphState()
        fn = state_writer(fields={"answer": "final"})
        result = fn("plain string", state, {})
        assert result == "plain string"
        assert "final" not in state.context


class TestOutputTruncator:
    def test_truncates_long_string(self):
        fn = output_truncator(max_chars=50)
        result = fn("x" * 200, GraphState(), {})
        assert len(result) == 53  # 50 + "..."
        assert result.endswith("...")

    def test_leaves_short_string(self):
        fn = output_truncator(max_chars=100)
        result = fn("short", GraphState(), {})
        assert result == "short"

    def test_non_string_passed_through(self):
        fn = output_truncator(max_chars=10)
        result = fn({"key": "value"}, GraphState(), {})
        assert result == {"key": "value"}


class TestCombinators:
    def test_chain_preprocessors(self):
        calls = []

        def pre1(state, config):
            calls.append("pre1")
            state.context["a"] = 1

        def pre2(state, config):
            calls.append("pre2")
            state.context["b"] = 2

        def pre3(state, config):
            calls.append("pre3")
            state.context["c"] = 3

        state = GraphState()
        fn = chain_preprocessors(pre1, pre2, pre3)
        fn(state, {})

        assert calls == ["pre1", "pre2", "pre3"]
        assert state.context == {"a": 1, "b": 2, "c": 3}

    def test_chain_postprocessors(self):
        def add_prefix(output, state, config):
            return f"[processed] {output}"

        def uppercase(output, state, config):
            return output.upper()

        fn = chain_postprocessors(add_prefix, uppercase)
        result = fn("hello", GraphState(), {})
        assert result == "[PROCESSED] HELLO"

    def test_chain_postprocessors_piping(self):
        """Each postprocessor receives the output of the previous one."""

        def step1(output, state, config):
            assert output == "raw"
            return "step1"

        def step2(output, state, config):
            assert output == "step1"
            return "step2"

        def step3(output, state, config):
            assert output == "step2"
            return "final"

        fn = chain_postprocessors(step1, step2, step3)
        assert fn("raw", GraphState(), {}) == "final"
