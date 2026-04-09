"""Tests for promptise.runtime.callbacks — RuntimeCallbackHandler."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from promptise.runtime.callbacks import RuntimeCallbackHandler


class TestInit:
    def test_creation_with_both(self) -> None:
        budget = MagicMock()
        health = MagicMock()
        handler = RuntimeCallbackHandler(budget=budget, health=health)
        assert handler._budget is budget
        assert handler._health is health
        assert handler.pending_violations == []

    def test_creation_with_neither(self) -> None:
        handler = RuntimeCallbackHandler()
        assert handler._budget is None
        assert handler._health is None

    def test_creation_budget_only(self) -> None:
        budget = MagicMock()
        handler = RuntimeCallbackHandler(budget=budget)
        assert handler._budget is budget
        assert handler._health is None


class TestReset:
    def test_clears_pending_violations(self) -> None:
        handler = RuntimeCallbackHandler()
        handler.pending_violations.append(MagicMock())
        handler.pending_violations.append(MagicMock())
        assert len(handler.pending_violations) == 2
        handler.reset()
        assert handler.pending_violations == []


class TestOnToolStart:
    @pytest.mark.asyncio
    async def test_records_budget_tool_call(self) -> None:
        budget = AsyncMock()
        budget.record_tool_call = AsyncMock(return_value=None)
        handler = RuntimeCallbackHandler(budget=budget)

        await handler.on_tool_start({"name": "search"}, '{"q": "test"}')

        budget.record_tool_call.assert_awaited_once_with("search")

    @pytest.mark.asyncio
    async def test_collects_budget_violation(self) -> None:
        violation = MagicMock()
        budget = AsyncMock()
        budget.record_tool_call = AsyncMock(return_value=violation)
        handler = RuntimeCallbackHandler(budget=budget)

        await handler.on_tool_start({"name": "tool"}, "{}")

        assert len(handler.pending_violations) == 1
        assert handler.pending_violations[0] is violation

    @pytest.mark.asyncio
    async def test_records_health_tool_call(self) -> None:
        health = AsyncMock()
        health.record_tool_call = AsyncMock(return_value=None)
        handler = RuntimeCallbackHandler(health=health)

        await handler.on_tool_start({"name": "search"}, '{"q": "test"}')

        health.record_tool_call.assert_awaited_once()
        call_args = health.record_tool_call.call_args
        assert call_args[0][0] == "search"
        assert call_args[0][1] == {"q": "test"}

    @pytest.mark.asyncio
    async def test_extracts_tool_name_from_id_fallback(self) -> None:
        budget = AsyncMock()
        budget.record_tool_call = AsyncMock(return_value=None)
        handler = RuntimeCallbackHandler(budget=budget)

        await handler.on_tool_start({"id": "fallback_name"}, "{}")

        budget.record_tool_call.assert_awaited_once_with("fallback_name")

    @pytest.mark.asyncio
    async def test_empty_serialized_dict(self) -> None:
        budget = AsyncMock()
        budget.record_tool_call = AsyncMock(return_value=None)
        handler = RuntimeCallbackHandler(budget=budget)

        await handler.on_tool_start({}, "{}")

        budget.record_tool_call.assert_awaited_once_with("")

    @pytest.mark.asyncio
    async def test_non_dict_serialized(self) -> None:
        budget = AsyncMock()
        budget.record_tool_call = AsyncMock(return_value=None)
        handler = RuntimeCallbackHandler(budget=budget)

        await handler.on_tool_start("not_a_dict", "{}")

        budget.record_tool_call.assert_awaited_once_with("")

    @pytest.mark.asyncio
    async def test_budget_error_logged_not_raised(self) -> None:
        budget = AsyncMock()
        budget.record_tool_call = AsyncMock(side_effect=RuntimeError("boom"))
        handler = RuntimeCallbackHandler(budget=budget)

        # Should not raise
        await handler.on_tool_start({"name": "tool"}, "{}")

    @pytest.mark.asyncio
    async def test_health_error_logged_not_raised(self) -> None:
        health = AsyncMock()
        health.record_tool_call = AsyncMock(side_effect=RuntimeError("boom"))
        handler = RuntimeCallbackHandler(health=health)

        await handler.on_tool_start({"name": "tool"}, "{}")

    @pytest.mark.asyncio
    async def test_skipped_when_no_budget_no_health(self) -> None:
        handler = RuntimeCallbackHandler()
        # Should complete without error
        await handler.on_tool_start({"name": "tool"}, "{}")

    @pytest.mark.asyncio
    async def test_parses_dict_input(self) -> None:
        health = AsyncMock()
        health.record_tool_call = AsyncMock(return_value=None)
        handler = RuntimeCallbackHandler(health=health)

        await handler.on_tool_start({"name": "t"}, {"already": "parsed"})

        call_args = health.record_tool_call.call_args
        assert call_args[0][1] == {"already": "parsed"}

    @pytest.mark.asyncio
    async def test_parses_invalid_json_input(self) -> None:
        health = AsyncMock()
        health.record_tool_call = AsyncMock(return_value=None)
        handler = RuntimeCallbackHandler(health=health)

        await handler.on_tool_start({"name": "t"}, "not json at all")

        call_args = health.record_tool_call.call_args
        assert call_args[0][1] == {"raw": "not json at all"}


class TestOnToolEnd:
    @pytest.mark.asyncio
    async def test_records_response_for_health(self) -> None:
        health = AsyncMock()
        health.record_response = AsyncMock(return_value=None)
        handler = RuntimeCallbackHandler(health=health)

        await handler.on_tool_end("search result text")

        health.record_response.assert_awaited_once_with("search result text")

    @pytest.mark.asyncio
    async def test_handles_none_output(self) -> None:
        health = AsyncMock()
        health.record_response = AsyncMock(return_value=None)
        handler = RuntimeCallbackHandler(health=health)

        await handler.on_tool_end(None)

        health.record_response.assert_awaited_once_with("")

    @pytest.mark.asyncio
    async def test_noop_when_no_health(self) -> None:
        handler = RuntimeCallbackHandler()
        await handler.on_tool_end("output")  # Should not raise


class TestOnLlmStart:
    @pytest.mark.asyncio
    async def test_records_budget_llm_turn(self) -> None:
        budget = AsyncMock()
        budget.record_llm_turn = AsyncMock(return_value=None)
        handler = RuntimeCallbackHandler(budget=budget)

        await handler.on_llm_start({}, ["prompt"])

        budget.record_llm_turn.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_collects_llm_violation(self) -> None:
        violation = MagicMock()
        budget = AsyncMock()
        budget.record_llm_turn = AsyncMock(return_value=violation)
        handler = RuntimeCallbackHandler(budget=budget)

        await handler.on_llm_start({}, ["prompt"])

        assert len(handler.pending_violations) == 1

    @pytest.mark.asyncio
    async def test_budget_error_logged_not_raised(self) -> None:
        budget = AsyncMock()
        budget.record_llm_turn = AsyncMock(side_effect=RuntimeError("fail"))
        handler = RuntimeCallbackHandler(budget=budget)

        await handler.on_llm_start({}, ["prompt"])

    @pytest.mark.asyncio
    async def test_noop_when_no_budget(self) -> None:
        handler = RuntimeCallbackHandler()
        await handler.on_llm_start({}, ["prompt"])


class TestParseToolArgs:
    def test_dict_passthrough(self) -> None:
        result = RuntimeCallbackHandler._parse_tool_args({"key": "val"})
        assert result == {"key": "val"}

    def test_valid_json_string(self) -> None:
        result = RuntimeCallbackHandler._parse_tool_args('{"key": "val"}')
        assert result == {"key": "val"}

    def test_invalid_json_string(self) -> None:
        result = RuntimeCallbackHandler._parse_tool_args("not json")
        assert result == {"raw": "not json"}

    def test_non_dict_json(self) -> None:
        result = RuntimeCallbackHandler._parse_tool_args("[1, 2, 3]")
        assert result == {"raw": "[1, 2, 3]"}

    def test_non_string_non_dict(self) -> None:
        result = RuntimeCallbackHandler._parse_tool_args(12345)
        assert result == {}
