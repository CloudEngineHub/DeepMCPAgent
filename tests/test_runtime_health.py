"""Tests for promptise.runtime.health — HealthMonitor, AnomalyType, Anomaly."""

from __future__ import annotations

import asyncio
from datetime import timedelta
from typing import Any

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
    """``cooldown`` silences the *same* anomaly, not every anomaly of its kind."""

    @staticmethod
    async def _calls(monitor: HealthMonitor, *calls: tuple[str, dict[str, Any]]) -> list[Any]:
        return [await monitor.record_tool_call(name, args) for name, args in calls]

    @pytest.mark.asyncio
    async def test_same_stuck_call_reported_once_within_cooldown(self):
        monitor = HealthMonitor(
            HealthConfig(stuck_threshold=3, cooldown=60.0, loop_min_repeats=100), process_id="t"
        )
        results = await self._calls(monitor, *[("hammer", {})] * 8)
        assert [r is not None for r in results] == [False, False, True] + [False] * 5
        assert len(monitor.anomalies) == 1

    @pytest.mark.asyncio
    async def test_a_different_stuck_call_is_a_new_anomaly(self):
        """Before 1.3.4 the first stuck call silenced every other one for 300 s."""
        monitor = HealthMonitor(HealthConfig(stuck_threshold=3, loop_min_repeats=100), "t")
        await self._calls(monitor, *[("get_stock", {"sku": "A"})] * 3)
        results = await self._calls(monitor, *[("get_stock", {"sku": "B"})] * 3)
        assert results[-1] is not None
        assert results[-1].anomaly_type == AnomalyType.STUCK
        assert len(monitor.anomalies) == 2
        assert len(monitor.take_new_anomalies()) == 2

    @pytest.mark.asyncio
    async def test_same_call_stuck_again_in_the_next_run_stays_silenced(self):
        monitor = HealthMonitor(HealthConfig(stuck_threshold=3, loop_min_repeats=100), "t")
        await self._calls(monitor, *[("poll", {"id": 1})] * 3)
        monitor.begin_invocation()
        results = await self._calls(monitor, *[("poll", {"id": 1})] * 3)
        assert results == [None, None, None]
        monitor.begin_invocation()
        results = await self._calls(monitor, *[("poll", {"id": 2})] * 3)
        assert results[-1] is not None

    @pytest.mark.asyncio
    async def test_a_loop_is_one_anomaly_whichever_call_it_is_caught_at(self):
        monitor = HealthMonitor(HealthConfig(loop_window=20, loop_min_repeats=2), "t")
        a, b = ("search", {"q": "x"}), ("read", {"id": 1})
        await self._calls(monitor, *[a, b] * 6)
        assert [x.anomaly_type for x in monitor.anomalies] == [AnomalyType.LOOP]
        # A different loop is reported.
        c, d = ("list", {}), ("open", {"n": 2})
        await self._calls(monitor, *[c, d] * 3)
        assert len(monitor.anomalies) == 2

    @pytest.mark.asyncio
    async def test_cooldown_zero_reports_every_repeat_for_an_in_run_guard(self):
        monitor = HealthMonitor(
            HealthConfig(stuck_threshold=3, cooldown=0, loop_min_repeats=100), process_id="t"
        )
        results = await self._calls(monitor, *[("hammer", {})] * 6)
        assert [r is not None for r in results] == [False, False, True, True, True, True]

    @pytest.mark.asyncio
    async def test_anomaly_fires_again_after_cooldown_expires(self):
        monitor = HealthMonitor(
            HealthConfig(stuck_threshold=3, cooldown=60.0, loop_min_repeats=100), process_id="t"
        )
        await self._calls(monitor, *[("hammer", {})] * 3)
        for key in monitor._last_anomaly_time:
            monitor._last_anomaly_time[key] -= 61
        assert await monitor.record_tool_call("hammer", {}) is not None


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


# ---------------------------------------------------------------------------
# on_anomaly in AgentProcess: every anomaly of a run is acted on, once
# ---------------------------------------------------------------------------


class _StuckAgent:
    """Records ``stuck_threshold`` identical tool calls, like a stuck model.

    ``age`` back-dates the anomaly (the run went on that long after it was
    detected); ``fail`` makes the run raise afterwards, as a stuck loop
    does when it hits its iteration limit.
    """

    def __init__(self, process: Any, *, age: float = 0.0, fail: bool = False) -> None:
        self.process = process
        self.age = age
        self.fail = fail
        self.stuck = True
        self.calls = 0

    async def ainvoke(self, input: Any, config: Any = None) -> dict[str, Any]:
        self.calls += 1
        health = self.process._health
        if self.stuck:
            for _ in range(3):
                await health.record_tool_call("list_overdue", {})
            if self.age and health.latest_anomaly is not None:
                health.latest_anomaly.timestamp -= timedelta(seconds=self.age)
        if self.fail:
            raise RuntimeError("Graph iteration limit reached")
        return {"messages": [{"role": "assistant", "content": "Billing API unavailable."}]}

    async def shutdown(self) -> None:
        pass


async def _health_process(
    on_anomaly: str, **agent_kwargs: Any
) -> tuple[Any, _StuckAgent, list[Any]]:
    from promptise import CallbackSink, EventNotifier
    from promptise.runtime.config import ProcessConfig
    from promptise.runtime.process import AgentProcess

    seen: list[Any] = []
    process = AgentProcess(
        "h",
        ProcessConfig(health=HealthConfig(enabled=True, on_anomaly=on_anomaly, cooldown=0)),
        event_notifier=EventNotifier(sinks=[CallbackSink(seen.append)]),
    )
    agent = _StuckAgent(process, **agent_kwargs)

    async def build() -> None:
        process._agent = agent

    process._build_agent = build  # type: ignore[method-assign]
    await process.start()
    return process, agent, seen


async def _run_once(process: Any, agent: _StuckAgent) -> None:
    from promptise.runtime.triggers.base import TriggerEvent

    before = agent.calls
    await process.inject(TriggerEvent(trigger_id="t", trigger_type="manual", payload={}))
    for _ in range(200):
        if agent.calls > before:
            break
        await asyncio.sleep(0.01)
    await asyncio.sleep(0.2)  # let the post-run handling finish


class TestOnAnomalyInProcess:
    async def test_pause_applies_however_long_the_run_went_on(self) -> None:
        from promptise.runtime.lifecycle import ProcessState

        # The run kept working for a minute after the third identical call.
        process, agent, seen = await _health_process("pause", age=60)
        try:
            await _run_once(process, agent)
            assert process.state == ProcessState.SUSPENDED
        finally:
            await process.stop()
        anomalies = [e for e in seen if e.event_type == "health.anomaly"]
        assert [e.data["anomaly_type"] for e in anomalies] == ["stuck"]

    async def test_pause_applies_when_the_run_raises(self) -> None:
        from promptise.runtime.lifecycle import ProcessState

        process, agent, seen = await _health_process("pause", fail=True)
        try:
            await _run_once(process, agent)
            assert process.state == ProcessState.SUSPENDED
        finally:
            await process.stop()
        assert [e.event_type for e in seen].count("health.anomaly") == 1

    async def test_an_anomaly_is_acted_on_once(self) -> None:
        from promptise.runtime.lifecycle import ProcessState

        process, agent, seen = await _health_process("pause")
        try:
            await _run_once(process, agent)
            assert process.state == ProcessState.SUSPENDED
            await process.resume()
            agent.stuck = False  # the next run is healthy
            await _run_once(process, agent)
            # The earlier anomaly does not pause the process again.
            assert process.state == ProcessState.RUNNING
        finally:
            await process.stop()
        assert [e.event_type for e in seen].count("health.anomaly") == 1

    async def test_take_new_anomalies_returns_each_once(self) -> None:
        monitor = HealthMonitor(HealthConfig(enabled=True, cooldown=0), process_id="p")
        for _ in range(3):
            await monitor.record_tool_call("search", {"q": "x"})
        first = monitor.take_new_anomalies()
        assert [a.anomaly_type for a in first] == [AnomalyType.STUCK]
        assert monitor.take_new_anomalies() == []
        assert len(monitor.anomalies) == 1


# ---------------------------------------------------------------------------
# The docs' in-run guard (docs/runtime/governance/health.md)
# ---------------------------------------------------------------------------


class _AlwaysPoll:
    """A scripted chat model that polls the same job until it has 6 results."""

    @staticmethod
    def make() -> Any:
        from langchain_core.language_models.chat_models import BaseChatModel
        from langchain_core.messages import AIMessage, ToolMessage
        from langchain_core.outputs import ChatGeneration, ChatResult

        class Model(BaseChatModel):
            @property
            def _llm_type(self) -> str:
                return "always-poll"

            def bind_tools(self, tools: Any, **kwargs: Any) -> Model:  # type: ignore[override]
                return self

            def _generate(self, messages: Any, stop: Any = None, **kw: Any) -> ChatResult:
                raise NotImplementedError

            async def _agenerate(
                self, messages: Any, stop: Any = None, run_manager: Any = None, **kw: Any
            ) -> ChatResult:
                done = sum(isinstance(m, ToolMessage) for m in messages)
                if done >= 6:
                    message = AIMessage(content="gave up")
                else:
                    call = {"name": "poll", "args": {"job": "J1"}, "id": f"c{done}"}
                    message = AIMessage(content="", tool_calls=[call])
                return ChatResult(generations=[ChatGeneration(message=message)])

        return Model()


@pytest.mark.asyncio
async def test_docs_in_run_guard_blocks_every_repeat():
    import re
    from pathlib import Path

    from langchain_core.messages import HumanMessage, ToolMessage
    from langchain_core.tools import tool

    from promptise.engine import PromptGraph, PromptGraphEngine, PromptNode

    page = Path(__file__).resolve().parent.parent / "docs/runtime/governance/health.md"
    section = page.read_text(encoding="utf-8").split("## As an in-run guard", 1)[1]
    code = re.search(r"```python\n(.*?)```", section, re.DOTALL).group(1)
    ns: dict[str, Any] = {}
    exec(compile(code, "<health.md>", "exec"), ns)

    runs: list[str] = []

    @tool
    def poll(job: str) -> str:
        """Poll a job."""
        runs.append(job)
        return "processing"

    graph = PromptGraph(name="g")
    graph.add_node(PromptNode("work", tools=[poll], max_iterations=10, default_next="__end__"))
    graph.set_entry("work")
    engine = PromptGraphEngine(graph=graph, model=_AlwaysPoll.make(), hooks=[ns["LoopGuard"]()])
    result = await engine.ainvoke({"messages": [HumanMessage(content="go")]})

    results = [m.content for m in result["messages"] if isinstance(m, ToolMessage)]
    assert len(results) == 6
    # Calls 3-6 continue the stuck anomaly: each is blocked, none runs.
    assert len(runs) == 2
    assert all("stuck" in r and "stop calling it" in r for r in results[2:])
