---
title: "How do you revoke an AI agent's access instantly?"
description: "A leaked static per-agent key is revoked only by rotating it and redeploying every service that trusts it — slow, error-prone, and you may not even know…"
keywords: "revoke an AI agent's access, kill switch for AI agents, rotate agent credentials, disable a compromised agent, short-lived agent credential revocation, directory revocation for agents"
date: 2026-07-16
slug: revoke-an-ai-agent-s-access
categories:
  - Identity
---

# How do you revoke an AI agent's access instantly?

To **revoke an AI agent's access** the moment it misbehaves, you need a single place to flip — not a scavenger hunt for a leaked string across every service that trusts it. That is exactly what a static per-agent API key can't give you: revoking it means minting a new one, editing every deployment that holds the old value, and redeploying without a gap — all while you may not even be sure *which* agent leaked it. Back the agent with an identity from your IdP instead, and revocation collapses into one directory operation: disable the identity, and its short-lived, audience-scoped credentials stop validating everywhere, with no server reconfiguration. This post shows the revoke path, why short-lived tokens keep the blast radius small, and how Promptise zero-fills any credential a running agent still holds in memory.

## Rotating a static key means redeploying everything that trusts it

Here is the kill switch most teams actually have. Each agent carries a static bearer token or API key, pasted into its tool config and copied into CI, a `.env`, maybe a screenshot. When one leaks — or one agent goes rogue — "revoking" it means all of this, in order:

- **Mint a replacement.** Generate a new key so the legitimate agents keep working.
- **Find every trusting service.** The old key is validated by every server, gateway, and sidecar you handed it to. Miss one and the "revoked" key still opens a door.
- **Edit and redeploy each one.** Roll the new value into every config and restart, ideally with zero downtime for the agents that still need access.
- **Guess who leaked it.** If the key was shared across a fleet, the audit log only ever saw "the key" act — so you can't even scope the damage to one agent.

None of that is *instant*. It's a change-management project, run under incident pressure, with a long tail where the compromised key still works. And it's the same anti-pattern humans abandoned a decade ago when they stopped carrying passwords into every system and moved to directory-issued logins. Agents are simply the newest **non-human actor** that deserves the same treatment — the [Agent Identity overview](../../identity/overview.md) frames them exactly that way. The prerequisite for a real kill switch is a real identity, which is why [giving each AI agent its own identity, not a shared key](give-each-ai-agent-its-own-identity.md) comes first.

## Back the agent with an IdP identity — then revoke in one place

Promptise models the agent as a non-human actor with an **`AgentIdentity`**. Attach it at `build_agent()`; when the agent calls protected resources, make it **verifiable** by backing it with your IdP — Microsoft Entra, AWS IAM, Google Cloud, SPIFFE/SPIRE, or a generic OIDC issuer. Now the credential the agent presents is a short-lived, signed JWT minted from the workload's own identity, not a string you stored:

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

Because the directory is now the system of record — Promptise keeps **no identity store of its own** — revocation is a property of the directory, not of your deployment topology. To cut `billing-bot` off, you disable the agent's identity in the IdP. That single action does two things at once:

1. **No new credentials can be minted.** The provider will not issue another JWT for a disabled identity, so the agent cannot re-authenticate anywhere.
2. **Existing credentials expire out from under it.** The tokens already in flight are short-lived; as each reaches its `exp`, every resource that verifies it rejects it — no redeploy, no config edit, no per-server change.

The whole revoke path is one operation in a directory you already govern. There is no fleet-wide key to rotate, and no service to reconfigure — server-side verification is stateless (see the next section), so the servers don't even need to know a revocation happened. The [end-to-end identity guide](../../identity/guide.md) wires this lifecycle from mint through revoke, and the credential side of the story — why a workload-identity JWT beats a static key in the first place — is in [How does an AI agent authenticate to an API? (not API keys)](how-does-an-ai-agent-authenticate-to-an-api.md).

## Zero-fill the credentials a running agent is holding

Disabling the identity handles *future* access and stops issued tokens as they expire. But a long-running agent process may already be holding downstream secrets in memory right now. For those, Promptise gives the runtime a second, immediate lever: a per-process `SecretScope` that keeps values encrypted in memory only — never serialized to a journal, checkpoint, or status output — and can **zero-fill and drop every one on command**.

This block is fully runnable with no infrastructure at all:

```python
import asyncio

from promptise.runtime.secrets import SecretScope
from promptise.runtime.config import SecretScopeConfig


async def main() -> None:
    scope = SecretScope(
        config=SecretScopeConfig(
            secrets={"downstream_api_key": "sk-live-do-not-leak"},
            default_ttl=3600,
        ),
        process_id="billing-bot",
    )
    await scope.resolve_initial()

    print("before:", scope.get("downstream_api_key"))  # sk-live-do-not-leak
    print("active:", scope.active_secret_names)          # ['downstream_api_key']

    # The kill switch for runtime-held credentials.
    await scope.revoke_all()

    print("after :", scope.get("downstream_api_key"))  # None
    print("active:", scope.active_secret_names)          # []


asyncio.run(main())
```

`revoke_all()` overwrites each stored value with null bytes, clears the store, rotates the in-memory Fernet key so the old encryption key is discarded too, and forces a garbage-collection pass to clear copies sooner. Every access is journalled by name (never by value), and the revocation itself is recorded — so you have a trail of exactly when the credential was cut.

You rarely call this by hand. Secret scoping is declared on the agent's manifest with `revoke_on_stop=True` (the default), so the moment a governed process is stopped, its runtime-held secrets are zero-filled automatically. Stopping the compromised agent and disabling its IdP identity together give you both halves of the kill switch: the directory stops the agent from authenticating anywhere new, and the runtime destroys what it was already carrying.

## Short-lived, audience-scoped tokens keep the blast radius small

Revocation is only half the safety story; the other half is how *much* an unrevoked-for-a-few-minutes credential can do. Two properties bound it.

**Short lifetime bounds the time window.** The credential is a short-lived JWT, and Promptise reads its `exp` and re-acquires per audience as it nears expiry — you never hand-rotate. So the blast radius of a leaked token is bounded by its TTL, not by how fast your team can redeploy N services under pressure. Disabling the identity means the *next* acquisition simply never succeeds.

**Audience scoping bounds the reach.** You declare the audience each API expects on its `HTTPServerSpec` (`audience="api://billing"`, `audience="api://crm"` above), and the one identity mints a resource-scoped credential per audience. A token minted for billing carries `aud=api://billing` and is worthless at the CRM server, because that server verifies the audience and rejects anything else. You can present the same scoping by hand anywhere:

```python
identity.auth_header("api://billing")  # {"Authorization": "Bearer <jwt aud=api://billing>"}
identity.auth_header("api://crm")      # {"Authorization": "Bearer <jwt aud=api://crm>"}
```

Server-side, the resource verifies the token against the IdP's published keys — no shared secret, and IdP key rotation needs no reconfiguration. `audience` is required precisely so one agent can't replay a token minted for a different resource:

```python
from promptise.mcp.server import (
    MCPServer, AuthMiddleware, JwksAuth, RequireClientId, AuditMiddleware,
)

server = MCPServer(name="billing")

# Verify tokens this IdP issued for THIS resource. `audience` is mandatory.
server.add_middleware(AuthMiddleware(JwksAuth.from_discovery(
    issuer="https://login.microsoftonline.com/<tenant>/v2.0",
    audience="api://billing",
)))

# Each entry records the VERIFIED acting agent inside a tamper-evident HMAC chain.
server.add_middleware(AuditMiddleware(log_path="billing-audit.jsonl", signed=True))


@server.tool(auth=True, guards=[RequireClientId("billing-bot")])
async def issue_refund(ctx, invoice_id: str, amount: float) -> str:
    # ctx.client.subject -> the verified identity of the calling agent
    return f"Refunded {amount} on {invoice_id}"
```

Because verification is stateless JWKS validation, revocation propagates without any server ever being told: a disabled identity's tokens simply stop being minted and the ones in flight expire. And because the audit log records the *verified* subject inside an HMAC chain, "which agent leaked it?" is answerable after the fact — not a guess. The full threat model, including exactly what a revoked credential does and does not stop, is documented in the [Identity security guide](../../identity/security.md).

## What other frameworks do today

Be fair here: every mainstream framework can *authenticate* an agent to a resource. What differs is whether there is a single-place kill switch behind that authentication, or a static secret you own and must rotate.

- **LangChain / LangGraph** connect to MCP servers through `langchain-mcp-adapters`' `MultiServerMCPClient`, where each server's config accepts a `headers` dict — including `Authorization`. That's a *slot* for a static bearer you supply. Revoking it means rotating the secret and updating every place that config lives, then redeploying. There is no directory-level disable that stops the token everywhere.
- **CrewAI** and **AutoGen** authenticate tools with the API keys or tokens you configure, typically from environment variables. Revocation is the same rotate-and-redeploy across every deployment holding the value; neither's open-source core mints a credential from the agent's own workload identity that you could disable centrally.
- **LlamaIndex** tool specs take an API key or token you pass in at construction — again a static secret whose revocation is a rotation.
- The **MCP specification itself** defines an OAuth 2.1 authorization framework for HTTP transports (2025 spec). This is the honest exception: a real authorization server *can* support token revocation (RFC 7009) and issues short-lived access tokens that expire on their own. But the spec standardizes the token *handoff* — it doesn't source the credential from the agent's managed identity, IRSA role, or SPIFFE SVID, and it leaves you to stand up and operate that authorization server yourself.

So the honest delta isn't "nobody can revoke." Where the credential is a static secret (the common default), there is genuinely no single-place kill switch — you rotate and redeploy. Where an OAuth authorization server is in play, revocation is achievable, but it's infrastructure you run. Promptise makes the kill switch **structural** by consuming the IdP you already govern as the system of record: disabling one identity there stops future credentials and expires the ones in flight, with no server reconfiguration — and `revoke_on_stop` zero-fills whatever a running process still holds, which a config-file secret has no equivalent of.

## Frequently asked questions

### How do I revoke an AI agent's access instantly?

Disable the agent's identity in your IdP. Because Promptise sources the agent's credential from that directory rather than a stored key, no new token can be minted for a disabled identity, and the short-lived tokens already in flight stop validating at every resource as they expire. If a running process is holding downstream secrets, stop it (or call `revoke_all()`) to zero-fill them immediately. No server needs reconfiguration.

### Is revocation truly instant, or bounded by the token lifetime?

Two clocks. New authentication stops the instant you disable the identity — the provider won't mint another JWT. Tokens already issued remain valid until their `exp`, so the residual exposure is bounded by the credential's (short) TTL, not by how fast you can redeploy services. Runtime-held secrets are the exception: `revoke_all()` destroys them in memory immediately.

### Why is this better than rotating a static per-agent key?

Rotating a key means minting a replacement, editing every service that trusts the old value, and redeploying — under incident pressure, with a long tail where the old key still works. Directory revocation is one operation, and server-side verification is stateless JWKS validation, so the resources don't even need to be told. You also aren't left guessing which agent leaked a shared key, because each identity acts as its own verified subject.

### What does `revoke_on_stop` actually do to the secrets?

It overwrites each stored value with null bytes, clears the in-memory store, rotates the Fernet key so the old encryption key is discarded, and forces a garbage-collection pass. Values live only in memory and are never serialized to a journal, checkpoint, or status output, and the revocation event is recorded in the journal by name. It is best-effort against Python's immutable-string memory pool, but it removes the credential from the live process at once.

### Do I need cloud infrastructure to try this?

No for the runtime `SecretScope` example — it runs on a laptop with zero infrastructure. The directory-revocation path needs a verifiable identity backed by an IdP (Entra, AWS, GCP, SPIFFE, or OIDC), because that directory is what you disable to cut the agent off everywhere.

## Next steps

Design a real kill switch for your agents instead of a rotate-and-redeploy scramble. Start with the [Identity security guide](../../identity/security.md) to see exactly what revocation guarantees and where its boundaries are, then follow the [end-to-end identity guide](../../identity/guide.md) to wire one agent through two audiences with a full revoke path, and read the [Agent Identity overview](../../identity/overview.md) to see how attribution, audience-scoped credentials, and directory revocation fit together. New here? Make your agent's identity verifiable, set `revoke_on_stop=True` on any runtime secrets, and confirm you can cut it off from one place — the directory you already trust.
