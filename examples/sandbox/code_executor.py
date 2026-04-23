"""Code Execution Sandbox — Agent writes, tests, and iterates on code.

Demonstrates:
- Docker-based sandbox with security layers
- Agent writes Python code, executes it, reads errors, fixes, re-runs
- Network isolation, resource limits, read-only rootfs
- 3 coding challenges of increasing difficulty
- Shows the write → test → fix iteration loop

Requires: Docker running locally.

Run:
    python examples/sandbox/code_executor.py
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


async def main():
    from promptise import build_agent

    print(f"""
{BOLD}╔════════════════════════════════════════════════════════╗
║        Code Execution Sandbox — Write → Test → Fix      ║
║     Docker isolation · Resource limits · No network      ║
╚════════════════════════════════════════════════════════╝{RESET}
""")

    # Build agent with sandbox enabled
    # The sandbox auto-injects 5 tools: execute, read_file, write_file, list_files, install_package
    agent = await build_agent(
        model="openai:gpt-4o-mini",
        sandbox={
            "enabled": True,
            "cpu_limit": 1,
            "memory_limit": "256M",
            "network": "none",  # No internet access
            "timeout": 30,  # Max 30s per execution
        },
        instructions=(
            "You are a Python developer. When given a coding task:\n"
            "1. Write the solution to a file using write_file\n"
            "2. Write tests to verify it using write_file\n"
            "3. Execute the tests using execute\n"
            "4. If tests fail, read the error, fix the code, and re-run\n"
            "5. Report the final result\n\n"
            "Always write tests. Always verify your code works."
        ),
        max_agent_iterations=30,
    )

    challenges = [
        {
            "name": "FizzBuzz",
            "difficulty": "Easy",
            "prompt": (
                "Write a Python function `fizzbuzz(n)` that returns a list of strings from 1 to n. "
                "For multiples of 3, use 'Fizz'. For multiples of 5, use 'Buzz'. "
                "For multiples of both, use 'FizzBuzz'. Otherwise, use the number as a string. "
                "Write it to solution.py, write tests to test_solution.py, then run the tests."
            ),
        },
        {
            "name": "Data Processing",
            "difficulty": "Medium",
            "prompt": (
                "Write a Python function `process_csv(data: str) -> dict` that takes CSV text "
                "(with headers) and returns a dict with: 'row_count', 'columns' (list of header names), "
                "and 'summary' (dict mapping each numeric column to its average). "
                "Example input: 'name,age,score\\nAlice,30,95\\nBob,25,87' "
                "Should return: {'row_count': 2, 'columns': ['name', 'age', 'score'], 'summary': {'age': 27.5, 'score': 91.0}} "
                "Write it to solution.py, write tests, then run them."
            ),
        },
        {
            "name": "Recursive Tree",
            "difficulty": "Hard",
            "prompt": (
                "Write a Python class `TreeNode` with `value`, `left`, `right` and these methods:\n"
                "- `insert(val)`: BST insert\n"
                "- `search(val) -> bool`: BST search\n"
                "- `inorder() -> list`: in-order traversal\n"
                "- `height() -> int`: tree height\n"
                "Write it to solution.py, write comprehensive tests (including edge cases like "
                "empty tree, single node, unbalanced), then run them."
            ),
        },
    ]

    for challenge in challenges:
        print(f"\n{BOLD}{'═' * 60}{RESET}")
        print(f"  {CYAN}[{challenge['difficulty']}]{RESET} {BOLD}{challenge['name']}{RESET}")
        print(f"{BOLD}{'═' * 60}{RESET}\n")

        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": challenge["prompt"]}]}
        )

        # Extract tool calls to show the iteration
        tool_calls = []
        for msg in result["messages"]:
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                for tc in msg.tool_calls:
                    if isinstance(tc, dict) and tc.get("name"):
                        tool_calls.append(tc["name"])

        # Print response
        for msg in reversed(result["messages"]):
            if getattr(msg, "type", "") == "ai" and msg.content:
                print(f"  {GREEN}{msg.content}{RESET}")
                break

        # Show iteration stats
        writes = tool_calls.count("write_file")
        executes = tool_calls.count("execute")
        reads = tool_calls.count("read_file")
        print(f"\n  {DIM}Iterations: {writes} writes, {executes} executions, {reads} reads{RESET}")
        print(f"  {DIM}Tool chain: {' → '.join(tool_calls)}{RESET}")

    await agent.shutdown()
    print(f"\n{BOLD}All challenges completed.{RESET}")


if __name__ == "__main__":
    asyncio.run(main())
