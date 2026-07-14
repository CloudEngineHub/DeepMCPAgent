"""AWS Lambda — a verifiable agent identity backed by AWS IAM.

Demonstrates:
- AgentIdentity.from_aws() giving the agent a verifiable identity from the
  function's execution-role identity (STS GetWebIdentityToken on Lambda)
- build_agent(identity=...) attributing every recorded action to the agent,
  driven from a synchronous Lambda entrypoint via asyncio.run

The model keeps its own credential; the identity is about *who is acting*.

Deploy:  sam build && sam deploy --guided
STS mode needs boto3 (present in the Lambda runtime) and the AWS extra
locally:  pip install promptise[identity-aws]
"""

from __future__ import annotations

import asyncio
from typing import Any

from promptise import build_agent
from promptise.identity import AgentIdentity


async def _run(prompt: str) -> str:
    # AWS_REGION is set automatically in the Lambda environment.
    identity = AgentIdentity.from_aws(
        "data-bot",
        name="Data Bot",
        owner="analytics-team",
        audience="api://internal-tools",
    )
    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={},
        identity=identity,
        observe=True,
    )
    result = await agent.ainvoke({"messages": [{"role": "user", "content": prompt}]})
    answer = result["messages"][-1].content
    await agent.shutdown()
    return str(answer)


def handler(event: dict[str, Any], context: Any) -> dict[str, Any]:
    """Lambda entrypoint."""
    prompt = event.get("prompt", "Say hello in one sentence.")
    return {"statusCode": 200, "body": asyncio.run(_run(prompt))}
