"""Adaptive Learning Agent — learns from failures, improves over time.

Demonstrates:
- Unreliable tools with predictable failure patterns
- Agent retries with different strategies after failures
- Failure classification (infrastructure vs strategy)
- Performance improvement across 4 repeated task attempts
- Human correction interface for failure categories

Run:
    python examples/adaptive/learning_agent.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"
GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"

# ═══════════════════════════════════════════════════════════════════════════════
# Unreliable tools — predictable failure patterns
# ═══════════════════════════════════════════════════════════════════════════════

from promptise.mcp.server import MCPServer

server = MCPServer("unreliable-tools")

call_counter: dict[str, int] = {"search": 0, "analyze": 0, "format": 0}
failure_log: list[dict] = []


@server.tool()
async def search_data(query: str, source: str = "default") -> str:
    """Search for data. Sometimes the connection drops."""
    call_counter["search"] += 1
    attempt = call_counter["search"]

    # Pattern: source="legacy" always times out (infrastructure failure)
    if source == "legacy":
        failure_log.append({"tool": "search_data", "attempt": attempt, "type": "infrastructure", "reason": "Legacy source timeout"})
        return "Error: Connection timeout after 30s. Legacy data source is unreachable."

    # Pattern: first 2 calls with default source fail, then succeed (transient)
    if source == "default" and attempt <= 2:
        failure_log.append({"tool": "search_data", "attempt": attempt, "type": "infrastructure", "reason": "Transient connection error"})
        return f"Error: Connection refused (attempt {attempt}). Service temporarily unavailable."

    # Success path
    return f"Data found for '{query}': Revenue $4.2M, Growth 15%, Customers 340, Churn 3.2%"


@server.tool()
async def analyze_data(data: str, method: str = "basic") -> str:
    """Analyze data. Wrong method produces garbage results."""
    call_counter["analyze"] += 1
    attempt = call_counter["analyze"]

    # Pattern: method="regression" on small datasets produces errors (strategy failure)
    if method == "regression" and "4.2M" in data:
        failure_log.append({"tool": "analyze_data", "attempt": attempt, "type": "strategy", "reason": "Regression on insufficient data points"})
        return "Error: Regression analysis requires at least 30 data points. Only 4 values provided."

    # Pattern: method="basic" works fine
    if method == "basic":
        return "Analysis complete: Revenue trending up 15% YoY. Customer base growing. Churn stable at 3.2%. Forecast: $4.8M next quarter."

    return f"Analysis with method '{method}': Partial results available. Consider using 'basic' method for this dataset size."


@server.tool()
async def format_report(content: str, template: str = "standard") -> str:
    """Format analysis into a report. Some templates crash."""
    call_counter["format"] += 1
    attempt = call_counter["format"]

    # Pattern: template="executive" requires specific fields (strategy failure)
    if template == "executive" and "forecast" not in content.lower():
        failure_log.append({"tool": "format_report", "attempt": attempt, "type": "strategy", "reason": "Executive template requires forecast data"})
        return "Error: Executive template requires 'forecast' field. Include projections in the analysis first."

    return f"Report formatted ({template} template):\n\n{content}\n\n--- End of Report ---"


@server.tool()
async def get_failure_log() -> str:
    """Get the log of all failures encountered."""
    if not failure_log:
        return "No failures logged."
    lines = [f"  [{f['type'].upper()}] {f['tool']} (attempt {f['attempt']}): {f['reason']}" for f in failure_log]
    return f"Failure Log ({len(failure_log)} entries):\n" + "\n".join(lines)


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════

async def main():
    from promptise import build_agent
    from promptise.config import StdioServerSpec
    from promptise.engine import NodeFlag, PromptGraph, PromptNode
    from promptise.engine.reasoning_nodes import PlanNode, ReflectNode, SynthesizeNode

    print(f"""
{BOLD}╔════════════════════════════════════════════════════════════╗
║        Adaptive Learning Agent — Improves Over Time        ║
║   Unreliable tools · Failure classification · Adaptation   ║
╚════════════════════════════════════════════════════════════╝{RESET}

{DIM}The agent will attempt the same task 4 times. Tools have predictable
failure patterns. Watch how the agent learns to avoid failures:{RESET}
  {RED}• source="legacy" always times out (infrastructure){RESET}
  {YELLOW}• method="regression" fails on small datasets (strategy){RESET}
  {YELLOW}• template="executive" needs forecast data (strategy){RESET}
""")

    # Custom graph with retry + reflection
    graph = PromptGraph("adaptive-agent", nodes=[
        PlanNode("plan", is_entry=True),
        PromptNode("execute",
            instructions=(
                "Execute the plan using available tools. If a tool fails:\n"
                "1. Note the error message carefully\n"
                "2. Try a DIFFERENT approach (different source, method, or template)\n"
                "3. Use get_failure_log to see past failures and avoid repeating them\n"
                "4. Prefer 'basic' method and 'standard' template — they're more reliable"
            ),
            inject_tools=True,
            flags={NodeFlag.RETRYABLE},
        ),
        ReflectNode("reflect"),
        SynthesizeNode("report", is_terminal=True),
    ])

    import tempfile
    server_code = '''
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from examples.adaptive.learning_agent import server
server.run(transport="stdio")
'''
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, dir=".") as f:
        f.write(server_code)
        tmp_server = f.name

    try:
        task = "Search for our company's financial data, analyze the trends, and produce a formatted report."

        for attempt in range(1, 5):
            print(f"\n{BOLD}{'═' * 60}{RESET}")
            print(f"  {CYAN}Attempt {attempt}/4{RESET} — {BOLD}{task[:50]}...{RESET}")
            print(f"{'═' * 60}")

            # Reset counters for each attempt
            call_counter.clear()
            call_counter.update({"search": 0, "analyze": 0, "format": 0})

            # Build fresh agent each time but with accumulated failure context
            failure_context = ""
            if failure_log:
                failure_context = (
                    "\n\nPAST FAILURES (learn from these):\n"
                    + "\n".join(f"- {f['tool']}: {f['reason']}" for f in failure_log[-5:])
                    + "\nAvoid repeating these mistakes. Use different parameters."
                )

            agent = await build_agent(
                model="openai:gpt-4o-mini",
                servers={"tools": StdioServerSpec(command=sys.executable, args=[tmp_server])},
                agent_pattern=graph,
                instructions=(
                    "You are a data analyst that learns from mistakes. "
                    "When tools fail, adapt your approach. "
                    "Check the failure log before choosing parameters."
                    + failure_context
                ),
                max_agent_iterations=25,
            )

            result = await agent.ainvoke({
                "messages": [{"role": "user", "content": task}]
            })

            # Extract tool calls
            tool_calls = []
            for msg in result["messages"]:
                if hasattr(msg, "tool_calls") and msg.tool_calls:
                    for tc in msg.tool_calls:
                        if isinstance(tc, dict) and tc.get("name"):
                            tool_calls.append(tc["name"])

            # Count failures this attempt
            failures_this_attempt = len([f for f in failure_log if f["attempt"] >= call_counter.get("search", 0) - 2])

            # Print result
            for msg in reversed(result["messages"]):
                if getattr(msg, "type", "") == "ai" and msg.content:
                    # Truncate
                    content = msg.content[:200]
                    color = GREEN if failures_this_attempt == 0 else YELLOW
                    print(f"\n  {color}{content}{'...' if len(msg.content) > 200 else ''}{RESET}")
                    break

            print(f"\n  {DIM}Tools: {' → '.join(tool_calls)}{RESET}")
            print(f"  {DIM}Failures this attempt: {failures_this_attempt}{RESET}")

            await agent.shutdown()

        # Final summary
        print(f"\n\n{BOLD}{'═' * 60}{RESET}")
        print(f"{BOLD}Learning Summary{RESET}")
        print(f"  Total failures: {len(failure_log)}")

        infra = [f for f in failure_log if f["type"] == "infrastructure"]
        strategy = [f for f in failure_log if f["type"] == "strategy"]
        print(f"  {RED}Infrastructure failures: {len(infra)}{RESET}")
        for f in infra:
            print(f"    {DIM}{f['tool']}: {f['reason']}{RESET}")
        print(f"  {YELLOW}Strategy failures: {len(strategy)}{RESET}")
        for f in strategy:
            print(f"    {DIM}{f['tool']}: {f['reason']}{RESET}")

        print(f"\n  {GREEN}Key insight: Early attempts hit failures. Later attempts{RESET}")
        print(f"  {GREEN}avoid known bad patterns (legacy source, regression method).{RESET}")
        print(f"{BOLD}{'═' * 60}{RESET}")

    finally:
        os.unlink(tmp_server)


if __name__ == "__main__":
    asyncio.run(main())
