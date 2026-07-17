---
title: "How does an AI agent authenticate to an API? (not API keys)"
description: "The default answer — mint a static API key per agent and hope it never leaks — is the anti-pattern modern identity abandoned for humans. This pillar walks…"
keywords: "how does an AI agent authenticate to an API, AI agent API authentication, agent authentication without API keys, verifiable agent credential, MCP server authentication for agents, workload identity for AI agents"
date: 2026-07-16
slug: how-does-an-ai-agent-authenticate-to-an-api
categories:
  - Identity
---

# How does an AI agent authenticate to an API? (not API keys)

How does an AI agent authenticate to an API without a static, long-lived key you have to babysit? The default answer — mint one API key per agent, paste it into the tool config, and hope it never leaks — is the exact anti-pattern that modern identity abandoned for humans a decade ago. Humans stopped carrying passwords into every system and moved to short-lived, provider-issued tokens; agents, a brand-new class of non-human actor, deserve the same. This pillar walks the real alternative end to end: a short-lived credential minted from the agent's *own* workload identity, presented to each API automatically, and verified server-side with published keys. No secret to store, rotate, or leak.

First, the one distinction that untangles this whole topic: **the API key that authenticates your LLM call is not the credential that authenticates your agent to a downstream API.** The model keeps its own key. Everything below is about *who is acting* when the agent then reaches out to a billing service, a CRM, or an internal MCP server. The [Agent Identity overview](../../identity/overview.md) states it plainly — identity is orthogonal to the model credential.

## The default answer, and why it's the wrong one

The path of least resistance is to give each agent a bearer token or API key, drop it into an environment variable, and wire it into the tool config. It works on day one. It rots by month three:

- **It's a long-lived secret.** A static key sits in config, in CI, in a `.env`, in a screenshot. Every copy is a leak waiting to happen, and a leaked agent key grants exactly the access the agent had.
- **Rotation is manual and risky.** Rotating means editing every place that holds the key and redeploying without a gap. Most teams simply don't, so keys outlive the agents that used them.
- **Revocation is slow.** When you need to cut off one agent *now*, you're hunting the key across services instead of flipping one switch.
- **Attribution is guesswork.** A key shared across a fleet makes "which agent did this?" unanswerable after an incident.

None of this is new. It's precisely why people, workloads in Kubernetes, and cloud services all moved to directory-issued, short-lived credentials. Agents are the newest workload — [give each AI agent its own identity, not a shared key](give-each-ai-agent-its-own-identity.md) makes the case in full.

## What other frameworks do today

Be fair here: you *can* authenticate an agent to an API in every mainstream framework. Nobody is missing the ability to send a bearer token. What differs is how much of the loop the framework owns versus how much you wire and rotate yourself.

- **LangChain / LangGraph** connect to MCP servers through `langchain-mcp-adapters`' `MultiServerMCPClient`, where each server's config accepts a `headers` dict — including `Authorization`. For non-MCP tools, auth is whatever you code, usually a key from an env var. So it gives you a *slot* for a static bearer that you supply and rotate.
- **CrewAI** and **AutoGen** authenticate tools with the tokens or API keys you configure (typically env vars); neither's open-source core mints a credential from the agent's own cloud workload identity.
- **LlamaIndex** tool specs take an API key or token you pass in at construction.
- The **MCP specification itself** does define an OAuth 2.1 authorization framework for HTTP transports (2025 spec), where the client obtains a token from an authorization server. That's real and worth using — but it standardizes the token *handoff*; it doesn't source the token from the agent's managed identity, IRSA role, or SPIFFE SVID, nor mint one per resource for you. You still wire the acquisition.

So the honest delta is not "they can't authenticate." It's that authentication is left to you as a credential *slot* to fill and a secret to rotate. What no mainstream framework ships as a single first-class primitive is the full loop: **mint** a short-lived credential from the agent's own workload identity, **present** a per-resource credential to each API automatically, and **verify** it server-side with JWKS. Promptise Foundry wires all three together, so authentication becomes a property of the agent rather than a config field you maintain.

## The real answer: mint, present, verify — as one loop

Promptise models the agent as a non-human actor with an **`AgentIdentity`**. Start local — no infrastructure — and you already get fleet-wide attribution. This block is fully runnable; it needs only a model API key:

```python
import asyncio

from promptise import AgentIdentity, build_agent


async def main() -> None:
    # A real, non-human identity for the agent — not a key you minted by hand.
    identity = AgentIdentity(
        "billing-bot",
        name="Billing Bot",
        owner="payments",
        labels={"env": "prod"},
    )
    print(identity.agent_id)       # "billing-bot"
    print(identity.is_verifiable)  # False — attribution only, no infrastructure yet

    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={},
        identity=identity,
        observe=True,   # every tool call and LLM turn is now tagged with agent_id
    )
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Summarize today's invoices."}]}
    )
    print(result["messages"][-1].content)
    await agent.shutdown()


asyncio.run(main())
```

When the agent starts calling *protected* APIs, upgrade the same identity to **verifiable** by backing it with a credential provider — Microsoft Entra, AWS IAM, Google Cloud, SPIFFE/SPIRE, or a generic OIDC issuer. The credential is now a short-lived, signed JWT the provider mints from the workload's own identity — an Azure managed identity, an EKS IRSA role, a SPIFFE SVID, a metadata token — so there is no stored secret at all:

```python
from promptise import AgentIdentity, build_agent
from promptise.config import HTTPServerSpec

# Minted from the agent's own workload identity — nothing to store or rotate.
identity = AgentIdentity.from_entra(
    "billing-bot", client_id="<managed-identity-client-id>"
)

agent = await build_agent(
    model="openai:gpt-5-mini",
    identity=identity,
    servers={
        "billing": HTTPServerSpec(url="https://billing.internal/mcp",
                                  audience="api://billing"),
        "crm":     HTTPServerSpec(url="https://crm.internal/mcp",
                                  audience="api://crm"),
    },
)
```

That's the whole "present" step: pass `identity=`, and every server without its own `bearer_token` receives the agent's credential automatically. `AgentIdentity.auto("billing-bot")` will even detect the platform and pick the provider for you. The per-platform setup for Azure is in the [Microsoft Entra provider guide](../../identity/providers/entra.md).

## One identity, a credential per API — the part that's hard to bolt on

Here is the question a `headers` slot can't cleanly answer: *the billing server and the CRM server require different audiences — how do you send each the right token without managing two keys?*

Promptise makes this structural. You declare the audience each API expects on its `HTTPServerSpec` (`audience="api://billing"`, `audience="api://crm"` above), and the one identity mints a **resource-scoped credential per audience**. The billing server only ever sees a token whose `aud` claim is `api://billing`; the CRM server only ever sees `api://crm`. A token minted for one API is worthless at the other:

```python
identity.auth_header("api://billing")  # {"Authorization": "Bearer <jwt aud=api://billing>"}
identity.auth_header("api://crm")      # {"Authorization": "Bearer <jwt aud=api://crm>"}
```

With a static-key setup, "one credential per resource" means *N* keys to provision, store, and rotate. Here it's one workload identity that projects the correct short-lived, audience-bound token to each resource on demand — the [end-to-end identity guide](../../identity/guide.md) wires exactly this two-server scenario from mint to audit. This is the concrete edge: not that competitors *can't* send per-resource tokens, but that Promptise makes per-audience minting an invariant of the identity rather than a pile of secrets you juggle.

## Verify it server-side with JWKS, and revoke from your IdP

A credential is only worth something if the resource *checks* it. On the server side, the Promptise MCP Server SDK verifies the agent's IdP token against the issuer's published keys with `JwksAuth.from_discovery()` — no shared secret, and IdP key rotation needs no reconfiguration. Setting `audience` is required: it's what stops an agent from replaying a token minted for a different API.

```python
from promptise.mcp.server import (
    MCPServer, AuthMiddleware, JwksAuth, RequireClientId, AuditMiddleware,
)

server = MCPServer(name="billing")

# Verify tokens this IdP issued for THIS resource. `audience` is mandatory.
auth = JwksAuth.from_discovery(
    issuer="https://login.microsoftonline.com/<tenant>/v2.0",
    audience="api://billing",
)
server.add_middleware(AuthMiddleware(auth))

# Tamper-evident audit: each entry records the VERIFIED agent identity
# (subject / issuer / audience) inside an HMAC chain.
server.add_middleware(AuditMiddleware(log_path="billing-audit.jsonl", signed=True))


@server.tool(auth=True, guards=[RequireClientId("billing-bot")])
async def issue_refund(ctx, invoice_id: str, amount: float) -> str:
    # ctx.client.subject -> the IdP id of the calling agent, cryptographically verified
    return f"Refunded {amount} on {invoice_id}"
```

Now the loop is closed. The token flows from your directory, through the agent, to the API — verified cryptographically, never self-asserted — and every call is recorded against the *verified* subject, which is what makes fleet attribution trustworthy after the fact ([which AI agent did this?](ai-agent-action-attribution.md) covers that side).

The lifecycle is where the anti-pattern finally dies. Because the credential is a short-lived JWT from your IdP:

- **Rotation and expiry** are automatic — Promptise reads the JWT `exp` and re-acquires per audience as it nears expiry. There is no key to hand-rotate.
- **Key rotation** on the IdP just works — `JwksAuth` re-fetches published keys on demand, so no server redeploys when signing keys change.
- **Revocation is one switch** — disable the agent's identity in the directory, and its short-lived credentials stop validating at every resource as they expire. No hunting a leaked key across services.

## Frequently asked questions

### How does an AI agent authenticate to an API without an API key?

It presents a short-lived, signed JWT minted from its own workload identity (an Azure managed identity, AWS IAM role, GCP service account, or SPIFFE SVID) instead of a static key. Promptise mints that credential per audience, attaches it to each `HTTPServerSpec` automatically, and the receiving server verifies it against the issuer's published keys with `JwksAuth.from_discovery()`. There is no long-lived secret in config to leak or rotate.

### Isn't a bearer token just another API key?

No — the difference is lifetime and provenance. An API key is long-lived, self-provisioned, and stored wherever you paste it. A workload-identity JWT is short-lived, minted on demand from a directory that is the system of record, scoped to a single audience, and revoked by disabling the identity centrally. You never store or rotate it yourself.

### Do I need cloud infrastructure to start?

No. A local `AgentIdentity` needs zero infrastructure and immediately gives you fleet-wide attribution on the observability timeline and audit log. You upgrade the *same* identity object to a verifiable, IdP-backed credential only when a resource must cryptographically verify the caller — no rewrite, just a different constructor.

### How is this different from LangChain, CrewAI, or the MCP OAuth spec?

Those give you a place to put a credential — a `headers` dict, an env-var key, or a standardized OAuth token handoff — and leave acquisition and rotation to you. Promptise ships the full loop as one primitive: mint from the agent's workload identity, present a per-audience token to each API automatically, and verify server-side with JWKS.

## Next steps

Start with the [Agent Identity overview](../../identity/overview.md) to see how attribution, outbound auth, and inbound verification fit together, then wire the full mint → present → verify loop for two protected servers in the [end-to-end identity guide](../../identity/guide.md). Running on Azure? The [Microsoft Entra provider guide](../../identity/providers/entra.md) has the per-platform setup. New to the framework? Add an `identity=` argument to your first `build_agent()` call and turn on `observe=True` — you'll have a traceable, key-free agent in minutes.
