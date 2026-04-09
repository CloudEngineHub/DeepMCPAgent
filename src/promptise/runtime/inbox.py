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
"""

from __future__ import annotations

import asyncio
import logging
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
]


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

    def check(self, sender_id: str | None) -> None:
        """Raise ValueError if sender has exceeded rate limit."""
        if sender_id is None or self._max <= 0:
            return
        now = time.time()
        cutoff = now - 3600  # 1 hour window

        # Prune old entries for this sender
        entries = self._windows.get(sender_id, [])
        entries = [t for t in entries if t > cutoff]
        self._windows[sender_id] = entries

        if len(entries) >= self._max:
            raise ValueError(
                f"Rate limit exceeded: sender '{sender_id}' sent "
                f"{len(entries)} messages in the last hour (limit: {self._max})"
            )
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

    Args:
        max_messages: Maximum messages in the inbox. When full,
            lowest-priority messages are evicted.
        max_message_length: Maximum characters per message.
        default_ttl: Default time-to-live in seconds (0 = no expiry).
        max_ttl: Maximum allowed TTL in seconds.
        rate_limit_per_sender: Maximum messages per sender per hour.
    """

    def __init__(
        self,
        *,
        max_messages: int = 50,
        max_message_length: int = 2000,
        default_ttl: float = 3600,
        max_ttl: float = 86400,
        rate_limit_per_sender: int = 20,
    ) -> None:
        self._max_messages = max_messages
        self._max_message_length = max_message_length
        self._default_ttl = default_ttl
        self._max_ttl = max_ttl

        self._messages: dict[str, InboxMessage] = {}
        self._question_futures: dict[str, asyncio.Future[InboxResponse]] = {}
        self._lock = asyncio.Lock()
        self._rate_limiter = _SenderRateLimiter(rate_limit_per_sender)

    async def add(self, message: InboxMessage) -> str:
        """Add a message to the inbox.

        Args:
            message: The message to add.

        Returns:
            The message_id.

        Raises:
            ValueError: If rate limit exceeded or content too long.
        """
        # Validate content length
        if len(message.content) > self._max_message_length:
            message.content = message.content[: self._max_message_length]
            logger.debug("Inbox: message truncated to %d chars", self._max_message_length)

        # Rate limit check
        self._rate_limiter.check(message.sender_id)

        # Set expiry if not set
        if message.expires_at is None and self._default_ttl > 0:
            message.expires_at = time.time() + min(self._default_ttl, self._max_ttl)
        elif message.expires_at is not None:
            # Clamp to max TTL
            max_expire = time.time() + self._max_ttl
            if message.expires_at > max_expire:
                message.expires_at = max_expire

        async with self._lock:
            # Purge expired first
            self._purge_expired_locked()

            # Evict lowest-priority if at capacity
            if len(self._messages) >= self._max_messages:
                self._evict_lowest_priority()

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
        Does NOT remove messages — call :meth:`mark_processed` after
        the agent has seen them.
        """
        async with self._lock:
            self._purge_expired_locked()
            messages = list(self._messages.values())

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

    async def wait_for_response(self, question_id: str, timeout: float = 300) -> InboxResponse:
        """Wait for the agent to respond to a question.

        Args:
            question_id: The message_id of the question.
            timeout: Maximum seconds to wait.

        Returns:
            The agent's response.

        Raises:
            KeyError: If no pending question with this ID.
            asyncio.TimeoutError: If timeout expires.
        """
        async with self._lock:
            future = self._question_futures.get(question_id)
            if future is None:
                raise KeyError(f"No pending question with id {question_id!r}")

        return await asyncio.wait_for(future, timeout=timeout)

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

    async def purge_expired(self) -> int:
        """Remove all expired messages. Returns count removed."""
        async with self._lock:
            return self._purge_expired_locked()

    async def clear(self) -> None:
        """Clear all messages and cancel pending question futures."""
        async with self._lock:
            # Cancel all pending question futures
            for future in self._question_futures.values():
                if not future.done():
                    future.cancel()
            self._messages.clear()
            self._question_futures.clear()

    async def status(self) -> dict[str, Any]:
        """Get inbox status summary."""
        async with self._lock:
            self._purge_expired_locked()
            pending = list(self._messages.values())
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
            if future and not future.done():
                future.set_exception(
                    asyncio.TimeoutError("Question expired before agent responded")
                )
        if expired:
            logger.debug("Inbox: purged %d expired message(s)", len(expired))
        return len(expired)

    def _evict_lowest_priority(self) -> None:
        """Remove the lowest-priority, oldest message (caller holds lock).

        Critical messages are never evicted.
        """
        candidates = [
            (mid, msg) for mid, msg in self._messages.items() if msg.priority != "critical"
        ]
        if not candidates:
            return  # All critical — don't evict
        # Sort by priority (ascending) then age (oldest first)
        candidates.sort(key=lambda x: (_PRIORITY_ORDER.get(x[1].priority, 1), -x[1].created_at))
        evict_id = candidates[0][0]
        self._messages.pop(evict_id, None)
        future = self._question_futures.pop(evict_id, None)
        if future and not future.done():
            future.set_exception(ValueError("Message evicted from full inbox"))
        logger.debug("Inbox: evicted message %s (inbox full)", evict_id)


def format_inbox_for_prompt(messages: list[InboxMessage]) -> str:
    """Format inbox messages as a system prompt block.

    Used by AgentProcess to inject messages into the agent's context
    before each invocation.

    Args:
        messages: Pending messages from :meth:`MessageInbox.get_pending`.

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
