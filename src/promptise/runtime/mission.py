"""Mission-oriented process model for the agent runtime.

Transforms agents from task-runners into mission-driven autonomous
processes with evaluation, confidence thresholds, escalation, and
automatic completion.

Supports three evaluation modes:

1. **LLM-as-judge** (default) — a separate LLM evaluates progress
   based on a :class:`MissionEvidence` bundle containing conversation,
   state, tool history, and trigger metadata.
2. **Programmatic check** — a user-defined ``success_check`` callable
   returns ``True`` when the mission succeeds.  No LLM required.
3. **Combined** — programmatic check runs first; if inconclusive the
   LLM evaluator is consulted.

Example::

    from promptise.runtime.mission import MissionTracker, MissionState
    from promptise.runtime.config import MissionConfig

    tracker = MissionTracker(
        config=MissionConfig(
            enabled=True,
            objective="Migrate all database tables",
            success_criteria="All tables pass schema validation",
        ),
        process_id="proc-1",
    )
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from promptise.types import ModelLike

    from .config import MissionConfig
    from .journal import JournalProvider

logger = logging.getLogger("promptise.runtime.mission")


class MissionState(str, Enum):
    """Mission lifecycle states."""

    ACTIVE = "active"
    COMPLETED = "completed"
    FAILED = "failed"
    PAUSED = "paused"


@dataclass
class MissionEvidence:
    """Bundle of evidence passed to the evaluator.

    Built automatically by ``AgentProcess._invoke_agent()`` from the
    conversation buffer, AgentContext state, health monitor tool
    history, and the trigger event.

    Attributes:
        conversation: Recent conversation messages.
        state: AgentContext key-value state snapshot.
        tool_calls: Recent tool call log ``[(name, args_hash)]``.
        trigger_event: The trigger event that caused this invocation.
        invocation_count: Total invocations so far.
    """

    conversation: list[dict[str, Any]] = field(default_factory=list)
    state: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[tuple[str, str]] = field(default_factory=list)
    trigger_event: dict[str, Any] = field(default_factory=dict)
    invocation_count: int = 0


@dataclass
class MissionEvaluation:
    """Result of an evaluation.

    Attributes:
        achieved: Whether the mission is complete.
        confidence: Evaluator confidence (0.0–1.0).
        reasoning: Explanation of the evaluation.
        progress_summary: Brief summary of progress so far.
        timestamp: When the evaluation was performed.
        invocation_number: Which invocation triggered this evaluation.
        source: ``"llm"`` or ``"programmatic"``.
        skipped: ``True`` when no evaluation ran (another invocation was
            evaluating at the same time, ``concurrency > 1``).  Such a
            result is not recorded; the process emits no mission event
            and doesn't escalate for it.
    """

    achieved: bool
    confidence: float
    reasoning: str
    progress_summary: str
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    invocation_number: int = 0
    source: str = "llm"
    skipped: bool = False


class MissionTracker:
    """Tracks mission progress across invocations.

    Handles evaluation scheduling, LLM-as-judge calls, programmatic
    checks, timeout/limit checking, and context prompt generation.

    ``completed`` and ``failed`` are final: a judge verdict that arrives
    after the mission was failed (for example by ``POST /mission/fail``
    while an evaluation was running) doesn't change it.  Only an
    ``active`` mission can be paused; only a ``paused`` one resumed.

    The tracker keeps the latest :attr:`MAX_EVALUATIONS` evaluations.  Its
    state round-trips through :meth:`to_dict` and :meth:`restore` (or
    :meth:`from_dict`), so a process can carry the mission across a
    restart: invocation count, elapsed time, state, failure reason and
    evaluation history.

    Args:
        config: Mission configuration.
        process_id: Owning process ID.
        journal: Optional journal provider for logging evaluations.
        success_check: Optional callable ``(MissionEvidence) -> bool | None``.
            Return ``True`` for achieved, ``False`` for not achieved,
            ``None`` for inconclusive (fall through to LLM evaluator).
    """

    #: Most evaluations kept in memory and in checkpoints (oldest dropped).
    MAX_EVALUATIONS = 100

    def __init__(
        self,
        config: MissionConfig,
        process_id: str,
        journal: JournalProvider | None = None,
        success_check: Callable[[MissionEvidence], bool | None] | None = None,
    ) -> None:
        self._config = config
        self._process_id = process_id
        self._journal = journal
        self._success_check = success_check

        self._state: MissionState = MissionState.ACTIVE
        self._evaluations: list[MissionEvaluation] = []
        self._evaluation_total: int = 0
        self._invocation_count: int = 0
        self._started_at: float = time.monotonic()
        self._failure_reason: str | None = None
        self._failed_at: datetime | None = None
        self._eval_lock = asyncio.Lock()
        self._pending_journal: set[asyncio.Task[Any]] = set()

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def state(self) -> MissionState:
        """Current mission state."""
        return self._state

    @property
    def evaluations(self) -> list[MissionEvaluation]:
        """Evaluations performed so far, oldest first (the latest :attr:`MAX_EVALUATIONS`)."""
        return list(self._evaluations)

    @property
    def evaluation_count(self) -> int:
        """Total evaluations performed (including ones no longer kept)."""
        return max(self._evaluation_total, len(self._evaluations))

    @property
    def failure_reason(self) -> str | None:
        """Why the mission failed (``None`` unless it is ``failed``)."""
        return self._failure_reason if self._state == MissionState.FAILED else None

    def evaluation_history(self, limit: int = 10) -> list[MissionEvaluation]:
        """The latest *limit* evaluations, oldest first."""
        if limit <= 0:
            return []
        return list(self._evaluations[-limit:])

    def history_summary(self, limit: int = 10) -> str:
        """:meth:`context_summary` plus the latest *limit* evaluations.

        What the ``check_mission`` meta-tool returns: one line per
        evaluation with its invocation number, source, verdict, confidence
        and reasoning (each reasoning cut at 200 characters).
        """
        lines = [self.context_summary()]
        history = self.evaluation_history(limit)
        if not history:
            lines.append("Evaluation History: none yet")
            return "\n".join(lines)
        shown = len(history)
        total = self.evaluation_count
        header = (
            f"Evaluation History (latest {shown} of {total}):"
            if total > shown
            else (f"Evaluation History ({total}):")
        )
        lines.append(header)
        for ev in history:
            verdict = "achieved" if ev.achieved else "not achieved"
            lines.append(
                f"- #{ev.invocation_number} [{ev.source}] {verdict}, "
                f"confidence={ev.confidence:.2f}: {ev.reasoning[:200]}"
            )
        return "\n".join(lines)

    @property
    def invocation_count(self) -> int:
        """Number of invocations since mission started."""
        return self._invocation_count

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def increment_invocation(self) -> None:
        """Increment the invocation counter.  Called by AgentProcess."""
        self._invocation_count += 1

    def should_evaluate(self) -> bool:
        """Check whether it's time to run an evaluation.

        Returns ``True`` if the mission is active and the invocation
        count is a multiple of ``eval_every``.
        """
        if self._state != MissionState.ACTIVE:
            return False
        if self._invocation_count == 0:
            return False
        return self._invocation_count % self._config.eval_every == 0

    async def evaluate(
        self,
        evidence: MissionEvidence,
        model: ModelLike,
    ) -> MissionEvaluation:
        """Run a mission evaluation.

        If a ``success_check`` callable is configured, it runs first.
        If it returns ``True`` → mission achieved.  ``False`` → not
        achieved.  ``None`` → fall through to LLM evaluator.

        If the LLM call fails, returns a safe default evaluation
        (``achieved=False``, ``confidence=0.5``).

        Args:
            evidence: Evidence bundle with conversation, state, tool log.
            model: Fallback model (an ID such as ``"openai:gpt-5-mini"`` or
                a chat model instance) if ``eval_model`` is not set.

        Returns:
            The evaluation result.
        """
        # Try programmatic check first
        if self._success_check is not None:
            try:
                result = self._success_check(evidence)
                if result is True:
                    evaluation = MissionEvaluation(
                        achieved=True,
                        confidence=1.0,
                        reasoning="Programmatic success check returned True",
                        progress_summary="Mission criteria met",
                        invocation_number=self._invocation_count,
                        source="programmatic",
                    )
                    self._record_evaluation(evaluation)
                    if self._state == MissionState.ACTIVE:
                        self.complete()
                    await self._log_evaluation(evaluation)
                    return evaluation
                elif result is False:
                    evaluation = MissionEvaluation(
                        achieved=False,
                        confidence=1.0,
                        reasoning="Programmatic success check returned False",
                        progress_summary="Mission criteria not yet met",
                        invocation_number=self._invocation_count,
                        source="programmatic",
                    )
                    self._record_evaluation(evaluation)
                    await self._log_evaluation(evaluation)
                    return evaluation
                # None → fall through to LLM
            except Exception as exc:
                logger.warning("Programmatic success check failed: %s", exc)

        # Avoid concurrent evaluations
        if self._eval_lock.locked():
            logger.debug("Skipping evaluation — another is in progress")
            return MissionEvaluation(
                achieved=False,
                confidence=0.5,
                reasoning="Evaluation skipped (concurrent invocation)",
                progress_summary="",
                invocation_number=self._invocation_count,
                skipped=True,
            )
        async with self._eval_lock:
            return await self._do_evaluate(evidence, model)

    def is_timed_out(self) -> bool:
        """Check whether the mission has exceeded its timeout."""
        if self._config.timeout_hours <= 0:
            return False
        elapsed_hours = (time.monotonic() - self._started_at) / 3600
        return elapsed_hours >= self._config.timeout_hours

    def is_over_limit(self) -> bool:
        """Check whether the invocation limit has been exceeded."""
        if self._config.max_invocations <= 0:
            return False
        return self._invocation_count >= self._config.max_invocations

    def fail(self, reason: str, *, source: str = "manual") -> dict[str, Any]:
        """Transition mission to FAILED state and record why.

        The reason is kept (:attr:`failure_reason`, :meth:`to_dict`, the
        ``[Mission]`` prompt block) and journalled as a ``mission_failed``
        entry.

        Args:
            reason: Why the mission failed.
            source: Who failed it: ``"manual"`` (operator / API),
                ``"timeout"``, ``"max_invocations"`` or another short tag.

        Returns:
            The payload for a ``mission.failed`` event: ``process_id``,
            ``reason`` (the *source* tag, as the runtime's own
            ``mission.failed`` events use it), ``message`` (*reason*),
            ``objective``, ``invocation_count``, ``previous_state`` and
            ``failed_at`` (ISO UTC).

        Raises:
            ValueError: The mission is already completed or failed (both
                are final; in 1.4.3 and earlier a completed mission could
                be failed afterwards).
        """
        if self._state in (MissionState.COMPLETED, MissionState.FAILED):
            raise ValueError(
                f"The mission is already {self._state.value}; a {self._state.value} "
                "mission can't be failed"
            )
        previous = self._state
        self._state = MissionState.FAILED
        self._failure_reason = reason
        self._failed_at = datetime.now(timezone.utc)
        logger.warning("Mission failed for process %s: %s", self._process_id, reason)
        data = {
            "process_id": self._process_id,
            "reason": source,
            "message": reason,
            "objective": self._config.objective,
            "invocation_count": self._invocation_count,
            "previous_state": previous.value,
            "failed_at": self._failed_at.isoformat(),
        }
        self._journal_soon("mission_failed", data)
        return data

    def pause(self) -> None:
        """Transition an ACTIVE mission to PAUSED (awaiting human input).

        Has no effect on a completed or failed mission.
        """
        if self._state != MissionState.ACTIVE:
            logger.debug(
                "Mission for process %s is %s; not pausing", self._process_id, self._state.value
            )
            return
        self._state = MissionState.PAUSED
        logger.info("Mission paused for process %s", self._process_id)

    def resume(self) -> None:
        """Transition mission from PAUSED back to ACTIVE."""
        if self._state == MissionState.PAUSED:
            self._state = MissionState.ACTIVE
            logger.info("Mission resumed for process %s", self._process_id)

    def complete(self) -> None:
        """Transition mission to COMPLETED state.

        A failed mission stays failed.
        """
        if self._state == MissionState.FAILED:
            logger.info(
                "Mission for process %s already failed; not marking it completed",
                self._process_id,
            )
            return
        self._state = MissionState.COMPLETED
        logger.info("Mission completed for process %s", self._process_id)

    # ------------------------------------------------------------------
    # Context injection
    # ------------------------------------------------------------------

    def context_summary(self) -> str:
        """Generate context for injection into the agent's system prompt."""
        lines: list[str] = [
            f"Objective: {self._config.objective}",
            f"Success Criteria: {self._config.success_criteria}",
            f"Status: {self._state.value}",
        ]
        if self.failure_reason:
            lines.append(f"Failure Reason: {self.failure_reason[:200]}")

        progress_parts: list[str] = []
        progress_parts.append(f"{self._invocation_count} invocations")
        if self._config.max_invocations > 0:
            progress_parts[-1] += f"/{self._config.max_invocations}"
        if self._config.timeout_hours > 0:
            elapsed = (time.monotonic() - self._started_at) / 3600
            progress_parts.append(f"{elapsed:.1f}/{self._config.timeout_hours:.1f} hours")
        lines.append(f"Progress: {', '.join(progress_parts)}")

        if self._evaluations:
            last = self._evaluations[-1]
            lines.append(
                f"Last Evaluation: confidence={last.confidence:.2f} — {last.reasoning[:200]}"
            )
            if last.progress_summary:
                lines.append(f"Progress Summary: {last.progress_summary[:200]}")

        return "\n".join(lines)

    # ------------------------------------------------------------------
    # Serialisation
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialise for journal checkpointing (JSON-safe).

        Restore it with :meth:`restore` (into the process's own tracker)
        or :meth:`from_dict`.
        """
        return {
            "state": self._state.value,
            "invocation_count": self._invocation_count,
            "started_at_elapsed": time.monotonic() - self._started_at,
            "failure_reason": self._failure_reason,
            "failed_at": self._failed_at.isoformat() if self._failed_at else None,
            "evaluation_count": self.evaluation_count,
            "objective": self._config.objective,
            "evaluations": [
                {
                    "achieved": e.achieved,
                    "confidence": e.confidence,
                    "reasoning": e.reasoning,
                    "progress_summary": e.progress_summary,
                    "timestamp": e.timestamp.isoformat(),
                    "invocation_number": e.invocation_number,
                    "source": e.source,
                }
                for e in self._evaluations
            ],
        }

    @classmethod
    def from_dict(
        cls,
        data: dict[str, Any],
        config: Any,
        *,
        process_id: str = "recovered",
        journal: JournalProvider | None = None,
        success_check: Callable[[MissionEvidence], bool | None] | None = None,
    ) -> MissionTracker:
        """Reconstruct from a journal checkpoint (see :meth:`restore`)."""
        tracker = cls(config, process_id=process_id, journal=journal, success_check=success_check)
        tracker.restore(data)
        return tracker

    def restore(self, data: dict[str, Any]) -> None:
        """Load a :meth:`to_dict` snapshot into this tracker.

        Restores the state, invocation count, elapsed time (the timeout
        clock continues where it stopped), failure reason and evaluation
        history.  Configuration, journal and ``success_check`` stay the
        tracker's own.  A snapshot taken for a different objective is
        refused, since its progress doesn't apply.

        Raises:
            ValueError: *data* is malformed or for another objective.
        """
        objective = data.get("objective")
        if objective is not None and objective != self._config.objective:
            raise ValueError(
                f"Mission snapshot is for objective {objective!r}, not "
                f"{self._config.objective!r}; not restoring it"
            )
        try:
            state = MissionState(data.get("state", "active"))
            invocation_count = int(data.get("invocation_count", 0))
            elapsed = float(data.get("started_at_elapsed", 0.0))
            evaluations = [
                MissionEvaluation(
                    achieved=bool(e_data.get("achieved", False)),
                    confidence=float(e_data.get("confidence", 0.5)),
                    reasoning=str(e_data.get("reasoning", "")),
                    progress_summary=str(e_data.get("progress_summary", "")),
                    timestamp=datetime.fromisoformat(
                        e_data.get("timestamp") or datetime.now(timezone.utc).isoformat()
                    ),
                    invocation_number=int(e_data.get("invocation_number", 0)),
                    source=str(e_data.get("source", "llm")),
                )
                for e_data in data.get("evaluations", [])
            ]
            failed_at = data.get("failed_at")
            failed_at_dt = datetime.fromisoformat(failed_at) if failed_at else None
        except (TypeError, ValueError, AttributeError) as exc:
            raise ValueError(f"Malformed mission snapshot: {exc}") from exc

        self._state = state
        self._invocation_count = invocation_count
        self._started_at = time.monotonic() - max(0.0, elapsed)
        self._evaluations = evaluations[-self.MAX_EVALUATIONS :]
        self._evaluation_total = max(int(data.get("evaluation_count", 0) or 0), len(evaluations))
        self._failure_reason = data.get("failure_reason")
        self._failed_at = failed_at_dt

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    async def _do_evaluate(
        self,
        evidence: MissionEvidence,
        model: ModelLike,
    ) -> MissionEvaluation:
        """Perform the actual evaluation LLM call."""
        eval_model: ModelLike = self._config.eval_model or model
        if not eval_model:
            eval_model = "openai:gpt-5-mini"

        system_prompt = (
            "You evaluate whether an AI agent has completed its mission.\n"
            "Respond with ONLY valid JSON (no markdown, no code fences):\n"
            '{"achieved": true/false, "confidence": 0.0-1.0, '
            '"reasoning": "...", "progress_summary": "..."}\n\n'
            f"Objective: {self._config.objective}\n"
            f"Success Criteria: {self._config.success_criteria}"
        )

        # Build evidence text
        parts: list[str] = []

        if evidence.state:
            state_str = json.dumps(evidence.state, default=str)[:1000]
            parts.append(f"[Agent State]\n{state_str}")

        if evidence.tool_calls:
            recent_tools = evidence.tool_calls[-20:]
            tool_lines = [f"  {name}({h})" for name, h in recent_tools]
            parts.append("[Recent Tool Calls]\n" + "\n".join(tool_lines))

        if evidence.trigger_event:
            parts.append(f"[Trigger] {json.dumps(evidence.trigger_event, default=str)[:300]}")

        if evidence.conversation:
            recent = evidence.conversation[-10:]
            conv_lines = [
                f"[{msg.get('role', 'unknown')}]: {str(msg.get('content', ''))[:500]}"
                for msg in recent
            ]
            parts.append("[Conversation]\n" + "\n".join(conv_lines))

        evidence_text = "\n\n".join(parts) if parts else "(no evidence available)"

        try:
            from promptise import build_agent

            # The judge reads the evidence only: no MCP servers, no tools.
            eval_agent = await build_agent(
                servers={},
                model=eval_model,
                instructions=system_prompt,
            )
            try:
                result = await eval_agent.ainvoke(
                    {"messages": [{"role": "user", "content": evidence_text}]}
                )
            finally:
                with contextlib.suppress(Exception):
                    await eval_agent.shutdown()

            text = ""
            if isinstance(result, dict):
                messages = result.get("messages", [])
                if messages:
                    last_msg = messages[-1]
                    text = last_msg.content if hasattr(last_msg, "content") else str(last_msg)
            elif isinstance(result, str):
                text = result

            text = text.strip()
            if text.startswith("```"):
                text = text.split("\n", 1)[-1]
            if text.endswith("```"):
                text = text.rsplit("```", 1)[0]
            text = text.strip()

            parsed = json.loads(text)
            confidence = float(parsed.get("confidence", 0.5))
            if not 0.0 <= confidence <= 1.0:
                logger.debug(
                    "Clamping out-of-range confidence %.2f to [0, 1]",
                    confidence,
                )
                confidence = max(0.0, min(1.0, confidence))

            evaluation = MissionEvaluation(
                achieved=bool(parsed.get("achieved", False)),
                confidence=confidence,
                reasoning=str(parsed.get("reasoning", "")),
                progress_summary=str(parsed.get("progress_summary", "")),
                invocation_number=self._invocation_count,
                source="llm",
            )

        except Exception as exc:
            logger.warning("Mission evaluation failed: %s", exc)
            evaluation = MissionEvaluation(
                achieved=False,
                confidence=0.5,
                reasoning=f"Evaluation failed: {exc}",
                progress_summary="",
                invocation_number=self._invocation_count,
                source="llm",
            )

        self._record_evaluation(evaluation)

        # A verdict that arrives after the mission was failed, completed or
        # paused (e.g. over the API during the judge call) doesn't change it.
        if self._state == MissionState.ACTIVE:
            if evaluation.achieved:
                self.complete()
            elif evaluation.confidence < self._config.confidence_threshold:
                self.pause()

        await self._log_evaluation(evaluation)
        return evaluation

    def _record_evaluation(self, evaluation: MissionEvaluation) -> None:
        self._evaluation_total = max(self._evaluation_total, len(self._evaluations)) + 1
        self._evaluations.append(evaluation)
        del self._evaluations[: -self.MAX_EVALUATIONS]

    def _journal_soon(self, entry_type: str, data: dict[str, Any]) -> None:
        """Append a journal entry from sync code (best effort)."""
        if self._journal is None:
            return
        try:
            from .journal import JournalEntry

            entry = JournalEntry(
                process_id=self._process_id,
                timestamp=datetime.now(timezone.utc),
                entry_type=entry_type,
                data=dict(data),
            )
            loop = asyncio.get_running_loop()
            task = loop.create_task(self._journal.append(entry))
            self._pending_journal.add(task)
            task.add_done_callback(self._pending_journal.discard)
        except RuntimeError:
            pass  # No event loop — skip
        except Exception:
            logger.debug("Mission journal entry failed", exc_info=True)

    async def _log_evaluation(self, evaluation: MissionEvaluation) -> None:
        """Record evaluation in the journal."""
        if self._journal is None:
            return
        try:
            from .journal import JournalEntry

            entry = JournalEntry(
                process_id=self._process_id,
                timestamp=datetime.now(timezone.utc),
                entry_type="mission_evaluation",
                data={
                    "achieved": evaluation.achieved,
                    "confidence": evaluation.confidence,
                    "reasoning": evaluation.reasoning[:500],
                    "progress_summary": evaluation.progress_summary[:200],
                    "mission_state": self._state.value,
                    "invocation_number": evaluation.invocation_number,
                    "source": evaluation.source,
                },
            )
            await self._journal.append(entry)
        except Exception:
            # The journal is a record, not part of the evaluation: keep going.
            logger.debug("Mission: journaling the evaluation failed", exc_info=True)
