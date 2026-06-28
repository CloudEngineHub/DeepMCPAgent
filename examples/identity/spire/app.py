"""SPIRE workload — a verifiable agent identity backed by SPIFFE/SPIRE.

Demonstrates:
- AgentIdentity.from_spiffe() giving the agent a verifiable identity from a
  JWT-SVID fetched over the SPIRE Workload API socket (SDK mode)
- build_agent(identity=...) attributing every recorded action to the agent

SDK mode needs pyspiffe:  pip install promptise[identity-spiffe]
(File mode — spiffe-helper writing a JWT-SVID to disk — needs no pyspiffe:
AgentIdentity.from_spiffe("data-bot", token_file="/run/spiffe/jwt-svid.token").)

Run (inside a workload registered with SPIRE):
    python app.py
"""

from __future__ import annotations

import asyncio

from promptise import build_agent
from promptise.identity import AgentIdentity


async def main() -> None:
    identity = AgentIdentity.from_spiffe(
        "data-bot",
        name="Data Bot",
        owner="platform-team",
        audience="api://internal-tools",
        # SDK mode via $SPIFFE_ENDPOINT_SOCKET
    )
    print(f"[identity] {identity.claims()}")

    agent = await build_agent(
        model="anthropic:claude-sonnet-4-5",
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
