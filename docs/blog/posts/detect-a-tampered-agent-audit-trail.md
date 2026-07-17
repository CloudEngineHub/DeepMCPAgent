---
title: "How to Detect a Tampered AI Agent Audit Trail"
description: "The operational verification workflow, not the theory. A plain JSONL or stdout log can be edited or truncated with no trace. Walks through exactly what…"
keywords: "detect a tampered agent audit trail, verify audit log integrity, verify_chain audit check, prove an agent log wasn't edited, hmac chain tamper detection"
date: 2026-07-16
slug: detect-a-tampered-agent-audit-trail
categories:
  - Compliance & Audit
---

# How to Detect a Tampered AI Agent Audit Trail

To **detect a tampered agent audit trail** you need one property a plain JSONL file or stdout stream can never give you on its own: a way to prove, after the fact, that no record was edited, deleted, or reordered. A log your agent appends to is just bytes on disk. Anyone with write access — a compromised process, a careless migration script, an insider covering their tracks — can rewrite a line, drop an entry, or truncate the file, and nothing about the surviving data reveals the change. This post is the operational verification workflow, not the theory: exactly what Promptise's `verify_chain()` checks, how the break localizes to the specific entry an insert, edit, or delete touched, and how to wire the check into CI, a periodic integrity job, and an incident-response runbook so you can prove a clean chain on demand.

If you are still deciding whether you even need this — as opposed to the trace exports you already have — read [Why AI Agent Traces Aren't an Audit Trail (or SOC 2 Proof)](ai-agent-observability-vs-audit-trail.md) first. This post assumes you've decided the log is evidence and now need to verify it.

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## What a tampered audit trail looks like

Start with the failure mode, because it's invisible by construction. Say your agent writes one line per privileged tool call to `audit.jsonl`:

```json
{"timestamp": 1721000000.0, "tool": "grant_admin", "client_id": "ops-agent", "user_id": "u-2002"}
{"timestamp": 1721000005.0, "tool": "delete_records", "client_id": "ops-agent", "table": "invoices"}
```

Three ways this record gets falsified, none of which leaves a mark in a plain file:

- **Edit** — someone changes `"grant_admin"` to `"get_status"` on line 1 to hide who was escalated. Same line count, same structure, plausible content.
- **Delete** — someone removes the `delete_records` line entirely so the destructive action never happened, as far as the file is concerned.
- **Insert / reorder** — someone injects a fabricated entry, or swaps two lines so the timeline no longer matches reality.

A byte-for-byte diff only catches these if you kept a trusted copy of the original — which is exactly what you don't have during an incident. The record itself has to carry the proof. That's what a hash chain does.

## What `verify_chain()` actually checks

Promptise's [`AuditMiddleware`](../../mcp/server/observability.md) writes one JSON line per tool call, and when `signed=True` (the default) each entry carries two extra fields that turn the file into a chain:

- `prev_hash` — the `hmac` of the entry immediately before it. The first entry links to a fixed **genesis hash** of 64 zeros.
- `hmac` — a keyed HMAC-SHA256 computed over *this* entry's fields (including its `prev_hash`), using a secret only you hold.

Because each entry commits to the one before it, the entries form a tamper-evident chain rather than an independent pile of lines. `verify_chain()` walks that chain from the genesis hash and performs two checks on every entry:

1. **Linkage** — does this entry's `prev_hash` equal the previous entry's `hmac`? If not, an entry was deleted, inserted, or reordered here.
2. **Recomputation** — does the entry's `hmac` recompute to the stored value from its current fields and the secret? If not, a field in this entry was edited.

It returns a plain `bool`: `True` for an intact chain, `False` the moment either check fails. That boolean is the whole point — it's a primitive you can assert. Because the HMAC uses a secret key, an attacker who edits the file *cannot* forge a valid chain without also holding the key, so silently "repairing" the chain after a tamper isn't available to them.

One detail matters for compliance: when the caller authenticated through a JWT or JWKS provider (see [authentication & security](../../mcp/server/auth-security.md)), each entry also carries a verified `identity` block — `subject`, `issuer`, `audience`, `roles`, `tenant_id` — *inside* the hashed payload. So the answer to "which principal did this" is itself tamper-evident, not a mutable string you have to take on faith.

## Detect an edit, a delete, and an insert

Here is the verification workflow end to end. It stands up a server, records three privileged calls through the real middleware pipeline, verifies the intact chain, then reproduces each of the three tamper classes and shows `verify_chain()` catching every one. It runs in-process with `TestClient` — no network, no LLM key:

```python
# detect_tamper.py — prove an agent audit trail wasn't edited
import asyncio
import copy
from promptise.mcp.server import MCPServer, AuditMiddleware, TestClient

server = MCPServer(name="admin-api")

audit = AuditMiddleware(
    log_path="audit.jsonl",
    signed=True,                       # HMAC-chain every entry
    hmac_secret="rotate-me-in-prod",   # in prod: set PROMPTISE_AUDIT_SECRET
)
server.add_middleware(audit)


@server.tool()
async def grant_admin(user_id: str) -> dict:
    """Escalate a user to admin — the kind of action an auditor scrutinizes."""
    return {"user_id": user_id, "role": "admin"}


@server.tool()
async def delete_records(table: str) -> dict:
    """Irreversible bulk delete."""
    return {"table": table, "deleted": True}


def first_broken_link(entries: list[dict]) -> int | None:
    """Localize an insert/delete/reorder using only the chain fields.

    No HMAC secret needed: each entry's ``prev_hash`` must equal the
    previous entry's ``hmac``. Returns the index of the first entry whose
    linkage is wrong, or ``None`` if the structure is intact.
    """
    prev = "0" * 64  # genesis hash
    for i, entry in enumerate(entries):
        if entry.get("prev_hash") != prev:
            return i
        prev = entry.get("hmac")
    return None


async def main() -> None:
    client = TestClient(server)
    await client.call_tool("grant_admin", {"user_id": "u-1001"})
    await client.call_tool("delete_records", {"table": "invoices"})
    await client.call_tool("grant_admin", {"user_id": "u-2002"})

    clean = copy.deepcopy(audit.entries)
    print("intact:", audit.verify_chain())                 # True

    # (1) EDIT — rewrite a field to hide what ran.
    audit.entries[0]["tool"] = "get_status"
    print("after edit:", audit.verify_chain())             # False
    print("  structural scan:", first_broken_link(audit.entries))  # None
    audit.entries[:] = copy.deepcopy(clean)

    # (2) DELETE — drop an entry to erase an action.
    del audit.entries[1]
    print("after delete:", audit.verify_chain())           # False
    print("  broken link at index:", first_broken_link(audit.entries))  # 1
    audit.entries[:] = copy.deepcopy(clean)

    # (3) INSERT / REORDER — move entries around.
    audit.entries[0], audit.entries[1] = audit.entries[1], audit.entries[0]
    print("after reorder:", audit.verify_chain())          # False
    print("  broken link at index:", first_broken_link(audit.entries))  # 0
    audit.entries[:] = copy.deepcopy(clean)

    print("restored:", audit.verify_chain())               # True


asyncio.run(main())
```

Running it prints `True`, then `False` for each of the three tampers, then `True` again once the original bytes are restored — proving the check is deterministic and reversible, not a one-way alarm you can't reset. The `hmac_secret` resolves from the constructor argument, then `PROMPTISE_AUDIT_SECRET`, and only falls back to a random per-process secret (with a warning) if neither is set. In production, load it from your secrets manager so the same key verifies logs from every instance and across restarts.

## Pinpoint the broken link

`verify_chain()` answers *whether* the trail is intact. During an incident you also want to know *where* it broke — which is where the two-part check pays off, because the two tamper families surface differently:

- A **delete, insert, or reorder** breaks the `prev_hash` **linkage**. You can localize it without the secret at all — walk the entries and find the first one whose `prev_hash` doesn't equal the prior entry's `hmac`. That's the `first_broken_link()` helper above, and it reads only the two documented chain fields. In the run it returns index `1` for the deleted entry and `0` for the reorder.
- A **content edit** leaves the linkage intact (the edited entry's stored `hmac` and its neighbor's `prev_hash` are untouched), so the structural scan returns `None`. Only the **recomputation** check — which requires the secret — catches it, and that's exactly what `verify_chain()` did when it returned `False` on the edit while the structural scan stayed clean.

That split is useful in practice: anyone can run the secret-free structural scan to prove entries weren't dropped or shuffled, while only a holder of the audit secret can prove no field was rewritten. Once you have the index of the first broken link, you cross-reference the surrounding entries — each carries `request_id`, `timestamp`, `tool`, `client_id`, and the verified `identity` block — against your source of truth to reconstruct what was changed and by whom.

## Wire `verify_chain()` into CI, a cron job, and your incident runbook

A boolean is only evidence if something runs it on a schedule. Three places to wire it in:

**CI assertion.** Drive your privileged tools through `TestClient` in a test and assert the chain, so a regression that breaks signing fails the build:

```python
import pytest
from promptise.mcp.server import TestClient

@pytest.mark.asyncio
async def test_audit_chain_is_intact(server_with_audit):
    server, audit = server_with_audit
    client = TestClient(server)
    await client.call_tool("grant_admin", {"user_id": "u-1"})
    await client.call_tool("delete_records", {"table": "t"})
    assert audit.verify_chain() is True
```

**Periodic integrity job.** On a long-running server the middleware accumulates the chain in memory, so a background task can re-verify it on an interval and alarm the instant it breaks — the documented handle is the middleware instance itself:

```python
import asyncio

async def integrity_watchdog(audit, interval_s: int = 300) -> None:
    while True:
        if not audit.verify_chain():
            # page on-call: the in-memory audit chain no longer verifies
            raise RuntimeError("AUDIT CHAIN BROKEN — escalate immediately")
        await asyncio.sleep(interval_s)
```

**Incident-response runbook.** When a tamper alarm fires, or an auditor asks you to demonstrate integrity, the steps are mechanical: (1) run `verify_chain()` and record the boolean with a timestamp; (2) if it's `False`, run the secret-free structural scan to get the first broken index; (3) inspect the entries around that index — their `request_id`, `identity.subject`, and `timestamp` — to scope what was altered; (4) compare against your durable, write-once copy of `audit.jsonl` (ship it to append-only or WORM storage so the on-disk evidence is independently defensible); (5) rotate the audit secret if you suspect key compromise. The point is that every step produces a concrete artifact you can hand an auditor, instead of a verbal assurance.

## What other frameworks do today

To be fair about the landscape: this isn't "other tools don't log." They log well. The gap is a built-in *integrity proof over the record* and a single command to check it.

**OpenTelemetry** models a tool call as a span with attributes and timing, built to debug latency and errors across services. Spans are mutable in the SDK before export and then land in whatever backend you point them at, retained and sampled on your policy. The spec defines no per-span signature and no hash linking one span to the next — there's no integrity mechanism to verify, by design. Spans *do* carry attribution-like attributes (client id, tool name); what's absent is any cryptographic guarantee the stored span matches what happened.

**LangSmith** is a hosted tracing and evaluation platform; its runs can be created, updated, and deleted through its API and UI, which is exactly right for an evaluation workflow. LangChain publishes a SOC 2 report for the service. As shipped, it doesn't advertise a hash-chained, tamper-evident audit primitive you run against your own record — its value is queryable telemetry. **Pydantic Logfire** sits in the same category: rich, queryable OpenTelemetry-based debugging telemetry, not a per-record integrity check.

The delta is consistent, and I'll state it as a capability rather than an accusation: to the best of what these tools publish, none ships a keyed hash chain over each record plus a single `verify_chain()`-style boolean you can assert in CI or run during an incident. You can build integrity on top of them, but you're then hoping your own pipeline and backend enforce it. Promptise's edge is that it makes verifiable integrity a **structural, first-class property of the record itself** — the chain is written as each entry is appended, and the verify step is one method call. That's the difference between "our backend has good controls" and "here is the record, verify it yourself." For how one such trail satisfies several regimes at once, see [One Audit Trail for SOC 2, HIPAA and the EU AI Act](ai-agent-compliance-audit-trail.md).

## Frequently asked questions

### How do I verify audit log integrity without a trusted original copy?

That's the whole reason for the hash chain. To **verify audit log integrity** you don't diff against a saved original — you call `verify_chain()`, which recomputes each entry's HMAC and checks its linkage to the previous entry. The proof travels inside the record, so you can verify a file you were handed cold, with only the secret key.

### What does the `verify_chain` audit check actually return?

A plain boolean. The `verify_chain` audit check returns `True` for an intact chain and `False` the moment any entry's `prev_hash` linkage or recomputed HMAC fails. Because it's a boolean, you assert it in CI, poll it from a background watchdog, or run it live during an incident — the same primitive in all three places.

### How do I prove an agent log wasn't edited?

Enable signing (`AuditMiddleware(signed=True)`) with a managed `PROMPTISE_AUDIT_SECRET`, then call `verify_chain()`. To **prove an agent log wasn't edited**, a `True` result shows every field of every entry recomputes to its stored HMAC; a `False` result means a field changed, and the structural scan tells you whether entries were also dropped or reordered.

### How does HMAC chain tamper detection localize the break?

The two checks split the work. **HMAC chain tamper detection** catches content edits via recomputation (needs the secret), while inserts, deletes, and reorders break the `prev_hash` linkage and can be localized to the first offending index with no secret at all — walk the entries until one's `prev_hash` doesn't match the prior entry's `hmac`.

## Next steps

Add `AuditMiddleware(signed=True)` to your MCP server, set `PROMPTISE_AUDIT_SECRET` from your secrets manager, and drop `verify_chain()` into a CI assertion and a periodic watchdog today — that's the difference between "the log says so" and "verify it yourself." The [MCP server observability](../../mcp/server/observability.md) page covers configuration, the exact entry schema, and middleware ordering; the [authentication & security](../../mcp/server/auth-security.md) page shows how a verified JWT/JWKS identity lands inside the chain so *which agent did what* is tamper-evident too. Then wire the check into your audit-review runbook so you can prove a clean chain on demand — no trusted original required.
