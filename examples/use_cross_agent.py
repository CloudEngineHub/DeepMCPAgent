# use_cross_agent.py
"""
Example: Cross-Agent Communication with DeepMCPAgent.

This demonstrates wiring a specialist peer agent into a primary agent so the
primary can delegate work via cross-agent tools:

- ask_agent_<name>(message, context?, timeout_s?)
- broadcast_to_agents(message, peers?, timeout_s?)

Console output:
- Discovered MCP tools (from your servers)
- Advertised cross-agent tools (derived from peers)
- Each tool invocation + result (via deepmcpagent trace hooks)
- Final LLM answer
"""

import asyncio
from typing import Any

from dotenv import load_dotenv
from langchain_openai import ChatOpenAI
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from deepmcpagent import HTTPServerSpec, build_deep_agent
from deepmcpagent.cross_agent import CrossAgent


def _extract_final_answer(result: Any) -> str:
    """Best-effort extraction of the final text from different executors."""
    try:
        # LangGraph prebuilt typically returns {"messages": [...]}
        if isinstance(result, dict) and "messages" in result and result["messages"]:
            last = result["messages"][-1]
            content = getattr(last, "content", None)
            if isinstance(content, str) and content:
                return content
            if isinstance(content, list) and content and isinstance(content[0], dict):
                return content[0].get("text") or str(content)
            return str(last)
        return str(result)
    except Exception:
        return str(result)


async def main() -> None:
    console = Console()
    load_dotenv()

    # Ensure your MCP server (e.g., math_server.py) is running in another terminal:
    #   python math_server.py
    servers = {
        "math": HTTPServerSpec(
            url="http://127.0.0.1:8000/mcp",
            transport="http",
        ),
    }

    # Any LangChain-compatible chat model (or init string) works here.
    # Use the same or different models for main/peer agents as you prefer.
    main_model = ChatOpenAI(model="gpt-4.1")
    peer_model = ChatOpenAI(model="gpt-4o-mini")

    # ---------------------------------------------------------------------
    # 1) Build a specialist peer agent (math-focused)
    # ---------------------------------------------------------------------
    math_peer_graph, _ = await build_deep_agent(
        servers=servers,
        model=peer_model,
        instructions=(
            "You are a focused Math Specialist. ALWAYS use available MCP math tools "
            "to compute precisely. Return concise numeric results with brief steps."
        ),
        trace_tools=True,  # See the math peer's own tool usage when directly invoked
    )

    # Wrap the peer as a CrossAgent so it can be exposed as a tool.
    peers = {
        "mathpeer": CrossAgent(
            agent=math_peer_graph,
            description="Specialist math agent that uses MCP math tools for accurate computation.",
        )
    }

    # ---------------------------------------------------------------------
    # 2) Build the main agent and attach cross-agent tools
    # ---------------------------------------------------------------------
    main_graph, loader = await build_deep_agent(
        servers=servers,
        model=main_model,
        instructions=(
            "You are a helpful orchestrator. Prefer calling tools rather than guessing. "
            "If the task is mathematical, DELEGATE to the math peer via the tool "
            "'ask_agent_mathpeer'. If multiple peers exist, you may also use "
            "'broadcast_to_agents' to compare answers."
        ),
        trace_tools=True,  # See tool invocations from the main agent (including cross-agent tools)
        cross_agents=peers,  # <-- Attach the peer(s)
    )

    # ---------------------------------------------------------------------
    # 3) Show discovered tools (MCP) + cross-agent tools
    # ---------------------------------------------------------------------
    infos = await loader.list_tool_info()
    infos = list(infos) if infos else []

    mcp_table = Table(title="Discovered MCP Tools", show_lines=True)
    mcp_table.add_column("Name", style="cyan", no_wrap=True)
    mcp_table.add_column("Description", style="green")
    if infos:
        for t in infos:
            mcp_table.add_row(t.name, t.description or "-")
    else:
        mcp_table.add_row("— none —", "No tools discovered (is your MCP server running?)")
    console.print(mcp_table)

    cross_table = Table(title="Cross-Agent Tools (exposed on MAIN agent)", show_lines=True)
    cross_table.add_column("Tool", style="cyan", no_wrap=True)
    cross_table.add_column("What it does", style="green")
    # One per peer:
    for name in peers:
        cross_table.add_row(f"ask_agent_{name}", f"Ask the '{name}' peer for help.")
    # Broadcast tool is always added by make_cross_agent_tools in our integration
    cross_table.add_row(
        "broadcast_to_agents", "Ask multiple peers in parallel and collect answers."
    )
    console.print(cross_table)

    # ---------------------------------------------------------------------
    # 4) Run a single-turn query that should trigger delegation
    # ---------------------------------------------------------------------
    query = (
        "A rectangle is width 3.5 and length 6.2. "
        "Compute area and perimeter, then add 17^2 to the sum of (area + perimeter). "
        "Please DELEGATE to the math peer via the 'ask_agent_mathpeer' tool and show brief steps."
    )
    console.print(
        Panel.fit(query, title="User Query (expects cross-agent delegation)", style="bold magenta")
    )

    result = await main_graph.ainvoke({"messages": [{"role": "user", "content": query}]})
    final_text = _extract_final_answer(result)
    console.print(Panel(final_text or "(no content)", title="Final LLM Answer", style="bold green"))

    # ---------------------------------------------------------------------
    # 5) (Optional) Demonstrate broadcast to peers — with one peer it's trivial,
    #    but we show how you'd instruct the main agent to use it.
    # ---------------------------------------------------------------------
    query2 = (
        "As a quick check, consult all peers via 'broadcast_to_agents' with the message "
        "'Compute (3 + 5) * 7 using MCP math tools.' Then summarize the responses."
    )
    console.print(Panel.fit(query2, title="User Query (broadcast demo)", style="bold magenta"))
    result2 = await main_graph.ainvoke({"messages": [{"role": "user", "content": query2}]})
    final_text2 = _extract_final_answer(result2)
    console.print(
        Panel(
            final_text2 or "(no content)", title="Final LLM Answer (Broadcast)", style="bold green"
        )
    )


if __name__ == "__main__":
    asyncio.run(main())
