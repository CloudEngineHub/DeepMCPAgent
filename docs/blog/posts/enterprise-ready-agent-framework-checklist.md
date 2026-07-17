---
title: "Enterprise-Ready Agent Framework Checklist: What's Left to You"
description: "Most 'production-ready?' roundups grade frameworks on GitHub stars and integration counts, not on the enterprise controls you actually get audited on. This…"
keywords: "enterprise-ready agent framework checklist, what agent frameworks leave to you, production agent framework comparison, agent framework security checklist, is my agent framework enterprise ready, agent framework gap analysis"
date: 2026-07-16
slug: enterprise-ready-agent-framework-checklist
categories:
  - Comparisons
---

# Enterprise-Ready Agent Framework Checklist: What's Left to You

An honest **enterprise-ready agent framework checklist** doesn't grade tools on GitHub stars or integration counts — it grades them on the controls a security or compliance reviewer will actually ask you to demonstrate. Stars measure enthusiasm; they say nothing about whether tenant A can reach tenant B's data, whether a refund tool paged a human before it ran, or whether your audit log can be edited without anyone noticing. This pillar walks five control areas — multi-tenancy, server-enforced approval, principal propagation across delegation, runtime governance, and tamper-evident audit — and says plainly, capability by capability, what LangChain, LangGraph, CrewAI, AutoGen, and Pydantic AI ship today versus what they leave you to build. You'll finish with a checklist you can score your own stack against, and a runnable file that proves three of the five controls in one process.

<!-- more -->

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## The five controls an enterprise audit actually checks

A demo agent and an audited production agent share almost no non-functional requirements. The demo needs a model and a loop. The audited version needs the boring stuff to be *default behavior*, because a reviewer will ask you to show it working, not describe it:

- **Multi-tenancy.** Is `tenant_id` a real isolation boundary across cache, memory, rate limits, and audit — or a field you promise to thread through everywhere by hand?
- **Server-enforced approval.** When a consequential tool runs, is a human approval enforced *at the tool server*, deny-by-default, for every client — or is it a pause your own code has to remember to insert?
- **Principal propagation across delegation.** When agent A delegates to agent B, does the original caller's identity (`user_id`, `roles`, `tenant_id`) travel with the request so B is subject to the same rules — or does identity evaporate at the first hop?
- **Runtime governance.** For a long-running agent, are there ceilings on cost and irreversible actions, plus behavioral anomaly detection — or just a step counter?
- **Tamper-evident audit.** Can you prove, cryptographically, that the record of who did what wasn't reordered or edited after the fact — or is it plain application logging?

The [honest Why Promptise breakdown](../../getting-started/why-promptise.md) is written against exactly these criteria, and, unusually for a framework's own docs, it tells you when a competitor is the better pick. That's the spirit of this checklist too.

## What other frameworks do today

These are capable frameworks, and each is excellent at what it was built for. The gap isn't quality — it's that most of them are *integration toolkits or orchestration layers*, not a control plane. Here is what they genuinely ship, described as precisely and fairly as we can as of this writing.

**LangChain and LlamaIndex** are integration toolkits, and their breadth of connectors, loaders, retrievers, and provider wrappers is unmatched. Their memory and vector stores are single-namespace by default: one store, one keyspace. There is no tenant boundary, no agent-identity primitive, no server-enforced approval, and no runtime-governance object in the core. You can build all of it, and many teams do — but you build and own it. (Whether the multi-tenancy story is different is a common question; we dug into it in [Does LangChain support multi-tenancy?](does-langchain-support-multi-tenancy.md).)

**LangGraph** goes further, and it deserves credit for it. It adds durable checkpointing — graph state persists across steps and survives restarts — and a graph-side `interrupt()` that pauses a run to collect human input. That is a real human-in-the-loop mechanism. The exact delta: `interrupt()` pauses *the graph you wrote, inside your process*. It is not a deny-by-default gate the tool server enforces on every client that calls the tool. Its runaway-loop guard is `recursion_limit`, which is a genuine ceiling — but it counts super-steps, not cost or irreversible actions, and it isn't behavioral anomaly detection. Structural tenancy isn't part of the model.

**CrewAI** ships real self-throttles: `max_iter` caps a crew's reasoning iterations and `max_rpm` caps its request rate. Those are honest limits. The delta is that they are per-crew self-limits the crew imposes on itself — not per-tenant governance with escalation, and not a separate, hard ceiling on irreversible actions.

**AutoGen** ships a genuine distributed, message-passing runtime for multi-agent systems, which is a real strength when you're scaling conversations across processes. But message passing is transport; it is not tenancy, nor per-actor tamper-evident audit — those remain yours to build.

**Pydantic AI** is deliberately lean and typed, with structured output as the default. That leanness is a feature. It also means memory, tenancy, approval, and governance are explicitly out of scope.

The honest summary: none of these — LangChain, LlamaIndex, LangGraph, CrewAI, AutoGen, or Pydantic AI — makes tenant isolation, principal propagation across delegation, tamper-evident per-actor audit, or runtime governance a *structural invariant*. Each is left to the developer. Promptise's edge isn't that these frameworks "can't" do it; it's that Promptise makes all five first-class in one `pip install promptise`, so they're on by default instead of assembled by you.

## The checklist: five controls, five frameworks

Here's the scorecard. It's not a "we win every row" table for its own sake — the middle column names what these tools actually give you, so you can judge the delta yourself.

| Control | Where it stands in LangChain / LlamaIndex / LangGraph / CrewAI / AutoGen / Pydantic AI | Promptise Foundry |
|---|---|---|
| **1. Multi-tenancy** | Single-namespace stores; `tenant_id` is yours to thread through cache, memory, rate limits, and audit | `tenant_id` from a configurable JWT claim, tenant-qualified rate-limit buckets, `RequireTenant` guards, `MCPServer(require_tenant=True)` server invariant |
| **2. Server-enforced approval** | LangGraph `interrupt()` pauses your graph in-process; others leave it to you | `@server.tool(requires_approval=True)` + `ApprovalGateMiddleware`, deny-by-default on timeout, enforced for any MCP client |
| **3. Principal propagation** | Not a core primitive; identity is yours to carry across hops | `CallerContext` carries `user_id` / `roles` / `tenant_id` through every delegated hop |
| **4. Runtime governance** | LangGraph `recursion_limit` (step count); CrewAI `max_iter` / `max_rpm` (self-throttle) | Budget (cost + irreversible-action ceilings), behavioral health checks, mission constraints, scoped secrets |
| **5. Tamper-evident audit** | Application logging; ordering and tamper-evidence are yours | HMAC-chained `AuditMiddleware` with `verify_chain()` that detects edits and reordering |

Score your current stack against these five rows. Every cell in the middle column that reads "yours to build" is a subproject with an owner, a backlog, and an on-call rotation — before you've written a line of agent behavior.

## Three of the five controls in one runnable file

Checklists are easy to hand-wave. This file isn't a diagram — it's a complete, runnable Promptise MCP server that demonstrates **multi-tenancy**, **verified principal propagation**, and **tamper-evident audit** at once, using the in-process `TestClient` so there's no network to stand up. The principal is *checked* from a JWT, not asserted by the caller; the tenant is read from a claim; and the audit chain catches an insider edit.

```python
# soc2_audit.py — least-privilege access control + a verifiable audit trail
import asyncio
import json

from promptise.mcp.server import (
    MCPServer, AuthMiddleware, JWTAuth, AuditMiddleware, TestClient,
    RequestContext, HasRole,
)

SECRET = "rotate-me-in-prod"          # prod: PROMPTISE_AUDIT_SECRET from a vault

server = MCPServer(name="billing-api")

# 1. Verify the caller's JWT server-side. The principal is CHECKED, not asserted;
#    the tenant is read from the `tenant_id` claim onto ctx.client.tenant_id.
auth = JWTAuth(secret=SECRET)
server.add_middleware(AuthMiddleware(auth, tenant_claim="tenant_id"))

# 2. One HMAC-chained trail. include_args stays False, so amounts and order ids
#    (potential PII) never enter the evidence log.
audit = AuditMiddleware(log_path="billing-audit.jsonl", signed=True, hmac_secret=SECRET)
server.add_middleware(audit)


# Capability-based per-tool guard: only a billing-clerk agent may issue refunds.
@server.tool(auth=True, guards=[HasRole("billing-clerk")])
async def refund(order_id: str, amount: float, ctx: RequestContext) -> dict:
    """Issue a refund — a consequential action a SOC 2 reviewer scrutinizes."""
    return {"order_id": order_id, "refunded": amount, "by": ctx.client.subject}


async def main() -> None:
    # Two IdP-issued tokens in tenant "acme": one clerk, one read-only analyst.
    clerk = auth.create_token(
        {"sub": "billing-agent", "roles": ["billing-clerk"], "tenant_id": "acme"}
    )
    analyst = auth.create_token(
        {"sub": "analytics-agent", "roles": ["analyst"], "tenant_id": "acme"}
    )

    # Allowed: the clerk agent holds the required capability.
    ok = TestClient(server, meta={"authorization": f"Bearer {clerk}"})
    await ok.call_tool("refund", {"order_id": "A-1001", "amount": 49.0})

    # Denied: the analyst lacks billing-clerk. Least privilege, enforced
    # server-side — and the rejected attempt is recorded, not swallowed.
    denied = TestClient(server, meta={"authorization": f"Bearer {analyst}"})
    result = await denied.call_tool("refund", {"order_id": "A-1002", "amount": 5000.0})
    print("analyst refund ->", json.loads(result[0].text)["error"]["code"])   # ACCESS_DENIED

    # Two entries, both attributed to a VERIFIED principal (not a self-report):
    # the allowed action AND the denied attempt.
    for e in audit.entries:
        print(e["identity"]["subject"], e["tool"], "->", e["status"])
    print("chain valid:", audit.verify_chain())      # True

    # An insider edits the trail to hide who was refused...
    audit.entries[1]["identity"]["subject"] = "someone-else"
    print("chain valid:", audit.verify_chain())      # False — tamper detected


asyncio.run(main())
```

Three checklist rows, one file: the tenant rides in on the `tenant_id` claim, the audited subject is the *verified* JWT principal rather than a self-report, and `verify_chain()` flips to `False` the instant an entry is edited. The two controls not shown here — server-enforced approval and runtime governance — are the same shape of "declare it, the framework enforces it": approval is `@server.tool(requires_approval=True)` backed by `ApprovalGateMiddleware` (a declared-but-ungated tool refuses to build, so you can't ship an unenforced gate), and governance is a `BudgetConfig` with `max_irreversible_per_run` and per-tool `ToolCostAnnotation` weights, a `HealthMonitor` for behavioral anomalies, and a `MissionTracker` for objective completion.

## Making all five structural, not bolt-on

The reason a checklist matters is that these controls are *load-bearing together*. Tenant isolation without principal propagation leaks the moment one agent delegates to another. Approval gates without tamper-evident audit can't prove the approval happened. Budgets without health checks stop a spender but not a spinner. That's why Promptise treats the five as one design decision instead of five plugins: identity flows through `CallerContext` on every hop, the same `ApprovalHandler` protocol serves both agent-side and server-side gates, tenant-qualified rate limits and audit entries share the same verified `tenant_id`, and governance runs in the runtime around the agent rather than inside your prompt.

The end-to-end version of this — a tenant-isolated platform with approval gates, propagated identity, and a verifiable trail — is the [Secure Multi-Tenant Platform guide](../../guides/secure-multi-tenant-platform.md). Two of the five controls have their own deep dives worth reading alongside this checklist: our honest look at [which agent frameworks actually enforce tool approval](agent-framework-tool-approval-comparison.md) unpacks the server-enforced-vs-in-process distinction, and if you're coming from another framework, the [migration guide](../../resources/migration.md) maps your existing agents, tools, and stores onto Promptise's primitives so you're not rewriting from scratch to gain the controls.

## Frequently asked questions

### What makes an agent framework "enterprise-ready" versus just "production-capable"?

Production-capable means it can serve real traffic. Enterprise-ready means it can pass an audit: you can *demonstrate* tenant isolation, prove a human approved a consequential action, show that a delegated call carried the original caller's identity, and hand a reviewer a tamper-evident record. The delta is almost never the model or the tool-calling loop — it's whether these five controls are structural invariants or homework.

### Do LangChain, LangGraph, or CrewAI fail this checklist?

No — "fail" is the wrong frame, and this checklist isn't a pass/fail exam. They're strong tools that draw their scope differently. LangGraph genuinely ships durable checkpointing and an in-process `interrupt()`; CrewAI genuinely ships `max_iter` and `max_rpm` throttles; LangChain ships unmatched integration breadth. The precise point is that none of them makes tenant isolation, principal propagation, server-enforced approval, tamper-evident audit, and cost/irreversible governance *first-class and default* — each is left to you to build and own. Promptise's contribution is bundling all five into one install.

### Can't I just build these five controls myself on top of any framework?

You can, and plenty of teams have. The question is whether you want to own five security-critical subsystems — a tenant boundary that never leaks, a deny-by-default approval gate, identity that survives delegation, an HMAC-chained log, and a governance layer — as ongoing infrastructure. Building them once is doable; keeping them correct across every new tool, hop, and release is the expensive part. That maintenance is exactly what a control plane is for.

### Is any of this vendor lock-in?

The controls are built on open primitives: JWT auth, the Model Context Protocol for tools, standard vector stores for memory, and plain JSONL for the audit trail. Your MCP servers work with any MCP-compatible client, and the model string is swappable across OpenAI, Anthropic, and local Ollama. The migration guide exists precisely so moving in — and, in principle, out — is a mapping exercise, not a rewrite.

## Next steps

Run the checklist against your current stack: for each of the five controls, decide whether it's a structural invariant or a subproject you own. Then read the [Why Promptise](../../getting-started/why-promptise.md) breakdown to see the criteria applied end to end, stand up the tenant-isolated reference in the [Secure Multi-Tenant Platform guide](../../guides/secure-multi-tenant-platform.md), and if you're switching, use the [migration guide](../../resources/migration.md) to map your agents across. When you're ready to get the whole control plane in one install: `pip install promptise`.
