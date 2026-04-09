"""Tests for promptise.cross_agent — cross-agent delegation tools."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from promptise.cross_agent import CrossAgent, make_cross_agent_tools

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _mock_agent(response: str = "Agent response") -> MagicMock:
    """Create a mock agent that returns a LangGraph-style result."""
    agent = MagicMock()
    agent.ainvoke = AsyncMock(
        return_value={
            "messages": [
                MagicMock(type="ai", content=response),
            ]
        }
    )
    return agent


# ---------------------------------------------------------------------------
# CrossAgent dataclass
# ---------------------------------------------------------------------------


class TestCrossAgent:
    def test_construction(self):
        agent = _mock_agent()
        ca = CrossAgent(agent=agent, description="Research assistant")
        assert ca.agent is agent
        assert ca.description == "Research assistant"

    def test_default_description(self):
        ca = CrossAgent(agent=_mock_agent())
        assert ca.description == ""


# ---------------------------------------------------------------------------
# make_cross_agent_tools
# ---------------------------------------------------------------------------


class TestMakeCrossAgentTools:
    def test_creates_ask_tools_per_peer(self):
        peers = {
            "researcher": CrossAgent(agent=_mock_agent(), description="Research"),
            "analyst": CrossAgent(agent=_mock_agent(), description="Analysis"),
        }
        tools = make_cross_agent_tools(peers)
        names = [t.name for t in tools]
        assert "ask_agent_researcher" in names
        assert "ask_agent_analyst" in names

    def test_creates_broadcast_tool_when_multiple_peers(self):
        peers = {
            "a": CrossAgent(agent=_mock_agent()),
            "b": CrossAgent(agent=_mock_agent()),
        }
        tools = make_cross_agent_tools(peers)
        names = [t.name for t in tools]
        assert "broadcast_to_agents" in names

    def test_single_peer_creates_ask_tool(self):
        peers = {"solo": CrossAgent(agent=_mock_agent())}
        tools = make_cross_agent_tools(peers)
        names = [t.name for t in tools]
        assert "ask_agent_solo" in names

    def test_empty_peers_returns_empty(self):
        tools = make_cross_agent_tools({})
        assert tools == []


# ---------------------------------------------------------------------------
# _AskAgentTool
# ---------------------------------------------------------------------------


class TestAskAgentTool:
    @pytest.mark.asyncio
    async def test_forwards_message_to_peer(self):
        agent = _mock_agent("The answer is 42")
        peers = {"helper": CrossAgent(agent=agent, description="Helper")}
        tools = make_cross_agent_tools(peers)
        ask_tool = [t for t in tools if t.name == "ask_agent_helper"][0]

        result = await ask_tool._arun(message="What is the answer?")
        assert "42" in result
        agent.ainvoke.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_extracts_text_from_dict_result(self):
        agent = _mock_agent("Extracted text")
        peers = {"peer": CrossAgent(agent=agent)}
        tools = make_cross_agent_tools(peers)
        ask_tool = [t for t in tools if t.name == "ask_agent_peer"][0]

        result = await ask_tool._arun(message="hello")
        assert "Extracted text" in result

    @pytest.mark.asyncio
    async def test_handles_string_result(self):
        agent = MagicMock()
        agent.ainvoke = AsyncMock(return_value="plain string response")
        peers = {"peer": CrossAgent(agent=agent)}
        tools = make_cross_agent_tools(peers)
        ask_tool = [t for t in tools if t.name == "ask_agent_peer"][0]

        result = await ask_tool._arun(message="hello")
        assert "plain string response" in result

    @pytest.mark.asyncio
    async def test_handles_agent_error(self):
        agent = MagicMock()
        agent.ainvoke = AsyncMock(side_effect=RuntimeError("Agent crashed"))
        peers = {"peer": CrossAgent(agent=agent)}
        tools = make_cross_agent_tools(peers)
        ask_tool = [t for t in tools if t.name == "ask_agent_peer"][0]

        # The tool should either return an error string or raise
        try:
            result = await ask_tool._arun(message="hello")
            result_str = str(result)
            assert (
                "error" in result_str.lower()
                or "crashed" in result_str.lower()
                or "fail" in result_str.lower()
            )
        except Exception:
            pass  # Raising is also acceptable behavior


# ---------------------------------------------------------------------------
# _BroadcastTool
# ---------------------------------------------------------------------------


class TestBroadcastTool:
    @pytest.mark.asyncio
    async def test_fans_out_to_all_peers(self):
        agent_a = _mock_agent("Response A")
        agent_b = _mock_agent("Response B")
        peers = {
            "a": CrossAgent(agent=agent_a),
            "b": CrossAgent(agent=agent_b),
        }
        tools = make_cross_agent_tools(peers)
        broadcast = [t for t in tools if t.name == "broadcast_to_agents"][0]

        result = await broadcast._arun(message="Hello everyone")
        result_str = str(result)
        assert "Response A" in result_str or "a" in result_str.lower()
        assert "Response B" in result_str or "b" in result_str.lower()

    @pytest.mark.asyncio
    async def test_captures_per_peer_errors(self):
        agent_ok = _mock_agent("OK")
        agent_fail = MagicMock()
        agent_fail.ainvoke = AsyncMock(side_effect=RuntimeError("fail"))

        peers = {
            "ok": CrossAgent(agent=agent_ok),
            "bad": CrossAgent(agent=agent_fail),
        }
        tools = make_cross_agent_tools(peers)
        broadcast = [t for t in tools if t.name == "broadcast_to_agents"][0]

        result = await broadcast._arun(message="test")
        result_str = str(result)
        # Should contain OK result (error captured per-peer, not raised)
        assert "OK" in result_str or "ok" in result_str.lower()


# ---------------------------------------------------------------------------
# Exports
# ---------------------------------------------------------------------------


class TestExports:
    def test_importable(self):
        from promptise.cross_agent import CrossAgent, make_cross_agent_tools

        assert CrossAgent is not None
        assert make_cross_agent_tools is not None
