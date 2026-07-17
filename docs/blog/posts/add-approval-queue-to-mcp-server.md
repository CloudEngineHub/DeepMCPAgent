---
title: "Add a Four-Eyes Approval Queue to Your MCP Server"
description: "The reviewer-side build tutorial. Stand up an independent queue with PendingApprover: it auto-registers role-guarded approvals_list and approvals_decide…"
keywords: "add approval queue to mcp server, pendingapprover setup, approvals_list approvals_decide, role-guarded approval admin tools, reviewer role rbac mcp"
date: 2026-07-16
slug: add-approval-queue-to-mcp-server
categories:
  - Approvals & HITL
---

# Add a Four-Eyes Approval Queue to Your MCP Server

Search **add approval queue to MCP server** and you'll find plenty on gating a tool, but almost nothing on the reviewer side — and the reviewer side is four separate chores: a pending store that holds a blocked call, admin endpoints a human can drive to release it, role-based access so only reviewers can touch those endpoints, and a separation-of-duties check so the person who *made* the call can't rubber-stamp their own request. This is the reviewer-side build — not "how do I gate a tool" (a gate that runs a policy or a webhook is covered elsewhere), but "how do I stand up an independent human queue that a *different* person drains." In Promptise Foundry it is one object: `PendingApprover`. This post shows the whole flow end to end, then covers the honest edges so you deploy it knowing exactly what it does and doesn't survive.

<!-- more -->

## What the queue has to do that a gate alone doesn't

An [approval gate](../../mcp/server/approval-gates.md) answers one question: *may this specific call proceed?* Point it at a callback and it decides in-line; point it at a webhook and an external system decides. Neither of those is a **queue**. A four-eyes review queue has a harder shape:

- The gated call must **block** — hold the request open — while a human, somewhere else, thinks about it.
- A reviewer needs a way to **see** what's waiting: which tool, which arguments, which caller, how long it's been pending.
- Only actual reviewers may act. That's `reviewer role` RBAC on the MCP server, not a convention in a wiki.
- And the reviewer must not be the caller. If the same principal can submit *and* approve, you don't have four-eyes control — you have a two-step no-op.

Build that by hand and you're writing a store, two endpoints, a role guard on each, and an identity comparison — plus the bounded-backlog logic so a flood of pending calls doesn't pile up unboundedly. `PendingApprover` ships all of it as one call, and the admin tools it registers are ordinary guarded MCP tools, so any client your reviewers already use can drain the queue.

## PendingApprover setup: one call, two admin tools

Here is the complete `PendingApprover` setup. It runs with nothing but `pip install promptise` — no API key, no network. Auth here is `APIKeyAuth` with two principals, *both* holding the `approver` role, precisely to prove the separation-of-duties point in the next section:

```python
import asyncio
from promptise.mcp.server import (
    APIKeyAuth, AuthMiddleware, ApprovalGateMiddleware,
    MCPServer, PendingApprover, TestClient,
)

server = MCPServer(name="billing")
server.add_middleware(
    AuthMiddleware(
        APIKeyAuth(keys={
            # The caller ALSO holds the approver role — the tempting shortcut
            # that most stacks leave open. Promptise closes it anyway.
            "sk-dana": {"client_id": "dana", "roles": ["approver"]},
            "sk-omar": {"client_id": "omar", "roles": ["approver"]},
        })
    )
)
approver = PendingApprover(server)  # auto-registers approvals_list / approvals_decide
server.add_middleware(ApprovalGateMiddleware(approver, timeout=30))


@server.tool(auth=True, requires_approval=True)
async def refund(order_id: str, amount: float) -> str:
    """Refund an order — blocks until an independent reviewer signs off."""
    return f"refunded {order_id} (${amount})"


async def main():
    client = TestClient(server)

    # dana asks the agent to refund; the gated call blocks in the pending store.
    call = asyncio.create_task(
        client.call_tool(
            "refund", {"order_id": "A-1", "amount": 5000.0},
            headers={"x-api-key": "sk-dana"},
        )
    )
    while not approver.pending():
        await asyncio.sleep(0.01)
    request_id = approver.pending()[0]["request_id"]

    # dana tries to approve her OWN request — she holds "approver", so a role
    # check alone would let this through. The four-eyes invariant refuses it.
    self_try = await client.call_tool(
        "approvals_decide", {"request_id": request_id, "approve": True},
        headers={"x-api-key": "sk-dana"},
    )
    print("dana approves her own call ->", self_try[0].text)

    # omar — a different principal — releases it.
    other = await client.call_tool(
        "approvals_decide", {"request_id": request_id, "approve": True},
        headers={"x-api-key": "sk-omar"},
    )
    print("omar approves dana's call ->", other[0].text)
    print("tool result             ->", (await call)[0].text)


asyncio.run(main())
```

Running it prints exactly:

```text
dana approves her own call -> {"resolved": false, "error": "cannot approve your own request — four-eyes separation of duties requires a different reviewer"}
omar approves dana's call -> {"resolved": true, "request_id": "99fe8b9b96f0baee175c0e68e3788aa6", "approved": true}
tool result             -> refunded A-1 ($5000.0)
```

`PendingApprover(server)` did three things in one line: it registered `approvals_list` and `approvals_decide` as **role-guarded approval admin tools** (each carries `auth=True` and a `HasRole("approver")` guard), it became the handler the `ApprovalGateMiddleware` awaits, and it created the process-local store the blocked `refund` call now waits in. You never wrote an endpoint, a guard, or a store.

Those two auto-registered tools are the entire reviewer surface, and because they're normal MCP tools your reviewers reach them through the same client they already use — no side channel, no admin web app to build.

- `approvals_list()` returns each waiting request as `{request_id, tool, arguments, client_id, tenant_id, age_seconds}`. That's enough to decide without grepping logs: the reviewer sees the tool, the exact validated arguments, *who* called, which tenant they belong to, and how stale the request is.
- `approvals_decide(request_id, approve, reason)` releases or denies a specific request. The reviewer's identity is read from the **active request context**, not passed as a parameter — so the `client_id` recorded on the decision is the authenticated caller, and it's consistent across stdio, HTTP, and SSE transports. A stray parameter would let a reviewer spoof who approved; reading it from context closes that.

Both are guarded by the `approver_role` (default `"approver"`, configurable): `PendingApprover(server, approver_role="reviewer")` gives you `reviewer` role RBAC on the MCP server instead. A caller without the role that tries `approvals_list` is denied by the guard before any queue state is touched — the same `HasRole` guard the rest of your tools use.

## Separation of duties is an invariant, not a lint rule

The line most home-grown queues get wrong is the one this design refuses to let you skip. In the example, `dana` holds the `approver` role. A plain role check — "is the caller an approver?" — would happily let her approve her own $5,000 refund. That's the exact hole four-eyes control exists to close, and it's easy to leave open because the caller genuinely *is* a valid reviewer.

`approvals_decide` compares the reviewer's `client_id` against the original caller recorded on the request. If they match **and** the decision is an approval, it refuses:

```text
cannot approve your own request — four-eyes separation of duties
requires a different reviewer
```

Note the asymmetry, which is deliberate: **denying** your own request is always allowed — anyone should be able to cancel a call they started — but **approving** it requires a different principal. This runs on the server, for every client, regardless of how the reviewer connects. It is not advice in a runbook; it's the return value of the admin tool.

Backpressure is the other structural guard. `PendingApprover(server, max_pending=100)` (100 is the default) denies new gated calls immediately once that many are already waiting, so a burst of blocked calls can't grow without bound. Beyond the cap, the gate fails closed with a clear reason rather than queueing forever.

## The honest edges: process-local, and what a restart does

The queue is **process-local**, and you should deploy it knowing exactly what that means — the same honesty the [Approval Gates guide](../../mcp/server/approval-gates.md) states plainly:

- **A pending call does not survive a server restart.** The store lives in the process's memory (like the in-memory job queue). Restart the server and the blocked call is gone — but it fails *safe*: it was never approved, so nothing ran. From the caller's side it resolves as a denial once the gate's `timeout` elapses.
- **Replicas do not share one queue.** If you run three instances behind a load balancer, a call blocked on instance A is invisible to a reviewer whose `approvals_list` happens to hit instance B. For a shared queue across replicas today, terminate approvals at a single instance (or a dedicated approver process — pass `PendingApprover(server=None)` and call `register_tools()` on the server you want reviewers to reach), or use a webhook handler backed by your own durable store. A distributed backend is on the roadmap alongside the durable job queue.
- **Timeouts drain the queue.** Every blocked call carries the gate's `timeout`. When it elapses without a decision, the gate denies by default and the entry is removed from the store. So the queue can't accumulate zombie entries: either a reviewer decides, or the timeout does. Set `timeout` to your real review SLA — 30 seconds in the demo, minutes to hours in production.

These are the correct trade-offs for a control that fails closed: the worst case of a lost queue entry is a denied refund, never an unattended one.

## What other frameworks do today

Human-in-the-loop is not a Promptise invention, and the major frameworks each ship a real HITL primitive. The honest delta here is narrow and specific: it's about the *reviewer-side queue* — the store, the admin endpoints, the reviewer RBAC, and the separation-of-duties check — not about whether a human can approve at all.

- **LangGraph** has genuinely durable HITL: `interrupt()` plus a configured checkpointer saves graph state and resumes via `Command(resume=...)`. That checkpointing is an area where LangGraph is *ahead* of `PendingApprover`'s process-local store — a LangGraph interrupt can survive a restart, which the in-memory queue above cannot. What LangGraph doesn't hand you is the *reviewer* half: there's no built-in `approvals_list`/`approvals_decide` surface, no role guard on who may resume, and no check that the resumer isn't the requester. You build those around the resume call.
- **CrewAI** has `human_input=True` on a `Task`, which prompts for feedback on that task's output — real and easy, but a console/stdin prompt in the driving process, not a queryable queue with per-reviewer RBAC or a distinct-reviewer rule.
- **AutoGen** wires approval through `UserProxyAgent(human_input_mode="ALWAYS")` — approval is a property of the conversation topology. There's no separate pending store with role-guarded admin tools that an independent reviewer drains out-of-band.
- **Pydantic AI** models approval as a deferred tool resolved app-side: the requirement surfaces to your application, which handles it and re-runs. The store, the reviewer permissions, and the separation-of-duties check are yours to write.

None of this means those frameworks *can't* run a four-eyes queue — with enough glue any of them can. The exact delta is that in each one you assemble the pending store, the admin endpoints, the reviewer RBAC, and the caller-≠-reviewer check yourself, and it's the last one teams forget. Promptise makes all four **structural**: one `PendingApprover` call, and the separation-of-duties refusal is the admin tool's own return value. For where each framework's HITL physically executes, see [LangGraph vs CrewAI vs AutoGen: Where HITL Runs](langgraph-vs-crewai-vs-autogen-human-in-the-loop.md); for why a *declared* approval can't ship ungated in the first place, see [An MCP Approval Gate That Refuses to Ship Ungated](build-time-enforced-approval-gate-mcp.md).

## Frequently asked questions

### Do I have to write the approval admin endpoints myself?

No. `PendingApprover(server)` auto-registers `approvals_list` and `approvals_decide` as role-guarded tools on the server you pass. They're ordinary MCP tools, so reviewers call them through whatever MCP client they already use. If you want the admin tools on a *different* server than the one being gated, construct `PendingApprover(server=None)` and call `register_tools(other_server)` yourself.

### How does it stop a caller from approving their own request?

`approvals_decide` reads the reviewer's `client_id` from the authenticated request context and compares it to the caller recorded on the pending request. If they match and the reviewer is trying to *approve*, the tool refuses with a separation-of-duties error — even when that caller also holds the `approver` role. Denying your own request is still allowed, so anyone can cancel a call they started.

### What happens to pending calls when the server restarts?

They're gone from the store, and because nothing was ever approved, nothing ran — the caller's request resolves as a denial once the gate `timeout` elapses. The queue is process-local: it does not survive a restart, and replicas don't share one queue. Terminate approvals at a single instance, or back the gate with a webhook handler over your own durable store if you need cross-restart, cross-replica persistence.

### Can I change the required role or cap the backlog?

Yes. `PendingApprover(server, approver_role="reviewer", max_pending=250)` guards the admin tools with the `reviewer` role instead of `approver` and denies new gated calls immediately beyond 250 waiting requests. The role flows straight into the `HasRole` guard on both admin tools.

### Does this work on a multi-tenant server?

Yes. Each pending request carries its `tenant_id`, so `approvals_list` shows which tenant a call belongs to, and on a `require_tenant=True` server the admin tools themselves demand tenant identity from reviewers. The [Secure Multi-Tenant Platform guide](../../guides/secure-multi-tenant-platform.md) wires the gate alongside per-tenant identity end to end.

## Next steps

Add `PendingApprover(server, approver_role="approver")` to your server, mark one real tool `requires_approval=True`, and give two principals the `approver` role — then run the snippet above and watch the self-approval get refused while a second reviewer releases the call. Once that works, deliberately test the process-local edge: block a call, restart the server, and confirm the caller resolves to a denial rather than an unattended execution. When you're ready to compose it with auth, tenancy, rate limiting, and tamper-evident audit, read the [Approval Gates guide](../../mcp/server/approval-gates.md) for the full gate semantics and the [Secure Multi-Tenant Platform guide](../../guides/secure-multi-tenant-platform.md) to run reviewers on a verified per-tenant identity foundation.
