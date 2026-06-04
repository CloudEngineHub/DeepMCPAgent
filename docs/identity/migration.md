# Adding identity to existing agents

Agent Identity is **additive**. It does not change how your agents are
authenticated to their model or how they call tools — it gives them a
traceable identity on top of what you already have. Adopting it is a
one-line change, and it touches nothing else.

## Before

```python
agent = await build_agent(
    model="anthropic:claude-sonnet-4-5",
    servers={...},
)
```

## After

```python
from promptise.identity import AgentIdentity

agent = await build_agent(
    model="anthropic:claude-sonnet-4-5",
    servers={...},
    identity=AgentIdentity("billing-bot", name="Billing Bot", owner="payments"),
)
```

That is the whole change. Your model credential, MCP servers, memory,
guardrails, and every other argument are untouched. The only difference
is that the agent's recorded actions are now attributed to
`billing-bot`.

## Rollout

1. **Name your agents.** Give each agent process a stable `agent_id`.
   Treat it like a service-account name: durable, unique, meaningful.

2. **Add `identity=`** to each `build_agent()` call. Start with a local
   identity — no infrastructure needed. With `observe=` enabled, the
   timeline immediately shows which agent produced each tool call and
   LLM turn.

3. **Make it verifiable where it matters.** For agents that call MCP
   servers or APIs which must *verify* the caller, switch the local
   identity to a credential-backed one
   (`AgentIdentity.from_entra(...)`, `.from_aws(...)`, `.auto(...)`),
   and present `identity.get_credential()` to those resources. The
   server validates the signed credential and attributes the call.

4. **Roll back trivially.** Remove the `identity=` argument. There is no
   state to migrate and nothing else changes.

## What this is not

- It is **not** a change to how the agent authenticates to its LLM. The
  model keeps its own credentials.
- It is **not** a credential store. A verifiable identity holds a
  short-lived credential in memory only; the platform issues and rotates
  it.

## A fleet-wide view

Because the identity is stamped onto observability and audit, a fleet of
agents becomes traceable: filter the timeline or audit log by `agent_id`
to see exactly what one agent did, or which agent touched a resource.
`AgentIdentity.auto()` lets the *same image* roll out across mixed
platforms, each instance picking the right credential provider for where
it runs.
