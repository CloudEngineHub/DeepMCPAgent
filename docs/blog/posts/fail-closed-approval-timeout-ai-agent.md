---
title: "Deny-on-Timeout: Fail-Closed Approval for AI Agents"
description: "The definitive treatment of every deny path, not just the timeout. The invariant is that every ambiguous outcome resolves to denial: on_timeout='deny' by…"
keywords: "fail-closed approval timeout ai agent, deny by default on approval timeout, on_timeout deny mcp approval, handler crash denies approval, max_pending approval backpressure"
date: 2026-07-16
slug: fail-closed-approval-timeout-ai-agent
categories:
  - Approvals & HITL
---

# Deny-on-Timeout: Fail-Closed Approval for AI Agents

A **fail-closed approval timeout** is the single setting that decides what your **AI agent** does when a human reviewer never answers — and for anything that moves money, deletes data, or pages a customer, the only safe answer is *don't run it*. Most human-in-the-loop wiring gets the happy path right: a reviewer clicks approve, the tool fires; they click deny, it doesn't. The dangerous cases are the ambiguous ones — the reviewer is asleep, the approval channel is down, the reviewer edited the request, or a thousand pending calls have buried the queue. What happens *then* is the whole game, and it is exactly where home-grown approval logic tends to leave a hole. This post is the complete treatment of every deny path in Promptise Foundry's server-side gate, and the one invariant that ties them together: **every ambiguous outcome resolves to a denial**.

## The action that runs because nobody said no

Picture a `wire_transfer` tool guarded by an approval step. At 3 a.m. an autonomous agent — a scheduled trigger, a batch job, a second agent in a delegation chain — decides to call it. There is no human at a console. The approval request goes out over your pager integration.

Now walk the ambiguity. The on-call engineer's phone is on Do Not Disturb, so no decision arrives. Or the pager webhook throws a 500 and your handler raises. Or the engineer *does* look, thinks "$5,000 is too much, make it $50," and submits an edit. Or a misconfigured loop has already fired 900 approval requests and the reviewer can't find the real one. In each case a naïve implementation has an undefined answer — and "undefined" in production almost always resolves to *the tool ran anyway*, because the path of least resistance in code is to fall through. The money moves. The audit log shows a clean, successful call. The only signal is a support ticket.

The failure isn't that approval was skipped. It's that approval reached a state nobody wrote a rule for, and the default was permissive. A safe approval system inverts that: the default for *anything that isn't an explicit, unmodified "yes"* is **no**.

## The invariant: every ambiguous outcome is a denial

Promptise makes that inversion structural. When you install `ApprovalGateMiddleware`, four distinct failure modes all collapse to the same denial, and three of them are the framework's *default* — you don't opt into safety, you'd have to opt out:

| Ambiguous outcome | Result | Default? |
|---|---|---|
| No decision within `timeout` | `APPROVAL_DENIED` — "denied by default" | Yes (`on_timeout="deny"`) |
| Handler raises an exception | Non-retryable error; tool body never runs | Yes (fail through error pipeline) |
| Reviewer returns `modified_arguments` | `APPROVAL_DENIED` — gate can't rewrite bound args | Yes |
| Pending queue is full | `APPROVAL_DENIED` immediately, no reviewer paged | Yes (`max_pending`) |

The snippet below runs with nothing but `pip install promptise` — no API key, no network. It builds a fresh gated server for each failure mode and shows all four fail closed, then confirms a clean approval still runs the body:

```python
import asyncio

from promptise.mcp.server import (
    ApprovalGateMiddleware,
    AuthMiddleware,
    JWTAuth,
    MCPServer,
    PendingApprover,
    TestClient,
)
from promptise.approval import ApprovalDecision

auth = JWTAuth(secret="dev-secret")
hdr = lambda tok: {"authorization": f"Bearer {tok}"}


def build(gate):
    """A fresh ops server with one high-stakes, gated tool."""
    server = MCPServer(name="ops")
    server.add_middleware(AuthMiddleware(auth))          # identity first
    server.add_middleware(gate)

    @server.tool(auth=True, roles=["operator"], requires_approval=True)
    async def wire_transfer(to_account: str, amount: float) -> dict:
        """Send money — a human must approve before this body runs."""
        return {"to": to_account, "amount": amount, "status": "SENT"}

    return server


async def main():
    op = auth.create_token({"sub": "op-1", "roles": ["operator"]})
    args = {"to_account": "ACME-1", "amount": 5000.0}

    # (1) on_timeout='deny' (the default): a decision that never arrives is refused.
    async def never(request):
        await asyncio.sleep(3600)                       # reviewer asleep at 3 a.m.

    gate = ApprovalGateMiddleware(never, timeout=1.0, on_timeout="deny")
    out = await TestClient(build(gate)).call_tool("wire_transfer", args, headers=hdr(op))
    print("(1) timeout   :", out[0].text[:64])           # APPROVAL_DENIED, "denied by default"

    # (2) Handler crash: an exception in the approver denies; the body never runs.
    def crash(request):
        raise RuntimeError("pager integration is down")

    gate = ApprovalGateMiddleware(crash, timeout=5)
    out = await TestClient(build(gate)).call_tool("wire_transfer", args, headers=hdr(op))
    print("(2) crash     :", out[0].text[:64])           # INTERNAL_ERROR, retryable: false

    # (3) A reviewer edit denies — the server gate cannot rewrite bound args.
    def edit(request):
        return ApprovalDecision(approved=True, reviewer_id="mgr",
                                modified_arguments={"amount": 1.0})

    gate = ApprovalGateMiddleware(edit, timeout=5)
    out = await TestClient(build(gate)).call_tool("wire_transfer", args, headers=hdr(op))
    print("(3) edited    :", out[0].text[:64])           # APPROVAL_DENIED

    # (4) max_pending backpressure: a full queue refuses new calls immediately.
    approver = PendingApprover(max_pending=1)
    gate = ApprovalGateMiddleware(approver, timeout=30)
    client = TestClient(build(gate))
    parked = asyncio.create_task(                         # first call parks, undecided
        client.call_tool("wire_transfer", args, headers=hdr(op))
    )
    await asyncio.sleep(0.1)
    out = await client.call_tool("wire_transfer", args, headers=hdr(op))
    print("(4) full queue:", out[0].text[:64])           # APPROVAL_DENIED, "queue is full"
    parked.cancel()

    # A clean approval of untouched arguments runs the body.
    ok = ApprovalGateMiddleware(
        lambda r: ApprovalDecision(approved=True, reviewer_id="mgr"), timeout=5
    )
    out = await TestClient(build(ok)).call_tool(
        "wire_transfer", {"to_account": "ACME-1", "amount": 10.0}, headers=hdr(op)
    )
    print("    approved  :", out[0].text)                 # {"status": "SENT"}


asyncio.run(main())
```

Line **(1)** returns `APPROVAL_DENIED` with the message *"timed out after 1s and was denied by default."* `on_timeout="deny"` is the default; you'd have to write `on_timeout="allow"` explicitly, and only for a genuinely low-risk tool, to get the other behavior. Line **(2)** shows a crashing handler surface as a non-retryable `INTERNAL_ERROR` (and log a traceback) — the point is that `call_next` is never invoked, so the wire transfer *body never executes*. A broken pager can't launder into a completed transfer. Line **(3)** denies a reviewer's edit: the gate presented specific arguments and cannot rewrite the already-bound call, so running the *original* $5,000 the reviewer just rejected is off the table — it fails closed. Line **(4)** is backpressure: with `max_pending=1`, the second call is refused the instant the queue is full, before any reviewer is touched. Only the final clean approve-as-presented returns `{"status": "SENT"}`.

The [Approval Gates guide](../../mcp/server/approval-gates.md) documents each row of that table as first-class semantics, including the detail that the gate evaluates the tool's own guards *before* asking a human — so an unauthorized caller is rejected with `ACCESS_DENIED` and never even generates a request to flood the queue behind `max_pending`.

## max_pending: a flood can't exhaust your reviewers

The queue-full path deserves its own note, because it closes an availability hole the other three don't. Timeout, crash, and edit-denial each protect a *single* call. `max_pending` protects the *reviewers*. Without a bound, a runaway agent — or an attacker who got past guards on an intentionally unauthenticated tool — can enqueue thousands of pending approvals, and a human drowning in noise is a human who eventually rubber-stamps the wrong one. `PendingApprover(max_pending=100)` caps the waiting set; call 101 is denied immediately with *"pending approval queue is full,"* no reviewer paged, no memory growth. It's the same fail-closed instinct applied to the review capacity itself: when the safe resource is exhausted, deny rather than degrade.

Agent-side approval carries the same idea for Promptise-driven agents rather than raw MCP clients — `ApprovalPolicy` exposes its own `timeout`, `on_timeout="deny"`, and `max_pending` knobs, documented in the [agent-side Approval guide](../../core/approval.md). One notable asymmetry: the agent-side policy *does* honor `modified_arguments`, because it re-binds arguments before dispatch and can guarantee the edited set is what runs. The server-side gate deliberately refuses to, because it can't make that guarantee for a call whose arguments are already bound — so it denies. Same protocol, honest about where edit-then-run is safe.

## What other frameworks do today

To be fair about the delta: human-in-the-loop is not a Promptise invention, and the major frameworks all ship a real primitive for it. The precise difference is what happens in the ambiguous states, and whether *you* have to hand-wire the safe default.

- **LangGraph** has genuinely first-class HITL via `interrupt()` plus its checkpointer-backed `Command(resume=...)` flow — a durable pause that survives process restarts, which is more than the gate here does. But `interrupt()` has **no built-in deny-on-timeout**: the graph stays paused until an explicit resume arrives, so an unreviewed action simply *sits*, indefinitely, rather than resolving to a denial. If you want "auto-deny after 5 minutes," you build the timer, the cancellation, and the denial branch yourself.
- **CrewAI** offers `human_input=True` on a `Task`, which prompts for review on the console. It's real and easy — but it **blocks the running process** waiting on `input()`; there is no defined timeout policy. On a headless 3 a.m. trigger with no console attached, that's a hang, not a safe denial.
- **AutoGen** wires HITL through `UserProxyAgent` with `human_input_mode="ALWAYS"`, which likewise **requests console input and blocks** the conversation. Same shape: excellent for an interactive session, undefined for the unattended one.

None of this means those frameworks *can't* fail closed — with a timer, a try/except, an argument-equality check, and a queue bound, you can get there in any of them. The exact delta is that **none ships a single standard policy that denies on timeout, on handler crash, and on argument modification together, with a bounded queue on top** — and in each of them the safe behavior is code *you* remember to write, so a forgotten branch degrades open. Promptise makes fail-closed the *default* across all four paths: you configure `timeout` and, if anything, opt *out* of denial for a specific low-risk tool. For where each framework's HITL actually executes, see [LangGraph vs CrewAI vs AutoGen: Where HITL Runs](langgraph-vs-crewai-vs-autogen-human-in-the-loop.md); for why a *declared* approval can't be silently dropped in the first place, see [An MCP Approval Gate That Refuses to Ship Ungated](build-time-enforced-approval-gate-mcp.md).

## Frequently asked questions

### What does `on_timeout="deny"` actually guarantee?

That a call which receives no decision within `timeout` seconds is refused with a non-retryable `APPROVAL_DENIED` error, and the tool body never runs. It's the default, enforced with `asyncio.wait_for`, so a slow or absent reviewer can't leave the call hanging or let it fall through to execution. `on_timeout="allow"` is the explicit, documented escape hatch for tools where waiting is riskier than acting — reserve it for genuinely low-stakes operations.

### If my approval handler crashes, does the tool run?

No. An exception from the handler propagates through the error pipeline as a non-retryable error (an `INTERNAL_ERROR`, with a logged traceback) *before* `call_next` is invoked, so the tool body is never reached. A broken pager, a network blip talking to your approval service, a bug in a custom handler — all of them deny. The failure is loud in your logs and safe in its outcome.

### Why deny a reviewer's edited arguments instead of running the edit?

Because the server-side gate binds the exact arguments it presented and cannot substitute a new set into an already-bound call. Running the *original* arguments after a reviewer changed them would execute something they explicitly rejected, so the gate returns `APPROVAL_DENIED`. If you need reviewers to edit-then-run, that belongs in agent-side `ApprovalPolicy`, which re-binds before dispatch and supports `modified_arguments` safely.

### Won't `max_pending` reject legitimate calls under load?

It rejects *new* calls once the configured number are already waiting — which is the point: an unbounded pending set is how a flood exhausts your reviewers or your memory. The bound (default 100) should exceed your real concurrent-review capacity; calls beyond it are denied immediately rather than piling up invisibly. Pair the gate with guards so unauthorized callers are rejected before they ever consume a slot.

### Is this the same as the build-time "refuses to ship ungated" invariant?

No — they're complementary. The build-time invariant guarantees a declared approval is *wired to an enforcer at all* (an ungated `requires_approval` tool refuses to build). This post is about what that enforcer does once it's running: every ambiguous *runtime* outcome resolves to denial. You need both — an enforcer that exists, and an enforcer that fails safe.

## Next steps

Set `timeout` and keep `on_timeout="deny"`, then prove it: with `TestClient`, call a gated tool whose handler never answers and confirm you get `APPROVAL_DENIED` — an un-reviewed action refused, not left running — *before* you ship. Run the snippet above as-is; it exercises all four deny paths offline in seconds. From there, read the [Approval Gates guide](../../mcp/server/approval-gates.md) to wire a `PendingApprover` four-eyes queue or an HMAC-signed webhook handler, tune `max_pending` to your review capacity, and cross-check the [agent-side Approval guide](../../core/approval.md) for the edit-then-run cases the server gate deliberately leaves to the agent. If you're still deciding where approval should live at all, [LangGraph vs CrewAI vs AutoGen: Where HITL Runs](langgraph-vs-crewai-vs-autogen-human-in-the-loop.md) maps the trade-offs end to end.
