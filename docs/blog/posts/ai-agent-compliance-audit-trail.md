---
title: "One Audit Trail for SOC 2, HIPAA and the EU AI Act"
description: "The cluster hub. One HMAC-chained AuditMiddleware trail produces evidence for three different regimes: SOC 2 (integrity, attribution, completeness), HIPAA…"
keywords: "ai agent compliance audit trail, soc 2 hipaa eu ai act agent logging, compliance evidence for llm agents, one audit log multiple regulations, tamper-evident audit for compliance"
date: 2026-07-16
slug: ai-agent-compliance-audit-trail
categories:
  - Compliance & Audit
---

# One Audit Trail for SOC 2, HIPAA and the EU AI Act

An ai agent compliance audit trail is the one artifact three very different regulators end up asking for in the same shape: show us who did what, prove the record wasn't edited, and prove nothing went missing. A SOC 2 reviewer, a HIPAA auditor, and an EU AI Act conformity assessor read different rulebooks, but when your autonomous agents call tools that move money, touch patient data, or take consequential actions, all three converge on the same primitive — a record of every action, attributed to a verified principal, that survives an adversary with write access. This post shows how one HMAC-chained `AuditMiddleware` trail in Promptise Foundry produces evidence for all three, maps each regime's ask to concrete fields, and is honest about where the log ends and a governance program begins.

<!-- more -->

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## One behavior, three regimes

The instinct is to build three logging pipelines: a SOC 2 one, a HIPAA one, an EU AI Act one. That's wasted effort, because the three regimes are not asking for different *data* — they're asking for different *properties* of the same data.

- **SOC 2** cares about the integrity of your monitoring records (were they altered?), attribution (whose action was it?), and completeness (were records silently dropped?).
- **HIPAA** §164.312(b) requires "audit controls" — mechanisms that record and examine activity in systems that use electronic PHI — and §164.312(c) requires you to protect that record from improper alteration.
- **The EU AI Act**, Article 12, requires high-risk systems to *automatically* record events (logs) over the system's lifetime, with enough traceability to reconstruct what happened.

Read those side by side and the overlap is nearly total: a record of every consequential action, tied to the identity that took it, that you can prove wasn't tampered with. One trail with the right properties is evidence for all three. Three trails without those properties is evidence for none.

## What other frameworks do today

Being fair here matters, because the popular agent and observability stacks *do* produce a record of what an agent did — just not one built to be evidence. It's worth naming their actual behavior precisely.

- **LangSmith and Pydantic Logfire** persist rich traces and spans for every run, and they're excellent at it — this is how most teams debug and replay agent behavior. The properties to be precise about: the trace store is a *mutable database*, and the attributes on a span (including any user or agent id) are written by the emitting process, so the "who" is self-reported rather than checked against an identity provider. That's the right design for observability; it just isn't tamper-evidence.
- **LangGraph checkpointers** persist graph state at each step so a run can resume, time-travel, or hand off to a human. That's genuinely valuable durable execution — but a checkpoint is *resumable state*, not a cryptographically chained record whose edits and deletions are detectable, and the checkpoint backend (SQLite, Postgres, Redis) is mutable by anyone with access. Resume-after-crash and tamper-evidence are different guarantees.
- **OpenTelemetry / OpenLLMetry GenAI** instrumentation is the same shape one layer down: span attributes set at emit time, stored in a queryable backend. Superb for latency and cost; not a per-record signed chain.

None of this is wrong. For debugging and replay it's exactly right. The gap is narrow and specific, and it's the gap a compliance reviewer lives in: a mutable telemetry store is not a record whose *edits and deletions are detectable*, and a self-asserted attribute is not a *verified per-principal, per-tenant* attribution you can map to a control. Promptise's edge isn't "nobody else logs anything." It's that tamper-evidence and verified attribution are a *first-class audit primitive* — structural, not a metadata convention you have to remember to set — so a single trail can serve across regimes. If you want the long-form version of that argument, see [Why AI Agent Traces Aren't an Audit Trail (or SOC 2 Proof)](ai-agent-observability-vs-audit-trail.md).

## The primitive: one HMAC-chained audit trail

Here's the whole mechanism in one runnable file. It stands up a records server, verifies a caller's token, records a PHI read, prints the *verified* principal and tenant the audit captured, confirms the chain, then simulates an insider rewriting history to blame a different agent — and watches the chain catch it. Every API is real, and it runs in-process with `TestClient`: no network, no LLM key.

```python
# compliance_audit.py — one tamper-evident trail, three regimes.
import asyncio

from promptise.mcp.server import (
    MCPServer, AuthMiddleware, JWTAuth, AuditMiddleware, TestClient, RequestContext,
)

SECRET = "rotate-me-in-prod"          # prod: PROMPTISE_AUDIT_SECRET from a vault

server = MCPServer(name="records-api")

# 1. Verify the caller's JWT server-side. The principal is CHECKED, not asserted;
#    the tenant is read from the `tenant_id` claim onto ctx.client.tenant_id.
#    (Use JwksAuth against your IdP's keys in production for audience checks.)
auth = JWTAuth(secret=SECRET)
server.add_middleware(AuthMiddleware(auth, tenant_claim="tenant_id"))

# 2. One HMAC-chained trail. include_args stays False so PHI never enters the log.
audit = AuditMiddleware(log_path="records-audit.jsonl", signed=True, hmac_secret=SECRET)
server.add_middleware(audit)


@server.tool(auth=True)
async def read_patient_record(patient_id: str, ctx: RequestContext) -> dict:
    """Read a patient record. Every access is attributable and audited."""
    return {"patient_id": patient_id, "read_by": ctx.client.subject}


async def main() -> None:
    # An IdP-issued token for one clinician agent in tenant "clinic-west".
    token = auth.create_token({
        "sub": "intake-agent",
        "iss": "https://login.example.com",
        "aud": "api://records",
        "roles": ["clinician"],
        "tenant_id": "clinic-west",
    })
    client = TestClient(server, meta={"authorization": f"Bearer {token}"})

    await client.call_tool("read_patient_record", {"patient_id": "P-4471"})

    entry = audit.entries[-1]
    print("identity:", entry["identity"])
    # -> {'subject': 'intake-agent', 'issuer': 'https://login.example.com',
    #     'audience': 'api://records', 'tenant_id': 'clinic-west',
    #     'roles': ['clinician']}
    print("chain valid:", audit.verify_chain())      # True

    # An insider edits the trail to hide who read the record...
    audit.entries[0]["identity"]["subject"] = "billing-agent"
    print("chain valid:", audit.verify_chain())      # False — tamper detected


asyncio.run(main())
```

Two lines of setup produce the whole primitive. `AuditMiddleware(signed=True)` writes one JSON line per call, each carrying an HMAC-SHA256 computed over its own fields *plus* the previous entry's hash (`prev_hash`) — the same chaining idea behind git commits. Edit any field, delete any line, or reorder two entries, and `verify_chain()` returns `False` and the break is localized. The `identity` block is not a string the agent typed about itself; it's what the server extracted *after* validating the token signature, so it names the principal the resource actually authenticated. For the full field reference and recommended middleware ordering, see the [Observability & Audit page](../../mcp/server/observability.md).

## Mapping the trail: SOC 2, HIPAA, EU AI Act Article 12

This is the payoff — the same handful of fields answering three rulebooks. Every entry records `timestamp`, `tool`, `client_id`, `request_id`, `status`, `duration_s`, the `identity` descriptors, and `prev_hash`/`hmac`.

| Regime asks for | The field that answers it |
|---|---|
| **SOC 2 — integrity** (records weren't altered) | `hmac` + `prev_hash` chain; `verify_chain()` returns a plain boolean you can wire into a periodic alarm |
| **SOC 2 — attribution** (whose action) | `identity` block: verified `subject`, `issuer`, `audience`, `roles` |
| **SOC 2 — completeness** (nothing dropped) | Chain linkage — a deleted entry breaks the next entry's `prev_hash` |
| **HIPAA §164.312(b)** — audit controls on PHI access | Automatic per-call entry: `tool`, `timestamp`, `request_id`, and the acting principal |
| **HIPAA §164.312(c)** — protect the record from alteration | The HMAC chain makes any post-hoc edit detectable |
| **HIPAA multi-facility isolation** | `tenant_id` in the identity block — one facility's agent can be proven never to have touched another's tools |
| **EU AI Act Art. 12** — *automatic* logging over the lifetime | `AuditMiddleware` runs on every call by construction, not opt-in per call site |
| **EU AI Act Art. 12** — traceability / attribution | `identity` block ties each lifecycle event to a verified principal |

The `tenant_id` field is what makes this usable in a multi-tenant SaaS or a multi-facility healthcare deployment: `AuthMiddleware(auth, tenant_claim="tenant_id")` reads the tenant from a configurable JWT claim onto `ctx.client.tenant_id`, and it lands in every audit entry's identity descriptors — tenant-scoped forensics with no external join. See [Multi-Tenancy](../../mcp/server/multi-tenancy.md) for how the tenant becomes part of every isolation key. And because Article 12 specifically requires *automatic* recording, the fact that audit is server-side middleware — not a call each tool has to remember — is itself the compliance property; the deeper mapping is in [EU AI Act Article 12: Logging Requirements for AI Agents](eu-ai-act-agent-logging.md).

## Where PHI erasure lives — and why not in the chain

Here's where teams get the architecture backwards, so be precise about it. An append-only, tamper-evident log is the *last* place you want to store erasable personal data — the whole point is that you *can't* quietly remove a line. That's a feature: HIPAA (§164.316) expects you to *retain* audit records, not delete them, and a SOC 2 reviewer treats a log you can edit as no log at all.

So the audit trail is deliberately PII-minimal. `include_args` and `include_result` default to `False`, so tool arguments and results — the fields most likely to carry PHI — never enter the log unless you opt in. The `identity` block records descriptors only (`subject`, `issuer`, `audience`, `roles`, `tenant_id`), never the raw token or the full claim set. The trail proves *who accessed what, when* without becoming a second copy of the sensitive data.

Erasure — the "right to be forgotten" workflow — targets the mutable stores where PII actually lives, not the immutable access log. That's why `purge_user(user_id)` is a first-class method on the surfaces that *hold* user content: memory providers, the semantic cache (the tenant-aware `purge_user(user_id, tenant_id=...)` variant scopes the delete to exactly one tenant), and the observability recorder. You erase the data subject from the operational stores and leave the tamper-evident access record — which was PII-free by design — intact and verifiable. That separation is the honest way to satisfy "retain the access trail" and "erase the personal data" at the same time.

## What this is not: a log primitive, not a GRC program

Overclaiming on compliance is worse than saying nothing, so here's the boundary, stated plainly. `AuditMiddleware` is a *log primitive*. It gives you attributable, tamper-evident, automatically-generated records with a mechanical `verify_chain()` proof. It does not, by itself, make you SOC 2, HIPAA, or EU AI Act compliant — those are programs, not a middleware.

What the primitive does not do: it doesn't write your policies, run your risk assessments, manage business-associate agreements, define retention schedules, or perform the conformity assessment. It doesn't guarantee your storage layer is immutable — pair it with write-once (WORM) or append-only storage and keep the HMAC secret out of the log-writer's reach, or an attacker who holds the key can re-sign a forged chain. And a verified `subject` is only as sound as the auth provider behind it: use `JwksAuth` against your IdP's published keys in production, where the required `audience` check is load-bearing precisely so a token minted for one resource can't stand in for another.

What it does do is give an auditor the one thing a spreadsheet of policies can't: a record they can independently verify wasn't rewritten after the incident. That's the difference between "trust us, the log says so" and "check the math." For the end-to-end picture — auth, tenant isolation, and audit wired into one governed deployment — the [Secure Multi-Tenant Agent Platform](../../guides/secure-multi-tenant-platform.md) guide builds it from scratch.

## Frequently asked questions

### Can one audit log really satisfy SOC 2, HIPAA, and the EU AI Act?

One log *primitive* produces evidence for all three, because the three regimes ask for the same underlying properties — attribution, integrity, and completeness of a record of consequential actions. `AuditMiddleware` supplies those properties (verified `identity` block, HMAC chain, `verify_chain()`). It is not a substitute for the surrounding compliance program — policies, risk assessments, and retention schedules still live outside the log.

### How is this different from LangSmith or LangGraph checkpoints?

LangSmith/Logfire traces are mutable telemetry with self-asserted attributes — ideal for debugging and replay. LangGraph checkpoints are resumable state for durable execution. Neither is designed to be a cryptographically chained record whose edits are detectable, nor a verified per-principal attribution. Promptise makes tamper-evidence and verified attribution structural fields inside the audit itself, which is what a compliance reviewer needs.

### Do I have to log PHI or tool arguments to make the trail useful?

No — and you shouldn't by default. `include_args` and `include_result` are `False` by default, and the identity block records descriptors only. The trail answers "who accessed what, when" without copying the sensitive payload. Turn argument capture on only where the compliance benefit clearly outweighs the privacy cost.

### How do I honor a data-subject erasure request without breaking the audit chain?

Erase from the mutable stores, not the immutable log. Call `purge_user()` on your memory provider, semantic cache (with the `tenant_id=` scope where relevant), and observability recorder to remove the personal data. The tamper-evident access trail — PII-minimal by design — is retained intact, which is exactly what HIPAA's retention rules expect.

### Where do I keep the HMAC secret in production?

Set `PROMPTISE_AUDIT_SECRET` from your secrets manager, or pass `hmac_secret=` explicitly. Use the same key across every instance so any node's log verifies anywhere, and keep it out of reach of whoever can write the log file — otherwise a tamper *and* re-sign is possible.

## Next steps

Stand up one `AuditMiddleware` trail and map it to your regime: add `AuthMiddleware(JWTAuth(...), tenant_claim=...)` and `AuditMiddleware(signed=True)` to your MCP server, run `verify_chain()` on a schedule, and keep `include_args=False`. Start with the [Observability & Audit reference](../../mcp/server/observability.md) for the full field list, then wire tenant isolation and audit together with the [Secure Multi-Tenant Agent Platform](../../guides/secure-multi-tenant-platform.md) guide. From there, the two deep-dives in this cluster take each regime further: [Why AI Agent Traces Aren't an Audit Trail (or SOC 2 Proof)](ai-agent-observability-vs-audit-trail.md) for the SOC 2 argument, and [EU AI Act Article 12: Logging Requirements for AI Agents](eu-ai-act-agent-logging.md) for the record-keeping mandate. One trail, three rulebooks, verifiable math.
