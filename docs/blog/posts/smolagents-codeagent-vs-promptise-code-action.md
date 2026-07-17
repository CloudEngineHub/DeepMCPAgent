---
title: "Smolagents CodeAgent vs Promptise: Where Do Tool Calls Run?"
description: "Both frameworks let the model write one program over your tools, so this is a precise-delta piece, not a 'they have nothing' one. In smolagents the tool…"
keywords: "smolagents codeagent vs promptise code-action, where code-action tool calls run, governed tool bridge sandbox, code-action approval gates"
date: 2026-07-16
slug: smolagents-codeagent-vs-promptise-code-action
categories:
  - Sandboxing
---

# Smolagents CodeAgent vs Promptise: Where Do Tool Calls Run?

The **smolagents codeagent vs promptise code-action** question is not "who lets the model write one program over your tools" — both frameworks do, and both do it well. It is a narrower, more consequential question: when that program calls `issue_refund()` or `delete_customer()`, *where does that call execute, and what governs it?* This is a precise-delta piece, not a "they have nothing" one. By the end you will know exactly what smolagents' CodeAgent does today, where Promptise's bridge draws a different line, and how a sandboxed program can still trip your approval gate and a hard call cap.

## Same program, different execution boundary

Both frameworks implement the same core idea, and it is a good one. Instead of the model emitting a chain of conversational tool calls — reason, act, observe, repeat — it writes a single Python program that loops, filters, joins, and aggregates over your tools, then runs it. Hugging Face's smolagents calls this a `CodeAgent`; the underlying research idea is the "code as action" (CodeAct) pattern. Promptise calls it `agent_pattern="code-action"`. For data-heavy work, one program beats thirty tool calls on tokens, latency, and arithmetic accuracy — the full case for the pattern is in our [code-action guide](../../guides/code-action.md).

So the interesting difference is not *whether* the model writes a program. It is the **execution boundary**: for each `get_order()` or `issue_refund()` call the program makes, does that call run inside the code sandbox, or does it get routed back to the real tool on your host — where your approval gates, budgets, and audit trail live?

That boundary is the whole post.

## What smolagents CodeAgent does today (precisely)

Let us be exact and fair, because smolagents is a well-built library and vague swipes help nobody.

A smolagents `CodeAgent` writes Python and executes it in a *code execution environment*. You choose which one:

- **`LocalPythonExecutor`** (the default): a restricted, in-process interpreter with an import allowlist (`additional_authorized_imports`). Your tools are registered as callables and invoked directly, in the same process as the agent.
- **`E2BExecutor` / `DockerExecutor` / Wasm executor**: remote or containerized runtimes that smolagents added specifically so untrusted model-written code is contained. When you enable one, the tool code is shipped *into* that sandbox and runs there.

Credit where it is due: those remote executors mean smolagents contains the untrusted **code** properly. Containment of the code is not the gap. smolagents also ships a `Monitor` that tracks token usage and step durations, plus `step_callbacks` and `final_answer_checks` that fire around each step.

Here is the precise delta. In a `CodeAgent`, a "step" is *one whole code execution* — the entire program. Monitoring and callbacks operate at that granularity. Individual tool calls *inside* the running program are just function calls resolved by the executor: directly in-process for the local executor, or inside the sandbox for the remote ones. There is no built-in mechanism to intercept one specific tool call within the program and route it back through a host-side, per-call **approval / budget / audit** layer. If you want a human to approve *this one refund* the code is about to issue, you have to build that logic into the tool function itself. It is not a first-class, declarative gate the framework wraps around the call independent of where the code runs.

That is the honest boundary. Neither framework is "wrong" — they draw the line in different places. Promptise's edge is making per-call governance **structural** rather than something you hand-roll inside each tool.

## Where Promptise runs the call: the host, through the bridge

Promptise's `code-action` runs the model's program in a hardened Docker sandbox — read-only rootfs, dropped capabilities, seccomp, resource limits, and `network="none"` auto-set — the same container security layers documented under [the sandbox reference](../../core/sandbox.md). So far, so similar: the untrusted code is contained.

The difference is what happens when the program calls a tool. It does **not** run the tool inside the sandbox. The generated in-container stub writes a request file to a `/workspace` tmpfs and blocks. A concurrent loop *on your host* sees the request, runs the **real** `BaseTool`, and writes the response file back. The program unblocks with the result. No network is involved — the channel is a filesystem rendezvous, which is exactly why the sandbox can stay fully network-isolated. The mechanics are laid out step by step in the [code-action guide](../../guides/code-action.md).

Because the bridge invokes the *real* host tool, everything you wrapped that tool with still applies:

- **Approval gates fire.** If you built the agent with an `ApprovalPolicy` over a mutating tool, a bridged call to that tool triggers the reviewer — even though the call originated inside a sandboxed program the model wrote.
- **A hard, hook-independent call cap.** The code-action node enforces its own `max_tool_calls` cap per run (default 50). Once reached, further bridged calls return an error to the program instead of executing. This bound holds even with no governance runtime attached, so a generated program can never loop a tool unbounded.
- **Governance hooks apply per call.** Run the agent under the Agent Runtime and its budget, health, and audit hooks wrap *each* bridged call — more on that below.

The containment stops the model's *code* from escaping the box; the bridge is what keeps its *tool calls* under your control. That is the structural difference from executing tools inside the code environment.

## Run it: a sandboxed program that still trips your approval gate

Here is the delta as runnable code. The program the model writes will call `issue_refund` from *inside* the sandbox — and your host-side reviewer still gets the final say on each call. Set `OPENAI_API_KEY` and have Docker running.

```python
import asyncio
from langchain_core.tools import tool
from promptise import build_agent, ApprovalPolicy, CallbackApprovalHandler, ApprovalDecision

ORDERS = {
    "O-1001": {"customer": "acme", "total": 42.0,  "status": "shipped"},
    "O-1002": {"customer": "acme", "total": 890.0, "status": "shipped"},
}

@tool("list_orders")
def list_orders() -> list:
    """Return every order id."""
    return list(ORDERS)

@tool("get_order")
def get_order(order_id: str) -> dict:
    """Return {customer, total, status} for an order id."""
    return {"order_id": order_id, **ORDERS[order_id]}

@tool("issue_refund")
def issue_refund(order_id: str, amount: float) -> str:
    """Refund `amount` to the customer for `order_id`. Mutating + irreversible."""
    return f"refunded ${amount:.2f} for {order_id}"

# This reviewer runs on YOUR host — not in the sandbox — so it sees and governs
# every bridged call the model-written program makes.
async def review(request):
    amount = float(request.arguments.get("amount", 0))
    approved = amount <= 100
    print(f"[approval] {request.tool_name}({request.arguments}) -> "
          f"{'APPROVE' if approved else 'DENY (refund over $100 needs a human)'}")
    return ApprovalDecision(approved=approved, reviewer_id="policy-bot")

async def main():
    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={},                          # or your MCP servers
        agent_pattern="code-action",         # sandbox auto-enabled (Docker required)
        extra_tools=[list_orders, get_order, issue_refund],
        approval=ApprovalPolicy(
            tools=["issue_refund"],          # gate only the mutating tool
            handler=CallbackApprovalHandler(review),
            timeout=120,
        ),
    )
    result = await agent.ainvoke({"messages": [{"role": "user", "content":
        "Refund order O-1002 in full."}]})
    print(result["messages"][-1].content)
    await agent.shutdown()

asyncio.run(main())
```

The model writes one program: look up `O-1002`, read its `total`, call `issue_refund`. That `issue_refund` call leaves the sandbox as a request file, and Promptise runs the **approval-wrapped** host tool. Because `build_agent` wraps your tools with the `ApprovalPolicy` *before* handing them to the code-action node, the reviewer decides — and an \$890 refund is denied by policy, not silently executed inside a scratch container. Swap `list_orders`/`get_order` for read-only tools and only the mutating one carries a gate.

## Governance that travels with each bridged call

Approval is the vivid example, but the same bridge carries the rest of your governance. When you run a code-action agent under the [Agent Runtime](../../runtime/index.md), its hooks attach to every bridged call:

- **Budget** — per-run and daily caps on tool calls, LLM turns, and irreversible actions. A program that tries to fan out a hundred writes hits the budget, not just the node's hard `max_tool_calls` floor.
- **Health** — behavioral anomaly detection (stuck / loop / repeated-call patterns) evaluated across the bridged calls the program makes.
- **Audit** — each bridged call is recorded in the tamper-evident journal with the caller identity, so "the sandbox did it" is never an unaudited black box.

In smolagents you would place these controls inside each tool or in step-level callbacks that see a whole code block, not the individual call. Promptise makes them a property of the execution boundary itself: the program can only reach a tool by asking the host to run it, and the host runs it through the full hook chain every time. For the broader "where should model-written code run" decision, our field guide [How to Run AI-Generated Code Safely](run-ai-generated-code-safely.md) contrasts the three common setups, and [The Real Risks of Running AI-Generated Code In-Process](risks-of-running-ai-generated-code.md) shows what a permissive in-process executor gives away.

## Frequently asked questions

### Does smolagents CodeAgent run tool calls in a sandbox or on the host?

It depends on the executor. With the default `LocalPythonExecutor`, tools are called directly in-process. With the E2B, Docker, or Wasm executors — which smolagents added to contain untrusted code — the tool code runs inside the sandbox. In both cases the tool call resolves within the code execution environment; there is no built-in hook to route each individual call back through a host-side approval, budget, and audit layer. That per-call governance is what Promptise's bridge makes structural.

### Is Promptise's approach just a slower sandbox?

There is a real latency floor — each run spins a fresh container (~1–2s), and the filesystem bridge adds a small per-call round-trip. On genuine aggregation work the token and accuracy wins dominate; for a one-shot lookup a plain `react` call is snappier. The bridge's payoff is governance, not raw speed: you trade a few milliseconds per call for every host-side hook applying to it.

### Can a model-written program bypass the approval gate?

No. The program has no network and no host access; its only way to reach a tool is to write a request file that your host loop services by running the real, approval-wrapped `BaseTool`. The gate is on the host side of the boundary, so the program cannot route around it. Independently, the code-action node caps bridged calls per run (default 50) even with no governance hooks attached.

### Does the containment make my tools safe?

The sandbox stops the model's *code* from escaping; it does not make your *tools* safe on its own. For untrusted or multi-tenant input, keep the code-action tool set least-privilege, wrap mutating tools with approval, set a conservative cap, and prefer the gVisor backend for kernel-level isolation. Containment and least-privilege are complementary, not substitutes.

## Next steps

Decide where your tool calls belong — inside the code environment, or back on your host through a governed bridge. If you want each call wrapped by approval, budget, and audit no matter what program the model writes, that is Promptise's default line. Start by running the example above with `agent_pattern="code-action"`, then read the [code-action guide](../../guides/code-action.md) to wire your own tools, the [sandbox reference](../../core/sandbox.md) for the container security layers, and the [Agent Runtime](../../runtime/index.md) to attach budget, health, and audit hooks to every bridged call.
