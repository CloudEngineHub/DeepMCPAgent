"""GitHub Actions — federated Anthropic auth from an OIDC runner token.

Demonstrates:
- AgentIdentity.from_oidc() reading the GitHub Actions OIDC token from an
  environment variable the workflow exported
- build_agent(identity=...) with NO ANTHROPIC_API_KEY in the environment
- a single real agent invocation against an Anthropic model

This is the example exercised end-to-end by
``.github/workflows/identity-integration.yml`` on every pull request.

Run (inside a GitHub Actions job that set GITHUB_OIDC_TOKEN):
    python examples/identity/github_actions/script.py

Required environment (federation identifiers from the Anthropic Console):
    ANTHROPIC_FEDERATION_RULE_ID, ANTHROPIC_ORGANIZATION_ID,
    ANTHROPIC_SERVICE_ACCOUNT_ID
    GITHUB_OIDC_TOKEN  (the issuer JWT; the workflow fetches and exports it)
"""

from __future__ import annotations

import asyncio
import os
import sys

from promptise import build_agent
from promptise.identity import AgentIdentity, IdentityError


async def main() -> int:
    if not os.environ.get("GITHUB_OIDC_TOKEN"):
        print("GITHUB_OIDC_TOKEN is not set — run this inside the workflow.")
        return 2

    identity = AgentIdentity.from_oidc(
        issuer="https://token.actions.githubusercontent.com",
        token_env_var="GITHUB_OIDC_TOKEN",
        # federation_rule_id / organization_id / service_account_id are read
        # from the ANTHROPIC_* environment variables.
    )
    print(
        f"[identity] provider={identity.provider_name} "
        f"service_account={identity.service_account_id}"
    )

    try:
        # Prove the exchange works before spending tokens on an LLM call.
        token = identity.get_token()
        assert token.startswith("sk-ant-oat01-")
        print("[identity] federated token minted successfully")

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
    except IdentityError as exc:
        print(f"[identity] federation failed: {type(exc).__name__}: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
