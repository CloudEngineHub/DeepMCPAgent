"""Local tools in the Agent Runtime: ``ProcessConfig.extra_tools`` and
``AgentProcess(extra_tools=...)``.

The tools reach every agent the process builds, go through the same
governance as MCP tools (budget, approval, journal) and are validated when
the config or process is created.  They are code only: never serialised.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.language_models.chat_models import BaseChatModel
from langchain_core.messages import AIMessage
from langchain_core.outputs import ChatGeneration, ChatResult
from pydantic import PrivateAttr

import promptise.agent as agent_module
from promptise import tool
from promptise.approval import ApprovalDecision, ApprovalPolicy
from promptise.runtime import AgentProcess, ProcessConfig, RuntimeConfig
from promptise.runtime.config import BudgetConfig, ExecutionMode, JournalConfig
from promptise.runtime.lifecycle import ProcessState
from promptise.runtime.triggers.base import TriggerEvent

_REAL_BUILD = agent_module.build_agent


class _ScriptedModel(BaseChatModel):
    """Emits the scripted tool calls, one turn each, then answers ``done``."""

    _script: list[list[dict[str, Any]]] = PrivateAttr()

    def __init__(self, script: list[list[dict[str, Any]]]) -> None:
        super().__init__()
        self._script = list(script)

    @property
    def _llm_type(self) -> str:
        return "scripted"

    def bind_tools(self, tools: Any, **kwargs: Any) -> _ScriptedModel:
        return self

    def _generate(self, messages, stop=None, run_manager=None, **kwargs) -> ChatResult:
        if self._script:
            message = AIMessage(content="", tool_calls=self._script.pop(0))
        else:
            message = AIMessage(content="done")
        return ChatResult(generations=[ChatGeneration(message=message)])


def _with_model(model: BaseChatModel) -> Any:
    """Patch build_agent so the process's agent is built for real, on *model*."""

    async def build(**kwargs: Any) -> Any:
        kwargs["model"] = model
        return await _REAL_BUILD(**kwargs)

    return patch("promptise.agent.build_agent", new=build)


def _lookup_calls(*order_ids: str) -> list[list[dict[str, Any]]]:
    return [
        [{"name": "lookup_order", "args": {"order_id": oid}, "id": f"c{i}"}]
        for i, oid in enumerate(order_ids)
    ]


async def _wait_for(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def _event() -> TriggerEvent:
    return TriggerEvent(trigger_id="t", trigger_type="manual", payload={"task": "check A-1"})


def _make_lookup(ran: list[str]) -> Any:
    @tool
    def lookup_order(order_id: str) -> str:
        """Look up an order's status."""
        ran.append(order_id)
        return f"{order_id}: shipped"

    return lookup_order


class TestLocalToolsReachTheAgent:
    async def test_tool_function_is_called_journaled_and_counted(self) -> None:
        ran: list[str] = []
        config = ProcessConfig(
            extra_tools=[_make_lookup(ran)],
            budget=BudgetConfig(enabled=True, max_tool_calls_per_day=10),
            journal=JournalConfig(level="full", backend="memory"),
        )
        with _with_model(_ScriptedModel(_lookup_calls("A-1"))):
            process = AgentProcess("orders", config)
            await process.start()
            try:
                await process.inject(_event())
                await _wait_for(lambda: process._invocation_count == 1)
            finally:
                await process.stop()

        assert ran == ["A-1"]
        assert process._budget is not None and process._budget.daily_tool_calls == 1
        entries = await process._journal.read("orders", entry_type="tool_call")
        assert [e.data["tool"] for e in entries] == ["lookup_order"]

    async def test_budget_blocks_a_local_tool(self) -> None:
        ran: list[str] = []
        config = ProcessConfig(
            extra_tools=[_make_lookup(ran)],
            budget=BudgetConfig(enabled=True, max_tool_calls_per_run=1, on_exceeded="pause"),
        )
        with _with_model(_ScriptedModel(_lookup_calls("A-1", "A-2"))):
            process = AgentProcess("orders", config)
            await process.start()
            try:
                await process.inject(_event())
                await _wait_for(lambda: process.state == ProcessState.SUSPENDED)
            finally:
                await process.stop()

        assert ran == ["A-1"]

    async def test_approval_gates_a_local_tool(self) -> None:
        ran: list[str] = []
        requests: list[Any] = []

        class Deny:
            async def request_approval(self, request: Any) -> ApprovalDecision:
                requests.append(request)
                return ApprovalDecision(False)

        config = ProcessConfig(
            extra_tools=[_make_lookup(ran)],
            approval=ApprovalPolicy(tools=["lookup_*"], handler=Deny()),
        )
        with _with_model(_ScriptedModel(_lookup_calls("A-1"))):
            process = AgentProcess("orders", config)
            await process.start()
            try:
                await process.inject(_event())
                await _wait_for(lambda: process._invocation_count == 1)
            finally:
                await process.stop()

        assert ran == []
        assert [r.tool_name for r in requests] == ["lookup_order"]

    async def test_constructor_tools_join_config_tools_on_every_build(self) -> None:
        def refund(order_id: str) -> str:
            """Refund an order."""
            return "refunded"

        agent = AsyncMock()
        build = AsyncMock(return_value=agent)
        with patch("promptise.agent.build_agent", new=build):
            process = AgentProcess(
                "orders",
                ProcessConfig(extra_tools=[_make_lookup([])]),
                extra_tools=[refund],
            )
            await process._build_agent()
            process.update_instructions("Be brief.")
            await process._apply_instruction_change()
            await process._build_agent()  # as a restart or hot reload does

        assert build.await_count == 3
        for call in build.await_args_list:
            names = [t.name for t in call.kwargs["extra_tools"]]
            assert names[:2] == ["lookup_order", "refund"]


class TestValidation:
    def test_config_rejects_a_non_tool(self) -> None:
        with pytest.raises(TypeError, match=r"extra_tools\[0\] must be a function"):
            ProcessConfig(extra_tools=[42])

    def test_config_rejects_a_single_tool_not_in_a_list(self) -> None:
        with pytest.raises(TypeError, match="must be a list"):
            ProcessConfig(extra_tools=_make_lookup([]))

    def test_config_rejects_names_from_a_manifest(self) -> None:
        with pytest.raises(TypeError, match="can't name them"):
            ProcessConfig.model_validate({"extra_tools": ["tools:lookup_order"]})

    def test_process_rejects_a_non_tool(self) -> None:
        with pytest.raises(TypeError, match=r"extra_tools\[0\] must be a function"):
            AgentProcess("orders", ProcessConfig(), extra_tools=[object()])

    def test_plain_functions_become_tools(self) -> None:
        def ping(host: str) -> str:
            """Ping a host."""
            return "pong"

        config = ProcessConfig(extra_tools=[ping])
        assert [t.name for t in config.extra_tools] == ["ping"]


class TestNotSerialised:
    def test_dump_and_round_trip_leave_the_tools_out(self) -> None:
        config = ProcessConfig(extra_tools=[_make_lookup([])])
        assert "extra_tools" not in config.model_dump()
        assert "extra_tools" not in config.model_dump(mode="json")
        data = RuntimeConfig(processes={"orders": config}).to_dict()
        restored = RuntimeConfig.from_dict(data)
        assert restored.processes["orders"].extra_tools == []


class TestOpenMode:
    async def test_agent_cannot_create_a_tool_named_like_a_local_tool(self) -> None:
        from promptise.runtime.meta_tools import _create_tool

        process = AgentProcess(
            "orders",
            ProcessConfig(execution_mode=ExecutionMode.OPEN, extra_tools=[_make_lookup([])]),
        )
        process._build_agent = AsyncMock()  # type: ignore[method-assign]
        result = await _create_tool(
            process,
            tool_name="lookup_order",
            tool_description="Shadow the operator's tool",
            parameters={},
            python_code="def run(**kwargs): return 'x'",
        )
        assert "already exists" in result
        assert process._custom_tools == []
