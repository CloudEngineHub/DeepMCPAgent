"""Agent Identity — local identity in 30 seconds, no infrastructure, no API key.

The simplest, most common starting point: give an agent a stable, traceable
identity so every action it records is attributable to *which agent acted*.
A local identity needs no cloud, no IdP, no credentials — just an id.

Run (nothing to install or configure):

    python examples/identity/local/app.py

Then, when you want the agent to *prove* its identity to the MCP servers and
APIs it calls, make it verifiable with a provider — see ../verifiable_mcp/ for a
runnable end-to-end demo, or the cloud examples (aws_lambda, gke_pod, ...).
"""

from __future__ import annotations

import asyncio

from promptise import AgentIdentity

BOLD = "\033[1m"
DIM = "\033[2m"
GREEN = "\033[32m"
CYAN = "\033[36m"
RESET = "\033[0m"


async def main() -> None:
    # 1. Create the identity. That's it — no infrastructure.
    identity = AgentIdentity(
        "billing-bot",
        name="Billing Bot",
        owner="payments-team",
        labels={"env": "prod", "team": "payments"},
    )

    print(f"\n{BOLD}A local agent identity — no cloud, no credentials{RESET}\n")
    print(f"  {CYAN}agent_id{RESET}       {identity.agent_id}")
    print(f"  {CYAN}is_verifiable{RESET}  {identity.is_verifiable}   {DIM}(local — id only){RESET}")
    print(f"  {CYAN}claims(){RESET}       {identity.claims()}")

    # 2. This identifier is what the framework stamps onto the observability
    #    timeline and audit log. Across a fleet you can answer "which agent did
    #    what?" without any extra wiring. To see it on a live timeline, build an
    #    agent with this identity and observe=True (needs a model API key):
    #
    #    from promptise import build_agent
    #    agent = await build_agent(
    #        model="openai:gpt-5-mini", servers={}, identity=identity, observe=True,
    #    )
    #    await agent.ainvoke({"messages": [{"role": "user", "content": "Summarize invoices."}]})
    #    # → every tool call + LLM turn on the timeline is tagged agent_id="billing-bot"

    print(
        f"\n{GREEN}✓{RESET} Every action this identity is attached to is attributed to "
        f"{BOLD}{identity.agent_id}{RESET}."
    )
    print(
        f"{DIM}  Next: make it verifiable so MCP servers can cryptographically verify the\n"
        f"  caller — see examples/identity/verifiable_mcp/app.py.{RESET}\n"
    )


if __name__ == "__main__":
    asyncio.run(main())
