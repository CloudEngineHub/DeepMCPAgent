"""GKE pod — a verifiable agent identity backed by Google Cloud.

Demonstrates:
- AgentIdentity.from_gcp() giving the agent a verifiable identity from the
  GCP metadata server (GKE Workload Identity, GCE, Cloud Run, …)
- build_agent(identity=...) attributing every recorded action to the agent
- presenting the identity credential to a resource the agent calls

The model keeps its own credential (e.g. an Anthropic/OpenAI key in the
environment); the identity is about *who is acting*, not the LLM key.

Run (inside a GKE pod bound to a GCP service account):
    python app.py
"""

from __future__ import annotations

import asyncio

from promptise import build_agent
from promptise.identity import AgentIdentity


async def main() -> None:
    identity = AgentIdentity.from_gcp(
        "data-bot",
        name="Data Bot",
        owner="analytics-team",
        audience="api://internal-tools",
    )
    print(f"[identity] {identity.claims()}")
    print(f"[identity] credential preview: {identity.get_credential()[:16]}…")

    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={},
        identity=identity,
        observe=True,   # the timeline now attributes actions to "data-bot"
    )
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Say hello in one sentence."}]}
    )
    print("[agent]", result["messages"][-1].content)
    await agent.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
