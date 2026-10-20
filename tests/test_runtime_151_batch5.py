"""Runtime fixes in 1.5.1 (stop/hook race, refused runs, budget, cluster).

- A ``stop()`` during a finished run's ``task_completed`` hook no longer
  dead-letters the event (a redelivery ran the finished run again).
- A run refused by ``max_runs_per_day`` reports outcome ``"refused"`` and is
  dead-lettered with reason ``"budget: max_runs_per_day"``, so it can be
  redelivered after the reset.
- A call an approval gate denies gives its irreversible action back.
- An unreachable cluster node raises ``NodeUnreachableError`` (a RuntimeError).
- The refused node id is logged with single quotes.
- A budget refusal is not logged as a LangChain callback error.
"""

from __future__ import annotations

import asyncio
import logging
import socket
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import ClientSession
from langchain_core.messages import AIMessage
from langchain_core.tools import tool

from promptise.approval import (
    ApprovalDecision,
    ApprovalPolicy,
    CallbackApprovalHandler,
    on_approval_denied,
    wrap_tools_with_approval,
)
from promptise.runtime import AgentRuntime, BudgetConfig, ProcessConfig, ToolCostAnnotation
from promptise.runtime.budget import BudgetState
from promptise.runtime.callbacks import BudgetGuard
from promptise.runtime.exceptions import BudgetExceededError
from promptise.runtime.hooks import HookEvent
from promptise.runtime.process import AgentProcess
from promptise.runtime.triggers.base import TriggerEvent

BUILD_TARGET = "promptise.agent.build_agent"


def _agent() -> AsyncMock:
    async def ainvoke(inp: dict[str, Any], config: Any = None) -> dict[str, Any]:
        return {"messages": [*inp["messages"], AIMessage(content="done")]}

    agent = AsyncMock()
    agent.ainvoke = AsyncMock(side_effect=ainvoke)
    agent.shutdown = AsyncMock()
    return agent


async def _wait_for(cond: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def _event(n: int = 0) -> TriggerEvent:
    return TriggerEvent(trigger_id="t", trigger_type="manual", payload={"n": n})


# ── 1. stop() while task_completed hooks run ──────────────────────────


async def test_stop_during_task_completed_hook_does_not_dead_letter_the_event() -> None:
    process = AgentProcess("orders", ProcessConfig())
    in_hook = asyncio.Event()
    release = asyncio.Event()
    outcomes: list[str] = []

    async def slow_hook(ctx: Any) -> None:
        outcomes.append(ctx.data["outcome"])
        in_hook.set()
        await release.wait()  # still running when stop() arrives

    process.hooks.register(HookEvent.TASK_COMPLETED, slow_hook)
    with patch(BUILD_TARGET, new_callable=lambda: AsyncMock(return_value=_agent())):
        await process.start()
        event = _event(1)
        await process.inject(event)
        await asyncio.wait_for(in_hook.wait(), 5)
        stopping = asyncio.ensure_future(process.stop())
        await asyncio.sleep(0.05)
        release.set()
        await asyncio.wait_for(stopping, 10)

    assert outcomes == ["success"]
    assert process._invocation_count == 1
    assert [d["event"].event_id for d in process.dead_letters] == []
    assert await process.redeliver_dead_letters() == 0  # nothing to run again


async def test_stop_still_dead_letters_an_event_that_did_not_run() -> None:
    process = AgentProcess("orders", ProcessConfig())
    started = asyncio.Event()

    async def slow_run(inp: dict[str, Any], config: Any = None) -> dict[str, Any]:
        started.set()
        await asyncio.sleep(30)
        return {"messages": inp["messages"]}

    agent = _agent()
    agent.ainvoke = AsyncMock(side_effect=slow_run)
    with patch(BUILD_TARGET, new_callable=lambda: AsyncMock(return_value=agent)):
        await process.start()
        await process.inject(_event(2))
        await asyncio.wait_for(started.wait(), 5)
        await process.stop()
    [entry] = process.dead_letters
    assert entry["reason"] == "process stopped" and entry["error"] == "run cancelled"


# ── 2. A run refused by max_runs_per_day ──────────────────────────────


async def test_refused_run_is_dead_lettered_and_can_be_redelivered() -> None:
    process = AgentProcess(
        "daily",
        ProcessConfig(
            budget=BudgetConfig(enabled=True, max_runs_per_day=1, on_exceeded="pause")
        ),
    )
    outcomes: list[tuple[str, Any]] = []

    async def record(ctx: Any) -> None:
        outcomes.append((ctx.data["outcome"], ctx.data["error"]))

    process.hooks.register(HookEvent.TASK_COMPLETED, record)
    with patch(BUILD_TARGET, new_callable=lambda: AsyncMock(return_value=_agent())):
        await process.start()
        try:
            await process.inject(_event(1))
            await _wait_for(lambda: len(outcomes) == 1)
            await process.inject(_event(2))
            await _wait_for(lambda: len(outcomes) == 2)
            assert outcomes[0] == ("success", None)
            assert outcomes[1][0] == "refused"
            assert "max_runs_per_day" in outcomes[1][1]
            [entry] = process.dead_letters
            assert entry["reason"] == "budget: max_runs_per_day"
            assert entry["event"].payload == {"n": 2}
            assert process._invocation_count == 1
            assert process._consecutive_failures == 0

            # After the daily reset, the refused event runs when redelivered
            process._budget.daily_runs = 0
            await process.resume()
            assert await process.redeliver_dead_letters() == 1
            await _wait_for(lambda: len(outcomes) == 3)
            assert outcomes[2] == ("success", None)
            assert process._invocation_count == 2
        finally:
            await process.stop()


# ── 3. An approval-denied call gives its irreversible action back ─────


async def test_denied_call_does_not_use_up_max_irreversible_per_run() -> None:
    budget = BudgetState(
        BudgetConfig(
            enabled=True,
            max_irreversible_per_run=1,
            tool_costs={"wire_money": ToolCostAnnotation(irreversible=True)},
        )
    )
    ran: list[int] = []

    @tool
    async def wire_money(amount: int) -> str:
        """Wire money."""
        ran.append(amount)
        return "sent"

    answers = [False, True]

    async def decide(request: Any) -> ApprovalDecision:
        return ApprovalDecision(approved=answers.pop(0), reason="reviewer")

    [gated] = wrap_tools_with_approval(
        [wire_money],
        ApprovalPolicy(tools=["wire_money"], handler=CallbackApprovalHandler(decide)),
    )
    guard = BudgetGuard(budget, [])
    with on_approval_denied(budget.refund_irreversible):
        denied = await gated.ainvoke({"amount": 1}, config={"callbacks": [guard]})
        assert str(denied).startswith("DENIED")
        assert budget.run_irreversible == 0  # the denied call gave it back
        # The cap still allows one irreversible call, which is approved
        assert await gated.ainvoke({"amount": 2}, config={"callbacks": [guard]}) == "sent"
        assert budget.run_irreversible == 1
        with pytest.raises(BudgetExceededError):
            await gated.ainvoke({"amount": 3}, config={"callbacks": [guard]})
    assert ran == [2]
    assert budget.run_tool_calls == 2  # a denied call is still a tool call


# ── 4/5. Cluster: unreachable node, refused node id ───────────────────


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_unreachable_node_raises_node_unreachable_error() -> None:
    from promptise.runtime.distributed import NodeUnreachableError, RuntimeCoordinator

    coordinator = RuntimeCoordinator()
    coordinator.register_node("w1", f"http://127.0.0.1:{_closed_port()}")
    with pytest.raises(NodeUnreachableError) as info:
        await coordinator.start_process_on_node("w1", "watcher")
    assert isinstance(info.value, RuntimeError)
    assert info.value.node_id == "w1"
    assert "unreachable" in str(info.value)
    with pytest.raises(RuntimeError, match="Status request failed"):
        await coordinator.get_node_status("w1")


async def test_refused_node_is_logged_with_single_quotes(caplog: pytest.LogCaptureFixture) -> None:
    from promptise.runtime.distributed import RuntimeCoordinator
    from promptise.runtime.distributed.discovery import RegistryDiscovery
    from promptise.runtime.distributed.transport import RuntimeTransport

    transport = RuntimeTransport(
        AgentRuntime(),
        port=0,
        node_id="coord",
        auth_token="cluster",
        coordinator=RuntimeCoordinator(auth_token="cluster"),
        discovery=RegistryDiscovery(),
        allowed_node_hosts=["*.cluster.internal"],
    )
    await transport.start()
    try:
        with caplog.at_level(logging.WARNING):
            async with ClientSession() as session:
                async with session.post(
                    f"http://127.0.0.1:{transport.port}/cluster/nodes",
                    json={"node_id": "w2", "url": "http://169.254.169.254"},
                    headers={"Authorization": "Bearer cluster"},
                ) as resp:
                    assert resp.status == 403
    finally:
        await transport.stop()
    [message] = [r.getMessage() for r in caplog.records if "refused to register" in r.getMessage()]
    assert "node 'w2' at" in message and "\"'w2'\"" not in message


# ── 6. A budget refusal is not logged as a callback crash ─────────────


async def test_budget_refusal_is_not_logged_as_a_callback_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    budget = BudgetState(BudgetConfig(enabled=True, max_tool_calls_per_run=1))

    @tool
    async def lookup(q: str) -> str:
        """Look something up."""
        return "x"

    guard = BudgetGuard(budget, [])
    await lookup.ainvoke({"q": "a"}, config={"callbacks": [guard]})
    with caplog.at_level(logging.INFO), pytest.raises(BudgetExceededError):
        await lookup.ainvoke({"q": "b"}, config={"callbacks": [guard]})
    messages = [r.getMessage() for r in caplog.records]
    assert not any("Error in BudgetGuard" in m for m in messages), messages
    assert any("Budget refused tool call lookup" in m for m in messages), messages
