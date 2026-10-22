"""Budget-refused tool calls are journaled (``tool_denied``), the violation
the process acts on is journaled (``budget_violation``), and the
``permission_denied`` hook gets the refused call's arguments."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage

from promptise.runtime import (
    BudgetConfig,
    HookContext,
    HookEvent,
    JournalConfig,
    ProcessConfig,
)
from promptise.runtime.budget import BudgetState
from promptise.runtime.callbacks import RuntimeCallbackHandler
from promptise.runtime.exceptions import BudgetExceededError
from promptise.runtime.process import AgentProcess
from promptise.runtime.triggers.base import TriggerEvent

BUILD_TARGET = "promptise.agent.build_agent"


async def _wait_for(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def _event() -> TriggerEvent:
    return TriggerEvent(trigger_id="t", trigger_type="manual", payload={})


def _agent(on_invoke: Any) -> AsyncMock:
    async def ainvoke(inp: dict[str, Any], config: Any = None) -> dict[str, Any]:
        await on_invoke(config)
        return {"messages": [*inp["messages"], AIMessage(content="done")]}

    agent = AsyncMock()
    agent.ainvoke = AsyncMock(side_effect=ainvoke)
    agent.shutdown = AsyncMock()
    return agent


def _two_closes(in_thread: bool) -> Any:
    """An agent run that calls close_ticket twice; the budget allows one."""

    async def call_tools(config: Any) -> None:
        guard = config["callbacks"][0]

        def two_calls() -> None:
            guard.on_tool_start({"name": "close_ticket"}, "", inputs={"ticket_id": "T-1"})
            with pytest.raises(BudgetExceededError):
                guard.on_tool_start({"name": "close_ticket"}, "", inputs={"ticket_id": "T-2"})

        if in_thread:  # a synchronous tool: LangChain runs it in an executor
            await asyncio.to_thread(two_calls)
        else:
            two_calls()

    return call_tools


def _process(level: str) -> AgentProcess:
    return AgentProcess(
        "triage",
        ProcessConfig(
            budget=BudgetConfig(enabled=True, max_tool_calls_per_run=1, on_exceeded="pause"),
            journal=JournalConfig(backend="memory", level=level),  # type: ignore[arg-type]
        ),
    )


async def _entries(process: AgentProcess, entry_type: str) -> list[dict[str, Any]]:
    entries = await process._journal.read("triage")
    return [e.data for e in entries if e.entry_type == entry_type]


def test_guard_puts_the_call_arguments_on_the_violation() -> None:
    denied: list[Any] = []
    budget = BudgetState(BudgetConfig(enabled=True, max_tool_calls_per_run=1))
    guard = RuntimeCallbackHandler(budget=budget, on_budget_denied=denied.append).budget_guard
    assert guard is not None
    guard.on_tool_start({"name": "send"}, "", inputs={"to": "a"})
    with pytest.raises(BudgetExceededError):
        guard.on_tool_start({"name": "send"}, '{"to": "b"}')
    [violation] = denied
    assert violation.arguments == {"to": "b"}
    assert "arguments" not in repr(violation)


@pytest.mark.parametrize("in_thread", [False, True])
async def test_full_journal_records_the_refused_call(in_thread: bool) -> None:
    process = _process("full")
    with patch(
        BUILD_TARGET, new_callable=lambda: AsyncMock(return_value=_agent(_two_closes(in_thread)))
    ):
        await process.start()
        await process.inject(_event())
        await _wait_for(lambda: process.suspend_reason == "budget")
        await process.stop()

    [denied] = await _entries(process, "tool_denied")
    assert denied["tool"] == "close_ticket"
    assert denied["kind"] == "budget"
    assert denied["limit_name"] == "max_tool_calls_per_run"
    assert denied["limit_value"] == 1 and denied["current_value"] == 2
    assert "max_tool_calls_per_run" in denied["reason"]
    assert denied["args"] == {"ticket_id": "T-2"}

    [violation] = await _entries(process, "budget_violation")
    assert violation == {
        "limit_name": "max_tool_calls_per_run",
        "limit_value": 1,
        "current_value": 2,
        "tool_name": "close_ticket",
        "blocked": True,
        "action": "pause",
    }


async def test_checkpoint_journal_records_the_refusal_without_args() -> None:
    process = _process("checkpoint")
    with patch(
        BUILD_TARGET, new_callable=lambda: AsyncMock(return_value=_agent(_two_closes(False)))
    ):
        await process.start()
        await process.inject(_event())
        await _wait_for(lambda: process.suspend_reason == "budget")
        await process.stop()

    [denied] = await _entries(process, "tool_denied")
    assert denied["tool"] == "close_ticket" and "args" not in denied
    assert await _entries(process, "tool_call") == []
    assert len(await _entries(process, "budget_violation")) == 1


async def test_run_refusal_journals_the_violation() -> None:
    process = AgentProcess(
        "triage",
        ProcessConfig(
            budget=BudgetConfig(enabled=True, max_runs_per_day=1, on_exceeded="pause"),
            journal=JournalConfig(backend="memory"),
        ),
    )

    async def nothing(config: Any) -> None:
        return None

    with patch(BUILD_TARGET, new_callable=lambda: AsyncMock(return_value=_agent(nothing))):
        await process.start()
        await process.inject(_event())
        await process.inject(_event())
        await _wait_for(lambda: process.suspend_reason == "budget")
        await process.stop()

    [violation] = await _entries(process, "budget_violation")
    assert violation["limit_name"] == "max_runs_per_day"
    assert violation["tool_name"] is None and violation["blocked"] is False


async def test_permission_denied_hook_gets_the_arguments() -> None:
    seen: list[dict[str, Any]] = []

    async def record(ctx: HookContext) -> None:
        seen.append(dict(ctx.data))

    process = _process("none")
    process.hooks.register(HookEvent.PERMISSION_DENIED, record)
    with patch(
        BUILD_TARGET, new_callable=lambda: AsyncMock(return_value=_agent(_two_closes(False)))
    ):
        await process.start()
        await process.inject(_event())
        await _wait_for(lambda: len(seen) == 1)
        await process.stop()

    [data] = seen
    assert data["kind"] == "budget" and data["tool_name"] == "close_ticket"
    assert data["arguments"] == {"ticket_id": "T-2"}
