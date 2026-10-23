"""Core observability: timeline collector, event types, and entry model.

This module provides the :class:`ObservabilityCollector` and related types
for recording and querying structured timeline events from agents, tools,
prompts, and the runtime.

Example::

    from promptise.observability import ObservabilityCollector, TimelineEventType

    collector = ObservabilityCollector("my-session")
    collector.add_transporter(ConsoleTransporter())

    entry = collector.record(
        TimelineEventType.TOOL_CALL,
        agent_id="my-agent",
        details="Calling search_files",
        metadata={"tool_name": "search_files"},
    )
"""

from __future__ import annotations

import contextvars
import json
import logging
import threading
import time
import uuid
from collections import deque
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

from .events import _PII_PATTERNS, _is_secret_key

logger = logging.getLogger(__name__)

#: Carries the identity of a *delegating* agent across a cross-agent call so
#: the peer's timeline entries are stamped with ``delegated_by``. Set by the
#: cross-agent tools around a peer invocation; read by
#: :meth:`ObservabilityCollector.record`. Distinct from the caller contextvar
#: (which a peer's ``ainvoke`` resets), so it survives into the peer's run.
_delegation_ctx_var: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "promptise_delegation", default=None
)


def get_current_delegation() -> dict[str, Any] | None:
    """Return the delegating agent's identity claims, if a cross-agent call
    is in progress; otherwise ``None``."""
    return _delegation_ctx_var.get()


@dataclass
class AgentRun:
    """One observed agent invocation, from ``agent.input`` to ``agent.output``.

    :class:`~promptise.agent.PromptiseAgent` opens a run for every
    invocation it observes and makes it current for the duration of the
    call.  While a run is current, :meth:`ObservabilityCollector.record`
    sets each event's ``parent_id`` to :attr:`entry_id` (unless one is
    passed explicitly), which is what links LLM turns, tool calls and
    cache events to the invocation that caused them — the OpenTelemetry
    transporter builds its span tree from it.  The callback handler also
    adds the run's token usage and call counts here, so ``agent.output``
    can report per-invocation totals.

    Attributes:
        entry_id: ``entry_id`` of the run's ``agent.input`` event.
        started: Epoch seconds when the run started.
        prompt_tokens: Input tokens used by this run's LLM calls.
        completion_tokens: Output tokens used by this run's LLM calls.
        llm_calls: LLM calls made during this run.
        tool_calls: Tool calls made during this run.
        errors: LLM and tool errors during this run.
        cache_hit: Whether the run was answered from the semantic cache.
        collector: The collector the run is recorded on, so code running
            inside the run (the autonomy budget, approval gates) can add
            entries to it.  ``None`` for a run created by hand.
        agent_id: The ``agent_id`` the run's entries carry.
        record_tool_io: Whether tool arguments may be recorded (the
            handler's ``record_tool_io``).
        denied_tool_runs: Tool runs (LangChain run ids) an approval gate
            denied, with the denial's fields, until their ``tool.result``
            is recorded.
    """

    entry_id: str
    started: float
    prompt_tokens: int = 0
    completion_tokens: int = 0
    llm_calls: int = 0
    tool_calls: int = 0
    errors: int = 0
    cache_hit: bool = False
    collector: Any = field(default=None, repr=False, compare=False)
    agent_id: str | None = None
    record_tool_io: bool = True
    denied_tool_runs: dict[str, dict[str, Any]] = field(
        default_factory=dict, repr=False, compare=False
    )

    @property
    def total_tokens(self) -> int:
        """Input plus output tokens used by this run."""
        return self.prompt_tokens + self.completion_tokens


#: The agent run currently being observed (see :class:`AgentRun`).
_run_ctx_var: contextvars.ContextVar[AgentRun | None] = contextvars.ContextVar(
    "promptise_agent_run", default=None
)


def get_current_run() -> AgentRun | None:
    """Return the :class:`AgentRun` being observed in this context, if any."""
    return _run_ctx_var.get()


def _record_on_current_run(
    event_type: TimelineEventType,
    *,
    details: str | None = None,
    metadata: dict[str, Any] | None = None,
) -> TimelineEntry | None:
    """Record an event on the collector of the run being observed, if any.

    For code that runs inside an agent run but has no collector of its own
    (approval gates, the runtime's budget guard).  Never raises.
    """
    run = _run_ctx_var.get()
    if run is None or run.collector is None:
        return None
    collector = run.collector
    try:
        entry: TimelineEntry = collector.record(
            event_type, agent_id=run.agent_id, details=details, metadata=metadata
        )
        return entry
    except Exception:
        logger.debug("Could not record %s", event_type.value, exc_info=True)
        return None


def _record_denied_tool_call(
    tool_name: str,
    *,
    reason: str,
    denied_by: str,
    arguments: Any = None,
    run_id: Any = None,
    metadata: dict[str, Any] | None = None,
) -> bool:
    """Record a tool call refused before it ran on the current run's trace.

    Records a ``tool.call`` and a ``tool.result`` with ``status="denied"``,
    the *reason* and *denied_by* (``"budget"``, ``"approval"``), so the
    call shows up like any other, paired by ``run_id``.  Returns whether
    anything was recorded (``False`` outside an observed run).
    """
    run = _run_ctx_var.get()
    if run is None or run.collector is None:
        return False
    fields: dict[str, Any] = {"status": "denied", "denied_by": denied_by, "reason": reason}
    fields.update(metadata or {})
    call_meta: dict[str, Any] = {"tool_name": tool_name, **fields}
    if run_id is not None:
        call_meta["run_id"] = str(run_id)
    if arguments is not None:
        if run.record_tool_io:
            call_meta["arguments"] = _json_safe_arguments(arguments)
        else:
            call_meta["arguments_length"] = len(json.dumps(arguments, default=str))
    run.tool_calls += 1
    _record_on_current_run(
        TimelineEventType.TOOL_CALL,
        details=f"Tool call refused ({denied_by}): {tool_name}",
        metadata=call_meta,
    )
    result_meta = {k: v for k, v in call_meta.items() if k not in ("arguments", "arguments_length")}
    result_meta["latency_ms"] = 0.0
    _record_on_current_run(
        TimelineEventType.TOOL_RESULT,
        details=f"Tool call refused ({denied_by}): {tool_name}: {reason}",
        metadata=result_meta,
    )
    return True


def _mark_tool_run_denied(run_id: Any, *, reason: str, denied_by: str, **fields: Any) -> None:
    """Mark a running tool call as denied, for its ``tool.result``.

    An approval gate denies a call inside the tool's own run: the
    callback handler records its ``tool.call`` as usual, and its
    ``tool.result`` with ``status="denied"`` and these fields.
    """
    run = _run_ctx_var.get()
    if run is None or run_id is None:
        return
    if len(run.denied_tool_runs) >= 1000:
        run.denied_tool_runs.clear()
    run.denied_tool_runs[str(run_id)] = {
        "status": "denied",
        "denied_by": denied_by,
        "reason": reason,
        **fields,
    }


#: Metadata keys holding Promptise / LangChain identifiers, never redacted
#: (a run id that happened to look like a card number would stop matching).
_UNREDACTED_KEYS = frozenset({"run_id", "entry_id", "parent_id"})

#: The notifier's patterns, URL credentials first: the email pattern would
#: otherwise consume ``password@host`` and leave the user name behind.
_REDACTION_PATTERNS = sorted(_PII_PATTERNS, key=lambda p: not p[1].startswith("://"))


def _redact_value(value: Any, key: str | None = None) -> Any:
    if isinstance(value, str):
        if key in _UNREDACTED_KEYS:
            return value
        if _is_secret_key(key):
            return "[REDACTED]"
        for pattern, replacement in _REDACTION_PATTERNS:
            value = pattern.sub(replacement, value)
        return value
    if isinstance(value, dict):
        return {k: _redact_value(v, k if isinstance(k, str) else None) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return type(value)(_redact_value(v) for v in value)
    return value


def redact_sensitive(metadata: dict[str, Any]) -> dict[str, Any]:
    """Replace credentials and common PII in event metadata with placeholders.

    The sanitizer :func:`~promptise.agent.build_agent` installs on the
    collector it creates (``ObservabilityConfig.redact_sensitive``, on by
    default), so tool arguments, tool results, error messages and recorded
    prompt text cannot carry secrets into reports, log files or exported
    traces.  It uses the same patterns as the event notifier: API keys
    (OpenAI/Anthropic ``sk-…``, Stripe, Google), AWS access key IDs and
    secret access keys, GitHub tokens (``ghp_``, ``gho_``, ``ghu_``,
    ``ghs_``, ``ghr_``, ``github_pat_``), GitLab and Slack tokens, JWTs,
    PEM private keys, ``Bearer`` tokens and every ``Authorization`` value
    (any scheme), credentials in URLs (``://user:password@``), card
    numbers, US social security numbers and email addresses.  The value
    of a credential key (``Authorization``, ``password``, ``api_key``,
    ``access_token``...) is replaced whatever it looks like.

    String values are scrubbed at any depth; other values (numbers, flags)
    are kept as they are.  Pattern matching cannot recognise every kind of
    personal data (names, addresses), so set ``record_tool_io=False`` and
    leave ``record_prompts`` off when traces leave your trust boundary.

    Args:
        metadata: Event metadata (or any JSON-like dict).

    Returns:
        A new dict with sensitive substrings replaced.
    """
    return dict(_redact_value(metadata))


__all__ = [
    "AgentRun",
    "get_current_run",
    "redact_sensitive",
    "TimelineEventType",
    "TimelineEventCategory",
    "TimelineEntry",
    "ObservabilityCollector",
    "_derive_category",
    "_truncate_for_metadata",
]


# ---------------------------------------------------------------------------
# Event type enum
# ---------------------------------------------------------------------------


class TimelineEventType(str, Enum):
    """All timeline event types.

    Values are dot-notation strings that appear in serialized output.
    """

    # Agent lifecycle
    AGENT_REGISTERED = "agent.registered"
    AGENT_DEREGISTERED = "agent.deregistered"
    AGENT_INPUT = "agent.input"
    AGENT_OUTPUT = "agent.output"
    AGENT_ERROR = "agent.error"

    # Task lifecycle (kept for extensibility)
    TASK_CREATED = "task.created"
    TASK_STARTED = "task.started"
    TASK_COMPLETED = "task.completed"
    TASK_FAILED = "task.failed"
    TASK_ASSIGNED = "task.assigned"

    # Orchestration (kept for extensibility)
    PHASE_START = "phase.start"
    PHASE_END = "phase.end"
    DECOMPOSITION = "decomposition"
    DISTRIBUTION = "distribution"
    CONSENSUS = "consensus"
    AGGREGATION = "aggregation"
    PIPELINE_STAGE = "pipeline.stage"

    # Security (kept for extensibility)
    AUTH_ATTEMPT = "auth.attempt"
    RBAC_CHECK = "rbac.check"

    # System
    HEALTH_CHECK = "health.check"
    CIRCUIT_BREAKER = "circuit.breaker"
    SESSION_START = "session.start"
    SESSION_END = "session.end"

    # Invocation events
    INVOCATION_TIMEOUT = "invocation.timeout"

    # LLM events
    LLM_TURN = "llm.turn"
    LLM_START = "llm.start"
    LLM_END = "llm.end"
    LLM_ERROR = "llm.error"
    LLM_RETRY = "llm.retry"

    # Tool events
    TOOL_CALL = "tool.call"
    TOOL_RESULT = "tool.result"
    TOOL_ERROR = "tool.error"

    # Prompt events
    PROMPT_START = "prompt.start"
    PROMPT_END = "prompt.end"
    PROMPT_ERROR = "prompt.error"
    PROMPT_GUARD_BLOCK = "prompt.guard_block"
    PROMPT_CONTEXT = "prompt.context"

    # Cache events
    CACHE_HIT = "cache.hit"
    CACHE_MISS = "cache.miss"
    CACHE_STORE = "cache.store"
    CACHE_ERROR = "cache.error"

    # Approval events
    APPROVAL_REQUESTED = "approval.requested"
    APPROVAL_GRANTED = "approval.granted"
    APPROVAL_DENIED = "approval.denied"
    APPROVAL_TIMEOUT = "approval.timeout"

    # Runtime events
    PROCESS_START = "process.start"
    PROCESS_STOP = "process.stop"
    TRIGGER_FIRED = "trigger.fired"


# ---------------------------------------------------------------------------
# Event category enum
# ---------------------------------------------------------------------------


class TimelineEventCategory(str, Enum):
    """High-level category groupings for timeline events."""

    AGENT = "agent"
    TASK = "task"
    ORCHESTRATION = "orchestration"
    SECURITY = "security"
    SYSTEM = "system"
    TRANSPARENCY = "transparency"
    PROMPT = "prompt"
    RUNTIME = "runtime"


# ---------------------------------------------------------------------------
# Category derivation
# ---------------------------------------------------------------------------

_TRANSPARENCY_EVENTS = frozenset(
    {
        TimelineEventType.AGENT_INPUT,
        TimelineEventType.AGENT_OUTPUT,
        TimelineEventType.AGENT_ERROR,
        TimelineEventType.TOOL_CALL,
        TimelineEventType.TOOL_RESULT,
        TimelineEventType.TOOL_ERROR,
        TimelineEventType.LLM_TURN,
        TimelineEventType.LLM_START,
        TimelineEventType.LLM_END,
        TimelineEventType.LLM_ERROR,
        TimelineEventType.LLM_RETRY,
    }
)

_TASK_EVENTS = frozenset(
    {
        TimelineEventType.TASK_CREATED,
        TimelineEventType.TASK_STARTED,
        TimelineEventType.TASK_COMPLETED,
        TimelineEventType.TASK_FAILED,
        TimelineEventType.TASK_ASSIGNED,
    }
)

_ORCHESTRATION_EVENTS = frozenset(
    {
        TimelineEventType.PHASE_START,
        TimelineEventType.PHASE_END,
        TimelineEventType.DECOMPOSITION,
        TimelineEventType.DISTRIBUTION,
        TimelineEventType.CONSENSUS,
        TimelineEventType.AGGREGATION,
        TimelineEventType.PIPELINE_STAGE,
    }
)

_SECURITY_EVENTS = frozenset(
    {
        TimelineEventType.AUTH_ATTEMPT,
        TimelineEventType.RBAC_CHECK,
    }
)

_SYSTEM_EVENTS = frozenset(
    {
        TimelineEventType.HEALTH_CHECK,
        TimelineEventType.CIRCUIT_BREAKER,
        TimelineEventType.SESSION_START,
        TimelineEventType.SESSION_END,
    }
)

_PROMPT_EVENTS = frozenset(
    {
        TimelineEventType.PROMPT_START,
        TimelineEventType.PROMPT_END,
        TimelineEventType.PROMPT_ERROR,
        TimelineEventType.PROMPT_GUARD_BLOCK,
        TimelineEventType.PROMPT_CONTEXT,
    }
)

_RUNTIME_EVENTS = frozenset(
    {
        TimelineEventType.PROCESS_START,
        TimelineEventType.PROCESS_STOP,
        TimelineEventType.TRIGGER_FIRED,
    }
)


def _derive_category(event_type: TimelineEventType) -> TimelineEventCategory:
    """Derive the category for a given event type.

    Args:
        event_type: The event type to categorise.

    Returns:
        The appropriate :class:`TimelineEventCategory`.
    """
    if event_type in _TRANSPARENCY_EVENTS:
        return TimelineEventCategory.TRANSPARENCY
    if event_type in _TASK_EVENTS:
        return TimelineEventCategory.TASK
    if event_type in _ORCHESTRATION_EVENTS:
        return TimelineEventCategory.ORCHESTRATION
    if event_type in _SECURITY_EVENTS:
        return TimelineEventCategory.SECURITY
    if event_type in _SYSTEM_EVENTS:
        return TimelineEventCategory.SYSTEM
    if event_type in _PROMPT_EVENTS:
        return TimelineEventCategory.PROMPT
    if event_type in _RUNTIME_EVENTS:
        return TimelineEventCategory.RUNTIME
    # Fallback: derive from value prefix
    val = event_type.value
    if val.startswith("agent."):
        return TimelineEventCategory.AGENT
    return TimelineEventCategory.SYSTEM


# ---------------------------------------------------------------------------
# Utility
# ---------------------------------------------------------------------------


def _truncate_for_metadata(s: str, max_len: int = 2000) -> str:
    """Truncate a string for safe inclusion in event metadata.

    Args:
        s: The string to truncate.
        max_len: Maximum allowed length. Defaults to 2000.

    Returns:
        The original string if within limit, otherwise a truncated string
        with an annotation of the total character count.
    """
    if len(s) <= max_len:
        return s
    return s[:max_len] + f"... [truncated, {len(s)} total chars]"


def _owner_key(user_id: str | None, tenant_id: str | None) -> str | None:
    """``"{tenant_id}::{user_id}"`` with a tenant, else *user_id*."""
    if user_id is None:
        return None
    return f"{tenant_id}::{user_id}" if tenant_id else user_id


#: Limits for tool arguments recorded on the timeline.
_ARG_MAX_STR = 2000
_ARG_MAX_ITEMS = 200
_ARG_MAX_DEPTH = 8


def _json_safe_arguments(value: Any, *, max_str: int = _ARG_MAX_STR) -> Any:
    """*value* as JSON data, for the ``arguments`` of a ``tool.call`` entry.

    Pydantic models, dataclasses, enums, dates, sets and bytes become their
    JSON form (as pydantic serialises them), anything else its ``str()``.
    Strings longer than *max_str* are cut with a ``[truncated, N total
    chars]`` note; a list or object with more than 200 items keeps the
    first 200, and nesting deeper than 8 levels is replaced by its text.
    """
    from pydantic_core import to_jsonable_python

    try:
        plain = to_jsonable_python(value, fallback=str)
    except Exception:
        plain = str(value)
    return _cap_json(plain, max_str, 0)


def _cap_json(value: Any, max_str: int, depth: int) -> Any:
    if isinstance(value, str):
        return _truncate_for_metadata(value, max_str)
    if isinstance(value, dict):
        if depth >= _ARG_MAX_DEPTH:
            return _truncate_for_metadata(json.dumps(value, default=str), max_str)
        items = list(value.items())
        out = {str(k): _cap_json(v, max_str, depth + 1) for k, v in items[:_ARG_MAX_ITEMS]}
        if len(items) > _ARG_MAX_ITEMS:
            out["..."] = f"[{len(items) - _ARG_MAX_ITEMS} more keys not recorded]"
        return out
    if isinstance(value, list):
        if depth >= _ARG_MAX_DEPTH:
            return _truncate_for_metadata(json.dumps(value, default=str), max_str)
        out_list = [_cap_json(v, max_str, depth + 1) for v in value[:_ARG_MAX_ITEMS]]
        if len(value) > _ARG_MAX_ITEMS:
            out_list.append(f"[{len(value) - _ARG_MAX_ITEMS} more items not recorded]")
        return out_list
    return value


def _parse_tool_input(input_str: str, inputs: Any = None) -> Any:
    """The arguments of a tool call as JSON data.

    *inputs* is the dict LangChain passes to ``on_tool_start`` for a
    structured call; without it, *input_str* is read as JSON or as the
    Python literal LangChain renders a dict as.  A tool given plain text
    keeps the text.
    """
    if isinstance(inputs, dict):
        return _json_safe_arguments(inputs)
    text = input_str.strip() if isinstance(input_str, str) else ""
    if text[:1] in ("{", "["):
        try:
            return _json_safe_arguments(json.loads(text))
        except ValueError:
            pass
        if len(text) <= 100_000:
            import ast

            try:
                parsed = ast.literal_eval(text)
            except (ValueError, SyntaxError, TypeError, MemoryError, RecursionError):
                parsed = None
            if isinstance(parsed, (dict, list)):
                return _json_safe_arguments(parsed)
    return _json_safe_arguments(input_str)


# ---------------------------------------------------------------------------
# TimelineEntry
# ---------------------------------------------------------------------------


@dataclass
class TimelineEntry:
    """A single event recorded on the observability timeline.

    Args:
        entry_id: Unique identifier for this entry.
        timestamp: Unix epoch seconds when the event was recorded.
        event_type: The type of event.
        category: High-level category derived from the event type.
        agent_id: Identifier of the agent that produced the event.
        phase: Optional orchestration or execution phase label.
        details: Human-readable description of the event.
        duration: Duration in seconds (for span events).
        parent_id: Parent entry ID for hierarchical tracing.
        user_id: Owning user for this event (``user_id`` of the
            :class:`~promptise.agent.CallerContext`).  ``None`` means
            the event was produced outside of a per-caller invocation.
        session_id: Conversation session associated with the event,
            when available: an explicit ``session_id`` kwarg to
            :meth:`~ObservabilityCollector.record`, else
            ``caller.metadata['session_id']``, else the session of the
            :meth:`~promptise.agent.PromptiseAgent.chat` call that is running.
        metadata: Arbitrary structured data for the event.
        tenant_id: Tenant of the owning user (``tenant_id`` of the
            :class:`~promptise.agent.CallerContext`), or ``None``.  Two
            tenants' users with the same ``user_id`` are told apart by it;
            :attr:`isolation_key` joins the two.
    """

    entry_id: str
    timestamp: float
    event_type: TimelineEventType
    category: TimelineEventCategory
    agent_id: str | None = None
    phase: str | None = None
    details: str | None = None
    duration: float | None = None
    parent_id: str | None = None
    user_id: str | None = None
    session_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    # Last, so positional construction from older code keeps working.
    tenant_id: str | None = None

    @property
    def isolation_key(self) -> str | None:
        """The owner's key, as :attr:`CallerContext.isolation_key
        <promptise.agent.CallerContext.isolation_key>` builds it:
        ``"{tenant_id}::{user_id}"`` with a tenant, else ``user_id``."""
        return _owner_key(self.user_id, self.tenant_id)

    def to_dict(self) -> dict[str, Any]:
        """Serialise the entry to a plain dictionary.

        Returns:
            Dict with all fields; ``event_type`` and ``category`` are
            their string values.
        """
        return {
            "entry_id": self.entry_id,
            "timestamp": self.timestamp,
            "event_type": self.event_type.value,
            "category": self.category.value,
            "agent_id": self.agent_id,
            "phase": self.phase,
            "details": self.details,
            "duration": self.duration,
            "parent_id": self.parent_id,
            "user_id": self.user_id,
            "tenant_id": self.tenant_id,
            "session_id": self.session_id,
            "metadata": self.metadata,
        }


# ---------------------------------------------------------------------------
# ObservabilityCollector
# ---------------------------------------------------------------------------


class ObservabilityCollector:
    """Thread-safe collector for structured timeline events.

    Records events from agents, tools, prompts, and the runtime.
    Events are dispatched to registered :class:`BaseTransporter` instances
    in real time.  Supports ring-buffer eviction via ``max_entries``.

    Args:
        session_name: Human-readable label for this session.
        max_entries: Maximum number of entries to retain. Oldest entries
            are evicted when the buffer is full. Defaults to 100,000.
        sanitizer: Optional callable applied to every event before it is
            stored or sent to a transporter: it gets the event's metadata,
            and ``{"details": <text>}`` for its details, and returns the
            cleaned dict.  :func:`redact_sensitive` is the one
            :func:`~promptise.agent.build_agent` uses.  ``None`` (default)
            records events unchanged.

    Example::

        collector = ObservabilityCollector("my-agent-session")
        collector.add_transporter(ConsoleTransporter())

        entry = collector.record(
            TimelineEventType.TOOL_CALL,
            agent_id="my-agent",
            details="Calling search_files",
            metadata={"tool_name": "search_files"},
        )
    """

    def __init__(
        self,
        session_name: str = "promptise",
        max_entries: int = 100_000,
        sanitizer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
    ) -> None:
        self.session_name = session_name
        self.max_entries = max_entries
        self.session_start: float = time.time()
        self._entries: deque[TimelineEntry] = deque(maxlen=max_entries)
        self._transporters: list[Any] = []
        self._lock = threading.Lock()
        self._sanitizer = sanitizer

    # ------------------------------------------------------------------
    # Recording
    # ------------------------------------------------------------------

    def record(
        self,
        event_type: TimelineEventType,
        *,
        agent_id: str | None = None,
        phase: str | None = None,
        details: str | None = None,
        duration: float | None = None,
        parent_id: str | None = None,
        user_id: str | None = None,
        session_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        tenant_id: str | None = None,
    ) -> TimelineEntry:
        """Record a single timeline event.

        Args:
            event_type: The event type.
            agent_id: Agent that produced the event.
            phase: Execution phase label.
            details: Human-readable event description.
            duration: Duration in seconds (for span events).
            parent_id: Parent entry for hierarchical tracing.  When
                omitted and an agent run is being observed (see
                :class:`AgentRun`), the run's ``agent.input`` entry.
            user_id: Explicit owning user for this event.  When omitted,
                the collector reads :func:`promptise.agent.get_current_caller`
                and attaches ``caller.user_id`` and ``caller.tenant_id``
                automatically.  An isolation key (``"acme::alice"``) is
                split into user and tenant.
            session_id: Conversation session associated with this event.
                Falls back to ``caller.metadata['session_id']``, then to the
                session of the running :meth:`~promptise.agent.PromptiseAgent.chat`
                call.
            metadata: Arbitrary structured data.
            tenant_id: Explicit tenant of the owning user.  When omitted
                with ``user_id``, it is read from the caller as well.

        Returns:
            The created :class:`TimelineEntry`.
        """
        raw_metadata = metadata or {}
        if self._sanitizer is not None:
            raw_metadata = self._sanitizer(raw_metadata)
            # ``details`` is free text too (error messages end up in it).
            if details:
                details = str(self._sanitizer({"details": details}).get("details", details))

        # Stamp the delegating agent's identity when this event is recorded
        # inside a cross-agent delegation, so the peer's timeline answers
        # "who delegated this?". Identity descriptors only (no credential).
        delegation = _delegation_ctx_var.get()
        if delegation is not None and "delegated_by" not in raw_metadata:
            # Snapshot the claims — the contextvar holds one dict shared by
            # every entry recorded during this delegation, so store an
            # independent copy or a later mutation of one entry's
            # ``delegated_by`` would retroactively alter its siblings.
            raw_metadata = {**raw_metadata, "delegated_by": dict(delegation)}

        # Auto-propagate identity from CallerContext when not explicitly
        # supplied.  Imported lazily to avoid a circular dependency with
        # ``promptise.agent``.
        effective_user_id = user_id
        effective_tenant_id = tenant_id
        effective_session_id = session_id
        if effective_user_id is not None and effective_tenant_id is None:
            # An isolation key: CallerContext forbids '::' in a user_id.
            tenant, sep, user = effective_user_id.partition("::")
            if sep and tenant and user:
                effective_tenant_id, effective_user_id = tenant, user
        if effective_user_id is None or effective_session_id is None:
            try:
                from .agent import get_current_caller, get_current_session_id
            except Exception:  # pragma: no cover — defensive
                get_current_caller = None  # type: ignore[assignment]
                get_current_session_id = None  # type: ignore[assignment]
            if get_current_caller is not None:
                caller = get_current_caller()
                if caller is not None:
                    if effective_user_id is None:
                        effective_user_id = getattr(caller, "user_id", None)
                        if effective_tenant_id is None:
                            effective_tenant_id = getattr(caller, "tenant_id", None)
                    if effective_session_id is None:
                        caller_meta = getattr(caller, "metadata", None) or {}
                        effective_session_id = caller_meta.get("session_id")
            if effective_session_id is None and get_current_session_id is not None:
                effective_session_id = get_current_session_id()

        effective_parent_id = parent_id
        if effective_parent_id is None:
            run = _run_ctx_var.get()
            if run is not None:
                effective_parent_id = run.entry_id

        entry = TimelineEntry(
            entry_id=str(uuid.uuid4()),
            timestamp=time.time(),
            event_type=event_type,
            category=_derive_category(event_type),
            agent_id=agent_id,
            phase=phase,
            details=details,
            duration=duration,
            parent_id=effective_parent_id,
            user_id=effective_user_id,
            session_id=effective_session_id,
            metadata=raw_metadata,
            tenant_id=effective_tenant_id,
        )
        with self._lock:
            self._entries.append(entry)
        self._dispatch(entry)
        return entry

    def record_event(
        self,
        event_type: TimelineEventType,
        agent_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> TimelineEntry:
        """Record an event using the ``data`` keyword (prompt bridge API).

        This is a convenience wrapper around :meth:`record` for callers
        that pass structured data as a ``data`` dict rather than keyword args.

        Args:
            event_type: The event type.
            agent_id: Agent that produced the event.
            data: Structured event data, stored as metadata.

        Returns:
            The created :class:`TimelineEntry`.
        """
        return self.record(event_type, agent_id=agent_id, metadata=data or {})

    @contextmanager
    def span(
        self,
        event_type: TimelineEventType,
        **kwargs: Any,
    ) -> Iterator[TimelineEntry]:
        """Context manager that records an event and measures its duration.

        The entry is recorded immediately on entry; its ``duration`` field
        is set to the elapsed seconds when the context exits (even if an
        exception is raised).

        Args:
            event_type: The event type.
            **kwargs: Passed through to :meth:`record`.

        Yields:
            The :class:`TimelineEntry` being tracked.

        Example::

            with collector.span(TimelineEventType.PHASE_START, phase="pipeline") as e:
                await do_work()
            print(e.duration)  # seconds
        """
        entry = self.record(event_type, **kwargs)
        start = time.perf_counter()
        try:
            yield entry
        finally:
            entry.duration = time.perf_counter() - start

    # ------------------------------------------------------------------
    # Transporters
    # ------------------------------------------------------------------

    def add_transporter(self, transporter: Any) -> None:
        """Register a transporter to receive timeline events.

        Args:
            transporter: Any object with an ``on_event(entry)`` method.
        """
        with self._lock:
            self._transporters.append(transporter)

    def _dispatch(self, entry: TimelineEntry) -> None:
        """Dispatch an entry to all registered transporters."""
        with self._lock:
            transporters = list(self._transporters)
        for t in transporters:
            try:
                t.on_event(entry)
            except Exception as exc:
                logger.debug("Transporter %r error: %s", t, exc)

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def get_timeline(self) -> list[TimelineEntry]:
        """Return all entries sorted by timestamp.

        Returns:
            Sorted list of :class:`TimelineEntry` objects.
        """
        with self._lock:
            entries = list(self._entries)
        return sorted(entries, key=lambda e: e.timestamp)

    def get_agents(self) -> list[str]:
        """Return sorted unique agent IDs seen in the timeline.

        Returns:
            Sorted list of agent ID strings (None entries excluded).
        """
        with self._lock:
            entries = list(self._entries)
        return sorted({e.agent_id for e in entries if e.agent_id is not None})

    def get_phases(self) -> list[str]:
        """Return sorted unique phase labels seen in the timeline.

        Returns:
            Sorted list of phase strings (None entries excluded).
        """
        with self._lock:
            entries = list(self._entries)
        return sorted({e.phase for e in entries if e.phase is not None})

    def query(
        self,
        *,
        event_types: list[TimelineEventType | str] | None = None,
        agent_ids: list[str] | None = None,
        user_ids: list[str] | None = None,
        session_ids: list[str] | None = None,
        tenant_ids: list[str | None] | None = None,
        limit: int | None = None,
        offset: int | None = None,
        metadata_key: str | None = None,
        metadata_value: Any = None,
    ) -> list[TimelineEntry]:
        """Query the timeline with optional filters.

        Args:
            event_types: Restrict to these event types (enum or string values).
            agent_ids: Restrict to these agent IDs.
            user_ids: Restrict to events owned by these users (matches
                :attr:`TimelineEntry.user_id`, in any tenant; combine with
                ``tenant_ids``, or use :meth:`for_user`, for one tenant's
                user).
            session_ids: Restrict to events tied to these session IDs
                (matches :attr:`TimelineEntry.session_id`).
            tenant_ids: Restrict to events of these tenants (matches
                :attr:`TimelineEntry.tenant_id`; ``None`` in the list
                selects events without a tenant).
            limit: Maximum number of results to return.
            offset: Number of results to skip from the start.
            metadata_key: Restrict to entries whose metadata contains this key.
            metadata_value: If set, also match the metadata value for ``metadata_key``.

        Returns:
            Filtered and sliced list of :class:`TimelineEntry` objects.
        """
        entries = self.get_timeline()

        if event_types is not None:
            resolved: set[TimelineEventType] = set()
            for et in event_types:
                if isinstance(et, TimelineEventType):
                    resolved.add(et)
                else:
                    try:
                        resolved.add(TimelineEventType(et))
                    except ValueError:
                        pass  # unknown event type name: matches nothing
            entries = [e for e in entries if e.event_type in resolved]

        if agent_ids is not None:
            agent_set = set(agent_ids)
            entries = [e for e in entries if e.agent_id in agent_set]

        if user_ids is not None:
            user_set = set(user_ids)
            entries = [e for e in entries if e.user_id in user_set]

        if session_ids is not None:
            session_set = set(session_ids)
            entries = [e for e in entries if e.session_id in session_set]

        if tenant_ids is not None:
            tenant_set = set(tenant_ids)
            entries = [e for e in entries if e.tenant_id in tenant_set]

        if metadata_key is not None:
            if metadata_value is not None:
                entries = [e for e in entries if e.metadata.get(metadata_key) == metadata_value]
            else:
                entries = [e for e in entries if metadata_key in e.metadata]

        start = offset or 0
        entries = entries[start:]
        if limit is not None:
            entries = entries[:limit]

        return entries

    def for_user(self, user_id: str, *, tenant_id: str | None = None) -> list[TimelineEntry]:
        """Return all entries owned by one caller (sorted by timestamp).

        Pass the ``user_id`` and ``tenant_id`` the caller had, or its
        isolation key (``"acme::alice"``) as ``user_id``.  Without a
        tenant, only the user's events recorded without a tenant match:
        ``for_user("alice")`` never returns ``acme``'s Alice.  Returns an
        empty list when the user has no events.
        """
        key = _owner_key(user_id, tenant_id) if user_id else None
        if not key:
            return []
        return [e for e in self.get_timeline() if e.isolation_key == key]

    def get_users(self) -> list[str]:
        """Return the sorted unique owners seen in the timeline.

        Each owner is its isolation key: the ``user_id``, or
        ``"{tenant_id}::{user_id}"`` for a user with a tenant, the form
        :meth:`for_user` and :meth:`purge_user` take.  Events without a
        user_id (e.g. system events recorded outside of an invocation)
        are excluded.
        """
        with self._lock:
            entries = list(self._entries)
        return sorted({k for e in entries if (k := e.isolation_key) is not None})

    def purge_user(self, user_id: str, *, tenant_id: str | None = None) -> int:
        """Delete every timeline entry owned by one caller from the buffer.

        Pass the ``user_id`` and ``tenant_id`` the caller had, or its
        isolation key (``"acme::alice"``) as ``user_id``, as for
        :meth:`for_user`.  Another tenant's user with the same id is not
        touched, and ``purge_user("alice")`` without a tenant removes only
        Alice's events recorded without one (a warning says how many
        events of a tenant's ``alice`` were kept).

        Does not propagate to already-flushed transporters (e.g. a
        writable JSON file on disk) — those are external sinks and must
        be purged by their owners.  Returns the number of in-memory
        entries removed.

        Use it to delete one user's data on request.
        """
        key = _owner_key(user_id, tenant_id) if user_id else None
        if not key:
            return 0
        with self._lock:
            kept = [e for e in self._entries if e.isolation_key != key]
            removed = len(self._entries) - len(kept)
            if removed:
                self._entries.clear()
                self._entries.extend(kept)
            other_tenants = (
                sum(1 for e in kept if e.user_id == user_id and e.tenant_id is not None)
                if tenant_id is None and "::" not in user_id
                else 0
            )
        if other_tenants:
            logger.warning(
                "purge_user(%r) kept %d event(s) of a user with that id in a tenant; "
                "pass tenant_id= (or the 'tenant::user' key) to remove those",
                user_id,
                other_tenants,
            )
        return removed

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def get_stats(self) -> dict[str, Any]:
        """Return aggregated statistics for the current session.

        Returns:
            Dict with token counts, latency percentiles, error counts,
            and per-agent/per-type/per-category breakdowns.
        """
        entries = self.get_timeline()

        total_prompt_tokens = 0
        total_completion_tokens = 0
        total_tokens = 0
        llm_call_count = 0
        tool_call_count = 0
        tool_denied_count = 0
        error_count = 0
        retry_count = 0
        tokens_by_agent: dict[str, int] = {}
        events_by_category: dict[str, int] = {}
        events_by_type: dict[str, int] = {}
        latencies: list[float] = []

        # Runs observed at BASIC level record no llm.end events; their
        # agent.output carries the run's totals instead.  Count those only
        # for runs that have no llm.end, so nothing is counted twice.
        runs_with_llm_events = {
            e.parent_id
            for e in entries
            if e.event_type == TimelineEventType.LLM_END and e.parent_id is not None
        }

        for e in entries:
            if (
                e.event_type == TimelineEventType.AGENT_OUTPUT
                and e.parent_id is not None
                and e.parent_id not in runs_with_llm_events
                and "llm_call_count" in e.metadata
            ):
                llm_call_count += int(e.metadata.get("llm_call_count", 0))
                pt = int(e.metadata.get("prompt_tokens", 0))
                ct = int(e.metadata.get("completion_tokens", 0))
                tt = int(e.metadata.get("total_tokens", pt + ct))
                total_prompt_tokens += pt
                total_completion_tokens += ct
                total_tokens += tt
                if e.agent_id:
                    tokens_by_agent[e.agent_id] = tokens_by_agent.get(e.agent_id, 0) + tt

            # Token accounting from LLM_END events
            if e.event_type == TimelineEventType.LLM_END:
                llm_call_count += 1
                pt = int(e.metadata.get("prompt_tokens", 0))
                ct = int(e.metadata.get("completion_tokens", 0))
                tt = int(e.metadata.get("total_tokens", pt + ct))
                total_prompt_tokens += pt
                total_completion_tokens += ct
                total_tokens += tt
                if e.agent_id:
                    tokens_by_agent[e.agent_id] = tokens_by_agent.get(e.agent_id, 0) + tt

            if e.event_type == TimelineEventType.TOOL_CALL:
                tool_call_count += 1

            if (
                e.event_type == TimelineEventType.TOOL_RESULT
                and e.metadata.get("status") == "denied"
            ):
                # Refused before it ran (budget, approval): not an error.
                tool_denied_count += 1

            if e.event_type in (
                TimelineEventType.LLM_ERROR,
                TimelineEventType.TOOL_ERROR,
                TimelineEventType.AGENT_ERROR,
                TimelineEventType.TASK_FAILED,
            ) or (
                # A tool that reported an error instead of raising
                # (ToolMessage with status="error").
                e.event_type == TimelineEventType.TOOL_RESULT
                and e.metadata.get("status") == "error"
            ):
                error_count += 1

            if e.event_type == TimelineEventType.LLM_RETRY:
                retry_count += 1

            if e.duration is not None:
                latencies.append(e.duration * 1000)  # ms

            cat_key = e.category.value
            events_by_category[cat_key] = events_by_category.get(cat_key, 0) + 1

            type_key = e.event_type.value
            events_by_type[type_key] = events_by_type.get(type_key, 0) + 1

        def _percentile(data: list[float], pct: float) -> float:
            if not data:
                return 0.0
            s = sorted(data)
            idx = int(len(s) * pct / 100)
            return s[min(idx, len(s) - 1)]

        return {
            "entry_count": len(entries),
            "agent_count": len(self.get_agents()),
            "total_duration_s": time.time() - self.session_start,
            "total_prompt_tokens": total_prompt_tokens,
            "total_completion_tokens": total_completion_tokens,
            "total_tokens": total_tokens,
            "llm_call_count": llm_call_count,
            "tool_call_count": tool_call_count,
            "tool_denied_count": tool_denied_count,
            "error_count": error_count,
            "retry_count": retry_count,
            "latency_p50_ms": _percentile(latencies, 50),
            "latency_p95_ms": _percentile(latencies, 95),
            "latency_p99_ms": _percentile(latencies, 99),
            "tokens_by_agent": tokens_by_agent,
            "events_by_category": events_by_category,
            "events_by_type": events_by_type,
        }

    # ------------------------------------------------------------------
    # Serialization
    # ------------------------------------------------------------------

    def to_dict(self) -> dict[str, Any]:
        """Serialise the full session to a dictionary.

        Returns:
            Dict with session metadata, entry list, and stats.
        """
        entries = self.get_timeline()
        return {
            "session_name": self.session_name,
            "entry_count": len(entries),
            "agents": self.get_agents(),
            "phases": self.get_phases(),
            "total_duration": time.time() - self.session_start,
            "entries": [e.to_dict() for e in entries],
            "stats": self.get_stats(),
        }

    def to_json(self, indent: int | None = None) -> str:
        """Serialise the full session to a JSON string.

        Args:
            indent: Optional JSON indentation level.

        Returns:
            JSON-encoded session data.
        """
        return json.dumps(self.to_dict(), indent=indent, default=str)

    def to_ndjson(self) -> str:
        """Serialise entries as newline-delimited JSON.

        Each line is an independent JSON object containing the entry fields
        plus a ``session_name`` key.

        Returns:
            NDJSON string (empty string if no entries).
        """
        entries = self.get_timeline()
        if not entries:
            return ""
        lines = []
        for e in entries:
            d = e.to_dict()
            d["session_name"] = self.session_name
            lines.append(json.dumps(d, default=str))
        return "\n".join(lines)

    def clear(self) -> None:
        """Clear all timeline entries and reset the session start time."""
        with self._lock:
            self._entries.clear()
        self.session_start = time.time()

    def __repr__(self) -> str:
        return (
            f"<ObservabilityCollector session={self.session_name!r} "
            f"entries={len(self._entries)} "
            f"transporters={len(self._transporters)}>"
        )
