"""Minimal MCP server for e2e reasoning graph tests.

Provides simple deterministic tools that the LLM can call during
reasoning graph execution. Run via StdioServerSpec.
"""

from __future__ import annotations

from promptise.mcp.server import MCPServer

server = MCPServer("reasoning-tools")


@server.tool()
async def search_knowledge_base(query: str) -> str:
    """Search the internal knowledge base for information on a topic."""
    responses = {
        "revenue": "Q4 2025 revenue was $42M, up 15% YoY. Main drivers: enterprise contracts.",
        "employees": "Current headcount: 340. Engineering: 120, Sales: 80, Operations: 60, Other: 80.",
        "product": "Promptise Foundry v2.0 launched in March 2026. Key features: Reasoning Graph, MCP Server SDK.",
        "competitors": "Main competitors: LangChain (open-source), CrewAI (multi-agent), Autogen (Microsoft).",
    }
    for key, response in responses.items():
        if key in query.lower():
            return response
    return f"No results found for: {query}"


@server.tool()
async def calculate(expression: str) -> str:
    """Evaluate a mathematical expression and return the result."""
    try:
        # Safe eval for simple math
        allowed = set("0123456789+-*/.() ")
        if all(c in allowed for c in expression):
            return str(eval(expression))  # noqa: S307
        return f"Cannot evaluate: {expression}"
    except Exception as e:
        return f"Error: {e}"


@server.tool()
async def get_current_date() -> str:
    """Get the current date."""
    return "2026-04-02"


@server.tool()
async def write_report(title: str, content: str) -> str:
    """Write a report with the given title and content. Returns confirmation."""
    word_count = len(content.split())
    return f"Report '{title}' saved successfully ({word_count} words)."


if __name__ == "__main__":
    server.run(transport="stdio")
