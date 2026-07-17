---
title: "One agent, many APIs: per-resource credentials done right"
description: "A real agent calls several backends — a billing server, a CRM — and each demands a token minted for its own audience. Hand-managing one static token per…"
keywords: "per-resource credentials for one agent, per-audience token minting, scoped credential per MCP server, token substitution prevention, audience-scoped agent token"
date: 2026-07-16
slug: per-resource-credentials-for-one-agent
categories:
  - Identity
---

# One agent, many APIs: per-resource credentials done right

Getting **per-resource credentials for one agent** right is the difference between a fleet you can reason about and a drawer full of static tokens you rotate by hand. A real agent rarely talks to one backend. `billing-bot` reads invoices from a billing MCP server *and* pulls account details from a CRM API — and each of those resources, if it takes security seriously, demands a token minted for *its own* audience. The billing server wants an `aud` of `api://billing`; the CRM wants `api://crm`; neither should ever accept the other's token. The naive answer is one static bearer per backend, provisioned and rotated separately, which is exactly where leaks and over-scoping creep in. Promptise Foundry mints a resource-scoped credential *per audience* from a single `AgentIdentity` — cached and refreshed independently, and verified server-side with an audience check that blocks a token from being replayed at the wrong resource.

## The problem: one agent, N backends, N audiences

Picture the ordinary case. Your agent needs both of these:

```python
from promptise.config import HTTPServerSpec

servers = {
    "billing": HTTPServerSpec(url="https://billing.internal/mcp"),
    "crm":     HTTPServerSpec(url="https://crm.internal/mcp"),
}
```

Both servers verify JWTs. Both require the audience claim to name *them* — a billing token presented to the CRM must be rejected, and vice versa. That single requirement is what makes a naive setup rot:

- **You mint and store two secrets.** One long-lived bearer for billing, one for CRM, each in config or a secret manager, each a copy that can leak. "One credential per resource" quietly means *N* credentials to provision.
- **Rotation is *N* separate chores.** Each token expires or gets rotated on its own schedule, in its own place, and a missed one is a silent outage or a stale grant.
- **Substitution is on you to prevent.** If the CRM's verification is sloppy about audience, a billing token works there too — and the only thing standing between you and that is server code you hand-wrote.

The right shape is the one enterprises already use for service accounts: a single identity that *projects* the correct short-lived, audience-bound token to each resource on demand. That is what [give each AI agent its own identity, not a shared key](give-each-ai-agent-its-own-identity.md) argues for at the fleet level; this post is the mechanics for the multi-backend case.

## One identity, a credential per audience

In Promptise an agent is a non-human actor with an `AgentIdentity`. Start local — no infrastructure — and you already get fleet-wide attribution; the block below is fully runnable and needs only a model API key:

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
    print(identity.is_verifiable)  # False — attribution only, no provider yet

    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={},
        identity=identity,
        observe=True,   # every tool call and LLM turn is now tagged agent_id
    )
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Summarize today's invoices."}]}
    )
    print(result["messages"][-1].content)
    await agent.shutdown()


asyncio.run(main())
```

A local identity is pure attribution. The moment the agent calls a *protected* resource, upgrade the **same** identity to verifiable by backing it with a credential provider — Microsoft Entra, AWS IAM, Google Cloud, SPIFFE/SPIRE, or a generic OIDC issuer. Now the identity can mint a signed JWT, and this is where the per-resource part becomes structural. You declare the audience each backend expects on its `HTTPServerSpec`, and the one identity mints a resource-scoped credential per audience:

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

That is the whole "present" step. Pass `identity=`, and every server that has no `bearer_token` of its own automatically receives the agent's credential — scoped to *that server's* `audience`. The billing server only ever sees a token whose `aud` claim is `api://billing`; the CRM only ever sees `api://crm`. An explicit per-server `bearer_token` still wins if you set one. And you can mint by hand anywhere you need a header — a raw HTTP call, a custom client:

```python
identity.auth_header("api://billing")  # {"Authorization": "Bearer <jwt aud=api://billing>"}
identity.auth_header("api://crm")       # {"Authorization": "Bearer <jwt aud=api://crm>"}
identity.get_credential("api://billing")  # the scoped JWT itself
```

With a static-key setup, "one credential per resource" is *N* keys to provision, store, and rotate. Here it is one workload identity that projects the correct audience-bound token to each resource. The [Agent Identity overview](../../identity/overview.md) frames the two tiers — local vs verifiable — and the [end-to-end identity guide](../../identity/guide.md) wires this exact two-server scenario from mint to audit.

## Active vs passive providers: what `audience=` actually does

Not every credential source can re-mint on demand, and Promptise is honest about the split rather than pretending otherwise. What `audience=` does depends on the provider kind:

| Provider kind | Providers | `audience=` behaviour |
| --- | --- | --- |
| **Active** (re-mints on demand) | Entra IMDS, AWS STS, GCP metadata, SPIFFE SDK | Mints a credential scoped to the requested audience; falls back to the factory default when omitted. |
| **Passive** (fixed audience) | EKS / AKS projected-token files, OIDC file / env / callable | The audience is fixed at issue time by the platform; a per-request `audience` is accepted but **ignored**. |

An active provider — an Azure managed identity over IMDS, AWS STS, the GCP metadata server, the SPIFFE Workload API — will mint a distinct token for each audience you ask for. So one `AgentIdentity.from_entra(...)` genuinely serves billing *and* CRM with separate, correctly-scoped tokens.

A passive source is different, and pretending it re-mints would be a lie that fails in production. A Kubernetes projected-token file or an OIDC file/env token carries exactly the audience the platform stamped into it at issue time. Promptise accepts a per-request `audience` there but does not silently forge a new one — it cannot re-sign what it did not sign. If a projected-token identity needs a *second* audience, you issue a second token at the source (a second projected-token volume, a different CI claim) and build a second identity. That constraint is documented, not hidden, in [Per-resource credentials](../../identity/architecture.md#per-resource-credentials).

Either way, caching is per-audience. Each distinct audience is cached and refreshed on its own:

- The framework reads each credential's standard `exp` claim and re-acquires the token for that audience once it is within `CREDENTIAL_REFRESH_BUFFER_SECONDS` (60 seconds) of expiry.
- A credential with no decodable `exp` — an opaque token, or a projected token the platform rotates in place — is treated as always-stale and re-read on every use, so in-place rotation is always observed.
- Concurrent callers for the same audience collapse into a single acquisition via a lock; the cache is process-local.

So repeated billing calls reuse one cached `api://billing` token while the CRM gets its own `api://crm` token on its own expiry clock. The full [credential lifecycle](../../identity/architecture.md#credential-lifecycle) — rotation, expiry, and the always-stale rule — is spelled out in the architecture reference.

## Close the loop server-side: audience-required JwksAuth blocks replay

Minting a scoped token is only half the guarantee. The other half is that each resource *rejects* a token minted for a different one — token substitution prevention has to be enforced where the token lands. On the server, the Promptise MCP Server SDK verifies the agent's IdP token against the issuer's published keys with `JwksAuth.from_discovery()`, and **`audience` is required**:

```python
from promptise.mcp.server import (
    MCPServer, AuthMiddleware, JwksAuth, RequireClientId, AuditMiddleware,
)

server = MCPServer(name="billing")

# Verify tokens this IdP issued for THIS resource. `audience` is mandatory —
# it is what stops a token minted for api://crm being replayed here.
auth = JwksAuth.from_discovery(
    issuer="https://login.microsoftonline.com/<tenant>/v2.0",
    audience="api://billing",
)
server.add_middleware(AuthMiddleware(auth))

# Tamper-evident audit: each entry records the VERIFIED agent identity
# (subject / issuer / audience / roles) inside an HMAC chain.
server.add_middleware(AuditMiddleware(log_path="billing-audit.jsonl", signed=True))


@server.tool(auth=True, guards=[RequireClientId("billing-bot")])
async def issue_refund(ctx, invoice_id: str, amount: float) -> str:
    # ctx.client.subject -> the IdP id of the calling agent, cryptographically verified
    return f"Refunded {amount} on {invoice_id}"
```

The billing server declares `audience="api://billing"`, so a token whose `aud` is `api://crm` fails verification at the door — before any tool runs. The mint side scopes the token; the verify side refuses anything else. That is the closed loop: an `api://crm` token is worthless at billing not by convention but by two enforced invariants. Because the credential is a short-lived JWT from your IdP, key rotation needs no redeploy (`JwksAuth` re-fetches published keys on demand) and revocation is one switch (disable the agent in the directory; its tokens stop validating everywhere as they expire). The credential side of *why* a short-lived IdP token beats a static key is covered in [how does an AI agent authenticate to an API? (not API keys)](how-does-an-ai-agent-authenticate-to-an-api.md).

## What other frameworks do today

Be fair here: you can point an agent at several backends with different tokens in every mainstream framework. What differs is whether *deriving* those audience-scoped tokens from one identity, and *preventing* their substitution, is a framework primitive or your homework.

- **LangChain / LangGraph** connect to MCP servers through `langchain-mcp-adapters`' `MultiServerMCPClient`, and each server's config accepts its own `headers` dict — including `Authorization`. So you genuinely *can* put a different bearer per server. The delta is precise: you mint, store, and rotate each of those *N* tokens yourself; nothing derives them from a single agent identity, binds each to an audience, or refreshes them per resource. It is a per-server slot, not per-resource minting.
- **CrewAI** and **AutoGen** authenticate tools with the tokens or API keys you configure (typically env vars). Neither's open-source core mints a credential from the agent's own cloud workload identity, and neither scopes a per-audience token per backend for you.
- **LlamaIndex** tool specs take an API key or token you pass in at construction — one credential per tool that you supply and manage.
- The **MCP specification itself** defines an OAuth 2.1 authorization framework for HTTP transports (2025 spec): the client obtains a token from an authorization server, and a resource can require a matching audience. That is real and worth using — but it standardizes the token *handoff* and *validation*; it does not source the token from the agent's managed identity, IRSA role, or SPIFFE SVID, nor mint one per audience from a single identity for you. You still wire acquisition and the per-resource fan-out.

So the honest gap is not "competitors can't send per-server tokens" — with a `headers` dict, they can. It is that stopping a token minted for one resource from being replayed at another, and deriving all those scoped tokens from *one* agent identity, is left to you to assemble and get right. Promptise makes **per-audience minting a first-class property of the `AgentIdentity`** and makes **`audience` required on `JwksAuth`**, so the substitution guard is structural rather than something you remember to code. That is the difference: a capability you wire and police yourself versus an invariant the framework holds.

## Frequently asked questions

### How does one agent get a different token for each API?

Give it a verifiable `AgentIdentity` (backed by Entra, AWS, GCP, SPIFFE, or OIDC) and declare the audience each backend expects on its `HTTPServerSpec` — `audience="api://billing"`, `audience="api://crm"`. When you pass `identity=` to `build_agent()`, each server without its own `bearer_token` automatically receives a credential scoped to *its* audience, all minted from the one identity. You can also mint by hand with `identity.auth_header("api://crm")`.

### What stops a token minted for one resource from being used at another?

Two enforced invariants. The mint side scopes each token to a single audience. The verify side, `JwksAuth.from_discovery(issuer=..., audience=...)`, *requires* `audience` and rejects any token whose `aud` claim does not match — so a token minted for `api://crm` fails verification at the billing server before any tool runs. Neither side trusts a self-asserted value.

### My provider can't re-mint per audience. Now what?

That is the passive case — a Kubernetes projected token or an OIDC file/env token carries the fixed audience the platform stamped at issue time, and a per-request `audience` is accepted but ignored. Promptise will not forge a token it did not sign. Issue a second token at the source (a second projected-token volume, a different CI claim) and build a second identity for the second audience. The active providers (Entra IMDS, AWS STS, GCP metadata, SPIFFE SDK) re-mint per audience from one identity.

### Do the per-audience tokens share a cache or refresh together?

No — each audience is cached and refreshed independently, keyed by audience and expiring on its own `exp`. A token is re-acquired within `CREDENTIAL_REFRESH_BUFFER_SECONDS` (60s) of expiry; a token with no decodable `exp` is treated as always-stale and re-read every use so in-place rotation is observed. Repeated calls to the same resource reuse one token while different resources get their own.

### Do I need cloud infrastructure to start?

No. A local `AgentIdentity` needs zero infrastructure and gives you fleet-wide attribution immediately. You upgrade the *same* object to a verifiable, IdP-backed credential — and unlock per-audience minting — only when a resource must cryptographically verify the caller. You swap the constructor, not your code.

## Next steps

See per-resource credentials in action in the [architecture reference](../../identity/architecture.md#per-resource-credentials) — it lays out active vs passive providers, per-audience caching, and the always-stale rotation rule in full. Then wire the two-server, two-audience scenario end to end with the [end-to-end identity guide](../../identity/guide.md), and skim the [Agent Identity overview](../../identity/overview.md) to place attribution, outbound auth, and inbound verification in one picture. New to the framework? Add an `identity=` argument to your first `build_agent()` call with `observe=True`, then set `audience=` on each `HTTPServerSpec` the day your agent starts calling a second protected backend.
