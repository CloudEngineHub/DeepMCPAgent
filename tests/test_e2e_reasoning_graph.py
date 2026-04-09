"""End-to-end tests: Reasoning Graph engine with real LLM calls.

Connects to a real MCP server via stdio, uses real OpenAI GPT-4o-mini,
and validates that reasoning patterns actually execute correctly with
real tool calling, state mutations, and multi-step reasoning.

Requires:
    OPENAI_API_KEY environment variable set.

Run:
    .venv/bin/python -m pytest tests/test_e2e_reasoning_graph.py -x -v
"""

from __future__ import annotations

import os
import sys

import pytest

# ---------------------------------------------------------------------------
# Skip entire module if no API key
# ---------------------------------------------------------------------------

pytestmark = pytest.mark.skipif(
    not os.getenv("OPENAI_API_KEY"),
    reason="OPENAI_API_KEY not set — skipping real LLM e2e tests",
)

SERVER_SCRIPT = os.path.join(os.path.dirname(__file__), "_e2e_reasoning_server.py")


def _extract_tool_calls(result: dict) -> list[str]:
    """Extract tool names called during agent execution."""
    names: list[str] = []
    for msg in result.get("messages", []):
        msg_type = getattr(msg, "type", "")
        # ToolMessage has .name
        if msg_type == "tool":
            name = getattr(msg, "name", None)
            if name:
                names.append(name)
        # AIMessage has .tool_calls list
        if hasattr(msg, "tool_calls") and msg.tool_calls:
            for tc in msg.tool_calls:
                if isinstance(tc, dict) and tc.get("name"):
                    names.append(tc["name"])
    return names


def _get_final_answer(result: dict) -> str:
    """Get the last AI message content."""
    for msg in reversed(result.get("messages", [])):
        if getattr(msg, "type", "") == "ai" and hasattr(msg, "content"):
            if msg.content:
                return msg.content
    return ""


# ═══════════════════════════════════════════════════════════════════════════════
# ReAct (default) — real tool calling
# ═══════════════════════════════════════════════════════════════════════════════


class TestReActE2E:
    """Default ReAct agent with real LLM and real MCP tools."""

    @pytest.fixture
    async def agent(self):
        from promptise import build_agent
        from promptise.config import StdioServerSpec

        agent = await build_agent(
            model="openai:gpt-4o-mini",
            servers={
                "tools": StdioServerSpec(command=sys.executable, args=[SERVER_SCRIPT]),
            },
            instructions=(
                "You are a business analyst. Use your tools to answer questions. "
                "Always call a tool — never answer from memory."
            ),
            max_agent_iterations=30,
        )
        yield agent
        await agent.shutdown()

    @pytest.mark.asyncio
    async def test_single_tool_call(self, agent):
        """Agent should call search_knowledge_base for a factual question."""
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": "What was Q4 2025 revenue? Use search_knowledge_base."}]}
        )
        tools = _extract_tool_calls(result)

        # The agent should have called search_knowledge_base at least once
        assert "search_knowledge_base" in tools, f"Expected search tool call, got: {tools}"

    @pytest.mark.asyncio
    async def test_calculation_tool(self, agent):
        """Agent should use the calculate tool for math."""
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": "Use the calculate tool to compute 42 * 17."}]}
        )
        tools = _extract_tool_calls(result)

        assert "calculate" in tools, f"Expected calculate tool call, got: {tools}"

    @pytest.mark.asyncio
    async def test_produces_final_answer(self, agent):
        """Agent should produce a non-empty final answer after tool use."""
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": "What is today's date? Use the get_current_date tool."}]}
        )
        tools = _extract_tool_calls(result)
        answer = _get_final_answer(result)

        assert "get_current_date" in tools
        assert len(answer) > 0


# ═══════════════════════════════════════════════════════════════════════════════
# Custom Reasoning Graph — real execution
# ═══════════════════════════════════════════════════════════════════════════════


class TestCustomGraphE2E:
    """Custom PromptGraph with real LLM execution."""

    @pytest.fixture
    async def agent(self):
        from promptise import build_agent
        from promptise.config import StdioServerSpec
        from promptise.engine import PromptGraph, PromptNode

        # Build a research → synthesize pipeline
        graph = PromptGraph("research-pipeline", mode="static")

        graph.add_node(
            PromptNode(
                "research",
                instructions=(
                    "You are a research agent. Use search_knowledge_base to gather "
                    "information about the user's question. Call the tool at least once."
                ),
                inject_tools=True,
                output_key="findings",
                default_next="synthesize",
            )
        )

        graph.add_node(
            PromptNode(
                "synthesize",
                instructions=(
                    "You are a synthesis agent. Based on the research findings, "
                    "write a clear 2-3 sentence summary answering the user's question. "
                    "Do NOT call any tools."
                ),
                inherit_context_from="research",
                default_next="__end__",
            )
        )

        graph.set_entry("research")
        graph.add_edge("research", "synthesize")

        agent = await build_agent(
            model="openai:gpt-4o-mini",
            servers={
                "tools": StdioServerSpec(command=sys.executable, args=[SERVER_SCRIPT]),
            },
            agent_pattern=graph,
            instructions="Answer business questions using research tools.",
        )
        yield agent
        await agent.shutdown()

    @pytest.mark.asyncio
    async def test_two_node_pipeline(self, agent):
        """Custom graph with research→synthesize should produce a meaningful answer."""
        result = await agent.ainvoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "Use search_knowledge_base to find info about our product, then summarize it.",
                    }
                ]
            }
        )
        answer = _get_final_answer(result)

        # The pipeline should produce SOME answer (tool calls may or may not show
        # depending on how the graph routes — the key test is that the agent
        # completes without error and produces output)
        assert len(answer) > 0, "Expected non-empty answer from pipeline"

    @pytest.mark.asyncio
    async def test_custom_graph_completes(self, agent):
        """Custom graph should complete without errors."""
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": "What do we know about competitors?"}]}
        )
        # Should have produced messages (not crash)
        assert len(result.get("messages", [])) > 1


# ═══════════════════════════════════════════════════════════════════════════════
# Reasoning Patterns — real execution
# ═══════════════════════════════════════════════════════════════════════════════


class TestReasoningPatternsE2E:
    """Test built-in reasoning patterns with real LLM."""

    @pytest.fixture
    async def peoatr_agent(self):
        from promptise import build_agent
        from promptise.config import StdioServerSpec

        agent = await build_agent(
            model="openai:gpt-4o-mini",
            servers={
                "tools": StdioServerSpec(command=sys.executable, args=[SERVER_SCRIPT]),
            },
            agent_pattern="peoatr",
            instructions=(
                "You are a thorough research analyst. Plan your approach, "
                "execute with tools, think about results, and reflect on quality."
            ),
        )
        yield agent
        await agent.shutdown()

    @pytest.mark.asyncio
    async def test_peoatr_completes(self, peoatr_agent):
        """PEOATR pattern should plan, act, think, reflect, and produce an answer."""
        result = await peoatr_agent.ainvoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "Research our company's employee count and calculate revenue per employee.",
                    }
                ]
            }
        )
        tools = _extract_tool_calls(result)
        answer = _get_final_answer(result)

        # Should have used at least one tool
        assert len(tools) > 0
        # Should have produced an answer
        assert len(answer) > 10


class TestReasoningNodesE2E:
    """Test custom graphs with reasoning nodes and real LLM."""

    @pytest.fixture
    async def agent(self):
        from promptise import build_agent
        from promptise.config import StdioServerSpec
        from promptise.engine import NodeFlag, PromptGraph, PromptNode
        from promptise.engine.reasoning_nodes import PlanNode, SynthesizeNode, ThinkNode

        graph = PromptGraph("deliberate-agent", nodes=[
            PlanNode("plan", is_entry=True),
            PromptNode("act", inject_tools=True, flags={NodeFlag.RETRYABLE}),
            ThinkNode("think"),
            SynthesizeNode("answer", is_terminal=True),
        ])

        agent = await build_agent(
            model="openai:gpt-4o-mini",
            servers={
                "tools": StdioServerSpec(command=sys.executable, args=[SERVER_SCRIPT]),
            },
            agent_pattern=graph,
            instructions="Answer business questions by planning, acting, thinking, then synthesizing.",
        )
        yield agent
        await agent.shutdown()

    @pytest.mark.asyncio
    async def test_reasoning_nodes_execute(self, agent):
        """Reasoning nodes should execute and produce a final answer."""
        result = await agent.ainvoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "What is our company headcount?",
                    }
                ]
            }
        )
        answer = _get_final_answer(result)

        # Should have produced a meaningful answer
        assert len(answer) > 10


# ═══════════════════════════════════════════════════════════════════════════════
# Agent with hooks — real execution + observability
# ═══════════════════════════════════════════════════════════════════════════════


class TestAgentObservabilityE2E:
    """Test that observability works with real agent execution."""

    @pytest.fixture
    async def agent(self):
        from promptise import build_agent
        from promptise.config import StdioServerSpec
        agent = await build_agent(
            model="openai:gpt-4o-mini",
            servers={
                "tools": StdioServerSpec(command=sys.executable, args=[SERVER_SCRIPT]),
            },
            instructions="Answer questions using tools. Be concise.",
            observe=True,
        )
        yield agent
        await agent.shutdown()

    @pytest.mark.asyncio
    async def test_invocation_completes(self, agent):
        """Agent with observability enabled should complete an invocation."""
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": "Use get_current_date to tell me today's date."}]}
        )
        tools = _extract_tool_calls(result)
        assert "get_current_date" in tools

    @pytest.mark.asyncio
    async def test_multiple_invocations(self, agent):
        """Agent should handle multiple sequential invocations."""
        r1 = await agent.ainvoke(
            {"messages": [{"role": "user", "content": "Use get_current_date to get today's date."}]}
        )
        r2 = await agent.ainvoke(
            {"messages": [{"role": "user", "content": "Use calculate to compute 10 + 20."}]}
        )
        # Both should complete and have messages
        assert len(r1.get("messages", [])) > 1
        assert len(r2.get("messages", [])) > 1


# ═══════════════════════════════════════════════════════════════════════════════
# Write tool — tests that tools with side effects work
# ═══════════════════════════════════════════════════════════════════════════════


class TestToolSideEffectsE2E:
    """Test tools that have side effects (write_report)."""

    @pytest.fixture
    async def agent(self):
        from promptise import build_agent
        from promptise.config import StdioServerSpec

        agent = await build_agent(
            model="openai:gpt-4o-mini",
            servers={
                "tools": StdioServerSpec(command=sys.executable, args=[SERVER_SCRIPT]),
            },
            instructions=(
                "You are a report writer. When asked, research a topic using "
                "search_knowledge_base, then use write_report to save your findings."
            ),
        )
        yield agent
        await agent.shutdown()

    @pytest.mark.asyncio
    async def test_research_and_write(self, agent):
        """Agent should research a topic and write a report."""
        result = await agent.ainvoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "Research our product and write a brief report about it.",
                    }
                ]
            }
        )
        tools = _extract_tool_calls(result)

        assert "search_knowledge_base" in tools
        assert "write_report" in tools
