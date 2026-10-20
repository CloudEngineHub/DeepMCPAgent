"""Trigger fixes in 1.5.1.

- ``redeliver_dead_letters()`` wakes a process suspended for ``idle_timeout``.
- An ``async def`` filter is rejected (it was never awaited: every event passed).
- Trigger IDs with a function filter are the same in every program run, and
  are hashed with credential-like ``custom_config`` values masked.
- ``GET /triggers`` masks the user info of URL values.
- Event and message triggers count (``dropped_events``) and dead-letter the
  events their full buffer drops.
- ``promptise runtime start/validate --import MODULE`` loads custom trigger
  types; without it both report the unknown type before anything starts.
"""

from __future__ import annotations

import asyncio
import functools
import json
import subprocess
import sys
import textwrap
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from aiohttp import ClientSession
from langchain_core.messages import AIMessage
from pydantic import ValidationError
from typer.testing import CliRunner

from promptise.runtime import (
    AgentRuntime,
    InProcessEventBus,
    OrchestrationAPI,
    ProcessConfig,
    TriggerConfig,
    _cli_state,
)
from promptise.runtime.cli import runtime_app
from promptise.runtime.lifecycle import ProcessState
from promptise.runtime.process import AgentProcess
from promptise.runtime.triggers import FilterExpressionError, unregister_trigger_type
from promptise.runtime.triggers.base import (
    TriggerEvent,
    config_trigger_ids,
    mask_trigger_config,
)
from promptise.runtime.triggers.event import EventTrigger, MessageTrigger
from promptise.runtime.triggers.filters import compile_filter

SRC = Path(__file__).resolve().parents[1] / "src"
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


# ── 1. Redelivery wakes an idle process ───────────────────────────────


async def test_redelivered_event_wakes_an_idle_suspended_process() -> None:
    process = AgentProcess("idle", ProcessConfig())
    with patch(BUILD_TARGET, new_callable=lambda: AsyncMock(return_value=_agent())):
        await process.start()
        try:
            await process.suspend(reason="idle")
            event = TriggerEvent(trigger_id="t", trigger_type="manual", payload={"n": 1})
            process._dead_letter(event, "process stopped")
            assert await process.redeliver_dead_letters() == 1
            assert process.state == ProcessState.RUNNING
            await _wait_for(lambda: process._invocation_count == 1)
        finally:
            await process.stop()


async def test_redelivery_leaves_a_manually_suspended_process_alone() -> None:
    process = AgentProcess("manual", ProcessConfig())
    with patch(BUILD_TARGET, new_callable=lambda: AsyncMock(return_value=_agent())):
        await process.start()
        try:
            await process.suspend()
            process._dead_letter(TriggerEvent(trigger_id="t", trigger_type="manual"), "x")
            assert await process.redeliver_dead_letters() == 1
            assert process.state == ProcessState.SUSPENDED
        finally:
            await process.stop()


# ── 2. Async filters ──────────────────────────────────────────────────


async def _async_filter(event: TriggerEvent) -> bool:
    return False


def test_async_filter_is_rejected() -> None:
    with pytest.raises(FilterExpressionError, match="async"):
        compile_filter(_async_filter)
    with pytest.raises(ValidationError, match="async"):
        TriggerConfig(type="cron", cron_expression="* * * * *", filter_expression=_async_filter)


def test_filter_returning_an_awaitable_skips_the_event() -> None:
    # a plain function returning a coroutine passes the check at compile time
    with pytest.raises(FilterExpressionError):
        compile_filter(functools.partial(_async_filter))
    match = compile_filter(lambda event: _async_filter(event))
    assert match(TriggerEvent(trigger_id="t", trigger_type="manual")) is False


def test_sync_filter_still_works() -> None:
    match = compile_filter(lambda event: event.payload.get("ok"))
    assert match(TriggerEvent(trigger_id="t", trigger_type="manual", payload={"ok": True}))
    assert not match(TriggerEvent(trigger_id="t", trigger_type="manual", payload={}))


# ── 3/4. Stable, secret-free trigger IDs ──────────────────────────────


def test_function_filter_id_is_the_same_in_every_run(tmp_path: Path) -> None:
    script = tmp_path / "ids.py"
    script.write_text(
        textwrap.dedent(
            """
            from promptise.runtime import TriggerConfig
            from promptise.runtime.triggers.base import config_trigger_ids

            def only_bugs(event):
                return event.payload.get("label") == "bug"

            print(config_trigger_ids([TriggerConfig(
                type="cron", cron_expression="0 * * * *", filter_expression=only_bugs
            )])[0])
            """
        )
    )
    env = {"PYTHONPATH": str(SRC), "PATH": "/usr/bin:/bin"}
    ids = {
        subprocess.run(
            [sys.executable, str(script)], capture_output=True, text=True, env=env, check=True
        ).stdout.strip()
        for _ in range(2)
    }
    assert len(ids) == 1 and next(iter(ids)).startswith("cron-")


def test_id_does_not_depend_on_credential_values() -> None:
    def trigger(**custom: Any) -> TriggerConfig:
        return TriggerConfig(type="sqs-151", custom_config={"queue": "orders", **custom})

    a, b, c = config_trigger_ids([trigger(api_token="one")]), [], []
    b = config_trigger_ids([trigger(api_token="two")])
    c = config_trigger_ids([trigger(api_token="two", region="eu")])
    assert a == b  # only the secret differs: same ID, so the secret isn't in it
    assert a != c
    d = config_trigger_ids([trigger(dsn="postgres://app:one@db/x")])
    e = config_trigger_ids([trigger(dsn="postgres://app:two@db/x")])
    assert d == e


# ── 5. URL user info masked ───────────────────────────────────────────


def test_mask_trigger_config_masks_url_user_info() -> None:
    masked = mask_trigger_config(
        {
            "type": "x",
            "signature_header": "X-Signature",
            "custom_config": {
                "dsn": "postgres://app:s3cret@db:5432/orders",
                "nested": {"urls": ["https://user:pw@example.com/a", "https://example.com/b"]},
                "password": "hunter2",
                "region": "eu",
            },
        }
    )
    text = json.dumps(masked)
    assert "s3cret" not in text and "hunter2" not in text and "user:pw" not in text
    assert masked["custom_config"]["dsn"] == "postgres://**********@db:5432/orders"
    assert masked["custom_config"]["nested"]["urls"][1] == "https://example.com/b"
    assert masked["custom_config"]["region"] == "eu"
    assert masked["signature_header"] == "X-Signature"  # not a secret


async def test_get_triggers_masks_url_user_info() -> None:
    runtime = AgentRuntime()
    await runtime.add_process(
        "p",
        ProcessConfig(
            triggers=[
                TriggerConfig(
                    type="cron",
                    cron_expression="0 * * * *",
                    custom_config={"callback": "https://bot:t0ken@hooks.example.com/x"},
                )
            ]
        ),
    )
    api = OrchestrationAPI(runtime, host="127.0.0.1", port=0)
    await api.start()
    try:
        async with ClientSession() as session:
            url = f"http://127.0.0.1:{api.port}/api/v1/processes/p/triggers"
            async with session.get(url) as resp:
                assert resp.status == 200
                body = await resp.json()
    finally:
        await api.stop()
    config = body["triggers"][0]["config"]
    assert config["custom_config"]["callback"] == "https://**********@hooks.example.com/x"
    assert "t0ken" not in json.dumps(body)


# ── 6. Event and message trigger overflow ─────────────────────────────


async def test_event_trigger_counts_and_hands_on_dropped_events() -> None:
    bus = InProcessEventBus()
    trigger = EventTrigger(bus, "orders.created")
    dropped: list[TriggerEvent] = []
    trigger.set_overflow_handler(dropped.append)
    await trigger.start()
    try:
        for i in range(103):
            await bus.emit("orders.created", {"i": i})
    finally:
        await trigger.stop()
    assert trigger.dropped_events == 3
    assert [e.payload["data"]["i"] for e in dropped] == [100, 101, 102]


async def test_message_trigger_counts_dropped_messages() -> None:
    handlers: list[Any] = []

    class _Broker:
        async def subscribe(self, topic: str, handler: Any) -> str:
            handlers.append(handler)
            return "sub-1"

        async def unsubscribe(self, subscription_id: str) -> None:
            pass

    trigger = MessageTrigger(_Broker(), "reports")
    await trigger.start()
    try:
        for i in range(101):
            await handlers[0](type("M", (), {"content": str(i)})())
    finally:
        await trigger.stop()
    assert trigger.dropped_events == 1


async def test_process_dead_letters_dropped_events_and_reports_them() -> None:
    bus = InProcessEventBus()
    process = AgentProcess(
        "events",
        ProcessConfig(triggers=[TriggerConfig(type="event", event_type="orders.created")]),
        event_bus=bus,
    )
    with patch(BUILD_TARGET, new_callable=lambda: AsyncMock(return_value=_agent())):
        await process.start()
        try:
            trigger = process._triggers[0]
            await _wait_for(lambda: trigger._on_overflow is not None)
            event = TriggerEvent(trigger_id=trigger.trigger_id, trigger_type="event")
            trigger._overflow(event, "event")
            assert process.dead_letters[-1]["reason"] == "trigger buffer full"
            status = process.status()["triggers"][0]
            assert status["dropped_events"] == 1
        finally:
            await process.stop()


# ── 7. Custom trigger types from the CLI ──────────────────────────────

_PLUGIN = """
from promptise.runtime.triggers import register_trigger_type
from promptise.runtime.triggers.cron import CronTrigger


def _factory(config, *, event_bus=None, broker=None):
    return CronTrigger("0 0 1 1 *")


register_trigger_type("nightly_151", _factory, overwrite=True)
"""

_MANIFEST = """name: custom
model: openai:gpt-5-mini
instructions: hi
triggers:
  - type: nightly_151
"""


@pytest.fixture
def plugin_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv(_cli_state.STATE_DIR_ENV, str(tmp_path / "state"))
    monkeypatch.delenv(_cli_state.TOKEN_ENV, raising=False)
    (tmp_path / "plugin_151.py").write_text(_PLUGIN)
    (tmp_path / "custom.agent").write_text(_MANIFEST)
    monkeypatch.chdir(tmp_path)
    yield tmp_path
    unregister_trigger_type("nightly_151")
    sys.modules.pop("plugin_151", None)


def test_validate_reports_an_unregistered_trigger_type(plugin_dir: Path) -> None:
    unregister_trigger_type("nightly_151")
    result = CliRunner().invoke(runtime_app, ["validate", "custom.agent"])
    output = " ".join(result.output.split())
    assert result.exit_code == 1
    assert "unknown trigger type 'nightly_151'" in output and "--import" in output


def test_validate_with_import_accepts_the_custom_type(plugin_dir: Path) -> None:
    result = CliRunner().invoke(runtime_app, ["validate", "custom.agent", "--import", "plugin_151"])
    assert result.exit_code == 0, result.output
    assert "Validation complete" in result.output


def test_start_reports_the_unregistered_type_before_starting(plugin_dir: Path) -> None:
    unregister_trigger_type("nightly_151")
    result = CliRunner().invoke(runtime_app, ["start", "."])
    output = " ".join(result.output.split())
    assert result.exit_code == 1
    assert "unknown trigger type 'nightly_151'" in output
    assert "nothing was started" in output


def test_import_of_a_missing_module_is_a_clean_error(plugin_dir: Path) -> None:
    result = CliRunner().invoke(runtime_app, ["validate", "custom.agent", "--import", "nope_151"])
    assert result.exit_code == 1
    assert "--import nope_151: ModuleNotFoundError" in result.output
