"""Tests for runtime Phase 3: journal, manifest."""

from __future__ import annotations

from pathlib import Path

import pytest
import yaml

from promptise.runtime.exceptions import (
    ManifestError,
    ManifestValidationError,
)
from promptise.runtime.journal import (
    FileJournal,
    InMemoryJournal,
    JournalEntry,
    JournalLevel,
    ReplayEngine,
)
from promptise.runtime.manifest import (
    AgentManifestSchema,
    load_manifest,
    manifest_to_process_config,
    save_manifest,
    validate_manifest,
)

# =========================================================================
# JournalEntry
# =========================================================================


class TestJournalEntry:
    """Verify journal entry creation and serialization."""

    def test_create(self) -> None:
        entry = JournalEntry(
            process_id="p-1",
            entry_type="trigger_event",
            data={"trigger_type": "cron"},
        )
        assert entry.process_id == "p-1"
        assert entry.entry_type == "trigger_event"
        assert entry.entry_id  # auto-generated

    def test_to_dict_from_dict_roundtrip(self) -> None:
        entry = JournalEntry(
            process_id="p-1",
            entry_type="checkpoint",
            data={"state": {"counter": 42}},
        )
        data = entry.to_dict()
        restored = JournalEntry.from_dict(data)
        assert restored.process_id == entry.process_id
        assert restored.entry_type == entry.entry_type
        assert restored.data == entry.data


class TestJournalLevel:
    """Verify journal level enum."""

    def test_values(self) -> None:
        assert JournalLevel.NONE == "none"
        assert JournalLevel.CHECKPOINT == "checkpoint"
        assert JournalLevel.FULL == "full"


# =========================================================================
# InMemoryJournal
# =========================================================================


class TestInMemoryJournal:
    """Verify in-memory journal backend."""

    async def test_append_and_read(self) -> None:
        journal = InMemoryJournal()
        await journal.append(JournalEntry(process_id="p-1", entry_type="test", data={"x": 1}))
        await journal.append(JournalEntry(process_id="p-1", entry_type="test", data={"x": 2}))
        await journal.append(JournalEntry(process_id="p-2", entry_type="test", data={"x": 3}))

        entries = await journal.read("p-1")
        assert len(entries) == 2
        assert entries[0].data["x"] == 1

    async def test_read_with_entry_type_filter(self) -> None:
        journal = InMemoryJournal()
        await journal.append(JournalEntry(process_id="p-1", entry_type="trigger"))
        await journal.append(JournalEntry(process_id="p-1", entry_type="checkpoint"))

        entries = await journal.read("p-1", entry_type="checkpoint")
        assert len(entries) == 1
        assert entries[0].entry_type == "checkpoint"

    async def test_read_with_limit(self) -> None:
        journal = InMemoryJournal()
        for i in range(10):
            await journal.append(JournalEntry(process_id="p-1", entry_type="test"))

        entries = await journal.read("p-1", limit=3)
        assert len(entries) == 3

    async def test_checkpoint_and_last_checkpoint(self) -> None:
        journal = InMemoryJournal()
        await journal.checkpoint("p-1", {"counter": 10})
        await journal.checkpoint("p-1", {"counter": 20})

        last = await journal.last_checkpoint("p-1")
        assert last == {"counter": 20}

    async def test_last_checkpoint_missing(self) -> None:
        journal = InMemoryJournal()
        assert await journal.last_checkpoint("nonexistent") is None

    async def test_close(self) -> None:
        journal = InMemoryJournal()
        await journal.close()  # No-op, should not raise

    def test_repr(self) -> None:
        journal = InMemoryJournal()
        assert "entries=0" in repr(journal)


# =========================================================================
# FileJournal
# =========================================================================


class TestFileJournal:
    """Verify file-based journal backend."""

    async def test_append_and_read(self, tmp_path: Path) -> None:
        journal = FileJournal(str(tmp_path))
        await journal.append(JournalEntry(process_id="p-1", entry_type="test", data={"x": 1}))
        await journal.append(JournalEntry(process_id="p-1", entry_type="test", data={"x": 2}))

        entries = await journal.read("p-1")
        assert len(entries) == 2
        assert entries[0].data["x"] == 1
        assert entries[1].data["x"] == 2

    async def test_jsonl_format(self, tmp_path: Path) -> None:
        journal = FileJournal(str(tmp_path))
        await journal.append(JournalEntry(process_id="p-1", entry_type="test", data={"x": 1}))

        # Verify JSONL file exists and is valid
        jsonl_path = tmp_path / "p-1.jsonl"
        assert jsonl_path.exists()
        lines = jsonl_path.read_text().strip().split("\n")
        assert len(lines) == 1

    async def test_checkpoint(self, tmp_path: Path) -> None:
        journal = FileJournal(str(tmp_path))
        await journal.checkpoint("p-1", {"counter": 42})

        last = await journal.last_checkpoint("p-1")
        assert last == {"counter": 42}

        # Checkpoint should also be in journal
        entries = await journal.read("p-1", entry_type="checkpoint")
        assert len(entries) == 1

    async def test_read_with_filters(self, tmp_path: Path) -> None:
        journal = FileJournal(str(tmp_path))
        await journal.append(JournalEntry(process_id="p-1", entry_type="trigger"))
        await journal.append(JournalEntry(process_id="p-1", entry_type="invoke"))

        entries = await journal.read("p-1", entry_type="invoke")
        assert len(entries) == 1

    async def test_read_nonexistent_process(self, tmp_path: Path) -> None:
        journal = FileJournal(str(tmp_path))
        entries = await journal.read("nonexistent")
        assert entries == []

    async def test_close(self, tmp_path: Path) -> None:
        journal = FileJournal(str(tmp_path))
        await journal.close()  # No-op


# =========================================================================
# ReplayEngine
# =========================================================================


class TestReplayEngine:
    """Verify journal replay for crash recovery."""

    async def test_recover_from_checkpoint(self) -> None:
        journal = InMemoryJournal()

        # Write some entries + checkpoint + more entries
        await journal.append(
            JournalEntry(
                process_id="p-1",
                entry_type="state_transition",
                data={"to_state": "starting"},
            )
        )
        await journal.checkpoint(
            "p-1", {"context_state": {"counter": 5}, "lifecycle_state": "running"}
        )
        await journal.append(
            JournalEntry(
                process_id="p-1",
                entry_type="context_update",
                data={"key": "counter", "value": 10},
            )
        )

        engine = ReplayEngine(journal)
        result = await engine.recover("p-1")

        assert result["context_state"]["counter"] == 10
        assert result["lifecycle_state"] == "running"
        assert result["entries_replayed"] == 1

    async def test_recover_without_checkpoint(self) -> None:
        journal = InMemoryJournal()
        await journal.append(
            JournalEntry(
                process_id="p-1",
                entry_type="state_transition",
                data={"to_state": "running"},
            )
        )

        engine = ReplayEngine(journal)
        result = await engine.recover("p-1")

        assert result["lifecycle_state"] == "running"
        assert result["entries_replayed"] == 1

    async def test_recover_empty_journal(self) -> None:
        journal = InMemoryJournal()
        engine = ReplayEngine(journal)
        result = await engine.recover("p-1")

        assert result["context_state"] == {}
        assert result["lifecycle_state"] == "created"
        assert result["entries_replayed"] == 0


# =========================================================================
# AgentManifestSchema
# =========================================================================


class TestAgentManifestSchema:
    """Verify manifest schema validation."""

    def test_valid_manifest(self) -> None:
        manifest = AgentManifestSchema(
            name="test-agent",
            model="openai:gpt-5-mini",
            instructions="Do stuff",
            triggers=[
                {"type": "cron", "cron_expression": "*/5 * * * *"},
            ],
        )
        assert manifest.name == "test-agent"
        assert manifest.version == "1.0"

    def test_name_required(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            AgentManifestSchema()  # type: ignore[call-arg]

    def test_extra_fields_rejected(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError):
            AgentManifestSchema(name="test", unknown_field="bad")  # type: ignore[call-arg]

    def test_trigger_missing_type_raises(self) -> None:
        from pydantic import ValidationError

        with pytest.raises(ValidationError, match="type"):
            AgentManifestSchema(
                name="test",
                triggers=[{"cron_expression": "* * * * *"}],  # missing type
            )


# =========================================================================
# load_manifest / save_manifest
# =========================================================================


class TestLoadSaveManifest:
    """Verify manifest file I/O."""

    def test_load_valid_manifest(self, tmp_path: Path) -> None:
        manifest_file = tmp_path / "test.agent"
        manifest_file.write_text(
            yaml.dump(
                {
                    "version": "1.0",
                    "name": "test-agent",
                    "model": "openai:gpt-5-mini",
                    "instructions": "Monitor data",
                    "triggers": [
                        {"type": "cron", "cron_expression": "*/5 * * * *"},
                    ],
                    "world": {"status": "healthy"},
                }
            )
        )

        manifest = load_manifest(manifest_file)
        assert manifest.name == "test-agent"
        assert len(manifest.triggers) == 1
        assert manifest.world["status"] == "healthy"

    def test_load_missing_file_raises(self) -> None:
        with pytest.raises(ManifestError, match="not found"):
            load_manifest("/nonexistent/path.agent")

    def test_load_invalid_yaml_raises(self, tmp_path: Path) -> None:
        bad_file = tmp_path / "bad.agent"
        bad_file.write_text("{ invalid yaml [")

        with pytest.raises(ManifestError, match="Invalid YAML"):
            load_manifest(bad_file)

    def test_load_invalid_schema_raises(self, tmp_path: Path) -> None:
        bad_file = tmp_path / "bad.agent"
        bad_file.write_text(yaml.dump({"version": "1.0"}))  # missing name

        with pytest.raises(ManifestValidationError):
            load_manifest(bad_file)

    def test_save_and_load_roundtrip(self, tmp_path: Path) -> None:
        manifest = AgentManifestSchema(
            name="roundtrip-agent",
            model="openai:gpt-4o",
            instructions="Test instructions",
            triggers=[{"type": "webhook"}],
            world={"key": "value"},
        )
        path = tmp_path / "roundtrip.agent"
        save_manifest(manifest, path)

        loaded = load_manifest(path)
        assert loaded.name == "roundtrip-agent"
        assert loaded.model == "openai:gpt-4o"
        assert loaded.world["key"] == "value"

    def test_validate_manifest_warnings(self, tmp_path: Path) -> None:
        manifest_file = tmp_path / "bare.agent"
        manifest_file.write_text(yaml.dump({"version": "1.0", "name": "bare"}))

        warnings = validate_manifest(manifest_file)
        assert len(warnings) >= 2  # No instructions, no triggers, no servers


# =========================================================================
# manifest_to_process_config
# =========================================================================


class TestManifestToProcessConfig:
    """Verify conversion from manifest to ProcessConfig."""

    def test_basic_conversion(self) -> None:
        manifest = AgentManifestSchema(
            name="converter-test",
            model="openai:gpt-4o",
            instructions="Test",
            triggers=[
                {"type": "cron", "cron_expression": "*/10 * * * *"},
            ],
            world={"counter": 0},
            config={"concurrency": 3, "heartbeat_interval": 30},
        )

        config = manifest_to_process_config(manifest)
        assert config.model == "openai:gpt-4o"
        assert config.instructions == "Test"
        assert len(config.triggers) == 1
        assert config.triggers[0].type == "cron"
        assert config.context.initial_state == {"counter": 0}
        assert config.concurrency == 3
        assert config.heartbeat_interval == 30

    def test_defaults_applied(self) -> None:
        manifest = AgentManifestSchema(name="minimal")
        config = manifest_to_process_config(manifest)
        assert config.model == "openai:gpt-5-mini"
        assert config.journal.level == "checkpoint"
