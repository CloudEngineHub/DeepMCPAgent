"""PromptNode settings that reach the model call, and what a node's reply feeds.

- ``tool_choice``, ``temperature`` and ``max_tokens`` are applied to the call
- a plain-text reply is the node's output (``output_key``, postprocessors)
- a preprocessor that raises stops the node before the model is called
- ``chain_preprocessors`` / ``chain_postprocessors`` await async steps
- string ``model_override`` resolves in ``RouterNode`` and ``CodeActionNode``
- a provider failure in ``CodeActionNode`` keeps its exception as the cause
"""

from __future__ import annotations

from typing import Any

import pytest
from langchain_core.tools import tool
from pydantic import Field

from promptise.engine import (
    GraphExecutionError,
    GraphState,
    PromptGraph,
    PromptGraphEngine,
    PromptNode,
    RouterNode,
)
from promptise.engine.code_action import CodeActionNode
from promptise.engine.processors import (
    chain_postprocessors,
    chain_preprocessors,
    input_validator,
    json_extractor,
)
from promptise.testing import ScriptedModel, ToolCall

QUESTION = {"messages": [{"role": "user", "content": "hi"}]}


class SamplingModel(ScriptedModel):
    """A scripted model with the sampling fields provider models have."""

    temperature: float | None = None
    max_tokens: int | None = None
    seen: list[dict[str, Any]] = Field(default_factory=list)

    def _generate(self, messages, stop=None, run_manager=None, **kwargs):  # type: ignore[override]
        self.seen.append(
            {
                "temperature": self.temperature,
                "max_tokens": self.max_tokens,
                "tool_choice": kwargs.get("tool_choice"),
            }
        )
        return super()._generate(messages, stop, run_manager, **kwargs)


@tool
def ping(x: int) -> str:
    """Ping."""
    return f"pong {x}"


def _engine(node: PromptNode, model: Any) -> PromptGraphEngine:
    graph = PromptGraph("g", nodes=[node])
    graph.set_entry(node.name)
    return PromptGraphEngine(graph, model)


class TestModelSettings:
    @pytest.mark.asyncio
    async def test_temperature_and_max_tokens_reach_the_model(self):
        model = SamplingModel(["done"], seen=[])
        node = PromptNode("a", temperature=0.7, max_tokens=123, default_next="__end__")
        await _engine(node, model).ainvoke(QUESTION)
        assert model.seen == [{"temperature": 0.7, "max_tokens": 123, "tool_choice": None}]

    @pytest.mark.asyncio
    async def test_unset_settings_keep_the_models_own(self):
        model = SamplingModel(["done"], temperature=0.3, max_tokens=50, seen=[])
        await _engine(PromptNode("a", default_next="__end__"), model).ainvoke(QUESTION)
        assert model.seen[0]["temperature"] == 0.3
        assert model.seen[0]["max_tokens"] == 50
        # The engine's model itself is never changed.
        assert model.temperature == 0.3

    @pytest.mark.asyncio
    async def test_required_tool_choice_forces_the_first_call_only(self):
        model = SamplingModel([ToolCall("ping", {"x": 1}), "pong received"], seen=[])
        node = PromptNode("a", tools=[ping], tool_choice="required", default_next="__end__")
        out = await _engine(node, model).ainvoke(QUESTION)
        assert [s["tool_choice"] for s in model.seen] == ["any", None]
        assert out["messages"][-1].content == "pong received"

    @pytest.mark.asyncio
    async def test_tool_name_choice_is_passed_through(self):
        model = SamplingModel([ToolCall("ping", {"x": 1}), "ok"], seen=[])
        node = PromptNode("a", tools=[ping], tool_choice="ping", default_next="__end__")
        await _engine(node, model).ainvoke(QUESTION)
        assert model.seen[0]["tool_choice"] == "ping"

    @pytest.mark.asyncio
    async def test_none_tool_choice_calls_the_model_without_tools(self):
        model = ScriptedModel(["no tools here"])
        node = PromptNode("a", tools=[ping], tool_choice="none", default_next="__end__")
        await _engine(node, model).ainvoke(QUESTION)
        assert model.calls[0].tools == []

    @pytest.mark.asyncio
    async def test_auto_tool_choice_binds_the_tools(self):
        model = SamplingModel(["ok"], seen=[])
        node = PromptNode("a", tools=[ping], default_next="__end__")
        await _engine(node, model).ainvoke(QUESTION)
        assert model.calls[0].tools == ["ping"]
        assert model.seen[0]["tool_choice"] is None


class TestTextOutput:
    @pytest.mark.asyncio
    async def test_text_reply_reaches_output_key_and_postprocessor(self):
        seen: dict[str, Any] = {}

        def post(output, state, config):
            seen["post"] = output
            return output

        captured: dict[str, Any] = {}

        class Capture:
            async def post_node(self, node, result, state):
                captured["context"] = dict(state.context)
                captured["output"] = result.output
                return result

        node = PromptNode("a", output_key="k", postprocessor=post, default_next="__end__")
        engine = _engine(node, ScriptedModel(["the answer"]))
        engine.hooks = [Capture()]
        await engine.ainvoke(QUESTION)
        assert seen["post"] == "the answer"
        assert captured["output"] == "the answer"
        assert captured["context"]["k"] == "the answer"

    @pytest.mark.asyncio
    async def test_json_extractor_parses_a_text_reply(self):
        captured: dict[str, Any] = {}

        class Capture:
            async def post_node(self, node, result, state):
                captured["k"] = state.context.get("k")
                return result

        node = PromptNode(
            "a", output_key="k", postprocessor=json_extractor(), default_next="__end__"
        )
        engine = _engine(node, ScriptedModel(['Result: {"score": 4}']))
        engine.hooks = [Capture()]
        await engine.ainvoke(QUESTION)
        assert captured["k"] == {"score": 4}

    @pytest.mark.asyncio
    async def test_postprocessor_skips_steps_that_only_call_tools(self):
        seen: list[Any] = []

        def post(output, state, config):
            seen.append(output)
            return output

        node = PromptNode("a", tools=[ping], postprocessor=post, default_next="__end__")
        await _engine(node, ScriptedModel([ToolCall("ping", {"x": 2}), "final"])).ainvoke(QUESTION)
        assert seen == ["final"]


class TestPreprocessorFailsClosed:
    @pytest.mark.asyncio
    async def test_raising_preprocessor_stops_the_node(self):
        model = ScriptedModel(["should not be called"])
        node = PromptNode(
            "a", preprocessor=input_validator(required_keys=["q"]), default_next="__end__"
        )
        with pytest.raises(GraphExecutionError) as excinfo:
            await _engine(node, model).ainvoke(QUESTION)
        assert model.calls == []
        assert "Preprocessor failed" in str(excinfo.value)
        assert "Missing required context keys" in str(excinfo.value)
        assert isinstance(excinfo.value.__cause__, ValueError)

    @pytest.mark.asyncio
    async def test_failed_preprocessor_follows_an_on_error_edge(self):
        model = ScriptedModel(["handled"])
        graph = PromptGraph(
            "g",
            nodes=[
                PromptNode("a", preprocessor=input_validator(required_keys=["q"])),
                PromptNode("fallback", default_next="__end__"),
            ],
        )
        graph.on_error("a", "fallback")
        graph.set_entry("a")
        engine = PromptGraphEngine(graph, model)
        out = await engine.ainvoke(QUESTION)
        assert out["messages"][-1].content == "handled"
        assert engine.last_report.nodes_visited == ["a", "fallback"]


class TestChainHelpers:
    @pytest.mark.asyncio
    async def test_chain_preprocessors_awaits_async_steps(self):
        order: list[str] = []

        def first(state, config):
            order.append("first")

        async def second(state, config):
            order.append("second")
            state.context["q"] = "set"

        def third(state, config):
            order.append("third")

        node = PromptNode(
            "a",
            preprocessor=chain_preprocessors(
                first, second, third, input_validator(required_keys=["q"])
            ),
            default_next="__end__",
        )
        await _engine(node, ScriptedModel(["ok"])).ainvoke(QUESTION)
        assert order == ["first", "second", "third"]

    def test_sync_chain_stays_synchronous(self):
        def add(output, state, config):
            return output + 1

        assert chain_postprocessors(add, add)(1, None, {}) == 3

    @pytest.mark.asyncio
    async def test_chain_postprocessors_awaits_async_steps(self):
        async def double(output, state, config):
            return output * 2

        def add(output, state, config):
            return output + 1

        result = chain_postprocessors(add, double, add)(1, None, {})
        assert await result == 5


class TestStringModelOverride:
    @pytest.mark.asyncio
    async def test_router_node_resolves_a_string_override(self, monkeypatch):
        override = ScriptedModel(["yes"])
        resolved: list[str] = []

        def fake_resolve(spec: str) -> Any:
            resolved.append(spec)
            return override

        monkeypatch.setattr("promptise.models.resolve_model", fake_resolve)
        router = RouterNode(
            "route",
            routes={"yes": "__end__", "no": "__end__"},
            model_override="openai:gpt-5-nano",
        )
        graph = PromptGraph("g", nodes=[router])
        graph.set_entry("route")
        engine_model = ScriptedModel([])
        await PromptGraphEngine(graph, engine_model).ainvoke(QUESTION)
        assert resolved == ["openai:gpt-5-nano"]
        assert len(override.calls) == 1
        assert engine_model.calls == []

    @pytest.mark.asyncio
    async def test_router_node_reports_an_unresolvable_override(self):
        router = RouterNode("route", routes={"a": "__end__"}, model_override="nope:model")
        result = await router.execute(GraphState(), {})
        assert result.error is not None
        assert "Failed to initialize model 'nope:model'" in result.error

    @pytest.mark.asyncio
    async def test_code_action_node_resolves_a_string_override(self, monkeypatch):
        override = ScriptedModel(["no program here"], on_exhausted="repeat_last")
        monkeypatch.setattr("promptise.models.resolve_model", lambda spec: override)

        async def factory() -> Any:
            raise RuntimeError("no sandbox in this test")

        node = CodeActionNode(
            "code", model_override="openai:gpt-5-mini", sandbox_factory=factory, max_repairs=0
        )
        # The override writes the program, then the node opens the sandbox.
        with pytest.raises(RuntimeError, match="no sandbox in this test"):
            await node.execute(GraphState(messages=[]), {})
        assert len(override.calls) == 1


class TestCodeActionFailureCause:
    @pytest.mark.asyncio
    async def test_provider_error_is_the_cause(self):
        class Boom(RuntimeError):
            pass

        class FailingModel(ScriptedModel):
            async def ainvoke(self, *args: Any, **kwargs: Any) -> Any:  # type: ignore[override]
                raise Boom("provider down")

        async def factory() -> Any:
            raise AssertionError("the sandbox is opened after the program is written")

        graph = PromptGraph(
            "g", nodes=[CodeActionNode("code", sandbox_factory=factory, default_next="__end__")]
        )
        graph.set_entry("code")
        with pytest.raises(GraphExecutionError) as excinfo:
            await PromptGraphEngine(graph, FailingModel([])).ainvoke(QUESTION)
        assert isinstance(excinfo.value.__cause__, Boom)
