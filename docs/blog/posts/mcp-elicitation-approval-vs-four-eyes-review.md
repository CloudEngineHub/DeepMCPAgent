---
title: "Elicitation vs Four-Eyes: Two Ways to Approve AI Actions"
description: "A decision guide between two legitimate approval models built on one shared handler: confirm with the human behind the calling client mid-run…"
keywords: "mcp elicitation approval vs four-eyes review, elicitation approval mid-run, independent reviewer vs self-confirm ai, pending approval queue vs client confirm, when to use elicitationapprover"
date: 2026-07-16
slug: mcp-elicitation-approval-vs-four-eyes-review
categories:
  - Approvals & HITL
---

# Elicitation vs Four-Eyes: Two Ways to Approve AI Actions

The choice at the heart of **mcp elicitation approval vs four-eyes review** is not "should this tool need a human?" — you already decided that when you marked it `requires_approval=True`. The real question is *which* human, and *when*: do you confirm with the person sitting behind the client that made the call, right now, mid-run — or do you park the call in a queue for an independent reviewer who was not the one who triggered it? Both are legitimate. They protect against different failures, and picking the wrong one either annoys your users or lets a caller wave through their own risky action. This post is a decision guide, and the good news is that in Promptise Foundry the two models sit behind one interchangeable handler, so choosing is a one-line change you can make per tool.

## "Really delete this?" is not "a second person must sign off"

Start from the two questions the approval is actually answering, because they are not the same question.

**"Really delete this?"** is a *confirmation*. The person who asked the agent to do the thing is the right person to confirm the thing. There is no conflict of interest — you are protecting a user from their own typo, a fat-fingered "delete everything," an agent that over-interpreted a vague instruction. The reviewer *is* the caller, and that is fine. What you want here is a fast, in-line prompt that interrupts the exact session that made the call.

**"A second person must sign off on this payment"** is a *control*. The whole point is that the person who initiated the action must **not** be the person who approves it. This is dual control, four-eyes, separation of duties — the thing every finance and security review asks for first. If the caller can approve their own $50,000 refund, you do not have a control; you have a speed bump. What you want here is an independent queue that a *different* human drains, and a hard rule that the initiator cannot release their own request.

Confuse the two and you get a real vulnerability, not a UX nit. A confirm-the-caller prompt dressed up as "four-eyes review" is self-approval with extra steps. An independent queue used for "really delete this?" is a reviewer three time zones away staring at a stranger's routine cache purge. Promptise Foundry gives each pattern a purpose-built approver — `ElicitationApprover` for the confirmation, `PendingApprover` for the control — and both plug into the same [server-side approval gate](../../mcp/server/approval-gates.md).

## Both are the same one-line swap

Here is the part that makes the decision cheap to get right: both approvers implement the same `ApprovalHandler` protocol, the same one that backs [agent-side `ApprovalPolicy`](../../core/approval.md). You build the gate once; swapping *how* the human is reached is a single argument. The snippet below runs with nothing but `pip install promptise` — no API key, no network — and exercises both models end to end:

```python
import asyncio

from promptise.mcp.server import (
    ApprovalGateMiddleware,
    ElicitationApprover,
    MCPServer,
    PendingApprover,
    TestClient,
)


def build_billing(approver):
    """One factory, one gate. Swapping the approver is the only line that
    differs between 'confirm with the caller' and 'an independent reviewer
    signs off' — both implement the same ApprovalHandler protocol."""
    server = MCPServer(name="billing")
    server.add_middleware(ApprovalGateMiddleware(approver, timeout=5))

    @server.tool(requires_approval=True)
    async def refund(order_id: str, amount: float) -> dict:
        """Refund an order — a human must approve before this body runs."""
        return {"order_id": order_id, "amount": amount, "status": "refunded"}

    return server


async def main():
    # (A) ElicitationApprover — confirm with the human behind the calling
    #     client. Under TestClient there is no live elicitation session, so it
    #     FAILS CLOSED: the call is denied, never silently allowed.
    elicit = build_billing(ElicitationApprover())
    denied = await TestClient(elicit).call_tool(
        "refund", {"order_id": "A-1", "amount": 10.0}
    )
    print("(A) elicitation, no live session:", denied[0].text[:72])

    # (B) PendingApprover — an INDEPENDENT reviewer drains a queue. The call
    #     blocks while a *different* human lists it and decides. One line changed.
    reviewer = PendingApprover(approver_role="approver")
    client = TestClient(build_billing(reviewer))

    call = asyncio.create_task(
        client.call_tool("refund", {"order_id": "A-2", "amount": 25.0})
    )
    await asyncio.sleep(0.1)                  # let the call reach the queue
    waiting = reviewer.pending()             # what approvals_list() surfaces
    print("(B) awaiting a second person:", waiting[0]["tool"], waiting[0]["arguments"])
    reviewer.decide(waiting[0]["request_id"], True, reviewer_id="manager-2")

    approved = await call
    print("(B) approved by reviewer:", approved[0].text)


asyncio.run(main())
```

Run it and you get exactly this:

```text
(A) elicitation, no live session: {
  "error": {
    "code": "APPROVAL_DENIED",
    "message": "Approval d
(B) awaiting a second person: refund {'order_id': 'A-2', 'amount': 25.0}
(B) approved by reviewer: {"order_id": "A-2", "amount": 25.0, "status": "refunded"}
```

The only thing that changed between the two models is the object passed to `build_billing`. The gate, the tool declaration, the fail-closed timeout, the audit trail — identical. That is the whole design goal: the decision "confirm the caller vs independent reviewer" should never force you to rewrite your server.

## ElicitationApprover: confirm with the human behind the client — and fail closed

`ElicitationApprover` uses [MCP elicitation](../../mcp/server/approval-gates.md) to ask the *calling* client's user to confirm the call, returning `{"approve": bool, "reason": str}`. This is the "really delete this?" path: the confirmation goes to the same session that made the call, so it is instant and needs no separate reviewer role or queue. Reach for it on destructive-but-personal operations — deleting a user's own document, purging a cache they own, sending a message from their account.

The property that makes it safe to leave running is that **it fails closed with no live session**. Elicitation only works when the transport has a live MCP session that supports it. A `TestClient`, a stdio client without elicitation support, a batch job with no human attached — none of those can be asked to confirm. Rather than guess, the approver **denies**, with a clear reason (`elicitation approval mid-run` is only granted when there is genuinely a caller to ask). That is why line (A) above prints `APPROVAL_DENIED`: the test harness has no elicitation session, so the confirmation cannot happen, so the refund does not run. An approval you cannot obtain is never silently treated as an approval.

The flip side is the honest limitation: because the confirmation is the caller confirming their own action, `ElicitationApprover` is **not** separation of duties. It is confirm-your-own-action dual control. If your requirement is "the initiator must not be the approver," this is the wrong tool — which is exactly what the next section is for.

## PendingApprover: an independent reviewer draining a queue

`PendingApprover` blocks the gated call in a pending store and hands the decision to a *different* human — anyone holding `approver_role`. In production that reviewer works through two auto-registered, role-guarded admin tools:

```text
approvals_list()                          → pending requests: tool, args,
                                            caller, tenant, age
approvals_decide(request_id, approve,     → release or deny; the reviewer's
                 reason)                     client_id is recorded
```

In the snippet I used the equivalent in-process reviewer API (`reviewer.pending()` and `reviewer.decide(...)`) so the script stays self-contained, but the shape is the same: a queue exists, a second person inspects it, and the original call unblocks only when they decide. This is the `pending approval queue vs client confirm` distinction made concrete — the caller is *not* in the loop, by design.

The control that turns this from "a queue" into real four-eyes is enforced, not documented: **`approvals_decide` refuses an *approval* whose reviewer `client_id` equals the request's original caller.** You cannot release your own call, even if you also hold `approver_role` (denying your own is always allowed — you can always stop your own action). That is the `independent reviewer vs self-confirm ai` guarantee, and it is checked in the server, so no amount of client-side cleverness routes around it. For the reasoning behind that rule, see [An MCP Approval Gate That Refuses to Ship Ungated](build-time-enforced-approval-gate-mcp.md), which covers why the gate also refuses to build if you declare `requires_approval=True` and forget to install it.

Be honest about the edges: the pending store is process-local (like the in-memory job queue), so calls that outlive the gate timeout are denied by default, and replicas do not share a queue yet. `max_pending` (default 100) denies immediately beyond that many waiting calls, so a flood of blocked approvals cannot pile up unbounded.

## Which one fits — a decision table

| Question | `ElicitationApprover` | `PendingApprover` |
|---|---|---|
| Who approves? | The human behind the *calling* client | An *independent* human with `approver_role` |
| Protects against | The caller's own mistakes ("really delete this?") | The caller acting unchecked (payments, four-eyes) |
| Separation of duties | No — confirm-your-own-action | Yes — initiator cannot release their own call |
| Needs a live session | Yes — fails closed without one | No — the call waits in a queue for a reviewer |
| Latency | Instant, in-line with the call | Bounded by how fast a reviewer drains the queue |
| Typical use | Delete my document, send from my account | Refund > $X, delete another tenant's data, wire transfer |

The rule of thumb: if the person who triggered the action is the right person to confirm it, use `ElicitationApprover`. If they are explicitly the *wrong* person to confirm it, use `PendingApprover`. And because both are `ApprovalHandler`s, you can decide *per tool* — elicitation on `delete_my_note`, a pending queue on `issue_refund` — without changing anything else about the gate.

## What other frameworks do today

To be fair about the delta: none of this is Promptise inventing human-in-the-loop, and elicitation in particular is not a Promptise feature. **MCP elicitation is part of the Model Context Protocol spec itself**, and a growing set of clients implement it. Any MCP server that exposes elicitation therefore hands you the *confirm-with-the-caller* primitive — the reference MCP SDKs let a tool ask the connected client for structured input mid-call, which is exactly the raw material `ElicitationApprover` is built from. So the mid-run confirm is genuinely available elsewhere; we are not claiming otherwise.

The precise gap is what sits *around* that primitive:

- **The mid-run confirm exists, but the fail-closed timeout is on you.** Calling the client's elicitation gives you a prompt; it does not, by itself, decide what happens when the client ignores it, has no session, or takes forever. Without a wrapper you get an indefinite block or an unhandled path. Promptise's gate applies `on_timeout="deny"` and denies when there is no live session — the approval you can't obtain becomes a denial, not a hang.
- **The independent-reviewer queue is a separate build.** Elicitation talks to the *caller*. Four-eyes needs a *different* person, which means a pending store, a reviewer role, list/decide tooling, and a rule that the initiator can't self-approve. That is real infrastructure you assemble yourself on top of the elicitation primitive.
- **General agent frameworks land the check app-side.** [LangGraph](../../core/approval.md) has a genuinely capable HITL primitive — `interrupt()` plus a checkpointer, resumed with `Command(resume=...)` — that can model *either* pattern. But it pauses in the graph runtime inside your process, and the queue and separation-of-duties check are yours to build; the pause is a property of the node, not the shared tool. (For a fuller matrix of where each framework's HITL physically runs, see [LangGraph vs CrewAI vs AutoGen: Where HITL Runs](langgraph-vs-crewai-vs-autogen-human-in-the-loop.md).)

So the differentiator is not "nobody else has elicitation" — they do. It is that Promptise ships **both** the mid-run confirm and the independent four-eyes queue as first-class, interchangeable `ApprovalHandler`s behind one gate, with the timeout, the fail-closed session check, and the self-approval rule as structural invariants rather than glue you write per project. The question a confirm-only primitive can't cleanly answer — *"can the person who made this call be stopped from approving it?"* — has a one-word answer here, enforced in the server: with `PendingApprover`, no.

## Frequently asked questions

### When should I use ElicitationApprover vs PendingApprover?

Use `ElicitationApprover` when the person who triggered the action is the right person to confirm it — "really delete this?", sending from a user's own account, purging data they own. Use `PendingApprover` when the initiator must explicitly *not* be the approver — payments, refunds above a threshold, cross-tenant deletions — because it routes the call to an independent reviewer and blocks self-approval. Both are `ApprovalHandler`s, so you can pick per tool.

### Does ElicitationApprover give me separation of duties?

No. It confirms with the human *behind the calling client*, which is confirm-your-own-action dual control, not four-eyes. If your control requires that the initiator cannot approve their own request, use `PendingApprover`, whose `approvals_decide` rejects an approval when the reviewer's `client_id` equals the original caller's.

### What happens if the client can't do elicitation?

`ElicitationApprover` fails closed. If the transport has no live MCP session that supports elicitation (a `TestClient`, a stdio client without support, a headless batch job), the request is **denied** with a clear reason instead of being silently allowed. An approval that can't be obtained is treated as a denial.

### Is switching between the two really one line?

Yes. Both approvers implement the same `ApprovalHandler` protocol, so you build `ApprovalGateMiddleware` once and change only the object you pass it — `ElicitationApprover()` or `PendingApprover(...)`. Nothing else about the tool declaration, the timeout, or the audit path changes.

### Can I still confirm the caller *and* require a second person?

They target different risks, so you normally pick one per tool. Because the choice is per-tool, a single server can use `ElicitationApprover` on personal-destructive tools and `PendingApprover` on money-movement tools at the same time — the gate and the audit chain are shared across both.

## Next steps

Pick `ElicitationApprover` or `PendingApprover` per tool — same middleware, one line to swap — and let the fail-closed timeout and the self-approval rule do the enforcing for you. Start with the [Approval Gates guide](../../mcp/server/approval-gates.md) to wire the gate in under 15 lines, read [agent-side Approval](../../core/approval.md) to see how the same `ApprovalHandler` protocol governs a Promptise agent's own tool calls, and if you want the design rationale for why a gated tool refuses to ship without its gate, see [An MCP Approval Gate That Refuses to Ship Ungated](build-time-enforced-approval-gate-mcp.md).
