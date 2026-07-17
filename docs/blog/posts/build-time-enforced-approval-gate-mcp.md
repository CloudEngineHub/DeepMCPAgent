---
title: "An MCP Approval Gate That Refuses to Ship Ungated"
description: "The cluster anchor, deliberately re-scoped off the well-worn 'client-side is bypassable' premise (already owned by the existing human-in-the-loop-approval…"
keywords: "build-time enforced approval gate mcp, requires_approval refuses to build, approval declaration that must enforce, argument tamper denial mcp approval, server-enforced approval invariant"
date: 2026-07-16
slug: build-time-enforced-approval-gate-mcp
categories:
  - Approvals & HITL
---

# An MCP Approval Gate That Refuses to Ship Ungated

A **build-time enforced approval gate MCP** servers can't quietly skip is the difference between an approval you *declared* and an approval that actually *fires* — and that gap is where most human-in-the-loop wiring silently fails. The usual argument for server-side approval is that client-side prompts are bypassable; that case is already made, and it is not this post. The sharper question is the one that bites teams six months into production: you marked a tool as needing sign-off, so why did it run unattended anyway? Almost always because the approval lived somewhere that could be *forgotten* — a branch in the driver code, a flag on a task, a node you meant to add — and nothing failed when it wasn't there. This is about the three enforcement invariants that make a *declared* approval mechanically un-skippable.

<!-- more -->

## The approval you wrote that never fires

Picture a `refund` tool. Someone did the right thing and marked it as high-stakes: it needs a human to sign off before money moves. Months later it fires at 3 a.m. with no reviewer in sight. Nobody removed the approval. Nobody bypassed a prompt. The approval was declared in one place and *enforced* in another, and the two drifted apart without a single error.

That drift has a specific shape. In most agent stacks the "please pause for a human" logic lives in the code that *drives* the agent — the graph that routes to the tool, the loop that dispatches calls, the task definition. The tool itself carries no obligation. So the failure modes are all omissions that no compiler and no test suite catches:

- A new caller — a batch job, a scheduled trigger, a second agent — reaches the same tool through a path that never had the pause wired in.
- A refactor moves the dispatch logic and the interrupt gets dropped.
- The tool is mounted into a second server, or exposed to a second client, and the approval simply doesn't come along for the ride.

None of these throw. The tool runs; the money moves; the audit log shows a successful call. The only signal is a support ticket. An approval that can silently not-fire is arguably worse than no approval at all, because it manufactures false confidence: the code *looks* governed.

## The invariant: `requires_approval=True` refuses to build ungated

Promptise Foundry moves the enforcement point to the declaration itself. You mark the tool, and the *server* takes on the obligation to gate it. The mechanism is deliberately blunt: if any tool declares `requires_approval=True` and no `ApprovalGateMiddleware` is installed to enforce it, the server **refuses to build**. Not a warning, not a log line — a `RuntimeError` before the server ever accepts a request. A declared-but-unenforced approval literally cannot ship.

The snippet below runs with nothing but `pip install promptise` — no API key, no network — and exercises all three invariants at once. Watch it refuse first, then enforce:

```python
import asyncio

from promptise.mcp.server import (
    ApprovalGateMiddleware,
    AuthMiddleware,
    JWTAuth,
    MCPServer,
    TestClient,
)
from promptise.approval import ApprovalDecision

auth = JWTAuth(secret="dev-secret")


def build_billing(*, gate_handler=None):
    """A fresh billing server. gate_handler=None declares the approval
    requirement but installs NO gate — an unenforceable declaration."""
    server = MCPServer(name="billing")
    server.add_middleware(AuthMiddleware(auth))          # identity first
    if gate_handler is not None:
        server.add_middleware(ApprovalGateMiddleware(gate_handler, timeout=5))

    @server.tool(auth=True, roles=["clerk"], requires_approval=True)
    async def refund(order_id: str, amount: float) -> dict:
        """Refund an order — a human must approve before this body runs."""
        return {"order_id": order_id, "amount": amount, "status": "refunded"}

    return server


async def main():
    clerk = auth.create_token({"sub": "clerk-1", "roles": ["clerk"]})
    stranger = auth.create_token({"sub": "intruder", "roles": []})
    hdr = lambda tok: {"authorization": f"Bearer {tok}"}

    # (1) A declared-but-ungated approval CANNOT ship: no gate -> refuses.
    try:
        await TestClient(build_billing()).call_tool(
            "refund", {"order_id": "A-1", "amount": 10.0}, headers=hdr(clerk)
        )
    except RuntimeError as exc:
        print("(1) build refused:", str(exc)[:72], "...")

    # (3) Guards run BEFORE the gate: an unauthorized caller is rejected
    #     without ever paging a reviewer.
    approve_all = lambda req: ApprovalDecision(approved=True, reviewer_id="mgr")
    gated = build_billing(gate_handler=approve_all)
    denied = await TestClient(gated).call_tool(
        "refund", {"order_id": "A-1", "amount": 10.0}, headers=hdr(stranger)
    )
    print("(3) no-role caller:", denied[0].text[:60])   # ACCESS_DENIED

    # (2) A reviewer-edited argument set is DENIED, not silently run.
    def tamper(req):
        return ApprovalDecision(
            approved=True, reviewer_id="mgr", modified_arguments={"amount": 1.0}
        )

    tampered = build_billing(gate_handler=tamper)
    out = await TestClient(tampered).call_tool(
        "refund", {"order_id": "A-1", "amount": 5000.0}, headers=hdr(clerk)
    )
    print("(2) tampered args:", out[0].text[:60])        # APPROVAL_DENIED

    # A clean approval of untouched arguments runs the body.
    ok = await TestClient(gated).call_tool(
        "refund", {"order_id": "A-1", "amount": 10.0}, headers=hdr(clerk)
    )
    print("    approved:", ok[0].text)


asyncio.run(main())
```

The first call raises `RuntimeError: Tool 'refund' declares requires_approval=True but no ApprovalGateMiddleware is installed`. That is the whole point: you cannot construct — or even *test*, since `TestClient` enforces the same invariant — a server that promises approval and doesn't deliver it. The [Approval Gates guide](../../mcp/server/approval-gates.md) documents the full rule: a gate at the server level or the router level satisfies it, but *some* gate must exist, and the framework won't auto-insert one because it can't decide *who* approves on your behalf.

## A reviewer's edit is denied, not quietly run

The second invariant is subtler and it is where a lot of home-grown approval code has a hole. A gate binds the *specific* arguments it presented to the reviewer. If a reviewer looks at a $5,000 refund and decides "$1 is more reasonable," the decision carries `modified_arguments`. The server-side gate cannot rewrite the already-bound call — so it does the only safe thing: it **denies**, surfacing `APPROVAL_DENIED`, rather than executing the *original* $5,000 that the reviewer explicitly rejected.

Run the snippet and line (2) prints exactly that. This closes a tamper class most people never consider: an approval flow that shows the reviewer one thing and runs another. Approve-or-deny on the exact arguments presented is the only decision the gate honors; anything else fails closed. (Argument rewriting is a real capability — Promptise's [agent-side approval](../../core/approval.md) supports it because it re-binds before dispatch — but the server-side gate deliberately refuses to, because it can't guarantee the substitution is what ran.)

## Guards run before the gate, so unauthorized callers never reach a reviewer

The third invariant orders the pipeline correctly. A gated tool almost always also has access control — `auth=True`, a required role, a tenant guard. Naively, a middleware gate would fire *before* the innermost guard check, meaning an unauthorized or unauthenticated caller could trigger a real human approval request with attacker-chosen arguments. That is a denial-of-service on your reviewers: fill the bounded pending queue with junk, and legitimate approvals starve.

Promptise evaluates the tool's own guards *first*. In the snippet, the `stranger` token has no `clerk` role, so line (3) returns `ACCESS_DENIED` immediately — the reviewer is never paged, the pending queue is never touched. Deny first, then ask. On a multi-tenant deployment this matters even more: pair `requires_approval=True` with `auth=True` and tenant guards so every request that *does* reach a reviewer already carries a verified `client_id`, `tenant_id`, and JWT subject. The [Secure Multi-Tenant Platform guide](../../guides/secure-multi-tenant-platform.md) wires the gate alongside per-tenant identity end to end, and the [Production Features overview](../../mcp/server/production-features.md) shows it composed with auth, rate limiting, tamper-evident audit, and metrics in one pipeline.

Together the three invariants deliver a single guarantee: **an approval you declare is an approval that is enforced** — on the exact arguments, for authorized callers, or the server doesn't start.

## What other frameworks do today

To be fair about the delta, human-in-the-loop is not a Promptise invention, and every major framework ships a real mechanism for it. The precise difference is *where enforcement lives* and *what happens when you forget it*.

- **LangGraph** has first-class HITL via `interrupt()` and its checkpointer-backed `Command(resume=...)` flow — a genuinely powerful primitive that can pause and resume durable graph state. But the interrupt is placed *in the graph node* that drives the tool. If a node calls the tool without the interrupt, or a new edge routes around it, the tool runs; nothing about the tool declaration forces the pause, and the build succeeds either way.
- **CrewAI** has `human_input=True` on a `Task`, which prompts for review of that task's output. It's real and easy to use, but it's a flag on the *task definition* in the driver code; a tool invoked outside that task, or a task where the flag was omitted, carries no obligation.
- **AutoGen** wires HITL through `UserProxyAgent` and `human_input_mode="ALWAYS"` — approval is a property of the *conversation topology* you assemble. A different entry point that reaches the same tool through another agent doesn't inherit it.
- **Pydantic AI** can mark a tool as requiring approval (its deferred-tool / approval-required pattern) — but it resolves that approval *app-side*: the calling application detects the requirement and handles it before re-running. The declaration signals intent; the surrounding app code is still responsible for honoring it.

None of this means those frameworks *can't* enforce approval — with discipline you can wire any of them correctly. The exact delta is that in each one the pause lives in the code that drives the agent, so **forgetting to add it silently ships an ungated tool and nothing fails at build**. Promptise makes the declaration itself the enforcement point: an ungated `requires_approval` tool refuses to build, so you cannot ship an approval that quietly never fires. For a side-by-side of exactly where each framework's HITL executes, see [LangGraph vs CrewAI vs AutoGen: Where HITL Runs](langgraph-vs-crewai-vs-autogen-human-in-the-loop.md); for the related question of *who* is allowed to approve, [Should an AI Agent Approve Its Own Action?](separation-of-duties-for-ai-agents.md) covers the separation-of-duties rule that stops a caller from signing off on its own request.

## Frequently asked questions

### What does "build-time enforced" actually mean here?

It means the check happens when you assemble the server, not when a request arrives. If a tool declares `requires_approval=True` and no `ApprovalGateMiddleware` is present at the server or router level, constructing the server raises `RuntimeError`, and `TestClient` raises the same on first call. There is no code path in which a declared-but-ungated approval reaches production — the failure is loud and early, not a silent unattended execution in month six.

### Why deny a reviewer's edited arguments instead of running them?

Because the server-side gate binds the arguments it showed the reviewer, and it can't guarantee that a rewritten set is what actually executes. Running the *original* arguments after a reviewer changed them would execute something they explicitly rejected, so the gate returns `APPROVAL_DENIED`. If you need reviewers to edit-then-run, that belongs in agent-side `ApprovalPolicy`, which re-binds arguments before dispatch.

### Won't the gate spam my reviewers if an attacker hammers the tool?

No — the tool's guards are evaluated before the gate. An unauthenticated or unauthorized caller is rejected with `ACCESS_DENIED` and never generates an approval request, so the bounded pending queue (default 100) can't be flooded by callers who'd fail authorization anyway. Pair `requires_approval=True` with `auth=True` and role/tenant guards so there is always an identity to check first.

### Is this the same as the client-side-is-bypassable argument?

No. That argument (a rogue MCP client skips a confirmation prompt) is about *runtime* bypass and is covered elsewhere. This post is about *declaration drift*: an approval you correctly declared that never gets wired to an enforcer. The build-time invariant closes that specific hole regardless of which client calls the tool.

## Next steps

Declare `requires_approval=True` on your riskiest tool and try to build the server *without* a gate — watch it refuse — then read the [Approval Gates guide](../../mcp/server/approval-gates.md) to wire enforcement in under 15 lines. From there, add an independent-reviewer `PendingApprover` or an HMAC-signed webhook handler, and fold the gate into a full production pipeline using the [Production Features overview](../../mcp/server/production-features.md). If you're running multiple customers, do it on the identity foundation in the [Secure Multi-Tenant Platform guide](../../guides/secure-multi-tenant-platform.md) so every approval request a reviewer sees carries a verified caller and tenant.
