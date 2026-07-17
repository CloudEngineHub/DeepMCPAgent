---
title: "Microsoft Entra Agent ID with Promptise Foundry"
description: "A framework-specific walkthrough for teams already standardized on Entra: back an agent with a managed identity or Agent ID, present a resource-scoped token…"
keywords: "Entra agent identity, Microsoft Entra Agent ID, Azure managed identity for agents, AKS workload identity JWT, federated token agent, Entra MCP authentication"
date: 2026-07-16
slug: entra-agent-identity
categories:
  - Identity
---

# Microsoft Entra Agent ID with Promptise Foundry

If your organization already runs on Azure, wiring **Entra agent identity** into your AI agents is less about adopting something new and more about consuming an identity you can already issue. This post is for teams standardized on Microsoft Entra who want a billing bot, a reporting bot, or any autonomous agent to present a signed, Entra-verified token to the MCP servers it calls — automatically, with no static API keys to leak or rotate. By the end you will know exactly which piece Azure owns, which piece Promptise Foundry owns, and how to get from a managed identity to a verified tool call in one `build_agent()` call.

<!-- more -->

## What Entra agent identity actually gives you

An agent is a non-human actor. It calls tools, hits internal APIs, and often acts with no human in the loop — so "which agent did this?" needs a real answer, not a shared key or a name a process prints about itself. Promptise gives every agent a stable identity for attribution, and, when you want it, a **verifiable** identity backed by a credential provider that mints a short-lived signed JWT. The [Agent Identity overview](../../identity/overview.md) lays out the two tiers: a local identity (zero infrastructure, just an `agent_id`) and a verifiable identity backed by Entra, AWS, GCP, SPIFFE, or a generic OIDC issuer.

The boundary matters, and it is the thing that decides whether this fits your stack:

- **Azure owns the directory.** You register the agent's identity in Entra once — as a user-assigned managed identity, an app with a federated credential, or an **Entra Agent ID**. That directory is the system of record: it persists, inventories, governs, and revokes the identity.
- **Promptise consumes the token.** It reads the credential Azure already issues (from IMDS or the AKS-projected file), scopes it to the resource being called, and presents it. Promptise keeps no identity store of its own.

That division is why there are no new secrets to manage. Revocation is a directory operation: disable the identity in Entra and its short-lived credentials stop validating everywhere, with no server reconfiguration.

## IMDS vs. AKS workload identity JWT: pick by where the agent runs

`AgentIdentity.from_entra` supports two acquisition modes, and `mode="auto"` (the default) chooses between them for you:

- **IMDS** — for VM, VMSS, and Container Apps workloads using a managed identity. Promptise reads an `id_token` from the Azure Instance Metadata Service at `169.254.169.254`.
- **Projected token** — for **AKS Workload Identity**. Promptise reads the JWT that AKS projects to `$AZURE_FEDERATED_TOKEN_FILE`. This is the federated token agent path — no metadata endpoint involved.

With `mode="auto"`, Promptise picks projected when `$AZURE_FEDERATED_TOKEN_FILE` is set and IMDS otherwise, so the same code moves from a VM to an AKS pod unchanged. You can pin either mode explicitly, and for a user-assigned managed identity you pass its `client_id`. The [Entra provider page](../../identity/providers/entra.md) documents each mode, the prerequisites, and a troubleshooting table for the common failures (blocked egress to IMDS, an unset projected-token file, a mismatched audience).

You can confirm which mode you got at runtime:

```python
from promptise.identity import AgentIdentity

identity = AgentIdentity.from_entra("billing-bot", resource="api://billing")
print(identity.credential_provider)   # "entra-imds" or "entra-projected"
print(identity.is_verifiable)         # True
```

## Wire per-resource credential scoping into build_agent

Here is the part that makes this a one-parameter feature. You build a single Entra-backed identity, hand it to `build_agent()`, and Promptise mints a credential per server, each scoped to that server's audience. There are no per-server bearer tokens to distribute.

```python
import asyncio
from promptise import build_agent
from promptise.identity import AgentIdentity
from promptise.config import HTTPServerSpec


async def main():
    # One Entra identity. mode="auto" -> IMDS on a VM/Container App,
    # or the AKS projected token when $AZURE_FEDERATED_TOKEN_FILE is set.
    identity = AgentIdentity.from_entra(
        "billing-bot",
        name="Billing Bot",
        owner="payments",
        client_id="<managed-identity-client-id>",  # user-assigned MI (IMDS)
        resource="api://billing",                   # default audience
    )
    print(identity.credential_provider)             # entra-imds / entra-projected

    agent = await build_agent(
        model="openai:gpt-5-mini",
        identity=identity,
        observe=True,   # timeline tags every action with the agent's identity
        servers={
            # Each server receives a credential minted for ITS audience,
            # from the one identity. No per-server secrets.
            "billing": HTTPServerSpec(url="https://billing.internal/mcp",
                                      audience="api://billing"),
            "crm":     HTTPServerSpec(url="https://crm.internal/mcp",
                                      audience="api://crm"),
        },
        instructions="You are a billing assistant.",
    )

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "List overdue invoices for ACME."}]}
    )
    print(result["messages"][-1].content)
    await agent.shutdown()


asyncio.run(main())
```

A few things worth calling out:

- **One identity, many resources.** IMDS is an active provider, so when the `billing` and `crm` servers declare different audiences, Promptise re-mints a distinct, audience-scoped token for each. A token minted for `api://billing` cannot be replayed against the CRM server.
- **Projected mode is fixed-audience.** On AKS, the platform stamps the audience into the projected token, so all servers share that one audience. If you need per-resource scoping on AKS, you set it up on the Entra side.
- **Explicit tokens still win.** If a server has its own `bearer_token`, that overrides the identity credential for that server.
- **Fail-closed.** If Entra is briefly unreachable when a credential is acquired, Promptise logs a warning and connects unauthenticated rather than silently dropping auth — a server that requires auth then rejects the call, so you find out from the rejection, not from mysterious unattributed access.

You can also present the credential by hand to any HTTP API: `identity.auth_header("api://billing")` returns `{"Authorization": "Bearer <jwt>"}`.

## Verify the Entra MCP authentication on the server

An outbound credential only means something if the resource checks it. On the server side, the Promptise MCP Server SDK verifies the agent's Entra token against Microsoft's published JWKS keys — no shared secret, and Entra key rotation needs no reconfiguration. The [end-to-end identity guide](../../identity/guide.md) walks the full billing-bot scenario, but the core of the inbound half is small:

```python
from promptise.mcp.server import MCPServer, AuthMiddleware, JwksAuth, RequireClientId

server = MCPServer(name="billing")

# audience is required — it stops an agent replaying a token
# that Entra minted for a different resource.
auth = JwksAuth.from_discovery(
    issuer="https://login.microsoftonline.com/<tenant>/v2.0",
    audience="api://billing",
)
server.add_middleware(AuthMiddleware(auth))


@server.tool(auth=True, guards=[RequireClientId("billing-bot", "reporting-bot")])
async def issue_refund(ctx, invoice_id: str, amount: float) -> str:
    # ctx.client.subject -> the verified Entra id of the calling agent
    return f"Refunded {amount} on {invoice_id}"
```

The credential is verified cryptographically against Entra's keys, and the validated subject lands on `ctx.client`, so guards like `RequireClientId` and `HasRole` decide *which* agent may call *which* tool. If you want the mechanics of JWKS-based verification in depth — issuer discovery, key caching, audience checks — the companion post [JWT Authentication for MCP Servers: Step by Step](jwt-authentication-for-mcp-servers.md) covers the server-side auth surface end to end.

## When Entra is the right fit — and when it isn't

Entra agent identity is the right choice when your workloads already run on Azure with managed identities or AKS Workload Identity, and Entra is where you inventory and govern service principals. In that case Promptise adds almost no surface area: register once in the directory, consume the token, done.

It is *not* the right fit in a few honest cases:

- **You're not on Azure.** If your agents run on AWS (`from_aws`), GCP (`from_gcp`), a SPIFFE/SPIRE mesh (`from_spiffe`), or any OIDC issuer such as GitHub or GitLab CI (`from_oidc`), use that provider's factory instead. Same identity model, different directory.
- **You only need attribution, not authentication.** If your MCP servers are internal and don't verify callers yet, a local `AgentIdentity("billing-bot")` already tags the observability timeline — you don't need Entra to answer "which agent did this?" across a fleet.
- **You're multi-cloud and don't want to pick.** `AgentIdentity.auto()` detects the platform from environment markers and dispatches to the right factory, which is often the better default for portable deployments.

For the full decision matrix across providers and tiers, the pillar [AI Agent Identity & Authentication: The Complete Guide](ai-agent-identity.md) compares them side by side.

## Frequently asked questions

### Does Promptise create the Entra Agent ID for me?

No. Registering the identity — as a managed identity, a federated app credential, or an Entra Agent ID — is an Azure-side operation you do once. Promptise consumes the token that identity produces (via IMDS or the AKS projected file) and presents it to the resources the agent calls. The directory stays the system of record.

### How does one agent authenticate to several MCP servers with different audiences?

Give each server a distinct `audience` on its `HTTPServerSpec`. Because IMDS is an active provider, Promptise re-mints a resource-scoped credential per audience from the single identity, so a token for `api://billing` can't be replayed against `api://crm`. On AKS projected-token mode the audience is fixed by the platform, so scoping is configured on the Entra side.

### How do I revoke an agent's access?

Disable its identity in Entra. Its credentials are short-lived JWTs, so they stop validating at every resource as they expire — no change to any server's configuration. Revocation being a directory operation is the whole reason to keep Entra as the system of record.

## Next steps

Follow the Entra setup and hand your billing bot a signed, Azure-verified identity today: pin the mode to your platform, declare an `audience` per server, and verify inbound with `JwksAuth`. Start from the [Quick Start](../../getting-started/quickstart.md) to stand up an agent, then use the [Entra provider page](../../identity/providers/entra.md) to complete the Azure-side registration and connect it.
