---
title: "OAuth for AI Agents: client_credentials & JWKS"
description: "Cuts through OAuth confusion for the machine-to-machine (client_credentials) case that agents actually use — no user redirect, just a signed token verified…"
keywords: "OAuth for AI agents, OAuth2 client credentials agents, JwksAuth, RS256 JWT verification, asymmetric JWT auth, verify agent tokens"
date: 2026-07-16
slug: oauth-for-ai-agents
categories:
  - Identity
---

# OAuth for AI Agents: client_credentials & JWKS

Most guides to OAuth for AI agents drown you in redirect URLs, consent screens, and PKCE — none of which your agent uses. An autonomous agent is a machine calling another machine. There is no browser, no human clicking "Allow." The grant you actually want is **client_credentials**: the agent presents a signed token it got from your identity provider, and your server verifies that token against the issuer's public keys. By the end of this post you'll know exactly which OAuth flow agents use, and how to verify agent-presented tokens in a Promptise MCP server with `JwksAuth` and `AsymmetricJWTAuth` — no shared secret in sight.

<!-- more -->

## What "OAuth for AI agents" actually means

OAuth2 has several grant types. The interactive ones (`authorization_code`, with PKCE) exist to get a human's consent through a browser. Agents don't have a browser and don't represent a logged-in person, so those flows don't fit.

The **OAuth2 client credentials** grant is the machine-to-machine case:

1. The agent authenticates to your identity provider (Entra, Okta, Auth0, Keycloak, an internal OIDC IdP) using its own service-account credential.
2. The IdP mints a short-lived JWT — an access token — scoped to a specific `audience` (the resource the agent intends to call).
3. The agent presents that token to your MCP server on every request as `Authorization: Bearer <jwt>`.
4. Your server **verifies the signature** against the IdP's published keys and reads the claims to decide what the agent may do.

No user redirect, no consent screen, no session cookie. Just a signed assertion the agent carries and your server checks. That last step — verifying agent tokens — is where Promptise fits, and it's worth being precise about scope: Promptise **consumes and verifies** tokens your IdP issues. It does not mint them, and it is not a replacement for your identity provider. Your IdP stays the source of truth; Promptise is the resource server that trusts it.

## Verify agent tokens with JwksAuth (no shared secret)

The cleanest way to verify agent tokens from a real IdP is [`JwksAuth`](../../mcp/server/auth-security.md). Instead of embedding a secret in your server, you point it at the issuer's **JWKS** endpoint — the JSON Web Key Set where the IdP publishes its public signing keys. When a token arrives, `JwksAuth` reads the token's `kid` (key ID) header, fetches the matching public key, and verifies the RS256/ES256 signature. Keys are fetched on demand and cached, so when the IdP rotates its signing keys, nothing on your side needs to change.

```python
from promptise.mcp.server import MCPServer, AuthMiddleware, JwksAuth

server = MCPServer("billing")

auth = JwksAuth(
    jwks_url="https://login.microsoftonline.com/<tenant>/discovery/v2.0/keys",
    issuer="https://login.microsoftonline.com/<tenant>/v2.0",
    audience="api://billing-mcp",   # the resource these agents target
)
server.add_middleware(AuthMiddleware(auth))
```

Two claims do the security-critical work here:

- **`audience` (required).** `JwksAuth` refuses to build without it. Verifying only the signature would accept *any* valid token from that IdP — including one an agent was legitimately issued for a different resource. Checking `aud` on every request is what stops that token-substitution replay.
- **`issuer` (strongly recommended).** When set, tokens from any other issuer are rejected, even if their signature checks out against some key you fetched.

If your IdP exposes OIDC discovery, you can skip the raw JWKS URL and let Promptise resolve it from `{issuer}/.well-known/openid-configuration`:

```python
auth = JwksAuth.from_discovery(
    issuer="https://login.microsoftonline.com/<tenant>/v2.0",
    audience="api://billing-mcp",
)
```

After verification, the validated `sub`, issuer, audience, and claims land on `ctx.client`, so per-tool guards like `RequireClientId` and `HasRole` — and your audit log — can see **which agent** made the call, not just that *some* authenticated caller did.

## RS256 JWT verification with AsymmetricJWTAuth

Sometimes you don't have a JWKS endpoint — you just hold the issuer's public key as a PEM file. Maybe an internal service signs tokens with a static key pair, or you run your own tiny token issuer. For that, [`AsymmetricJWTAuth`](../../mcp/server/auth-security.md) does **RS256 JWT verification** (or ES256 for ECDSA) against a public key you provide directly:

```python
from promptise.mcp.server import AsymmetricJWTAuth, AuthMiddleware

auth = AsymmetricJWTAuth(
    public_key=open("/etc/keys/issuer-public.pem").read(),
    algorithm="RS256",   # or "ES256" for ECDSA
)
server.add_middleware(AuthMiddleware(auth))
```

The distinction between symmetric and asymmetric JWT auth matters for who can mint tokens. With HS256 (`JWTAuth`), the same shared secret both signs and verifies — every service that can *check* a token can also *forge* one. With **asymmetric JWT auth**, the IdP holds the private signing key and your server only ever holds the public key. Your resource server can verify agents all day and still be unable to issue a valid token itself. That's the property you want when many services need to accept the same tokens.

Both `JwksAuth` and `AsymmetricJWTAuth` reject a token that claims a symmetric algorithm like `HS256`, which closes the classic algorithm-confusion attack. Both require the `PyJWT` and `cryptography` packages (optional dependencies): `pip install PyJWT cryptography`.

## A runnable example: verify agent tokens end to end

Here's a complete, self-contained script. It generates an RSA key pair, signs a token the way an IdP would, stands up a Promptise MCP server that verifies it with `AsymmetricJWTAuth`, and runs the whole pipeline in-process with `TestClient` — no network, no live IdP. This mirrors exactly what happens in production; only the token source differs.

```python
import asyncio
import jwt  # PyJWT
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.hazmat.primitives import serialization
from promptise.mcp.server import MCPServer, AuthMiddleware, AsymmetricJWTAuth, TestClient


def _keypair() -> tuple[str, str]:
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_pem = key.private_bytes(
        serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8,
        serialization.NoEncryption(),
    ).decode()
    public_pem = key.public_key().public_bytes(
        serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo,
    ).decode()
    return private_pem, public_pem


async def main() -> None:
    private_pem, public_pem = _keypair()

    server = MCPServer("billing")
    server.add_middleware(AuthMiddleware(AsymmetricJWTAuth(public_key=public_pem, algorithm="RS256")))

    @server.tool(auth=True)
    async def issue_refund(order_id: str, amount: float) -> str:
        """Refund an order (requires a verified agent token)."""
        return f"Refunded ${amount:.2f} for {order_id}"

    # The IdP would mint this; here we sign it ourselves with the private key.
    token = jwt.encode({"sub": "billing-bot", "client_id": "billing-bot"}, private_pem, algorithm="RS256")

    client = TestClient(server, meta={"authorization": f"Bearer {token}"})
    result = await client.call_tool("issue_refund", {"order_id": "A-1001", "amount": 12.50})
    print(result[0].text)  # -> Refunded $12.50 for A-1001


asyncio.run(main())
```

Swap the hand-signed token for a real one from your IdP and change `AsymmetricJWTAuth` to `JwksAuth(jwks_url=..., audience=...)` — the server code and the tool don't change. That's the point: your tools stay auth-agnostic while the verification layer plugs into whatever issuer you already run.

## The other side: agents that present verifiable identity

Verification only means something if the agent presents a real, IdP-backed credential in the first place. Promptise agents can do that natively through [Agent Identity](../../identity/overview.md): `build_agent(..., identity=AgentIdentity.auto())` gives an agent a non-human, service-account-style identity from Entra, AWS, GCP, SPIFFE, or an OIDC source, and automatically attaches the right bearer token — minted per `audience` — to every MCP server it calls. The [Agent Identity guide](../../identity/guide.md) walks through wiring a specific provider and shows the inbound-verification counterpart with `JwksAuth`, so the outbound and inbound halves line up. For the identity concepts underneath all of this, the [AI Agent Identity & Authentication: The Complete Guide](ai-agent-identity.md) is the pillar to start from; if you're weighing HS256 shared secrets against the asymmetric route, [JWT Authentication for MCP Servers: Step by Step](jwt-authentication-for-mcp-servers.md) covers the symmetric case in depth.

## When a shared secret is the better fit

`JwksAuth` and `AsymmetricJWTAuth` are the right default when tokens come from an external or shared IdP, when many services must accept the same tokens, or when you want key rotation to be a non-event. But they aren't always the simplest choice:

- **You control both ends and there's no real IdP.** For a single service issuing tokens to a single trusted client in a closed system, HS256 `JWTAuth` with one shared secret is less machinery. You lose the sign/verify separation, so weigh that against the simplicity.
- **You aren't using JWTs at all.** Internal scripts and CI jobs are often fine with `APIKeyAuth`. It won't carry rich claims or expiry the way a JWT does, but it's honest about what it is.

Asymmetric verification earns its keep precisely when the *forge-proof* property and rotation-without-redeploy matter. If neither applies to your setup, don't add the ceremony.

## Frequently asked questions

### Which OAuth grant type do AI agents use?

The **client credentials** grant. It's the machine-to-machine flow with no user, no browser redirect, and no consent screen. The agent authenticates to your IdP with its own service credential, receives a short-lived JWT scoped to a target `audience`, and presents it as a bearer token. Interactive grants like `authorization_code` are for human logins and don't apply to autonomous agents.

### Does Promptise replace my identity provider?

No. Promptise **consumes and verifies** tokens your existing IdP issues — Entra, Okta, Auth0, Keycloak, or any OIDC provider. It does not mint tokens or manage users. `JwksAuth` verifies agent-presented tokens against your issuer's published keys; your IdP remains the source of truth for identity.

### Why is `audience` required on JwksAuth?

Because verifying only the signature would accept any valid token from that issuer, including one minted for a *different* resource. Checking the `aud` claim on every request ties the token to *this* server and prevents an agent from replaying a token it was issued for something else (token substitution). `JwksAuth` refuses to build without it, by design.

## Next steps

Point `JwksAuth` at your issuer and verify agent-presented tokens without ever holding a shared secret — your tools stay auth-agnostic while your IdP stays in charge of identity. Start with the [Quick Start](../../getting-started/quickstart.md) to get an agent and server running, then follow the [auth & security guide](../../mcp/server/auth-security.md) to wire verification into your MCP server.
