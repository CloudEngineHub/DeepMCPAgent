---
title: "Who Approved That AI Refund? Reviewer Attribution"
description: "Scoped narrowly to approval-decision provenance, distinct from the general HMAC-chain audit hub (which owns tamper-evidence). When an agent issues a refund…"
keywords: "who approved this ai agent action, approval reviewer attribution, approval_request_id audit, prove who signed off ai refund, approval_denied audited error"
date: 2026-07-16
slug: who-approved-this-ai-agent-action
categories:
  - Approvals & HITL
---

# Who Approved That AI Refund? Reviewer Attribution

When compliance asks **who approved this ai agent action** — the $5,000 refund your agent issued to order A-1 at 3 a.m. — the honest answer is often a shrug, a Slack screenshot, and a log line that says the call succeeded. That is not reviewer attribution; that is a story. This post is scoped narrowly to one thing: the *provenance of an approval decision*. Not whether the audit log is tamper-evident (that question is settled elsewhere), but what actually lands on it when a human signs off, and whether that record can survive an auditor who does not trust your good intentions.

<!-- more -->

The tamper-evidence of the chain — the HMAC links that prove no entry was edited after the fact — is a separate concern, covered in the [Production Features overview](../../mcp/server/production-features.md). Here we assume the chain is trustworthy and ask a sharper question: *given a trustworthy record, does an approval decision leave enough on it to defend the sign-off?*

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## The question that arrives six months later

A refund tool fires. Money moves. Later, a dispute, an audit, or an incident review lands on your desk with a deceptively simple ask: **prove who signed off on this refund.** Three sub-questions hide inside it, and a "successful call" log answers none of them:

- **Who reviewed it** — a real, named principal, not "a human, probably."
- **Were they someone other than the caller** — because a sign-off by the same identity that requested the action is not dual control; it is a rubber stamp with extra steps.
- **Was it *this* call they approved** — tied to the exact arguments and request, not a different refund from the same minute.

Most agent stacks can produce a decision *somewhere* — a webhook payload, an orchestrator log line, a row your driver code wrote when it remembered to. What they rarely produce is a decision bound to a verified reviewer, provably distinct from the caller, on a record you can hand to an auditor without a follow-up meeting. That binding is the whole job of approval reviewer attribution, and it is the part that is easy to skip until the day you can't.

## Three things a defensible sign-off needs

A record you can defend has exactly three properties. Promptise Foundry's server-side [approval gate](../../mcp/server/approval-gates.md) produces all three as a structural consequence of how the gate is wired — you do not assemble them by hand.

1. **A verified reviewer identity, provably different from the caller.** When a human uses the `PendingApprover` admin tool `approvals_decide` to release a call, their `client_id` is read from the *authenticated request context* — the JWT subject the auth layer verified — not from a parameter they could spoof. And separation of duties is enforced at decision time: `approvals_decide` refuses an *approval* whose reviewer equals the request's original caller. You cannot approve your own refund, even if you happen to hold both roles.
2. **A stable `approval_request_id` tying the sign-off to the exact call.** Every gated call mints a `request_id` (a 128-bit token). The reviewer quotes it to decide; it is returned in the denial's structured `details`; it appears in the gate's decision logs. One id joins "the call that asked" to "the human who answered."
3. **The denial path surfaced as a structured, audited outcome** — an `APPROVAL_DENIED` error with a code, a reason, and the reviewer in `details`, recorded on the chain as an error entry — rather than a swallowed exception that leaves no trace of the *no*.

The next section shows all three landing on the tamper-evident chain in one runnable script.

## Reviewer attribution on the audit chain

The snippet below runs with nothing but `pip install promptise` — no API key, no network. Two identities: a `clerk` who issues refunds and a `manager` who reviews them. `AuditMiddleware` sits outermost so it records every outcome; the gate and the `PendingApprover` sit behind auth so every decision carries a verified identity. One refund is approved, one is denied, and then we print the chain.

```python
import asyncio, json
from promptise.mcp.server import (
    MCPServer, TestClient, AuthMiddleware, JWTAuth,
    AuditMiddleware, ApprovalGateMiddleware, PendingApprover,
)

auth = JWTAuth(secret="dev-secret")
server = MCPServer(name="billing")

audit = AuditMiddleware(hmac_secret="rotate-me-in-prod")   # outermost: record everything
server.add_middleware(audit)
server.add_middleware(AuthMiddleware(auth))                # identity before the gate
approver = PendingApprover(server, approver_role="approver")
server.add_middleware(ApprovalGateMiddleware(approver, timeout=10))


@server.tool(auth=True, roles=["clerk"], requires_approval=True)
async def refund(order_id: str, amount: float) -> dict:
    """Refund an order — a human must sign off before the money moves."""
    return {"order_id": order_id, "amount": amount, "status": "refunded"}


async def main():
    clerk = auth.create_token({"sub": "clerk-1", "roles": ["clerk"], "tenant_id": "acme"})
    manager = auth.create_token({"sub": "manager-9", "roles": ["approver"], "tenant_id": "acme"})
    h = lambda t: {"authorization": f"Bearer {t}"}
    client = TestClient(server)

    async def review(order_id, amount, *, approve, reason):
        """Caller issues the refund; a DIFFERENT human decides it."""
        call = asyncio.create_task(
            client.call_tool("refund", {"order_id": order_id, "amount": amount}, headers=h(clerk))
        )
        rid = None
        while rid is None:                       # reviewer waits for it to appear
            rows = json.loads(
                (await client.call_tool("approvals_list", {}, headers=h(manager)))[0].text
            )
            hit = [r for r in rows if r["arguments"].get("order_id") == order_id]
            rid = hit[0]["request_id"] if hit else None
            if rid is None:
                await asyncio.sleep(0.01)
        await client.call_tool(
            "approvals_decide",
            {"request_id": rid, "approve": approve, "reason": reason},
            headers=h(manager),
        )
        return rid, json.loads((await call)[0].text)

    rid_ok, granted = await review("A-1", 42.0, approve=True, reason="verified with customer")
    print("GRANT ", granted)

    rid_no, denied = await review("A-2", 5000.0, approve=False, reason="exceeds refund policy")
    print("DENY  ", denied["error"]["code"], denied["error"]["details"])

    print("\nAudit chain — one tamper-evident record of who did what:")
    for e in audit.entries:
        if e["tool"] == "approvals_list":        # skip the reviewer's queue reads
            continue
        idy = e.get("identity", {})
        print(f'  {e["tool"]:17} client={e["client_id"]:10} '
              f'subject={idy.get("subject","-"):10} tenant={idy.get("tenant_id","-")} '
              f'status={e["status"]}')
    print("\nchain valid:", audit.verify_chain())


asyncio.run(main())
```

The output (request ids differ per run):

```text
GRANT  {'order_id': 'A-1', 'amount': 42.0, 'status': 'refunded'}
DENY   APPROVAL_DENIED {'approval_request_id': '6c4127df16d5…', 'reviewer_id': 'manager-9'}

Audit chain — one tamper-evident record of who did what:
  approvals_decide  client=manager-9  subject=manager-9  tenant=acme status=ok
  refund            client=clerk-1    subject=clerk-1    tenant=acme status=ok
  approvals_decide  client=manager-9  subject=manager-9  tenant=acme status=ok
  refund            client=clerk-1    subject=clerk-1    tenant=acme status=error

chain valid: True
```

Read the chain. Each `refund` is issued by `clerk-1`; each decision is made by `manager-9`. Those are two different verified subjects, side by side on the *same* HMAC-linked chain, so the record shows not just that an approval happened but that a **different** principal made it — and `verify_chain()` proves nobody edited that ordering afterward. The tenant travels on every entry, so a per-tenant forensic slice ("show me every approval decision for `acme`") is a filter on a first-class field, not a log grep. This is what turns "we have approvals" into an answer you can hand to an auditor: the identity of the reviewer, distinct from the caller, is *on the record*, not in someone's memory of the incident.

## The denial path is an audited outcome, not a swallowed exception

Look again at the last line of the chain: the denied `refund` is `status=error`, not a missing entry. This is the part most home-grown approval code gets wrong. A denial is a *decision* — arguably the more important one to keep — and it deserves a record at least as durable as a grant. In Promptise the gate raises a structured `ApprovalDeniedError` (`code="APPROVAL_DENIED"`, `retryable=false`) whose `details` carry the `approval_request_id` and the `reviewer_id`. The caller receives it as structured content:

```json
{
  "error": {
    "code": "APPROVAL_DENIED",
    "message": "Approval denied for tool 'refund': exceeds refund policy",
    "retryable": false,
    "details": { "approval_request_id": "6c4127df16d5…", "reviewer_id": "manager-9" }
  }
}
```

Because `AuditMiddleware` wraps the pipeline, that same denial is written to the chain as an error entry — the `approval_denied audited error` is a fact on the record, with the caller's verified identity and tenant attached, chained to everything before and after it. Contrast the common failure mode: a rejection that surfaces as a generic exception the driver code catches and logs as "tool failed," erasing the fact that a *human said no to this specific call*. Fail-closed is the default the whole way down — a timeout with no decision denies, a reviewer who tries to edit the arguments is denied (the gate won't run arguments nobody approved), a crashed handler denies through the error pipeline — and every one of those outcomes is an audited `APPROVAL_DENIED`, never a silent pass. The full outcome table lives in the [Approval Gates guide](../../mcp/server/approval-gates.md).

## What other frameworks do today

To be fair: human-in-the-loop is not a Promptise invention, and the major frameworks all ship real HITL. The delta here is narrow and specific — it is about the *record the decision leaves*, not whether the pause exists. (For where each framework's pause physically runs, see [LangGraph vs CrewAI vs AutoGen: Where HITL Runs](langgraph-vs-crewai-vs-autogen-human-in-the-loop.md).)

- **LangGraph** has genuinely durable checkpointing: a checkpointer persists graph state per thread, and HITL uses `interrupt()` resumed with `Command(resume=<value>)`. That is powerful — but the checkpoint records the resume *payload your application supplies*. A verified reviewer identity independently authenticated and provably distinct from the caller, a stable per-call approval id, and separation of duties are things you assemble on top; the framework persists the graph state, not the who-approved-this attribution.
- **CrewAI** has `human_input=True` on a `Task`, which prompts for feedback on the console and folds the response back in. It is real and easy, but the answer comes from whoever is at that console; the decision is not stamped with a verified reviewer principal recorded distinct from the caller.
- **AutoGen** routes approval through a `UserProxyAgent` with `human_input_mode="ALWAYS"`, collecting input in the conversation loop. Same shape: the human's input is captured, but the identity of the approver — as a verified principal, distinct from the requester, tied to the exact call — is not something the mechanism attaches for you.

None of this means those frameworks *can't* record who approved what; with discipline you can log a reviewer id and a request id in any of them. The exact delta is that the record — verified reviewer, provably distinct from the caller, bound to the precise call, on a tamper-evident chain — is left for you to build and to keep correct. Promptise makes it structural: reviewer identity and `approval_request_id` are attached to every grant and denial by the gate itself, so `approval_request_id audit` queries and "prove who signed off ai refund" requests are answered by the framework, not by your incident-response heroics. The design rationale behind making the gate itself the enforcement point is written up in [An MCP Approval Gate That Refuses to Ship Ungated](build-time-enforced-approval-gate-mcp.md).

## Frequently asked questions

### How do I prove the reviewer was not the same principal that made the call?

Two ways, and they compose. First, the `approvals_decide` tool reads the reviewer's identity from the verified request context and **refuses** an approval when that identity equals the request's original caller — four-eyes is enforced at decision time, so a same-principal approval cannot be recorded in the first place. Second, the reviewer's decision is itself an audited tool call: on the chain you see the `refund` entry under the caller's subject and the `approvals_decide` entry under the reviewer's subject, two distinct verified identities on one HMAC-linked record. The separation is both prevented and provable.

### Where does the `approval_request_id` actually live?

It is minted per gated call and used as the join key across the whole flow: the reviewer quotes it in `approvals_decide(request_id, …)`, it is returned in the denial's structured `details.approval_request_id`, and the gate logs it on every requested/granted/denied decision. It ties the sign-off to the exact call and its exact arguments, so "which refund did the manager approve" is a lookup, not a reconstruction.

### Is a denied approval recorded, or does it just disappear?

Recorded. A denial raises a structured `APPROVAL_DENIED` error (not retryable) with the `reviewer_id` and `approval_request_id` in `details`, and `AuditMiddleware` writes it to the chain as an error-status entry with the caller's verified identity and tenant. A reviewer's *no* is as durable as their *yes* — never a swallowed exception.

### Does this replace the tamper-evident audit chain?

No — it rides on it. The HMAC chain's job is integrity (proving no entry was altered); this post is about *what an approval decision contributes* to that chain — reviewer, request id, tenant, and outcome. The two are complementary: see the [Production Features overview](../../mcp/server/production-features.md) for how `AuditMiddleware` composes with auth, rate limiting, and metrics in one pipeline.

## Next steps

Declare `requires_approval=True` on your riskiest tool, put `AuditMiddleware` outermost, and run the snippet above — then verify the chain shows a reviewer subject distinct from the caller and a denial recorded as `APPROVAL_DENIED`. The full option surface (`PendingApprover`, `ElicitationApprover`, webhook handlers, `on_timeout`, tenant-aware review) is in the [Approval Gates guide](../../mcp/server/approval-gates.md), and the way approval outcomes fold into a complete production pipeline — auth, tamper-evident audit, metrics — is in the [Production Features overview](../../mcp/server/production-features.md). When the question is "who approved that AI refund," the answer should be a query, not a story.
