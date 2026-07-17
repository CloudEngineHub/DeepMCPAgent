---
title: "Can CrewAI Propagate a User's Identity Across Delegation?"
description: "CrewAI can delegate work from one agent to another, but when it does the original end-user principal evaporates — the second agent acts with whatever ambient…"
keywords: "propagate user identity across agent delegation, crewai agent identity, does crewai propagate user identity, end-user identity multi-agent, agent delegation authorization"
date: 2026-07-16
slug: propagate-user-identity-across-agent-delegation-2
categories:
  - Comparisons
---

# Can CrewAI Propagate a User's Identity Across Delegation?

If you need to **propagate user identity across agent delegation**, the honest answer for CrewAI is: not out of the box. CrewAI can absolutely hand work from one agent to another — that part works well and has for a long time. What it doesn't do is carry the *requesting human* along for the ride. When agent A delegates to agent B, the person who asked the question disappears, and B runs with whatever ambient credentials the process happens to hold. This post explains exactly where the principal is lost, what CrewAI actually does today, and how Promptise Foundry's `CallerContext` keeps `user_id`, `tenant_id`, `roles`, and `scopes` intact across every delegated hop.

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## Where the principal disappears in a delegated hop

Delegation is a text hand-off. An orchestrator agent decides "the billing specialist should handle this," packages the task as a string, and passes it to a coworker agent. The coworker reads the task, does its work, and returns a result. That flow is fine for *reasoning*, but it quietly drops something important: the identity of the human who started the request.

Once the principal is gone, three things break at once:

- **Least privilege collapses.** The delegated agent can't be scoped to "what *this user* is allowed to do," because it no longer knows who the user is. It acts with the crew's shared access, which is usually the union of everything.
- **Audit attribution goes fuzzy.** Your logs record "the crew did X" or "the billing agent did X," not "`acme::user-42` caused X via delegation." After an incident, that's the difference between a clean answer and a shrug.
- **Per-user isolation surfaces leak.** Memory search, semantic cache, and conversation ownership all key on *some* identity. If the delegated agent has none, it either shares one global scope or invents its own — and in a multi-tenant system, that's exactly how tenant A ends up seeing tenant B's cached answer.

The fix isn't clever prompting. It's an identity object that rides the delegation automatically, so the second agent is *born* knowing who it's acting for.

## What CrewAI (and the others) actually do today

Let's be precise and fair, because "framework X can't do Y" is easy to get wrong.

**CrewAI has real delegation.** Set `allow_delegation=True` on an agent and CrewAI injects two coworker tools — "Delegate work to coworker" and "Ask question to coworker" — that let one agent route a task or a question to another agent by name. This is a genuine, useful feature, and it's more than some frameworks ship.

The gap is narrower and more specific: **those tools carry a task string and a bit of context, not a caller principal.** There is no per-request object representing the end user that flows into the coworker and scopes what it can touch. The delegated agent runs under the same process-level credentials and API keys the crew was configured with, so downstream tool calls and any audit you build attribute the action to *the crew*, not the person who asked. CrewAI also has no first-class workload identity for the acting agent (no Entra / AWS IAM / GCP / SPIFFE integration) — if you want either the end-user principal or a verifiable agent identity, you build and thread both yourself.

Here's the honest per-framework picture for this one capability — *does the requesting user's identity survive a delegated hop?*

| Framework | Delegation mechanism | Carries the end-user principal? |
|---|---|---|
| **CrewAI** | `allow_delegation` + coworker tools | No — task text only; coworker runs under shared crew credentials |
| **AutoGen** | Message passing between agents | No — messages carry content, not a caller identity |
| **LangGraph** | Shared state / `config` through subgraphs | Partial — you *can* thread a value through, but there's no defined principal that automatically scopes memory, cache, guardrails, conversation ownership, and audit |
| **Promptise Foundry** | `cross_agents` + `CallerContext` | Yes — `user_id`/`tenant_id`/`roles`/`scopes` inherited on every hop |

Note the LangGraph row carefully: it's a *partial*, not a *no*. LangGraph's state and `config` genuinely flow into subgraphs, so you can stuff a user id in there. What's missing is a *governing* principal — one identity that the framework itself uses to scope every per-user surface downstream. Threading a raw value is not the same as the framework enforcing isolation on it. Promptise's edge here is structural: the principal isn't a value you remembered to pass, it's the thing the whole isolation model keys on.

## How Promptise keeps the principal alive

In Promptise, you attach one `CallerContext` at the top of the request and it becomes ambient for the entire async task — including any agent you delegate to. When a coordinator calls a peer, the peer inherits that context automatically instead of overwriting it with `None`. You don't re-pass it, and you can't forget to.

```python
import asyncio
from promptise import build_agent, CallerContext
from promptise.identity import AgentIdentity
from promptise.cross_agent import CrossAgent


async def main() -> None:
    # A specialist peer. It will act on behalf of the ORIGINAL user,
    # not under some shared crew credential.
    billing = await build_agent(
        model="openai:gpt-5-mini",
        servers={},
        instructions="You answer billing questions for the requesting user.",
        identity=AgentIdentity("billing-bot", name="Billing Bot", owner="payments"),
    )

    # The coordinator delegates to the peer. Passing `identity=` also stamps
    # a "delegated by" marker on the peer's timeline for attribution.
    coordinator = await build_agent(
        model="openai:gpt-5-mini",
        servers={},
        instructions=(
            "You triage support requests. Delegate billing questions to the "
            "billing specialist, then summarize the answer."
        ),
        identity=AgentIdentity("support-coordinator", owner="support"),
        cross_agents={
            "billing": CrossAgent(
                agent=billing,
                description="Answers billing and invoice questions",
            ),
        },
    )

    # The real end user. tenant_id + user_id + roles ride through every hop.
    caller = CallerContext(user_id="user-42", tenant_id="acme", roles={"customer"})

    result = await coordinator.ainvoke(
        {"messages": [{"role": "user", "content": "Why was I charged twice this month?"}]},
        caller=caller,
    )

    # When the coordinator invokes ask_agent_billing (or broadcast_to_agents),
    # the billing peer inherits `caller` from the ambient context automatically.
    # Its cache, memory, guardrails, conversation ownership, and audit all stay
    # scoped to isolation_key "acme::user-42" — never the shared process.
    print(result["messages"][-1].content)

    await coordinator.shutdown()
    await billing.shutdown()


asyncio.run(main())
```

Two things are happening in that snippet, and both matter:

1. **`CallerContext` propagates the human.** The `caller` you pass to the coordinator's `ainvoke()` is stored in an async-safe context variable. When the LLM decides to call `ask_agent_billing`, the billing peer's own `ainvoke()` reads that same ambient context, so it scopes to `isolation_key` `"acme::user-42"`. Two tenants that happen to share a `user_id` can never collide, because tenant isolation is baked into the key derivation rather than left to convention. The same inheritance holds for `broadcast_to_agents` when you fan a question out to several peers.
2. **`AgentIdentity` names the actor.** Each agent gets a stable, traceable identity, so the peer's timeline records both *who acted* (`billing-bot`) and *who delegated* (`support-coordinator`) — and, when you want it, a verifiable credential minted per resource from Entra, AWS IAM, GCP, SPIFFE, or a generic OIDC issuer. That's the [agent identity overview](../../identity/overview.md) in one line: attribution first, a verifiable credential when you need one.

The delegation mechanics themselves — the auto-generated `ask_agent_<name>` and `broadcast_to_agents` tools, timeouts, and context injection — are covered in full in [cross-agent delegation](../../core/agents/cross-agent.md). The identity plumbing rides *through* that machinery; you don't wire it separately.

## Why the delta matters in production

The reason to care isn't philosophical neatness — it's what a reviewer or an incident asks of you.

**Least privilege becomes enforceable.** Because the delegated agent knows it's acting for `acme::user-42` with role `customer`, downstream guardrails and tools can make decisions scoped to that user instead of granting crew-wide access. This is the same "default-on governance" thesis behind [Why Promptise Foundry](../../getting-started/why-promptise.md): the boring safety infrastructure has to be structural, not something you remember to bolt on after the demo works.

**Attribution survives the hop.** When audit records "`billing-bot`, delegated by `support-coordinator`, acting for `acme::user-42`, issued a refund," you can answer *who caused this* even three delegations deep. That's the answer SOC 2 and ISO reviewers actually want, and it's the answer a shared crew credential can't give.

If you're evaluating how much of this you'd have to build yourself in any given stack, the [Enterprise-Ready Agent Framework Checklist](enterprise-ready-agent-framework-checklist.md) turns it into concrete line items, and [Does LangChain Support Multi-Tenancy? The Honest Answer](does-langchain-support-multi-tenancy.md) walks the same "structural vs. do-it-yourself" distinction for the tenancy side of the problem.

## Frequently asked questions

### Does CrewAI propagate user identity across delegation?

Not automatically. CrewAI delegates *work* — via `allow_delegation` and its coworker tools — but the hand-off is a task string plus context, not a caller principal. The delegated agent runs under the crew's shared credentials, so there's no built-in `user_id`/`tenant_id` that flows into the coworker and scopes its access, cache, or audit. You can build that threading yourself, but it isn't a first-class object in the framework.

### Do I have to pass CallerContext to every peer manually?

No. You attach one `CallerContext` when you invoke the top-level agent, and it becomes ambient for the whole async task. Peers invoked through `ask_agent_<name>` or `broadcast_to_agents` inherit it — a peer that's called without an explicit `caller` reads the active context instead of clearing it. That inheritance is the whole point: forgetting to re-pass identity is the bug the design removes.

### What's the difference between CallerContext and AgentIdentity?

They answer two different questions. `CallerContext` is *who the request is for* — the end-user principal (`user_id`, `tenant_id`, `roles`, `scopes`) that scopes per-user isolation. `AgentIdentity` is *who is acting* — the agent's own stable, optionally IdP-verifiable identity used for attribution and, when you enable it, per-resource credentials. A delegated action records both: the acting agent and the human it acts for.

### Is Promptise delegation remote like CrewAI crews?

Cross-agent delegation is in-process: peers are LangChain `Runnable` objects (usually agents from `build_agent()`), so no extra infrastructure is needed. For work that spans machines, agents share MCP servers and exchange data through tools — and identity still travels, because the acting agent presents its verifiable credential to each MCP server it calls.

## Next steps

Keep the real principal alive across every hop. Read the [agent identity overview](../../identity/overview.md) to give each agent a traceable (and optionally verifiable) identity, wire `CallerContext` into your delegation with the [cross-agent delegation guide](../../core/agents/cross-agent.md), and see [Why Promptise Foundry](../../getting-started/why-promptise.md) for how attribution, least privilege, and tenant isolation stay default-on as your agent system grows.
