"""Verify & Managed — two single-node reasoning patterns for accuracy and efficiency.

Demonstrates the two context-managed built-in patterns:

- ``agent_pattern="verify"`` — single-pass self-verifying reasoning. In ONE
  LLM turn the model must plan, solve, and re-check its own answer before
  replying. Catches careless errors on weak/mainstream models without paying
  for a multi-stage pipeline. (No tools needed here.)

- ``agent_pattern="managed"`` — a tool loop with **context lifecycle
  management** (``context_scope="ledger"``). On a deep multi-tool task the
  model would normally re-query the same facts as its transcript grows; the
  managed pattern keeps a compact, deduplicated "facts gathered" ledger and
  serves identical ``(tool, args)`` calls from cache, so context stays bounded.
  It is an efficiency primitive — fewer redundant calls at equal accuracy.

Both use a real LLM via ``build_agent`` — no mocks.

Run:
    export OPENAI_API_KEY=...
    python examples/reasoning/verify_and_managed.py
"""

from __future__ import annotations

import asyncio
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

# ── Colors ──────────────────────────────────────────────────────────────────
DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"
GREEN = "\033[32m"
CYAN = "\033[36m"
YELLOW = "\033[33m"

# ── A tiny company dataset for the "managed" tool-loop demo ──────────────────
# The data is arbitrary, so the model CANNOT answer without chaining tool calls
# (list everyone → look each up → filter → aggregate). That long chain is
# exactly where context-lifecycle management earns its keep.

_EMPLOYEES: dict[str, tuple[str, int]] = {
    # name: (department, annual_salary)
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


def _make_company_tools() -> list:
    """Return LangChain tools over the company dataset (structured lookups)."""
    from langchain_core.tools import tool

    @tool("list_employees")
    def list_employees() -> str:
        """List the names of every employee in the company."""
        return ", ".join(_EMPLOYEES)

    @tool("get_employee")
    def get_employee(name: str) -> str:
        """Get an employee's department and annual salary by exact name."""
        rec = _EMPLOYEES.get(name)
        if rec is None:
            return f"no employee named {name!r}"
        dept, salary = rec
        return f"{name}: department={dept}; salary={salary}"

    return [list_employees, get_employee]


def _print_answer(result: dict) -> tuple[str, int]:
    """Extract the final answer text and count tool calls from a run result."""
    tool_calls = 0
    for msg in result["messages"]:
        for tc in getattr(msg, "tool_calls", None) or []:
            if isinstance(tc, dict) and tc.get("name"):
                tool_calls += 1
    answer = ""
    for msg in reversed(result["messages"]):
        if getattr(msg, "type", "") == "ai" and msg.content:
            answer = msg.content if isinstance(msg.content, str) else str(msg.content)
            break
    return answer, tool_calls


async def demo_verify() -> None:
    """Single-pass self-verifying reasoning on a question that's easy to flub."""
    from promptise import build_agent

    print(f"\n{BOLD}{'═' * 64}{RESET}")
    print(f"  {CYAN}verify{RESET} — plan → solve → self-check, in ONE turn (no tools)")
    print(f"{BOLD}{'═' * 64}{RESET}")

    agent = await build_agent(
        servers={},  # no MCP tools — pure reasoning
        model="openai:gpt-5-mini",
        agent_pattern="verify",
        instructions="You are a careful reasoner. Give only the final numeric answer at the end.",
    )

    # A classic trap: the intuitive answer (0.10) is wrong; the verify step
    # should catch it and correct to 0.05.
    question = (
        "A bat and a ball cost $1.10 in total. The bat costs $1.00 more than "
        "the ball. How much does the ball cost, in dollars?"
    )
    print(f"\n  {BOLD}Q:{RESET} {question}\n")
    result = await agent.ainvoke({"messages": [{"role": "user", "content": question}]})
    answer, _ = _print_answer(result)
    print(f"  {GREEN}{answer}{RESET}")
    print(f"  {DIM}(expected: 0.05 — the self-check catches the 0.10 trap){RESET}")
    await agent.shutdown()


async def demo_managed() -> None:
    """Deep multi-tool task where the facts-ledger keeps context bounded."""
    from promptise import build_agent

    print(f"\n{BOLD}{'═' * 64}{RESET}")
    print(f"  {CYAN}managed{RESET} — tool loop + deduplicated facts ledger")
    print(f"{BOLD}{'═' * 64}{RESET}")

    agent = await build_agent(
        servers={},
        model="openai:gpt-5-mini",
        agent_pattern="managed",
        extra_tools=_make_company_tools(),
        instructions=(
            "You total salaries for a department using the tools. Procedure: "
            "call list_employees to get every name; then call get_employee for "
            "EACH name to learn their department and salary; then add up the "
            "salaries of those whose department matches. A ledger of facts "
            "already gathered is provided each turn — consult it and never look "
            "up the same person twice. Do NOT ask the user anything; everything "
            "you need is in the tools. When done, reply with ONLY the final "
            "total as a plain number (no words, no currency sign)."
        ),
        max_agent_iterations=30,  # deep chains make many calls
    )

    # Requires: list everyone → look each up → keep only Engineering → sum.
    # Engineering = Priya 210k + Alex 140k + Jo 135k + Ravi 150k = 635000.
    question = (
        "What is the combined annual salary of all employees in the "
        "Engineering department? Reply with just the number."
    )
    print(f"\n  {BOLD}Q:{RESET} {question}\n")
    result = await agent.ainvoke({"messages": [{"role": "user", "content": question}]})
    answer, tool_calls = _print_answer(result)
    print(f"  {GREEN}{answer}{RESET}")
    print(
        f"  {DIM}(expected: 635000 — {tool_calls} tool calls; the ledger "
        f"prevents re-querying the same employees){RESET}"
    )
    await agent.shutdown()


async def main() -> None:
    print(f"""
{BOLD}╔══════════════════════════════════════════════════════════════╗
║   Verify & Managed — single-node patterns for accuracy + cost  ║
╚══════════════════════════════════════════════════════════════╝{RESET}
{DIM}verify  → a self-check that catches careless errors, 1 turn, no tools
managed → a long tool chain kept efficient by a deduplicated facts ledger{RESET}""")

    if not os.environ.get("OPENAI_API_KEY"):
        print(f"\n{YELLOW}Set OPENAI_API_KEY to run this example (it makes real LLM calls).{RESET}")
        return

    await demo_verify()
    await demo_managed()
    print(f"\n{BOLD}Done.{RESET}\n")


if __name__ == "__main__":
    asyncio.run(main())
