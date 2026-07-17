---
title: "Which Agent Frameworks Actually Enforce Tool Approval?"
description: "Every framework advertises 'human-in-the-loop,' but the question that matters is where the check actually runs. This side-by-side maps exactly where…"
keywords: "agent framework tool approval comparison, which agent framework enforces tool approval, langgraph interrupt vs approval gate, crewai human_input approval, autogen userproxyagent approval, pydantic ai deferred tool approval"
date: 2026-07-16
slug: agent-framework-tool-approval-comparison
categories:
  - Comparisons
---

# Which Agent Frameworks Actually Enforce Tool Approval?

Any useful **agent framework tool approval comparison** has to start with the question the feature checklists quietly skip: not *does* the framework support human-in-the-loop, but *where does the approval check physically execute* — and what happens the day a second client calls the same tool. Every major framework ticks the "supports HITL" box, and every one of them ships a real mechanism. They differ on one property that decides your architecture: the location of the enforcement point. This post maps that property precisely, framework by framework, and shows the row that sits somewhere structurally different — on the tool itself.

<!-- more -->

## The buyer's question: where does the check actually run?

"Human-in-the-loop" is not one feature. It's a pause, and a pause has to live *somewhere*. In the mainstream agent frameworks, that somewhere is the process that drives the agent: the graph executor, the crew loop, the conversation topology, or your own application code. For a single self-contained app, that's exactly right — there's one caller, and the pause lives with it.

The gap opens the moment the gated tool becomes a shared endpoint. Expose `refund` or `delete_records` as an MCP tool so a second client can use it — Claude Desktop, Cursor, a teammate's script, a nightly cron job — and ask the one question that separates a demo from a control: *if I point a different, headless MCP client at the same tool, does the approval still fire?* If the pause lives in the caller, the answer is no. The control was never a property of the tool; it was a property of the process that happened to be driving the agent, and the new client doesn't go through that process. Nothing malicious happened. The gate just wasn't where the risk was. That question — and who's left holding the enforcement — is the same theme running through the [Enterprise-Ready Agent Framework Checklist](enterprise-ready-agent-framework-checklist.md): the capability exists, but the wiring is left to you.

## What each framework does today, precisely

Let's be fair, because none of these is a client-side "are you sure?" hack — they're all real, working controls. The delta is purely *where enforcement lives* and *what the framework guarantees when you forget to add it.*

- **LangGraph** has genuinely first-class HITL: `interrupt()` (and `interrupt_before` on nodes) with a checkpointer that persists graph state, resumed by *your application* calling `Command(resume=...)`. The pause is real and, notably, **durable across restarts** — a strength worth naming honestly. But it is placed *inside a graph node*. If a node reaches the tool without the interrupt, or a new edge routes around it, the tool runs and the build still succeeds.
- **CrewAI** uses `human_input=True` on a `Task`. When the task produces output, the crew prompts on the console for human feedback and folds it back in. It's easy and real — but it's a flag on the *task definition* in your driver code, and it reviews the task's output rather than gating an arbitrary tool call at the boundary.
- **AutoGen** routes approval through a `UserProxyAgent` with `human_input_mode` (`ALWAYS`, `TERMINATE`, `NEVER`). Approval becomes a property of the *conversation topology* you assemble; a different entry point that reaches the same underlying tool through another agent doesn't inherit it.
- **Pydantic AI** models a tool that needs sign-off as a deferred/approval-required tool call: the requirement is surfaced back to the *application* driving the agent, which resolves it and re-runs. The declaration signals intent; your surrounding app code is responsible for honoring it.

Now the matrix that actually matters — not "has HITL" (they all do), but where the check sits and which enforcement guarantees ship with it:

| Framework | HITL mechanism | Enforcement point | Fires for a *second* MCP client? | Built-in fail-closed timeout | Argument-tamper denial | Self-approval (four-eyes) rejection |
|---|---|---|---|---|---|---|
| LangGraph | `interrupt()` + checkpointer, `Command(resume=...)` | Graph runtime in your process | No — pause is in the node | Not a built-in policy | App-defined | App-defined |
| CrewAI | `human_input=True` on a `Task` | Crew loop (console prompt) | No — prompt is in the crew process | Not a built-in policy | App-defined | App-defined |
| AutoGen | `UserProxyAgent(human_input_mode=...)` | Agent conversation loop | No — proxy is in the process | Not a built-in policy | App-defined | App-defined |
| Pydantic AI | Deferred / approval-required tool call | Your app code, resolved app-side | No — resolved by the one app | Not a built-in policy | App-defined | App-defined |
| Promptise Foundry | `@server.tool(requires_approval=True)` + `ApprovalGateMiddleware` | The MCP server that owns the tool | **Yes — every client traverses it** | **Yes (deny by default)** | **Yes (denied)** | **Yes (`PendingApprover`)** |

To be scrupulously fair: the "App-defined" cells are not failures — you *can* wire a timeout, an argument check, or a two-person rule into any of these frameworks with enough discipline. The point of the comparison is that none of the four ship those as a structural policy attached to the tool, and all four locate the check in code the developer maintains, so *forgetting* it silently ships an ungated tool with nothing failing at build. For a deeper walk through exactly where each framework's pause physically executes, see [LangGraph vs CrewAI vs AutoGen: Where HITL Runs](langgraph-vs-crewai-vs-autogen-human-in-the-loop.md).

## The Promptise row: the gate lives on the tool

Promptise Foundry moves the enforcement point to the MCP server that owns the tool. You declare `requires_approval=True` on the tool and install one middleware; the gate then sits in the server pipeline, in front of the handler, for *every* client that ever calls it. This is the [Approval Gates](../../mcp/server/approval-gates.md) mechanism, and the snippet below runs end to end with nothing but `pip install promptise` — no API key, no network. It builds one gated `wire_transfer` tool and calls it from two independent clients:

```python
import asyncio
import json

from promptise.mcp.server import (
    ApprovalGateMiddleware,
    AuthMiddleware,
    JWTAuth,
    MCPServer,
    TestClient,
)
from promptise.approval import ApprovalDecision

auth = JWTAuth(secret="dev-secret")

# One server, one gated tool. The gate lives in the SERVER pipeline, so it
# fires for every client that ever reaches wire_transfer — your agent, a
# headless cron job, Claude Desktop, a teammate's script. No client is trusted
# to pause on its own.
server = MCPServer(name="treasury")
server.add_middleware(AuthMiddleware(auth))  # identity first


async def review(request):
    """Stand-in reviewer/policy. Swap for PendingApprover (four-eyes) or a
    signed WebhookApprovalHandler in production."""
    amount = request.arguments["amount"]
    if amount < 1000:
        return ApprovalDecision(approved=True, reviewer_id="treasury-lead")
    if amount > 5000:
        # Reviewer tries to CAP the transfer by editing the amount. The
        # server-side gate refuses to run arguments nobody approved: DENIED.
        return ApprovalDecision(
            approved=True, reviewer_id="treasury-lead",
            modified_arguments={"amount": 5000.0},
        )
    return ApprovalDecision(
        approved=False, reviewer_id="treasury-lead", reason="over auto-approve limit",
    )


server.add_middleware(ApprovalGateMiddleware(review, timeout=5))


@server.tool(auth=True, roles=["operator"], requires_approval=True)
async def wire_transfer(to_account: str, amount: float) -> dict:
    """Move money — no client may run this without an approval decision."""
    return {"to": to_account, "amount": amount, "status": "sent"}


def outcome(result):
    """Server serializes denials into a structured error JSON — read the code."""
    payload = json.loads(result[0].text)
    if "error" in payload:
        return payload["error"]["code"]      # e.g. APPROVAL_DENIED
    return payload["status"]                 # e.g. sent


async def main():
    op = auth.create_token({"sub": "op-1", "roles": ["operator"]})
    hdr = {"authorization": f"Bearer {op}"}

    # Two INDEPENDENT MCP clients against the same server: think "your agent"
    # and "a headless nightly job". Both traverse the identical gate.
    primary_agent = TestClient(server)
    headless_cron = TestClient(server)

    small = await primary_agent.call_tool(
        "wire_transfer", {"to_account": "ACME", "amount": 50.0}, headers=hdr
    )
    print("agent, $50   ->", outcome(small))       # sent — approved, body ran

    over = await headless_cron.call_tool(
        "wire_transfer", {"to_account": "ACME", "amount": 2000.0}, headers=hdr
    )
    print("cron,  $2000 ->", outcome(over))         # APPROVAL_DENIED — same gate

    tampered = await primary_agent.call_tool(
        "wire_transfer", {"to_account": "ACME", "amount": 9000.0}, headers=hdr
    )
    print("agent, $9000 ->", outcome(tampered))     # APPROVAL_DENIED — edited args


asyncio.run(main())
```

Running it prints:

```text
agent, $50   -> sent
cron,  $2000 -> APPROVAL_DENIED
agent, $9000 -> APPROVAL_DENIED
```

The load-bearing detail is that `primary_agent` and `headless_cron` are two *separate* clients, and both hit the identical gate — because the gate is bound to the tool, not to whoever opened the connection. Serve the same `server` object over stdio, streamable HTTP, or SSE and the gate is unchanged; every MCP client that discovers `wire_transfer` inherits the approval requirement automatically. That is the row the four competitors can't reproduce without rebuilding their control at the tool boundary.

## Enforcement, not just presence: timeout, tamper, four-eyes

"Supports approval" and "enforces approval" diverge exactly at the edge cases, and the gate is fail-closed at every one of them:

- **Fail-closed timeout.** No decision within `timeout` (default 300s) is **denied by default**. Allowing on timeout is an explicit opt-out (`on_timeout="allow"`), never the accident. A reviewer who's asleep can't turn into an unattended execution.
- **Argument-tamper denial.** If a reviewer edits the arguments — the `modified_arguments={"amount": 5000.0}` branch above — the server-side gate **denies** rather than run something nobody signed off on. It can't rewrite the already-bound call, so it refuses; you saw it print `APPROVAL_DENIED` for the $9,000 transfer. (Edit-then-run *is* supported one layer up, in the agent-side [`ApprovalPolicy`](../../core/approval.md), which re-binds arguments before dispatch.)
- **Four-eyes separation of duties.** Swap the `review` callable for `PendingApprover`, and reviewers act through two role-guarded admin tools — `approvals_list()` and `approvals_decide(request_id, approve, reason)`. `approvals_decide` rejects an *approval* whose reviewer `client_id` equals the original caller: you cannot approve your own call, even holding the approver role.
- **Refuses to ship ungated.** If any tool declares `requires_approval=True` and no `ApprovalGateMiddleware` is installed, the server **raises at build time** and `TestClient` raises on call. A declared-but-unenforced approval literally cannot ship — the design rationale is in [An MCP Approval Gate That Refuses to Ship Ungated](build-time-enforced-approval-gate-mcp.md).

One honest edge, to keep the comparison balanced: the `PendingApprover` pending store is **process-local**. Calls that outlive the gate timeout are denied, and replicas don't share a queue — a distributed backend is on the roadmap. This is precisely the dimension where LangGraph's checkpointer-backed `interrupt()` is genuinely ahead: its paused state is durable across restarts. The two designs optimize for different risks — durable single-app pause versus a gate that holds for every client — and Promptise keeps *both* models available.

## When a tool-boundary gate actually earns its keep

The framing that keeps this fair: if your risky tool only ever runs inside one application's process, in-process HITL is a *complete* control, and often the simpler, lower-latency choice. Promptise even mirrors that model — agent-side [`ApprovalPolicy`](../../core/approval.md) governs a Promptise agent's *own* tool calls exactly like LangGraph's `interrupt()`, CrewAI's `human_input`, or AutoGen's `UserProxyAgent`. Reach for the server-side gate specifically when the tool is — or will become — a shared MCP endpoint that more than one client can reach.

That's the same reason the enforcement point matters for tenancy: a control that lives in one caller can't enforce a rule that has to hold across many. When several customers or agents share the same tool, you want approval — and identity, and rate limits — bound to the tool, not to a caller you don't control. We work through the honest version of that story for a neighboring capability in [Does LangChain Support Multi-Tenancy? The Honest Answer](does-langchain-support-multi-tenancy.md), and the broader design philosophy — make the safety property structural, not a courtesy of the caller — is laid out in [Why Promptise Foundry](../../getting-started/why-promptise.md).

## Frequently asked questions

### Which agent framework actually enforces tool approval for any client?

All of LangGraph, CrewAI, AutoGen, and Pydantic AI enforce approval for the caller that drives the agent, because that's where their pause lives. Only a tool-boundary gate — Promptise's `@server.tool(requires_approval=True)` + `ApprovalGateMiddleware` — enforces it for *every* MCP client that calls the tool, including a second agent, a headless job, or a third-party client, without any client-side wiring.

### How is LangGraph's `interrupt()` different from an approval gate?

`interrupt()` pauses durable graph state inside the graph runtime and resumes when your app sends `Command(resume=...)`. It's a real, restart-durable control — but it fires only when execution flows through that node. A different MCP client calling the same underlying tool never enters your graph, so the pause doesn't apply. An approval gate binds the check to the tool instead, so the enforcement point doesn't depend on which client called.

### Does CrewAI `human_input` or AutoGen `UserProxyAgent` gate a raw tool call?

They gate within their own loop: CrewAI's `human_input=True` prompts for feedback on a task's output inside the crew process, and AutoGen's `UserProxyAgent` asks via `human_input_mode` inside the conversation. Both are real HITL, but the check is a property of the task/conversation topology in your process — not of the tool — so it doesn't travel when the tool is exposed to another client.

### What does Promptise add beyond "an approval exists"?

Four enforcement guarantees the others leave to your app code: deny-by-default on timeout, denial of reviewer-edited (tampered) arguments, four-eyes rejection of self-approval via `PendingApprover`, and a build-time refusal to ship a tool that declares approval without a gate installed.

### Do I lose agent-side approval by moving the check to the server?

No. Promptise keeps both. Agent-side [`ApprovalPolicy`](../../core/approval.md) governs a Promptise agent's own calls (the in-process model the four frameworks use), while `ApprovalGateMiddleware` enforces at the tool boundary. They share one `ApprovalHandler` protocol, so a webhook or callback handler you write once works in either layer.

## Next steps

Find your framework's row in the matrix, then decide whether your risky tools are single-app or shared. If they're shared, move the check to where the tool lives: declare `requires_approval=True`, install `ApprovalGateMiddleware`, and let the deny-by-default, tamper-denying, four-eyes, refuse-to-ship-ungated semantics do the work — start with the [Approval Gates guide](../../mcp/server/approval-gates.md). From there, wire an independent-reviewer `PendingApprover` or an HMAC-signed webhook handler, cross-check the in-process analog in [agent-side Approval](../../core/approval.md), and enforce approval at the tool with `pip install promptise`.
