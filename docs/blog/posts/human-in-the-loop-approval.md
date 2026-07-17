---
title: "Human-in-the-Loop Approval for AI Agents, Done Right"
description: "Client-side 'are you sure?' prompts are trivially bypassed by any other MCP client; this deep-dive argues approval must be enforced server-side and…"
keywords: "human-in-the-loop approval, AI agent approval workflow, server-side approval gate, four-eyes approval agents, approve irreversible tool calls, fail-closed approval"
date: 2026-07-16
slug: human-in-the-loop-approval
categories:
  - Production
---

# Human-in-the-Loop Approval for AI Agents, Done Right

Human-in-the-loop approval is the control that lets an autonomous agent do real work — issue a refund, delete a record, wire money — without letting it do that work unsupervised. The instinct most teams reach for first is a client-side confirmation: the agent asks "are you sure?" and waits for a click. That feels safe, but it protects nothing, because the confirmation lives in the caller you happen to be using today. By the end of this article you'll understand why approval has to live on the server that owns the tool, and you'll have a runnable gate that denies irreversible calls until a human signs off.

<!-- more -->

## Why client-side confirmation isn't a control

An MCP tool is a network endpoint. The moment you expose `refund` or `delete_account`, any MCP client can call it — your production agent, a teammate's script, a scheduled job, a future integration you haven't built yet. A confirmation prompt rendered in one agent's chat UI is invisible to every other caller. It's a UX nicety, not a security boundary.

The failure mode is concrete. You ship an agent with a polite "confirm before refunding" prompt. Six months later someone wires the same MCP server into a batch reconciliation job that has no UI at all. The prompt never fires. The batch job refunds ten thousand orders at 3 a.m. Nothing was bypassed maliciously — the control simply wasn't where the risk was.

The fix is to make approval a property of the *tool*, not a courtesy of the caller. That means a **server-side approval gate**: a middleware in the server's pipeline that intercepts a declared tool and refuses to run it until an authorized human decides. Every client inherits the control automatically, whether it's a chat agent, a cron trigger, or a curl command.

## What a server-side approval gate actually does

In Promptise Foundry you declare the requirement on the tool and install one middleware. The [Approval Gates guide](../../mcp/server/approval-gates.md) covers every option; here is the smallest version that runs end to end:

```python
import asyncio
from promptise.mcp.server import MCPServer, ApprovalGateMiddleware, TestClient

server = MCPServer(name="billing")

# The simplest ApprovalHandler is a callable. Auto-approve small refunds and
# route everything else to a human. Returning False denies the call outright.
def policy(request):
    return request.arguments.get("amount", 0) < 100

server.add_middleware(ApprovalGateMiddleware(policy, timeout=300))

@server.tool(requires_approval=True)
async def refund(order_id: str, amount: float) -> dict:
    """Refund an order — the gate must approve before this body runs."""
    return {"order_id": order_id, "amount": amount, "status": "refunded"}

async def main():
    client = TestClient(server)

    ok = await client.call_tool("refund", {"order_id": "A-1", "amount": 42.0})
    print("small refund:", ok[0].text)        # executed — under the threshold

    blocked = await client.call_tool("refund", {"order_id": "B-2", "amount": 5000.0})
    print("large refund:", blocked[0].text)    # APPROVAL_DENIED — body never ran

asyncio.run(main())
```

Two things make this a real control rather than a suggestion. First, the gate sits in the server's middleware chain, so it applies no matter who calls `refund`. Second — and this is the detail that stops half-measures — if any tool declares `requires_approval=True` and no `ApprovalGateMiddleware` is installed, **the server refuses to build**. A declared approval that silently doesn't enforce would be worse than none, so Promptise raises at build time (and `TestClient` raises on the call) instead of letting it slip through.

## Four-eyes approval agents can trust

The callable above is a *policy* approver — good for "amounts under $100 are fine." When you need an actual second person, use `PendingApprover`, which implements independent four-eyes review. Gated calls block in a pending store, and a human holding the approver role releases or denies them through two auto-registered, role-guarded admin tools:

```python
from promptise.mcp.server import (
    ApprovalGateMiddleware, AuthMiddleware, JWTAuth, MCPServer, PendingApprover,
)

server = MCPServer(name="billing")
server.add_middleware(AuthMiddleware(JWTAuth(secret="...")))  # identity first
approver = PendingApprover(server, approver_role="approver")
server.add_middleware(ApprovalGateMiddleware(approver, timeout=300))

@server.tool(auth=True, requires_approval=True)
async def refund(order_id: str, amount: float) -> dict:
    """Refund an order — requires an independent human sign-off."""
    ...
```

Reviewers work the queue with `approvals_list()` (pending tool, arguments, caller, tenant, and age) and `approvals_decide(request_id, approve, reason)`. The reviewer's own client id is recorded on every decision.

The part that makes this genuine dual control: **you cannot approve your own call.** `approvals_decide` rejects an *approval* whose reviewer client id equals the original caller — even if that person also holds the approver role. Denying your own request is always allowed; releasing it is not. Install the gate *after* `AuthMiddleware` so each request carries the verified caller identity, and the gate evaluates the tool's own guards first, so an unauthorized caller is rejected immediately and never reaches a reviewer or fills the pending queue.

## Three ways to get a human decision

Different operations need different approval channels, so the same gate accepts three kinds of approver — all built on one shared `ApprovalHandler` protocol:

- **`PendingApprover`** — independent four-eyes review through the admin tools above. Right for financial or destructive operations where a *different* person must sign off.
- **`ElicitationApprover`** — uses MCP elicitation to ask the calling client's own user to confirm (`{"approve": bool, "reason": str}`). Right for destructive-but-personal actions ("really delete this?"). It fails closed: if the transport has no live elicitation-capable session, the call is denied with a clear reason rather than silently allowed.
- **Callbacks and webhooks** — pass any callable, or reuse an existing handler like `WebhookApprovalHandler` to POST an HMAC-signed request to your own approvals service and poll for the verdict. This is how you wire approvals into Slack, PagerDuty, or an internal review app.

Because these approvers slot into the standard middleware chain, they compose with the rest of your production stack — the [Production Features overview](../../mcp/server/production-features.md) shows the gate alongside auth, rate limiting, metrics, and tamper-evident audit in a single pipeline.

## Fail-closed approval is the whole point

An approval system is only as trustworthy as its behavior when things go wrong. Promptise gates are **deny-by-default** across the board:

| Event | Outcome |
|-------|---------|
| Reviewer approves | Call proceeds; approval logged |
| Reviewer denies | `ApprovalDeniedError` (`APPROVAL_DENIED`, not retryable); reviewer id and reason attached |
| No decision within `timeout` | **Denied by default** (`on_timeout="allow"` opts out, explicitly) |
| Reviewer edits the arguments | Denied — the server-side gate can't rewrite bound args, so it won't run something the reviewer didn't actually approve |
| Handler crashes | Denied through the error pipeline |

The default `timeout` is 300 seconds, and `max_pending` (default 100) rejects new calls immediately once the queue is full so a flood can't exhaust reviewers. Every denial surfaces as a structured `APPROVAL_DENIED` error that `AuditMiddleware` records like any other outcome, with the approval request id in the details — so "who approved the 3 a.m. refund" has an answer. Each request also carries `client_id`, `tenant_id`, and the JWT subject and issuer, which is exactly the identity a reviewer needs on a multi-tenant server; see [Multi-Tenancy](../../mcp/server/multi-tenancy.md) for how the tenant is derived and how `approvals_list` shows which tenant a pending call belongs to.

Two honest edges worth knowing before you ship: the `PendingApprover` store is process-local, so pending calls don't survive a restart (they're denied by the timeout) and replicas don't share a queue; and argument modification isn't supported server-side — reviewers approve or deny, they don't rewrite.

## When you shouldn't gate at all

Approval gates add latency and a human dependency, and that isn't free. For a fully autonomous, low-risk workflow — an agent that summarizes tickets, tags emails, or drafts replies for later review — a gate is friction with no payoff. Nothing it does is irreversible, so blocking on a human just slows the loop and trains reviewers to rubber-stamp.

Gate the calls that are expensive to undo: money movement, deletions, external sends, privilege changes. Leave read-only and easily reversible tools ungated. If you're deciding which tools cross that line, the [Production AI Agent Checklist](production-ai-agent-checklist.md) walks through classifying tool risk alongside the other controls a shipping agent needs. The goal isn't maximum approval — it's approval exactly where an autonomous system could otherwise do lasting harm.

## Frequently asked questions

### What is human-in-the-loop approval for AI agents?

It's a control that pauses an agent before it runs a high-stakes tool call and requires a human decision to proceed. Done right, it's enforced on the server that owns the tool — as a `requires_approval=True` declaration plus an approval gate — so the control applies to every client, not just the one agent that happens to render a confirmation prompt.

### Why is server-side approval better than a client-side confirmation prompt?

A client-side prompt only exists in the caller showing it; any other MCP client, script, or scheduled job calls the same tool with no prompt at all. A server-side approval gate makes the requirement a property of the tool itself, so a batch job or a future integration can't bypass it by simply not asking.

### What happens if no one approves in time?

The call is denied by default. Promptise gates fail closed: a decision that doesn't arrive within the timeout, a reviewer edit to the arguments, or a crashed handler all resolve to denial, and the outcome is recorded in the audit chain as `APPROVAL_DENIED`.

## Next steps

Read the [Approval Gates guide](../../mcp/server/approval-gates.md) and gate your first irreversible tool — declare `requires_approval=True`, install `ApprovalGateMiddleware`, and let the deny-by-default semantics do the rest. New to Promptise? Start with the [Quick Start](../../getting-started/quickstart.md), then layer the gate onto a server built from the [Production Features overview](../../mcp/server/production-features.md).
