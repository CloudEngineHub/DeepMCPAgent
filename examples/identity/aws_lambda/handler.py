"""AWS Lambda — federated Anthropic auth with AgentIdentity.from_aws().

Demonstrates:
- AgentIdentity.from_aws() using the function's execution-role identity via
  STS GetWebIdentityToken (mode="auto" -> "sts" on Lambda)
- build_agent(identity=...) with NO ANTHROPIC_API_KEY
- driving the agent from a synchronous Lambda entrypoint via asyncio.run

Deploy:
    sam build && sam deploy --guided

Required environment (set in template.yaml; identifiers from the Console):
    ANTHROPIC_FEDERATION_RULE_ID, ANTHROPIC_ORGANIZATION_ID,
    ANTHROPIC_SERVICE_ACCOUNT_ID

Packaging note: STS mode needs boto3 (already present in the Lambda runtime)
and the AWS extra locally — install with: pip install promptise[identity-aws]
"""

from __future__ import annotations

import asyncio
from typing import Any

from promptise import build_agent
from promptise.identity import AgentIdentity


async def _run(prompt: str) -> str:
    # AWS_REGION is set automatically in the Lambda environment, so
    # from_aws() needs no arguments.
    identity = AgentIdentity.from_aws()
    agent = await build_agent(
        model="anthropic:claude-sonnet-4-5",
        servers={},
        identity=identity,
    )
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": prompt}]}
    )
    answer = result["messages"][-1].content
    await agent.shutdown()
    return str(answer)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entrypoint."""
    prompt = event.get("prompt", "Say hello in one sentence.")
    answer = asyncio.run(_run(prompt))
    return {"statusCode": 200, "body": answer}
