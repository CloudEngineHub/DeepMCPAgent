"""Code-action — the model writes ONE program instead of chaining tool calls.

For aggregation / data-traversal tasks (gather many facts, then compute),
chaining dozens of conversational tool calls is slow, expensive, and unreliable.
``agent_pattern="code-action"`` changes the action space: in a single LLM turn
the model writes one Python program that calls your tools (bridged into a
hardened Docker sandbox) and computes the answer deterministically.

Best practice shown here: give code-action tools that return **structured data**
(lists / dicts / numbers), not formatted prose — the program then uses the
values directly.

Requires Docker running (the program executes in a sandbox) and an API key.

Run:
    export OPENAI_API_KEY=...
    python examples/reasoning/code_action_agent.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

from langchain_core.tools import tool  # noqa: E402

DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"
GREEN = "\033[32m"
CYAN = "\033[36m"
YELLOW = "\033[33m"

# A small company dataset. The data is arbitrary, so the only way to answer is to
# traverse it with the tools — list everyone, look each up, filter, aggregate.
_EMPLOYEES: dict[str, tuple[str, int]] = {
    "Dana Cole": ("Executive", 300000),
    "Priya Anand": ("Engineering", 210000),
    "Alex Kim": ("Engineering", 140000),
    "Jo Park": ("Engineering", 135000),
    "Ravi Shah": ("Engineering", 150000),
    "Sam Ortiz": ("Finance", 195000),
    "Eva Lund": ("Finance", 125000),
    "Lin Wei": ("Analytics", 190000),
    "Mae Tan": ("Analytics", 130000),
}


@tool("list_employees")
def list_employees() -> list:
    """Return a list of every employee name."""
    return list(_EMPLOYEES)


@tool("get_employee")
def get_employee(name: str) -> dict:
    """Return {name, department, salary} for an employee by exact name."""
    rec = _EMPLOYEES.get(name)
    if rec is None:
        return {"error": f"no employee named {name!r}"}
    return {"name": name, "department": rec[0], "salary": rec[1]}


async def main() -> None:
    from promptise import build_agent

    print(f"""
{BOLD}╔══════════════════════════════════════════════════════════════╗
║   Code-Action — one sandboxed program beats a 20-call tool loop ║
╚══════════════════════════════════════════════════════════════╝{RESET}
{DIM}The model writes ONE Python program over your tools; it runs in a Docker
sandbox (read-only rootfs, no network); tool calls bridge back to the host.{RESET}""")

    if not os.environ.get("OPENAI_API_KEY"):
        print(f"\n{YELLOW}Set OPENAI_API_KEY to run this example (it makes real LLM calls).{RESET}")
        return

    # sandbox is auto-enabled for code-action (Docker must be running).
    agent = await build_agent(
        servers={},
        model="openai:gpt-5-mini",
        agent_pattern="code-action",
        extra_tools=[list_employees, get_employee],
        instructions="You answer questions about the company by writing a program.",
    )

    questions = [
        "What is the combined annual salary of everyone in the Engineering department?",
        "Which department has the highest average salary, and what is that average?",
    ]

    try:
        for q in questions:
            print(f"\n{BOLD}{'═' * 64}{RESET}")
            print(f"  {CYAN}Q:{RESET} {q}")
            print(f"{BOLD}{'═' * 64}{RESET}")
            result = await agent.ainvoke({"messages": [{"role": "user", "content": q}]})

            answer = ""
            ai_turns = 0
            for msg in result["messages"]:
                if getattr(msg, "type", "") == "ai":
                    ai_turns += 1
            for msg in reversed(result["messages"]):
                if getattr(msg, "type", "") == "ai" and msg.content:
                    answer = msg.content if isinstance(msg.content, str) else str(msg.content)
                    break
            print(f"  {GREEN}{answer}{RESET}")
            print(f"  {DIM}(answered in {ai_turns} LLM turn — the program did the work){RESET}")
    finally:
        await agent.shutdown()

    print(f"\n{BOLD}Done.{RESET}\n")


if __name__ == "__main__":
    asyncio.run(main())
