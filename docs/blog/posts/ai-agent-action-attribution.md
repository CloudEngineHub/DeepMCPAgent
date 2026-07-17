---
title: "Which AI agent did this? Attribution for agent fleets"
description: "When a fleet shares one key, a SOC 2 or ISO reviewer asking 'who performed this action?' gets 'the model' as the answer. This piece frames attribution as a…"
keywords: "AI agent action attribution, which AI agent did this, SOC 2 agent attribution, tamper-evident agent audit, who performed this action agent, verified principal audit"
date: 2026-07-16
slug: ai-agent-action-attribution
categories:
  - Identity
---

# Which AI agent did this? Attribution for agent fleets

AI agent action attribution is the forensic question every autonomous fleet eventually has to answer: when a refund was issued, a record was deleted, or a customer's data was exported, *which agent did it?* If your fleet shares one API key and one model credential, the honest answer is "the model" — and "the model did it" is not a sentence a SOC 2 or ISO reviewer will accept as attribution. This post frames attribution as a forensics problem rather than a logging convenience, and shows the two properties a record needs to actually survive an incident: the acting agent stamped on *every* action, and a *verified* principal landing in a *tamper-evident* log.

<!-- more -->

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## "The model did it" is not an answer

Attribution breaks in a very specific place. A fleet of ten agents shares one key. Each one can call the same tools with the same authority. Post-incident, your logs faithfully record that `issue_refund` was called — but every line looks identical, because every agent presented the same credential. You can see *what* happened. You cannot see *who* did it. That is the shared-key anti-pattern that human identity abandoned decades ago, and it is exactly what most agent fleets still run. (For why each agent should carry its own identity rather than a shared secret, see [Give each AI agent its own identity, not a shared key](give-each-ai-agent-its-own-identity.md).)

Attribution is not the same as observability. Observability tells you a call happened and how long it took. Attribution has to hold up when someone disputes it — when a customer says "your agent charged me twice" or a reviewer asks you to prove that agent A, owned by team X, took an action three weeks ago. That standard raises two demands a debug log never has to meet:

1. **Every action carries the acting agent.** Not a sampled subset, not "usually" — every recorded tool call and LLM turn names the principal that took it, or the record has a hole exactly where the dispute is.
2. **The recorded principal is verified, and the record can't be quietly rewritten.** A name a process typed about itself proves nothing, and a log an insider can edit after the fact proves nothing either.

Get either wrong and you have a story, not evidence.

## What other frameworks record today

It is worth being precise and fair here, because agent frameworks *do* record a "who" — just not one that clears the forensic bar.

- **LangChain callbacks and LangSmith tracing** let you attach a `run_name`, tags, and a free-form `metadata` dict to every run, and LangSmith persists the resulting traces for inspection. That is genuinely useful, and for many teams it is enough to eyeball which chain ran. The catch is where the "who" comes from: those fields are written by the emitting process. A run labels *itself*. Nothing on the trace is checked against an identity provider, so the label is an assertion, not a verified fact.
- **OpenTelemetry / OpenLLMetry GenAI instrumentation** is the same shape one layer down. Span attributes — including any user or agent id — are set by the SDK at emit time. Spans are excellent for latency and cost, but a span attribute is self-reported, and a span store is a queryable database, not a per-record signed chain.
- **CrewAI** surfaces verbose execution logs and can export to those same backends; the actor it records is the configured agent's *role name* — again a string the framework assigns to itself.

None of this is wrong. For observability it is exactly right. The gap is narrow and specific: the recorded principal is **self-asserted rather than signature-verified**, and the destination is a **mutable trace database rather than a tamper-evident chain**. For debugging, neither property matters. For attribution — proving who performed an action, using a record that stays trustworthy *after* an adversary has had write access to your logs — those two properties are the entire game. Promptise's edge is not "nobody else logs a who." It is that the who is a *verified* principal and it is a *first-class field inside a chained audit* — a structural guarantee, not a metadata convention you have to remember to set.

## Attribution done right: a verified principal in a tamper-evident log

Promptise closes both gaps at once, and in two places, so attribution is redundant by design.

**On the observability timeline.** Attach an `AgentIdentity` to an agent and turn on observability, and every tool call and LLM turn is stamped with that agent's identifier — its `agent_id`, or the IdP-assigned `subject` for a verifiable identity. Delegation, the point where attribution usually evaporates, stays intact: when one agent hands work to a peer, the peer's entries are stamped with `delegated_by`, so work done "by the other agent" is still traceable to the originator. The full four-touchpoint walkthrough — attribution, outbound auth, inbound verification, and audit — is the [end-to-end Identity guide](../../identity/guide.md).

**In the tamper-evident audit.** This is the part that survives a dispute. When an MCP server verifies the caller's token — with `JWTAuth` against a shared secret, or `JwksAuth` against your IdP's published keys — the *verified* descriptors of the acting agent (`subject`, `issuer`, `audience`, `roles`) land as a first-class `identity` block inside every audit entry. And because `AuditMiddleware(signed=True)` writes an HMAC-chained log — each entry hashes the one before it — editing, deleting, or reordering any entry breaks the chain at that point. An insider can't rewrite `subject` from `billing-bot` to `reporting-bot` without invalidating the signature.

That combination is the answer competitors don't cleanly give: *who performed this action* is (a) checked server-side against an identity provider, not asserted by the caller, and (b) bound into a chain where any later edit is detectable. The principal reaches `ctx.client` on the server the same way a human user's identity does — through the [multi-user identity flow](../../guides/multi-user-identity.md), where `CallerContext` carries the bearer token to the server and roles/subject are extracted from the JWT server-side, never trusted from the client. How an agent obtains that token in the first place is covered in [How does an AI agent authenticate to an API? (not API keys)](how-does-an-ai-agent-authenticate-to-an-api.md).

## See it run: from JWT to a signed identity block

Here is the whole mechanism in one runnable file. It stands up a billing server, verifies a caller's token, records a refund, prints the **verified** principal the audit captured, then simulates an insider rewriting history to blame a different agent — and watches the chain catch it. Every API is real, and it runs in-process with `TestClient`: no network, no LLM key.

```python
# attribution_demo.py — prove which agent performed an action, tamper-evidently.
import asyncio

from promptise.mcp.server import (
    MCPServer, AuthMiddleware, JWTAuth, AuditMiddleware, TestClient, RequestContext,
)

SECRET = "rotate-me-in-prod"          # in prod: PROMPTISE_AUDIT_SECRET from a vault

server = MCPServer(name="billing-api")

# 1. Verify the caller's JWT server-side — the principal is CHECKED, not asserted.
auth = JWTAuth(secret=SECRET)
server.add_middleware(AuthMiddleware(auth))

# 2. Write each call to an HMAC-chained audit. The verified subject / issuer /
#    audience / roles land as a first-class `identity` field in every entry.
audit = AuditMiddleware(log_path="billing-audit.jsonl", signed=True, hmac_secret=SECRET)
server.add_middleware(audit)


@server.tool(auth=True)
async def issue_refund(order_id: str, amount: float, ctx: RequestContext) -> dict:
    """Issue a refund. Requires a valid, verified identity."""
    return {"order_id": order_id, "refunded": amount, "by": ctx.client.subject}


async def main() -> None:
    # billing-bot presents an IdP-issued token (minted here for the demo).
    token = auth.create_token({
        "sub": "billing-bot",
        "iss": "https://login.example.com",
        "aud": "api://billing",
        "roles": ["refunder"],
    })
    client = TestClient(server, meta={"authorization": f"Bearer {token}"})

    await client.call_tool("issue_refund", {"order_id": "A-1001", "amount": 49.0})

    # The audit entry attributes the action to a VERIFIED principal:
    entry = audit.entries[-1]
    print("identity:", entry["identity"])
    # -> {'subject': 'billing-bot', 'issuer': 'https://login.example.com',
    #     'audience': 'api://billing', 'roles': ['refunder']}
    print("chain valid:", audit.verify_chain())        # True

    # An insider rewrites history to blame a different agent...
    audit.entries[0]["identity"]["subject"] = "reporting-bot"
    print("chain valid:", audit.verify_chain())        # False — tamper detected


asyncio.run(main())
```

The `identity` block is not a string the agent typed about itself — it is what the server extracted *after* validating the token's signature, so it names the principal the resource actually authenticated. And because that block is part of the payload the HMAC covers, rewriting `subject` after the fact is exactly the edit `verify_chain()` is built to catch. That is attribution you can hand an auditor: they don't take your word for the "who," they check the math. Argument and result capture stay opt-in (`include_args`, `include_result`) so the trail records *who did what* without dragging PII into your logs by default.

## What a verified subject proves — and what it doesn't

Honest attribution means being precise about the trust boundary, and Promptise is deliberately blunt about it. A verified `subject` on the server side means the resource checked a signature against the IdP's keys — that is real cryptographic proof the caller holds a valid credential for *this* audience. But two caveats matter:

- **The audience is load-bearing.** `JwksAuth` *requires* an `audience` precisely so an agent can't replay a token minted for a different resource. Attribution to "billing-bot" is only as sound as the audience check that stopped a CRM token from standing in for a billing one.
- **`subject()` read on the *holder* side is not verified.** When an agent reads its own token's `sub` for local attribution, it does so without verifying the signature — it trusts its own IdP-issued token. Cross-system trust comes from the *resource* verifying, not the holder asserting. The [Identity security page](../../identity/security.md) lays out the full threat model: what a `subject()` does and does not prove, why credentials never touch disk or logs, and where a compromised host sits outside the boundary.

That candor is the point. Attribution that overclaims is worse than none, because it invites reliance the mechanism can't support. The claim Promptise makes is bounded and testable: on a call the server verified, the acting principal is recorded, and any later edit to that record is detectable.

## Frequently asked questions

### How do I answer "which AI agent did this?" after an incident?

Give each agent its own `AgentIdentity`, verify its token server-side (`JWTAuth` or `JwksAuth`), and run `AuditMiddleware(signed=True)`. Every audited call then carries an `identity` block — the verified `subject`, `issuer`, `audience`, and `roles` — inside an HMAC chain, so you can point at the exact entry and prove it wasn't edited afterward.

### Isn't LangSmith or OpenTelemetry tracing enough for attribution?

For debugging, yes. For forensic attribution, there's a specific gap: the "who" on a trace or span is set by the emitting process (self-asserted), and the trace store is a mutable database, not a per-record signed chain. Promptise makes the principal a *verified* value the server checked and binds it into a *tamper-evident* audit — the two properties a dispute actually turns on.

### What exactly does the audit record about the acting agent?

Identity *descriptors* only: `subject`, `issuer`, `audience`, `roles`, and `tenant_id` when present — never the raw token or the full claim set, so sensitive data stays out of your logs. Alongside those it records the tool, `client_id`, `request_id`, status, duration, timestamp, and the `prev_hash`/`hmac` chain linkage.

### Does attribution survive when one agent delegates to another?

Yes. With observability on, a peer's timeline entries are stamped with `delegated_by`, so work performed by the delegate is still traceable to the originating agent — answering "who *caused* this?", not just "who ran it?"

## Next steps

See how a verified principal reaches the audit log, end to end — attribution, outbound auth, inbound verification, and a tamper-evident trail wired together for one realistic fleet: read the [end-to-end Identity guide](../../identity/guide.md). If you're weighing how far to take it, the [Identity security page](../../identity/security.md) states exactly what a verified subject proves, and [multi-user identity](../../guides/multi-user-identity.md) shows the same principal-to-`ctx.client` flow for human callers. Already running agents? Add an `identity=` argument and `AuditMiddleware(signed=True)`, and your next incident review has a "who," not a shrug.
