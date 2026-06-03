"""GKE pod — federated Anthropic auth with AgentIdentity.from_gcp().

Demonstrates:
- AgentIdentity.from_gcp() reading an identity token from the GCP compute
  metadata server (works on GKE with Workload Identity, GCE, Cloud Run, …)
- build_agent(identity=...) with NO ANTHROPIC_API_KEY
- a single real agent invocation

Run (inside a GKE pod bound to a GCP service account):
    python app.py

Required environment (federation identifiers from the Anthropic Console):
    ANTHROPIC_FEDERATION_RULE_ID, ANTHROPIC_ORGANIZATION_ID,
    ANTHROPIC_SERVICE_ACCOUNT_ID
"""

from __future__ import annotations

import asyncio

from promptise import build_agent
from promptise.identity import AgentIdentity


async def main() -> None:
    identity = AgentIdentity.from_gcp()
    print(
        f"[identity] provider={identity.provider_name} "
        f"service_account={identity.service_account_id}"
    )
    agent = await build_agent(
        model="anthropic:claude-sonnet-4-5",
        servers={},
        identity=identity,
    )
    result = await agent.ainvoke(
        {
            "messages": [
                {
                    "role": "user",
                    "content": "In one sentence, what is workload identity federation?",
                }
            ]
        }
    )
    print("[agent]", result["messages"][-1].content)
    await agent.shutdown()


if __name__ == "__main__":
    asyncio.run(main())
