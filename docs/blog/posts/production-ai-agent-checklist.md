---
title: "The Production AI Agent Checklist: Ship Agents Safely"
description: "Most 'production checklist' posts stop at prompt tips; this one is a battle-tested engineering checklist (auth, per-tool rate limits, circuit breakers, HMAC…"
keywords: "production AI agent checklist, production-ready AI agents, deploying LLM agents to production, AI agent reliability, MCP server production hardening, enterprise AI agent requirements"
date: 2026-07-16
slug: production-ai-agent-checklist
categories:
  - Production
---

# The Production AI Agent Checklist: Ship Agents Safely

A production AI agent checklist is what separates a demo that answers once in your terminal from a system you can put in front of real users, bursty traffic, and an auditor who wants to know who invoked the refund tool at 2 a.m. The hard part of shipping an agent was never the prompt — it's everything around the tool calls: identity, load, failure, accountability, and isolation. This post is that checklist, mapped item by item to a concrete layer you can turn on today with [Promptise Foundry](../../getting-started/quickstart.md). By the end you'll know exactly what production-ready AI agents require and how to add each layer without a rewrite.

<!-- more -->

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## Why deploying LLM agents to production is a different job

A prototype agent works because nothing is trying to break it. There are no untrusted callers, no runaway loops hammering a downstream API, no second customer whose data must stay separate from the first. Deploying LLM agents to production removes all of those assumptions at once.

An agent is only as trustworthy as the tools it can call. In Promptise those tools live behind an MCP server, so most of your hardening happens at that boundary — the place every tool call must pass through. The concerns are not exotic; they're the same ones every network service has faced for decades, now applied to actions an autonomous model takes on your behalf:

- **Identity** — who is calling, and are they allowed to call *this* tool?
- **Load** — one client, or one stuck agent loop, must not exhaust capacity for everyone.
- **Failure** — when a dependency dies, calls should fail fast, not pile up.
- **Accountability** — every sensitive action needs a record you can trust wasn't edited later.
- **Isolation** — one tenant's traffic, quotas, and data stay separate from another's.
- **Operability** — your orchestrator needs to know when the agent is alive and ready.

## The production AI agent checklist

Before you touch code, score your current agent against a concrete list. Here are the enterprise AI agent requirements Promptise is built to satisfy, and the piece that covers each one:

| Requirement | What it means | Promptise piece |
|---|---|---|
| Authentication | Verify a JWT or API key on every request | `AuthMiddleware` + `JWTAuth` / `AsymmetricJWTAuth` / `APIKeyAuth` |
| Authorization | Per-tool role and scope checks | `@server.tool(auth=True, roles=[...])`, `HasRole`, `RequireAuth` |
| Rate limiting | Per-client and per-tool token buckets | `RateLimitMiddleware`, declared `rate_limit="100/min"` |
| Failure isolation | Trip a breaker after repeated errors | `CircuitBreakerMiddleware` |
| Audit trail | Tamper-evident record of every call | `AuditMiddleware` (HMAC-chained) |
| Multi-tenancy | Tenant identity as a first-class invariant | `MCPServer(require_tenant=True)`, `tenant_claim` |
| Health probes | Liveness/readiness for Kubernetes | `HealthCheck` |
| Guardrails | Block injection, redact PII/secrets on I/O | `build_agent(guardrails=True)` |

If your prototype already covers all eight, you may not need a framework — see the honesty note below. If it covers two, that gap is exactly what the rest of this checklist closes. Every item is documented in the [production features overview](../../mcp/server/production-features.md), which is the single page to bookmark.

## Turn the checklist on: one composable middleware chain

Here's the feature that ties the list together. Instead of gluing six libraries, you compose one ordered middleware chain that runs on **every** tool call, pre-compiled at build time so adding layers costs no per-request wiring overhead. You compose it the way you'd compose ASGI or Express middleware — each layer wraps the next, in the order you add it.

The example below stands up a production-shaped MCP server for a billing agent: authenticated, tenant-scoped, rate-limited, breaker-protected, and audited. Every API here is real.

```python
# server.py — a production-shaped MCP server for an agent's tools
import os
from promptise.mcp.server import (
    MCPServer,
    AuthMiddleware, JWTAuth,
    RateLimitMiddleware,
    CircuitBreakerMiddleware,
    AuditMiddleware,
    HealthCheck,
)

# require_tenant makes tenant identity a server-wide invariant:
# a tool refuses to build unless a tenant can be resolved for the caller.
server = MCPServer("billing-tools", require_tenant=True)

# The chain runs top-to-bottom on every call.
server.add_middleware(
    AuthMiddleware(JWTAuth(secret=os.environ["JWT_SECRET"]), tenant_claim="org")
)
server.add_middleware(RateLimitMiddleware(rate_per_minute=100, per_tool=True))
server.add_middleware(CircuitBreakerMiddleware(failure_threshold=5, recovery_timeout=60.0))
server.add_middleware(AuditMiddleware(log_path="audit.jsonl"))  # HMAC-chained

# Kubernetes probes: register the dependencies that actually gate traffic.
health = HealthCheck()
health.add_check("database", db_pool.is_connected, required_for_ready=True)
health.add_check("cache", redis.ping, required_for_ready=False)


@server.tool(auth=True, roles=["billing"], rate_limit="30/min")
async def issue_refund(order_id: str, amount: float) -> dict:
    """Issue a refund for an order."""
    return {"order_id": order_id, "refunded": amount, "status": "ok"}


if __name__ == "__main__":
    server.run(transport="http", port=8080)
```

A few details make this production-grade rather than production-shaped:

- **Auth first.** `AuthMiddleware` verifies the JWT and populates the client context every layer below reads. `tenant_claim="org"` pulls the tenant from the token's `org` claim.
- **Declared limits coexist with the policy.** The chain-wide `RateLimitMiddleware` sets a floor; the `rate_limit="30/min"` on `issue_refund` enforces that specific tool's contract on top — wired in automatically, no second middleware.
- **The breaker isolates failures per tool.** After five consecutive failures, `issue_refund` fails fast for 60 seconds instead of hammering a dead dependency, then lets one probe test recovery.
- **The audit log is tamper-evident.** `AuditMiddleware` writes one HMAC-chained JSON line per call, so any edit or deletion breaks the chain. Set `PROMPTISE_AUDIT_SECRET` in production so the chain survives restarts.

You can exercise this whole pipeline in-process with `TestClient(server).call_tool(...)` — no network, no live model — which is what makes hardening testable in CI.

## AI agent reliability: failure isolation and health probes

Two checklist items are where AI agent reliability is won or lost: what happens when a dependency fails, and whether your orchestrator can tell a healthy agent from a hung one.

**Failure isolation** keeps one bad dependency from taking down the whole agent. The circuit breaker in the chain above tracks consecutive failures *per tool* and opens after a threshold, so a dead payments API doesn't turn every refund call into a 30-second timeout. Retries, timeouts, and breaker tuning are covered in depth in the [resilience patterns guide](../../mcp/server/resilience-patterns.md) — read it before you pick failure thresholds, because the right numbers depend on your traffic shape.

**Health probes** are what let an orchestrator run the agent safely. `HealthCheck` exposes liveness ("is the process up?") and readiness ("should Kubernetes send it traffic yet?"). A failing *required* check makes readiness report not-ready, so traffic is held until your database is actually reachable. The full rollout story — probes, container hardening, and a Kubernetes manifest — lives in the [deployment guide](../../mcp/server/deployment.md).

## MCP server production hardening: tenants and per-tool limits

The last two items usually become the most bespoke code, so they deserve their own note in any MCP server production hardening pass.

**Tenant isolation** is easy to get subtly wrong. If scoping is something each tool remembers to do, one forgotten filter leaks data across customers. Promptise makes it a server invariant: `MCPServer(require_tenant=True)` refuses to build if any tool could run without a resolved tenant, and the tenant then flows automatically into rate-limit keys and audit entries. The full pattern — from JWT claim to per-tenant data boundaries — is the subject of [Multi-Tenant AI Agents: Architecture for SaaS](multi-tenant-ai-agent.md).

**Rate limiting** protects both your budget and your dependencies. With `per_tool=True`, each client gets its own token bucket per tool, and buckets never span tenants. Choosing sensible per-client versus per-tool limits is its own topic, walked through in [LLM Tool Rate Limiting: Per-Client & Per-Tool Guide](llm-tool-rate-limiting.md).

## When a lighter setup is the better fit

Honesty matters more than a sales pitch, so: you do not always need all eight layers. If you're exposing a single internal tool on a trusted network — a script your own agent calls, behind a VPN, no untrusted callers, no compliance requirement — the bare `MCPServer` with `@server.tool()` and nothing else is the right amount of framework. Adding auth, breakers, and audit to a two-person internal utility is overhead you'll resent.

Other tools are a better fit in specific cases, too. If your team is already deep in another agent ecosystem and your server is a thin adapter in front of it, staying native to that stack may beat introducing a second one. And if you need a language other than Python, MCP is polyglot — a TypeScript or Go server is the correct call there. Promptise earns its place in the middle-to-large case: untrusted or multi-tenant traffic, an auditable trail, and dependencies that fail — where you'd otherwise hand-roll every checklist item. Start minimal; add layers when a real requirement appears, not before.

## Frequently asked questions

### What makes an AI agent production-ready?

A production-ready agent authenticates every caller, authorizes each tool per role or scope, rate-limits per client and per tool, isolates downstream failures with a circuit breaker, records a tamper-evident audit trail, scopes tenants as an invariant, and exposes health probes an orchestrator can read. A prototype skips all of these because it trusts its caller. Score your agent against the eight-item checklist above to find the gaps.

### Do I need a framework, or can I harden the agent myself?

For a single internal tool on a trusted network, hand-rolling is fine and a framework is overhead. Reach for one once you have untrusted callers, multiple tenants, compliance requirements, or unreliable dependencies — that's when wiring auth, rate limiting, breakers, and audit by hand stops being worth it. Promptise gives you those as a composable middleware chain you opt into one layer at a time.

### Does the middleware chain slow the agent down?

The chain is pre-compiled when the server builds, so composing ten layers doesn't add ten layers of per-request indirection. Each middleware does real work only when it must — the limiter checks a token bucket, the breaker checks a counter — and you pay only for the layers you add. Start with auth and audit, then add concurrency limits or breakers when load and failure modes justify it.

## Next steps

Start with the [Production Features overview](../../mcp/server/production-features.md) and turn on one hardening layer at a time — not a rewrite. Run through the [Quick Start](../../getting-started/quickstart.md) to get an agent and its MCP server responding, then work down this production AI agent checklist until every item has a layer behind it. `pip install promptise` and ship it safely.
