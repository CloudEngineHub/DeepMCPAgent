"""Runtime CLI, manifest and dashboard fixes in 1.5.1.

- An unset ``${VAR}`` in a manifest is a clean error: ``runtime validate``
  reports it per file and goes on; ``runtime start`` checks every manifest
  first and starts nothing (it printed a traceback before).
- ``config:``, ``memory:`` and top-level keys get "did you mean" hints.
- Dashboard: the Processes tab always shows Name (less important columns
  go first), the Logs tab gives the journal panel its share of the
  height, the header's Events counter counts events, and the Events tab
  shows a payload preview.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from langchain_core.messages import AIMessage
from rich.console import Console
from typer.testing import CliRunner

from promptise.exceptions import EnvVarNotFoundError
from promptise.runtime import (
    ManifestEnvVarError,
    ManifestError,
    ManifestValidationError,
    ProcessConfig,
    _cli_state,
)
from promptise.runtime._dashboard import (
    EventLog,
    InvocationLog,
    ProcessSnapshot,
    RuntimeDashboard,
    RuntimeDashboardState,
    RuntimeDataCollector,
    _process_columns,
)
from promptise.runtime.cli import runtime_app
from promptise.runtime.hooks import HookEvent
from promptise.runtime.manifest import load_manifest
from promptise.runtime.process import AgentProcess
from promptise.runtime.triggers.base import TriggerEvent

runner = CliRunner()

_UNSET = "PROMPTISE_TEST_SURELY_UNSET_151"
_GOOD = "name: good\nmodel: openai:gpt-5-mini\ninstructions: hi\n"
_BAD = f"name: bad\nmodel: openai:gpt-5-mini\ninstructions: ${{{_UNSET}}}\n"


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "state"
    monkeypatch.setenv(_cli_state.STATE_DIR_ENV, str(path))
    monkeypatch.delenv(_cli_state.TOKEN_ENV, raising=False)
    monkeypatch.delenv(_UNSET, raising=False)
    return path


# ── Unset ${VAR}: a clean error ───────────────────────────────────────


def test_unset_var_is_a_manifest_error(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(_UNSET, raising=False)
    bad = tmp_path / "bad.agent"
    bad.write_text(_BAD)
    with pytest.raises(ManifestEnvVarError) as exc:
        load_manifest(bad)
    # Caught as a manifest problem, and as what load_manifest raised before
    assert isinstance(exc.value, ManifestError)
    assert isinstance(exc.value, EnvVarNotFoundError)
    assert str(bad) in str(exc.value) and _UNSET in str(exc.value)


def test_validate_reports_each_file_and_goes_on(tmp_path: Path, state_dir: Path) -> None:
    (tmp_path / "a_bad.agent").write_text(_BAD)
    (tmp_path / "b_good.agent").write_text(_GOOD)
    result = runner.invoke(runtime_app, ["validate", str(tmp_path)])
    output = " ".join(result.output.split())  # Rich wraps long lines
    assert result.exit_code == 1
    assert "Traceback" not in output
    assert f"Environment variable '{_UNSET}' not found" in output
    # The file after the bad one is still validated
    assert "b_good.agent" in output and "Validation complete" in output
    assert "1 of 2 manifest(s) failed" in output


@pytest.mark.parametrize("target", ["file", "directory"])
def test_start_refuses_before_starting_anything(
    tmp_path: Path, state_dir: Path, target: str
) -> None:
    (tmp_path / "a_bad.agent").write_text(_BAD)
    (tmp_path / "b_good.agent").write_text(_GOOD)
    path = tmp_path / "a_bad.agent" if target == "file" else tmp_path
    result = runner.invoke(runtime_app, ["start", str(path)])
    output = " ".join(result.output.split())  # Rich wraps long lines
    assert result.exit_code == 1
    assert "Traceback" not in output
    assert f"Environment variable '{_UNSET}' not found" in output
    if target == "directory":
        assert "nothing was started" in output
    assert "Loaded" not in output and "All processes stopped" not in output


# ── "did you mean" hints ──────────────────────────────────────────────


@pytest.mark.parametrize(
    "extra, hint",
    [
        ("trigers: []\n", "'trigers' -> 'triggers'"),
        ("config:\n  concurency: 2\n", "'concurency' -> 'concurrency'"),
        ("memory:\n  provder: chroma\n", "'provder' -> 'provider'"),
    ],
)
def test_unknown_keys_get_did_you_mean_hints(tmp_path: Path, extra: str, hint: str) -> None:
    file = tmp_path / "typo.agent"
    file.write_text(_GOOD + extra)
    with pytest.raises(ManifestValidationError) as exc:
        load_manifest(file)
    assert f"did you mean {hint}?" in str(exc.value)


# ── Dashboard ─────────────────────────────────────────────────────────


def _snapshot(name: str) -> ProcessSnapshot:
    return ProcessSnapshot(
        name=name,
        process_id="abcdef0123456789",
        state="running",
        model="openai:gpt-5-mini",
        invocation_count=3,
        consecutive_failures=0,
        trigger_count=1,
        queue_size=0,
        uptime_seconds=12.0,
        concurrency=1,
        heartbeat_interval=10.0,
    )


def _text(renderable: Any, width: int, height: int = 50) -> str:
    console = Console(width=width, height=height, record=True, color_system=None)
    console.print(renderable)
    return console.export_text()


@pytest.mark.parametrize("width", [60, 80, 100, 120, 140])
def test_processes_tab_always_shows_the_name(width: int) -> None:
    state = RuntimeDashboardState()
    state.processes["invoice-watcher"] = _snapshot("invoice-watcher")
    dashboard = RuntimeDashboard(state)
    dashboard._console = Console(width=width)
    text = _text(dashboard._tab_processes(), width)
    assert "invoice-watcher" in text
    assert "Name" in text and "State" in text
    if width >= 80:
        assert "Model" in text and "gpt-5-mini" in text


def test_processes_columns_drop_least_important_first() -> None:
    assert [c.header for c in _process_columns(200)] == [
        "Name",
        "State",
        "PID",
        "Model",
        "Invocations",
        "Failures",
        "Triggers",
        "Queue",
        "Conc.",
        "Heartbeat",
        "Uptime",
    ]
    narrow = [c.header for c in _process_columns(80)]
    assert narrow[:3] == ["Name", "State", "Model"]
    assert "Heartbeat" not in narrow and "PID" not in narrow
    assert [c.header for c in _process_columns(10)] == ["Name"]


def test_logs_tab_shows_the_journal_panel_on_a_normal_terminal() -> None:
    import logging

    state = RuntimeDashboardState()
    for i in range(5):
        state.record_journal(
            {"timestamp": time.time(), "entry_type": "checkpoint", "process_id": "p", "data": {}}
        )
    dashboard = RuntimeDashboard(state)
    for i in range(120):
        dashboard._log_capture.emit(
            logging.LogRecord("promptise.runtime", logging.DEBUG, __file__, 1, f"m{i}", (), None)
        )
    dashboard._current_tab = 4  # Logs
    text = _text(dashboard._render(), width=120, height=45)
    assert "Runtime Logs" in text
    assert "Journal Entries" in text
    assert "checkpoint" in text


def test_header_events_counter_counts_events() -> None:
    state = RuntimeDashboardState()
    for i in range(3):
        state.record_event(
            EventLog(
                timestamp=time.time(),
                process_name="p",
                trigger_type="manual",
                trigger_id="t",
                event_id=f"e{i}",
                payload_summary="-",
            )
        )
    state.record_invocation(
        InvocationLog(
            timestamp=time.time(),
            process_name="p",
            trigger_type="manual",
            trigger_id="t",
            success=True,
            duration_ms=1.0,
        )
    )
    text = _text(RuntimeDashboard(state)._header(), width=120)
    assert "Events     3" in text
    assert "1 runs" in text


async def test_events_tab_payload_comes_from_the_trigger_event() -> None:
    async def ainvoke(inp: dict[str, Any], config: Any = None) -> dict[str, Any]:
        return {"messages": [*inp["messages"], AIMessage(content="done")]}

    agent = AsyncMock()
    agent.ainvoke = AsyncMock(side_effect=ainvoke)
    agent.shutdown = AsyncMock()

    process = AgentProcess("watcher", ProcessConfig())
    runtime = type("_Runtime", (), {"processes": {"watcher": process}})()
    state = RuntimeDashboardState()
    collector = RuntimeDataCollector(runtime, state, loop=asyncio.get_running_loop())
    seen: list[dict[str, Any]] = []

    async def capture(ctx: Any) -> None:
        seen.append(dict(ctx.data))

    with patch("promptise.agent.build_agent", new_callable=lambda: AsyncMock(return_value=agent)):
        await process.start()
        try:
            collector._hook("watcher", process)
            process.hooks.register(HookEvent.TASK_CREATED, capture)
            await process.inject(
                TriggerEvent(trigger_id="t", trigger_type="manual", payload={"order": 42})
            )
            deadline = time.monotonic() + 5
            while not state.events and time.monotonic() < deadline:
                await asyncio.sleep(0.01)
        finally:
            collector.stop()
            await process.stop()

    assert seen[0]["payload_summary"] == '{"order": 42}'
    assert state.events[-1].payload_summary == '{"order": 42}'
    text = _text(RuntimeDashboard(state)._tab_events(), width=140)
    assert '{"order": 42}' in text


def test_payload_summary_is_one_short_line() -> None:
    process = AgentProcess("p", ProcessConfig())
    assert process._payload_summary({}) == ""
    long = process._payload_summary({"text": "line one\nline two " + "x" * 300})
    assert "\n" not in long and len(long) == 120 and long.endswith("...")
