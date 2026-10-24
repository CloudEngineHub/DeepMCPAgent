"""Message inbox for human-to-agent communication.

Provides a per-process message channel where humans can send directives,
context, corrections, and questions to running agents without stopping
them.  The agent checks the inbox between invocations and incorporates
messages into its context.

Example::

    inbox = MessageInbox(max_messages=50, default_ttl=3600)
    await inbox.add(InboxMessage(
        content="Ignore staging alerts for the next hour.",
        message_type=MessageType.DIRECTIVE,
        priority="high",
    ))

    # Agent checks before invocation:
    pending = await inbox.get_pending()
    questions = await inbox.get_questions()

Prompt format (:func:`format_inbox_for_prompt`)::

    === OPERATOR MESSAGES ===

    [HIGH DIRECTIVE] (from: alice, 2 min ago, expires in 58 min)
    Ignore staging alerts for the next hour.

    [QUESTION] (just now)
    How many alerts did you skip?
    → Please include your answer prefixed with "ANSWER Q1:"

    === END OPERATOR MESSAGES ===

The label is ``[<PRIORITY> <TYPE>]``, with the priority left out for
``normal``.  Questions are numbered ``Q1``, ``Q2``, … in the order they
appear in the block; :func:`parse_answers` maps the agent's
``ANSWER Qn:`` lines back to them.

A message stays in the inbox until it expires or is consumed:
:meth:`MessageInbox.remove_consumed` removes the messages a run has seen
(and the questions it answered).
"""

from __future__ import annotations

import asyncio
import logging
import re
import secrets
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any

logger = logging.getLogger("promptise.runtime.inbox")

__all__ = [
    "MessageType",
    "InboxMessage",
    "InboxResponse",
    "MessageInbox",
    "InboxError",
    "InboxFullError",
    "InboxRateLimitError",
    "InboxMessageRejected",
    "InvalidInboxMessage",
    "format_inbox_for_prompt",
    "parse_answers",
    "prompt_questions",
    "scan_inbox_message",
]


# ---------------------------------------------------------------------------
# Errors (all subclass ValueError, which add() raised in 1.4.3 and earlier)
# ---------------------------------------------------------------------------


class InboxError(ValueError):
    """Base class for messages the inbox refuses."""


class InvalidInboxMessage(InboxError):
    """The message is malformed (empty content, unknown priority or type)."""


class InboxRateLimitError(InboxError):
    """The sender exceeded ``rate_limit_per_sender`` messages per hour."""


class InboxFullError(InboxError):
    """The inbox is at ``max_messages`` and nothing can be evicted for this message."""


class InboxQuestionRemoved(InboxError):
    """A question was removed before it was answered (:meth:`MessageInbox.clear`).

    Raised to whoever waits for its answer
    (:meth:`MessageInbox.wait_for_response`).  A question evicted from a
    full inbox raises :class:`InboxFullError` there instead.
    """


class InboxMessageRejected(InboxError):
    """The message's content was flagged by the guardrail scanner.

    Attributes:
        reason: What the scanner found (finding categories).
    """

    def __init__(self, message: str, reason: str) -> None:
        super().__init__(message)
        self.reason = reason


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


class MessageType(str, Enum):
    """Types of human-to-agent messages."""

    DIRECTIVE = "directive"  # "Do X" or "Don't do Y"
    CONTEXT = "context"  # "FYI, here's something you should know"
    QUESTION = "question"  # "What's the status of X?"
    CORRECTION = "correction"  # "You were wrong about X"


# Priority ordering (higher number = higher priority)
_PRIORITY_ORDER = {"low": 0, "normal": 1, "high": 2, "critical": 3}


def _end_question(future: asyncio.Future[Any], exc: BaseException) -> None:
    """End an unanswered question's wait with *exc*.

    A waiter that is still there gets *exc* (through its shield).  The
    future is marked retrieved, so a question nobody waits for any more
    (its ``/ask`` timed out) does not log "Future exception was never
    retrieved" when it is garbage collected.
    """
    if future.done():
        return
    future.set_exception(exc)
    future.exception()


@dataclass
class InboxMessage:
    """A message from a human to a running agent.

    Attributes:
        message_id: Unique cryptographic ID.
        content: The message text.
        message_type: Type of message (directive, context, question, correction).
        sender_id: Who sent it (for audit trail).
        priority: Message priority (low, normal, high, critical).
        created_at: When the message was created.
        expires_at: When the message becomes stale (None = never).
        metadata: Developer-provided custom data.
    """

    content: str
    message_type: MessageType = MessageType.CONTEXT
    sender_id: str | None = None
    priority: str = "normal"
    message_id: str = field(default_factory=lambda: f"msg_{secrets.token_hex(12)}")
    created_at: float = field(default_factory=time.time)
    expires_at: float | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    def is_expired(self) -> bool:
        """Check if the message has expired."""
        if self.expires_at is None:
            return False
        return time.time() > self.expires_at

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict."""
        return {
            "message_id": self.message_id,
            "content": self.content,
            "message_type": self.message_type.value,
            "sender_id": self.sender_id,
            "priority": self.priority,
            "created_at": self.created_at,
            "expires_at": self.expires_at,
            "metadata": self.metadata,
        }


@dataclass
class InboxResponse:
    """An agent's response to a question.

    Attributes:
        question_id: The message_id of the original question.
        content: The agent's answer text.
        answered_at: When the agent generated the answer.
        invocation_id: Which invocation produced the answer.
    """

    question_id: str
    content: str
    answered_at: float = field(default_factory=time.time)
    invocation_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict."""
        return {
            "question_id": self.question_id,
            "content": self.content,
            "answered_at": self.answered_at,
            "invocation_id": self.invocation_id,
        }


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------


class _SenderRateLimiter:
    """Sliding window rate limiter per sender_id.

    Automatically prunes inactive senders to prevent memory leaks.
    """

    _MAX_TRACKED_SENDERS = 10_000

    def __init__(self, max_per_hour: int) -> None:
        self._max = max_per_hour
        self._windows: dict[str, list[float]] = {}

    def check(self, sender_id: str | None, *, record: bool = True) -> None:
        """Raise :class:`InboxRateLimitError` if the sender is over the limit.

        With ``record=False`` the check doesn't count as a message.
        """
        if sender_id is None or self._max <= 0:
            return
        now = time.time()
        cutoff = now - 3600  # 1 hour window

        # Prune old entries for this sender
        entries = self._windows.get(sender_id, [])
        entries = [t for t in entries if t > cutoff]
        self._windows[sender_id] = entries

        if len(entries) >= self._max:
            raise InboxRateLimitError(
                f"Rate limit exceeded: sender '{sender_id}' sent "
                f"{len(entries)} messages in the last hour (limit: {self._max})"
            )
        if not record:
            return
        entries.append(now)

        # Periodic cleanup: evict inactive senders when dict grows too large
        if len(self._windows) > self._MAX_TRACKED_SENDERS:
            self._cleanup(cutoff)

    def _cleanup(self, cutoff: float) -> None:
        """Remove senders with no activity in the last hour."""
        stale = [
            sid for sid, entries in self._windows.items() if not entries or entries[-1] < cutoff
        ]
        for sid in stale:
            del self._windows[sid]


# ---------------------------------------------------------------------------
# MessageInbox
# ---------------------------------------------------------------------------


class MessageInbox:
    """Async-safe inbox for human-to-agent messages.

    Provides a bounded, priority-sorted queue with TTL-based expiry,
    per-sender rate limiting, and question/response tracking.

    When the inbox holds ``max_messages`` messages, adding one removes an
    answered question if there is one, otherwise evicts the **oldest**
    message of the **lowest** priority present.  Critical
    messages are never evicted, and a message is never added in place of
    one with a higher priority: if every message in the inbox is critical,
    or the new message has a lower priority than every evictable one, the
    new message is refused with :class:`InboxFullError`.  The inbox never
    holds more than ``max_messages`` messages.

    Args:
        max_messages: Maximum messages in the inbox.
        max_message_length: Maximum characters per message; longer
            content is truncated.
        default_ttl: Default time-to-live in seconds (0 = no expiry).
        max_ttl: Maximum allowed TTL in seconds (0 = no cap).
        rate_limit_per_sender: Maximum messages per sender per hour
            (messages without ``sender_id`` are not limited).
        scanner: Optional guardrail scanner (an object with
            ``scan_text(text, direction=...)`` such as
            :class:`~promptise.guardrails.PromptiseSecurityScanner`, or
            with ``check_input(text)``).  When set, :meth:`add` refuses
            content it flags with :class:`InboxMessageRejected`.
    """

    def __init__(
        self,
        *,
        max_messages: int = 50,
        max_message_length: int = 2000,
        default_ttl: float = 3600,
        max_ttl: float = 86400,
        rate_limit_per_sender: int = 20,
        scanner: Any | None = None,
    ) -> None:
        if max_messages < 1:
            raise ValueError(f"max_messages must be at least 1, got {max_messages}")
        if max_message_length < 1:
            raise ValueError(f"max_message_length must be at least 1, got {max_message_length}")
        self._max_messages = max_messages
        self._max_message_length = max_message_length
        self._default_ttl = default_ttl
        self._max_ttl = max_ttl
        self.scanner = scanner

        self._messages: dict[str, InboxMessage] = {}
        self._question_futures: dict[str, asyncio.Future[InboxResponse]] = {}
        self._lock = asyncio.Lock()
        self._rate_limiter = _SenderRateLimiter(rate_limit_per_sender)

    async def add(self, message: InboxMessage) -> str:
        """Add a message to the inbox.

        Content longer than ``max_message_length`` is truncated.  The
        expiry is set from ``default_ttl`` when the message has none, and
        capped at ``max_ttl``.

        Args:
            message: The message to add.

        Returns:
            The message_id.

        Raises:
            InvalidInboxMessage: Empty content, unknown priority or type.
            InboxMessageRejected: The scanner flagged the content.
            InboxFullError: The inbox is full and nothing can be evicted.
            InboxRateLimitError: The sender exceeded its hourly limit.

            All four subclass :class:`ValueError`.
        """
        if isinstance(message.message_type, str) and not isinstance(
            message.message_type, MessageType
        ):
            try:
                message.message_type = MessageType(message.message_type)
            except ValueError:
                raise InvalidInboxMessage(
                    f"Unknown message_type {message.message_type!r}; "
                    f"choose from {[t.value for t in MessageType]}"
                ) from None
        if message.priority not in _PRIORITY_ORDER:
            raise InvalidInboxMessage(
                f"Unknown priority {message.priority!r}; choose from {list(_PRIORITY_ORDER)}"
            )
        if not isinstance(message.content, str) or not message.content.strip():
            raise InvalidInboxMessage("Message content must be a non-empty string")

        # Validate content length
        if len(message.content) > self._max_message_length:
            logger.info(
                "Inbox: message truncated from %d to %d chars",
                len(message.content),
                self._max_message_length,
            )
            message.content = message.content[: self._max_message_length]

        if self.scanner is not None:
            reason = await scan_inbox_message(message, self.scanner)
            if reason is not None:
                logger.warning(
                    "Inbox: refused %s message from %s (flagged: %s)",
                    message.message_type.value,
                    message.sender_id,
                    reason,
                )
                raise InboxMessageRejected(
                    f"Message refused: the guardrail scanner flagged it ({reason})", reason
                )

        # Set expiry if not set (max_ttl 0 = no cap)
        now = time.time()
        if message.expires_at is None and self._default_ttl > 0:
            ttl = self._default_ttl if self._max_ttl <= 0 else min(self._default_ttl, self._max_ttl)
            message.expires_at = now + ttl
        elif message.expires_at is not None and self._max_ttl > 0:
            message.expires_at = min(message.expires_at, now + self._max_ttl)

        async with self._lock:
            # Purge expired first
            self._purge_expired_locked()

            # Refuse before evicting anything; count only accepted messages.
            self._rate_limiter.check(message.sender_id, record=False)
            if len(self._messages) >= self._max_messages:
                self._evict_for_locked(message)
            self._rate_limiter.check(message.sender_id)
            self._messages[message.message_id] = message

            # If it's a question, create a future for the response
            if message.message_type == MessageType.QUESTION:
                loop = asyncio.get_running_loop()
                self._question_futures[message.message_id] = loop.create_future()

        logger.debug(
            "Inbox: added %s message (id=%s, sender=%s, priority=%s)",
            message.message_type.value,
            message.message_id,
            message.sender_id,
            message.priority,
        )
        return message.message_id

    async def get_pending(self) -> list[InboxMessage]:
        """Get all non-expired pending messages, sorted by priority.

        Returns messages in order: critical → high → normal → low,
        then by timestamp (oldest first within same priority).
        Questions that were already answered are left out.
        Does NOT remove messages — call :meth:`remove_consumed` after
        the agent has seen them.
        """
        async with self._lock:
            self._purge_expired_locked()
            messages = [m for m in self._messages.values() if not self._answered_locked(m)]

        # Sort by priority (descending) then timestamp (ascending)
        messages.sort(key=lambda m: (-_PRIORITY_ORDER.get(m.priority, 1), m.created_at))
        return messages

    async def get_questions(self) -> list[InboxMessage]:
        """Get pending questions that haven't been answered yet."""
        async with self._lock:
            return [
                msg
                for msg in self._messages.values()
                if msg.message_type == MessageType.QUESTION
                and msg.message_id in self._question_futures
                and not self._question_futures[msg.message_id].done()
                and not msg.is_expired()
            ]

    async def submit_response(self, question_id: str, response: InboxResponse) -> None:
        """Submit an agent's response to a question.

        Resolves the future that the caller of :meth:`wait_for_response`
        is awaiting.

        Args:
            question_id: The message_id of the original question.
            response: The agent's response.

        Raises:
            KeyError: If no pending question with this ID.
        """
        async with self._lock:
            future = self._question_futures.get(question_id)
            if future is None:
                raise KeyError(f"No pending question with id {question_id!r}")
            if not future.done():
                future.set_result(response)
        logger.debug("Inbox: response submitted for question %s", question_id)

    async def rescan(self) -> list[tuple[str, str]]:
        """Scan the messages already held with :attr:`scanner`; remove flagged ones.

        For a scanner attached after messages arrived (a process attaches
        its payload scanner when it starts, and a created process already
        takes messages).  A removed question's waiter gets
        :class:`InboxMessageRejected`.

        Returns:
            ``(message_id, reason)`` of every removed message (empty
            without a scanner).
        """
        if self.scanner is None:
            return []
        async with self._lock:
            held = list(self._messages.values())
        removed: list[tuple[str, str]] = []
        for message in held:
            reason = await scan_inbox_message(message, self.scanner)
            if reason is None:
                continue
            async with self._lock:
                if self._messages.pop(message.message_id, None) is None:
                    continue
                future = self._question_futures.pop(message.message_id, None)
            if future is not None:
                _end_question(
                    future,
                    InboxMessageRejected(
                        f"Message removed: the guardrail scanner flagged it ({reason})", reason
                    ),
                )
            logger.warning(
                "Inbox: removed %s message %s from %s (flagged when scanned later: %s)",
                message.message_type.value,
                message.message_id,
                message.sender_id,
                reason,
            )
            removed.append((message.message_id, reason))
        return removed

    async def wait_for_response(self, question_id: str, timeout: float = 300) -> InboxResponse:
        """Wait for the agent to respond to a question.

        Args:
            question_id: The message_id of the question.
            timeout: Maximum seconds to wait.

        Returns:
            The agent's response.

        Raises:
            KeyError: If no pending question with this ID.
            asyncio.TimeoutError: If timeout expires, or the question expired.
            InboxQuestionRemoved: The inbox was cleared.
            InboxFullError: The question was evicted from a full inbox.
            InboxMessageRejected: A later scan removed it (:meth:`rescan`).
        """
        async with self._lock:
            future = self._question_futures.get(question_id)
            if future is None:
                raise KeyError(f"No pending question with id {question_id!r}")

        # Shielded: a waiter that gives up must not cancel the question,
        # which stays in the inbox (and can be answered) until it expires.
        return await asyncio.wait_for(asyncio.shield(future), timeout=timeout)

    async def mark_processed(self, message_id: str) -> None:
        """Mark a message as processed (remove from inbox).

        Questions are kept until their response is submitted.
        """
        async with self._lock:
            msg = self._messages.get(message_id)
            if msg is None:
                return
            # Keep questions until answered
            if msg.message_type == MessageType.QUESTION:
                future = self._question_futures.get(message_id)
                if future and not future.done():
                    return  # Don't remove unanswered questions
            self._messages.pop(message_id, None)
            # Clean up answered question futures to prevent memory leak
            if message_id in self._question_futures:
                self._question_futures.pop(message_id, None)

    async def remove_consumed(self, delivered: list[InboxMessage]) -> int:
        """Remove the messages a run consumed.

        Call after a run with the messages that were placed in its prompt
        (the list from :meth:`get_pending`).  Directives, context and
        corrections are removed: the agent has seen them.  Questions are
        removed once answered (see :meth:`submit_response`); unanswered
        questions stay until they are answered or expire.

        Returns:
            How many messages were removed.
        """
        removed = 0
        async with self._lock:
            for msg in delivered:
                current = self._messages.get(msg.message_id)
                if current is None:
                    continue
                if current.message_type == MessageType.QUESTION and not self._answered_locked(
                    current
                ):
                    continue
                self._messages.pop(msg.message_id, None)
                self._question_futures.pop(msg.message_id, None)
                removed += 1
        if removed:
            logger.debug("Inbox: removed %d consumed message(s)", removed)
        return removed

    async def purge_expired(self) -> int:
        """Remove all expired messages. Returns count removed."""
        async with self._lock:
            return self._purge_expired_locked()

    async def clear(self) -> None:
        """Clear all messages.

        Whoever waits for the answer to a cleared question gets
        :class:`InboxQuestionRemoved` (in 1.4.3 and earlier the wait was cancelled,
        which dropped an ``/ask`` request's connection).
        """
        async with self._lock:
            for future in self._question_futures.values():
                _end_question(
                    future,
                    InboxQuestionRemoved("The inbox was cleared before the question was answered"),
                )
            self._messages.clear()
            self._question_futures.clear()

    async def status(self) -> dict[str, Any]:
        """Get inbox status summary (answered questions are not pending)."""
        async with self._lock:
            self._purge_expired_locked()
            pending = [m for m in self._messages.values() if not self._answered_locked(m)]
            questions = [
                m
                for m in pending
                if m.message_type == MessageType.QUESTION
                and m.message_id in self._question_futures
                and not self._question_futures[m.message_id].done()
            ]
            oldest_age = 0.0
            if pending:
                oldest_age = time.time() - min(m.created_at for m in pending)

        return {
            "pending_messages": len(pending),
            "pending_questions": len(questions),
            "oldest_message_age_seconds": round(oldest_age, 1),
            "messages": [
                {
                    "message_id": m.message_id,
                    "type": m.message_type.value,
                    "priority": m.priority,
                    "age_seconds": round(time.time() - m.created_at, 1),
                    "sender_id": m.sender_id,
                }
                for m in sorted(pending, key=lambda m: m.created_at)
            ],
        }

    # ── Internal helpers ──────────────────────────────────────────────

    def _purge_expired_locked(self) -> int:
        """Remove expired messages (caller must hold lock)."""
        expired = [mid for mid, msg in self._messages.items() if msg.is_expired()]
        for mid in expired:
            self._messages.pop(mid, None)
            future = self._question_futures.pop(mid, None)
            if future is not None:
                _end_question(
                    future, asyncio.TimeoutError("Question expired before agent responded")
                )
        if expired:
            logger.debug("Inbox: purged %d expired message(s)", len(expired))
        return len(expired)

    def _answered_locked(self, msg: InboxMessage) -> bool:
        """Whether *msg* is a question whose answer was submitted."""
        if msg.message_type != MessageType.QUESTION:
            return False
        future = self._question_futures.get(msg.message_id)
        return future is not None and future.done()

    def _evict_for_locked(self, incoming: InboxMessage) -> None:
        """Make room for *incoming* in a full inbox (caller holds the lock).

        Answered questions go first (they are done).  Otherwise evicts the
        oldest message of the lowest priority present.  Critical messages
        are never evicted, and nothing is evicted for a message of lower
        priority than the candidate.

        Raises:
            InboxFullError: Nothing can be evicted for *incoming*.
        """
        answered = [m for m in self._messages.values() if self._answered_locked(m)]
        if answered:
            done = min(answered, key=lambda m: m.created_at)
            self._messages.pop(done.message_id, None)
            self._question_futures.pop(done.message_id, None)
            return
        candidates = [msg for msg in self._messages.values() if msg.priority != "critical"]
        if not candidates:
            raise InboxFullError(
                f"Inbox is full ({self._max_messages} messages, all critical); "
                "wait for the agent to consume them or clear the inbox"
            )
        # Lowest priority first, then oldest first
        victim = min(candidates, key=lambda m: (_PRIORITY_ORDER[m.priority], m.created_at))
        if _PRIORITY_ORDER[incoming.priority] < _PRIORITY_ORDER[victim.priority]:
            raise InboxFullError(
                f"Inbox is full ({self._max_messages} messages) and every message in it has "
                f"a higher priority than {incoming.priority!r}; send it with a higher priority "
                "or wait for the agent to consume messages"
            )
        self._messages.pop(victim.message_id, None)
        future = self._question_futures.pop(victim.message_id, None)
        if future is not None:
            _end_question(future, InboxFullError("Question evicted from full inbox"))
        logger.info("Inbox: evicted %s message %s (inbox full)", victim.priority, victim.message_id)


def format_inbox_for_prompt(messages: list[InboxMessage]) -> str:
    """Format inbox messages as a system prompt block.

    Used by AgentProcess to inject messages into the agent's context
    before each invocation.  The block starts with
    ``=== OPERATOR MESSAGES ===`` and ends with
    ``=== END OPERATOR MESSAGES ===``; each message is a
    ``[<PRIORITY> <TYPE>] (from: <sender>, <age>, expires in <n>)`` line
    (priority omitted for ``normal``, sender and expiry when known)
    followed by the content.  Each question gets a line asking for an
    ``ANSWER Qn:`` prefix, numbered in block order (see
    :func:`prompt_questions` and :func:`parse_answers`).

    Args:
        messages: Pending messages from :meth:`MessageInbox.get_pending`,
            in the order they should appear.

    Returns:
        Formatted string block, or empty string if no messages.
    """
    if not messages:
        return ""

    lines = ["=== OPERATOR MESSAGES ===", ""]
    question_index = 0

    for msg in messages:
        age = time.time() - msg.created_at
        if age < 60:
            age_str = "just now"
        elif age < 3600:
            age_str = f"{int(age / 60)} min ago"
        else:
            age_str = f"{int(age / 3600)}h ago"

        sender = f"from: {msg.sender_id}, " if msg.sender_id else ""
        expires = ""
        if msg.expires_at:
            remaining = msg.expires_at - time.time()
            if remaining > 0:
                if remaining < 3600:
                    expires = f", expires in {int(remaining / 60)} min"
                else:
                    expires = f", expires in {int(remaining / 3600)}h"

        type_label = msg.message_type.value.upper()
        priority_label = msg.priority.upper() if msg.priority != "normal" else ""
        label = f"[{priority_label + ' ' if priority_label else ''}{type_label}]"

        lines.append(f"{label} ({sender}{age_str}{expires})")
        lines.append(msg.content)

        if msg.message_type == MessageType.QUESTION:
            question_index += 1
            lines.append(f'→ Please include your answer prefixed with "ANSWER Q{question_index}:"')
        lines.append("")

    lines.append("=== END OPERATOR MESSAGES ===")
    return "\n".join(lines)


def prompt_questions(messages: list[InboxMessage]) -> list[InboxMessage]:
    """The questions in *messages*, in the order the prompt numbers them.

    ``result[0]`` is ``Q1`` in :func:`format_inbox_for_prompt`'s block.
    """
    return [m for m in messages if m.message_type == MessageType.QUESTION]


_ANSWER_PATTERN = re.compile(r"ANSWER Q(\d+):\s*(.*?)(?=ANSWER Q\d+:|$)", re.DOTALL)


def parse_answers(text: str, questions: list[InboxMessage]) -> dict[str, str]:
    """Map the ``ANSWER Qn: ...`` parts of an agent reply to question IDs.

    Args:
        text: The agent's final reply.
        questions: :func:`prompt_questions` of the messages that were in
            the prompt.

    Returns:
        ``{message_id: answer}`` for every numbered answer that matches a
        question (out-of-range numbers are ignored; a repeated number
        keeps the first answer).
    """
    answers: dict[str, str] = {}
    for number, answer in _ANSWER_PATTERN.findall(text or ""):
        index = int(number) - 1
        if 0 <= index < len(questions):
            answers.setdefault(questions[index].message_id, answer.strip())
    return answers


def _text_windows(text: str, size: int = 500, overlap: int = 50) -> list[str]:
    """Overlapping windows of *text* (injection models read ~512 tokens)."""
    text = text.strip()
    if len(text) <= size:
        return [text] if text else []
    windows = []
    start = 0
    while start < len(text):
        windows.append(text[start : start + size])
        if start + size >= len(text):
            break
        start += size - overlap
    return windows


def _scans_whole_text(scanner: Any) -> bool:
    """Whether one scan of the whole text covers everything for *scanner*.

    :class:`~promptise.guardrails.PromptiseSecurityScanner` windows its
    length-limited model heads itself (injection, toxicity, content
    safety, NER) and runs its regex heads (credentials, PII) on the whole
    text.  Another scanner may have a model that reads only the start of
    a long text, so it also gets the text in windows.
    """
    try:
        from promptise.guardrails import PromptiseSecurityScanner
    except ImportError:  # pragma: no cover - guardrails ships with the package
        return False
    return isinstance(scanner, PromptiseSecurityScanner)


def _scan_pieces(text: str, scanner: Any) -> list[str]:
    """The texts *scanner* reads for *text*: the whole text first, windows if needed.

    The whole text always comes first, so a pattern match (a credential
    longer than the windows' overlap, say) is never cut in two at a
    window boundary.  The windows follow only for a scanner that does not
    window its models itself (see :func:`_scans_whole_text`).
    """
    if not text.strip():
        return []
    if _scans_whole_text(scanner):
        return [text]
    windows = _text_windows(text)
    return [text] if len(windows) <= 1 else [text, *windows]


async def scan_inbox_message(message: InboxMessage, scanner: Any) -> str | None:
    """Scan a message's content with a guardrail scanner.

    Used for ``InboxConfig.scan_with_guardrails``.  The whole content is
    scanned as input, so a credential is never cut in two at a window
    boundary; a scanner that does not window its models itself also gets
    overlapping 500-character windows (see :func:`_scans_whole_text`).
    The scanner's own input policy applies, as
    :meth:`~promptise.guardrails.PromptiseSecurityScanner.check_input`
    applies it to an agent's input:

    * a ``BLOCK`` finding refuses the message;
    * with ``redact_input=True``, a redacted finding (a credential, PII)
      is masked: ``message.content`` is replaced with the scanner's
      redacted text, so the agent never sees the value;
    * a credential that is only a warning (the default on input) lets the
      message through and logs one warning naming the finding categories,
      never the matched text.

    Args:
        message: The message to scan.  Its ``content`` is replaced when
            the scanner redacts it.
        scanner: An object with ``scan_text(text, direction="input")``
            returning a report with ``passed`` and ``blocked`` (such as
            :class:`~promptise.guardrails.PromptiseSecurityScanner`), or
            with ``check_input(text)`` that raises when it blocks.

    Returns:
        ``None`` when the message is accepted, otherwise a short reason (the
        flagged finding categories).
    """
    scan_text = getattr(scanner, "scan_text", None)
    check_input = getattr(scanner, "check_input", None)
    if scan_text is None and check_input is None:
        raise TypeError(
            f"{type(scanner).__name__} has neither scan_text() nor check_input(); "
            "pass a guardrail scanner such as PromptiseSecurityScanner"
        )
    redacted: str | None = None
    credential_warnings: set[str] = set()
    for index, piece in enumerate(_scan_pieces(message.content, scanner)):
        if scan_text is not None:
            report = await scan_text(piece, direction="input")
            if not getattr(report, "passed", True):
                blocked = getattr(report, "blocked", None) or []
                return ", ".join(sorted({str(f.category) for f in blocked})) or "blocked"
            if index == 0:  # the whole content
                redacted = getattr(report, "redacted_text", None)
            for finding in getattr(report, "findings", None) or []:
                action = getattr(finding, "action", None)
                if (
                    getattr(finding, "detector", None) == "credential"
                    and getattr(action, "value", action) == "warn"
                ):
                    credential_warnings.add(str(finding.category))
        else:
            try:
                await check_input(piece)
            except Exception as exc:
                return type(exc).__name__
    if redacted is not None and getattr(scanner, "redact_input", False):
        message.content = redacted
    if credential_warnings:
        logger.warning(
            "Inbox: accepted %s message %s from %s with credentials in it (%s); they "
            "reach the agent's prompt. Set redact_input=True on the scanner to mask "
            "them; with CredentialDetector(action=Action.BLOCK) as well, they are refused",
            message.message_type.value,
            message.message_id,
            message.sender_id,
            ", ".join(sorted(credential_warnings)),
        )
    return None
