---
title: "LangGraph Checkpoints vs. an Audit Log: The Real Difference"
description: "LangGraph checkpointing is often mistaken for an audit trail. It persists graph state to resume, replay and support human-in-the-loop, but it is not an…"
keywords: "langgraph checkpoint vs audit log, langgraph checkpointing for compliance, durable state vs audit trail, is a checkpoint an audit log, langgraph persistence compliance"
date: 2026-07-16
slug: langgraph-checkpoint-vs-audit-log
categories:
  - Compliance & Audit
---

# LangGraph Checkpoints vs. an Audit Log: The Real Difference

The **langgraph checkpoint vs audit log** confusion is one of the most expensive category errors in agent engineering: teams wire up a checkpointer, watch their graph resume cleanly after a crash, and quietly assume that same durable state will satisfy an auditor asking what the agent did and who authorized it. It won't. A checkpoint is a mutable snapshot of graph state built to *resume, replay, and pause for a human*. An audit log is an integrity-chained, attributable record built to *prove what happened*. Both are legitimate; they solve different problems, and using one where you need the other fails at the worst possible moment. This post draws the precise line, states exactly what LangGraph's checkpointers do and don't guarantee, and shows the one primitive a checkpoint can't give you.

<!-- more -->

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## Two jobs that look identical and aren't

Checkpoints and audit trails both "write down what happened," which is why they get conflated. But the questions they answer are different, and that difference decides whether either survives scrutiny.

A checkpoint answers an operational question: *how do I get this run back?* After a process dies, a network blip, or a human-in-the-loop pause, you want to reload the exact state and continue — or rewind to an earlier point and fork a new branch. That job demands durability and mutability: you overwrite state each super-step, you edit it to inject a human decision, you fork from a past point to try something else.

An audit log answers a narrower, adversarial question: *for this sensitive action, which principal performed it, and can you prove the record wasn't edited afterward?* When an agent releases funds, deletes records, or escalates a role, the log itself becomes the disputed artifact. "The state says so" collapses the instant the state is the thing under question — because anyone with write access can change it and nothing in the surviving data reveals the edit.

That is the heart of the **durable state vs audit trail** distinction. The defining property of a checkpoint is *recoverability*. The defining property of an audit trail is *integrity*. Optimizing for one does not give you the other for free.

## What LangGraph checkpointing actually does today

To be fair and precise about the competition: LangGraph checkpointing is excellent at the job it was designed for. A checkpointer (`MemorySaver`, `SqliteSaver`, `PostgresSaver`, or the standalone `langgraph-checkpoint-*` packages implementing `BaseCheckpointSaver`) persists a snapshot of the graph's channel state at each super-step, keyed by a `thread_id` and a `checkpoint_id` you pass through `{"configurable": {"thread_id": ...}}`. That single mechanism powers a lot:

- **Durable memory** across turns and sessions, so a conversation survives a restart.
- **Crash-safe resume** — reload the last checkpoint and continue from where the graph stopped.
- **Human-in-the-loop** — interrupt the graph, let a person edit state via `update_state`, then resume.
- **Time-travel** — `get_state_history` yields `StateSnapshot`s, and you can replay or fork from any past `checkpoint_id`.

It even carries provenance. Checkpoint metadata records a `source` (`input`, `loop`, `update`, `fork`), a `step` index, and a `writes` map of node name → the state update that node produced. So it is *not* true that LangGraph records nothing about how state changed — it records which **graph node** wrote which values at which step.

Here is the exact delta, stated honestly. That provenance is node-level, not a verified security principal: the checkpointer stamps which node ran, not an authenticated end-user or tenant. Any end-user or tenant identity appears only if *you* thread it into state yourself — and once it's in the snapshot it is unverified and just as mutable as everything else. And crucially, as shipped, LangGraph's checkpointers do not provide a per-checkpoint signature, a hash linking one checkpoint to the next, tamper detection, or a `verify` primitive. Mutability is deliberate — `update_state` forks and edits state, and the underlying SQLite or Postgres rows can be updated directly — because that is precisely what time-travel and HITL require. None of that is a criticism of checkpointing. It is the honest boundary of what a checkpoint is *for*.

## Is a checkpoint an audit log? The exact delta

So, **is a checkpoint an audit log**? No — and the reason is structural, not a missing config flag. Line the two up on the properties an auditor actually cares about:

| Property | LangGraph checkpoint | Tamper-evident audit log |
|---|---|---|
| Primary purpose | Resume, replay, time-travel, HITL | Prove what happened |
| Mutability | Mutable by design (`update_state`, direct store edits) | Append-only; edits are *detectable* |
| Keyed to | A `thread_id` / `checkpoint_id` | An action, with a verified principal |
| Provenance | Which graph **node** wrote state | Which authenticated **principal** acted |
| Integrity | No signature, no hash chain | Per-entry HMAC over the entry + prior hash |
| Tamper detection | None in the framework | A single `verify` pass returns a boolean |
| Handed to an auditor | "Trust the state" | "Verify it yourself" |

The distinction that matters most for **langgraph checkpointing for compliance** is the last three rows. A checkpoint lets you *reconstruct* state; it does not let you *prove* that a specific state, or a specific action, is byte-for-byte what occurred and was caused by a specific identity. That is exactly the property compliance evidence is made of — and exactly the property a snapshot store was never built to provide. The same gap shows up in observability tooling, for the same reason, as [Why AI Agent Traces Aren't an Audit Trail (or SOC 2 Proof)](ai-agent-observability-vs-audit-trail.md) lays out: rich, mutable, queryable data is not the same as verifiable evidence.

## The primitive a checkpoint can't give you: a verifiable hash chain

A tamper-evident audit log doesn't try to *prevent* edits — write-once storage and file permissions do that. It makes edits *detectable*. Promptise's `AuditMiddleware` does this by writing one JSON line per tool call in which every entry carries a keyed hash (HMAC-SHA256) computed over its own fields **plus the hash of the entry before it**. The chain starts from a fixed genesis hash (64 zeros), and each entry commits to the entire history preceding it via a `prev_hash` field and its own `hmac`.

That chaining is the whole point, and it is the structural inverse of a mutable snapshot:

- Edit any field in any entry and its HMAC no longer matches — the chain breaks at that entry.
- Delete an entry and the next entry's `prev_hash` no longer lines up.
- Reorder two entries and the linkage is wrong from the swap onward.

Because the HMAC uses a secret key, an attacker can't recompute a valid chain without it. And because verification is a single deterministic pass, anyone with the secret runs `verify_chain()` and gets a plain boolean back — the exact primitive a mutable checkpoint cannot offer. When the caller authenticated with a JWT or JWKS provider, each entry also carries a verified `identity` block (`subject`, `issuer`, `audience`, `roles`, `tenant_id`) *inside* the chain, so "which principal did what" is both attributable and tamper-evident — not a node name in editable metadata. The full behavior, configuration, and recommended middleware ordering live on the [MCP server observability](../../mcp/server/observability.md) page. Promptise's edge here isn't "LangGraph lacks persistence" — it obviously has excellent persistence. It's that Promptise makes verifiable integrity and verified attribution a **first-class, structural property of the record itself**, rather than something you hope your state store enforces.

## Turn the record into evidence in one line

Here is the difference made concrete. `AuditMiddleware` is a standard MCP server middleware: add it to the chain and every tool call becomes one signed JSON line. The example stands up a workflow server, records two privileged disbursements, verifies the chain, simulates someone rewriting the record to hide what ran, then restores the exact original value to show the check is deterministic.

Every API here is real, and it runs in-process with `TestClient` — no network and no LLM key required:

```python
# checkpoint_vs_audit.py — durable state resumes; a signed record proves what happened
import asyncio
from promptise.mcp.server import MCPServer, AuditMiddleware, TestClient

server = MCPServer(name="loan-workflow")

# One HMAC-chained JSON line per tool call. Each entry commits to the one before it.
audit = AuditMiddleware(
    log_path="workflow-audit.jsonl",
    signed=True,                       # chain each entry to its predecessor
    hmac_secret="rotate-me-in-prod",   # in prod: set PROMPTISE_AUDIT_SECRET
)
server.add_middleware(audit)


@server.tool()
async def approve_disbursement(loan_id: str, amount: int) -> dict:
    """Release funds — the action a regulator will ask you to prove."""
    return {"loan_id": loan_id, "amount": amount, "state": "disbursed"}


async def main():
    client = TestClient(server)
    await client.call_tool("approve_disbursement", {"loan_id": "L-77", "amount": 50000})
    await client.call_tool("approve_disbursement", {"loan_id": "L-88", "amount": 12000})

    # A checkpoint can be edited in the store; this record proves it wasn't.
    print("chain valid:", audit.verify_chain())        # True

    # Someone rewrites history to change which loan was approved.
    original = audit.entries[0]["tool"]
    audit.entries[0]["tool"] = "get_status"
    print("chain valid:", audit.verify_chain())        # False — tamper detected

    # Restore the exact original value and the chain verifies again.
    audit.entries[0]["tool"] = original
    print("chain valid:", audit.verify_chain())        # True


asyncio.run(main())
```

The `hmac_secret` resolves in a defined order: the constructor argument first, then the `PROMPTISE_AUDIT_SECRET` environment variable, and only if neither is set does it fall back to a random per-process secret (with a warning, because that can't verify the chain across restarts). In production, load it from your secrets manager so the same key verifies logs from every instance. Because `verify_chain()` returns a boolean, you can assert it in CI or wire it into a periodic job that alarms the instant integrity breaks — something no editable checkpoint can give you.

## You need both — durable state and a verifiable record

This isn't checkpoints *or* audit. A serious agent platform runs both, because they serve different masters — and Promptise ships both sides deliberately.

Keep durable state for what it's excellent at: crash-safe resume, human-in-the-loop pauses, conversational memory, and time-travel debugging. Promptise's own runtime offers the direct analog of checkpointing — journal backends with a replay engine and a rewind engine — so an agent process can recover from its last known-good state after a crash. There the properties you want are durability and mutability, exactly as with a LangGraph checkpointer.

Add a tamper-evident audit trail specifically where the log is *evidence*: disbursements, deletions, privilege changes, anything a regulator or incident responder will scrutinize. There the properties invert — you want completeness, verified attribution, and an integrity proof, not editable snapshots. Turning one on is a single `add_middleware` call, and it captures every call, including ones later rejected by auth or a guard. Alongside it, keep your debugging telemetry — the transporters on the [core observability](../../core/observability.md) page (HTML, structured logs, Prometheus, OTLP) are for profiling and evals, which is a third, mutable-by-design layer.

The mistake behind most **langgraph persistence compliance** questions is asking a recovery primitive to serve as an evidence primitive. Draw the line deliberately and you get all three: fast iteration from observability, crash-safe continuity from durable state, and a record you can defend from an audit trail.

## Frequently asked questions

### Is a checkpoint an audit log?

No. A LangGraph checkpoint is a mutable snapshot of graph state keyed to a thread, built to resume, replay, and support human-in-the-loop. It records which graph node wrote which values, but it has no per-entry signature, no hash chain, no tamper detection, and no verified end-user or tenant attribution. An audit log inverts all of those: append-only, integrity-chained, attributed to an authenticated principal, and verifiable with a single deterministic pass.

### Can I use LangGraph checkpointing for compliance evidence?

Not on its own. As shipped, checkpointers persist state for recovery and time-travel — the snapshots are editable via `update_state` and directly in the SQLite/Postgres store, with no cryptographic linkage between them. You can't hand an auditor a checkpoint row and prove it's byte-for-byte what happened, or that a specific principal caused it. For that you add a tamper-evident audit trail alongside the checkpointer.

### What's the difference between durable state and an audit trail?

Durable state exists to be reloaded and rewound, so it's mutable by design and keyed to a run or thread. An audit trail exists to be proven, so it's append-only, chained by an HMAC so any edit, deletion, or reorder is detectable, and attributed to a verified identity. Same underlying events, opposite guarantees — which is why you keep both.

### How does Promptise make an agent log tamper-evident?

Use `AuditMiddleware(signed=True)` with a managed `PROMPTISE_AUDIT_SECRET`, then call `verify_chain()`. Each entry's HMAC covers its own fields plus the previous entry's hash, so verification is a single deterministic pass that returns `True` for an intact chain and `False` — pointing at the break — if any entry was touched. When a JWT or JWKS provider authenticated the caller, the verified `identity` block is chained inside each entry too.

## Next steps

Add `AuditMiddleware` to your MCP server and run `verify_chain()` — it's one `add_middleware` call to move a sensitive action from "the checkpoint says so" to "verify it yourself," and it sits happily alongside whatever checkpointer or journal you already use for recovery. The [MCP server observability](../../mcp/server/observability.md) page shows the configuration and middleware ordering, and the [core observability](../../core/observability.md) page covers the mutable traces you'll keep for debugging. To see how one verifiable trail feeds SOC 2, HIPAA, and the EU AI Act at once, read [One Audit Trail for SOC 2, HIPAA and the EU AI Act](ai-agent-compliance-audit-trail.md).
