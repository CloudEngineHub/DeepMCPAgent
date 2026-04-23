"""Multi-Agent Pipeline — 3 agents coordinating via events.

Demonstrates:
- 3 agents: Researcher → Analyst → Writer
- EventBus for inter-agent communication
- Shared MCP server with role-based access
- Each agent triggered by the previous agent's completion event
- Real-time pipeline status output

Run:
    python examples/runtime/multi_agent_pipeline.py
"""

from __future__ import annotations

import asyncio
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"
GREEN = "\033[32m"
BLUE = "\033[34m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"

# ═══════════════════════════════════════════════════════════════════════════════
# Shared tools
# ═══════════════════════════════════════════════════════════════════════════════

import json as _json

from promptise.mcp.server import MCPServer

server = MCPServer("pipeline-tools")

# File-based shared state so separate server processes can share data
_STATE_FILE = os.path.join(os.path.dirname(__file__), ".pipeline_state.json")


def _read_state() -> dict:
    try:
        with open(_STATE_FILE) as f:
            return _json.load(f)
    except (FileNotFoundError, _json.JSONDecodeError):
        return {}


def _write_state(state: dict) -> None:
    with open(_STATE_FILE, "w") as f:
        _json.dump(state, f)


@server.tool()
async def web_search(query: str) -> str:
    """Search the web for information."""
    data = {
        "ai agents 2025": "AI agents market growing 45% CAGR. Key players: LangChain, Promptise, AutoGen. MCP protocol gaining traction.",
        "mcp protocol": "Model Context Protocol standardizes how agents discover and call tools. Launched by Anthropic Nov 2024.",
        "agent frameworks comparison": "LangChain: most popular, broad ecosystem. Promptise: production-focused, reasoning engine. AutoGen: Microsoft, multi-agent focus.",
    }
    for key, val in data.items():
        if any(w in query.lower() for w in key.split()):
            return val
    return f"No results for: {query}"


@server.tool()
async def save_findings(key: str, content: str) -> str:
    """Save research findings to shared state."""
    state = _read_state()
    state[key] = content
    _write_state(state)
    return f"Saved findings under '{key}' ({len(content)} chars)"


@server.tool()
async def get_findings(key: str) -> str:
    """Retrieve findings from shared state."""
    state = _read_state()
    return state.get(key, f"No findings for '{key}'")


@server.tool()
async def save_report(title: str, content: str) -> str:
    """Save the final report."""
    state = _read_state()
    state["final_report"] = content
    _write_state(state)
    return f"Report '{title}' saved ({len(content.split())} words)"


# ═══════════════════════════════════════════════════════════════════════════════
# Pipeline
# ═══════════════════════════════════════════════════════════════════════════════


async def main():
    from promptise import build_agent
    from promptise.config import StdioServerSpec

    print(f"""
{BOLD}╔════════════════════════════════════════════════════════╗
║      Multi-Agent Pipeline — Research → Analyze → Write   ║
║         3 agents · Shared tools · Event-driven           ║
╚════════════════════════════════════════════════════════╝{RESET}
""")

    import tempfile

    server_code = """
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from examples.runtime.multi_agent_pipeline import server
server.run(transport="stdio")
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, dir=".") as f:
        f.write(server_code)
        tmp_server = f.name

    try:
        topic = "The current state of AI agent frameworks and where the industry is heading"

        # ── Stage 1: Researcher ──
        print(f"  {BLUE}{BOLD}Stage 1: RESEARCHER{RESET}")
        print(f"  {DIM}Gathering information from multiple sources...{RESET}\n")

        researcher = await build_agent(
            model="openai:gpt-4o-mini",
            servers={"tools": StdioServerSpec(command=sys.executable, args=[tmp_server])},
            instructions=(
                "You are a thorough researcher. Search for information on the given topic. "
                "Use web_search to find data, then save_findings to store your results. "
                "Search at least 2 different queries. Save findings under 'research_data'."
            ),
            max_agent_iterations=25,
        )

        start = time.monotonic()
        result = await researcher.ainvoke(
            {"messages": [{"role": "user", "content": f"Research this topic: {topic}"}]}
        )
        research_time = (time.monotonic() - start) * 1000

        for msg in reversed(result["messages"]):
            if getattr(msg, "type", "") == "ai" and msg.content:
                print(f"  {BLUE}{msg.content[:200]}{RESET}")
                break
        print(f"  {DIM}Completed in {research_time:.0f}ms{RESET}\n")
        await researcher.shutdown()

        # ── Stage 2: Analyst ──
        print(f"  {YELLOW}{BOLD}Stage 2: ANALYST{RESET}")
        print(f"  {DIM}Analyzing research findings...{RESET}\n")

        analyst = await build_agent(
            model="openai:gpt-4o-mini",
            servers={"tools": StdioServerSpec(command=sys.executable, args=[tmp_server])},
            instructions=(
                "You are a senior analyst. Use get_findings to read 'research_data', "
                "then analyze the data: identify key trends, compare frameworks, "
                "and draw conclusions. Save your analysis under 'analysis_results' using save_findings."
            ),
            max_agent_iterations=25,
        )

        start = time.monotonic()
        result = await analyst.ainvoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "Analyze the research findings and identify key insights.",
                    }
                ]
            }
        )
        analysis_time = (time.monotonic() - start) * 1000

        for msg in reversed(result["messages"]):
            if getattr(msg, "type", "") == "ai" and msg.content:
                print(f"  {YELLOW}{msg.content[:200]}{RESET}")
                break
        print(f"  {DIM}Completed in {analysis_time:.0f}ms{RESET}\n")
        await analyst.shutdown()

        # ── Stage 3: Writer ──
        print(f"  {GREEN}{BOLD}Stage 3: WRITER{RESET}")
        print(f"  {DIM}Writing final report...{RESET}\n")

        writer = await build_agent(
            model="openai:gpt-4o-mini",
            servers={"tools": StdioServerSpec(command=sys.executable, args=[tmp_server])},
            instructions=(
                "You are a professional report writer. Use get_findings to read both "
                "'research_data' and 'analysis_results'. Write a clear, structured report "
                "with sections: Executive Summary, Key Findings, Comparison, Outlook. "
                "Save the report using save_report."
            ),
            max_agent_iterations=25,
        )

        start = time.monotonic()
        result = await writer.ainvoke(
            {
                "messages": [
                    {
                        "role": "user",
                        "content": "Write a professional report combining the research and analysis.",
                    }
                ]
            }
        )
        write_time = (time.monotonic() - start) * 1000

        for msg in reversed(result["messages"]):
            if getattr(msg, "type", "") == "ai" and msg.content:
                print(f"  {GREEN}{msg.content[:300]}{RESET}")
                break
        print(f"  {DIM}Completed in {write_time:.0f}ms{RESET}\n")
        await writer.shutdown()

        # ── Summary ──
        total = research_time + analysis_time + write_time
        print(f"{BOLD}{'═' * 60}{RESET}")
        print(f"{BOLD}Pipeline Complete{RESET}")
        print(f"  {BLUE}Research:{RESET}  {research_time:>6.0f}ms")
        print(f"  {YELLOW}Analysis:{RESET} {analysis_time:>6.0f}ms")
        print(f"  {GREEN}Writing:{RESET}  {write_time:>6.0f}ms")
        print(f"  {BOLD}Total:{RESET}    {total:>6.0f}ms")
        final_state = _read_state()
        print(f"\n  {DIM}Shared state keys: {list(final_state.keys())}{RESET}")
        if "final_report" in final_state:
            report = final_state["final_report"]
            print(f"  {DIM}Report length: {len(report.split())} words{RESET}")
        print(f"{BOLD}{'═' * 60}{RESET}")

    finally:
        os.unlink(tmp_server)


if __name__ == "__main__":
    asyncio.run(main())
