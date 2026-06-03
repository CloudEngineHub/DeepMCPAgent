# Quickstart

This five-minute path gets you a working federated agent with **no cloud
account**. It uses the [generic OIDC](providers/oidc.md) env-var mode, so you
can run it on a laptop or in a plain Docker container — the same code then works
unchanged on AWS, GCP, Azure, or Kubernetes by swapping the factory.

## 1. Register a federation rule

In the [Anthropic Console](https://console.anthropic.com), create a Workload
Identity Federation rule for your issuer and note three identifiers:

- the federation rule id (`fdrl_…`),
- your organization id (a UUID),
- the service account id (`svac_…`).

These are **identifiers, not secrets** — but treat them as configuration, not
literals in code.

## 2. Set the environment

```bash
export ANTHROPIC_FEDERATION_RULE_ID=fdrl_your_rule_id
export ANTHROPIC_ORGANIZATION_ID=your_org_uuid
export ANTHROPIC_SERVICE_ACCOUNT_ID=svac_your_service_account

# The issuer's OIDC token. In CI this is set for you (e.g. $CI_JOB_JWT_V2 on
# GitLab, the OIDC token on a GitHub Actions runner). Here we read one you
# already have on disk:
export MY_OIDC_TOKEN="$(cat ./oidc_token.jwt)"

# Make sure no static key is present — it would shadow the federated identity
# and build_agent() will refuse to start.
unset ANTHROPIC_API_KEY
```

## 3. Build the agent

```python
import asyncio

from promptise import build_agent
from promptise.identity import AgentIdentity


async def main() -> None:
    identity = AgentIdentity.from_oidc(
        issuer="https://gitlab.com",
        token_env_var="MY_OIDC_TOKEN",
        # federation_rule_id / organization_id / service_account_id are read
        # from the ANTHROPIC_* environment variables set above.
    )

    agent = await build_agent(
        model="anthropic:claude-sonnet-4-5",
        servers={},
        identity=identity,
    )

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Say hello in one sentence."}]}
    )
    print(result["messages"][-1].content)
    await agent.shutdown()


asyncio.run(main())
```

That is the whole integration. `build_agent` authenticates the agent's own
Anthropic calls with the federated token — no `ANTHROPIC_API_KEY` anywhere.

## 4. Verify the token exchange directly

You can mint a token without building an agent, which is handy for debugging:

```python
from promptise.identity import AgentIdentity

identity = AgentIdentity.from_oidc(
    issuer="https://gitlab.com",
    token_env_var="MY_OIDC_TOKEN",
)

token = identity.get_token()          # exchanges + caches
assert token.startswith("sk-ant-oat01-")
print(identity.get_auth_header())     # {"Authorization": "Bearer sk-ant-oat01-…"}
```

`get_token()` is cached and thread-safe: repeated calls return the same token
until it nears expiry, then refresh transparently.

## 5. Move to a real platform

When you deploy, delete the `MY_OIDC_TOKEN` plumbing and swap one line:

=== "AWS"

    ```python
    identity = AgentIdentity.from_aws()
    ```

=== "GCP"

    ```python
    identity = AgentIdentity.from_gcp()
    ```

=== "Azure"

    ```python
    identity = AgentIdentity.from_entra()
    ```

=== "SPIFFE"

    ```python
    identity = AgentIdentity.from_spiffe()
    ```

=== "Auto-detect"

    ```python
    identity = AgentIdentity.auto()
    ```

Each provider has its own setup page with prerequisites, Console configuration,
and troubleshooting — see [Microsoft Entra](providers/entra.md),
[AWS](providers/aws.md), [Google Cloud](providers/gcp.md),
[SPIFFE](providers/spiffe.md), and [Generic OIDC](providers/oidc.md).
