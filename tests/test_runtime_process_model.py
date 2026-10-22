"""A model object for an Agent Runtime process: ``ProcessConfig(model=...)``
and ``AgentProcess(..., model=...)`` take a chat model or ``promptise.Model``
as well as a model id.

A model object is code only, like ``extra_tools``: never serialised.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from pydantic import ValidationError

from promptise import Model, tool
from promptise.runtime import AgentProcess, ProcessConfig, RuntimeConfig
from promptise.runtime.config import model_label
from promptise.runtime.meta_tools import _list_capabilities
from promptise.runtime.triggers.base import TriggerEvent
from promptise.testing import ScriptedModel, ToolCall


async def _wait_for(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def _event() -> TriggerEvent:
    return TriggerEvent(trigger_id="t", trigger_type="manual", payload={"order": "A-1001"})


def _lookup(ran: list[str]) -> Any:
    @tool
    def lookup_order(order_id: str) -> str:
        """Look up an order's status."""
        ran.append(order_id)
        return f"{order_id}: shipped"

    return lookup_order


@pytest.fixture(autouse=True)
def _no_provider_keys(monkeypatch: pytest.MonkeyPatch) -> None:
    # Nothing may reach a provider: without keys a model id could not be built.
    for name in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY"):
        monkeypatch.delenv(name, raising=False)


class TestProcessRunsOnAModelObject:
    async def test_config_model_handles_an_injected_event(self) -> None:
        ran: list[str] = []
        model = ScriptedModel(
            [ToolCall("lookup_order", {"order_id": "A-1001"}), "Order A-1001 has shipped."]
        )
        process = AgentProcess(
            "orders",
            ProcessConfig(
                model=model,
                instructions="Answer questions about orders.",
                extra_tools=[_lookup(ran)],
            ),
        )
        await process.start()
        try:
            await process.inject(_event())
            await _wait_for(lambda: process._invocation_count == 1)
        finally:
            await process.stop()

        assert ran == ["A-1001"]
        assert len(model.calls) == 2
        assert model.calls[0].tools == ["lookup_order"]
        assert model.remaining == 0
        assert process._consecutive_failures == 0
        conversation = await process._conversation_buffer.async_snapshot()
        assert conversation[-1]["content"] == "Order A-1001 has shipped."

    async def test_constructor_model_wins_over_the_config(self) -> None:
        model = ScriptedModel(["All good."])
        process = AgentProcess(
            "checker",
            ProcessConfig(model="openai:gpt-5-mini", instructions="Check things."),
            model=model,
        )
        assert process.model is model
        await process.start()
        try:
            await process.inject(_event())
            await _wait_for(lambda: process._invocation_count == 1)
        finally:
            await process.stop()
        assert len(model.calls) == 1
        assert process.config.model == "openai:gpt-5-mini"

    async def test_restart_keeps_the_model(self) -> None:
        model = ScriptedModel(["one", "two"])
        process = AgentProcess("again", ProcessConfig(model=model))
        for expected in (1, 2):
            await process.start()
            try:
                await process.inject(_event())
                await _wait_for(lambda n=expected: process._invocation_count == n)
            finally:
                await process.stop()
        assert len(model.calls) == 2


class TestConfig:
    def test_model_object_is_not_serialised(self) -> None:
        config = ProcessConfig(model=ScriptedModel(["x"]), instructions="Hi.")
        assert "model" not in config.model_dump()
        assert "model" not in config.model_dump(mode="json")
        runtime = RuntimeConfig(processes={"a": config})
        dumped = runtime.to_dict()
        assert "model" not in dumped["processes"]["a"]
        # Read back, the config has the default model id.
        assert RuntimeConfig.from_dict(dumped).processes["a"].model == "openai:gpt-5-mini"

    def test_model_id_is_serialised_as_before(self) -> None:
        config = ProcessConfig(model="anthropic:claude-sonnet-4-5")
        assert config.model_dump()["model"] == "anthropic:claude-sonnet-4-5"
        assert config.model_dump(mode="json")["model"] == "anthropic:claude-sonnet-4-5"

    def test_promptise_model_is_accepted(self) -> None:
        model = Model("gpt-5-mini", provider="openai", api_key="sk-test-not-real")
        config = ProcessConfig(model=model)
        assert config.model is model
        assert "model" not in config.model_dump(mode="json")
        assert "sk-test" not in model_label(model)

    @pytest.mark.parametrize("value", [3, {"id": "openai:gpt-5-mini"}, ["a"]])
    def test_anything_else_is_refused(self, value: Any) -> None:
        with pytest.raises(ValidationError, match="takes a model id"):
            ProcessConfig(model=value)

    def test_constructor_refuses_a_non_model(self) -> None:
        with pytest.raises(ValueError, match="model takes a model id"):
            AgentProcess("p", ProcessConfig(), model=42)


class TestLabels:
    def test_labels(self) -> None:
        assert model_label("openai:gpt-5-mini") == "openai:gpt-5-mini"
        assert model_label(ScriptedModel(["x"])) == "ScriptedModel(scripted)"

    async def test_status_and_capabilities_name_the_model(self) -> None:
        process = AgentProcess("p", ProcessConfig(model=ScriptedModel(["x"])))
        assert process._event_metadata()["model"] == "ScriptedModel(scripted)"
        assert "Model: ScriptedModel(scripted)" in await _list_capabilities(process)
