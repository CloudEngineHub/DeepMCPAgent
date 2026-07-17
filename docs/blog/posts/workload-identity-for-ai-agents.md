---
title: "What Is Workload Identity for AI Agents?"
description: "Explains why the shared-API-key anti-pattern that humans abandoned is exactly what most agent fleets still run, and maps agents onto the workload-identity…"
keywords: "workload identity for AI agents, non-human workload identity, verifiable agent identity, IdP-minted agent credentials, short-lived JWT for agents, least privilege agents"
date: 2026-07-16
slug: workload-identity-for-ai-agents
categories:
  - Identity
---

# What Is Workload Identity for AI Agents?

Workload identity for AI agents is the practice of giving every agent a stable, verifiable identity of its own — minted by the identity provider you already run — instead of handing the whole fleet one shared API key. If you deploy more than one agent, you have probably already felt the gap: your logs say "the model" did something, your audit trail can't name which agent hit which tool, and every agent holds the same credential so every agent has the same blast radius. By the end of this post you will know exactly what workload identity means for agents, why it is the same problem enterprises solved for services years ago, and how to give your first agent a real identity with no new secrets to manage.

<!-- more -->

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## The shared-API-key anti-pattern humans already abandoned

Think back to how service-to-service auth used to work: one long-lived secret, copied into every box, rotated approximately never. The industry abandoned that for humans and services alike, and replaced it with workload identity — short-lived, provider-minted credentials scoped to a single principal. Microsoft Entra, AWS IAM, GCP service accounts, and SPIFFE/SPIRE all exist to answer one question reliably: *which workload did this?*

Agent fleets have quietly reintroduced the anti-pattern. An agent is a new kind of **non-human workload identity** — it calls tools, hits internal APIs, and acts continuously, often with no human in the loop. Yet most teams run agents with:

- **A shared LLM or API key**, so every agent has identical access and nothing to distinguish them after the fact.
- **A self-asserted name** — a string a process prints about itself in a log, which anyone or anything can print too.

That breaks down on exactly the axes reviewers care about: attribution ("which agent did this?" has no trustworthy answer), least privilege (you can't scope one agent to billing and another to read-only when they share a key), and audit ("the model did it" is not a SOC 2 answer). The good news is that you do not need a new system to fix it. You need to plug agents into the workload-identity model you already trust.

## What workload identity for AI agents actually means

It helps to separate two tiers, because you can adopt the first today and the second when you need it. The [Agent Identity overview](../../identity/overview.md) lays out the same split in more depth.

- **Local identity** — just a stable `agent_id` (plus optional name, owner, and labels). It needs zero infrastructure. Promptise stamps it onto every observability event, tool call, and audit entry, so "which agent did this?" finally has a reliable answer within your own systems.
- **Verifiable agent identity** — additionally backed by a credential provider that mints a signed, **short-lived JWT for agents** proving the identity. The agent presents this token to the resources it calls — an MCP server, an internal HTTP API — so they authenticate the caller cryptographically instead of trusting a self-asserted id.

The important mental shift: this is **not** the LLM's credential. The model keeps its own authentication. Identity is about *who is acting*, for attribution and authorization — a distinct concern from *how the model talks to its provider*.

## Map your agents onto the IdP you already run

Here is the part that makes adoption cheap: there are no new secrets to invent. Promptise's `AgentIdentity` consumes **IdP-minted agent credentials** from the platform you are already on. Detection picks the right provider automatically, or you can pin one explicitly:

- **Entra** — managed identity or projected federated tokens
- **AWS IAM** — instance/role credentials
- **GCP** — service-account identity tokens
- **SPIFFE/SPIRE** — workload SVIDs
- **Generic OIDC** — any issuer you already run

The following is runnable as-is on the local path. The verifiable variants are shown commented because they need cloud infrastructure present — an honest reflection of what each tier requires:

```python
import asyncio
from promptise import build_agent, AgentIdentity


async def main():
    # Local identity — zero infrastructure, attribution starting today.
    identity = AgentIdentity(
        "billing-bot",
        name="Billing Bot",
        owner="payments",
        labels={"env": "prod", "team": "payments"},
    )

    # Verifiable identity — let Promptise detect your platform
    # (Entra / AWS / GCP / SPIFFE) and mint a short-lived JWT:
    #   identity = AgentIdentity.auto("billing-bot")
    #
    # Or pin the provider explicitly:
    #   identity = AgentIdentity.from_entra(
    #       "billing-bot",
    #       client_id="<managed-identity-client-id>",
    #       resource="api://my-mcp-server",
    #   )

    agent = await build_agent(
        model="openai:gpt-5-mini",
        instructions="You reconcile invoices. Never touch anything outside billing.",
        identity=identity,
    )

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Summarize open invoices."}]}
    )
    print(result["messages"][-1].content)
    await agent.shutdown()


asyncio.run(main())
```

Swap the local `AgentIdentity(...)` for `AgentIdentity.auto("billing-bot")` once you are running on a supported platform, and `billing-bot` starts presenting a provider-signed token to the MCP servers and APIs it calls. The [Entra provider guide](../../identity/providers/entra.md) walks through managed-identity versus projected-token modes and the exact environment markers detection looks for.

## Least privilege agents, not one over-privileged fleet

Attribution is the first win; **least privilege agents** is the second. Once each agent presents a verifiable identity, the resources it calls can authorize *that* principal instead of trusting a shared key that grants everyone everything.

On the receiving side, an MCP server verifies the IdP-signed token with asymmetric verification (RS256/ES256 via `AsymmetricJWTAuth`) and gates tools per role — `billing-bot` gets the billing tools, a read-only reporting agent gets none of the mutating ones. That server-side half is a short step if you have set up token auth before; the walkthrough in [JWT Authentication for MCP Servers: Step by Step](jwt-authentication-for-mcp-servers.md) covers the verification and guard wiring. The result is a system where compromising one agent's short-lived credential exposes only that agent's scope — not the entire fleet — and every mutating action is attributable to a subject your IdP vouches for.

The end-to-end recipe, from minting the credential to recording the verified subject in the tamper-evident audit log, lives in the [Agent Identity guide](../../identity/guide.md).

## When a plain local identity is the better fit

Verifiable identity is not always the right first move, and it is worth being straight about that. A **local identity** is genuinely the better fit when:

- You are prototyping or running a single agent that only touches its own local resources — attribution in your own logs is all you need, and standing up IdP wiring would be pure overhead.
- Your agents call services that do not (yet) verify tokens. A signed credential nothing checks buys you nothing over a well-labeled local id.
- You have no platform IdP to lean on. Verifiable identity's whole value is *reusing* infrastructure you already trust; if there is none, start local and add a credential when the provider exists.

Start with a local id for clean attribution today, then upgrade the same agent to a verifiable one by changing a single constructor call when a protected resource actually requires it. Nothing else in your agent code changes.

## Frequently asked questions

### Is workload identity for AI agents the same as the LLM API key?

No. The LLM API key authenticates your code to the model provider — it is *how the model talks*. Workload identity answers *who is acting*: which agent made a tool call or hit an API, for attribution and authorization. They are separate credentials with separate lifecycles, and Promptise treats identity as distinct from the model credential.

### Do I need new secrets or a new identity system?

No. That is the point of consuming **IdP-minted agent credentials**: `AgentIdentity` uses the provider you already run — Entra, AWS IAM, GCP, SPIFFE/SPIRE, or a generic OIDC issuer — to mint short-lived tokens. There is no new vault, no new shared key, and nothing long-lived to rotate by hand.

### What is the difference between local and verifiable agent identity?

A local identity is a stable `agent_id` used for attribution across your own logs, audit trail, and observability — zero infrastructure. A verifiable agent identity adds a provider-signed, short-lived JWT the agent presents to the resources it calls, so those resources can authenticate and authorize it cryptographically. You can start local and upgrade in place.

## Next steps

See which provider fits your infrastructure and mint your agent's first short-lived credential: pick your platform in the [Agent Identity guide](../../identity/guide.md), then wire it into `build_agent`. If you want the broader picture of how identity, authentication, and MCP auth fit together, read [AI Agent Identity & Authentication: The Complete Guide](ai-agent-identity.md), and if you are new to the framework, start with the [Quick Start](../../getting-started/quickstart.md).
