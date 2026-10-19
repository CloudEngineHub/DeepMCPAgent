"""Behavioral health monitoring for the agent runtime.

Lightweight anomaly detection that identifies stuck agents, infinite
loops, empty responses, and high error rates — without making any
LLM calls.  All detection is pure pattern matching on tool call and
response history.

Example::

    from promptise.runtime.health import HealthMonitor
    from promptise.runtime.config import HealthConfig

    monitor = HealthMonitor(HealthConfig(enabled=True), process_id="p1")
    anomaly = await monitor.record_tool_call("search", {"q": "test"})
"""

from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from .config import HealthConfig

logger = logging.getLogger("promptise.runtime.health")


class AnomalyType(str, Enum):
    """Types of behavioral anomaly."""

    STUCK = "stuck"
    LOOP = "loop"
    EMPTY_RESPONSE = "empty_response"
    HIGH_ERROR_RATE = "high_error_rate"


@dataclass
class Anomaly:
    """A detected behavioral anomaly.

    Attributes:
        anomaly_type: Kind of anomaly.
        description: Human-readable explanation.
        timestamp: When the anomaly was detected.
        details: Additional evidence (tool sequences, counts, etc.).
    """

    anomaly_type: AnomalyType
    description: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    details: dict[str, Any] = field(default_factory=dict)


class HealthMonitor:
    """Lightweight behavioral health analysis.

    All detection methods are pure logic — no I/O, no LLM calls.
    The same anomaly is reported at most once per ``cooldown`` seconds, to
    avoid spam.  "The same" means the same kind *and* the same evidence:
    the same repeated call (stuck), the same repeating sequence (loop), or
    the same signal (empty responses, error rate).  A different stuck call
    or loop is a new anomaly and is reported at once.

    To use the monitor as an in-run guard that acts on *every* repeat (a
    ``pre_tool`` hook that blocks the call while the agent is stuck), set
    ``cooldown=0``: :meth:`record_tool_call` then returns the anomaly on
    each call that continues it.

    Window sizes follow the config: when ``error_window``, ``loop_window``,
    ``stuck_threshold`` or ``empty_threshold`` change on the config object
    the monitor reads (as ``PATCH /health`` does), the windows are resized
    on the next recorded call, keeping the most recent data.  Call
    :meth:`apply_config` to switch to a different :class:`HealthConfig`
    object.

    The anomaly history keeps the latest :attr:`MAX_ANOMALIES` anomalies;
    the per-type counts in :meth:`health_status` count all of them.

    Args:
        config: Health monitoring configuration.
        process_id: Owning process ID.
    """

    #: Most anomalies kept in :attr:`anomalies` (oldest dropped first).
    MAX_ANOMALIES = 1000

    def __init__(self, config: HealthConfig, process_id: str) -> None:
        self._config = config
        self._process_id = process_id

        self._tool_history: deque[tuple[str, str]] = deque()
        self._response_lengths: deque[int] = deque()
        self._error_window: deque[bool] = deque()
        self._window_sizes: tuple[int, int] = (0, 0)
        self._sync_windows()
        # Last report time per anomaly key (see _cooldown_key).
        self._last_anomaly_time: dict[tuple[Any, ...], float] = {}
        self._anomalies: deque[Anomaly] = deque(maxlen=self.MAX_ANOMALIES)
        self._anomaly_count: dict[AnomalyType, int] = dict.fromkeys(AnomalyType, 0)
        # Set when an anomaly fires; cleared by the next clean success.
        self._unrecovered = False
        # Anomalies not yet handed to the owner (see take_new_anomalies).
        self._new_anomalies: list[Anomaly] = []

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def config(self) -> HealthConfig:
        """The configuration the monitor reads."""
        return self._config

    def apply_config(self, config: HealthConfig) -> None:
        """Switch to *config*, resizing the windows and keeping the data.

        The most recent tool calls, reply lengths and error flags are
        kept (as many as fit the new windows), as are the anomaly history
        and the cooldowns.  The new thresholds apply from the next
        recorded call.
        """
        self._config = config
        self._sync_windows()

    def clear_anomalies(self) -> int:
        """Forget detected anomalies and reset their cooldowns.

        After a clear, the next occurrence of any anomaly is reported at
        once (not only after ``cooldown``), counts start from zero and the
        monitor reports healthy.  The tool-call, reply-length and error
        windows are kept: they are evidence, not reports.

        Returns:
            The number of anomalies that were in the history.
        """
        cleared = len(self._anomalies)
        self._anomalies.clear()
        self._new_anomalies.clear()
        self._last_anomaly_time.clear()
        self._anomaly_count = dict.fromkeys(AnomalyType, 0)
        self._unrecovered = False
        return cleared

    @property
    def anomalies(self) -> list[Anomaly]:
        """Detected anomalies, oldest first (the latest :attr:`MAX_ANOMALIES`)."""
        return list(self._anomalies)

    @property
    def latest_anomaly(self) -> Anomaly | None:
        """Most recent anomaly, or ``None``."""
        return self._anomalies[-1] if self._anomalies else None

    def take_new_anomalies(self) -> list[Anomaly]:
        """Return the anomalies detected since the previous call.

        Each anomaly is returned once.  ``AgentProcess`` calls this when a
        run ends (successfully or not) to apply ``on_anomaly`` to what the
        run produced, however long ago in the run it was detected.
        """
        new, self._new_anomalies = self._new_anomalies, []
        return new

    @property
    def tool_history(self) -> list[tuple[str, str]]:
        """Recent tool call history ``[(name, args_hash)]``."""
        return list(self._tool_history)

    # ------------------------------------------------------------------
    # Recording API
    # ------------------------------------------------------------------

    def begin_invocation(self) -> None:
        """Start a new invocation: forget the previous run's tool calls.

        Stuck and loop detection look for repetition *within* a run.  A
        scheduled agent that calls the same tool once per run (e.g. a
        queue check every minute) is behaving normally, so tool history
        does not carry over between invocations.  Response lengths and
        the error window do carry over: "three empty replies in a row"
        and "half the recent runs failed" are cross-run signals.
        """
        self._sync_windows()
        self._tool_history.clear()

    async def record_tool_call(self, tool_name: str, args: dict[str, Any]) -> Anomaly | None:
        """Record a tool call and check for stuck/loop anomalies."""
        self._sync_windows()
        args_hash = self._hash_args(args)
        self._tool_history.append((tool_name, args_hash))

        # Run detectors
        anomaly = self._detect_stuck() or self._detect_loop()
        if anomaly and self._is_cooled_down(anomaly):
            self._register_anomaly(anomaly)
            return anomaly
        return None

    async def record_response(self, content: str) -> Anomaly | None:
        """Record the agent's final reply and check for empty responses.

        Pass the reply the agent gave at the end of an invocation, not
        tool results: a tool returning ``[]`` is not an empty response.
        A reply of at most ``empty_max_chars`` characters (after
        stripping whitespace) counts as empty.
        """
        self._sync_windows()
        self._response_lengths.append(len(content.strip()))
        anomaly = self._detect_empty()
        if anomaly and self._is_cooled_down(anomaly):
            self._register_anomaly(anomaly)
            return anomaly
        return None

    async def record_success(self) -> bool:
        """Record a successful invocation.

        Returns:
            ``True`` if this success clears a previous anomaly (recovery).
        """
        self._sync_windows()
        self._error_window.append(False)
        # Recovery: the first clean success after an anomaly (once, so
        # process.py emits a single health.recovered per incident).
        if self._unrecovered:
            self._unrecovered = False
            return True
        return False

    async def record_error(self) -> Anomaly | None:
        """Record a failed invocation and check error rate."""
        self._sync_windows()
        self._error_window.append(True)
        anomaly = self._detect_error_rate()
        if anomaly and self._is_cooled_down(anomaly):
            self._register_anomaly(anomaly)
            return anomaly
        return None

    # ------------------------------------------------------------------
    # Detectors
    # ------------------------------------------------------------------

    def _detect_stuck(self) -> Anomaly | None:
        """Detect: last N tool calls are identical (same name + same args)."""
        threshold = self._config.stuck_threshold
        if len(self._tool_history) < threshold:
            return None

        last_n = list(self._tool_history)[-threshold:]
        if len(set(last_n)) == 1:
            tool_name, args_hash = last_n[0]
            return Anomaly(
                anomaly_type=AnomalyType.STUCK,
                description=(
                    f"Agent stuck: tool '{tool_name}' called {threshold} times "
                    f"consecutively with identical arguments"
                ),
                details={
                    "tool_name": tool_name,
                    "consecutive_count": threshold,
                },
            )
        return None

    def _detect_loop(self) -> Anomaly | None:
        """Detect: repeating subsequence in tool call history."""
        window = self._config.loop_window
        min_repeats = self._config.loop_min_repeats
        history = list(self._tool_history)[-window:]

        if len(history) < 4:
            return None

        # Check for patterns of length 2 up to half the window
        for pattern_len in range(2, len(history) // min_repeats + 1):
            pattern = history[-pattern_len:]
            repeat_count = 0
            idx = len(history) - pattern_len

            while idx >= pattern_len:
                candidate = history[idx - pattern_len : idx]
                if candidate == pattern:
                    repeat_count += 1
                    idx -= pattern_len
                else:
                    break

            if (
                repeat_count >= min_repeats - 1
            ):  # -1 because the last occurrence is the pattern itself
                tool_names = [t[0] for t in pattern]
                return Anomaly(
                    anomaly_type=AnomalyType.LOOP,
                    description=(
                        f"Agent in loop: sequence {tool_names} repeating {repeat_count + 1} times"
                    ),
                    details={
                        "pattern": tool_names,
                        "repeat_count": repeat_count + 1,
                        "pattern_length": pattern_len,
                    },
                )
        return None

    def _detect_empty(self) -> Anomaly | None:
        """Detect: N consecutive replies of at most ``empty_max_chars`` characters."""
        threshold = self._config.empty_threshold
        max_chars = self._config.empty_max_chars

        if len(self._response_lengths) < threshold:
            return None

        last_n = list(self._response_lengths)[-threshold:]
        if all(length <= max_chars for length in last_n):
            return Anomaly(
                anomaly_type=AnomalyType.EMPTY_RESPONSE,
                description=(
                    f"Agent producing empty responses: {threshold} consecutive "
                    f"replies of at most {max_chars} characters"
                ),
                details={
                    "consecutive_count": threshold,
                    "max_chars": max_chars,
                    "lengths": last_n,
                },
            )
        return None

    def _detect_error_rate(self) -> Anomaly | None:
        """Detect: error rate above threshold in the sliding window."""
        if len(self._error_window) < self._config.error_window:
            return None

        error_count = sum(1 for e in self._error_window if e)
        rate = error_count / len(self._error_window)

        if rate >= self._config.error_rate_threshold:
            return Anomaly(
                anomaly_type=AnomalyType.HIGH_ERROR_RATE,
                description=(
                    f"High error rate: {rate:.0%} errors in last "
                    f"{len(self._error_window)} invocations "
                    f"(threshold: {self._config.error_rate_threshold:.0%})"
                ),
                details={
                    "error_rate": round(rate, 3),
                    "error_count": error_count,
                    "window_size": len(self._error_window),
                    "threshold": self._config.error_rate_threshold,
                },
            )
        return None

    # ------------------------------------------------------------------
    # Status & serialisation
    # ------------------------------------------------------------------

    def health_status(self) -> dict[str, Any]:
        """Health summary (public API; JSON-safe).

        Returns:
            ``is_healthy`` (no anomaly reported within the last
            ``cooldown`` seconds), ``anomaly_count`` (total reported since
            the start or the last :meth:`clear_anomalies`),
            ``anomalies_by_type`` and ``latest_anomaly`` (type, description,
            ISO timestamp) or ``None``.
        """
        last = max(self._last_anomaly_time.values(), default=None)
        return {
            "is_healthy": last is None or (time.monotonic() - last) > self._config.cooldown,
            "anomaly_count": sum(self._anomaly_count.values()),
            "anomalies_by_type": {t.value: n for t, n in self._anomaly_count.items()},
            "latest_anomaly": (
                {
                    "type": self._anomalies[-1].anomaly_type.value,
                    "description": self._anomalies[-1].description,
                    "timestamp": self._anomalies[-1].timestamp.isoformat(),
                }
                if self._anomalies
                else None
            ),
        }

    def to_dict(self) -> dict[str, Any]:
        """Serialise for journal checkpointing."""
        return {
            "tool_history": list(self._tool_history),
            "response_lengths": list(self._response_lengths),
            "error_window": list(self._error_window),
            "anomaly_count": {t.value: n for t, n in self._anomaly_count.items()},
        }

    @classmethod
    def from_dict(
        cls, data: dict[str, Any], config: Any, *, process_id: str = "recovered"
    ) -> HealthMonitor:
        """Reconstruct from a journal checkpoint."""
        monitor = cls(config, process_id=process_id)
        for item in data.get("tool_history", []):
            if isinstance(item, (list, tuple)) and len(item) == 2:
                monitor._tool_history.append((item[0], item[1]))
        for length in data.get("response_lengths", []):
            monitor._response_lengths.append(length)
        for err in data.get("error_window", []):
            monitor._error_window.append(bool(err))
        counts = data.get("anomaly_count", {})
        for t in AnomalyType:
            # Keys are type values; older checkpoints may hold "AnomalyType.X".
            monitor._anomaly_count[t] = int(counts.get(t.value, counts.get(str(t), 0)) or 0)
        return monitor

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _sync_windows(self) -> None:
        """Resize the windows when the config's sizes changed, keeping data."""
        cfg = self._config
        history = max(
            cfg.loop_window,
            cfg.stuck_threshold * 2,
            cfg.empty_threshold * 2,
            cfg.error_window * 2,
        )
        sizes = (history, cfg.error_window)
        if sizes == self._window_sizes:
            return
        self._tool_history = deque(self._tool_history, maxlen=history)
        self._response_lengths = deque(self._response_lengths, maxlen=history)
        self._error_window = deque(self._error_window, maxlen=cfg.error_window)
        self._window_sizes = sizes

    def _hash_args(self, args: dict[str, Any]) -> str:
        """Stable hash of tool arguments for comparison."""
        try:
            raw = json.dumps(args, sort_keys=True, default=str)
        except (TypeError, ValueError):
            raw = str(args)
        # MD5 used as a non-cryptographic fingerprint for anomaly detection
        # (detecting identical repeated tool calls). Not security-sensitive.
        return hashlib.md5(raw.encode(), usedforsecurity=False).hexdigest()[:12]

    def _cooldown_key(self, anomaly: Anomaly) -> tuple[Any, ...]:
        """What makes two anomalies "the same" for the cooldown.

        Stuck: the repeated call (tool and arguments).  Loop: the repeating
        sequence, whichever call it is caught at (``A, B`` and ``B, A`` are
        one loop).  Empty responses and error rate: the kind alone.  Before
        1.3.4 the key was the kind alone, so a stuck call silenced every
        other stuck call for ``cooldown`` seconds (300 by default), in the
        same run and in the next ones.
        """
        if anomaly.anomaly_type is AnomalyType.STUCK and self._tool_history:
            return (anomaly.anomaly_type, self._tool_history[-1])
        if anomaly.anomaly_type is AnomalyType.LOOP and self._tool_history:
            length = int(anomaly.details.get("pattern_length", 0))
            pattern = list(self._tool_history)[-length:] if length else []
            if pattern:
                rotations = [tuple(pattern[i:] + pattern[:i]) for i in range(len(pattern))]
                return (anomaly.anomaly_type, min(rotations))
        return (anomaly.anomaly_type,)

    def _is_cooled_down(self, anomaly: Anomaly) -> bool:
        """Whether the same anomaly was last reported ``cooldown`` seconds ago or more."""
        last_time = self._last_anomaly_time.get(self._cooldown_key(anomaly))
        if last_time is None:
            return True
        return (time.monotonic() - last_time) >= self._config.cooldown

    def _register_anomaly(self, anomaly: Anomaly) -> None:
        """Record an anomaly and update cooldown tracking."""
        self._anomalies.append(anomaly)
        self._new_anomalies.append(anomaly)
        self._anomaly_count[anomaly.anomaly_type] += 1
        self._last_anomaly_time[self._cooldown_key(anomaly)] = time.monotonic()
        self._unrecovered = True
        logger.warning(
            "Behavioral anomaly detected: %s — %s",
            anomaly.anomaly_type.value,
            anomaly.description,
        )
