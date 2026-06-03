"""SPIRE workload — federated Anthropic auth with AgentIdentity.from_spiffe().

Demonstrates:
- AgentIdentity.from_spiffe() fetching a JWT-SVID from the SPIRE agent's
  Workload API socket (SDK mode); $SPIFFE_ENDPOINT_SOCKET selects the socket
- build_agent(identity=...) with NO ANTHROPIC_API_KEY
- a single real agent invocation

Run (inside a workload registered with SPIRE):
    python app.py

SDK mode needs pyspiffe:  pip install promptise[identity-spiffe]
(For the file-based alternative — spiffe-helper writing a JWT-SVID to disk —
use AgentIdentity.from_spiffe(token_file="/run/spiffe/jwt-svid.token"), which
needs no pyspiffe.)

Required environment (federation identifiers from the Anthropic Console):
    ANTHROPIC_FEDERATION_RULE_ID, ANTHROPIC_ORGANIZATION_ID,
    ANTHROPIC_SERVICE_ACCOUNT_ID
    SPIFFE_ENDPOINT_SOCKET  (e.g. unix:///run/spire/agent/api.sock)
"""

from __future__ import annotations

import asyncio

from promptise import build_agent
from promptise.identity import AgentIdentity


async def main() -> None:
    identity = AgentIdentity.from_spiffe()  # SDK mode via $SPIFFE_ENDPOINT_SOCKET
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
