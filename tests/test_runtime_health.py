"""Tests for promptise.runtime.health — HealthMonitor, AnomalyType, Anomaly."""

from __future__ import annotations

import time

import pytest

from promptise.runtime.config import HealthConfig
from promptise.runtime.health import Anomaly, AnomalyType, HealthMonitor

# ---------------------------------------------------------------------------
# AnomalyType enum
# ---------------------------------------------------------------------------


class TestAnomalyType:
    def test_stuck_value(self):
        assert AnomalyType.STUCK is not None

    def test_loop_value(self):
        assert AnomalyType.LOOP is not None

    def test_empty_response_value(self):
        assert AnomalyType.EMPTY_RESPONSE is not None

    def test_high_error_rate_value(self):
        assert AnomalyType.HIGH_ERROR_RATE is not None

    def test_members_are_unique(self):
        values = [m.value for m in AnomalyType]
        assert len(values) == len(set(values))


# ---------------------------------------------------------------------------
# Anomaly dataclass
# ---------------------------------------------------------------------------


class TestAnomaly:
    def test_creation(self):
        a = Anomaly(anomaly_type=AnomalyType.STUCK, description="stuck on tool X")
        assert a.anomaly_type == AnomalyType.STUCK
        assert a.description == "stuck on tool X"

    def test_creation_loop(self):
        a = Anomaly(anomaly_type=AnomalyType.LOOP, description="loop detected")
        assert a.anomaly_type == AnomalyType.LOOP

    def test_creation_empty_response(self):
        a = Anomaly(anomaly_type=AnomalyType.EMPTY_RESPONSE, description="empty")
        assert a.anomaly_type == AnomalyType.EMPTY_RESPONSE

    def test_creation_high_error_rate(self):
        a = Anomaly(anomaly_type=AnomalyType.HIGH_ERROR_RATE, description="errors")
        assert a.anomaly_type == AnomalyType.HIGH_ERROR_RATE


# ---------------------------------------------------------------------------
# HealthMonitor — stuck detection
# ---------------------------------------------------------------------------


class TestStuckDetection:
    @pytest.mark.asyncio
    async def test_identical_calls_trigger_stuck(self):
        cfg = HealthConfig(stuck_threshold=3)
        monitor = HealthMonitor(cfg, process_id="test")
        for _ in range(2):
            result = await monitor.record_tool_call("hammer", {})
        # Third identical call should trigger STUCK
        result = await monitor.record_tool_call("hammer", {})
        assert result is not None
        assert result.anomaly_type == AnomalyType.STUCK

    @pytest.mark.asyncio
    async def test_below_threshold_returns_none(self):
        cfg = HealthConfig(stuck_threshold=5, loop_window=20, loop_min_repeats=10)
        monitor = HealthMonitor(cfg, process_id="test")
        for _ in range(4):
            result = await monitor.record_tool_call("hammer", {})
        # 4 calls < 5 threshold → no anomaly yet
        assert result is None

    @pytest.mark.asyncio
    async def test_different_calls_no_stuck(self):
        cfg = HealthConfig(stuck_threshold=3)
        monitor = HealthMonitor(cfg, process_id="test")
        tools = ["hammer", "screwdriver", "wrench", "pliers", "saw"]
        results = []
        for t in tools:
            results.append(await monitor.record_tool_call(t, {}))
        assert all(r is None for r in results)


# ---------------------------------------------------------------------------
# HealthMonitor — loop detection
# ---------------------------------------------------------------------------


class TestLoopDetection:
    @pytest.mark.asyncio
    async def test_repeating_subsequence_triggers_loop(self):
        cfg = HealthConfig(stuck_threshold=100, cooldown=0.0)
        monitor = HealthMonitor(cfg, process_id="test")
        # A, B, A, B, A, B — should detect loop
        results = []
        for _ in range(3):
            results.append(await monitor.record_tool_call("A", {}))
            results.append(await monitor.record_tool_call("B", {}))
        loop_results = [r for r in results if r is not None]
        assert len(loop_results) >= 1
        assert loop_results[0].anomaly_type == AnomalyType.LOOP

    @pytest.mark.asyncio
    async def test_non_repeating_no_loop(self):
        cfg = HealthConfig(stuck_threshold=100)
        monitor = HealthMonitor(cfg, process_id="test")
        tools = ["A", "B", "C", "D", "E", "F"]
        results = []
        for t in tools:
            results.append(await monitor.record_tool_call(t, {}))
        assert all(r is None for r in results)


# ---------------------------------------------------------------------------
# HealthMonitor — empty response detection
# ---------------------------------------------------------------------------


class TestEmptyResponseDetection:
    @pytest.mark.asyncio
    async def test_short_responses_trigger_empty(self):
        cfg = HealthConfig(empty_response_threshold=3)
        monitor = HealthMonitor(cfg, process_id="test")
        result = None
        for _ in range(3):
            result = await monitor.record_response("")
        assert result is not None
        assert result.anomaly_type == AnomalyType.EMPTY_RESPONSE

    @pytest.mark.asyncio
    async def test_long_responses_no_anomaly(self):
        cfg = HealthConfig(empty_response_threshold=3)
        monitor = HealthMonitor(cfg, process_id="test")
        results = []
        for _ in range(5):
            results.append(
                await monitor.record_response(
                    "This is a perfectly normal, long response with lots of content."
                )
            )
        assert all(r is None for r in results)


# ---------------------------------------------------------------------------
# HealthMonitor — error rate tracking
# ---------------------------------------------------------------------------


class TestErrorRateTracking:
    @pytest.mark.asyncio
    async def test_record_error_and_success(self):
        cfg = HealthConfig()
        monitor = HealthMonitor(cfg, process_id="test")
        await monitor.record_error()
        await monitor.record_success()
        status = monitor.health_status()
        assert "is_healthy" in status

    @pytest.mark.asyncio
    async def test_high_error_rate_triggers_anomaly(self):
        cfg = HealthConfig(error_rate_threshold=0.5)
        monitor = HealthMonitor(cfg, process_id="test")
        # Record many errors to push rate above threshold
        result = None
        for _ in range(10):
            result = await monitor.record_error()
        assert result is not None
        assert result.anomaly_type == AnomalyType.HIGH_ERROR_RATE

    @pytest.mark.asyncio
    async def test_low_error_rate_no_anomaly(self):
        cfg = HealthConfig(error_rate_threshold=0.5)
        monitor = HealthMonitor(cfg, process_id="test")
        # Lots of successes, few errors → below threshold
        for _ in range(20):
            await monitor.record_success()
        result = await monitor.record_error()
        assert result is None


# ---------------------------------------------------------------------------
# HealthMonitor — cooldown
# ---------------------------------------------------------------------------


class TestCooldown:
    @pytest.mark.asyncio
    async def test_same_anomaly_suppressed_within_cooldown(self):
        cfg = HealthConfig(stuck_threshold=3, cooldown=60.0)
        monitor = HealthMonitor(cfg, process_id="test")

        # First trigger
        for _ in range(3):
            await monitor.record_tool_call("hammer", {})

        # Simulate recent anomaly
        monitor._last_anomaly_time[AnomalyType.STUCK] = time.monotonic()

        # Reset call history to trigger stuck again
        monitor._tool_calls = []
        results = []
        for _ in range(3):
            results.append(await monitor.record_tool_call("hammer", {}))

        # Should be suppressed — all None
        assert all(r is None for r in results)

    @pytest.mark.asyncio
    async def test_anomaly_fires_after_cooldown_expires(self):
        cfg = HealthConfig(stuck_threshold=3, cooldown=0.0, loop_min_repeats=100)
        monitor = HealthMonitor(cfg, process_id="test")

        # Simulate cooldown already expired
        monitor._last_anomaly_time[AnomalyType.STUCK] = time.monotonic() - 100

        results = []
        for _ in range(3):
            results.append(await monitor.record_tool_call("hammer", {}))

        # At least one should fire
        assert any(r is not None for r in results)


# ---------------------------------------------------------------------------
# HealthMonitor — health_status
# ---------------------------------------------------------------------------


class TestHealthStatus:
    def test_health_status_keys(self):
        cfg = HealthConfig()
        monitor = HealthMonitor(cfg, process_id="test")
        status = monitor.health_status()
        assert isinstance(status, dict)
        assert "is_healthy" in status
        assert "anomaly_count" in status

    @pytest.mark.asyncio
    async def test_health_status_after_anomaly(self):
        cfg = HealthConfig(stuck_threshold=2, cooldown=0.0)
        monitor = HealthMonitor(cfg, process_id="test")
        await monitor.record_tool_call("x", {})
        await monitor.record_tool_call("x", {})
        status = monitor.health_status()
        assert status["anomaly_count"] >= 1


# ---------------------------------------------------------------------------
# HealthMonitor — serialization round-trip
# ---------------------------------------------------------------------------


class TestSerialization:
    @pytest.mark.asyncio
    async def test_to_dict_from_dict_round_trip(self):
        cfg = HealthConfig(stuck_threshold=3)
        monitor = HealthMonitor(cfg, process_id="test")
        await monitor.record_tool_call("wrench", {})
        await monitor.record_tool_call("wrench", {})
        await monitor.record_success()

        data = monitor.to_dict()
        assert isinstance(data, dict)

        restored = HealthMonitor.from_dict(data, cfg)
        assert isinstance(restored, HealthMonitor)

        # Verify state survived
        original_status = monitor.health_status()
        restored_status = restored.health_status()
        assert original_status["anomaly_count"] == restored_status["anomaly_count"]

    def test_to_dict_fresh_monitor(self):
        cfg = HealthConfig()
        monitor = HealthMonitor(cfg, process_id="test")
        data = monitor.to_dict()
        assert isinstance(data, dict)

    def test_from_dict_restores_config(self):
        cfg = HealthConfig(stuck_threshold=7)
        monitor = HealthMonitor(cfg, process_id="test")
        data = monitor.to_dict()
        restored = HealthMonitor.from_dict(data, cfg)
        restored_status = restored.health_status()
        assert isinstance(restored_status, dict)
