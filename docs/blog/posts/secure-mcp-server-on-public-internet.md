---
title: "How to Secure an MCP Server on the Public Internet"
description: "Putting an MCP server on the open internet turns every tool into an attack surface. FastMCP gives you auth, but hardening for public exposure is more than a…"
keywords: "secure mcp server on public internet, mcp server security, public mcp server auth, jwt mcp server, mcp audit logging, harden mcp endpoint"
date: 2026-07-16
slug: secure-mcp-server-on-public-internet
categories:
  - MCP
---

# How to Secure an MCP Server on the Public Internet

To **secure an MCP server on the public internet** you have to accept one uncomfortable fact first: the moment your server has a public URL, every tool you registered becomes a callable endpoint for anyone who finds it — not just your agent, but any script, scanner, or rogue MCP client on Earth. A bearer token is table stakes, and it is not hardening. Hardening is what happens *after* auth: capability checks per tool, limits that survive a burst, breakers that stop a cascade, a trail you can prove wasn't edited, and a human in the loop on the calls that move money. This post wires all of that into one middleware chain you can copy, run offline, and ship.

## Every public tool is now an attack surface

On your laptop, an MCP server over stdio is only reachable by the process that launched it. Expose the same server over HTTP on a public host and the threat model inverts. The tools you wrote for "our agent" are now reachable by:

- A scanner that found your open port and is enumerating tool names.
- A second agent — yours or someone else's — that discovered the server and is calling tools you never meant it to reach.
- A legitimate client whose credentials leaked, now replaying calls at machine speed.
- An insider who can read your logs and wants to make one action look like another.

None of these are exotic. They are the default population of the public internet. And the tools most worth exposing — refunds, deletions, provisioning, anything that writes — are exactly the ones an attacker most wants to reach. Public **mcp server security** is therefore not one control but a *stack* of them, each catching what the one above it lets through. The rest of this post builds that stack as a single, ordered middleware chain.

One more thing the open internet changes: your server now has clients you don't control, so a careless tool-schema change can break them silently. That's a compatibility hazard, not a security one, but it lives in the same "you don't own the callers" reality — [Why a Small MCP Tool Change Broke Every Connected Agent](version-mcp-tools-without-breaking-clients.md) covers versioning tools without breaking the fleet.

## The hardened middleware chain, in one file

Here is the whole thing. It stands up a `refund` tool behind six layers — HMAC-chained audit, a circuit breaker, JWT auth, a per-tool role guard, a per-tool token-bucket rate limit, and a server-side approval gate — then probes each layer. It runs with nothing but `pip install promptise`: no API key, no network, no Docker. `TestClient` drives the full pipeline in-process.

```python
import asyncio

from promptise.mcp.server import (
    AuditMiddleware,
    AuthMiddleware,
    CircuitBreakerMiddleware,
    JWTAuth,
    MCPServer,
    RateLimitMiddleware,
    TokenBucketLimiter,
    ApprovalGateMiddleware,
    TestClient,
)

auth = JWTAuth(secret="dev-secret")
server = MCPServer(name="public-billing")

# Outermost: tamper-evident audit records EVERY attempt, even rejected ones.
audit = AuditMiddleware(log_path="billing-audit.jsonl", signed=True,
                        hmac_secret="rotate-me-in-prod")
server.add_middleware(audit)
# Fail fast if a downstream dependency starts erroring.
server.add_middleware(CircuitBreakerMiddleware(failure_threshold=5))
# Identity: reject anyone without a valid token before we do real work.
server.add_middleware(AuthMiddleware(auth))
# Per-tool token buckets: a burst from one caller can't drown the tool.
server.add_middleware(
    RateLimitMiddleware(TokenBucketLimiter(rate_per_minute=3, burst=3), per_tool=True)
)
# Server-side human approval — enforced for ANY client, not just polite ones.
server.add_middleware(ApprovalGateMiddleware(lambda req: req.arguments["amount"] < 1000))


@server.tool(auth=True, roles=["clerk"], requires_approval=True)
async def refund(order_id: str, amount: float) -> dict:
    """Refund an order. Guarded, rate-limited, approved, audited."""
    return {"order_id": order_id, "amount": amount, "status": "refunded"}


def outcome(result) -> str:
    """Collapse a tool result to the layer that decided it."""
    text = result[0].text
    for code in ("AUTHENTICATION_ERROR", "ACCESS_DENIED",
                 "APPROVAL_DENIED", "RATE_LIMIT_EXCEEDED"):
        if code in text:
            return code
    return "OK (refunded)"


async def main():
    client = TestClient(server)
    clerk = auth.create_token({"sub": "clerk-1", "roles": ["clerk"]})
    stranger = auth.create_token({"sub": "intruder", "roles": []})
    hdr = lambda tok: {"authorization": f"Bearer {tok}"}

    # 1. No token at all -> rejected at the auth layer.
    anon = await client.call_tool("refund", {"order_id": "A-1", "amount": 10.0})
    print("anon      :", outcome(anon))

    # 2. Authenticated but wrong role -> guard denies; no reviewer is paged.
    norole = await client.call_tool(
        "refund", {"order_id": "A-1", "amount": 10.0}, headers=hdr(stranger))
    print("no-role   :", outcome(norole))

    # 3. A high-value refund the approval policy rejects -> APPROVAL_DENIED.
    big = await client.call_tool(
        "refund", {"order_id": "A-2", "amount": 5000.0}, headers=hdr(clerk))
    print("over-limit:", outcome(big))

    # 4. A clean, approved call runs the body.
    ok = await client.call_tool(
        "refund", {"order_id": "A-3", "amount": 10.0}, headers=hdr(clerk))
    print("approved  :", outcome(ok))

    # 5. Burst past the token bucket -> RATE_LIMIT_EXCEEDED.
    for i in range(4):
        r = await client.call_tool(
            "refund", {"order_id": f"B-{i}", "amount": 5.0}, headers=hdr(clerk))
        print(f"burst {i}   :", outcome(r))

    # Every attempt above is one signed line; the chain proves none were edited.
    print("audit chain valid:", audit.verify_chain())


asyncio.run(main())
```

Run it and you get the same output every time:

```text
anon      : AUTHENTICATION_ERROR
no-role   : ACCESS_DENIED
over-limit: APPROVAL_DENIED
approved  : OK (refunded)
burst 0   : OK (refunded)
burst 1   : RATE_LIMIT_EXCEEDED
burst 2   : RATE_LIMIT_EXCEEDED
burst 3   : RATE_LIMIT_EXCEEDED
audit chain valid: True
```

Four different attackers, four different rejections, one honest success — and a cryptographic proof that the record of all of it is intact. That is what "hardened" means in practice, and it is the feature this post is really about: **layered access control as a single, ordered chain** rather than a pile of ad-hoc checks.

## What each layer actually blocks

The order matters, and Promptise composes middleware outermost-to-innermost in the order you add it. Read the chain from the top:

- **Audit (outermost).** `AuditMiddleware(signed=True)` writes one HMAC-chained JSONL line per attempt — including the rejected ones. Each entry hashes the previous entry, so editing, deleting, or reordering any line breaks the chain and `verify_chain()` returns `False`. This is what makes **mcp audit logging** evidence rather than a diary: an insider can't quietly rewrite `refund` to `get_status` after the fact. It sits outermost so it captures calls that later fail auth or a guard, giving you a complete record of *who tried what*.
- **Circuit breaker.** `CircuitBreakerMiddleware` trips after N consecutive failures and rejects further calls immediately with `CircuitOpenError` for a recovery window, instead of letting a dead downstream dependency pile up blocked connections on a public server. Full state machine and per-tool exclusions are in the [Resilience Patterns guide](../../mcp/server/resilience-patterns.md).
- **Auth.** `AuthMiddleware(JWTAuth(...))` verifies the HS256 token and populates a typed `ClientContext` — identity, roles, scopes, JWT claims. This is your **jwt mcp server** foundation. For production you'd swap `JWTAuth` for `AsymmetricJWTAuth` (RS256/ES256) or `JwksAuth` (verifies against your IdP's rotating JWKS keys), all documented in [Authentication & Security](../../mcp/server/auth-security.md). Note the token is verified before any expensive work happens — an anonymous caller never reaches the tool body.
- **Per-tool guards.** `roles=["clerk"]` is shorthand for a `HasRole` guard. Guards run after auth (they read `ctx.client`), so **public mcp server auth** becomes capability-based: authentication proves *who you are*, the guard decides *what you may call*. A valid token with the wrong role is denied with a descriptive `ACCESS_DENIED` — and critically, that denial happens *before* the approval gate, so an unauthorized caller can never spam your human reviewers.
- **Rate limit.** `RateLimitMiddleware(TokenBucketLimiter(...), per_tool=True)` gives each tool its own bucket, so a burst against `refund` can't starve every other tool. Buckets are keyed per client (and, on a multi-tenant deployment, tenant-qualified), and you can declare a limit inline with `@server.tool(rate_limit="100/min")`. Details in [Authentication & Security](../../mcp/server/auth-security.md)'s companion caching page.
- **Approval gate (innermost, before the handler).** `requires_approval=True` plus `ApprovalGateMiddleware` means the body cannot run until a policy — or a human — approves the *specific* call. It fails closed: no decision within the timeout is a denial, and a tool that declares `requires_approval=True` with no gate installed **refuses to build at all**. The [Approval Gates guide](../../mcp/server/approval-gates.md) covers the independent four-eyes `PendingApprover`, MCP-elicitation, and webhook approvers.

The point of the ordering is defense in depth: each layer assumes the ones above it can be bypassed and does its job anyway. That's how you **harden an MCP endpoint** rather than just gate it.

## What other frameworks do today

To be fair about the delta, "secure the MCP server" is not a Promptise-only idea, and the honest comparison is about *how much is first-class* versus *how much you assemble yourself*.

- **General agent frameworks — LangChain, CrewAI, AutoGen, LlamaIndex.** These orchestrate tool *calls*; they don't ship an MCP server you expose and secure. When you put their tools on a public port, you wrap them in your own FastAPI/Flask app and add auth, rate limiting, and audit by hand. That's the "entirely DIY" reality — not a knock on those frameworks, just a different layer of the stack.
- **FastMCP.** This is the fair, specific comparison, because FastMCP genuinely ships server-side security. It has real authentication — bearer tokens, JWT verification, and OAuth 2.1 / OIDC provider integrations (WorkOS, Auth0, GitHub, Google, Azure). It also ships a real **token-bucket `RateLimitingMiddleware`** and a `SlidingWindowRateLimitingMiddleware` in `fastmcp.server.middleware.rate_limiting`, with an optional custom client-ID extractor for per-client limiting. So the claim is *not* "FastMCP lacks rate limiting" — it clearly has it.

  The precise delta is what's *integrated as one hardening chain*. FastMCP's built-in rate limiter is global or per-client via that extractor, not a per-tool declared attribute (`@server.tool(rate_limit=...)`) and not tenant-qualified out of the box. And beyond auth and rate limiting, the remaining public-exposure controls are yours to source: FastMCP does not ship, in the same chain, a per-tool **circuit breaker**, an **HMAC-chained tamper-evident audit** middleware with a `verify_chain()` proof, or a **server-side approval gate** that refuses to build when a tool declares approval but none is installed. You can build each — the middleware hooks are there — but "build each" is exactly the assembly work a public deployment can't afford to get subtly wrong.

Promptise's edge is structural, not a feature checkbox: capability guards, per-tool/tenant-qualified limits, per-tool breaking, tamper-evident audit, and build-time-enforced approval are all first-class declarations on one pre-compiled chain, so hardening for public exposure is configuration, not a bespoke security project. For a fuller side-by-side of the two production stacks, see [FastMCP vs Promptise: The Production MCP Stack Compared](fastmcp-alternative-for-production.md).

## A go-live checklist before you expose the port

Before you point DNS at the box, work through the auth and security checklist:

1. **Auth on every non-public tool.** Set `require_auth=True` on the server, or `auth=True` per tool. Prefer `AsymmetricJWTAuth`/`JwksAuth` over a shared HS256 secret in production, and never hardcode the secret — load it from your secrets manager.
2. **A capability guard on every write tool.** `roles=[...]`, `HasScope`, `RequireClientId` — authentication is not authorization. Deny with intent.
3. **A per-tool rate limit on anything expensive or destructive**, tenant-qualified if you're multi-tenant, so one caller's burst can't degrade everyone.
4. **A circuit breaker in front of external dependencies** so an upstream outage doesn't cascade into connection exhaustion on a public server.
5. **HMAC-chained audit turned on**, with `PROMPTISE_AUDIT_SECRET` from your secrets manager so the chain verifies across restarts and every instance.
6. **An approval gate on the calls that move money or delete data.** Declare `requires_approval=True` and let the build fail loudly if you forget to install a gate.
7. **TLS at the edge and CORS locked down** at the transport layer, plus a plan for rotating tokens and audit secrets.

Each item maps to one `add_middleware` call or one decorator argument — which is the whole reason to treat hardening as a chain instead of a checklist you re-implement per project.

## Frequently asked questions

### Is JWT auth enough to secure a public MCP server?

No. A verified JWT proves *who* is calling; it says nothing about *what* they may do, how often, or whether a human should review it. On a public **jwt mcp server** you still need per-tool capability guards (authorization), rate limits (abuse control), a circuit breaker (blast-radius control), tamper-evident audit (accountability), and approval gates on high-stakes tools. Auth is the first layer, not the whole stack.

### Does the audit log prove nobody edited it?

Yes, that's the point of `AuditMiddleware(signed=True)`. Each entry stores an HMAC computed over its contents plus the previous entry's hash, forming a chain. `verify_chain()` returns `False` the instant any line is edited, deleted, or reordered. Set `PROMPTISE_AUDIT_SECRET` from your secrets manager so the same key verifies logs from every instance and across restarts — a random per-process secret can't. This is what turns **mcp audit logging** into evidence you can hand an auditor.

### Won't the approval gate let an attacker spam my reviewers?

No — guards run before the gate. An unauthenticated or wrong-role caller is rejected with `AUTHENTICATION_ERROR` or `ACCESS_DENIED` and never generates an approval request, so the bounded pending queue can't be flooded by callers who'd fail authorization anyway. Pair `requires_approval=True` with `auth=True` and a role guard so there's always a verified identity to check first.

### How is this different from FastMCP, which also has auth and rate limiting?

FastMCP ships real auth and a real token-bucket/sliding-window rate limiter, so the difference isn't "it can't do this." It's integration: Promptise makes per-tool capability guards, tenant-qualified per-tool limits, per-tool circuit breaking, HMAC-chained audit, and build-time-enforced approval gates first-class declarations on one pre-compiled chain, where FastMCP leaves the layers past auth and rate limiting for you to assemble.

### Can I test all of this without deploying?

Yes. Every snippet here runs in-process with `TestClient`, which executes the full pipeline — validation, DI, guards, middleware, handler — with no network and no LLM key. You can prove your hardening works in CI before anything touches a public port.

## Next steps

Harden your server before you expose it. Copy the chain above, run it locally, and confirm every layer rejects what it should — then read [Authentication & Security](../../mcp/server/auth-security.md) to move from a shared HS256 secret to `AsymmetricJWTAuth` or `JwksAuth` against your IdP. Add resilience with the [Resilience Patterns guide](../../mcp/server/resilience-patterns.md), gate your money-moving tools with the [Approval Gates guide](../../mcp/server/approval-gates.md), and when you're weighing the whole production stack, compare notes in [FastMCP vs Promptise: The Production MCP Stack Compared](fastmcp-alternative-for-production.md). Ship the auth, then ship the hardening — the public internet will test both.
