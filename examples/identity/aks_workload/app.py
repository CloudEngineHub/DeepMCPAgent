"""AKS workload — a verifiable agent identity backed by Microsoft Entra.

Demonstrates:
- AgentIdentity.from_entra() giving the agent a verifiable identity from
  AKS Workload Identity (the projected token at $AZURE_FEDERATED_TOKEN_FILE)
- build_agent(identity=...) attributing every recorded action to the agent

The model keeps its own credential; the identity is about *who is acting*.

Run (inside an AKS pod with Workload Identity enabled):
    python app.py
"""

from __future__ import annotations

import asyncio

from promptise import build_agent
from promptise.identity import AgentIdentity


async def main() -> None:
    identity = AgentIdentity.from_entra(
        "data-bot",
        name="Data Bot",
        owner="analytics-team",
        resource="api://internal-tools",
    )
    print(f"[identity] {identity.claims()}")

    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={},
        identity=identity,
        observe=True,
    )
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Say hello in one sentence."}]}
    )
    print("[agent]", result["messages"][-1].content)
    await agent.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
