---
title: "Human Approval + Budget Caps for Irreversible AI Calls"
description: "The distinct angle is composition across two pillars: the server-side requires_approval gate (per-call human sign-off that holds for any MCP client) layered…"
keywords: "gate irreversible ai tool calls, approval plus budget for destructive actions, max_irreversible_per_run, block payments and deletes ai agent, two-layer control irreversible tools"
date: 2026-07-16
slug: gate-irreversible-ai-tool-calls
categories:
  - Approvals & HITL
---

# Human Approval + Budget Caps for Irreversible AI Calls

To **gate irreversible AI tool calls** the way a security review actually asks for, one lock is never enough — because the two things that can go wrong are orthogonal. A destructive tool (a refund, a delete, an outbound email, a production deploy) can fire because *nobody said yes* to that specific call, or it can fire because *someone said yes too many times*. Approval closes the first hole. It does nothing about the second. Promptise Foundry pairs two independent controls so both have to agree before a destructive action runs: a server-side `requires_approval=True` gate that decides **who** signs off on each call, and an agent-runtime budget with `max_irreversible_per_run` that decides **how many** destructive calls a single run is even permitted. Approve-per-call and cap-the-count are different questions, and the answer to one never covers the other.

## The failure a single lock can't catch

Give a support agent a `refund` tool and a human approver. A reviewer sees each request and clicks approve or deny. This feels complete — a human is in the loop. It is not complete, and here is the exact run that gets past it.

The agent gets stuck in a retry pattern and fires the same refund four times in ninety seconds. Each request pages a reviewer; each one, in isolation, looks reasonable ("refund order A-1, $10"); the reviewer approves all four because there is nothing on the individual request that says *this is the fourth one*. Four refunds go out. No prompt was bypassed, no approval was forgotten — the gate did exactly what it was told, one call at a time. Per-call approval has no memory of *how many* destructive calls this run has already made.

Now flip it. Your approval prompt lives in the agent's driver loop, and a second MCP client — a batch job, a scheduled trigger, another team's agent — calls the same `refund` tool directly, through a path that never had the prompt wired in. This time nobody is asked at all.

These are two different holes. The first is "approved, but too many." The second is "not approved, because the approval lived in the wrong place." A control that only answers *who approves this call* cannot close the first; a control that only counts actions cannot close the second. You need both, and they have to be enforced somewhere a forgotten line of driver code can't undo.

## Lock one: `requires_approval=True` at the tool boundary

The first lock moves approval off the caller and onto the tool. In Promptise you mark the tool, and the **server** takes on the obligation to gate it for every client that ever reaches it:

```python
@server.tool(auth=True, roles=["clerk"], requires_approval=True)
async def refund(order_id: str, amount: float) -> dict:
    """Refund an order — a human must approve before money moves."""
    return {"order_id": order_id, "amount": amount, "status": "refunded"}
```

Because the requirement is a property of the tool declaration, the batch job and the other team's agent inherit it automatically — there is no driver path that reaches `refund` without passing the gate. And the enforcement is blunt on purpose: if a tool declares `requires_approval=True` and no `ApprovalGateMiddleware` is installed, the server **refuses to build** rather than shipping an approval that silently never fires. That build-time invariant is its own subject — [An MCP Approval Gate That Refuses to Ship Ungated](build-time-enforced-approval-gate-mcp.md) covers it — and it is what makes this lock a structural guarantee instead of a convention.

Be honest about the gate's edge: server-side, it is **approve or deny only**. A reviewer cannot rewrite the arguments — a decision that carries `modified_arguments` is denied, not silently run with the original values, because the gate can't guarantee the substitution is what executes. The full semantics (deny-by-default on timeout, guards-run-first so unauthorized callers never page a reviewer) are in the [Approval Gates guide](../../mcp/server/approval-gates.md). What matters here is what this lock *does not* do: it never counts. Approve the same refund five times and the gate approves five times. That is not a bug — it is the boundary of the question this lock answers.

## Lock two: `max_irreversible_per_run` counts destructive actions

The second lock lives in the agent runtime, and it answers the question the gate can't: *how many destructive actions is this run allowed, regardless of who approved them?* You annotate which tools are irreversible, then set a ceiling on the count:

```python
from promptise.runtime import BudgetConfig, ToolCostAnnotation

budget = BudgetConfig(
    enabled=True,
    max_irreversible_per_run=2,       # at most 2 destructive actions per run
    tool_costs={
        "refund":         ToolCostAnnotation(cost_weight=5.0, irreversible=True),
        "delete_account": ToolCostAnnotation(cost_weight=8.0, irreversible=True),
        "search":         ToolCostAnnotation(cost_weight=0.5),  # reversible
    },
)
```

This is a separate dimension from cost and tool-count. The agent can call `search` as many times as its cost budget allows; it can call `refund` and `delete_account` a *combined* two times per run, then the third one is a violation — no human needed, and no human able to wave it through. Where the gate has no memory across calls, the budget's whole job is memory: `irreversible=True` increments a dedicated counter, and `max_irreversible_per_run` is the ceiling on it.

One honesty caveat, the same one the [Autonomy Budget guide](../../runtime/governance/budget.md) leads with: these are **abstract weight units you define, not dollars**. `max_irreversible_per_run=2` means "two irreversible actions," and `cost_weight=5.0` means "five budget units," not $5. The framework does not read any provider's pricing API. What it controls is *what the agent does* — how many destructive calls, of any kind — which is exactly the ceiling a per-call approver can't provide.

## Both locks in one run

Here is the composition, fully runnable with nothing but `pip install promptise` — no API key, no network. The first half exercises the server gate through `TestClient`; the second half drives the runtime budget's irreversible counter directly. Watch approve/deny happen per call, then watch the third destructive action get stopped no matter what the reviewer would have said:

```python
import asyncio

from promptise.mcp.server import (
    ApprovalGateMiddleware, AuthMiddleware, JWTAuth, MCPServer, TestClient,
)
from promptise.approval import ApprovalDecision
from promptise.runtime import BudgetConfig, BudgetState, ToolCostAnnotation

auth = JWTAuth(secret="dev-secret")


def build_billing(approver):
    server = MCPServer(name="billing")
    server.add_middleware(AuthMiddleware(auth))                    # identity first
    server.add_middleware(ApprovalGateMiddleware(approver, timeout=5))

    @server.tool(auth=True, roles=["clerk"], requires_approval=True)
    async def refund(order_id: str, amount: float) -> dict:
        """Refund an order — a human must approve before money moves."""
        return {"order_id": order_id, "amount": amount, "status": "refunded"}

    return server


async def main():
    clerk = auth.create_token({"sub": "clerk-1", "roles": ["clerk"]})
    hdr = {"authorization": f"Bearer {clerk}"}

    # LOCK 1 — the server gate decides WHO says yes, per call.
    approve = lambda req: ApprovalDecision(approved=True, reviewer_id="mgr")
    deny = lambda req: ApprovalDecision(
        approved=False, reviewer_id="mgr", reason="over policy limit")

    ok = await TestClient(build_billing(approve)).call_tool(
        "refund", {"order_id": "A-1", "amount": 10.0}, headers=hdr)
    print("gate approved:", ok[0].text[:64])

    no = await TestClient(build_billing(deny)).call_tool(
        "refund", {"order_id": "A-2", "amount": 9000.0}, headers=hdr)
    print("gate denied  :", no[0].text[:40])   # APPROVAL_DENIED

    # LOCK 2 — the runtime budget decides HOW MANY are permitted, no human.
    budget = BudgetState(BudgetConfig(
        enabled=True,
        max_irreversible_per_run=2,
        tool_costs={
            "refund":         ToolCostAnnotation(cost_weight=5.0, irreversible=True),
            "delete_account": ToolCostAnnotation(cost_weight=8.0, irreversible=True),
            "search":         ToolCostAnnotation(cost_weight=0.5),
        },
    ))

    for _ in range(20):                          # reversible: never blocked
        await budget.record_tool_call("search")

    v1 = await budget.record_tool_call("refund")          # 1st irreversible: ok
    v2 = await budget.record_tool_call("delete_account")  # 2nd irreversible: ok
    v3 = await budget.record_tool_call("refund")          # 3rd: over the cap

    print("first two destructive calls:", v1, v2)         # None None
    print("third destructive call blocked:",
          v3.limit_name, f"{v3.current_value:.0f}/{v3.limit_value:.0f}")
    print("irreversible remaining:", budget.remaining()["irreversible_run"])


asyncio.run(main())
```

Running it prints, in order: the approved refund body, an `APPROVAL_DENIED` error for the denied one, `None None` for the first two destructive calls, then `max_irreversible_per_run 3/2` and `irreversible remaining: -1` — the third destructive action stopped by the count, even though the gate would have approved it. Twenty searches in between never touched the irreversible counter, because reversible reads aren't what this ceiling is for. Two orthogonal locks, one run: the gate said yes, the budget said *that's enough*.

## What other frameworks do today

Human-in-the-loop is not a Promptise invention, and every major framework ships a real mechanism for the *approve-per-call* half. Being precise about where each one lands is the whole point:

- **Pydantic AI** can mark a tool as requiring approval through its deferred-tool / approval-required pattern — a genuine capability. But it resolves that approval *app-side*: the calling application detects the requirement and handles it before re-running the tool. The declaration signals intent; your surrounding app code is responsible for honoring it, and a second entry point that doesn't run that code isn't gated.
- **LangGraph** has first-class HITL via `interrupt()` plus checkpointer-backed `Command(resume=...)`. It's powerful and durable, but the interrupt is placed *in the graph node* that drives the tool — approval is a property of the orchestration, not the tool, so a node or edge that reaches the tool without the interrupt isn't paused.
- **CrewAI** has `human_input=True` on a `Task` for output review, and separately ships `max_rpm` — a real requests-per-minute limiter at the agent and crew level. But `max_rpm` is a global outbound *rate* throttle: it caps calls-per-minute across the board, with no notion of which calls are irreversible. Slow down to one call per minute and you can still issue an unbounded number of destructive actions over time.
- **AutoGen** wires HITL through `UserProxyAgent` and `human_input_mode="ALWAYS"` — approval is a property of the conversation topology you assemble, inherited by the path you built and not by a different one.

None of these frameworks *can't* enforce approval — with discipline you can wire any of them correctly, and [LangGraph vs CrewAI vs AutoGen: Where HITL Runs](langgraph-vs-crewai-vs-autogen-human-in-the-loop.md) maps exactly where each executes. The precise delta is this: each ships one lock — a per-call approval that lives in the orchestrator or the app — and none pair it with a *separate runtime cap on the count of irreversible actions per run*. So a destructive call from a different client, or an approved-but-repeated one, has no second ceiling. Promptise makes both first-class and independent: the approval is a property of the tool declaration the server enforces for any MCP client, and the irreversible count is a dedicated budget dimension the runtime enforces with no human in the loop. Two questions, two locks, both structural.

## Frequently asked questions

### Why not just make the approver reject the fourth call?

Because the approver can't see that it's the fourth. A per-call gate is stateless by design — it decides on the request in front of it, and four identical `refund` requests look identical. The count lives in the runtime, not in the reviewer's head. `max_irreversible_per_run=2` enforces it mechanically, so "too many destructive actions" is caught even when every individual call was legitimately approvable.

### Does `max_irreversible_per_run` limit dollars spent?

No. It counts irreversible *actions*, and the related `cost_weight` values are abstract budget units you define, not currency. The framework does not connect to any LLM or payments provider's pricing. It controls what the agent does — how many destructive calls of any kind — not what those calls cost. For real monetary limits, pair it with your provider's usage dashboard; the [Autonomy Budget guide](../../runtime/governance/budget.md) spells out the boundary.

### Can a reviewer approve a smaller refund instead of denying it?

Not server-side. The `requires_approval=True` gate is approve-or-deny only; a decision carrying `modified_arguments` is denied rather than run, because the gate can't guarantee the substituted values are what executes. Argument rewriting is a real capability, but it belongs to agent-side `ApprovalPolicy`, which re-binds before dispatch. The [Approval Gates guide](../../mcp/server/approval-gates.md) documents this edge explicitly.

### What if a different MCP client calls the destructive tool directly?

That's exactly why the gate lives on the tool and not in the driver. Any client that reaches a `requires_approval=True` tool hits the gate — there is no path around it. And if that client is part of a multi-tenant deployment, the approval request carries the verified `client_id` and `tenant_id`, so reviewers see who is asking. The [Secure Multi-Tenant Platform guide](../../guides/secure-multi-tenant-platform.md) wires the gate and per-tenant identity together end to end.

### Which lock runs first?

They operate at different layers, so it isn't really a race. The server gate fires when a call reaches the tool boundary — one decision per call. The budget's irreversible counter is checked as the run accumulates actions across many calls. A call must pass *both*: approved at the boundary, and under the run's remaining irreversible ceiling. Fail either and the destructive action doesn't run.

## Next steps

List your irreversible tools first — the refunds, deletes, sends, and deploys. Gate each one with `requires_approval=True` and install an `ApprovalGateMiddleware`, following the [Approval Gates guide](../../mcp/server/approval-gates.md), so per-call sign-off holds for every MCP client. Then annotate those same tools with `irreversible=True` and set `max_irreversible_per_run` in your [Autonomy Budget](../../runtime/governance/budget.md), so the count has a ceiling no reviewer can wave past. Run the snippet above to see both locks fire in one run, read [An MCP Approval Gate That Refuses to Ship Ungated](build-time-enforced-approval-gate-mcp.md) for the build-time guarantee behind lock one, and when you're ready to put it in front of real customers, the [Secure Multi-Tenant Platform guide](../../guides/secure-multi-tenant-platform.md) composes both with tenant identity, tamper-evident audit, and rate limiting in one pipeline. Two orthogonal locks: approval decides who says yes, the budget decides how many are permitted — make both say yes before anything irreversible runs.
