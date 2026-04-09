"""Tests for observability transporters."""

from __future__ import annotations

import json
import os
import tempfile

import pytest

from promptise.observability import TimelineEntry, TimelineEventCategory, TimelineEventType


def _make_entry(**overrides) -> TimelineEntry:
    """Create a test TimelineEntry."""
    defaults = {
        "entry_id": "test-001",
        "timestamp": 1700000000.0,
        "event_type": TimelineEventType.TOOL_CALL,
        "category": TimelineEventCategory.AGENT,
        "agent_id": "test-agent",
        "details": "Tool call: search",
        "metadata": {"tool": "search"},
    }
    defaults.update(overrides)
    return TimelineEntry(**defaults)


# ---------------------------------------------------------------------------
# CallbackTransporter
# ---------------------------------------------------------------------------


class TestCallbackTransporter:
    def test_invokes_callback_on_event(self):
        from promptise.observability_transporters import CallbackTransporter

        received = []
        t = CallbackTransporter(callback=lambda e: received.append(e))
        t.on_event(_make_entry())
        assert len(received) == 1
        assert received[0].entry_id == "test-001"

    def test_callback_error_doesnt_crash(self):
        from promptise.observability_transporters import CallbackTransporter

        def bad_callback(e):
            raise RuntimeError("boom")

        t = CallbackTransporter(callback=bad_callback)
        t.on_event(_make_entry())  # Should not raise

    def test_flush_and_close_are_noop(self):
        from promptise.observability_transporters import CallbackTransporter

        t = CallbackTransporter(callback=lambda e: None)
        t.flush()
        t.close()


# ---------------------------------------------------------------------------
# JSONFileTransporter
# ---------------------------------------------------------------------------


class TestJSONFileTransporter:
    def test_ndjson_streaming(self):
        from promptise.observability_transporters import JSONFileTransporter

        with tempfile.TemporaryDirectory() as tmpdir:
            t = JSONFileTransporter(output_dir=tmpdir, session_name="test", stream=True)
            t.on_event(_make_entry(entry_id="e1"))
            t.on_event(_make_entry(entry_id="e2"))
            t.flush()

            # Find the NDJSON file
            files = os.listdir(tmpdir)
            ndjson_files = [f for f in files if f.endswith(".ndjson")]
            assert len(ndjson_files) == 1

            with open(os.path.join(tmpdir, ndjson_files[0])) as f:
                lines = f.readlines()
            assert len(lines) == 2
            assert json.loads(lines[0])["entry_id"] == "e1"

    def test_close_writes_summary(self):
        from promptise.observability_transporters import JSONFileTransporter

        with tempfile.TemporaryDirectory() as tmpdir:
            t = JSONFileTransporter(output_dir=tmpdir, session_name="test")
            t.on_event(_make_entry())
            t.close()
            # Should have at least one file
            assert len(os.listdir(tmpdir)) >= 1


# ---------------------------------------------------------------------------
# StructuredLogTransporter
# ---------------------------------------------------------------------------


class TestStructuredLogTransporter:
    def test_writes_to_file(self):
        from promptise.observability_transporters import StructuredLogTransporter

        with tempfile.NamedTemporaryFile(mode="w", suffix=".jsonl", delete=False) as f:
            path = f.name
        try:
            t = StructuredLogTransporter(log_file=path, session_name="test")
            t.on_event(_make_entry())
            t.flush()
            with open(path) as f:
                content = f.read()
            assert "test-001" in content
        finally:
            os.unlink(path)


# ---------------------------------------------------------------------------
# ConsoleTransporter
# ---------------------------------------------------------------------------


class TestConsoleTransporter:
    def test_prints_event(self, capsys):
        from promptise.observability_transporters import ConsoleTransporter

        t = ConsoleTransporter(live=False, verbose=True)
        t.on_event(_make_entry())
        # Should produce some output (Rich or plain)
        # Don't assert exact format — just that it doesn't crash


# ---------------------------------------------------------------------------
# HTMLReportTransporter
# ---------------------------------------------------------------------------


class TestHTMLReportTransporter:
    def test_accepts_events_without_crash(self):
        from promptise.observability_transporters import HTMLReportTransporter

        with tempfile.TemporaryDirectory() as tmpdir:
            t = HTMLReportTransporter(output_dir=tmpdir, session_name="test")
            t.on_event(_make_entry(entry_id="e1"))
            t.on_event(_make_entry(entry_id="e2"))
            t.flush()
            t.close()
            # Transporter should accept events without crashing


# ---------------------------------------------------------------------------
# PrometheusTransporter
# ---------------------------------------------------------------------------


class TestPrometheusTransporter:
    def test_records_metrics(self):
        from promptise.observability_transporters import PrometheusTransporter

        try:
            t = PrometheusTransporter(port=0)
            t.on_event(_make_entry())
            # Should not crash. Metrics recorded internally.
        except ImportError:
            pytest.skip("prometheus_client not installed")


# ---------------------------------------------------------------------------
# WebhookTransporter
# ---------------------------------------------------------------------------


class TestWebhookTransporter:
    def test_construction(self):
        from promptise.observability_transporters import WebhookTransporter

        t = WebhookTransporter(url="https://hooks.example.com/test")
        assert t.url == "https://hooks.example.com/test"

    def test_batch_mode(self):
        from promptise.observability_transporters import WebhookTransporter

        t = WebhookTransporter(
            url="https://hooks.example.com/test",
            batch_size=5,
        )
        # Should buffer events
        t.on_event(_make_entry())
        t.on_event(_make_entry())
        # Not flushed yet (batch_size=5)


# ---------------------------------------------------------------------------
# OTLPTransporter
# ---------------------------------------------------------------------------


class TestOTLPTransporter:
    def test_construction(self):
        from promptise.observability_transporters import OTLPTransporter

        try:
            t = OTLPTransporter(endpoint="http://localhost:4317")
            # Should construct without error
        except ImportError:
            pytest.skip("opentelemetry not available")
