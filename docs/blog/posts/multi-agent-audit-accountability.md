---
title: "Multi-Agent Audit: Who's the Principal After a Delegation Hop?"
description: "Not about how to authenticate a delegation call — that's a separate topic — but about the one compliance question it raises: after Agent A hands work to…"
keywords: "multi-agent audit accountability, who is accountable in a multi-agent audit log, originating user identity across agent delegation, delegated_by audit attribution, audit principal after agent handoff"
date: 2026-07-16
slug: multi-agent-audit-accountability
categories:
  - Compliance & Audit
---

# Multi-Agent Audit: Who's the Principal After a Delegation Hop?

Multi-agent audit accountability is the question a compliance reviewer asks the moment your fleet stops being one agent: when Agent A — running on Alice's behalf — hands a subtask to Agent B, and B exports a record or issues a refund, *which principal does the audit trail name as accountable?* This is not the question of how B authenticates the delegation call — that is a separate topic with its own answer. It is the narrower, sharper compliance question that delegation raises: after the handoff, does your tamper-evident record still name **the human who set the chain in motion**, or does it name the sub-agent's service account? Get that wrong and every audited action taken past the first hop is attributed to a bot, and "the bot did it" is not an answer a SOC 2, HIPAA, or EU AI Act auditor accepts.

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## The delegation hop that erases the human

Picture a support fleet. Alice, a user in tenant `acme`, asks a `support-orchestrator` to "pull my recent records and summarize them." The orchestrator delegates the retrieval to a `researcher` peer. Two agents now touch Alice's data, but only one of them was invoked by Alice.

Everything upstream of the delegation is easy to attribute correctly, because the request arrived with identity attached — a token, a session, a `user_id`. The failure mode is the *sub-call*. When the orchestrator delegates to the researcher, that peer is a fresh invocation. If nothing carries Alice's principal into it, the peer's audited actions record the sensible-but-wrong actor: the peer itself, or a shared service identity every agent presents. The log faithfully says `export_customer_records` ran. It does not say it ran **on Alice's behalf**.

That is the accountability gap. It is distinct from data isolation (does the peer read the *wrong* user's data — covered in [Does the user's identity survive agent delegation?](propagate-user-identity-across-agent-delegation.md)) and distinct from actor attribution (which *agent* ran — covered in [Which AI agent did this?](ai-agent-action-attribution.md)). This post is about the third thing: **chain of custody**. When work flows human → agent → agent → tool, a compliance-grade record has to reconstruct the whole line — who is accountable, and who relayed the work to get there. None of it shows up in a one-user demo. It shows up the first time an auditor points at a downstream action and asks "on whose authority?"

## What other frameworks do today

It is worth being precise and fair, because every mature framework *can* move context between agents. The gap is narrower than "they can't do it."

- **LangGraph** reliably threads its `RunnableConfig` — including the `configurable` dict where teams stash `user_id` and `thread_id` — into nested-graph and subgraph calls. That propagation is real. What it does not do is treat that `user_id` as a *principal* that lands on an audit record as the accountable actor. The checkpointer keys on `thread_id`; if you want the originating human on a downstream log line, you read `config["configurable"]["user_id"]` and write it there yourself.
- **CrewAI** hands work between agents through delegation tools (the "Delegate work to coworker" tool and the hierarchical manager). That is genuine agent-to-agent handoff, and its verbose logs record an actor — but the actor is the delegated agent's *role name*, a string the crew assigns to itself, and CrewAI's memory is scoped to the crew rather than to an end user.
- **AutoGen** routes messages between agents through group chats and, in the 0.4 line, a message-routing runtime. Real inter-agent communication — but the thing routed is a conversation message, not a per-user principal that automatically appears as the accountable party on each downstream action.

So the precise delta is this: LangGraph threads config, CrewAI and AutoGen pass messages, and **none of them defines an originating-human principal that, on its own, rides through the delegation and lands as the accountable actor on every downstream audited action — nor stamps a structured marker for which agent relayed the work.** Keeping the human accountable across the hop, and reconstructing the custody chain, is thread-it-yourself on every call; miss one and the record names the bot. That is a design choice, not a bug. Promptise makes the opposite choice: the human principal is a first-class, ambient object that inherits into the peer, and the delegating agent is stamped structurally — so accountability across a hop is the default, not a thing you remember to wire.

## Two identities every audited hop must keep

A compliance-grade delegation record has to carry two different identities, and Promptise carries both automatically.

**The accountable principal — the originating human.** In Promptise the human principal is a [`CallerContext`](../../core/observability.md) you attach to an invocation with `caller=`. The key behavior for delegation: when a peer is invoked *without* an explicit caller, `ainvoke` inherits the **ambient** `CallerContext` the orchestrator already set, rather than overwriting it with `None`. Delegation happens in-process, so that context is still live — the peer runs as Alice. And every timeline entry the peer records auto-attaches the caller's `user_id`; there is nothing to pass on the sub-call. That auto-attribution — the collector reading the caller from a contextvar at record time — is the same mechanism documented in [Observability → Multi-User Attribution](../../core/observability.md).

**The chain of custody — who relayed the work.** Preserving the human answers "on whose behalf?" but not "who caused this?" So Promptise carries a second identity on an independent channel: when the orchestrator delegates via `ask_peer` or `broadcast`, the delegating agent's identity claims ride along, and every entry the peer records during that run is stamped with **`delegated_by`** — descriptors only, never a credential. An auditor reading the researcher's timeline then sees both facts on the same line: it acted for `u-alice` (the accountable principal) and it was *caused* by `support-orchestrator` (the delegator). Message-passing between anonymous peers can't reconstruct that after the fact; a stamped `delegated_by` can.

## See it: the audit record after a hop

Here is the whole thing in one runnable file. It stands up a collector, sets the ambient `CallerContext` (what `ainvoke(caller=...)` does — and inherits into the peer) and the delegation marker (what the `ask_peer`/`broadcast` tool sets around the peer's run), then records a peer action **without passing any identity fields**. Both the accountable principal and the custody marker land on the entry on their own. Every API is real; it runs in-process with no LLM key and no network.

```python
# delegation_audit.py — the audit record a peer emits, after a delegation hop.
from promptise import CallerContext
from promptise.agent import _caller_ctx_var
from promptise.observability import (
    ObservabilityCollector, TimelineEventType, _delegation_ctx_var,
)

audit = ObservabilityCollector("support-fleet")

# 1. The request arrives as Alice (tenant acme). This is exactly what
#    build_agent's ainvoke(caller=...) sets — and it INHERITS into the peer,
#    so a delegation sub-call runs as Alice without threading her down by hand.
alice = CallerContext(user_id="u-alice", tenant_id="acme")
caller_token = _caller_ctx_var.set(alice)

# 2. The orchestrator delegates to a peer. This is what the ask_peer /
#    broadcast tool sets around the peer's run.
deleg_token = _delegation_ctx_var.set({"agent_id": "support-orchestrator"})

# 3. The peer acts. Note: NO user_id and NO delegated_by are passed here —
#    the collector fills both from the ambient context at record time.
audit.record(
    TimelineEventType.TOOL_CALL,
    agent_id="researcher",
    details="export_customer_records(customer_id=C-88)",
    metadata={"tool_name": "export_customer_records"},
)

_delegation_ctx_var.reset(deleg_token)
_caller_ctx_var.reset(caller_token)

entry = audit.get_timeline()[-1].to_dict()
print("accountable principal (user_id):", entry["user_id"])
print("acting agent (agent_id):        ", entry["agent_id"])
print("chain of custody (delegated_by):", entry["metadata"]["delegated_by"])

# Reconstruct every audited action accountable to one human principal:
print("actions attributed to u-alice: ", len(audit.for_user("u-alice")))
```

Running it prints the accountable record, unambiguous three ways:

```text
accountable principal (user_id): u-alice
acting agent (agent_id):         researcher
chain of custody (delegated_by): {'agent_id': 'support-orchestrator'}
actions attributed to u-alice:  1
```

The `researcher` did the work, but the record is accountable to `u-alice`, and it names `support-orchestrator` as the agent that relayed it. That is the custody chain, on a single line, with no identity parameter written on the delegation call. And because every entry keys on the human, `audit.for_user("u-alice")` (or the broader `audit.query(...)` and `audit.purge_user(...)`) reconstructs — or erases, for a GDPR request — the complete set of actions taken on one person's behalf across every agent in the fleet. Note the trust boundary honestly: the timeline stamps the caller's `user_id`; the `tenant_id` travels on the `CallerContext` and lands on the *server-side* audit's verified `identity` block, not on this in-process timeline field.

## Where the accountable record becomes evidence

An accountability record is only *evidence* if it can't be quietly rewritten after the fact. The in-process timeline above answers "on whose behalf, relayed by whom" during the run; the durable, tamper-evident half lives at the MCP boundary. When a tool server verifies the caller's token and runs `AuditMiddleware(signed=True)`, the *verified* principal (`subject`, `issuer`, `audience`, `roles`, and `tenant_id`) lands as a first-class `identity` block inside an HMAC-chained log — each entry hashing the one before it, so editing `subject` or reordering entries breaks the chain at that point. The full mechanism, including `verify_chain()`, is in [Authentication & Security](../../mcp/server/auth-security.md).

Put the two together and multi-agent audit accountability stops being a convention you hope every hop honored: the human principal inherits into each peer and stamps every action, `delegated_by` records who relayed it, and the verified server-side entry makes the whole thing tamper-evident. That is why a debug trace is not the same as an audit trail — a distinction we draw out in [Why AI Agent Traces Aren't an Audit Trail (or SOC 2 Proof)](ai-agent-observability-vs-audit-trail.md) — and why one accountable, tamper-evident record can satisfy several regimes at once, as in [One Audit Trail for SOC 2, HIPAA and the EU AI Act](ai-agent-compliance-audit-trail.md).

## Frequently asked questions

### After Agent A delegates to Agent B, who does the audit name as accountable?

The originating human. When A is invoked with `caller=CallerContext(user_id="u-alice", ...)`, that principal is held in an async-safe contextvar, and a peer invoked through `ask_peer`/`broadcast` without its own caller inherits it. So every action B records auto-attaches `u-alice` as the accountable `user_id` — not B's service account — with no identity argument on the delegation call.

### What is `delegated_by`, and does it forward a credential?

`delegated_by` is stamped on the peer's timeline with the delegating agent's identity *claims* — descriptors only, never a token or secret. It answers "who relayed this work?" independently of the human principal, so a single audited line carries both the accountable end-user and the agent that caused the action, letting you reconstruct the full custody chain human → agent → agent → tool.

### Isn't this the same as propagating user identity across delegation?

They share a mechanism but answer different questions. Identity *propagation* is about data isolation — the peer's memory and cache scoping to the right user so it never reads another tenant's data. Accountability is about the *record* — which principal a downstream audited action names, and whether the custody chain is reconstructable. This post is the latter; the isolation walkthrough is [Does the user's identity survive agent delegation?](propagate-user-identity-across-agent-delegation.md).

### How do I pull every action taken on one person's behalf?

Query the collector by principal: `audit.for_user("u-alice")` returns that human's entries across every agent, `audit.query(...)` filters by user, session, event type, or time window, and `audit.purge_user("u-alice")` drops them for a GDPR erasure request. Because delegated peers attribute to the same human, the result spans the whole fleet, not just the first hop.

### Do LangGraph, CrewAI, or AutoGen do this?

They can move context between agents — LangGraph threads `config`, CrewAI passes delegated tasks, AutoGen routes messages — but none makes the originating human a first-class principal that automatically appears as the accountable actor on downstream audited actions, nor stamps a structured `delegated_by`. You can build it by threading `user_id` onto every log line yourself; Promptise makes it the structural default.

## Next steps

Learn how the accountable principal survives every delegation hop: attach a `CallerContext` at the top of the request, wire peers with `cross_agents=` so `ask_peer`/`broadcast` inherit it, and let the ambient principal and `delegated_by` land on every downstream record automatically. Start with [Observability](../../core/observability.md) to see how the timeline attributes each action to the originating human, then [Authentication & Security](../../mcp/server/auth-security.md) to make the server-side record tamper-evident. For the compliance payoff, read [One Audit Trail for SOC 2, HIPAA and the EU AI Act](ai-agent-compliance-audit-trail.md).
