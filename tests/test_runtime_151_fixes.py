"""Runtime fixes in 1.5.1.

- ``COMMANDS_NOT_ALLOWED`` no longer suggests callback hooks, which a JSON
  deploy can't carry.
- ``promptise runtime rewind --plan`` says the affected count includes the
  target entry.
- The ``low_confidence`` mission escalation names the process
  (``process_name``), like the budget and health escalations.
- ``promptise runtime start`` runs its processes with an in-process event
  bus, so escalations with an ``event_type`` and ``event`` triggers work;
  without a bus the warning says how to fix it.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage
from rich.console import Console

from promptise.runtime import (
    AgentRuntime,
    InProcessEventBus,
    MissionConfig,
    ProcessConfig,
)
from promptise.runtime.config import EscalationTarget
from promptise.runtime.mission import MissionEvaluation
from promptise.runtime.process import AgentProcess
from promptise.runtime.triggers.base import TriggerEvent
from promptise.runtime.triggers.event import EventTrigger

BUILD_TARGET = "promptise.agent.build_agent"


async def _wait_for(cond: Callable[[], bool], timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not cond():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError("condition not met in time")
        await asyncio.sleep(0.01)


def _agent() -> AsyncMock:
    async def ainvoke(inp: dict[str, Any], config: Any = None) -> dict[str, Any]:
        return {"messages": [*inp["messages"], AIMessage(content="done")]}

    agent = AsyncMock()
    agent.ainvoke = AsyncMock(side_effect=ainvoke)
    agent.shutdown = AsyncMock()
    return agent


# ── COMMANDS_NOT_ALLOWED message ──────────────────────────────────────


def test_commands_not_allowed_does_not_suggest_callback_hooks() -> None:
    from promptise.runtime.api import OrchestrationAPI

    config = ProcessConfig(hooks=[{"event": "user_prompt_submit", "command": ["true"]}])
    response = OrchestrationAPI._refuse_commands(
        SimpleNamespace(_allow_command_deploys=False), config
    )
    assert response is not None and response.status == 403
    message = json.loads(response.text)["error"]["message"]
    assert "callback hooks" not in message
    assert "HTTP MCP servers instead of stdio ones" in message
    assert "leave out the command hooks" in message
    assert "--allow-command-deploys" in message


# ── rewind --plan wording ─────────────────────────────────────────────


def test_rewind_plan_counts_the_target(monkeypatch: pytest.MonkeyPatch) -> None:
    from promptise.runtime import cli as runtime_cli

    out = io.StringIO()
    monkeypatch.setattr(runtime_cli, "console", Console(file=out, width=200))
    runtime_cli._print_rewind_plan(
        "notes",
        {
            "mode": "both",
            "target_entry_id": "7c7b",
            "target_entry_type": "invocation_result",
            "entries_affected": 2,
            "entries_total": 6,
            "conversation_entries_affected": 1,
            "code_entries_affected": 0,
        },
    )
    text = out.getvalue()
    assert "2 of 6 journal entries: the target and the 1 after it" in text
    assert "journal entries after it" not in text


# ── low_confidence escalation payload ─────────────────────────────────


async def test_low_confidence_escalation_names_the_process() -> None:
    sent: list[dict[str, Any]] = []

    async def fake_escalate(target: Any, payload: dict[str, Any], *, event_bus: Any = None):
        sent.append(payload)

    process = AgentProcess(
        "watcher",
        ProcessConfig(
            mission=MissionConfig(
                enabled=True,
                objective="Keep the queue empty",
                success_criteria="Queue is empty",
                confidence_threshold=0.7,
                escalation=EscalationTarget(event_type="mission.low_confidence"),
            )
        ),
    )
    low = MissionEvaluation(
        achieved=False, confidence=0.2, reasoning="unclear", progress_summary="none"
    )
    with (
        patch(BUILD_TARGET, new_callable=lambda: AsyncMock(return_value=_agent())),
        patch("promptise.runtime.escalation.escalate", fake_escalate),
    ):
        await process.start()
        try:
            process._mission.evaluate = AsyncMock(return_value=low)  # type: ignore[union-attr]
            await process.inject(TriggerEvent(trigger_id="t", trigger_type="manual", payload={}))
            await _wait_for(lambda: bool(sent))
        finally:
            await process.stop()

    payload = sent[0]
    assert payload["type"] == "low_confidence"
    assert payload["process_name"] == "watcher"
    assert payload["process_id"] == process.process_id


# ── In-process event bus ──────────────────────────────────────────────


def test_cli_runtime_has_an_event_bus() -> None:
    from promptise.runtime import cli as runtime_cli

    runtime = runtime_cli._new_runtime()
    assert isinstance(runtime, AgentRuntime)
    assert isinstance(runtime._event_bus, InProcessEventBus)


async def test_escalation_event_reaches_an_event_trigger() -> None:
    from promptise.runtime.escalation import escalate

    bus = InProcessEventBus()
    trigger = EventTrigger(bus, "ops.escalation")
    await trigger.start()
    try:
        await escalate(
            EscalationTarget(event_type="ops.escalation"),
            {"type": "low_confidence", "process_name": "watcher"},
            event_bus=bus,
        )
        event = await asyncio.wait_for(trigger.wait_for_next(), timeout=2)
    finally:
        await trigger.stop()
    assert event.payload["event_type"] == "ops.escalation"
    assert event.payload["data"] == {"type": "low_confidence", "process_name": "watcher"}


async def test_event_bus_delivery_and_failing_handler() -> None:
    bus = InProcessEventBus()
    got: list[Any] = []

    async def bad(event: Any) -> None:
        raise RuntimeError("boom")

    async def good(event: Any) -> None:
        got.append(event)

    await bus.subscribe("x", bad)
    await bus.subscribe("x", good)
    assert await bus.emit("x", {"a": 1}, source="s") == 1
    assert got[0].data == {"a": 1} and got[0].source == "s" and got[0].event_type == "x"
    await bus.unsubscribe("x", good)
    await bus.unsubscribe("x", good)  # not subscribed any more: no error
    assert bus.subscriber_count("x") == 1
    assert await bus.emit("nobody-listens", {}) == 0


async def test_escalation_warnings_say_how_to_fix(caplog: pytest.LogCaptureFixture) -> None:
    from promptise.runtime.escalation import escalate

    with caplog.at_level(logging.WARNING, logger="promptise.runtime.escalation"):
        await escalate(EscalationTarget(event_type="alerts"), {"type": "x"})
        await escalate(
            EscalationTarget(event_type="alerts"), {"type": "x"}, event_bus=InProcessEventBus()
        )
    messages = [r.getMessage() for r in caplog.records]
    assert any("no event bus" in m and "AgentRuntime(event_bus=" in m for m in messages)
    assert any("reached no subscriber" in m for m in messages)
