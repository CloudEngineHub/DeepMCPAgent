---
title: "EU AI Act Article 12: Logging Requirements for AI Agents"
description: "Article 12 requires high-risk AI systems to automatically record events over their lifetime for traceability. Stdout prints and cloud traces don't meet…"
keywords: "eu ai act agent logging, eu ai act article 12 record keeping, high-risk ai automatic logging, ai act traceability requirements, eu ai act agent audit log"
date: 2026-07-16
slug: eu-ai-act-agent-logging
categories:
  - Compliance & Audit
---

# EU AI Act Article 12: Logging Requirements for AI Agents

EU AI Act agent logging is where a lot of otherwise-solid agent deployments quietly fail their first audit, because the log they built for debugging is not the log the regulation asks for. Article 12 of Regulation (EU) 2024/1689 requires high-risk AI systems to *technically allow for the automatic recording of events over the lifetime of the system*, at a level of detail that ensures traceability appropriate to the system's intended purpose. If your autonomous agent scores credit applications, screens CVs, or triages a benefits claim — all Annex III high-risk categories — a `print()` to stdout and a span in a trace backend do not clear that bar. This post explains what Article 12 actually asks for, why the usual logging falls short on three specific properties, and how a tamper-evident audit trail with verified per-principal attribution maps to the obligation. (It is engineering guidance, not legal advice — pair it with your own counsel.)

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## What Article 12 actually asks for

Strip Article 12 down to its operative demands and there are two:

1. **Automatic recording of events over the lifetime of the system** (Art. 12(1)). The recording is a property of the system, not a habit of the operator. It has to happen every time, without someone remembering to switch it on.
2. **Traceability appropriate to the intended purpose** (Art. 12(2)). The logs must be detailed enough to identify situations where the system could present a risk, to support post-market monitoring, and to monitor operation. In other words, when something goes wrong, the record has to let you reconstruct *what the system did* well enough to investigate it.

Two adjacent obligations set the stakes. Under Article 19 and Article 26(6), providers and deployers must **keep** those automatically generated logs — to the extent they are under their control — for a period appropriate to the purpose, and at least six months unless other law says otherwise. So the record you generate today is evidence someone may pull half a year from now, long after the incident, possibly in front of a market-surveillance authority.

Notice the word the regulation leans on: *traceability*. A record that can be silently edited, or that names "the system" rather than which principal acted, does not deliver traceability that survives scrutiny. The Act states the goal; integrity and attribution are how you actually meet it.

## Why stdout and cloud traces don't clear the bar

Most agent stacks log the obvious way: print tool calls to stdout, or emit spans to a tracing backend. That is genuinely useful for debugging. Measured against Article 12, it misses on three properties.

- **Automatic.** A `logger.info(...)` line lives in your handler and runs only if the code path reaches it and no one commented it out. A logging *capability of the system* is something the framework applies to every call by construction. The difference matters the moment an untested branch is exactly the one that caused the incident.
- **Tamper-resistant.** Traceability is only real if the record reflects what happened. A flat JSONL file or a mutable trace database can be edited, reordered, or truncated after the fact, and nothing about the surviving rows reveals it. "The log says so" is not an answer when the log is the thing under question.
- **Attributable to a principal.** "Traceability of the functioning of the system" for a fleet of agents sharing one key collapses to "the model did it." You can see *what* happened, not *which* agent, under *which* tenant, holding *which* verified credential.

None of these are exotic. They are the same properties that separate a debug log from an audit trail in every other regulated domain. We pull that distinction apart in detail in [Why AI Agent Traces Aren't an Audit Trail (or SOC 2 Proof)](ai-agent-observability-vs-audit-trail.md) — the short version is that a trace answers "was it fast?" and an audit record answers "can you prove who did it, and that this record wasn't changed?"

## What other frameworks do today

It is worth being precise and fair, because the mainstream agent frameworks *do* record events — just not ones that clear all three properties for a high-risk system.

- **LangChain callbacks and LangSmith tracing** let you attach a `run_name`, tags, and a free-form `metadata` dict to every run, and LangSmith *durably persists* those traces for inspection. That persistence is a real, partial step toward "automatic recording over the lifetime." The two gaps are specific: the "who" in that metadata is written by the emitting process (a run labels *itself*, unverified against any identity provider), and the trace store is a queryable, mutable database, not a per-record signed chain. Nothing stops a later edit from being invisible.
- **OpenTelemetry / OpenLLMetry GenAI instrumentation** is the same shape one layer down. Span attributes — including any user or agent id — are set by the SDK at emit time, so they are self-reported, and a span backend is built for aggregation and retention, not tamper-evidence per record.
- **CrewAI** surfaces verbose execution logs and can export to those same backends; the actor it records is the configured agent's *role name* — again a string the framework assigns to itself.

So the honest delta is narrow: these frameworks give you recording, and some give you durable retention, but they **leave lifecycle logging as something you assemble** — you bolt on WORM storage, a SIEM, and your own identity plumbing to reach integrity and verified attribution. For debugging none of that matters. For an Article 12 obligation, integrity and a verified principal are the whole point. Promptise's edge is not "nobody else logs events." It is that *automatic, attributable, tamper-evident recording is a first-class server primitive* — one middleware — rather than a metadata convention you have to remember to set and a storage layer you have to source separately.

## An Article-12-grade event record with AuditMiddleware

Here is the mechanism in one runnable file. It stands up a loan-decisioning server (Annex III, point 5 — creditworthiness evaluation is explicitly high-risk), verifies the scoring agent's JWT server-side, records a scoring decision, prints the **verified** principal and tenant the audit captured, then simulates someone editing the log months later to hide who acted — and watches the chain catch it. Every API is real, and it runs in-process with `TestClient`: no network, no LLM key.

```python
# article12_audit.py — an Article-12-grade event record for one agent action.
import asyncio

from promptise.mcp.server import (
    MCPServer, AuthMiddleware, JWTAuth, AuditMiddleware, TestClient, RequestContext,
)

SECRET = "rotate-me-in-prod"          # in prod: PROMPTISE_AUDIT_SECRET from a vault

server = MCPServer(name="loan-decisioning")

# 1. Verify the caller's JWT server-side. The principal is CHECKED, not asserted,
#    and the `tenant_id` claim lands on ctx.client.tenant_id.
auth = JWTAuth(secret=SECRET)
server.add_middleware(AuthMiddleware(auth))   # default tenant_claim="tenant_id"

# 2. Record every call to an HMAC-chained, append-only event log. The verified
#    subject / issuer / audience / tenant_id / roles land as a first-class
#    `identity` field in each entry — automatic, per-principal, tamper-evident.
audit = AuditMiddleware(log_path="loan-audit.jsonl", signed=True, hmac_secret=SECRET)
server.add_middleware(audit)


@server.tool(auth=True)
async def score_application(applicant_id: str, ctx: RequestContext) -> dict:
    """A high-risk decision: score a credit application."""
    return {"applicant_id": applicant_id, "decision": "refer", "by": ctx.client.subject}


async def main() -> None:
    # The scoring agent presents an IdP-issued token (minted here for the demo).
    token = auth.create_token({
        "sub": "scoring-agent-3",
        "iss": "https://login.example.com",
        "aud": "api://loan-decisioning",
        "tenant_id": "acme-bank",
        "roles": ["scorer"],
    })
    client = TestClient(server, meta={"authorization": f"Bearer {token}"})

    await client.call_tool("score_application", {"applicant_id": "APP-4471"})

    # The event record attributes the action to a VERIFIED principal + tenant.
    entry = audit.entries[-1]
    print("identity:", entry["identity"])
    print("chain valid:", audit.verify_chain())        # True

    # Six months later, someone edits the log to hide who scored the applicant.
    audit.entries[0]["identity"]["subject"] = "someone-else"
    print("chain valid:", audit.verify_chain())        # False — tamper detected


asyncio.run(main())
```

Run it and the identity block prints the *verified* descriptors the server extracted after validating the signature — `subject`, `issuer`, `audience`, `tenant_id`, `roles` — and `verify_chain()` flips from `True` to `False` the instant an entry is rewritten. The verified principal comes from the auth layer described in [Authentication & Security](../../mcp/server/auth-security.md): `AuthMiddleware` calls `JWTAuth` (or `JwksAuth` against your IdP's published keys), and only descriptors it authenticated land on `ctx.client`. `AuditMiddleware` never trusts a "who" the caller typed about itself.

## What each entry records — and how it maps to the obligation

`AuditMiddleware` writes one signed JSON line per call. Every entry carries the fields that make it evidence rather than telemetry:

- **`tool`, `client_id`, `request_id`** — the action, the caller, and a correlation id (Article 12's "which situation").
- **`status`, `error`, `duration_s`, `timestamp`** — the outcome and when it happened.
- **`identity`** — the verified `subject`, `issuer`, `audience`, `tenant_id`, and `roles`, present whenever the request was authenticated with a JWT or JWKS provider. This is the "attributable to a principal" property, and the `tenant_id` field is what lets you prove, in a multi-tenant SaaS, that one customer's agent never touched another's.
- **`prev_hash` and `hmac`** — each entry's keyed HMAC-SHA256 hashes its own contents *plus the previous entry's hash*, forming a chain. Edit, delete, or reorder any entry and the linkage breaks at that point; `verify_chain()` is a single pass that returns a plain boolean you can wire into a periodic integrity alarm.

Argument and result capture are opt-in (`include_args`, `include_result`), off by default, because tool arguments and results routinely contain personal data — you turn them on deliberately, only where the traceability benefit outweighs the privacy cost of logging the input. Because the whole entry is what the HMAC covers, rewriting the `subject` from `scoring-agent-3` to `someone-else` after the fact is precisely the edit the chain is built to catch. This audit primitive sits alongside the metrics, structured logging, and dashboard covered in [Observability & Monitoring](../../mcp/server/observability.md) — you keep the fast, mutable telemetry for operations and add the slow, verifiable chain exactly where the record is evidence.

## Honest scope: what audit logging is not

`AuditMiddleware` delivers the record-keeping mechanism Article 12 turns on — automatic, attributable, tamper-evident. It does not, by itself, make you compliant, and it is worth being blunt about the boundaries:

- **Article 12 is one obligation among many.** Risk management (Art. 9), data governance (Art. 10), human oversight (Art. 14), and transparency (Art. 13) are separate duties. A perfect log is necessary, not sufficient.
- **Retention is your storage layer's job.** The middleware generates and signs the record; keeping it for the required period (at least six months under Arts. 19 and 26(6)) and controlling who can access it is a storage and IAM decision. Pair the chain with write-once (WORM) or append-only storage and you get integrity at *both* the record and the medium.
- **The verified subject proves the token was valid, not that the human behind it was.** Attribution to `scoring-agent-3` is exactly as sound as the `audience` check that stopped a token minted for another resource from standing in.

The upside of making the record a structural primitive is that one trail can serve several regimes at once. The identity-attributed, tamper-evident entries that satisfy Article 12's traceability are the same entries a SOC 2 or HIPAA reviewer asks for — we walk through that convergence in [One Audit Trail for SOC 2, HIPAA and the EU AI Act](ai-agent-compliance-audit-trail.md).

## Frequently asked questions

### Does Article 12 literally require a tamper-evident log?

Article 12 requires *automatic recording of events over the lifetime* at a detail level that ensures *traceability appropriate to the intended purpose*; it does not spell out "HMAC chain." Tamper-evidence is the engineering property that makes traceability hold up when the record is later disputed or edited — the Act sets the goal, and an integrity-protected, attributable log is how you demonstrably meet it. Treat cryptographic integrity as the defensible implementation of "traceability," not as a verbatim legal requirement, and confirm specifics with counsel.

### Isn't LangSmith or OpenTelemetry tracing enough for high-risk logging?

For debugging and operations, yes. For an Article 12 high-risk obligation there are two specific gaps: the actor recorded on a trace or span is set by the emitting process (self-asserted, not verified against an identity provider), and the destination is a mutable database rather than a per-record signed chain. LangSmith does persist traces durably, which is a real partial step; the delta is integrity and verified attribution, which `AuditMiddleware(signed=True)` makes structural.

### How does the log attribute an action to a specific agent and tenant?

Verify the caller's token server-side with `JWTAuth` or `JwksAuth`. `AuthMiddleware` populates `ctx.client` with the authenticated `subject`, `issuer`, `audience`, `roles`, and — from the configurable `tenant_claim` (default `tenant_id`) — `tenant_id`. `AuditMiddleware` then writes those verified descriptors as the `identity` block in every entry. The raw token and full claim set are never logged, so sensitive data stays out of the trail.

### How long do I have to keep these logs?

Under Articles 19 and 26(6), providers and deployers must keep the automatically generated logs — to the extent under their control — for a period appropriate to the intended purpose and at least six months, unless other applicable law says otherwise. The middleware produces the durable JSONL; retention and access control are your storage layer's responsibility.

## Next steps

Add `AuditMiddleware(signed=True)` behind `AuthMiddleware(JWTAuth(...))` on your MCP server, set `PROMPTISE_AUDIT_SECRET` from your secrets manager, and your next high-risk agent action lands in an automatic, attributable, tamper-evident record — one `add_middleware` call from "we print to stdout" to "we can prove who did it and that this line wasn't changed." Start from [Authentication & Security](../../mcp/server/auth-security.md) to wire verified identity, see the full monitoring stack in [Observability & Monitoring](../../mcp/server/observability.md), and read [One Audit Trail for SOC 2, HIPAA and the EU AI Act](ai-agent-compliance-audit-trail.md) to see how the same trail serves more than one regime.
