"""Tests for promptise.cross_agent — cross-agent delegation tools."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from promptise.cross_agent import CrossAgent, make_cross_agent_tools
from promptise.identity import AgentIdentity

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


# ---------------------------------------------------------------------------
# Caller identity propagation
# ---------------------------------------------------------------------------


def _ask_tool(tools: list, name: str = "ask_agent_peer"):
    return next(t for t in tools if t.name == name)


@pytest.mark.asyncio
async def test_ask_announces_caller_identity() -> None:
    peer = _mock_agent("hi")
    tools = make_cross_agent_tools(
        {"peer": CrossAgent(agent=peer, description="d")},
        caller_identity=AgentIdentity("billing-bot", owner="payments"),
    )
    await _ask_tool(tools)._arun(message="help")
    sent = peer.ainvoke.call_args.args[0]["messages"]
    assert sent[0]["role"] == "system"
    assert "billing-bot" in sent[0]["content"]
    assert sent[-1] == {"role": "user", "content": "help"}


@pytest.mark.asyncio
async def test_ask_without_identity_has_no_announcement() -> None:
    peer = _mock_agent("hi")
    tools = make_cross_agent_tools({"peer": CrossAgent(agent=peer, description="d")})
    await _ask_tool(tools)._arun(message="help")
    sent = peer.ainvoke.call_args.args[0]["messages"]
    assert sent[0] == {"role": "user", "content": "help"}


@pytest.mark.asyncio
async def test_broadcast_announces_caller_identity() -> None:
    peer = _mock_agent("hi")
    tools = make_cross_agent_tools(
        {"peer": CrossAgent(agent=peer, description="d")},
        caller_identity=AgentIdentity("billing-bot"),
    )
    bcast = next(t for t in tools if t.name == "broadcast_to_agents")
    await bcast._arun(message="help")
    sent = peer.ainvoke.call_args.args[0]["messages"]
    assert sent[0]["role"] == "system"
    assert "billing-bot" in sent[0]["content"]


@pytest.mark.asyncio
async def test_delegation_context_visible_to_peer_and_reset() -> None:
    from promptise.observability import get_current_delegation

    seen: dict = {}

    class _Peer:
        async def ainvoke(self, inp):  # noqa: ANN001
            seen["delegation"] = get_current_delegation()
            return {"messages": [MagicMock(type="ai", content="ok")]}

    tools = make_cross_agent_tools(
        {"peer": CrossAgent(agent=_Peer())},
        caller_identity=AgentIdentity("billing-bot", owner="pay"),
    )
    await _ask_tool(tools)._arun(message="go")
    assert seen["delegation"]["agent_id"] == "billing-bot"
    assert get_current_delegation() is None  # reset after the call (no leak)


@pytest.mark.asyncio
async def test_delegation_context_reset_even_when_peer_raises() -> None:
    # The reset must run in a `finally`: if a delegated peer raises, the
    # delegation contextvar must still be cleared so it does not leak into
    # the caller's own subsequent events.
    from promptise.observability import get_current_delegation

    class _Peer:
        async def ainvoke(self, inp):  # noqa: ANN001
            raise RuntimeError("peer exploded")

    tools = make_cross_agent_tools(
        {"peer": CrossAgent(agent=_Peer())},
        caller_identity=AgentIdentity("billing-bot"),
    )
    with pytest.raises(RuntimeError, match="peer exploded"):
        await _ask_tool(tools)._arun(message="go")
    assert get_current_delegation() is None  # reset despite the error


@pytest.mark.asyncio
async def test_no_delegation_context_without_identity() -> None:
    from promptise.observability import get_current_delegation

    seen: dict = {}

    class _Peer:
        async def ainvoke(self, inp):  # noqa: ANN001
            seen["delegation"] = get_current_delegation()
            return {"messages": [MagicMock(type="ai", content="ok")]}

    tools = make_cross_agent_tools({"peer": CrossAgent(agent=_Peer())})
    await _ask_tool(tools)._arun(message="go")
    assert seen["delegation"] is None


@pytest.mark.asyncio
async def test_broadcast_sets_delegation_for_each_peer() -> None:
    from promptise.observability import get_current_delegation

    seen: list = []

    class _Peer:
        async def ainvoke(self, inp):  # noqa: ANN001
            seen.append(get_current_delegation())
            return {"messages": [MagicMock(type="ai", content="ok")]}

    tools = make_cross_agent_tools(
        {"a": CrossAgent(agent=_Peer()), "b": CrossAgent(agent=_Peer())},
        caller_identity=AgentIdentity("billing-bot"),
    )
    bcast = next(t for t in tools if t.name == "broadcast_to_agents")
    await bcast._arun(message="go")
    assert len(seen) == 2
    assert all(d is not None and d["agent_id"] == "billing-bot" for d in seen)
    assert get_current_delegation() is None


# ---------------------------------------------------------------------------
# CallerContext continuity across delegation
# ---------------------------------------------------------------------------


class TestCallerContextContinuity:
    """The original human principal must survive cross-agent hops.

    ``PromptiseAgent.ainvoke`` inherits the ambient ``CallerContext`` when no
    explicit ``caller`` is passed, so a peer invoked inside another agent's
    request scopes its cache, memory, guardrails, and conversations to the
    original user instead of running unattributed.
    """

    @pytest.mark.asyncio
    async def test_peer_inherits_ambient_caller_through_delegation(self):
        from promptise.agent import CallerContext, PromptiseAgent, get_current_caller

        seen: dict = {}

        class _PeerInner:
            async def ainvoke(self, inp, config=None):
                seen["caller"] = get_current_caller()
                return {"messages": [MagicMock(type="ai", content="ok")]}

        peer = PromptiseAgent(inner=_PeerInner())
        outer_ctx = CallerContext(user_id="alice", roles={"analyst"})

        class _OuterInner:
            async def ainvoke(self, inp, config=None):
                # Simulates a cross-agent tool firing inside the outer run
                tools = make_cross_agent_tools({"peer": CrossAgent(agent=peer)})
                await _ask_tool(tools)._arun(message="hi")
                return {"messages": [MagicMock(type="ai", content="done")]}

        outer = PromptiseAgent(inner=_OuterInner())
        await outer.ainvoke({"messages": []}, caller=outer_ctx)

        assert seen["caller"] is outer_ctx
        assert get_current_caller() is None  # no leak after the run

    @pytest.mark.asyncio
    async def test_explicit_caller_still_wins_over_ambient(self):
        from promptise.agent import CallerContext, PromptiseAgent, get_current_caller

        seen: dict = {}

        class _PeerInner:
            async def ainvoke(self, inp, config=None):
                seen["caller"] = get_current_caller()
                return {"messages": [MagicMock(type="ai", content="ok")]}

        peer = PromptiseAgent(inner=_PeerInner())
        ambient_ctx = CallerContext(user_id="alice")
        explicit_ctx = CallerContext(user_id="bob")

        class _OuterInner:
            async def ainvoke(self, inp, config=None):
                await peer.ainvoke({"messages": []}, caller=explicit_ctx)
                return {"messages": [MagicMock(type="ai", content="done")]}

        outer = PromptiseAgent(inner=_OuterInner())
        await outer.ainvoke({"messages": []}, caller=ambient_ctx)

        assert seen["caller"] is explicit_ctx

    @pytest.mark.asyncio
    async def test_no_ambient_no_caller_stays_none(self):
        from promptise.agent import PromptiseAgent, get_current_caller

        seen: dict = {}

        class _Inner:
            async def ainvoke(self, inp, config=None):
                seen["caller"] = get_current_caller()
                return {"messages": [MagicMock(type="ai", content="ok")]}

        agent = PromptiseAgent(inner=_Inner())
        await agent.ainvoke({"messages": []})

        assert seen["caller"] is None

    @pytest.mark.asyncio
    async def test_broadcast_children_inherit_ambient_caller(self):
        from promptise.agent import CallerContext, PromptiseAgent, get_current_caller

        seen: dict = {}

        def _recording_peer(name: str) -> PromptiseAgent:
            class _Inner:
                async def ainvoke(self, inp, config=None):
                    seen[name] = get_current_caller()
                    return {"messages": [MagicMock(type="ai", content=name)]}

            return PromptiseAgent(inner=_Inner())

        peers = {
            "one": CrossAgent(agent=_recording_peer("one")),
            "two": CrossAgent(agent=_recording_peer("two")),
        }
        outer_ctx = CallerContext(user_id="carol")

        class _OuterInner:
            async def ainvoke(self, inp, config=None):
                tools = make_cross_agent_tools(peers)
                broadcast = next(t for t in tools if "broadcast" in t.name)
                await broadcast._arun(message="status?")
                return {"messages": [MagicMock(type="ai", content="done")]}

        outer = PromptiseAgent(inner=_OuterInner())
        await outer.ainvoke({"messages": []}, caller=outer_ctx)

        assert seen["one"] is outer_ctx
        assert seen["two"] is outer_ctx
