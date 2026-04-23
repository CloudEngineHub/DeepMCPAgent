"""Phase 4 tests: AgentRuntime, CLI, WebhookTrigger, FileWatchTrigger.

Tests for:
- AgentRuntime (process management, manifest loading, lifecycle)
- CLI commands (init, validate via Typer CliRunner)
- WebhookTrigger (HTTP endpoint → TriggerEvent)
- FileWatchTrigger (filesystem changes → TriggerEvent)
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from typer.testing import CliRunner

from promptise.runtime.config import (
    ProcessConfig,
    RuntimeConfig,
    TriggerConfig,
)
from promptise.runtime.exceptions import (
    ManifestError,
    RuntimeBaseError,
)
from promptise.runtime.lifecycle import ProcessState
from promptise.runtime.runtime import AgentRuntime

# ======================================================================
# Helpers
# ======================================================================


def _make_process_config(**overrides) -> ProcessConfig:
    """Create a minimal ProcessConfig."""
    defaults = {
        "model": "openai:gpt-5-mini",
        "instructions": "test agent",
    }
    defaults.update(overrides)
    return ProcessConfig(**defaults)


def _write_agent_manifest(tmp_path: Path, name: str = "test-agent", **extra) -> Path:
    """Write a valid .agent manifest file."""
    import yaml

    data = {
        "version": "1.0",
        "name": name,
        "model": "openai:gpt-5-mini",
        "instructions": "Test agent instructions.",
        **extra,
    }
    path = tmp_path / f"{name}.agent"
    path.write_text(yaml.dump(data, default_flow_style=False))
    return path


# ======================================================================
# AgentRuntime
# ======================================================================


class TestAgentRuntimeProcessManagement:
    """Test process registration and removal."""

    @pytest.mark.asyncio
    async def test_add_process(self):
        runtime = AgentRuntime()
        config = _make_process_config()
        process = await runtime.add_process("test", config)
        assert process.name == "test"
        assert "test" in runtime.processes

    @pytest.mark.asyncio
    async def test_add_duplicate_raises(self):
        runtime = AgentRuntime()
        config = _make_process_config()
        await runtime.add_process("test", config)
        with pytest.raises(RuntimeBaseError, match="already exists"):
            await runtime.add_process("test", config)

    @pytest.mark.asyncio
    async def test_remove_process(self):
        runtime = AgentRuntime()
        config = _make_process_config()
        await runtime.add_process("test", config)
        await runtime.remove_process("test")
        assert "test" not in runtime.processes

    @pytest.mark.asyncio
    async def test_remove_nonexistent_raises(self):
        runtime = AgentRuntime()
        with pytest.raises(KeyError):
            await runtime.remove_process("nope")

    @pytest.mark.asyncio
    async def test_get_process(self):
        runtime = AgentRuntime()
        config = _make_process_config()
        await runtime.add_process("test", config)
        process = runtime.get_process("test")
        assert process.name == "test"

    @pytest.mark.asyncio
    async def test_get_nonexistent_raises(self):
        runtime = AgentRuntime()
        with pytest.raises(KeyError):
            runtime.get_process("nope")

    @pytest.mark.asyncio
    async def test_processes_property(self):
        runtime = AgentRuntime()
        config = _make_process_config()
        await runtime.add_process("a", config)
        await runtime.add_process("b", config)
        procs = runtime.processes
        assert len(procs) == 2
        assert "a" in procs
        assert "b" in procs


class TestAgentRuntimeManifestLoading:
    """Test manifest loading into processes."""

    @pytest.mark.asyncio
    async def test_load_manifest(self, tmp_path):
        path = _write_agent_manifest(tmp_path, name="from-manifest")
        runtime = AgentRuntime()
        process = await runtime.load_manifest(path)
        assert process.name == "from-manifest"
        assert "from-manifest" in runtime.processes

    @pytest.mark.asyncio
    async def test_load_manifest_name_override(self, tmp_path):
        path = _write_agent_manifest(tmp_path, name="original")
        runtime = AgentRuntime()
        process = await runtime.load_manifest(path, name_override="custom")
        assert process.name == "custom"
        assert "custom" in runtime.processes

    @pytest.mark.asyncio
    async def test_load_directory(self, tmp_path):
        _write_agent_manifest(tmp_path, name="agent-a")
        _write_agent_manifest(tmp_path, name="agent-b")
        runtime = AgentRuntime()
        loaded = await runtime.load_directory(tmp_path)
        assert len(loaded) == 2
        assert "agent-a" in loaded
        assert "agent-b" in loaded

    @pytest.mark.asyncio
    async def test_load_directory_nonexistent_raises(self, tmp_path):
        runtime = AgentRuntime()
        with pytest.raises(ManifestError, match="Not a directory"):
            await runtime.load_directory(tmp_path / "nonexistent")


class TestAgentRuntimeLifecycle:
    """Test start/stop/restart operations."""

    @pytest.mark.asyncio
    async def test_start_stop_process(self):
        """Test start/stop via runtime (mocked agent build)."""
        runtime = AgentRuntime()
        config = _make_process_config()
        await runtime.add_process("test", config)

        # Mock the agent build
        process = runtime.get_process("test")

        mock_agent = AsyncMock()
        mock_agent.ainvoke = AsyncMock(return_value={"messages": []})
        mock_agent.shutdown = AsyncMock()

        with patch("promptise.agent.build_agent") as mock_build:
            mock_build.return_value = mock_agent
            await runtime.start_process("test")
            assert process.state == ProcessState.RUNNING

            await runtime.stop_process("test")
            assert process.state == ProcessState.STOPPED

    @pytest.mark.asyncio
    async def test_start_all_stop_all(self):
        runtime = AgentRuntime()
        config = _make_process_config()
        await runtime.add_process("a", config)
        await runtime.add_process("b", config)

        mock_agent = AsyncMock()
        mock_agent.ainvoke = AsyncMock(return_value={"messages": []})
        mock_agent.shutdown = AsyncMock()

        with patch("promptise.agent.build_agent") as mock_build:
            mock_build.return_value = mock_agent
            await runtime.start_all()

            assert runtime.get_process("a").state == ProcessState.RUNNING
            assert runtime.get_process("b").state == ProcessState.RUNNING

            await runtime.stop_all()

            assert runtime.get_process("a").state == ProcessState.STOPPED
            assert runtime.get_process("b").state == ProcessState.STOPPED

    @pytest.mark.asyncio
    async def test_restart_process(self):
        runtime = AgentRuntime()
        config = _make_process_config()
        await runtime.add_process("test", config)

        mock_agent = AsyncMock()
        mock_agent.ainvoke = AsyncMock(return_value={"messages": []})
        mock_agent.shutdown = AsyncMock()

        with patch("promptise.agent.build_agent") as mock_build:
            mock_build.return_value = mock_agent
            await runtime.start_process("test")
            assert runtime.get_process("test").state == ProcessState.RUNNING

            await runtime.restart_process("test")
            assert runtime.get_process("test").state == ProcessState.RUNNING


class TestAgentRuntimeStatus:
    """Test status and monitoring."""

    @pytest.mark.asyncio
    async def test_status(self):
        runtime = AgentRuntime()
        config = _make_process_config()
        await runtime.add_process("test", config)

        status = runtime.status()
        assert status["process_count"] == 1
        assert "test" in status["processes"]

    @pytest.mark.asyncio
    async def test_process_status(self):
        runtime = AgentRuntime()
        config = _make_process_config()
        await runtime.add_process("test", config)

        status = runtime.process_status("test")
        assert status["name"] == "test"
        assert status["state"] == "created"

    @pytest.mark.asyncio
    async def test_list_processes(self):
        runtime = AgentRuntime()
        config = _make_process_config()
        await runtime.add_process("a", config)
        await runtime.add_process("b", config)

        processes = runtime.list_processes()
        assert len(processes) == 2
        names = {p["name"] for p in processes}
        assert names == {"a", "b"}


class TestAgentRuntimeContextManager:
    """Test context manager behavior."""

    @pytest.mark.asyncio
    async def test_context_manager(self):
        async with AgentRuntime() as runtime:
            config = _make_process_config()
            await runtime.add_process("test", config)
            assert "test" in runtime.processes

    @pytest.mark.asyncio
    async def test_from_config(self):
        config = RuntimeConfig(
            processes={
                "a": ProcessConfig(model="openai:gpt-5-mini"),
                "b": ProcessConfig(model="openai:gpt-5-mini"),
            }
        )
        runtime = await AgentRuntime.from_config(config)
        assert len(runtime.processes) == 2

    @pytest.mark.asyncio
    async def test_repr(self):
        runtime = AgentRuntime()
        config = _make_process_config()
        await runtime.add_process("test", config)
        r = repr(runtime)
        assert "AgentRuntime" in r
        assert "processes=1" in r


# ======================================================================
# FileWatchTrigger (polling fallback)
# ======================================================================


class TestFileWatchTrigger:
    """Test filesystem watch trigger with polling fallback."""

    @pytest.mark.asyncio
    async def test_file_creation_detected(self, tmp_path):
        """Creating a file should produce a TriggerEvent."""
        from promptise.runtime.triggers.file_watch import FileWatchTrigger

        trigger = FileWatchTrigger(
            watch_path=str(tmp_path),
            patterns=["*.txt"],
            events=["created"],
            poll_interval=0.1,
            debounce_seconds=0.0,
        )

        # Force polling mode even if watchdog is installed
        import promptise.runtime.triggers.file_watch as fw_module

        original_has_watchdog = fw_module.HAS_WATCHDOG
        fw_module.HAS_WATCHDOG = False

        try:
            await trigger.start()

            # Create a file after a slight delay
            await asyncio.sleep(0.15)
            test_file = tmp_path / "new_file.txt"
            test_file.write_text("hello")

            # Wait for the poller to detect it
            event = await asyncio.wait_for(trigger.wait_for_next(), timeout=3.0)

            assert event.trigger_type == "file_watch"
            assert event.payload["event_type"] == "created"
            assert "new_file.txt" in event.payload["filename"]

            await trigger.stop()
        finally:
            fw_module.HAS_WATCHDOG = original_has_watchdog

    @pytest.mark.asyncio
    async def test_pattern_filtering(self, tmp_path):
        """Only matching patterns should trigger events."""
        from promptise.runtime.triggers.file_watch import FileWatchTrigger

        trigger = FileWatchTrigger(
            watch_path=str(tmp_path),
            patterns=["*.csv"],
            events=["created"],
            poll_interval=0.1,
            debounce_seconds=0.0,
        )

        import promptise.runtime.triggers.file_watch as fw_module

        original_has_watchdog = fw_module.HAS_WATCHDOG
        fw_module.HAS_WATCHDOG = False

        try:
            await trigger.start()
            await asyncio.sleep(0.15)

            # Create a non-matching file
            (tmp_path / "ignore.txt").write_text("hello")
            await asyncio.sleep(0.15)

            # Create a matching file
            (tmp_path / "data.csv").write_text("a,b,c")

            event = await asyncio.wait_for(trigger.wait_for_next(), timeout=3.0)
            assert "data.csv" in event.payload["filename"]

            await trigger.stop()
        finally:
            fw_module.HAS_WATCHDOG = original_has_watchdog

    @pytest.mark.asyncio
    async def test_file_modification_detected(self, tmp_path):
        """Modifying a file should produce a TriggerEvent."""
        from promptise.runtime.triggers.file_watch import FileWatchTrigger

        # Create file before watching
        test_file = tmp_path / "existing.txt"
        test_file.write_text("original")

        trigger = FileWatchTrigger(
            watch_path=str(tmp_path),
            patterns=["*.txt"],
            events=["modified"],
            poll_interval=0.1,
            debounce_seconds=0.0,
        )

        import promptise.runtime.triggers.file_watch as fw_module

        original_has_watchdog = fw_module.HAS_WATCHDOG
        fw_module.HAS_WATCHDOG = False

        try:
            await trigger.start()
            await asyncio.sleep(0.15)

            # Modify the file
            test_file.write_text("modified content")

            event = await asyncio.wait_for(trigger.wait_for_next(), timeout=3.0)
            assert event.payload["event_type"] == "modified"

            await trigger.stop()
        finally:
            fw_module.HAS_WATCHDOG = original_has_watchdog

    @pytest.mark.asyncio
    async def test_stop_unblocks_wait(self, tmp_path):
        """Stopping the trigger should unblock waiters."""
        from promptise.runtime.triggers.file_watch import FileWatchTrigger

        trigger = FileWatchTrigger(
            watch_path=str(tmp_path),
            poll_interval=0.1,
        )

        import promptise.runtime.triggers.file_watch as fw_module

        original_has_watchdog = fw_module.HAS_WATCHDOG
        fw_module.HAS_WATCHDOG = False

        try:
            await trigger.start()

            async def stop_later():
                await asyncio.sleep(0.2)
                await trigger.stop()

            asyncio.create_task(stop_later())

            with pytest.raises(asyncio.CancelledError):
                await asyncio.wait_for(trigger.wait_for_next(), timeout=5.0)
        finally:
            fw_module.HAS_WATCHDOG = original_has_watchdog

    @pytest.mark.asyncio
    async def test_creates_watch_directory(self, tmp_path):
        """If the watch directory doesn't exist, it should be created."""
        from promptise.runtime.triggers.file_watch import FileWatchTrigger

        watch_dir = tmp_path / "new_dir" / "subdir"
        assert not watch_dir.exists()

        trigger = FileWatchTrigger(
            watch_path=str(watch_dir),
            poll_interval=0.1,
        )

        import promptise.runtime.triggers.file_watch as fw_module

        original_has_watchdog = fw_module.HAS_WATCHDOG
        fw_module.HAS_WATCHDOG = False

        try:
            await trigger.start()
            assert watch_dir.exists()
            await trigger.stop()
        finally:
            fw_module.HAS_WATCHDOG = original_has_watchdog

    def test_repr(self):
        from pathlib import Path

        from promptise.runtime.triggers.file_watch import FileWatchTrigger

        # Use a platform-native absolute path so the repr comparison works on
        # Windows (where ``/data/inbox`` is rewritten to ``\\data\\inbox``).
        path = str(Path("/data/inbox").resolve())
        trigger = FileWatchTrigger(
            watch_path=path,
            patterns=["*.csv"],
        )
        r = repr(trigger)
        assert "FileWatchTrigger" in r
        assert path in r or path.replace("\\", "\\\\") in r


# ======================================================================
# WebhookTrigger
# ======================================================================


class TestWebhookTrigger:
    """Test webhook HTTP trigger."""

    @pytest.mark.asyncio
    async def test_webhook_receives_post(self):
        """POST to the webhook should produce a TriggerEvent."""
        pytest.importorskip("aiohttp")
        from aiohttp import ClientSession

        from promptise.runtime.triggers.webhook import WebhookTrigger

        trigger = WebhookTrigger(path="/webhook", port=19876, host="127.0.0.1")
        await trigger.start()

        try:
            # Send a POST request
            async with ClientSession() as session:
                async with session.post(
                    "http://127.0.0.1:19876/webhook",
                    json={"key": "value", "count": 42},
                ) as resp:
                    assert resp.status == 202
                    body = await resp.json()
                    assert body["status"] == "accepted"

            # Get the event
            event = await asyncio.wait_for(trigger.wait_for_next(), timeout=2.0)
            assert event.trigger_type == "webhook"
            assert event.payload == {"key": "value", "count": 42}
            assert event.metadata["method"] == "POST"
        finally:
            await trigger.stop()

    @pytest.mark.asyncio
    async def test_webhook_health_check(self):
        """Health endpoint should return status."""
        pytest.importorskip("aiohttp")
        from aiohttp import ClientSession

        from promptise.runtime.triggers.webhook import WebhookTrigger

        trigger = WebhookTrigger(path="/webhook", port=19877, host="127.0.0.1")
        await trigger.start()

        try:
            async with ClientSession() as session:
                async with session.get("http://127.0.0.1:19877/health") as resp:
                    assert resp.status == 200
                    body = await resp.json()
                    assert body["status"] == "healthy"
        finally:
            await trigger.stop()

    @pytest.mark.asyncio
    async def test_webhook_text_body(self):
        """Non-JSON POST should still work with text payload."""
        pytest.importorskip("aiohttp")
        from aiohttp import ClientSession

        from promptise.runtime.triggers.webhook import WebhookTrigger

        trigger = WebhookTrigger(path="/hook", port=19878, host="127.0.0.1")
        await trigger.start()

        try:
            async with ClientSession() as session:
                async with session.post(
                    "http://127.0.0.1:19878/hook",
                    data="plain text body",
                    headers={"Content-Type": "text/plain"},
                ) as resp:
                    assert resp.status == 202

            event = await asyncio.wait_for(trigger.wait_for_next(), timeout=2.0)
            assert event.trigger_type == "webhook"
            assert event.payload == "plain text body"
        finally:
            await trigger.stop()

    @pytest.mark.asyncio
    async def test_webhook_stop_unblocks(self):
        """Stopping the trigger should unblock waiters."""
        pytest.importorskip("aiohttp")

        from promptise.runtime.triggers.webhook import WebhookTrigger

        trigger = WebhookTrigger(path="/webhook", port=19879, host="127.0.0.1")
        await trigger.start()

        async def stop_later():
            await asyncio.sleep(0.2)
            await trigger.stop()

        asyncio.create_task(stop_later())

        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(trigger.wait_for_next(), timeout=5.0)

    def test_repr(self):
        pytest.importorskip("aiohttp")

        from promptise.runtime.triggers.webhook import WebhookTrigger

        trigger = WebhookTrigger(path="/webhook", port=9090)
        r = repr(trigger)
        assert "WebhookTrigger" in r
        assert "9090" in r


# ======================================================================
# CLI Tests
# ======================================================================


class TestCLIInit:
    """Test runtime init command."""

    def test_init_basic(self, tmp_path):
        from promptise.runtime.cli import runtime_app

        runner = CliRunner()
        output = tmp_path / "test.agent"
        result = runner.invoke(runtime_app, ["init", "-o", str(output), "-t", "basic"])
        assert result.exit_code == 0
        assert output.exists()
        content = output.read_text()
        assert "version:" in content
        assert "name:" in content
        assert "my-agent" in content

    def test_init_cron_template(self, tmp_path):
        from promptise.runtime.cli import runtime_app

        runner = CliRunner()
        output = tmp_path / "cron.agent"
        result = runner.invoke(runtime_app, ["init", "-o", str(output), "-t", "cron"])
        assert result.exit_code == 0
        content = output.read_text()
        assert "cron_expression" in content
        assert "data-watcher" in content

    def test_init_full_template(self, tmp_path):
        from promptise.runtime.cli import runtime_app

        runner = CliRunner()
        output = tmp_path / "full.agent"
        result = runner.invoke(runtime_app, ["init", "-o", str(output), "-t", "full"])
        assert result.exit_code == 0
        content = output.read_text()
        assert "journal:" in content
        assert "triggers:" in content

    def test_init_no_overwrite(self, tmp_path):
        from promptise.runtime.cli import runtime_app

        output = tmp_path / "existing.agent"
        output.write_text("existing content")

        runner = CliRunner()
        result = runner.invoke(runtime_app, ["init", "-o", str(output)])
        assert result.exit_code == 1
        assert "already exists" in result.output

    def test_init_force_overwrite(self, tmp_path):
        from promptise.runtime.cli import runtime_app

        output = tmp_path / "existing.agent"
        output.write_text("old content")

        runner = CliRunner()
        result = runner.invoke(runtime_app, ["init", "-o", str(output), "--force"])
        assert result.exit_code == 0
        assert "old content" not in output.read_text()

    def test_init_unknown_template(self, tmp_path):
        from promptise.runtime.cli import runtime_app

        runner = CliRunner()
        result = runner.invoke(runtime_app, ["init", "-o", str(tmp_path / "x.agent"), "-t", "nope"])
        assert result.exit_code == 1
        assert "Unknown template" in result.output


class TestCLIValidate:
    """Test runtime validate command."""

    def test_validate_valid_manifest(self, tmp_path):
        from promptise.runtime.cli import runtime_app

        path = _write_agent_manifest(tmp_path, name="valid-agent")

        runner = CliRunner()
        result = runner.invoke(runtime_app, ["validate", str(path)])
        assert result.exit_code == 0
        assert "Validation complete" in result.output

    def test_validate_nonexistent(self, tmp_path):
        from promptise.runtime.cli import runtime_app

        runner = CliRunner()
        result = runner.invoke(runtime_app, ["validate", str(tmp_path / "nope.agent")])
        assert result.exit_code == 1
        assert "Not found" in result.output

    def test_validate_invalid_yaml(self, tmp_path):
        from promptise.runtime.cli import runtime_app

        bad_file = tmp_path / "bad.agent"
        bad_file.write_text("version: '1.0'\n  bad indent: yes\n")

        runner = CliRunner()
        result = runner.invoke(runtime_app, ["validate", str(bad_file)])
        assert result.exit_code == 1

    def test_validate_missing_required_field(self, tmp_path):
        from promptise.runtime.cli import runtime_app

        bad_file = tmp_path / "noname.agent"
        bad_file.write_text("version: '1.0'\nmodel: openai:gpt-5-mini\n")

        runner = CliRunner()
        result = runner.invoke(runtime_app, ["validate", str(bad_file)])
        assert result.exit_code == 1

    def test_validate_with_warnings(self, tmp_path):
        """Manifest with no triggers/servers/instructions should warn."""
        from promptise.runtime.cli import runtime_app

        path = _write_agent_manifest(tmp_path, name="warn-agent")

        runner = CliRunner()
        result = runner.invoke(runtime_app, ["validate", str(path)])
        assert result.exit_code == 0
        # Should have warnings about no triggers and no servers
        assert "⚠" in result.output or "warnings" in result.output.lower() or "No " in result.output


class TestCLILogs:
    """Test runtime logs command."""

    def test_logs_empty_journal(self, tmp_path):
        """Logs for nonexistent journal should say no entries."""
        from promptise.runtime.cli import runtime_app

        runner = CliRunner()
        result = runner.invoke(
            runtime_app,
            [
                "logs",
                "nonexistent-process",
                "--journal-path",
                str(tmp_path),
            ],
        )
        assert result.exit_code == 0
        assert "No journal entries" in result.output

    def test_logs_with_entries(self, tmp_path):
        """Logs should display journal entries."""
        from promptise.runtime.cli import runtime_app
        from promptise.runtime.journal.base import JournalEntry
        from promptise.runtime.journal.file import FileJournal

        # Write some entries synchronously
        async def _write():
            journal = FileJournal(base_path=str(tmp_path))
            entry = JournalEntry(
                process_id="test-proc",
                entry_type="state_transition",
                data={"from": "created", "to": "running"},
            )
            await journal.append(entry)
            await journal.close()

        asyncio.run(_write())

        runner = CliRunner()
        result = runner.invoke(
            runtime_app,
            ["logs", "test-proc", "--journal-path", str(tmp_path)],
        )
        assert result.exit_code == 0
        assert "state_transition" in result.output


# ======================================================================
# Integration: create_trigger factory with new trigger types
# ======================================================================


class TestCreateTriggerFactory:
    """Test create_trigger factory handles webhook and file_watch."""

    def test_create_webhook_trigger(self):
        """Factory should create WebhookTrigger when aiohttp available."""
        pytest.importorskip("aiohttp")

        from promptise.runtime.triggers import create_trigger
        from promptise.runtime.triggers.webhook import WebhookTrigger

        config = TriggerConfig(
            type="webhook",
            webhook_path="/test",
            webhook_port=19880,
        )
        trigger = create_trigger(config)
        assert isinstance(trigger, WebhookTrigger)

    def test_create_file_watch_trigger(self, tmp_path):
        """Factory should create FileWatchTrigger."""
        from promptise.runtime.triggers import create_trigger
        from promptise.runtime.triggers.file_watch import FileWatchTrigger

        config = TriggerConfig(
            type="file_watch",
            watch_path=str(tmp_path),
            watch_patterns=["*.txt"],
        )
        trigger = create_trigger(config)
        assert isinstance(trigger, FileWatchTrigger)

    def test_create_unknown_trigger_raises(self):
        """Unknown trigger type should raise."""
        from promptise.runtime.exceptions import TriggerError
        from promptise.runtime.triggers import create_trigger

        # We can't create a TriggerConfig with unknown type due to Literal
        # validation, so we test via a mock
        config = MagicMock()
        config.type = "unknown_type"
        with pytest.raises(TriggerError, match="Unknown trigger type"):
            create_trigger(config)
