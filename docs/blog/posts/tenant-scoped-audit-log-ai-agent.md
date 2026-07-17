---
title: "Tenant-Scoped Audit Logs for AI Agent Tool Calls"
description: "A compliance auditor asks 'show me every action taken for Acme, and prove the log wasn't edited.' This post is the per-tenant forensics angle specifically…"
keywords: "tenant-scoped audit log ai agent, filter audit trail by tenant, attribute tool calls to a tenant, per-tenant forensics ai agent, prove a per-tenant trail wasn't altered"
date: 2026-07-16
slug: tenant-scoped-audit-log-ai-agent
categories:
  - Multi-Tenancy
---

# Tenant-Scoped Audit Logs for AI Agent Tool Calls

A **tenant-scoped audit log** for an AI agent's tool calls exists to answer two questions a compliance auditor puts to you in the same breath: *show me every action your system took for Acme*, and *prove that record wasn't edited after the fact*. Both are easy to wave at and hard to actually satisfy. A general trace store can usually show you *something* about Acme, but "every action" means the tenant has to be a field you can filter on cleanly — not a tag one call site set and another forgot. And "prove it wasn't edited" means the trail needs integrity you can hand to a third party, not your assurance that the database looks fine. This post is about the per-tenant forensics angle specifically: how to produce a slice of the trail that belongs to exactly one customer, and hand it over with a proof it wasn't altered.

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## The auditor's two questions

Picture the scenario concretely. Acme, one of forty customers on your shared agent platform, has a dispute — a record was changed, and their legal team wants to know which automated action touched it and when. The auditor's request has two clauses, and each one breaks a naive logging setup in a different way.

**"Show me every action for Acme."** Agents are not one-request-per-action clients. Give an agent a task and it fans out across tools — reads, writes, retries, reformulations — dozens of tool calls per turn. So the raw material is there; the problem is slicing it. If your log keys each line on `client_id`, and you provisioned one service identity per agent *role* rather than per customer — a single `report-agent` credential deployed into every tenant's workspace — then Acme's `report-agent` and Globex's `report-agent` write byte-for-byte identical `client_id` values. Filtering on `client_id` hands the auditor both tenants' actions blended together. That is the same failure mode we covered for storage keys in [Same user_id, Two Tenants: Why That Isn't Isolation](same-user-id-across-two-tenants.md): an inner identifier that collides across tenants can't be the thing you slice on. You need the tenant as its own dimension.

**"Prove the log wasn't edited."** A JSONL file on disk is trivially editable. So is a row in a traces table. If your only answer to "did someone alter this?" is "our access controls are good," you don't have a forensic record — you have a hopeful one. What closes this clause is per-entry tamper-evidence: a cryptographic chain where changing, reordering, or deleting any line breaks a verification that anyone with the key can run.

Promptise Foundry's `AuditMiddleware` answers both clauses in the same object, because the tenant is a first-class field *inside* an HMAC-chained entry.

## Slice by tenant: a filter on a first-class field

Here is the mechanism end to end, and it is worth running because the whole argument is in the output. Two API keys deliberately share one `client_id` (`report-agent`) and differ only in `tenant_id`. `AuditMiddleware` records each tool call; then we slice the trail down to a single tenant by filtering on a first-class field — not by parsing a tag out of free-form metadata.

```python
import asyncio

from promptise.mcp.server import (
    MCPServer, TestClient, AuthMiddleware, APIKeyAuth, AuditMiddleware,
)

server = MCPServer("crm")

# Tamper-evident audit log. A fixed hmac_secret makes the chain verifiable
# across restarts (in production, load it from PROMPTISE_AUDIT_SECRET).
audit = AuditMiddleware(hmac_secret="rotate-me-in-prod")
server.add_middleware(audit)

# Two tenants, ONE shared client_id. Only the tenant differs.
server.add_middleware(
    AuthMiddleware(
        APIKeyAuth(keys={
            "sk-acme":   {"client_id": "report-agent", "roles": ["analyst"], "tenant_id": "acme"},
            "sk-globex": {"client_id": "report-agent", "roles": ["analyst"], "tenant_id": "globex"},
        })
    )
)


@server.tool(auth=True)
async def close_ticket(ticket_id: str) -> dict:
    """A write action worth attributing to exactly one tenant."""
    return {"ticket_id": ticket_id, "state": "closed"}


async def main() -> None:
    client = TestClient(server)
    for api_key, ticket in [("sk-acme", "T-1"), ("sk-globex", "T-2"), ("sk-acme", "T-3")]:
        await client.call_tool(
            "close_ticket", {"ticket_id": ticket}, headers={"x-api-key": api_key}
        )

    # "Show me every action for Acme" — a filter on a first-class field,
    # NOT on client_id (which both tenants share).
    acme_only = [e for e in audit.entries if e.get("identity", {}).get("tenant_id") == "acme"]
    for e in acme_only:
        ident = e["identity"]
        print(f'{ident["tenant_id"]:7} {e["tool"]:13} {e["status"]:4} '
              f'client={e["client_id"]} req={e["request_id"][:8]} hmac={e["hmac"][:12]}')

    print(f"\n{len(acme_only)} of {len(audit.entries)} entries belong to acme")


asyncio.run(main())
```

Running it prints:

```
acme    close_ticket  ok   client=report-agent req=a24e05aa hmac=f03bcea3568c
acme    close_ticket  ok   client=report-agent req=8e723a89 hmac=db89144837d4

2 of 3 entries belong to acme
```

Two things to notice. First, both tenants share `client_id=report-agent`, yet the Acme slice is exact — the filter reads `identity.tenant_id`, a field the auth layer populated from the caller's credentials, so Globex's identical-looking call never leaks into Acme's forensic view. Second, the whole pipeline — auth, the audit entry, the handler — ran in-process through `TestClient` with no network, so "the tenant slice is correct" is a property you can assert in a unit test, not something you discover during the audit itself.

Each entry is a plain dict (one JSON line when `log_path` is set) shaped like this:

```json
{
  "tool": "close_ticket",
  "client_id": "report-agent",
  "request_id": "a24e05aad3ea",
  "status": "ok",
  "duration_s": 0.0,
  "identity": { "tenant_id": "acme", "roles": ["analyst"] },
  "prev_hash": "0000...0000",
  "hmac": "f03bcea3568c44547b2e43826b4ada4c55d320910a2eec0be15391d073a58f13"
}
```

The `identity` block is the acting principal, captured from the verified credential — not just *which tenant* but *which agent role*. Under `JWTAuth`, that block also carries the token's `subject`, `issuer`, and `audience`, so a delegated call is attributed to the agent that actually presented the token. The token itself and the raw claim set are never written — only identity descriptors — so the audit log stays useful without becoming a second place your secrets leak.

## Prove it wasn't edited: the HMAC chain

The second clause — *prove nobody altered the record* — is what `prev_hash` and `hmac` are for. Each entry stores the HMAC of the previous entry, so the lines form a chain: edit one field, drop a line, or reorder two calls, and every downstream hash stops matching. You don't audit the auditor's honesty; you run a check:

```python
print("chain valid:", audit.verify_chain())          # True

# Someone edits a delivered log to hide an action...
audit.entries[0]["tool"] = "read_ticket"
print("chain valid:", audit.verify_chain())          # False
```

`verify_chain()` recomputes every entry's HMAC from its contents and confirms each `prev_hash` links to the one before it. Any post-hoc edit — including inside the Acme slice you handed over — flips it to `False`. Crucially, verification only needs the entries and the shared secret, so the recipient of a per-tenant export can confirm integrity themselves. Set `PROMPTISE_AUDIT_SECRET` (rather than letting the middleware auto-generate one) so the chain stays verifiable across process restarts; without a stable secret the log is still written, but it can't be checked after a restart. The mechanics of the chain itself — genesis hash, concurrency locking, why HMAC over a plain digest — are covered in [AI Agent Audit Logging: Tamper-Evident by Design](ai-agent-audit-logging.md); here the point is narrower: the *per-tenant* slice inherits that integrity, so filtering to one customer doesn't cost you the proof.

## Wire it for production: tenant from the JWT

The API-key map above keeps the demo self-contained. In production the tenant rides on the JWT, and `AuthMiddleware` lifts it from a configurable claim into `ctx.client.tenant_id`, which is exactly what `AuditMiddleware` stamps into each entry's `identity`:

```python
import os

from promptise.mcp.server import MCPServer, AuthMiddleware, JWTAuth, AuditMiddleware

server = MCPServer("crm")

server.add_middleware(
    AuditMiddleware(
        log_path="audit.jsonl",                       # one JSON line per tool call
        hmac_secret=os.environ["PROMPTISE_AUDIT_SECRET"],  # stable, verifiable across restarts
    )
)
server.add_middleware(
    AuthMiddleware(
        JWTAuth(secret="...", audience="api://crm"),
        tenant_claim="org_id",                        # whatever claim your IdP emits
    )
)
```

Now every tool call the agent makes lands in `audit.jsonl` stamped with the tenant from the signed token — no per-handler code, no manual tagging. Because the same `tenant_id` also keys rate-limit buckets, cache scopes, and tool-access guards, the value in your audit trail is the *same* value that isolated the request everywhere else; the [multi-tenancy reference](../../mcp/server/multi-tenancy.md) lists every surface it governs. To make it impossible to ship a tool that logs an *untenanted* action, build the server with `require_tenant=True` so any token missing the claim is denied before the handler runs. For the full wiring — token issuance, tenant-scoped tools, approval gates, and this tenant-stamped audit assembled into one deployment — the [Build a Secure Multi-Tenant Agent Platform](../../guides/secure-multi-tenant-platform.md) guide is the reference build.

## What other frameworks do today

To be fair about the delta, the popular observability stacks let you get *close*, and it's worth being precise about where the gap actually is.

- **LangSmith** attaches arbitrary `metadata` and `tags` to runs, and its UI and SDK let you filter and search traces by those values. Putting `{"tenant": "acme"}` on every run and filtering on it is a genuinely supported workflow. What it is, though, is a mutable observability store built for debugging and eval: the tenant is a free-form metadata string you remember to set (and could set inconsistently), not a reserved field derived from the verified credential, and there is no per-record cryptographic chain that proves a delivered trace slice wasn't edited or deleted in the backend afterward.
- **OpenTelemetry**-based agent tracing lets you set span attributes — there's even a semantic-convention `enduser.id` — and query them in whatever backend you export to (Tempo, Jaeger, Honeycomb). Again real and useful. But spans are telemetry: commonly *sampled*, so the record is not guaranteed complete; the attribute is a string on a span, not a first-class tenant dimension; and the backing store offers no tamper-evident hash chain a regulator can independently verify.

So nobody here "can't record a tenant" — both let you attach one and filter on it, and for observability that's the right tool. The precise delta is two structural properties. First, in Promptise the tenant is a **reserved field populated from the verified credential** (`identity.tenant_id`), so it can't be a tag one call site forgets and sampling can't quietly drop it. Second, each entry is **HMAC-chained and independently verifiable**, so a per-tenant slice ships with its own integrity proof. Promptise's contribution isn't "we invented audit logs"; it's that *filter by tenant* and *prove this slice wasn't altered* are both first-class on the same record, rather than a metadata convention layered over a mutable, sampled trace store. Keep your tracing stack for latency and debugging — use the tamper-evident audit trail for the forensic questions it isn't built to answer.

## Frequently asked questions

### Can I filter the trail by tenant if two tenants share a client_id?

Yes — that's the case it's built for. The slice keys on `identity.tenant_id`, which the auth layer populates from the caller's credential, not on `client_id`. Two tenants presenting the identical `client_id` string (a role-based service identity deployed into each workspace) produce entries you can separate cleanly, as the runnable example shows: three calls, two of them Acme's, filtered exactly.

### Where does the tenant_id in each entry come from?

From the verified credential, never from tool arguments. With `JWTAuth`, `AuthMiddleware` reads it from a configurable claim (`tenant_claim="org_id"`, `"tenant_id"`, whatever your IdP emits) into `ctx.client.tenant_id`; with `APIKeyAuth` it comes from the key's config dict. Only string claim values are accepted, so a malformed claim leaves the tenant unset rather than logging a wrong one.

### How does a regulator verify the per-tenant slice I hand over?

`verify_chain()` recomputes every entry's HMAC and confirms each `prev_hash` links to its predecessor, using only the entries plus the shared secret. Any edit, deletion, or reorder returns `False`. Set `PROMPTISE_AUDIT_SECRET` to a stable value so the chain remains verifiable across restarts and by whoever receives the export.

### Does logging arguments risk leaking PII into the audit trail?

`include_args` and `include_result` both default to `False` precisely because tool inputs and outputs may carry PII. The tenant, tool name, acting identity, status, and timing are recorded regardless, which is enough for "who did what, for which customer, when." Enable argument capture only for tools whose inputs you've confirmed are safe to retain.

## Next steps

If your audit trail keys on `client_id` alone, a shared service identity is already blending your tenants — fix the slice, not the query. Add `AuditMiddleware` with a stable `hmac_secret`, point `AuthMiddleware` at your tenant claim, and you get a tamper-evident, tenant-stamped JSONL trail you can filter per customer out of the box. Assert the Acme slice is exact and `verify_chain()` is `True` in a unit test, then read the [Build a Secure Multi-Tenant Agent Platform](../../guides/secure-multi-tenant-platform.md) guide to wire the same tenant identity through auth, tool guards, and rate limits, and use the [multi-tenancy reference](../../mcp/server/multi-tenancy.md) to confirm every surface it isolates. The same tenant value should partition your data too — [Multi-Tenant RAG: Isolate Customer Data in a Shared Store](multi-tenant-rag.md) covers the retrieval side of the same invariant.
