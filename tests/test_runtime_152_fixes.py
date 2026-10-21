"""Runtime fixes in 1.5.2.

- ``RuntimeCoordinator.get_node_status()`` raises ``NodeUnreachableError``
  (a RuntimeError) for a node it can't reach, as documented, instead of a
  plain RuntimeError.
- An event or message trigger's overflow handler is set when the trigger
  is created, so an event its full buffer drops before the trigger's
  listener runs is dead-lettered (reason ``"trigger buffer full"``), not
  only counted.
"""

from __future__ import annotations

import socket
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage

from promptise.runtime import (
    BusEvent,
    ExecutionMode,
    InProcessEventBus,
    ProcessConfig,
    TriggerConfig,
)
from promptise.runtime.distributed import NodeUnreachableError, RuntimeCoordinator
from promptise.runtime.process import AgentProcess
from promptise.runtime.triggers.event import _BUFFER_SIZE

BUILD_TARGET = "promptise.agent.build_agent"


def _agent() -> AsyncMock:
    async def ainvoke(inp: dict[str, Any], config: Any = None) -> dict[str, Any]:
        return {"messages": [*inp["messages"], AIMessage(content="done")]}

    agent = AsyncMock()
    agent.ainvoke = AsyncMock(side_effect=ainvoke)
    agent.shutdown = AsyncMock()
    return agent


def _patch_build() -> Any:
    return patch(BUILD_TARGET, new_callable=lambda: AsyncMock(return_value=_agent()))


# ── 1. get_node_status() keeps NodeUnreachableError ───────────────────


def _closed_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


async def test_get_node_status_raises_node_unreachable_error() -> None:
    coordinator = RuntimeCoordinator()
    coordinator.register_node("w1", f"http://127.0.0.1:{_closed_port()}")
    with pytest.raises(NodeUnreachableError) as info:
        await coordinator.get_node_status("w1")
    assert isinstance(info.value, RuntimeError)  # `except RuntimeError` still works
    assert info.value.node_id == "w1"
    assert str(info.value).startswith("Status request failed: ")
    assert "unreachable" in str(info.value)
    assert isinstance(info.value.__cause__, NodeUnreachableError)


async def test_get_node_status_error_status_stays_a_plain_runtime_error() -> None:
    coordinator = RuntimeCoordinator()
    coordinator.register_node("w1", "http://127.0.0.1:1")
    failure = RuntimeError("GET /status on node 'w1' failed: 500 boom")
    with (
        patch.object(coordinator, "_request", AsyncMock(side_effect=failure)),
        pytest.raises(RuntimeError, match="Status request failed: GET /status") as info,
    ):
        await coordinator.get_node_status("w1")
    assert not isinstance(info.value, NodeUnreachableError)


# ── 2. Drops before the listener runs are dead-lettered ───────────────


class _BacklogBus(InProcessEventBus):
    """A bus that hands a new subscriber its backlog at once (a replay)."""

    def __init__(self) -> None:
        super().__init__()
        self.backlog = 0

    async def subscribe(self, event_type: Any, handler: Any) -> None:
        await super().subscribe(event_type, handler)
        for i in range(self.backlog):
            await handler(BusEvent(event_type=str(event_type), data={"i": i}))


class _BacklogBroker:
    """A message broker that hands a new subscriber its backlog at once."""

    def __init__(self, backlog: int) -> None:
        self.backlog = backlog

    async def subscribe(self, topic: str, handler: Any) -> str:
        for i in range(self.backlog):
            await handler(type("Message", (), {"message_id": f"m{i}", "content": str(i)})())
        return "sub-1"

    async def unsubscribe(self, subscription_id: str) -> None:
        pass


def _buffer_full_letters(process: AgentProcess) -> list[dict[str, Any]]:
    return [d for d in process.dead_letters if d["reason"] == "trigger buffer full"]


async def test_event_dropped_while_the_process_starts_is_dead_lettered() -> None:
    bus = _BacklogBus()
    bus.backlog = _BUFFER_SIZE + 1  # one more than the trigger buffers
    process = AgentProcess(
        "events",
        ProcessConfig(triggers=[TriggerConfig(type="event", event_type="orders.created")]),
        event_bus=bus,
    )
    with _patch_build():
        await process.start()
        try:
            # Dropped inside trigger.start(), before any listener ran.
            letters = _buffer_full_letters(process)
            assert [d["event"].payload["data"] for d in letters] == [{"i": _BUFFER_SIZE}]
            assert process.status()["triggers"][0]["dropped_events"] == 1
        finally:
            await process.stop()


async def test_message_dropped_by_an_added_trigger_is_dead_lettered() -> None:
    process = AgentProcess(
        "messages", ProcessConfig(), broker=_BacklogBroker(backlog=_BUFFER_SIZE + 2)
    )
    with _patch_build():
        await process.start()
        try:
            await process.add_trigger(TriggerConfig(type="message", topic="reports"))
            letters = _buffer_full_letters(process)
            assert [d["event"].payload["content"] for d in letters] == [
                str(_BUFFER_SIZE),
                str(_BUFFER_SIZE + 1),
            ]
            assert process.status()["triggers"][0]["dropped_events"] == 2
        finally:
            await process.stop()


async def test_event_dropped_by_an_agent_added_trigger_is_dead_lettered() -> None:
    bus = _BacklogBus()
    process = AgentProcess("open", ProcessConfig(execution_mode=ExecutionMode.OPEN), event_bus=bus)
    with _patch_build():
        await process.start()
        try:
            bus.backlog = _BUFFER_SIZE + 1
            await process.add_dynamic_trigger({"type": "event", "event_type": "orders.created"})
            assert len(_buffer_full_letters(process)) == 1
            assert process.status()["triggers"][0]["dropped_events"] == 1
        finally:
            await process.stop()
