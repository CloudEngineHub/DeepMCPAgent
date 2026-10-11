"""Tests for promptise.runtime.api — Agent Orchestration API."""

from __future__ import annotations

import asyncio
import time

import pytest

from promptise.runtime import (
    AgentRuntime,
    InboxConfig,
    ProcessConfig,
)
from promptise.runtime.api import OrchestrationAPI, _json_dumps
from promptise.runtime.inbox import InboxMessage, InboxResponse, MessageInbox, MessageType

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


@pytest.fixture
def runtime():
    return AgentRuntime()


@pytest.fixture
def api(runtime):
    return OrchestrationAPI(runtime, host="127.0.0.1", port=0)


# ---------------------------------------------------------------------------
# MessageInbox unit tests
# ---------------------------------------------------------------------------


class TestMessageInbox:
    @pytest.mark.asyncio
    async def test_add_and_get_pending(self):
        inbox = MessageInbox()
        msg = InboxMessage(content="Hello", message_type=MessageType.CONTEXT)
        msg_id = await inbox.add(msg)
        assert msg_id == msg.message_id

        pending = await inbox.get_pending()
        assert len(pending) == 1
        assert pending[0].content == "Hello"

    @pytest.mark.asyncio
    async def test_priority_ordering(self):
        inbox = MessageInbox()
        await inbox.add(InboxMessage(content="low", priority="low"))
        await inbox.add(InboxMessage(content="critical", priority="critical"))
        await inbox.add(InboxMessage(content="normal", priority="normal"))

        pending = await inbox.get_pending()
        assert pending[0].content == "critical"
        assert pending[1].content == "normal"
        assert pending[2].content == "low"

    @pytest.mark.asyncio
    async def test_ttl_expiry(self):
        inbox = MessageInbox(default_ttl=0)  # No default TTL
        msg = InboxMessage(content="expires", expires_at=time.time() - 1)  # Already expired
        await inbox.add(msg)

        pending = await inbox.get_pending()
        assert len(pending) == 0  # Expired, purged

    @pytest.mark.asyncio
    async def test_max_messages_eviction(self):
        inbox = MessageInbox(max_messages=2, default_ttl=0)
        await inbox.add(InboxMessage(content="first", priority="low"))
        await inbox.add(InboxMessage(content="second", priority="normal"))
        await inbox.add(InboxMessage(content="third", priority="high"))

        pending = await inbox.get_pending()
        assert len(pending) == 2
        # Lowest priority (first) should be evicted
        contents = {m.content for m in pending}
        assert "first" not in contents
        assert "third" in contents

    @pytest.mark.asyncio
    async def test_critical_never_evicted(self):
        """Critical messages aren't evicted when inbox is full."""
        inbox = MessageInbox(max_messages=3, default_ttl=0)
        await inbox.add(InboxMessage(content="low_msg", priority="low"))
        await inbox.add(InboxMessage(content="critical1", priority="critical"))
        await inbox.add(InboxMessage(content="critical2", priority="critical"))
        # Inbox full (3/3). Adding 4th should evict "low_msg", not criticals.
        await inbox.add(InboxMessage(content="normal_msg", priority="normal"))

        pending = await inbox.get_pending()
        assert len(pending) == 3
        contents = {m.content for m in pending}
        assert "low_msg" not in contents  # Evicted (lowest priority)
        assert "critical1" in contents
        assert "critical2" in contents

    @pytest.mark.asyncio
    async def test_rate_limiting(self):
        inbox = MessageInbox(rate_limit_per_sender=2)
        await inbox.add(InboxMessage(content="1", sender_id="alice"))
        await inbox.add(InboxMessage(content="2", sender_id="alice"))
        with pytest.raises(ValueError, match="Rate limit"):
            await inbox.add(InboxMessage(content="3", sender_id="alice"))

    @pytest.mark.asyncio
    async def test_different_senders_independent(self):
        inbox = MessageInbox(rate_limit_per_sender=1)
        await inbox.add(InboxMessage(content="alice", sender_id="alice"))
        await inbox.add(InboxMessage(content="bob", sender_id="bob"))
        # Both succeed — independent limits

    @pytest.mark.asyncio
    async def test_question_and_response(self):
        inbox = MessageInbox()
        msg = InboxMessage(
            content="What's the status?",
            message_type=MessageType.QUESTION,
        )
        msg_id = await inbox.add(msg)

        questions = await inbox.get_questions()
        assert len(questions) == 1

        # Submit response
        response = InboxResponse(
            question_id=msg_id,
            content="All systems healthy.",
        )
        await inbox.submit_response(msg_id, response)

        # Wait for response
        result = await inbox.wait_for_response(msg_id, timeout=1)
        assert result.content == "All systems healthy."

    @pytest.mark.asyncio
    async def test_question_timeout(self):
        inbox = MessageInbox()
        msg = InboxMessage(
            content="Hello?",
            message_type=MessageType.QUESTION,
        )
        msg_id = await inbox.add(msg)

        with pytest.raises(asyncio.TimeoutError):
            await inbox.wait_for_response(msg_id, timeout=0.1)

    @pytest.mark.asyncio
    async def test_clear(self):
        inbox = MessageInbox()
        await inbox.add(InboxMessage(content="msg1"))
        await inbox.add(InboxMessage(content="msg2"))
        await inbox.clear()
        pending = await inbox.get_pending()
        assert len(pending) == 0

    @pytest.mark.asyncio
    async def test_status(self):
        inbox = MessageInbox()
        await inbox.add(InboxMessage(content="msg1"))
        await inbox.add(
            InboxMessage(
                content="question",
                message_type=MessageType.QUESTION,
            )
        )
        status = await inbox.status()
        assert status["pending_messages"] == 2
        assert status["pending_questions"] == 1

    @pytest.mark.asyncio
    async def test_message_truncation(self):
        inbox = MessageInbox(max_message_length=10)
        msg = InboxMessage(content="A" * 100)
        await inbox.add(msg)
        pending = await inbox.get_pending()
        assert len(pending[0].content) == 10

    @pytest.mark.asyncio
    async def test_mark_processed(self):
        inbox = MessageInbox(default_ttl=0)
        msg = InboxMessage(content="process me")
        await inbox.add(msg)
        await inbox.mark_processed(msg.message_id)
        pending = await inbox.get_pending()
        assert len(pending) == 0


# ---------------------------------------------------------------------------
# MessageType
# ---------------------------------------------------------------------------


class TestMessageType:
    def test_values(self):
        assert MessageType.DIRECTIVE.value == "directive"
        assert MessageType.CONTEXT.value == "context"
        assert MessageType.QUESTION.value == "question"
        assert MessageType.CORRECTION.value == "correction"


# ---------------------------------------------------------------------------
# InboxMessage
# ---------------------------------------------------------------------------


class TestInboxMessage:
    def test_construction(self):
        msg = InboxMessage(content="test")
        assert msg.content == "test"
        assert msg.message_type == MessageType.CONTEXT
        assert msg.priority == "normal"
        assert msg.message_id.startswith("msg_")
        assert isinstance(msg.created_at, float)

    def test_to_dict(self):
        msg = InboxMessage(content="test", sender_id="alice")
        d = msg.to_dict()
        assert d["content"] == "test"
        assert d["sender_id"] == "alice"
        assert d["message_type"] == "context"

    def test_is_expired(self):
        msg = InboxMessage(content="test", expires_at=time.time() - 1)
        assert msg.is_expired() is True

        msg2 = InboxMessage(content="test", expires_at=time.time() + 3600)
        assert msg2.is_expired() is False

        msg3 = InboxMessage(content="test")  # No expiry
        assert msg3.is_expired() is False


# ---------------------------------------------------------------------------
# format_inbox_for_prompt
# ---------------------------------------------------------------------------


class TestFormatInboxForPrompt:
    def test_empty_messages(self):
        from promptise.runtime.inbox import format_inbox_for_prompt

        assert format_inbox_for_prompt([]) == ""

    def test_formats_messages(self):
        from promptise.runtime.inbox import format_inbox_for_prompt

        messages = [
            InboxMessage(
                content="Ignore staging alerts.",
                message_type=MessageType.DIRECTIVE,
                priority="high",
                sender_id="ops",
            ),
            InboxMessage(
                content="What's the status?",
                message_type=MessageType.QUESTION,
            ),
        ]
        result = format_inbox_for_prompt(messages)
        assert "OPERATOR MESSAGES" in result
        assert "Ignore staging alerts" in result
        assert "DIRECTIVE" in result
        assert "QUESTION" in result
        assert "ANSWER Q1:" in result


# ---------------------------------------------------------------------------
# OrchestrationAPI construction
# ---------------------------------------------------------------------------


class TestAPIConstruction:
    def test_requires_auth_on_non_localhost(self):
        runtime = AgentRuntime()
        with pytest.raises(ValueError, match="auth_token is required"):
            OrchestrationAPI(runtime, host="0.0.0.0", port=9100)

    def test_localhost_no_auth_ok(self):
        runtime = AgentRuntime()
        api = OrchestrationAPI(runtime, host="127.0.0.1", port=9100)
        assert api._auth_token is None

    def test_non_localhost_with_auth_ok(self):
        runtime = AgentRuntime()
        api = OrchestrationAPI(runtime, host="0.0.0.0", port=9100, auth_token="secret")
        assert api._auth_token == "secret"


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


class TestAuth:
    def test_check_auth_no_token(self):
        runtime = AgentRuntime()
        api = OrchestrationAPI(runtime, host="127.0.0.1")
        # No auth required on localhost
        from unittest.mock import MagicMock

        req = MagicMock()
        req.headers = {}
        assert api._check_auth(req) is True

    def test_check_auth_valid_token(self):
        runtime = AgentRuntime()
        api = OrchestrationAPI(runtime, host="0.0.0.0", auth_token="my-secret")
        from unittest.mock import MagicMock

        req = MagicMock()
        req.headers = {"Authorization": "Bearer my-secret"}
        assert api._check_auth(req) is True

    def test_check_auth_invalid_token(self):
        runtime = AgentRuntime()
        api = OrchestrationAPI(runtime, host="0.0.0.0", auth_token="my-secret")
        from unittest.mock import MagicMock

        req = MagicMock()
        req.headers = {"Authorization": "Bearer wrong-token"}
        assert api._check_auth(req) is False

    def test_check_auth_missing_header(self):
        runtime = AgentRuntime()
        api = OrchestrationAPI(runtime, host="0.0.0.0", auth_token="my-secret")
        from unittest.mock import MagicMock

        req = MagicMock()
        req.headers = {}
        assert api._check_auth(req) is False


# ---------------------------------------------------------------------------
# Process name validation
# ---------------------------------------------------------------------------


class TestProcessNameValidation:
    def test_valid_names(self):
        from promptise.runtime.api import _PROCESS_NAME_RE

        assert _PROCESS_NAME_RE.match("monitor")
        assert _PROCESS_NAME_RE.match("data-watcher")
        assert _PROCESS_NAME_RE.match("agent_v2")
        assert _PROCESS_NAME_RE.match("a")

    def test_invalid_names(self):
        from promptise.runtime.api import _PROCESS_NAME_RE

        assert not _PROCESS_NAME_RE.match("")
        assert not _PROCESS_NAME_RE.match("123")  # Starts with number
        assert not _PROCESS_NAME_RE.match("-bad")  # Starts with hyphen
        assert not _PROCESS_NAME_RE.match("a" * 65)  # Too long
        assert not _PROCESS_NAME_RE.match("has spaces")


# ---------------------------------------------------------------------------
# JSON serialization
# ---------------------------------------------------------------------------


class TestJsonDumps:
    def test_handles_non_serializable(self):
        from datetime import datetime

        result = _json_dumps({"time": datetime(2026, 1, 1)})
        assert "2026" in result


# ---------------------------------------------------------------------------
# InboxConfig
# ---------------------------------------------------------------------------


class TestInboxConfig:
    def test_defaults(self):
        config = InboxConfig()
        assert config.enabled is False
        assert config.max_messages == 50
        assert config.default_ttl == 3600

    def test_enabled(self):
        config = InboxConfig(enabled=True, max_messages=100)
        assert config.enabled is True
        assert config.max_messages == 100


# ---------------------------------------------------------------------------
# ProcessConfig with inbox
# ---------------------------------------------------------------------------


class TestProcessConfigInbox:
    def test_default_inbox_disabled(self):
        config = ProcessConfig()
        assert config.inbox.enabled is False

    def test_enabled_inbox(self):
        config = ProcessConfig(inbox=InboxConfig(enabled=True, max_messages=25))
        assert config.inbox.enabled is True
        assert config.inbox.max_messages == 25


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------


class TestExports:
    def test_imports_from_runtime(self):
        from promptise.runtime import (
            InboxConfig,
            InboxMessage,
            InboxResponse,
            MessageInbox,
            MessageType,
            OrchestrationAPI,
        )

        assert OrchestrationAPI is not None
        assert MessageInbox is not None
        assert InboxMessage is not None
        assert InboxResponse is not None
        assert MessageType is not None
        assert InboxConfig is not None


# ---------------------------------------------------------------------------
# Journal and instructions endpoints against a running process
# ---------------------------------------------------------------------------


class _InstructedAgent:
    """Stands in for build_agent(): replies with the instructions it was built with."""

    def __init__(self, instructions: str) -> None:
        self.instructions = instructions
        self.shut_down = False

    async def ainvoke(self, input: dict, config: object = None) -> dict:
        return {"messages": [{"role": "assistant", "content": self.instructions}]}

    async def shutdown(self) -> None:
        self.shut_down = True


async def _http(method: str, url: str, body: dict | None = None) -> tuple[int, dict]:
    from aiohttp import ClientSession

    async with ClientSession() as session:
        async with session.request(method, url, json=body) as resp:
            return resp.status, await resp.json(content_type=None)


class TestJournalEndpoint:
    async def test_reads_the_entries_filed_under_the_process_name(self, tmp_path) -> None:
        from promptise.runtime import JournalConfig

        runtime = AgentRuntime()
        # A default process_id: a random UUID, not the name
        process = await runtime.add_process(
            "worker", ProcessConfig(journal=JournalConfig(level="full", path=str(tmp_path)))
        )
        assert process.process_id != "worker"

        async def build() -> None:
            process._agent = _InstructedAgent("x")

        process._build_agent = build
        await process.start()
        api = OrchestrationAPI(runtime, host="127.0.0.1", port=0)
        await api.start()
        try:
            status, body = await _http(
                "GET", f"http://127.0.0.1:{api.port}/api/v1/processes/worker/journal"
            )
        finally:
            await api.stop()
            await runtime.stop_all()
        assert status == 200
        assert body["count"] >= 2
        assert {e["entry_type"] for e in body["entries"]} == {"state_transition"}


class TestUpdateInstructionsEndpoint:
    async def test_next_invocation_uses_the_new_instructions(self, monkeypatch) -> None:
        import promptise.agent
        from promptise.runtime.triggers.base import TriggerEvent

        built: list[_InstructedAgent] = []

        async def fake_build_agent(**kwargs):
            agent = _InstructedAgent(kwargs["instructions"])
            built.append(agent)
            return agent

        monkeypatch.setattr(promptise.agent, "build_agent", fake_build_agent)

        runtime = AgentRuntime()
        process = await runtime.add_process("worker", ProcessConfig(instructions="Reply OLD."))
        replies: list[str] = []
        original = process._invoke_agent

        async def record(event):
            result = await original(event)
            replies.append(result["messages"][-1]["content"])
            return result

        process._invoke_agent = record
        await process.start()
        api = OrchestrationAPI(runtime, host="127.0.0.1", port=0)
        await api.start()

        async def run_once() -> None:
            count = len(replies)
            await process.inject(TriggerEvent(trigger_id="t", trigger_type="manual", payload={}))
            for _ in range(200):
                if len(replies) > count:
                    return
                await asyncio.sleep(0.01)
            raise AssertionError("the invocation did not run")

        try:
            await run_once()
            status, body = await _http(
                "PATCH",
                f"http://127.0.0.1:{api.port}/api/v1/processes/worker/instructions",
                {"instructions": "Reply PATCHED."},
            )
            assert status == 200
            assert body["effective_from"] == "next_invocation"
            await run_once()
            await run_once()
        finally:
            await api.stop()
            await runtime.stop_all()

        assert replies == ["Reply OLD.", "Reply PATCHED.", "Reply PATCHED."]
        assert process.config.instructions == "Reply PATCHED."
        # Rebuilt once, without a restart; the replaced agent was shut down.
        assert [a.instructions for a in built] == ["Reply OLD.", "Reply PATCHED."]
        assert built[0].shut_down is True

    async def test_rejects_non_string_instructions(self, runtime) -> None:
        await runtime.add_process("worker", ProcessConfig(instructions="keep"))
        api = OrchestrationAPI(runtime, host="127.0.0.1", port=0)
        await api.start()
        try:
            status, _ = await _http(
                "PATCH",
                f"http://127.0.0.1:{api.port}/api/v1/processes/worker/instructions",
                {"instructions": ["not", "a", "string"]},
            )
        finally:
            await api.stop()
        assert status == 422
        assert runtime.get_process("worker").config.instructions == "keep"
