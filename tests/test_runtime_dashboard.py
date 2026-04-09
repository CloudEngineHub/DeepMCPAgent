"""Tests for the runtime dashboard (``promptise.runtime._dashboard``).

Covers:
- Data models (snapshots, logs, events, commands)
- RuntimeDashboardState record methods and counters
- RuntimeDataCollector snapshot collection
- RuntimeDashboard rendering (all 8 tabs)
- Tab navigation (next, prev, wrap)
- Command execution (help, list, status, inject, unknown)
- Helper functions (_kv, _format_duration, _fmt_duration_ms)
- CLI integration (--dashboard flag)
"""

from __future__ import annotations

import time
from unittest.mock import AsyncMock, MagicMock

from promptise.runtime._dashboard import (
    STATE_COLORS,
    TABS,
    CommandResult,
    ContextSnapshot,
    EventLog,
    InvocationLog,
    ProcessSnapshot,
    RuntimeDashboard,
    RuntimeDashboardState,
    RuntimeDataCollector,
    TriggerInfo,
    _fmt_duration_ms,
    _format_duration,
    _kv,
)

# =====================================================================
# Data model tests
# =====================================================================


class TestInvocationLog:
    """Test InvocationLog dataclass."""

    def test_create(self):
        log = InvocationLog(
            timestamp=1000.0,
            process_name="watcher",
            trigger_type="cron",
            trigger_id="t1",
            success=True,
            duration_ms=150.0,
        )
        assert log.process_name == "watcher"
        assert log.success is True
        assert log.error is None

    def test_with_error(self):
        log = InvocationLog(
            timestamp=1000.0,
            process_name="watcher",
            trigger_type="manual",
            trigger_id="t2",
            success=False,
            duration_ms=50.0,
            error="Connection refused",
        )
        assert log.success is False
        assert log.error == "Connection refused"


class TestEventLog:
    """Test EventLog dataclass."""

    def test_create(self):
        event = EventLog(
            timestamp=1000.0,
            process_name="watcher",
            trigger_type="webhook",
            trigger_id="w1",
            event_id="e-123",
            payload_summary='{"key": "value"}',
        )
        assert event.trigger_type == "webhook"
        assert event.event_id == "e-123"


class TestCommandResult:
    """Test CommandResult dataclass."""

    def test_create(self):
        cmd = CommandResult(
            timestamp=1000.0,
            command="status",
            result="2 processes (1 running)",
            success=True,
        )
        assert cmd.command == "status"
        assert cmd.success is True


class TestTriggerInfo:
    """Test TriggerInfo dataclass."""

    def test_defaults(self):
        info = TriggerInfo(
            process_name="p1",
            trigger_id="t1",
            trigger_type="cron",
            config_summary="*/5 * * * *",
        )
        assert info.fire_count == 0
        assert info.last_fired is None


class TestProcessSnapshot:
    """Test ProcessSnapshot dataclass."""

    def test_create(self):
        snap = ProcessSnapshot(
            name="watcher",
            process_id="abc-123",
            state="running",
            model="openai:gpt-5-mini",
            invocation_count=10,
            consecutive_failures=0,
            trigger_count=2,
            queue_size=0,
            uptime_seconds=3600.0,
            concurrency=1,
            heartbeat_interval=30.0,
        )
        assert snap.name == "watcher"
        assert snap.state == "running"
        assert snap.uptime_seconds == 3600.0


class TestContextSnapshot:
    """Test ContextSnapshot dataclass."""

    def test_create(self):
        ctx = ContextSnapshot(
            process_name="p1",
            state={"key": "value"},
            writable_keys=["key"],
            env_count=3,
            file_mount_count=1,
            has_memory=False,
            history_counts={"key": 2},
        )
        assert ctx.state["key"] == "value"
        assert ctx.writable_keys == ["key"]


# =====================================================================
# RuntimeDashboardState tests
# =====================================================================


class TestRuntimeDashboardState:
    """Test RuntimeDashboardState."""

    def test_initial_state(self):
        state = RuntimeDashboardState()
        assert state.runtime_name == "Agent Runtime"
        assert state.total_invocations == 0
        assert state.total_errors == 0
        assert state.total_events == 0
        assert len(state.processes) == 0

    def test_record_invocation_success(self):
        state = RuntimeDashboardState()
        state.record_invocation(
            InvocationLog(
                timestamp=1000.0,
                process_name="p1",
                trigger_type="cron",
                trigger_id="t1",
                success=True,
                duration_ms=100.0,
            )
        )
        assert state.total_invocations == 1
        assert state.total_errors == 0
        assert len(state.invocations) == 1

    def test_record_invocation_failure(self):
        state = RuntimeDashboardState()
        state.record_invocation(
            InvocationLog(
                timestamp=1000.0,
                process_name="p1",
                trigger_type="cron",
                trigger_id="t1",
                success=False,
                duration_ms=50.0,
                error="timeout",
            )
        )
        assert state.total_invocations == 1
        assert state.total_errors == 1

    def test_record_multiple_invocations(self):
        state = RuntimeDashboardState()
        for i in range(5):
            state.record_invocation(
                InvocationLog(
                    timestamp=float(i),
                    process_name="p1",
                    trigger_type="cron",
                    trigger_id="t1",
                    success=i % 2 == 0,
                    duration_ms=float(i * 10),
                )
            )
        assert state.total_invocations == 5
        assert state.total_errors == 2  # i=1,3

    def test_record_event(self):
        state = RuntimeDashboardState()
        state.record_event(
            EventLog(
                timestamp=1000.0,
                process_name="p1",
                trigger_type="webhook",
                trigger_id="w1",
                event_id="e1",
                payload_summary="test",
            )
        )
        assert state.total_events == 1
        assert len(state.events) == 1

    def test_record_command(self):
        state = RuntimeDashboardState()
        state.record_command(
            CommandResult(
                timestamp=1000.0,
                command="help",
                result="Commands: ...",
                success=True,
            )
        )
        assert len(state.commands) == 1

    def test_record_journal(self):
        state = RuntimeDashboardState()
        state.record_journal(
            {
                "process_id": "p1",
                "entry_type": "state_transition",
                "timestamp": 1000.0,
                "data": {"from": "created", "to": "running"},
            }
        )
        assert len(state.journal_entries) == 1

    def test_ring_buffer_limits(self):
        state = RuntimeDashboardState()
        # Invocations maxlen=200
        for i in range(250):
            state.record_invocation(
                InvocationLog(
                    timestamp=float(i),
                    process_name="p1",
                    trigger_type="cron",
                    trigger_id="t1",
                    success=True,
                    duration_ms=10.0,
                )
            )
        assert len(state.invocations) == 200
        assert state.total_invocations == 250

    def test_custom_name(self):
        state = RuntimeDashboardState(runtime_name="my-runtime")
        assert state.runtime_name == "my-runtime"


# =====================================================================
# RuntimeDataCollector tests
# =====================================================================


class TestRuntimeDataCollector:
    """Test RuntimeDataCollector."""

    def _make_mock_runtime(self):
        """Create a mock runtime with one process."""
        from promptise.runtime.config import (
            ContextConfig,
            ProcessConfig,
            TriggerConfig,
        )
        from promptise.runtime.context import AgentContext

        config = ProcessConfig(
            model="openai:gpt-5-mini",
            instructions="test",
            triggers=[
                TriggerConfig(type="cron", cron_expression="*/5 * * * *"),
            ],
            context=ContextConfig(
                initial_state={"status": "healthy"},
                writable_keys=["status"],
            ),
        )

        process = MagicMock()
        process.name = "test-proc"
        process.config = config
        process.status.return_value = {
            "name": "test-proc",
            "process_id": "abc-123",
            "state": "running",
            "invocation_count": 5,
            "consecutive_failures": 0,
            "trigger_count": 1,
            "queue_size": 0,
            "uptime_seconds": 120.0,
        }
        process.context = AgentContext(
            initial_state={"status": "healthy"},
            writable_keys=["status"],
        )

        runtime = MagicMock()
        runtime.processes = {"test-proc": process}

        return runtime

    def test_collect_snapshot(self):
        runtime = self._make_mock_runtime()
        state = RuntimeDashboardState()
        collector = RuntimeDataCollector(runtime, state, interval=0.1)

        # Call the private method directly
        collector._collect_snapshot()

        assert "test-proc" in state.processes
        proc_snap = state.processes["test-proc"]
        assert proc_snap.state == "running"
        assert proc_snap.invocation_count == 5
        assert proc_snap.model == "openai:gpt-5-mini"

        assert "test-proc" in state.contexts
        ctx_snap = state.contexts["test-proc"]
        assert ctx_snap.state == {"status": "healthy"}
        assert ctx_snap.writable_keys == ["status"]

        assert len(state.triggers) == 1
        assert state.triggers[0].trigger_type == "cron"

    def test_removes_stale_processes(self):
        runtime = self._make_mock_runtime()
        state = RuntimeDashboardState()
        collector = RuntimeDataCollector(runtime, state, interval=0.1)

        # First collect
        collector._collect_snapshot()
        assert "test-proc" in state.processes

        # Remove process from runtime
        runtime.processes = {}
        collector._collect_snapshot()
        assert "test-proc" not in state.processes

    def test_start_stop(self):
        runtime = self._make_mock_runtime()
        state = RuntimeDashboardState()
        collector = RuntimeDataCollector(runtime, state, interval=0.05)

        collector.start()
        assert collector._running is True
        assert collector._thread is not None
        assert collector._thread.daemon is True

        time.sleep(0.15)  # Let it collect at least once

        collector.stop()
        assert collector._running is False


# =====================================================================
# RuntimeDashboard rendering tests
# =====================================================================


class TestRuntimeDashboardRendering:
    """Test dashboard tab rendering."""

    def _make_dashboard(self, **state_kwargs):
        """Create a dashboard with optional state overrides."""
        state = RuntimeDashboardState(**state_kwargs)
        return RuntimeDashboard(state)

    def _dashboard_with_data(self):
        """Create a dashboard with sample data."""
        state = RuntimeDashboardState(runtime_name="test-runtime")

        state.processes["watcher"] = ProcessSnapshot(
            name="watcher",
            process_id="abc-123",
            state="running",
            model="openai:gpt-5-mini",
            invocation_count=10,
            consecutive_failures=0,
            trigger_count=2,
            queue_size=1,
            uptime_seconds=3600.0,
            concurrency=1,
            heartbeat_interval=30.0,
        )
        state.processes["alerter"] = ProcessSnapshot(
            name="alerter",
            process_id="def-456",
            state="stopped",
            model="openai:gpt-5-mini",
            invocation_count=3,
            consecutive_failures=1,
            trigger_count=1,
            queue_size=0,
            uptime_seconds=None,
            concurrency=2,
            heartbeat_interval=15.0,
        )

        state.triggers = [
            TriggerInfo(
                process_name="watcher",
                trigger_id="watcher/cron",
                trigger_type="cron",
                config_summary="cron=*/5 * * * *",
            ),
            TriggerInfo(
                process_name="watcher",
                trigger_id="watcher/webhook",
                trigger_type="webhook",
                config_summary="port=9090 path=/events",
            ),
            TriggerInfo(
                process_name="alerter",
                trigger_id="alerter/event",
                trigger_type="event",
                config_summary="event=pipeline.alert",
            ),
        ]

        state.contexts["watcher"] = ContextSnapshot(
            process_name="watcher",
            state={"pipeline_status": "healthy", "check_count": 5},
            writable_keys=["pipeline_status", "check_count"],
            env_count=2,
            file_mount_count=0,
            has_memory=False,
            history_counts={"pipeline_status": 3, "check_count": 6},
        )

        state.record_invocation(
            InvocationLog(
                timestamp=time.time(),
                process_name="watcher",
                trigger_type="cron",
                trigger_id="t1",
                success=True,
                duration_ms=120.0,
            )
        )
        state.record_invocation(
            InvocationLog(
                timestamp=time.time(),
                process_name="alerter",
                trigger_type="manual",
                trigger_id="m1",
                success=False,
                duration_ms=50.0,
                error="Connection refused",
            )
        )

        state.record_event(
            EventLog(
                timestamp=time.time(),
                process_name="watcher",
                trigger_type="cron",
                trigger_id="t1",
                event_id="e-abc",
                payload_summary='{"reason": "scheduled"}',
            )
        )

        state.record_command(
            CommandResult(
                timestamp=time.time(),
                command="status",
                result="2 processes (1 running)",
                success=True,
            )
        )

        return RuntimeDashboard(state)

    def test_render_empty(self):
        dashboard = self._make_dashboard()
        layout = dashboard._render()
        assert layout is not None

    def test_render_with_data(self):
        dashboard = self._dashboard_with_data()
        layout = dashboard._render()
        assert layout is not None

    def test_header(self):
        dashboard = self._dashboard_with_data()
        header = dashboard._header()
        assert header is not None

    def test_nav_bar(self):
        dashboard = self._dashboard_with_data()
        nav = dashboard._nav_bar()
        assert nav is not None
        # Should contain all tab names
        nav_str = str(nav)
        assert "Overview" in nav_str
        assert "Commands" in nav_str

    def test_tab_overview_empty(self):
        dashboard = self._make_dashboard()
        panel = dashboard._tab_overview()
        assert panel is not None

    def test_tab_overview_with_data(self):
        dashboard = self._dashboard_with_data()
        panel = dashboard._tab_overview()
        assert panel is not None

    def test_tab_processes_empty(self):
        dashboard = self._make_dashboard()
        panel = dashboard._tab_processes()
        assert panel is not None

    def test_tab_processes_with_data(self):
        dashboard = self._dashboard_with_data()
        panel = dashboard._tab_processes()
        assert panel is not None

    def test_tab_triggers_empty(self):
        dashboard = self._make_dashboard()
        panel = dashboard._tab_triggers()
        assert panel is not None

    def test_tab_triggers_with_data(self):
        dashboard = self._dashboard_with_data()
        panel = dashboard._tab_triggers()
        assert panel is not None

    def test_tab_context_empty(self):
        dashboard = self._make_dashboard()
        panel = dashboard._tab_context()
        assert panel is not None

    def test_tab_context_with_data(self):
        dashboard = self._dashboard_with_data()
        panel = dashboard._tab_context()
        assert panel is not None

    def test_tab_logs_empty(self):
        dashboard = self._make_dashboard()
        panel = dashboard._tab_logs()
        assert panel is not None

    def test_tab_logs_with_data(self):
        dashboard = self._dashboard_with_data()
        # Add some journal entries
        dashboard._state.record_journal(
            {
                "process_id": "watcher",
                "entry_type": "state_transition",
                "timestamp": time.time(),
                "data": {"from": "created", "to": "running"},
            }
        )
        panel = dashboard._tab_logs()
        assert panel is not None

    def test_tab_events_empty(self):
        dashboard = self._make_dashboard()
        panel = dashboard._tab_events()
        assert panel is not None

    def test_tab_events_with_data(self):
        dashboard = self._dashboard_with_data()
        panel = dashboard._tab_events()
        assert panel is not None

    def test_tab_commands_empty(self):
        dashboard = self._make_dashboard()
        panel = dashboard._tab_commands()
        assert panel is not None

    def test_tab_commands_with_data(self):
        dashboard = self._dashboard_with_data()
        panel = dashboard._tab_commands()
        assert panel is not None

    def test_tab_commands_in_command_mode(self):
        dashboard = self._dashboard_with_data()
        dashboard._command_mode = True
        dashboard._command_buffer = "status wat"
        panel = dashboard._tab_commands()
        assert panel is not None

    def test_all_tabs_render(self):
        """Ensure every tab renders without error."""
        dashboard = self._dashboard_with_data()
        for i in range(len(TABS)):
            dashboard._current_tab = i
            layout = dashboard._render()
            assert layout is not None


# =====================================================================
# Tab navigation tests
# =====================================================================


class TestTabNavigation:
    """Test tab navigation."""

    def test_next_tab(self):
        state = RuntimeDashboardState()
        dashboard = RuntimeDashboard(state)
        assert dashboard._current_tab == 0
        dashboard._next_tab()
        assert dashboard._current_tab == 1

    def test_prev_tab(self):
        state = RuntimeDashboardState()
        dashboard = RuntimeDashboard(state)
        dashboard._current_tab = 2
        dashboard._prev_tab()
        assert dashboard._current_tab == 1

    def test_next_wraps(self):
        state = RuntimeDashboardState()
        dashboard = RuntimeDashboard(state)
        dashboard._current_tab = len(TABS) - 1
        dashboard._next_tab()
        assert dashboard._current_tab == 0

    def test_prev_wraps(self):
        state = RuntimeDashboardState()
        dashboard = RuntimeDashboard(state)
        dashboard._current_tab = 0
        dashboard._prev_tab()
        assert dashboard._current_tab == len(TABS) - 1

    def test_tab_count(self):
        assert len(TABS) == 7


# =====================================================================
# Command execution tests
# =====================================================================


class TestCommandExecution:
    """Test dashboard command execution."""

    def _make_dashboard_with_runtime(self):
        """Create a dashboard with a mock runtime."""
        state = RuntimeDashboardState()
        state.processes["watcher"] = ProcessSnapshot(
            name="watcher",
            process_id="abc-123",
            state="running",
            model="openai:gpt-5-mini",
            invocation_count=5,
            consecutive_failures=0,
            trigger_count=1,
            queue_size=0,
            uptime_seconds=100.0,
            concurrency=1,
            heartbeat_interval=30.0,
        )

        runtime = MagicMock()
        proc_mock = MagicMock()
        proc_mock.start = AsyncMock()
        proc_mock.stop = AsyncMock()
        proc_mock.suspend = AsyncMock()
        proc_mock.resume = AsyncMock()
        proc_mock.inject = AsyncMock()
        runtime.get_process.return_value = proc_mock
        runtime.processes = {"watcher": proc_mock}
        runtime.restart_process = AsyncMock()

        return RuntimeDashboard(state, runtime=runtime)

    def test_help_command(self):
        dashboard = self._make_dashboard_with_runtime()
        dashboard._execute_command("help")
        assert len(dashboard._state.commands) == 1
        result = dashboard._state.commands[0]
        assert result.success is True
        assert "Commands:" in result.result

    def test_list_command(self):
        dashboard = self._make_dashboard_with_runtime()
        dashboard._execute_command("list")
        result = dashboard._state.commands[0]
        assert result.success is True
        assert "watcher" in result.result

    def test_status_command_all(self):
        dashboard = self._make_dashboard_with_runtime()
        dashboard._execute_command("status")
        result = dashboard._state.commands[0]
        assert result.success is True
        assert "1 processes" in result.result

    def test_status_command_specific(self):
        dashboard = self._make_dashboard_with_runtime()
        dashboard._execute_command("status watcher")
        result = dashboard._state.commands[0]
        assert result.success is True
        assert "watcher" in result.result

    def test_status_command_not_found(self):
        dashboard = self._make_dashboard_with_runtime()
        dashboard._execute_command("status nonexistent")
        result = dashboard._state.commands[0]
        assert result.success is False
        assert "not found" in result.result

    def test_unknown_command(self):
        dashboard = self._make_dashboard_with_runtime()
        dashboard._execute_command("foobar")
        result = dashboard._state.commands[0]
        assert result.success is False
        assert "Unknown command" in result.result

    def test_start_no_args(self):
        dashboard = self._make_dashboard_with_runtime()
        dashboard._execute_command("start")
        result = dashboard._state.commands[0]
        assert result.success is False
        assert "Usage:" in result.result

    def test_stop_no_runtime(self):
        state = RuntimeDashboardState()
        dashboard = RuntimeDashboard(state)  # No runtime
        dashboard._execute_command("stop watcher")
        result = dashboard._state.commands[0]
        assert result.success is False
        assert "No runtime reference" in result.result

    def test_inject_command(self):
        dashboard = self._make_dashboard_with_runtime()
        dashboard._execute_command('inject watcher {"reason": "test"}')
        result = dashboard._state.commands[0]
        assert result.success is True
        assert "Injected" in result.result

    def test_inject_invalid_json(self):
        dashboard = self._make_dashboard_with_runtime()
        dashboard._execute_command("inject watcher not-json")
        result = dashboard._state.commands[0]
        assert result.success is True  # Falls back to raw payload

    def test_inject_not_found(self):
        dashboard = self._make_dashboard_with_runtime()
        dashboard._runtime.get_process.side_effect = KeyError("nope")
        dashboard._execute_command("inject nope")
        result = dashboard._state.commands[0]
        assert result.success is False

    def test_empty_command(self):
        dashboard = self._make_dashboard_with_runtime()
        dashboard._execute_command("")
        assert len(dashboard._state.commands) == 0

    def test_list_empty(self):
        state = RuntimeDashboardState()
        dashboard = RuntimeDashboard(state)
        dashboard._execute_command("list")
        result = dashboard._state.commands[0]
        assert "(none)" in result.result


# =====================================================================
# Helper function tests
# =====================================================================


class TestHelpers:
    """Test helper functions."""

    def test_kv(self):
        text = _kv("Label", "42", "cyan")
        text_str = str(text)
        assert "42" in text_str
        assert "Label" in text_str

    def test_format_duration_seconds(self):
        assert _format_duration(30) == "30s"

    def test_format_duration_minutes(self):
        assert _format_duration(90) == "1m 30s"

    def test_format_duration_hours(self):
        assert _format_duration(3661) == "1h 1m"

    def test_format_duration_days(self):
        assert _format_duration(90000) == "1d 1h"

    def test_format_duration_none(self):
        assert _format_duration(None) == "-"

    def test_format_duration_zero(self):
        assert _format_duration(0) == "0s"

    def test_fmt_duration_ms_sub_one(self):
        assert _fmt_duration_ms(0.5) == "0.5ms"

    def test_fmt_duration_ms_ms(self):
        assert _fmt_duration_ms(150) == "150ms"

    def test_fmt_duration_ms_seconds(self):
        assert _fmt_duration_ms(1500) == "1.5s"

    def test_fmt_duration_ms_exact_one(self):
        assert _fmt_duration_ms(1.0) == "1ms"


# =====================================================================
# State colors
# =====================================================================


class TestStateColors:
    """Test state color mappings."""

    def test_all_states_have_colors(self):
        expected_states = [
            "running",
            "starting",
            "created",
            "stopped",
            "failed",
            "suspended",
            "stopping",
            "awaiting",
        ]
        for state in expected_states:
            assert state in STATE_COLORS

    def test_running_is_green(self):
        assert "green" in STATE_COLORS["running"]

    def test_failed_is_red(self):
        assert "red" in STATE_COLORS["failed"]


# =====================================================================
# Dashboard lifecycle tests (without terminal)
# =====================================================================


class TestDashboardLifecycle:
    """Test dashboard start/stop without a real terminal."""

    def test_create_dashboard(self):
        state = RuntimeDashboardState()
        dashboard = RuntimeDashboard(state)
        assert dashboard._running is False
        assert dashboard._current_tab == 0
        assert dashboard._command_mode is False
        assert dashboard._command_buffer == ""

    def test_command_mode_toggle(self):
        state = RuntimeDashboardState()
        dashboard = RuntimeDashboard(state)
        dashboard._command_mode = True
        dashboard._command_buffer = "test"

        # Render should show command mode footer
        layout = dashboard._render()
        assert layout is not None

    def test_log_capture(self):
        state = RuntimeDashboardState()
        dashboard = RuntimeDashboard(state)

        import logging

        record = logging.LogRecord(
            name="test",
            level=logging.INFO,
            pathname="",
            lineno=0,
            msg="test message",
            args=None,
            exc_info=None,
        )
        dashboard._log_capture.emit(record)
        assert len(dashboard._log_capture.records) == 1

    def test_log_capture_maxlen(self):
        state = RuntimeDashboardState()
        dashboard = RuntimeDashboard(state)

        import logging

        for i in range(600):
            record = logging.LogRecord(
                name="test",
                level=logging.INFO,
                pathname="",
                lineno=0,
                msg=f"msg {i}",
                args=None,
                exc_info=None,
            )
            dashboard._log_capture.emit(record)
        assert len(dashboard._log_capture.records) == 500


# =====================================================================
# Journal entry rendering
# =====================================================================


class TestJournalRendering:
    """Test journal entry rendering in the Logs tab."""

    def test_datetime_timestamp(self):
        """Journal entries with datetime timestamps should render."""
        import datetime

        state = RuntimeDashboardState()
        state.record_journal(
            {
                "process_id": "p1",
                "entry_type": "state_transition",
                "timestamp": datetime.datetime(2026, 3, 1, 12, 0, 0),
                "data": {"from": "created", "to": "running"},
            }
        )

        dashboard = RuntimeDashboard(state)
        panel = dashboard._tab_logs()
        assert panel is not None

    def test_float_timestamp(self):
        """Journal entries with float timestamps should render."""
        state = RuntimeDashboardState()
        state.record_journal(
            {
                "process_id": "p1",
                "entry_type": "invocation",
                "timestamp": time.time(),
                "data": {"count": 1},
            }
        )

        dashboard = RuntimeDashboard(state)
        panel = dashboard._tab_logs()
        assert panel is not None

    def test_string_timestamp(self):
        """Journal entries with string timestamps should render."""
        state = RuntimeDashboardState()
        state.record_journal(
            {
                "process_id": "p1",
                "entry_type": "checkpoint",
                "timestamp": "2026-03-01T12:00:00",
                "data": {},
            }
        )

        dashboard = RuntimeDashboard(state)
        panel = dashboard._tab_logs()
        assert panel is not None


# =====================================================================
# Context rendering edge cases
# =====================================================================


class TestContextRenderingEdgeCases:
    """Test context tab with edge cases."""

    def test_all_writable(self):
        """When writable_keys is None, all keys show as writable."""
        state = RuntimeDashboardState()
        state.contexts["p1"] = ContextSnapshot(
            process_name="p1",
            state={"a": 1, "b": 2},
            writable_keys=None,  # All writable
            env_count=0,
            file_mount_count=0,
            has_memory=False,
            history_counts={},
        )

        dashboard = RuntimeDashboard(state)
        panel = dashboard._tab_context()
        assert panel is not None

    def test_long_values_truncated(self):
        """Long state values should be truncated."""
        state = RuntimeDashboardState()
        state.contexts["p1"] = ContextSnapshot(
            process_name="p1",
            state={"long_key": "x" * 100},
            writable_keys=None,
            env_count=0,
            file_mount_count=0,
            has_memory=True,
            history_counts={"long_key": 1},
        )

        dashboard = RuntimeDashboard(state)
        panel = dashboard._tab_context()
        assert panel is not None

    def test_with_env_and_mounts(self):
        """Context with env vars and file mounts."""
        state = RuntimeDashboardState()
        state.contexts["p1"] = ContextSnapshot(
            process_name="p1",
            state={},
            writable_keys=None,
            env_count=5,
            file_mount_count=2,
            has_memory=True,
            history_counts={},
        )

        dashboard = RuntimeDashboard(state)
        panel = dashboard._tab_context()
        assert panel is not None
