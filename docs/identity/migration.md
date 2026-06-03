# Migration

Moving an existing agent from a static `ANTHROPIC_API_KEY` to federated identity
is a one-line code change plus an environment change. This page shows how to do
it without downtime.

## Before

```python
# ANTHROPIC_API_KEY is set in the environment; build_agent reads it implicitly.
agent = await build_agent(
    model="anthropic:claude-sonnet-4-5",
    servers={},
)
```

## After

```python
from promptise.identity import AgentIdentity

agent = await build_agent(
    model="anthropic:claude-sonnet-4-5",
    servers={},
    identity=AgentIdentity.auto(),   # or from_aws(), from_gcp(), …
)
```

## Step-by-step, zero downtime

1. **Register the federation rule** in the Anthropic Console for the platform
   your workload runs on, and record the `fdrl_…`, organization UUID, and
   `svac_…` identifiers. (See the [provider pages](../identity/overview.md#supported-providers)
   for per-platform Console setup.)

2. **Verify the exchange out-of-band**, while the old key is still in place.
   Mint a token in a throwaway script and confirm it succeeds:

    ```python
    from promptise.identity import AgentIdentity

    identity = AgentIdentity.auto()
    print(identity.provider_name)
    token = identity.get_token()
    assert token.startswith("sk-ant-oat01-")
    print("federation works")
    ```

    If this raises, fix the federation rule before touching the running agent —
    the live agent is still healthy on its API key.

3. **Add `identity=` to `build_agent()`** and deploy. Because the credential
   precedence guard refuses to run with *both* an identity and
   `ANTHROPIC_API_KEY`, do this together with step 4.

4. **Unset `ANTHROPIC_API_KEY`** in the same deploy. The guard exists precisely
   so you cannot accidentally ship a config where the static key silently
   shadows the federated identity:

    ```text
    CredentialPrecedenceError: Both an AgentIdentity and ANTHROPIC_API_KEY are
    configured. The static API key would silently shadow the federated identity.
    Either unset ANTHROPIC_API_KEY or remove the identity= argument.
    ```

5. **Roll back instantly if needed.** Reverting is symmetric: remove the
   `identity=` argument and restore `ANTHROPIC_API_KEY`. No data migration, no
   token state to clean up — the cache is in-memory and per-process.

## Gradual rollout across a fleet

`AgentIdentity.auto()` reads platform environment markers, so the *same image*
can roll out to mixed platforms and each instance picks the right provider.
Combined with a feature flag around the `identity=` argument, you can canary one
deployment, watch the audit log for the `identity.provider` field, and expand.

## Things that do not change

- Your model string, MCP servers, memory, guardrails, and every other
  `build_agent()` argument are untouched.
- Downstream MCP tools keep working; they gain the *option* of calling
  `agent.identity.get_auth_header()` for authenticated outbound requests.
- Cost, latency, and the agent's behavior are unchanged — only the
  authentication mechanism moved.
