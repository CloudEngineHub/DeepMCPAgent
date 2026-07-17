---
title: "Give an AI agent a SPIFFE SVID identity in Kubernetes"
description: "If your platform runs SPIRE, every workload already gets a cryptographic SVID from the Workload API — but no agent framework consumes one to authenticate the…"
keywords: "SPIFFE SVID identity for AI agents, SPIRE workload identity agent, JWT-SVID for MCP auth, Kubernetes agent identity, from_spiffe provider, mesh identity for AI agents"
date: 2026-07-16
slug: spiffe-svid-identity-for-ai-agents
categories:
  - Identity
---

# Give an AI agent a SPIFFE SVID identity in Kubernetes

A SPIFFE SVID identity for AI agents is the cleanest way to authenticate an autonomous agent inside a service mesh you already run: if your platform uses SPIRE, every workload already receives a short-lived, cryptographic SVID from the Workload API, yet no mainstream agent framework consumes one to prove *which* agent is acting. This how-to closes that gap. You will wire `AgentIdentity.from_spiffe` into `build_agent`, let SPIRE mint a per-audience JWT-SVID for each MCP server the agent calls, and verify those tokens server-side with `JwksAuth` against your trust domain — with automatic SVID rotation and not one static secret in code or `env`.

<!-- more -->

## What a SPIFFE SVID actually gives your agent

SPIFFE (the Secure Production Identity Framework For Everyone) issues each workload a **SVID** — a SPIFFE Verifiable Identity Document — bound to a SPIFFE ID like `spiffe://example.org/ns/prod/sa/billing-bot`. SPIRE, its reference implementation, runs a node agent that serves the local Workload API socket; a workload asks that socket for its identity and gets back a signed credential no human provisioned. There are two SVID shapes: an **X.509-SVID** for mTLS, and a **JWT-SVID** — a short-lived, signed JWT scoped to a requested `audience`. For an agent calling MCP servers over HTTP with a bearer token, the JWT-SVID is the right instrument.

That maps exactly onto how Promptise models agents. An agent is a non-human actor, and "which agent did this?" needs a verifiable answer, not a shared key or a name a process prints about itself. The [Agent Identity overview](../../identity/overview.md) lays out the two tiers: a **local** identity (zero infrastructure, pure attribution) and a **verifiable** identity backed by a credential provider — Entra, AWS, GCP, a generic OIDC issuer, or **SPIFFE/SPIRE** — that mints a signed token the resource can check. A SPIFFE SVID is the mesh-native way to make an agent's identity verifiable. And because the credential is minted from the workload's own identity, there is nothing to store or leak — the argument [give each AI agent its own identity, not a shared key](give-each-ai-agent-its-own-identity.md) makes in full applies directly here.

## What other frameworks do today

Be precise about the delta, because it is not "competitors can't authenticate." Every mainstream framework can send a bearer token; SPIFFE itself ships excellent workload-side libraries. What is missing is a framework that *consumes* an SVID as the acting agent's identity.

- **LangChain / LangGraph** connect to MCP servers through `langchain-mcp-adapters`' `MultiServerMCPClient`, whose per-server config accepts a `headers` dict — including `Authorization`. That is a *slot*: you would fetch the JWT-SVID yourself (via `py-spiffe`'s `JwtSource`/`WorkloadApiClient`, or a `spiffe-helper`-written file), inject it into the header, and re-fetch it before the short TTL expires. The rotation loop and the per-audience minting are your code, not the framework's.
- **CrewAI** and **AutoGen** authenticate an agent's tools with the tokens or API keys you configure (typically env vars). Neither's open-source core calls the SPIRE Workload API or treats an SVID as the agent's principal.
- **LlamaIndex** tool specs take a key or token you pass at construction — again, a value you source and refresh by hand.
- **The SPIFFE ecosystem** (`go-spiffe`, `py-spiffe`, `spiffe-helper`) is the real, mature way to fetch and rotate SVIDs. It is workload plumbing, though — none of it wires an SVID into an agent's tool-calling loop, per-server audience selection, or the audit trail of who acted.

So the honest gap is structural, not "nobody has anything": to give an agent a SPIFFE identity elsewhere, you hand-roll the Workload API call, the SVID rotation, and the per-audience JWT-SVID minting, then thread the result into a headers slot. Promptise's `from_spiffe` provider makes all of that a first-class property of the agent. The broader version of this argument — why a short-lived, provider-minted token beats a static key an agent presents — is in [How does an AI agent authenticate to an API? (not API keys)](how-does-an-ai-agent-authenticate-to-an-api.md).

## Consume the Workload API SVID with AgentIdentity.from_spiffe

Start local so you can see the identity primitive before any SPIRE wiring. This block is fully runnable — it needs only a model API key. A local `AgentIdentity` is pure attribution: every tool call and LLM turn the agent records is tagged with its `agent_id`, no infrastructure required.

```python
import asyncio

from promptise import AgentIdentity, build_agent


async def main() -> None:
    # Start local: a real, non-human identity — attribution only, zero infra.
    # You upgrade the SAME object to from_spiffe once SPIRE is in the mesh.
    identity = AgentIdentity(
        "billing-bot",
        name="Billing Bot",
        owner="payments",
        labels={"env": "prod", "mesh": "spire"},
    )
    print(identity.agent_id)       # "billing-bot"
    print(identity.is_verifiable)  # False — no SVID yet, attribution only

    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={},
        identity=identity,
        observe=True,   # every tool call and LLM turn is tagged with agent_id
    )
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Summarize today's invoices."}]}
    )
    print(result["messages"][-1].content)
    await agent.shutdown()


asyncio.run(main())
```

Now make the identity verifiable by backing it with SPIRE. `from_spiffe` has two modes, and `mode="auto"` (the default) picks between them: **file** mode reads a JWT-SVID that `spiffe-helper` writes to a path (no extra dependency), and **SDK** mode fetches a JWT-SVID directly from the SPIRE agent's Workload API socket via `pyspiffe` (`pip install promptise[identity-spiffe]`). In a Kubernetes pod with the SPIRE agent socket mounted, SDK mode is the natural fit:

```python
from promptise import AgentIdentity, build_agent
from promptise.config import HTTPServerSpec

# SDK mode: fetch a JWT-SVID from the SPIRE agent's Workload API socket.
# No token in code, no secret in env — the SVID comes from the mesh.
identity = AgentIdentity.from_spiffe(
    "billing-bot",
    mode="sdk",
    socket_path="unix:///run/spire/agent/api.sock",  # or $SPIFFE_ENDPOINT_SOCKET
    audience="api://billing",                          # default audience
)
print(identity.credential_provider)   # "spiffe-sdk"  (or "spiffe-file")
print(identity.is_verifiable)         # True
print(identity.get_credential())      # a signed JWT-SVID (sub = the SPIFFE ID)
```

The per-mode prerequisites, the `$SPIFFE_ENDPOINT_SOCKET` fallback, and a troubleshooting table for the common failures (SDK mode without `pyspiffe`, no SPIRE agent on the socket, a missing registration entry, `spiffe-helper` writing elsewhere) live on the [SPIFFE / SPIRE provider page](../../identity/providers/spiffe.md).

## One identity, a JWT-SVID per audience

Here is the part a headers slot can't answer cleanly: the billing server and the CRM server require *different* audiences — how does the agent send each the right token without juggling two credentials? You declare the audience each server expects on its `HTTPServerSpec`, pass the one identity to `build_agent`, and Promptise mints a **resource-scoped JWT-SVID per audience** — the same wiring the [end-to-end identity guide](../../identity/guide.md) walks for two servers:

```python
agent = await build_agent(
    model="openai:gpt-5-mini",
    identity=identity,
    observe=True,
    servers={
        # Each server receives a JWT-SVID minted for ITS audience.
        # A token with aud=api://billing is worthless at the CRM server.
        "billing": HTTPServerSpec(url="https://billing.internal/mcp",
                                  audience="api://billing"),
        "crm":     HTTPServerSpec(url="https://crm.internal/mcp",
                                  audience="api://crm"),
    },
)
```

A few honest specifics worth pinning down:

- **SDK mode is active; file mode is fixed-audience.** Because the Workload API can mint an SVID for any requested audience, SDK mode re-mints a distinct, audience-scoped JWT-SVID per server. A `spiffe-helper` file, by contrast, carries the single audience it was written with — for per-resource scoping there, configure `spiffe-helper` (or use SDK mode). This mirrors the active-vs-passive split documented for every provider.
- **Automatic presentation, explicit override.** Every server without its own `bearer_token` receives the agent's SVID automatically; an explicit per-server `bearer_token` always wins.
- **Present it by hand anywhere.** `identity.auth_header("api://billing")` returns `{"Authorization": "Bearer <jwt-svid>"}` for any non-MCP HTTP call.

The [end-to-end identity guide](../../identity/guide.md) wires exactly this two-server scenario from mint through present, verify, and audit — swap its provider factory for `from_spiffe` and the rest of the flow is identical.

## Verify the JWT-SVID server-side against your trust domain

An outbound SVID means nothing unless the resource checks it. SPIRE's **OIDC Discovery Provider** publishes the trust domain's signing keys as a standard JWKS, so `JwksAuth` can verify a JWT-SVID with no shared secret and no reconfiguration when SPIRE rotates keys. Point it at that JWKS URL and set `audience` — which is required, because it is exactly what stops an agent replaying an SVID minted for a different resource:

```python
from promptise.mcp.server import (
    MCPServer, AuthMiddleware, JwksAuth, RequireClientId, AuditMiddleware,
)

server = MCPServer(name="billing")

# Verify JWT-SVIDs against the SPIRE trust domain. The OIDC Discovery Provider
# serves the trust bundle as a JWKS, so there is no shared secret to manage.
# audience is required; it rejects an SVID minted for a different resource.
auth = JwksAuth(
    jwks_url="https://oidc.example.org/keys",  # SPIRE OIDC Discovery Provider
    audience="api://billing",
)
server.add_middleware(AuthMiddleware(auth))

# Tamper-evident audit: each entry records the verified SPIFFE ID in an HMAC chain.
server.add_middleware(AuditMiddleware(log_path="billing-audit.jsonl", signed=True))


@server.tool(
    auth=True,
    guards=[RequireClientId("spiffe://example.org/ns/prod/sa/billing-bot")],
)
async def issue_refund(ctx, invoice_id: str, amount: float) -> str:
    # ctx.client.subject -> the verified SPIFFE ID of the calling agent
    return f"Refunded {amount} on {invoice_id}"
```

Because a JWT-SVID's `sub` claim *is* the SPIFFE ID, `RequireClientId` gates the tool to a specific mesh identity — the reporting bot's SVID, presented to the same server, is rejected before `issue_refund` runs. `JwksAuth` verifies the signature against the trust bundle and enforces `audience` and expiry; if your OIDC Discovery Provider also stamps an `iss` you recognize, add `issuer=` for defense in depth. Every field the server sees after verification is on `ctx.client`.

Rotation and revocation come for free from the model. SPIRE issues JWT-SVIDs with short TTLs, so in SDK mode Promptise re-acquires the credential from the Workload API as it nears expiry (a `spiffe-helper` file is re-read on each refresh so an in-place rewrite is always observed) — there is no key to hand-rotate. Key rotation on SPIRE just works, since `JwksAuth` re-fetches the JWKS on demand. And revocation is a mesh operation: delete the workload's registration entry in the SPIRE server and its short-lived SVIDs stop validating everywhere as they expire — no server reconfiguration, no hunting a leaked secret.

## Frequently asked questions

### Does Promptise call the SPIRE Workload API, or do I fetch the SVID myself?

Promptise calls it. In SDK mode, `AgentIdentity.from_spiffe(..., mode="sdk")` fetches the JWT-SVID from the SPIRE agent socket via `pyspiffe` for you, re-acquires it before the TTL expires, and presents it — scoped to each server's audience — automatically. In file mode it reads a JWT-SVID that `spiffe-helper` maintains. Either way you do not write the Workload API call, the rotation loop, or the per-audience minting by hand.

### JWT-SVID or X.509-SVID — which does `from_spiffe` use?

The JWT-SVID. MCP servers here are called over HTTP with a bearer token, and a JWT-SVID is a signed JWT scoped to a requested `audience`, which is exactly what a bearer flow needs. X.509-SVIDs are for mTLS between workloads and are a different transport concern.

### How do I verify a SPIFFE SVID without a shared secret?

Run SPIRE's OIDC Discovery Provider, which publishes the trust domain's keys as a JWKS, and point `JwksAuth(jwks_url=..., audience=...)` at it. It fetches the keys on demand and caches them, so key rotation needs no redeploy, and the required `audience` check rejects any SVID minted for another resource. The verified SPIFFE ID lands on `ctx.client.subject`, so `RequireClientId` and `HasRole` guards can authorize specific mesh identities.

### Can one agent authenticate to several MCP servers with different audiences?

Yes. Give each server a distinct `audience` on its `HTTPServerSpec`. In SDK mode the Workload API re-mints a per-audience JWT-SVID from the single identity, so a token for `api://billing` cannot be replayed against `api://crm`. A `spiffe-helper` file carries one fixed audience; configure it (or use SDK mode) when you need per-resource scoping.

## Next steps

Follow the SPIFFE provider setup and ship a mesh-verified agent today. Start from the [SPIFFE / SPIRE provider page](../../identity/providers/spiffe.md) to confirm your Workload API socket and registration entry, then read the [Agent Identity overview](../../identity/overview.md) to see how attribution, per-audience outbound auth, and inbound verification fit together. When you are ready to wire two protected servers end to end — mint, present, verify, and audit — the [end-to-end identity guide](../../identity/guide.md) walks the full flow; swap its provider factory for `AgentIdentity.from_spiffe` and you have a Kubernetes agent identity backed by the SVID your mesh already issues.
