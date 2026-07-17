---
title: "AI Agent Identity & Authentication: The Complete Guide"
description: "Most 'AI agent auth' results conflate the LLM's API key with the agent's identity; this hub separates the two and frames agents as a new class of non-human…"
keywords: "AI agent identity, agent authentication, who is acting AI agent, non-human identity, agent identity vs model credential, traceable agent identity"
date: 2026-07-16
slug: ai-agent-identity
categories:
  - Identity
---

# AI Agent Identity & Authentication: The Complete Guide

AI agent identity is the answer to a deceptively simple question: *which agent did this?* If you searched for "AI agent authentication" and landed on a page about storing your OpenAI key safely, you found the wrong thing — that secures the model call, not the actor. This guide separates the two clearly, explains why an autonomous agent is a new kind of non-human actor that needs its own identity, and shows you how to give one a traceable identity in a few lines of Python. By the end you'll know when a name-only identity is enough and when you need a signed, verifiable one your identity provider mints.

<!-- more -->

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## Agent identity vs. the model credential

The single most common mistake in this space is treating the LLM's API key as the agent's identity. They are unrelated concerns.

- The **model credential** authenticates the *call to the LLM provider*. It answers "is this a paying, authorized request to OpenAI or Anthropic?"
- The **agent identity** answers "*who is the actor* taking actions in your systems?" — calling tools, hitting internal APIs, delegating to peers, writing to databases.

An agent can share a model credential with a hundred other agents and still need its own distinct identity. Attribution, least privilege, and audit all hinge on *who acted*, not on how the model turn was billed. Keeping this distinction sharp — **agent identity vs. model credential** — is the whole reason this topic deserves its own answer. The [Agent Identity overview](../../identity/overview.md) states it plainly: identity is orthogonal to the model; the model keeps its own authentication, and identity is about who is acting for the purposes of attribution and authorization.

## Agents are a new class of non-human identity

Enterprises already have a mature model for non-human actors: service accounts. A CI pipeline, a cron job, and a microservice each get an identity in a directory, a short-lived credential, and a place where they can be inventoried and revoked. Agents are the newest members of that family — but most teams run them with no real identity at all.

Instead, the typical setup is one of two anti-patterns:

- A **shared API key** across a fleet of agents. Every agent that holds it has identical access, and after an incident you cannot tell which one acted.
- A **self-asserted name** — a string the process prints into a log. Nothing verifies it, so it is worthless for audit or authorization.

This matters because agents act continuously, often with no human in the loop, and often on untrusted input. When a reviewer asks *who performed this action*, "the model did it" is not an answer a SOC 2 or ISO audit accepts. The industry has started treating agents as first-class **non-human identity** for exactly this reason — the same shift that gave workloads their own identities in Kubernetes and cloud IAM. If you want the broader framing of why workloads (and now agents) get directory-issued identities, see [What Is Workload Identity for AI Agents?](workload-identity-for-ai-agents.md).

## Two tiers of AI agent identity: local and verifiable

Promptise Foundry models this with **`AgentIdentity`** — a two-tier design so you can start with attribution today and upgrade to cryptographic verification when a resource actually needs to *trust* the caller. You don't rewrite anything to move between tiers; you swap one constructor.

**Tier 1 — local identity.** Just an `agent_id` (plus optional `name`, `owner`, and `labels`). No infrastructure, no keys. It is the value the framework stamps onto the observability timeline and audit log so you can trace which agent did what across a fleet.

**Tier 2 — verifiable identity.** The same identity, additionally backed by a credential provider — Microsoft Entra, AWS IAM, Google Cloud, SPIFFE/SPIRE, or a generic OIDC issuer — that mints a short-lived, signed JWT proving the identity. The agent presents that credential to the resources it calls, so an MCP server or internal API can *verify* the caller instead of trusting a self-asserted string.

The key design decision: your identity provider is the system of record, not Promptise. The framework keeps **no identity store of its own** — it authenticates against your IdP and uses the identity the IdP issues, which is where you create, inventory, govern, and revoke agents. That means "verifiable" costs you no new secrets to manage.

## Give your agent a traceable identity in code

Here's a complete, runnable Tier 1 example. Creating the identity needs nothing at all; running it through an agent needs only a model API key. Every tool call and LLM turn is stamped with `agent_id="billing-bot"`.

```python
import asyncio

from promptise import AgentIdentity, build_agent


async def main() -> None:
    # Tier 1 — a local identity. No infrastructure, no extra keys.
    identity = AgentIdentity(
        "billing-bot",
        name="Billing Bot",
        owner="payments-team",
        labels={"env": "prod"},
    )
    print(identity.agent_id)       # "billing-bot"
    print(identity.is_verifiable)  # False — attribution only
    print(identity.claims())       # {"agent_id": "billing-bot", "verifiable": False, ...}

    # Attach it to an agent and turn on the timeline.
    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={},
        identity=identity,
        observe=True,   # every turn is now tagged with agent_id="billing-bot"
    )
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Summarize today's invoices."}]}
    )
    print(result["messages"][-1].content)
    await agent.shutdown()


asyncio.run(main())
```

When the agent starts calling *protected* resources, upgrade to Tier 2. `AgentIdentity.auto()` detects your platform from environment markers and picks the right provider; the signed credential is then presented to your MCP servers automatically as their bearer token:

```python
from promptise import AgentIdentity, build_agent
from promptise.config import HTTPServerSpec

# Tier 2 — a verifiable identity minted by your IdP.
identity = AgentIdentity.auto("billing-bot")   # Entra / AWS / GCP / SPIFFE / OIDC
identity.auth_header()                          # {"Authorization": "Bearer <signed jwt>"}

agent = await build_agent(
    model="openai:gpt-5-mini",
    servers={"tools": HTTPServerSpec(url="https://tools.internal/mcp")},
    identity=identity,   # presented to "tools" automatically
)
```

You can also declare identity outside code entirely, in a `.superagent` YAML file, and let the CLI apply it. The [Agent Identity quickstart](../../identity/quickstart.md) walks through both the local and verifiable paths in about five minutes.

## Where a traceable agent identity shows up

Giving an agent an identity is only useful if that identity travels with its actions. In Promptise it flows through four touch points automatically:

- **Observability** — every tool call and LLM turn the agent records is tagged with its identifier (the `agent_id`, or the IdP-assigned `subject` for a verifiable identity), so the timeline tells you which agent acted.
- **MCP & APIs** — a verifiable identity is presented to MCP servers automatically; the server verifies the JWT and authorizes the agent with its existing auth and role or client-id guards. If you want the server side of that handshake, the walkthrough in [JWT Authentication for MCP Servers: Step by Step](jwt-authentication-for-mcp-servers.md) covers verifying and attributing the caller.
- **Audit** — the tamper-evident audit log records the verified agent identity (subject, issuer, audience, roles) inside its HMAC-chained entries, so "who did what" survives after the fact.
- **Cross-agent delegation** — when one agent hands work to another, the peer records *who* delegated, so the chain of action stays attributable.

For a full end-to-end scenario — one agent, two MCP servers with different audiences, a delegated sub-task, and a complete audit trail — the [end-to-end identity guide](../../identity/guide.md) wires all four touch points together.

## When a local identity is all you need

Verifiable identity is not always the right investment, and it's worth being honest about that. If you run a single agent on your laptop, in tests, or in a trusted internal batch job where no resource needs to *cryptographically verify* the caller, a Tier 1 local identity is genuinely enough — you get attribution on the timeline and audit log with zero infrastructure. Reach for a verifiable, IdP-backed credential when at least one of these is true:

- Agents call protected MCP servers or internal APIs that must not trust a self-asserted name.
- You have audit or compliance requirements that demand a *verified* subject, not a logged string.
- You run multi-tenant or accept untrusted input, and you need least-privilege scoping to bound the blast radius.

Adopting the local tier first is a deliberate on-ramp, not a compromise: the same `AgentIdentity` object upgrades in place when you're ready.

## Frequently asked questions

### Is an AI agent's identity the same as its API key?

No. The API key authenticates the call to the model provider; the identity says *who the acting agent is* in your systems. One shared model credential can back many agents, each of which still needs its own distinct identity for attribution, authorization, and audit. Conflating the two is the most common mistake in agent authentication.

### Do I need an identity provider to give an agent an identity?

Not to start. A local identity is just an `agent_id` with optional metadata and needs no infrastructure — it's stamped onto the observability timeline and audit log immediately. You only need an identity provider (Entra, AWS, GCP, SPIFFE, or OIDC) when a resource the agent calls must *verify* the caller with a signed credential rather than trust a name.

### How do I answer "which agent did this?" across a fleet?

Attach an `AgentIdentity` to each agent and enable observability. Every tool call, LLM turn, and audit entry is then tagged with that agent's identifier, so a fleet-wide timeline and tamper-evident log can tell you exactly which agent, owned by which team, took any given action.

## Next steps

Give an agent a traceable identity in five minutes with the [local-identity quickstart](../../identity/quickstart.md) — no infrastructure required — then read the [Agent Identity overview](../../identity/overview.md) to see how attribution, outbound auth, and audit fit together. New to the framework? Start with the [Quick Start](../../getting-started/quickstart.md) and add an `identity=` argument to your first `build_agent()` call.
