"""Enterprise MCP CLI — Interactive agent with role switching.

Connects to the 30-tool enterprise MCP server and lets you:
- Chat with the agent as different users (viewer, analyst, admin)
- Switch roles mid-conversation to see tool access change
- View the audit log of tool calls

Run:
    # Terminal 1: Start the server
    python examples/mcp/enterprise_server.py

    # Terminal 2: Run this CLI
    python examples/mcp/enterprise_cli.py

    # Or run everything in one command (server launches as subprocess):
    python examples/mcp/enterprise_cli.py --auto
"""

from __future__ import annotations

import asyncio
import os
import sys

# Add project root to path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))


# ═══════════════════════════════════════════════════════════════════════════════
# Colors
# ═══════════════════════════════════════════════════════════════════════════════

DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"
BLUE = "\033[34m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"
MAGENTA = "\033[35m"

ROLE_COLORS = {"viewer": BLUE, "analyst": YELLOW, "admin": RED}
ROLE_DESCRIPTIONS = {
    "viewer": "Can see HR data and support tickets. Read-only.",
    "analyst": "Viewer + Finance, Inventory, Analytics. Can create tickets.",
    "admin": "Full access. System admin, secrets, rate limits, stock adjustments.",
}


def print_banner():
    print(f"""
{BOLD}╔══════════════════════════════════════════════════════════════╗
║           Enterprise MCP Server — Interactive CLI             ║
║                30 tools · 6 domains · 3 roles                ║
╚══════════════════════════════════════════════════════════════╝{RESET}

{DIM}Commands:{RESET}
  {CYAN}/switch viewer|analyst|admin{RESET}  — Change role
  {CYAN}/tools{RESET}                        — Show accessible tools
  {CYAN}/whoami{RESET}                       — Show current identity
  {CYAN}/quit{RESET}                         — Exit

{DIM}Type a question to ask the agent. It will use only tools your role permits.{RESET}
""")


def print_role_badge(role: str):
    color = ROLE_COLORS.get(role, DIM)
    desc = ROLE_DESCRIPTIONS.get(role, "")
    print(f"\n  {color}{BOLD}● {role.upper()}{RESET} {DIM}— {desc}{RESET}\n")


async def main():
    from promptise import build_agent
    from promptise.config import StdioServerSpec

    server_script = os.path.join(os.path.dirname(__file__), "enterprise_server.py")

    # Current role
    current_role = "viewer"
    agent = None
    tool_call_log: list[str] = []

    async def build_for_role(role: str):
        """Build an agent connected to the enterprise server."""
        nonlocal agent
        if agent:
            await agent.shutdown()

        agent = await build_agent(
            model="openai:gpt-4o-mini",
            servers={
                "enterprise": StdioServerSpec(
                    command=sys.executable,
                    args=[server_script],
                ),
            },
            instructions=(
                f"You are a business assistant with {role.upper()} access level. "
                f"Use the available tools to answer questions. "
                f"If a tool call is denied due to permissions, tell the user they need a higher role. "
                f"Be concise and precise with data."
            ),
            max_agent_iterations=30,
        )
        return agent

    print_banner()

    # Build initial agent
    print(f"{DIM}Connecting to enterprise server...{RESET}")
    agent = await build_for_role(current_role)
    print_role_badge(current_role)

    while True:
        try:
            user_input = input(f"  {ROLE_COLORS.get(current_role, '')}{current_role}{RESET} > ").strip()
        except (EOFError, KeyboardInterrupt):
            break

        if not user_input:
            continue

        # ── Commands ──
        if user_input.startswith("/"):
            cmd = user_input.lower().split()

            if cmd[0] == "/quit":
                break

            elif cmd[0] == "/switch" and len(cmd) > 1:
                new_role = cmd[1]
                if new_role not in ("viewer", "analyst", "admin"):
                    print(f"  {RED}Unknown role: {new_role}. Use viewer, analyst, or admin.{RESET}")
                    continue
                current_role = new_role
                print(f"\n{DIM}  Switching to {new_role}...{RESET}")
                agent = await build_for_role(current_role)
                print_role_badge(current_role)

            elif cmd[0] == "/tools":
                # Ask the agent to list tools
                result = await agent.ainvoke({
                    "messages": [{"role": "user", "content": "List all tools you have access to. Just list the tool names grouped by domain."}]
                })
                for msg in reversed(result["messages"]):
                    if getattr(msg, "type", "") == "ai" and msg.content:
                        print(f"\n{DIM}{msg.content}{RESET}\n")
                        break

            elif cmd[0] == "/whoami":
                color = ROLE_COLORS.get(current_role, "")
                desc = ROLE_DESCRIPTIONS.get(current_role, "")
                print(f"\n  {color}{BOLD}Role: {current_role.upper()}{RESET}")
                print(f"  {DIM}{desc}{RESET}\n")

            else:
                print(f"  {DIM}Unknown command. Try /switch, /tools, /whoami, /quit{RESET}")

            continue

        # ── Agent query ──
        print(f"  {DIM}Thinking...{RESET}")
        result = await agent.ainvoke({
            "messages": [{"role": "user", "content": user_input}]
        })

        # Extract tool calls for log
        for msg in result.get("messages", []):
            if hasattr(msg, "tool_calls") and msg.tool_calls:
                for tc in msg.tool_calls:
                    if isinstance(tc, dict) and tc.get("name"):
                        tool_call_log.append(f"{current_role} → {tc['name']}")

        # Print response
        for msg in reversed(result["messages"]):
            if getattr(msg, "type", "") == "ai" and msg.content:
                print(f"\n  {GREEN}{msg.content}{RESET}\n")
                break

    # Cleanup
    if agent:
        await agent.shutdown()
    print(f"\n{DIM}Session ended. {len(tool_call_log)} tool calls made.{RESET}")


if __name__ == "__main__":
    asyncio.run(main())
