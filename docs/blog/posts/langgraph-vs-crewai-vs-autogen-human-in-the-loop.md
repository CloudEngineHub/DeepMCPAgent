---
title: "LangGraph vs CrewAI vs AutoGen: Where HITL Runs"
description: "A capability matrix, not another retelling of the bypass argument. For each framework, state exactly where the approval physically executes and what that…"
keywords: "langgraph vs crewai vs autogen human in the loop, where does hitl approval execute, pydantic ai deferred tool approval, client-side vs server-side agent approval, mcp tool approval enforcement point"
date: 2026-07-16
slug: langgraph-vs-crewai-vs-autogen-human-in-the-loop
categories:
  - Approvals & HITL
---

# LangGraph vs CrewAI vs AutoGen: Where HITL Runs

Read enough roundups and you get the same **LangGraph vs CrewAI vs AutoGen human in the loop** comparison — three feature checklists, all ticking the "supports HITL" box — while the question that actually decides your architecture goes unasked: *where does the approval physically execute?* This post is a capability matrix, not another retelling of the bypass argument. For each framework we state exactly where the check runs and what that implies, then show the one row that's structurally different: a gate that lives on the tool itself.

<!-- more -->

## Where HITL runs in each framework today

Let's be precise and fair, because every framework here ships a *real*, working human-in-the-loop control. None of these is a client-side "are you sure?" hack. They differ in one property — the location of the enforcement point.

- **LangGraph** pauses in the graph runtime. You call `interrupt()` inside a node; with a checkpointer configured, the graph state is saved and execution stops. It resumes when *your application* calls back with `Command(resume=...)`. The pause is real and durable — but it lives in the graph executor running inside your process.
- **CrewAI** uses `human_input=True` on a `Task`. When the task finishes, the crew prompts for human feedback on stdin/console and folds the response back in. That prompt fires inside the crew process that's driving the run.
- **AutoGen** routes approval through a `UserProxyAgent` with `human_input_mode` (`ALWAYS`, `TERMINATE`, or `NEVER`). The proxy asks for input inside the agent conversation loop — again, inside the process hosting the agents.
- **Pydantic AI** models it as deferred-tool approval: a tool call that needs a human is surfaced back to the application code driving the agent, which resolves it and re-runs the graph. The decision happens app-side, in your own control flow.

Here is the matrix that matters — not "does it have HITL" (they all do), but where the check physically sits:

| Framework | HITL mechanism | Where the check physically runs |
|---|---|---|
| LangGraph | `interrupt()` + checkpointer, resume via `Command(resume=...)` | The graph runtime inside your app process |
| CrewAI | `human_input=True` on a `Task` | The crew process (console/stdin prompt) |
| AutoGen | `UserProxyAgent(human_input_mode=...)` | The agent process (conversation loop) |
| Pydantic AI | Deferred-tool approval, resolved app-side | Your application code driving the agent |
| Promptise Foundry | `@server.tool(requires_approval=True)` + `ApprovalGateMiddleware` | The MCP server that owns the tool |

Four of those five rows land in the same place: **the enforcement point is the process driving the agent.** That's not a flaw — for a single-app deployment it's exactly right, and it's often more ergonomic than a network round trip. It just has a consequence worth naming.

## The delta is location, not presence

Here's the honest version of the differentiator, stated so it can't be mistaken for a cheap shot.

If the tool being gated only ever runs inside one application's process — the same process that renders the LangGraph interrupt, the CrewAI prompt, or the AutoGen proxy — then in-process HITL is a *complete* control. Nothing escapes it, because there's only one caller and the pause lives with that caller.

The gap opens the moment that tool becomes a shared endpoint. Say you expose `delete_records` or `refund` as an MCP tool so a second client can use it — Claude Desktop, Cursor, a teammate's script, a nightly cron job. The in-process pause does **not** travel with the tool, because it was never a property of the tool. It was a property of the process that happened to be driving the agent. Point a different MCP client at the same tool and the approval simply doesn't fire.

This is the question competitors can't cleanly answer: *if I aim a different MCP client at the same tool, does the approval still run?* With an in-process interrupt, no. The pause is in the graph executor, the crew loop, or the app's deferred-tool handler — none of which the second client goes through. The failure isn't malicious; the control just wasn't where the risk was. (Promptise has an in-process model too — agent-side [`ApprovalPolicy`](../../core/approval.md) governs a Promptise agent's *own* tool calls, exactly like the four frameworks above. The point isn't that in-process is wrong; it's that it's not sufficient when the tool is shared.)

## Promptise's row: the check lives on the tool

Promptise Foundry moves the enforcement point to the MCP server that owns the tool. You declare `requires_approval=True` on the tool and install one middleware; the gate then sits in the server pipeline, in front of the handler, for *every* client that ever calls it. This is the [Approval Gates](../../mcp/server/approval-gates.md) mechanism, and here is the smallest version that runs end to end:

```python
import asyncio
from promptise.mcp.server import MCPServer, ApprovalGateMiddleware, TestClient

server = MCPServer(name="ops")

# A policy approver: auto-approve low-risk calls, route the rest to a human.
# Returning False denies outright. This gate is in the SERVER pipeline, so it
# fires for every client that ever calls delete_records — not just one agent.
def policy(request):
    return request.arguments.get("count", 0) < 10

server.add_middleware(ApprovalGateMiddleware(policy, timeout=300))

@server.tool(requires_approval=True)
async def delete_records(table: str, count: int) -> dict:
    """Delete rows — the gate must approve before this body runs."""
    return {"table": table, "deleted": count, "status": "ok"}

async def main():
    client = TestClient(server)

    ok = await client.call_tool("delete_records", {"table": "logs", "count": 3})
    print("small delete:", ok[0].text)        # ran — under the threshold

    blocked = await client.call_tool("delete_records", {"table": "users", "count": 5000})
    print("large delete:", blocked[0].text)    # APPROVAL_DENIED — body never ran

asyncio.run(main())
```

The `policy` callable is the simplest of three approvers. For a genuine second person, swap it for `PendingApprover`, which blocks the call in a pending store and hands reviewers two role-guarded admin tools (`approvals_list()` and `approvals_decide(request_id, approve, reason)`) — and enforces that you cannot release your own call. That separation-of-duties question is worth its own read: [Should an AI Agent Approve Its Own Action?](separation-of-duties-for-ai-agents.md). For an external system, pass a `WebhookApprovalHandler`; the same `ApprovalHandler` protocol backs both the [agent-side policy](../../core/approval.md) and the server-side gate, so a handler you write once works in either place.

## Point any MCP client at it — the gate still fires

The reason this closes the tool-boundary gap is that the check is bound to the tool, not the transport or the caller. The *same* `server` object is served over stdio, streamable HTTP, or SSE, and the gate is identical in all three:

```bash
# One server object, any transport. The gate is in the pipeline, not the transport.
promptise serve ops_server:server --transport stdio
promptise serve ops_server:server --transport http --port 8080
promptise serve ops_server:server --transport sse  --port 8080
```

Now every MCP client that discovers `delete_records` — your Promptise agent, Claude Desktop, Cursor, a curl script, a 3 a.m. cron job — inherits the approval requirement automatically. There is no "the agent that renders the prompt" versus "the batch job that doesn't." The gate is a property of the tool, so the answer to "does approval still run through a different client?" is finally *yes*, without you wiring anything client-side.

The semantics are fail-closed the whole way down: a denial raises a structured `APPROVAL_DENIED` error, no decision within `timeout` is denied by default (`on_timeout="allow"` is the explicit opt-out), a reviewer editing the arguments is denied because the gate won't run args nobody approved, and a crashed handler resolves to denial through the error pipeline. Every outcome flows to the audit chain with the approval request id attached.

## The invariant: a gated tool refuses to ship ungated

There's one more property none of the in-process approaches structurally have, and it's the part that turns "we added approval" into "approval cannot be silently dropped." In Promptise, if any tool declares `requires_approval=True` and no `ApprovalGateMiddleware` is installed, **the server refuses to build** — and `TestClient` raises on the call. A declared approval that quietly doesn't enforce would be worse than none, so the framework makes the misconfiguration impossible to ship rather than something you catch in an incident review.

That's the difference between a feature and an invariant. An in-process interrupt is a line of code a refactor can remove without a peep. A build-time-enforced gate is a structural guarantee: the tool literally will not serve without its gate present. We wrote up that design in [An MCP Approval Gate That Refuses to Ship Ungated](build-time-enforced-approval-gate-mcp.md), and the full option surface — `on_timeout`, `include_arguments`, `PendingApprover`, `ElicitationApprover`, tenant-aware review — lives in the [Approval Gates guide](../../mcp/server/approval-gates.md).

To keep the framing honest: this matters *specifically* when tools are shared MCP endpoints. If your agent is a self-contained app whose tools never leave its process, LangGraph's `interrupt()`, CrewAI's `human_input`, AutoGen's `UserProxyAgent`, or Pydantic AI's deferred approval will serve you well and cost you less latency. The tool-boundary gate earns its keep the moment more than one client can reach the tool.

## Frequently asked questions

### Where does HITL approval execute in LangGraph, CrewAI, and AutoGen?

In the process driving the agent. LangGraph pauses in the graph runtime at `interrupt()` and resumes when your app sends `Command(resume=...)`; CrewAI prompts on the console inside the crew process via `human_input=True`; AutoGen asks through a `UserProxyAgent` with `human_input_mode` inside the agent loop. All three are real controls — the enforcement point is just the app hosting the agent, not the tool.

### How is Pydantic AI's deferred tool approval different from a server-side gate?

Pydantic AI surfaces a deferred tool call to your application code, which resolves the approval and re-runs the agent. The decision is app-side, so it protects that one application. A server-side gate binds the check to the MCP tool instead, so the requirement applies to every client that calls the tool — the delta is the location of the enforcement point, not whether HITL exists.

### If my agent tool is only ever used by one app, do I need a server-side gate?

No. If the tool never becomes a shared MCP endpoint, in-process HITL is a complete control and often the simpler choice. Reach for a server-side gate when the same tool is (or will be) callable by multiple MCP clients — a second agent, Claude Desktop, a script, or a scheduled job — where an in-process pause wouldn't travel with the tool.

### Does moving the check server-side mean giving up agent-side approval?

No. Promptise keeps both. Agent-side [`ApprovalPolicy`](../../core/approval.md) governs a Promptise agent's own calls, mirroring the in-process model of the other frameworks, while `ApprovalGateMiddleware` enforces at the tool boundary. They share one `ApprovalHandler` protocol, so a webhook or callback handler works in either layer.

## Next steps

Find your framework's row in the matrix, then decide whether your risky tools are single-app or shared. If they're shared, move the check to where the tool lives: declare `requires_approval=True`, install `ApprovalGateMiddleware`, and let the deny-by-default, refuse-to-ship-ungated semantics do the rest — start with the [Approval Gates guide](../../mcp/server/approval-gates.md). To see how the same handler protocol powers a Promptise agent's own calls, read [agent-side Approval](../../core/approval.md), and for the design rationale behind the build-time invariant, see [An MCP Approval Gate That Refuses to Ship Ungated](build-time-enforced-approval-gate-mcp.md).
