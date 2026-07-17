---
title: "Should an AI Agent Approve Its Own Action?"
description: "A deep dive on separation of duties that goes well past the two sentences the general approval post spends on it. Explain what dual control means in audit…"
keywords: "separation of duties for ai agents, four-eyes approval for ai actions, can an agent approve its own tool call, self-approval rejected mcp, dual control ai agent"
date: 2026-07-16
slug: separation-of-duties-for-ai-agents
categories:
  - Approvals & HITL
---

# Should an AI Agent Approve Its Own Action?

Separation of duties for AI agents is the rule that the principal who *requests* a sensitive action can never be the one who *approves* it — and it is exactly the control that quietly collapses the moment whoever drives an agent can also click "approve" on the agent's own request. Most human-in-the-loop wiring stops one step short of this. It pauses before a risky tool call and asks a human to decide, which feels like dual control. But if the human answering the prompt is the same person (or the same service identity) that triggered the call, you have a rubber stamp with extra latency, not a second set of eyes. This post goes past the two sentences a general approval overview can spare and works through what dual control actually means, why the reviewer has to be a *different* principal, and how Promptise Foundry makes `reviewer client_id != caller client_id` a server-enforced default instead of a policy you hope everyone remembers to build.

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## Separation of duties, in the language auditors use

Separation of duties (SoD) — also called dual control, four-eyes, or maker-checker — predates AI by decades. It's the accounting principle that no single actor should control every step of a high-impact transaction. The person who submits a payment can't be the person who releases it; the engineer who writes a change can't be the sole approver of its deploy. The point isn't distrust of any one person. It's that a single compromised, mistaken, or coerced actor shouldn't be able to complete a damaging action alone.

Every serious audit framework encodes this:

- **SOX** (Sarbanes-Oxley) requires SoD over financial reporting so that no one individual can both initiate and conceal a fraudulent transaction.
- **PCI DSS** mandates dual control and split knowledge for cardholder-data key management, and separation of duties between roles that develop and roles that operate.
- **SOC 2** auditors look for SoD as a Common Criteria control — evidence that privileged actions require an independent approver, and that the approval is *attributable* to someone other than the requester.

When you put an autonomous agent in the middle of a workflow that used to have a maker and a checker, the agent becomes the maker. The checker still has to be a human — and, critically, a *different* human from the one whose identity the agent is acting under. An approval step that a SOC 2 auditor will accept has to answer one question in the logs: *who, other than the requester, released this?* If the answer can be "the requester themselves," the control has failed on its own terms.

## The failure mode almost every agent stack shares

Here's the concrete gap. You give an operator an agent that can issue refunds. You add a human-in-the-loop step so refunds over a threshold pause for approval. The approval prompt renders in the same console, chat UI, or app the operator is already driving. So the operator kicks off the refund, sees the "approve?" prompt for the refund *they just triggered*, and clicks yes. Two events, one principal. No independent review happened.

This isn't a hypothetical edge case — it's the default shape of client-side and in-process HITL. The approval executes wherever the agent is being driven, which means the identity answering the prompt is the identity that requested the action. Nothing stops the requester from being the approver, because nothing in the mechanism knows they're the same principal. The agent can effectively approve its own action, using whichever human is holding the wheel.

Promptise draws a clean line between two layers here. Agent-side [`ApprovalPolicy`](../../core/approval.md) governs a Promptise agent's *own* tool calls in-process — great for "amounts under $100 are fine, everything else asks a human," and structurally the same in-process model the other frameworks ship. But when the tool is a shared MCP endpoint that more than one client can reach, the enforcement point has to move to the tool itself, and the reviewer's identity has to be checked against the caller's. That's what [Approval Gates (server-side HITL)](../../mcp/server/approval-gates.md) are for, and it's where the four-eyes invariant lives.

## What other frameworks do today

Let's be precise and fair, because every framework below ships a *real*, working human-in-the-loop control. The gap isn't "they don't have HITL." It's that none of them ship a built-in *reviewer-is-not-the-caller* invariant on the approval primitive — so separation of duties is left for you to build.

- **LangGraph** pauses in the graph runtime: you call `interrupt()` inside a node, and with a checkpointer configured the graph state is saved until your application resumes it with `Command(resume=...)`. The pause is real and durable, but the decision is supplied by whatever code calls `resume` — LangGraph doesn't bind that decision to a principal distinct from the one that started the run.
- **CrewAI** uses `human_input=True` on a `Task`, which prompts for feedback on the console inside the crew process. It also has controls like `max_rpm` for rate — but the approval prompt is answered by whoever is at that console, requester included.
- **AutoGen** routes approval through a `UserProxyAgent` with `human_input_mode` (`ALWAYS`, `TERMINATE`, `NEVER`), and it even ships a distributed runtime for multi-process agents. The human input, though, arrives through the proxy in the agent loop; there's no built-in check that the approver differs from the initiator.
- **Pydantic AI** models it as deferred-tool approval: a call needing sign-off is surfaced back to your application code, which resolves it and re-runs the agent. The decision happens app-side, so the identity that approves is whatever your app decides — SoD is your code to write.

You *can* build reviewer≠caller on top of any of these — it's application logic. The delta is that in each case it's a thing you assemble and must not forget, not a property the approval primitive enforces for you. (For a fuller map of where each framework's HITL physically executes, see [LangGraph vs CrewAI vs AutoGen: Where HITL Runs](langgraph-vs-crewai-vs-autogen-human-in-the-loop.md).) Promptise's edge is to make that check a default of the gate, not a task on your backlog.

## How Promptise makes reviewer ≠ caller a server-side invariant

`PendingApprover` implements independent four-eyes review. A tool declared `requires_approval=True` blocks in a pending store until a human holding the approver role releases it through two auto-registered, role-guarded admin tools: `approvals_list()` and `approvals_decide(request_id, approve, reason)`. The reviewer's verified `client_id` — read from the authenticated request context, not a parameter the caller can spoof — is recorded on every decision.

The invariant that turns this from a queue into dual control: **`approvals_decide` rejects an approval whose reviewer `client_id` equals the original caller's** — even if that same principal also holds the approver role. Denying your own request is always allowed (anyone can cancel their own action), but *releasing* it is not. Here is the whole thing, end to end and runnable — the caller `dana` holds the `approver` role, and she still cannot approve her own refund:

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

Run it and the output is unambiguous:

```text
dana approves her own call -> {"resolved": false, "error": "cannot approve your own request — four-eyes separation of duties requires a different reviewer"}
omar approves dana's call -> {"resolved": true, "request_id": "…", "approved": true}
tool result             -> refunded A-1 ($5000.0)
```

Two details make this hold under scrutiny. First, `AuthMiddleware` runs before the gate, so every request — the refund *and* the decision — carries a verified `client_id`; the reviewer identity the invariant compares against is authenticated, not asserted. Second, the check is server-side, so it applies no matter which MCP client calls the tool: a chat agent, a cron job, a curl command, or a second agent all inherit the same "you can't approve your own call" rule. It's an invariant of the tool, not a courtesy of the caller — the same philosophy that makes the gate [refuse to ship ungated](build-time-enforced-approval-gate-mcp.md) in the first place. A role check alone would have let `dana` through, because she holds `approver`; the identity comparison is what makes it genuine dual control.

## Frequently asked questions

### What is separation of duties for AI agents?

It's applying the maker-checker principle to autonomous systems: the agent (acting as the maker) can *request* a sensitive action, but a *different* human principal has to approve it before it runs. Concretely, it means the identity that approves a tool call must not be the identity that triggered it — the same SoD control SOX, PCI DSS, and SOC 2 require for privileged human actions.

### Can an agent approve its own tool call in Promptise?

No — not when the tool is behind a `PendingApprover` gate. `approvals_decide` compares the reviewer's authenticated `client_id` to the original caller's and rejects an *approval* when they match, even if that principal also holds the approver role. Denying your own request is always allowed, since cancelling your own action needs no second party. Self-approval is rejected server-side, so no MCP client can route around it.

### Do LangGraph, CrewAI, AutoGen, or Pydantic AI enforce reviewer ≠ caller?

They all ship real HITL — LangGraph's `interrupt()` + checkpointer, CrewAI's `human_input=True`, AutoGen's `UserProxyAgent(human_input_mode=...)`, Pydantic AI's deferred-tool approval — but none of them ship a built-in invariant that the approver must be a different principal than the requester. You can build that check in your own application code on top of any of them; Promptise makes it a default of the gate instead of something you must remember to add.

## Next steps

See how `PendingApprover` binds a different reviewer to every call and rejects self-approval by default: read the [Approval Gates guide](../../mcp/server/approval-gates.md), declare `requires_approval=True` on your first irreversible tool, and install `ApprovalGateMiddleware(PendingApprover(server))`. For governing an agent's own in-process calls, pair it with agent-side [`ApprovalPolicy`](../../core/approval.md); to understand why a shared tool needs the check at the tool boundary at all, read [LangGraph vs CrewAI vs AutoGen: Where HITL Runs](langgraph-vs-crewai-vs-autogen-human-in-the-loop.md) and [An MCP Approval Gate That Refuses to Ship Ungated](build-time-enforced-approval-gate-mcp.md).
