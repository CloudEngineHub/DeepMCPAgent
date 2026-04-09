"""Research Agent — Custom 5-node reasoning graph with parallel search.

Demonstrates:
- Custom PromptGraph with specialized reasoning nodes
- FanOutNode for parallel research across 3 sources
- ValidateNode for fact-checking before synthesis
- ReflectNode for quality assessment with conditional re-planning
- Per-node model override (cheap for planning, expensive for synthesis)
- NodeFlags: RETRYABLE, CRITICAL, CACHEABLE

Run:
    python examples/reasoning/research_agent.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

# ═══════════════════════════════════════════════════════════════════════════════
# Colors
# ═══════════════════════════════════════════════════════════════════════════════

DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"
GREEN = "\033[32m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
PURPLE = "\033[35m"

# ═══════════════════════════════════════════════════════════════════════════════
# Research tools server
# ═══════════════════════════════════════════════════════════════════════════════

from promptise.mcp.server import MCPServer

server = MCPServer("research-tools")

KNOWLEDGE = {
    "web": {
        "quantum computing": "Quantum computers use qubits. Google achieved quantum supremacy in 2019. IBM has 1000+ qubit processors as of 2025. Main challenge: error correction. Applications: cryptography, drug discovery, optimization.",
        "climate change": "Global avg temp +1.1°C since pre-industrial. CO2 at 424ppm (2025). Sea level rise: 3.7mm/year. Paris Agreement target: 1.5°C. Renewable energy reached 30% of global electricity in 2024.",
        "ai agents": "AI agents are autonomous systems that perceive, decide, act. Key frameworks: LangChain, Promptise, AutoGen. Major trend: MCP protocol for tool interop. Challenges: reliability, safety, cost.",
    },
    "academic": {
        "quantum computing": "Shor's algorithm (1994) factors integers in polynomial time. Grover's algorithm provides quadratic speedup for search. Surface codes are leading error correction approach. Topological qubits remain theoretical.",
        "climate change": "IPCC AR6 (2023): human influence unequivocal. Carbon budget for 1.5°C: ~500 GtCO2 remaining. Tipping points: AMOC weakening, Amazon dieback, permafrost thaw. Negative emissions technologies needed.",
        "ai agents": "ReAct pattern (Yao et al., 2022) combines reasoning and acting. Tree-of-thought (Yao et al., 2023) explores multiple reasoning paths. Reflexion (Shinn et al., 2023) adds self-reflection. Constitutional AI provides safety alignment.",
    },
    "news": {
        "quantum computing": "Microsoft announced topological qubit breakthrough (Feb 2025). Google's Willow chip achieves 105 qubits with real-time error correction. China's quantum network spans 4,600km.",
        "climate change": "2024 was hottest year on record (1.45°C above pre-industrial). EU Carbon Border Tax took effect Jan 2025. Global renewable investment hit $500B in 2024.",
        "ai agents": "Anthropic launched MCP protocol (Nov 2024). OpenAI Swarm framework for multi-agent (Oct 2024). Promptise Foundry v2.0 with Reasoning Engine (Mar 2026). Agent spending expected to reach $12B by 2027.",
    },
}


@server.tool()
async def search_web(query: str) -> str:
    """Search the web for current information on a topic."""
    for key, content in KNOWLEDGE["web"].items():
        if key in query.lower():
            return f"[Web Search] {content}"
    return f"[Web Search] No relevant results for: {query}"


@server.tool()
async def search_academic(query: str) -> str:
    """Search academic papers and research for scholarly information."""
    for key, content in KNOWLEDGE["academic"].items():
        if key in query.lower():
            return f"[Academic] {content}"
    return f"[Academic] No papers found for: {query}"


@server.tool()
async def search_news(query: str) -> str:
    """Search recent news articles for the latest developments."""
    for key, content in KNOWLEDGE["news"].items():
        if key in query.lower():
            return f"[News] {content}"
    return f"[News] No recent articles for: {query}"


@server.tool()
async def fact_check(claim: str) -> str:
    """Verify a specific factual claim against known data."""
    claim_lower = claim.lower()
    if "1.1" in claim_lower or "temperature" in claim_lower:
        return "VERIFIED: Global average temperature has risen approximately 1.1°C since pre-industrial era (IPCC AR6)."
    if "quantum supremacy" in claim_lower or "google" in claim_lower:
        return "VERIFIED: Google achieved quantum supremacy in 2019 with Sycamore processor (Nature, 2019)."
    if "mcp" in claim_lower or "anthropic" in claim_lower:
        return "VERIFIED: Anthropic launched MCP (Model Context Protocol) in November 2024."
    return f"UNVERIFIED: Unable to confirm claim: {claim}"


# ═══════════════════════════════════════════════════════════════════════════════
# Research Graph
# ═══════════════════════════════════════════════════════════════════════════════

async def main():
    from promptise import build_agent
    from promptise.config import StdioServerSpec
    from promptise.engine import NodeFlag, PromptGraph, PromptNode
    from promptise.engine.reasoning_nodes import (
        PlanNode,
        ReflectNode,
        SynthesizeNode,
        ValidateNode,
    )

    print(f"""
{BOLD}╔════════════════════════════════════════════════════════╗
║          Research Agent — Custom Reasoning Graph         ║
║   Plan → Search (parallel) → Validate → Reflect → Synthesize   ║
╚════════════════════════════════════════════════════════╝{RESET}
""")

    # Build the reasoning graph
    graph = PromptGraph("deep-researcher", nodes=[
        # Step 1: Plan the research approach
        PlanNode("plan",
            is_entry=True,
            max_subgoals=3,
            quality_threshold=3,
        ),

        # Step 2: Execute search across all sources
        PromptNode("search",
            instructions=(
                "Execute a thorough multi-source search. For each aspect of the research plan:\n"
                "1. Search the web for current information (search_web)\n"
                "2. Search academic papers for depth (search_academic)\n"
                "3. Search news for recent developments (search_news)\n"
                "Execute ALL three search tools. Don't stop after one source."
            ),
            inject_tools=True,
            flags={NodeFlag.RETRYABLE},
        ),

        # Step 3: Validate key claims
        ValidateNode("verify",
            criteria=[
                "Key factual claims are verifiable",
                "Sources are consistent (no contradictions)",
                "Recent data is from 2024-2026",
            ],
            on_pass="reflect",
            on_fail="search",
        ),

        # Step 4: Reflect on quality
        ReflectNode("reflect"),

        # Step 5: Synthesize final report
        SynthesizeNode("report", is_terminal=True),
    ])

    # Save server to temp file
    import tempfile
    server_code = '''
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from examples.reasoning.research_agent import server
server.run(transport="stdio")
'''
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, dir=".") as f:
        f.write(server_code)
        tmp_server = f.name

    try:
        agent = await build_agent(
            model="openai:gpt-4o-mini",
            servers={
                "research": StdioServerSpec(command=sys.executable, args=[tmp_server]),
            },
            agent_pattern=graph,
            instructions=(
                "You are a thorough research agent. Plan your approach, "
                "search multiple sources, verify claims, reflect on quality, "
                "and synthesize a comprehensive report with citations."
            ),
            max_agent_iterations=30,
        )

        # Research questions — increasing complexity
        questions = [
            ("Simple", "What is quantum computing and what are its main applications?"),
            ("Medium", "How has climate change progressed and what are the key tipping points according to recent IPCC data?"),
            ("Complex", "Compare the current state of AI agent frameworks — what approaches exist, what are their trade-offs, and where is the field heading?"),
        ]

        for difficulty, question in questions:
            print(f"\n{BOLD}{'═' * 60}{RESET}")
            print(f"  {CYAN}[{difficulty}]{RESET} {BOLD}{question}{RESET}")
            print(f"{BOLD}{'═' * 60}{RESET}\n")

            result = await agent.ainvoke({
                "messages": [{"role": "user", "content": question}]
            })

            # Count tool calls
            tool_calls = []
            for msg in result["messages"]:
                if hasattr(msg, "tool_calls") and msg.tool_calls:
                    for tc in msg.tool_calls:
                        if isinstance(tc, dict) and tc.get("name"):
                            tool_calls.append(tc["name"])

            # Print response
            for msg in reversed(result["messages"]):
                if getattr(msg, "type", "") == "ai" and msg.content:
                    print(f"{GREEN}{msg.content}{RESET}")
                    break

            print(f"\n  {DIM}Tools used: {' → '.join(tool_calls)}{RESET}")
            print(f"  {DIM}Total tool calls: {len(tool_calls)}{RESET}")

        await agent.shutdown()

    finally:
        os.unlink(tmp_server)


if __name__ == "__main__":
    asyncio.run(main())
