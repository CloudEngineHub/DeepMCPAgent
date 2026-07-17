---
title: "HIPAA-Grade Audit Logging for Healthcare AI Agents"
description: "HIPAA needs attributable access logs and supports erasure of upstream data. Shows how to record who-touched-what tamper-evidently while keeping PHI out of…"
keywords: "hipaa ai agent audit, hipaa audit log ai agent, phi access logging llm, healthcare ai agent compliance logging, right to erasure ai agent log"
date: 2026-07-16
slug: hipaa-ai-agent-audit
categories:
  - Compliance & Audit
---

# HIPAA-Grade Audit Logging for Healthcare AI Agents

A HIPAA AI agent audit has one non-negotiable job: prove *which* verified principal touched *which* patient record, *when*, in a record an insider can't quietly rewrite — while never turning that log into a second, leakier copy of the PHI it describes. When an autonomous agent calls a `read_patient_record` tool, a `schedule_appointment` tool, or anything that reads or writes electronic protected health information, HIPAA's audit-controls rule (§164.312(b)) expects a mechanism that records and lets you examine that activity, and §164.312(c) expects that record to be protected from improper alteration. At the same time, HIPAA's Privacy Rule and overlapping regimes like GDPR give data subjects a path to *erasure* of their upstream data. Those two demands pull in opposite directions — retain the access trail, delete the personal data — and most agent stacks make you reconcile them by hand. This post shows how Promptise Foundry's `AuditMiddleware` plus a tenant-scoped `purge_user` do it structurally: attributable and PHI-safe by default, erasable where the PHI actually lives, with the integrity chain left intact.

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## What HIPAA actually asks of an AI agent's logs

Strip the paperwork away and the audit-controls rule reduces to four concrete properties your log must have when a covered entity's agent handles PHI:

- **Attribution.** Every access is tied to a *specific* acting identity — not "the service account," but the agent principal and, in a multi-facility deployment, the facility it acted for. §164.312(b) is about examining activity by who did it.
- **Integrity.** The record survives an insider with write access. §164.312(c) is explicit that ePHI-related records must be protected from improper alteration or destruction, and an access log you can silently edit fails that on its face.
- **Retention, not deletion.** §164.316 expects you to *keep* audit documentation (six years is the common baseline). The access trail is the one thing you are *not* supposed to erase on request.
- **Minimum necessary.** The Privacy Rule's minimum-necessary principle cuts against dumping full request payloads into a log. An access record should prove *that* a record was read without becoming a redundant, sprawling copy of *what* was in it.

Hold those next to the right-to-erasure workflow and the tension is obvious: you must retain a tamper-proof access trail *and* be able to remove a data subject's personal data from your systems. The only clean way to satisfy both is to keep the two in different places — a PHI-free access log you retain, and mutable operational stores you can purge. That architectural split is the whole design below.

## What other frameworks do today

Being fair here matters, because the popular agent and tracing stacks absolutely *do* record what an agent did. The gap isn't "nobody logs." It's that the three HIPAA-relevant primitives — attributable *and* tamper-evident access logs, PHI-safe-by-default capture, and a tenant-scoped erasure that leaves the log's integrity intact — rarely ship together, so teams bolt them on by hand. Naming the actual behavior precisely:

- **LangSmith / LangChain tracing** captures run inputs and outputs *by default* and stores them as spans in a hosted, mutable database. It's excellent observability, and it does offer masking — `hide_inputs`/`hide_outputs` and rule-based redaction you configure — plus run deletion for erasure. The exact deltas for a HIPAA posture: PHI capture is opt-*out* (you must remember to mask) rather than opt-*in*; the span's user/agent attribute is written by the emitting process, so the "who" is self-reported rather than checked against the token; and the store is telemetry, not a per-record signed chain whose edits are detectable.
- **LangGraph checkpointers** persist graph state at each step so a run can resume, time-travel, or hand to a human. That's genuinely useful durable execution — but a checkpoint is *resumable state* in a mutable backend (SQLite, Postgres, Redis), not a cryptographically chained access record. Resume-after-crash and tamper-evidence are different guarantees.
- **OpenTelemetry / OpenLLMetry GenAI** instrumentation is the same shape one layer down: attributes set at emit time, stored in a queryable backend. Superb for latency and cost; not a keyed, chained audit record.
- **CrewAI and AutoGen** leave the audit trail to you entirely — they don't ship a tamper-evident tool-call log or a built-in PHI-safe capture default, so the attribution, integrity, and erasure plumbing is yours to write and wire together.

None of this is *wrong* — for debugging and replay it's exactly right. Promptise's edge isn't a missing feature elsewhere; it's that tamper-evidence, verified attribution, and PHI-safe capture are a **first-class audit primitive with the safe defaults already set**, and erasure is a first-class method on the stores that actually hold PHI. You configure the HIPAA posture rather than assemble it. The long-form version of the "traces aren't an audit trail" argument lives in [Why AI Agent Traces Aren't an Audit Trail (or SOC 2 Proof)](ai-agent-observability-vs-audit-trail.md).

## The primitive: attributable, PHI-safe audit in two lines

Here is the whole mechanism in one runnable file. It stands up a records server, verifies a clinician agent's token, gates the tool behind a capability guard, records a PHI read *without* the PHI, prints the **verified** principal and facility the audit captured, confirms the chain, then simulates an insider rewriting history to blame a different agent — and watches the chain catch it. Every API is real, and it runs in-process with `TestClient`: no network, no LLM key.

```python
# hipaa_audit.py — attributable, PHI-safe, tamper-evident access logging.
import asyncio

from promptise.mcp.server import (
    MCPServer, AuthMiddleware, JWTAuth, AuditMiddleware,
    HasRole, TestClient, RequestContext,
)

SECRET = "rotate-me-in-prod"          # prod: PROMPTISE_AUDIT_SECRET from a vault

server = MCPServer(name="records-api")

# 1. Verify the caller's JWT server-side. The principal is CHECKED, not asserted;
#    the facility is read from the `tenant_id` claim onto ctx.client.tenant_id.
#    (Use JwksAuth against your IdP's published keys in production.)
auth = JWTAuth(secret=SECRET)
server.add_middleware(AuthMiddleware(auth, tenant_claim="tenant_id"))

# 2. One HMAC-chained trail. include_args defaults to False, so patient_id and any
#    PHI in arguments or results never enter the log — minimum necessary by default.
audit = AuditMiddleware(log_path="records-audit.jsonl", signed=True, hmac_secret=SECRET)
server.add_middleware(audit)


@server.tool(auth=True, guards=[HasRole("clinician")])
async def read_patient_record(patient_id: str, ctx: RequestContext) -> dict:
    """Read a patient record. Only clinicians; every access is attributable."""
    return {"patient_id": patient_id, "read_by": ctx.client.subject}


async def main() -> None:
    # An IdP-issued token for one clinician agent in facility "clinic-west".
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
    print("args captured:", "args" in entry)          # False — PHI stayed out
    print("chain valid:", audit.verify_chain())       # True

    # An insider edits the trail to hide who read the record...
    audit.entries[0]["identity"]["subject"] = "billing-agent"
    print("chain valid:", audit.verify_chain())       # False — tamper detected


asyncio.run(main())
```

Two lines of middleware produce the whole primitive. `AuthMiddleware(auth, tenant_claim="tenant_id")` validates the JWT signature and lifts the verified `subject`, `roles`, and facility (`tenant_id`) onto `ctx.client` — so the `identity` block in the log is what the server *authenticated*, not a string the agent typed about itself. `HasRole("clinician")` is a per-tool capability guard: a token without the `clinician` role is denied before the handler runs, and that denied call is still audited. `AuditMiddleware(signed=True)` writes one JSON line per call, each carrying an HMAC-SHA256 over its own fields *plus* the previous entry's hash (`prev_hash`) — the git-commit chaining idea. Edit a field, delete a line, or reorder two entries and `verify_chain()` returns `False`, localizing the break. Crucially, `include_args` and `include_result` default to `False`, so `patient_id` — and anything PHI-bearing in results — never lands in the log. The verified-identity mechanics behind `AuthMiddleware` and the guard model are covered in [Authentication & Security](../../mcp/server/auth-security.md); the audit field reference and recommended middleware ordering are on the [Observability & Audit page](../../core/observability.md).

## Erasure without breaking the chain: purge_user across memory, cache, observability

Here's where teams get the architecture backwards. An append-only, tamper-evident log is the *last* place you want erasable personal data — the whole point is that you *can't* quietly remove a line, and HIPAA §164.316 wants you to retain it. So the access trail is PHI-free by construction (identity descriptors and hashes, never the payload), and the right-to-erasure workflow targets the *mutable* stores where a patient's data actually accumulates: the agent's memory, its semantic cache, and its observability timeline.

That's why `purge_user` is a first-class method on each of those surfaces, and the cache's variant is tenant-scoped so you erase exactly one facility's copy:

```python
# erase_patient.py — honor a right-to-erasure request across the mutable stores,
# leaving the tamper-evident access trail (which holds no PHI) intact.
async def erase_data_subject(user_id: str, *, facility: str,
                             memory, cache, recorder) -> None:
    # 1. Long-term memory (InMemoryProvider / ChromaProvider / Mem0Provider).
    n_mem = await memory.purge_user(user_id)

    # 2. Semantic cache — tenant_id scopes the delete to ONE facility's entries,
    #    so two facilities sharing a user_id can never collide.
    n_cache = await cache.purge_user(user_id, tenant_id=facility)

    # 3. Observability timeline (sync). Already-flushed external sinks are theirs.
    n_obs = recorder.purge_user(user_id)

    print(f"erased memory={n_mem} cache={n_cache} observability={n_obs}")
    # The HMAC-chained audit trail is deliberately NOT touched here:
    # it names who-accessed-what, holds no PHI, and stays verifiable.
```

Each call returns the count it removed, and the audit chain is never in the loop — it was PHI-free from the start, so retaining it doesn't retain the data subject's PHI. That's the honest way to satisfy "keep the six-year access trail" and "erase the personal data" at the same time. Because the cache purge is keyed by an injective `(tenant_id, user_id)` pair, a data-subject request for one facility can't accidentally wipe another's cache, and can't miss entries because two facilities happened to reuse the same `user_id`. The observability recorder's `purge_user` clears the in-memory timeline; already-flushed external sinks (a JSON file on disk, your SIEM) are external systems you purge on their own terms — the [Observability & Audit page](../../core/observability.md) is explicit about that boundary. For the end-to-end wiring — auth, per-facility isolation, audit, and erasure in one governed deployment — the [Secure Multi-Tenant Agent Platform](../../guides/secure-multi-tenant-platform.md) guide builds it from scratch.

## Where the log ends and your compliance program begins

Overclaiming on HIPAA is worse than saying nothing, so state the boundary plainly. `AuditMiddleware` is a *log primitive*: attributable, tamper-evident, automatically-generated access records with a mechanical `verify_chain()` proof, and PHI-safe defaults. It does not, by itself, make you HIPAA compliant. It doesn't write your policies, run your risk analysis, sign your business-associate agreements, or set your retention schedule.

Three caveats decide whether the primitive actually holds up:

- **Storage immutability is yours.** The HMAC chain makes edits *detectable*, not *impossible*. Pair it with write-once (WORM) or append-only storage so an attacker can't simply drop the file, and keep the HMAC secret out of the log-writer's reach — an adversary who holds the key can re-sign a forged chain. Set `PROMPTISE_AUDIT_SECRET` from your secrets manager, the same key across every instance so any node's log verifies anywhere.
- **A verified subject is only as sound as the IdP behind it.** In production use `JwksAuth` against your identity provider's published keys, where the `audience` check is load-bearing precisely so a token minted for one resource can't stand in for another.
- **Erasure completeness is a program property.** `purge_user` covers the stores Promptise owns; any downstream warehouse, backup, or third-party sink that received PHI is yours to purge on its own terms.

What the primitive *does* give you is the one thing a binder of policies can't: a record an auditor can independently verify wasn't rewritten after an incident — "check the math," not "trust us." For how this same trail also produces evidence for SOC 2 and the EU AI Act from the same fields, see [One Audit Trail for SOC 2, HIPAA and the EU AI Act](ai-agent-compliance-audit-trail.md).

## Frequently asked questions

### Does HIPAA-grade audit logging mean I have to store PHI in the log?

No — and you shouldn't. `AuditMiddleware` sets `include_args` and `include_result` to `False` by default, so tool arguments (like `patient_id`) and results never enter the log. The `identity` block records descriptors only — verified `subject`, `issuer`, `audience`, `roles`, `tenant_id` — never the raw token or claim set. The trail proves *who accessed what, when* without becoming a second copy of the PHI, which is exactly what the minimum-necessary principle wants.

### How do I honor a right-to-erasure request without breaking the audit chain?

Erase from the mutable stores, not the immutable access log. Call `purge_user()` on your memory provider, the semantic cache (with `tenant_id=` for the right facility), and the observability recorder. Because the audit trail holds identity descriptors and hashes — never PHI — retaining it doesn't retain the data subject's data, and §164.316's retention expectation is satisfied at the same time.

### Is the "who" in the log trustworthy, or self-reported?

It's verified. `AuthMiddleware` validates the JWT signature and populates `ctx.client` from the checked claims, so the `identity` block names the principal the server *authenticated*, not a value the agent asserted. Add `HasRole(...)` guards to gate PHI tools by capability, and use `JwksAuth` against your IdP in production so the signature and `audience` are checked against your provider's keys.

### How is this different from LangSmith tracing or LangGraph checkpoints?

LangSmith traces capture inputs/outputs by default in a mutable store with self-reported attributes (redaction and deletion are available but opt-in). LangGraph checkpoints are resumable state, not a signed record. Neither is a per-record HMAC chain whose edits are detectable, and neither defaults to keeping PHI out. Promptise makes tamper-evidence, verified attribution, and PHI-safe capture structural defaults, and pairs them with a tenant-scoped `purge_user` for erasure.

### Where do I keep the HMAC secret in production?

Set `PROMPTISE_AUDIT_SECRET` from your secrets manager, or pass `hmac_secret=` explicitly. Use the same key across every instance so any node's log verifies anywhere, and keep it out of reach of whoever can write the log file — otherwise a tamper *and* re-sign is possible.

## Next steps

Deploy attributable, PHI-safe audit logging with `purge_user`: add `AuthMiddleware(JWTAuth(...), tenant_claim="tenant_id")` and `AuditMiddleware(signed=True)` to your MCP server, gate PHI tools with `HasRole(...)`, keep `include_args=False`, and run `verify_chain()` on a schedule. Start with [Authentication & Security](../../mcp/server/auth-security.md) for the verified-identity and guard model, then the [Observability & Audit reference](../../core/observability.md) for the full audit field list and `purge_user` semantics. When you're ready to wire auth, per-facility isolation, audit, and erasure into one governed deployment, the [Secure Multi-Tenant Agent Platform](../../guides/secure-multi-tenant-platform.md) guide builds it end to end — and [One Audit Trail for SOC 2, HIPAA and the EU AI Act](ai-agent-compliance-audit-trail.md) shows the same trail satisfying three regimes at once.
