---
title: "Can an AI agent have an identity without an IdP?"
description: "Most agent-identity advice assumes an IdP, which is a non-starter for air-gapped or early-stage deployments. Promptise's two-tier model starts with a local…"
keywords: "agent identity without an identity provider, local agent identity, air-gapped agent identity, agent_id attribution, zero-infrastructure agent identity, on-prem AI agent identity"
date: 2026-07-16
slug: agent-identity-without-an-identity-provider
categories:
  - Identity
---

# Can an AI agent have an identity without an IdP?

You can give an AI agent a real, traceable **agent identity without an identity provider** — no IdP, no new secrets, no infrastructure at all — and still answer "which agent did this?" within your own systems from the very first run. Most identity advice skips straight to the hard part: register the agent in Entra, mint short-lived JWTs, verify them with JWKS. That's the right destination for a fleet calling protected APIs, but it's a non-starter on an air-gapped network or in an early-stage prototype where there is no directory to register anything in yet. Promptise Foundry's `AgentIdentity` is deliberately two-tier: it starts as a **local identity** that costs nothing to stand up, already stamps attribution onto everything the agent does, and upgrades *in place* to a verifiable, IdP-backed credential the day you actually need one.

<!-- more -->

## The IdP assumption is a non-starter for air-gapped and early-stage agents

Walk through the standard "give your agent an identity" checklist and you hit the same wall twice.

- **Air-gapped and on-prem deployments** often have no reachable identity provider at all — no Entra tenant to call, no metadata server, sometimes no outbound network. On-prem AI agent identity advice that begins with "register an app in your directory" simply doesn't apply. You still need to know which of ten agents on that isolated box touched a record, but the recommended tooling assumes cloud you don't have.
- **Early-stage projects** have an IdP somewhere, but wiring workload identity into a two-day prototype is premature. You want attribution *now*, on your laptop, before you've decided whether this agent ships.

In both cases the usual fallback is to give the agent nothing: it runs anonymously, and "which agent did this?" becomes a grep through process logs after the fact. That's the gap the local tier closes. A local identity is pure attribution — an `agent_id` plus optional `name`, `owner`, and `labels` — and it needs zero infrastructure. The [Agent Identity overview](../../identity/overview.md) frames the two tiers explicitly: a **local identity** for attribution you can start today, and a **verifiable identity** backed by your IdP for when a resource must cryptographically check the caller.

## Tier one: a local identity that needs zero infrastructure

A zero-infrastructure agent identity is one object and one argument. `AgentIdentity` takes a stable `agent_id` plus optional `name`, `owner`, and `labels`; you pass it to `build_agent(identity=...)`, turn on `observe=True`, and every LLM turn and tool call that agent records is tagged with *its* id. There is no key, no directory, no network call involved in creating it — constructing and inspecting an identity works fully offline.

This is runnable end-to-end with only an `OPENAI_API_KEY` set (the model call is the only thing that needs the network; the identity itself does not):

```python
import asyncio

from promptise import AgentIdentity, build_agent


async def main() -> None:
    # A local identity — no IdP, no secret, no infrastructure.
    identity = AgentIdentity(
        "billing-bot",
        name="Billing Bot",
        owner="payments-team",
        labels={"env": "prod", "site": "air-gapped-dc"},
    )

    print(identity.agent_id)       # "billing-bot"
    print(identity.is_verifiable)  # False — local tier, attribution only
    print(identity.claims())       # what gets stamped onto traces + audit

    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={},
        identity=identity,   # attach the identity right here
        observe=True,        # every turn is now tagged agent_id="billing-bot"
    )
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Summarize today's invoices."}]}
    )
    print(result["messages"][-1].content)
    await agent.shutdown()


asyncio.run(main())
```

`claims()` returns exactly what flows onto the timeline and audit log — `{"verifiable": False, "agent_id": "billing-bot", "name": "Billing Bot", "owner": "payments-team", "labels": {...}}` — and never a credential token, because at this tier there isn't one. That's the entire local contract: attach an object, get per-agent attribution. The [Identity quickstart](../../identity/quickstart.md) walks the same path in about five minutes, including a `.superagent` YAML variant if you'd rather declare `provider: local` outside code.

The two lines that matter are the constructor and the `identity=` argument. Everything else — where the id shows up, how it's recorded — the framework does for you.

## Where the local id already shows up: observability and audit

The reason a local identity is worth more than a variable you named your agent is that Promptise *stamps* it, structurally, at the points where attribution actually has to hold up.

- **Observability timeline.** With `observe=True`, every tool call and LLM turn is tagged with the agent's identifier — its `agent_id` at the local tier. Across a fleet on the same box you filter the timeline by agent instead of reconstructing "who acted" from interleaved log lines. This is `agent_id` attribution as a property of the record, not a convention you hope everyone followed.
- **Tamper-evident audit.** When you add the Guardrails audit log, each entry carries the acting agent inside an HMAC-chained JSONL file. Nothing about that requires a directory — the chain lives on local disk, which is exactly what an air-gapped deployment can rely on.
- **Cross-agent delegation.** When one agent hands work to a peer, the peer's timeline stamps `delegated_by` with the originating agent's `claims()`. So even at the local tier, delegated work traces back to the agent that *caused* it, not just the one that ran it.

None of this depends on an IdP being reachable. The local id answers "which agent did this?" entirely within your own systems — the precise thing that's off the table when the recommended answer starts with "register an app in Entra."

## What other frameworks do today

Being fair matters here, because most mainstream frameworks *do* let you attach some kind of label to an agent. The honest question isn't "do they have a name field" — it's whether that label is a first-class identity primitive with a zero-infra local tier that upgrades to a verifiable credential. The precise deltas:

- **LangChain / LangGraph** let you thread a `RunnableConfig` with `run_name`, `tags`, and `metadata` through a graph and its subgraphs, surfaced in LangSmith tracing. That's genuine observability, and you can absolutely put an agent name in `metadata`. But those values are self-asserted trace labels for a tracing product; they don't become the authorization principal on a tool call, and LangGraph's checkpointer persists graph *state*, not a governing identity. There's no identity object that starts as local attribution and later becomes a verifiable, IdP-signed credential — that plumbing is yours to build.
- **CrewAI** defines an agent with `role`, `goal`, and `backstory`. Those are descriptive strings that shape the agent's persona in prompts and orchestration — useful, but a behavior label, not a principal stamped onto each action for attribution or authorization.
- **AutoGen** gives each agent a `name` used to route messages between agents (and, in 0.4, across its distributed runtime). That name is an addressable handle for the conversation graph — routing metadata, not an identity a record is attributed to or a credential a resource verifies.
- **LlamaIndex** agents and tools authenticate to backends with keys you provide; there's no first-class per-agent identity object stamped across every action.

So the honest gap is not "nobody has anything." It's that a role, a name, a trace tag, and a metadata dict are conventions for *prompting and tracing*. To get an actual attributable principal in those frameworks, your practical options are to hand-wire a full IdP integration or to leave the agent effectively anonymous — there is no lightweight "local identity" step in between. Promptise makes that in-between step the **default first move**: `AgentIdentity("billing-bot", ...)` is a zero-infra object the framework guarantees onto every tool call, LLM turn, and audit entry, and it is the *same* object you later make verifiable. The edge is structural — the local tier and the verifiable tier are one primitive, not two systems you bridge. (For the credential side of the story — why a short-lived IdP token beats a static key — see [How does an AI agent authenticate to an API? (not API keys)](how-does-an-ai-agent-authenticate-to-an-api.md).)

## Tier two: upgrade in place to a verifiable, IdP-backed identity

The local tier is the right answer until a resource has to *verify* the caller rather than trust a self-asserted id — an internal MCP server or a protected API that must reject an agent it doesn't recognize. That's when you make the identity verifiable by backing it with a credential provider. The important part for anyone who started local: you swap the constructor, not your code. Everything downstream — the `identity=` argument, `observe=True`, the audit log — stays exactly as it was.

```python
# Same agent, same wiring — just a verifiable constructor now.
# Generic OIDC works with any issuer (CI, self-hosted, any OAuth provider):
identity = AgentIdentity.from_oidc(
    "billing-bot",
    issuer="https://idp.internal/realms/agents",
    token_env_var="AGENT_OIDC_TOKEN",
)

# Or let Promptise detect the platform and pick the provider:
identity = AgentIdentity.auto("billing-bot")
```

Once verifiable, `identity.is_verifiable` is `True`, the authoritative id can come from the credential's `sub`/`oid` claim instead of the string you passed, and the signed credential is presented to your MCP servers automatically — each scoped to that server's `audience`. The server verifies it against the issuer's published keys with `JwksAuth` and authorizes specific agents on specific tools with guards like `RequireClientId` and `HasRole`. The full outbound-and-inbound wiring — one identity, two audiences, delegation, and a signed audit log — is laid out step by step in the [end-to-end identity guide](../../identity/guide.md).

The two-tier design is the whole point: you don't choose between "anonymous prototype" and "full IdP rollout." You start with a name in two lines, get attribution immediately, and pay the cost of verifiable identity only when — and where — a resource actually demands it. And if you're already running several agents, giving each its *own* local identity is the same move that lets you later [give each AI agent its own identity, not a shared key](give-each-ai-agent-its-own-identity.md).

## Frequently asked questions

### Do I really need no IdP to get started?

Correct — the local tier needs zero infrastructure. `AgentIdentity("billing-bot", owner=..., labels=...)` is a plain object with no key, no directory, and no network call; constructing and inspecting it works fully offline. Attach it with `build_agent(identity=..., observe=True)` and you have per-agent attribution on the timeline and audit log immediately. You only reach for an identity provider when a resource must *verify* the caller.

### Is a local identity secure, or is it just a name?

It's attribution, not authentication — and it's honest about that: `is_verifiable` is `False`, and `claims()` carries no token. A local id proves nothing to a *remote* resource, so don't use it to gate access across a trust boundary. What it does give you, structurally, is a stamped record of which agent acted inside your own systems — on the observability timeline and in a tamper-evident, HMAC-chained audit log. When you need a resource to cryptographically verify the caller, upgrade the same object to the verifiable tier.

### How is this different from just naming my agent in LangSmith or a metadata field?

A trace tag, a `run_name`, a CrewAI `role`, or an AutoGen `name` is a self-asserted label that lives in prompts or tracing metadata. A local `AgentIdentity` is an object the framework stamps onto every tool call, LLM turn, and audit entry as the acting agent — and it's the *same* object that becomes a verified principal once you make it IdP-backed. One is a label for observability; the other is an identity primitive with a local tier and a verifiable tier.

### What changes in my code when I upgrade to a verifiable identity?

Just the constructor. You replace `AgentIdentity("billing-bot", ...)` with `AgentIdentity.from_oidc(...)`, `from_entra(...)`, or `AgentIdentity.auto(...)`. The `identity=` argument, `observe=True`, your tools, and your audit configuration are untouched. The framework then starts presenting the signed credential to servers and reading the authoritative id from the IdP `subject`, with no rewrite.

### Does the air-gapped local tier still work for a fleet on one box?

Yes. Give each agent a distinct `agent_id` and the timeline and audit log distinguish them without any external service. Delegated work stays attributed too — the peer stamps `delegated_by` with the originating agent's `claims()` — so on-prem AI agent identity across a fleet is answerable entirely on local disk.

## Next steps

Give your agent a name in two lines, no IdP required — start with the [Identity quickstart](../../identity/quickstart.md), which takes you from a local, attribution-only identity to a verifiable one in about five minutes. Read the [Agent Identity overview](../../identity/overview.md) to see how the two tiers, attribution, and tamper-evident audit fit together, and when you're ready to make the identity verifiable, the [end-to-end identity guide](../../identity/guide.md) wires one agent through two protected MCP servers with separate audiences and a full audit trail. New here? Add an `identity=AgentIdentity("your-agent")` argument to your very first `build_agent()` call, turn on `observe=True`, and watch the timeline name the actor — no infrastructure required.
