"""Tests for the ObservabilityCollector and timeline data model."""

import json
import time

import pytest

from promptise.observability import (
    ObservabilityCollector,
    TimelineEntry,
    TimelineEventCategory,
    TimelineEventType,
    _derive_category,
    _truncate_for_metadata,
)

# ---------------------------------------------------------------------------
# TimelineEventType / Category derivation
# ---------------------------------------------------------------------------


class TestCategoryDerivation:
    def test_agent_events_map_to_agent_category(self):
        assert _derive_category(TimelineEventType.AGENT_REGISTERED) == TimelineEventCategory.AGENT
        assert _derive_category(TimelineEventType.AGENT_DEREGISTERED) == TimelineEventCategory.AGENT

    def test_task_events_map_to_task_category(self):
        assert _derive_category(TimelineEventType.TASK_CREATED) == TimelineEventCategory.TASK
        assert _derive_category(TimelineEventType.TASK_STARTED) == TimelineEventCategory.TASK
        assert _derive_category(TimelineEventType.TASK_COMPLETED) == TimelineEventCategory.TASK
        assert _derive_category(TimelineEventType.TASK_FAILED) == TimelineEventCategory.TASK
        assert _derive_category(TimelineEventType.TASK_ASSIGNED) == TimelineEventCategory.TASK

    def test_orchestration_events_map_to_orchestration_category(self):
        assert (
            _derive_category(TimelineEventType.PHASE_START) == TimelineEventCategory.ORCHESTRATION
        )
        assert _derive_category(TimelineEventType.PHASE_END) == TimelineEventCategory.ORCHESTRATION
        assert (
            _derive_category(TimelineEventType.DECOMPOSITION) == TimelineEventCategory.ORCHESTRATION
        )
        assert (
            _derive_category(TimelineEventType.DISTRIBUTION) == TimelineEventCategory.ORCHESTRATION
        )
        assert _derive_category(TimelineEventType.CONSENSUS) == TimelineEventCategory.ORCHESTRATION
        assert (
            _derive_category(TimelineEventType.AGGREGATION) == TimelineEventCategory.ORCHESTRATION
        )
        assert (
            _derive_category(TimelineEventType.PIPELINE_STAGE)
            == TimelineEventCategory.ORCHESTRATION
        )

    def test_security_events_map_to_security_category(self):
        assert _derive_category(TimelineEventType.AUTH_ATTEMPT) == TimelineEventCategory.SECURITY
        assert _derive_category(TimelineEventType.RBAC_CHECK) == TimelineEventCategory.SECURITY

    def test_system_events_map_to_system_category(self):
        assert _derive_category(TimelineEventType.HEALTH_CHECK) == TimelineEventCategory.SYSTEM
        assert _derive_category(TimelineEventType.CIRCUIT_BREAKER) == TimelineEventCategory.SYSTEM

    def test_transparency_events_map_to_transparency_category(self):
        assert _derive_category(TimelineEventType.AGENT_INPUT) == TimelineEventCategory.TRANSPARENCY
        assert (
            _derive_category(TimelineEventType.AGENT_OUTPUT) == TimelineEventCategory.TRANSPARENCY
        )
        assert _derive_category(TimelineEventType.TOOL_CALL) == TimelineEventCategory.TRANSPARENCY
        assert _derive_category(TimelineEventType.TOOL_RESULT) == TimelineEventCategory.TRANSPARENCY
        assert _derive_category(TimelineEventType.TOOL_ERROR) == TimelineEventCategory.TRANSPARENCY
        assert _derive_category(TimelineEventType.LLM_TURN) == TimelineEventCategory.TRANSPARENCY

    def test_agent_input_output_override_agent_prefix(self):
        """agent.input and agent.output should map to TRANSPARENCY, not AGENT."""
        assert _derive_category(TimelineEventType.AGENT_INPUT) != TimelineEventCategory.AGENT
        assert _derive_category(TimelineEventType.AGENT_OUTPUT) != TimelineEventCategory.AGENT


# ---------------------------------------------------------------------------
# TimelineEntry
# ---------------------------------------------------------------------------


class TestTimelineEntry:
    def test_to_dict(self):
        entry = TimelineEntry(
            entry_id="abc123",
            timestamp=1000.0,
            event_type=TimelineEventType.TASK_STARTED,
            category=TimelineEventCategory.TASK,
            agent_id="agent-1",
            phase="hierarchical",
            details="Started task",
            duration=5.0,
            parent_id=None,
            metadata={"key": "value"},
        )
        d = entry.to_dict()
        assert d["entry_id"] == "abc123"
        assert d["event_type"] == "task.started"
        assert d["category"] == "task"
        assert d["agent_id"] == "agent-1"
        assert d["phase"] == "hierarchical"
        assert d["duration"] == 5.0
        assert d["metadata"] == {"key": "value"}


# ---------------------------------------------------------------------------
# ObservabilityCollector — record()
# ---------------------------------------------------------------------------


class TestCollectorRecord:
    def test_record_creates_entry(self):
        c = ObservabilityCollector("test")
        entry = c.record(TimelineEventType.AGENT_REGISTERED, agent_id="a1", details="registered")
        assert entry.event_type == TimelineEventType.AGENT_REGISTERED
        assert entry.agent_id == "a1"
        assert entry.details == "registered"
        assert entry.category == TimelineEventCategory.AGENT

    def test_record_appears_in_timeline(self):
        c = ObservabilityCollector("test")
        c.record(TimelineEventType.TASK_STARTED, agent_id="a1")
        timeline = c.get_timeline()
        assert len(timeline) == 1
        assert timeline[0].event_type == TimelineEventType.TASK_STARTED

    def test_record_with_metadata(self):
        c = ObservabilityCollector("test")
        entry = c.record(
            TimelineEventType.DECOMPOSITION,
            metadata={"subtask_count": 3},
        )
        assert entry.metadata == {"subtask_count": 3}

    def test_record_without_optional_fields(self):
        c = ObservabilityCollector("test")
        entry = c.record(TimelineEventType.HEALTH_CHECK)
        assert entry.agent_id is None
        assert entry.phase is None
        assert entry.duration is None
        assert entry.parent_id is None
        assert entry.metadata == {}


# ---------------------------------------------------------------------------
# ObservabilityCollector — span()
# ---------------------------------------------------------------------------


class TestCollectorSpan:
    def test_span_sets_duration(self):
        c = ObservabilityCollector("test")
        with c.span(TimelineEventType.PHASE_START, phase="hierarchical") as entry:
            time.sleep(0.05)
        assert entry.duration is not None
        assert entry.duration >= 0.04  # allow slight timing variance

    def test_span_appears_in_timeline(self):
        c = ObservabilityCollector("test")
        with c.span(TimelineEventType.PHASE_START, phase="swarm"):
            pass
        timeline = c.get_timeline()
        assert len(timeline) == 1
        assert timeline[0].phase == "swarm"
        assert timeline[0].duration is not None

    def test_span_sets_duration_on_exception(self):
        c = ObservabilityCollector("test")
        with pytest.raises(ValueError):
            with c.span(TimelineEventType.PHASE_START, phase="pipeline") as entry:
                raise ValueError("oops")
        # Duration should still be set even though an exception occurred
        assert entry.duration is not None


# ---------------------------------------------------------------------------
# ObservabilityCollector — queries
# ---------------------------------------------------------------------------


class TestCollectorQueries:
    def test_get_timeline_sorted(self):
        c = ObservabilityCollector("test")
        # Record entries with slight time gaps
        c.record(TimelineEventType.AGENT_REGISTERED, agent_id="a1")
        time.sleep(0.01)
        c.record(TimelineEventType.TASK_STARTED, agent_id="a2")
        time.sleep(0.01)
        c.record(TimelineEventType.TASK_COMPLETED, agent_id="a1")

        timeline = c.get_timeline()
        assert len(timeline) == 3
        # Should be sorted by timestamp
        for i in range(len(timeline) - 1):
            assert timeline[i].timestamp <= timeline[i + 1].timestamp

    def test_get_agents(self):
        c = ObservabilityCollector("test")
        c.record(TimelineEventType.TASK_STARTED, agent_id="b-agent")
        c.record(TimelineEventType.TASK_COMPLETED, agent_id="a-agent")
        c.record(TimelineEventType.TASK_COMPLETED, agent_id="b-agent")
        c.record(TimelineEventType.HEALTH_CHECK)  # no agent_id

        agents = c.get_agents()
        assert agents == ["a-agent", "b-agent"]

    def test_get_phases(self):
        c = ObservabilityCollector("test")
        c.record(TimelineEventType.PHASE_START, phase="swarm")
        c.record(TimelineEventType.PHASE_START, phase="hierarchical")
        c.record(TimelineEventType.TASK_STARTED)  # no phase

        phases = c.get_phases()
        assert phases == ["hierarchical", "swarm"]

    def test_get_agents_empty(self):
        c = ObservabilityCollector("test")
        assert c.get_agents() == []

    def test_get_phases_empty(self):
        c = ObservabilityCollector("test")
        assert c.get_phases() == []


# ---------------------------------------------------------------------------
# ObservabilityCollector — serialization
# ---------------------------------------------------------------------------


class TestCollectorSerialization:
    def test_to_dict(self):
        c = ObservabilityCollector("my-session")
        c.record(TimelineEventType.AGENT_REGISTERED, agent_id="x")
        c.record(TimelineEventType.TASK_COMPLETED, agent_id="x", phase="hierarchical")

        d = c.to_dict()
        assert d["session_name"] == "my-session"
        assert d["entry_count"] == 2
        assert "x" in d["agents"]
        assert "hierarchical" in d["phases"]
        assert len(d["entries"]) == 2
        assert d["total_duration"] >= 0

    def test_to_json(self):
        c = ObservabilityCollector("json-test")
        c.record(TimelineEventType.HEALTH_CHECK, details="ok")

        j = c.to_json()
        data = json.loads(j)
        assert data["session_name"] == "json-test"
        assert len(data["entries"]) == 1
        assert data["entries"][0]["event_type"] == "health.check"

    def test_to_dict_empty(self):
        c = ObservabilityCollector("empty")
        d = c.to_dict()
        assert d["entry_count"] == 0
        assert d["entries"] == []
        assert d["agents"] == []

    def test_entry_to_dict_roundtrip(self):
        c = ObservabilityCollector("test")
        entry = c.record(
            TimelineEventType.TASK_ASSIGNED,
            agent_id="agent-1",
            phase="hierarchical",
            details="Assigned task code_review",
            metadata={"task_id": "t-123"},
        )
        d = entry.to_dict()
        assert d["entry_id"] == entry.entry_id
        assert d["event_type"] == "task.assigned"
        assert d["category"] == "task"
        assert d["agent_id"] == "agent-1"


# ---------------------------------------------------------------------------
# _truncate_for_metadata
# ---------------------------------------------------------------------------


class TestTruncateForMetadata:
    def test_short_string_unchanged(self):
        assert _truncate_for_metadata("hello") == "hello"

    def test_empty_string_unchanged(self):
        assert _truncate_for_metadata("") == ""

    def test_string_at_max_length_unchanged(self):
        s = "x" * 2000
        assert _truncate_for_metadata(s) == s

    def test_string_over_max_length_truncated(self):
        s = "x" * 2500
        result = _truncate_for_metadata(s)
        assert len(result) < len(s)
        assert result.startswith("x" * 2000)
        assert "truncated" in result
        assert "2500 total chars" in result

    def test_custom_max_length(self):
        s = "abcdefghij"
        result = _truncate_for_metadata(s, max_len=5)
        assert result.startswith("abcde")
        assert "truncated" in result
        assert "10 total chars" in result

    def test_exact_max_length_not_truncated(self):
        s = "abc"
        result = _truncate_for_metadata(s, max_len=3)
        assert result == "abc"


# ---------------------------------------------------------------------------
# Transparency event recording integration
# ---------------------------------------------------------------------------


class TestTransparencyRecording:
    """Test that transparency events work with the collector end-to-end."""

    def test_record_tool_call(self):
        c = ObservabilityCollector("test")
        entry = c.record(
            TimelineEventType.TOOL_CALL,
            agent_id="agent-1",
            details="Calling tool: search_files",
            metadata={"tool_name": "search_files", "arguments": "{'query': 'test'}"},
        )
        assert entry.event_type == TimelineEventType.TOOL_CALL
        assert entry.category == TimelineEventCategory.TRANSPARENCY
        assert entry.metadata["tool_name"] == "search_files"

    def test_record_llm_turn(self):
        c = ObservabilityCollector("test")
        entry = c.record(
            TimelineEventType.LLM_TURN,
            agent_id="agent-1",
            details="LLM requested tool: read_file",
            metadata={
                "message_type": "ai_tool_call",
                "tool_name": "read_file",
                "tool_args": "{'path': '/app/main.py'}",
            },
        )
        assert entry.event_type == TimelineEventType.LLM_TURN
        assert entry.category == TimelineEventCategory.TRANSPARENCY
        assert entry.metadata["message_type"] == "ai_tool_call"

    def test_record_agent_input_output(self):
        c = ObservabilityCollector("test")
        c.record(
            TimelineEventType.AGENT_INPUT,
            agent_id="agent-1",
            details="Agent input for task review",
            metadata={"task_id": "t-1", "input_text": "Review this code"},
        )
        c.record(
            TimelineEventType.AGENT_OUTPUT,
            agent_id="agent-1",
            details="Agent output for task review (5 messages)",
            metadata={"task_id": "t-1", "message_count": 5, "final_output": "Done"},
        )

        timeline = c.get_timeline()
        transparency = [e for e in timeline if e.category == TimelineEventCategory.TRANSPARENCY]
        assert len(transparency) == 2
        assert transparency[0].event_type == TimelineEventType.AGENT_INPUT
        assert transparency[1].event_type == TimelineEventType.AGENT_OUTPUT

    def test_transparency_events_in_serialization(self):
        c = ObservabilityCollector("test")
        c.record(TimelineEventType.TOOL_CALL, agent_id="a1", details="test")
        c.record(TimelineEventType.TOOL_RESULT, agent_id="a1", details="result")
        c.record(TimelineEventType.TOOL_ERROR, agent_id="a1", details="error")

        data = c.to_dict()
        types = [e["event_type"] for e in data["entries"]]
        assert "tool.call" in types
        assert "tool.result" in types
        assert "tool.error" in types
        categories = [e["category"] for e in data["entries"]]
        assert all(c == "transparency" for c in categories)
