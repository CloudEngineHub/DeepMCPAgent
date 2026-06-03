"""AKS workload — federated Anthropic auth with AgentIdentity.from_entra().

Demonstrates:
- AgentIdentity.from_entra() reading the projected token AKS Workload Identity
  writes to $AZURE_FEDERATED_TOKEN_FILE (mode="auto" -> "projected" on AKS)
- build_agent(identity=...) with NO ANTHROPIC_API_KEY
- a single real agent invocation

Run (inside an AKS pod with Workload Identity enabled):
    python app.py

Required environment (federation identifiers from the Anthropic Console):
    ANTHROPIC_FEDERATION_RULE_ID, ANTHROPIC_ORGANIZATION_ID,
    ANTHROPIC_SERVICE_ACCOUNT_ID
    (AZURE_FEDERATED_TOKEN_FILE and AZURE_CLIENT_ID are injected by AKS.)
"""

from __future__ import annotations

import asyncio

from promptise import build_agent
from promptise.identity import AgentIdentity


async def main() -> None:
    identity = AgentIdentity.from_entra()
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
