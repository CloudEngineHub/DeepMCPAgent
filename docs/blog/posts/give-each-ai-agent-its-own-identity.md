---
title: "Give each AI agent its own identity, not a shared key"
description: "A concrete how-to, not another 'identity gap' essay: stop threading one shared key through env and give every agent a distinct AgentIdentity in the…"
keywords: "give each AI agent its own identity, per-agent identity setup, unique identity per AI agent, AgentIdentity build_agent, stop sharing API keys across agents, least privilege per agent"
date: 2026-07-16
slug: give-each-ai-agent-its-own-identity
categories:
  - Identity
---

# Give each AI agent its own identity, not a shared key

Give each AI agent its own identity and "which agent did this?" stops being a guess: instead of threading one shared API key through `env` for a whole fleet, you attach a distinct `AgentIdentity` in the `build_agent()` call, and that identity gets stamped onto every tool call, every LLM turn, and every HMAC-chained audit entry the agent produces. This is a concrete how-to — not another essay about the "identity gap." By the end you'll have two agents that no longer share one credential or one blast radius: a billing bot that can issue refunds and a read-only reporter that can't, each traceable to itself.

## One shared key is one shared blast radius

Here's the setup almost every team starts with. You export one `API_KEY`, read it in every process, and point a fleet of agents at the same MCP servers. It works on day one. Then three things go wrong at once:

- **Attribution collapses.** When a bad refund lands in the ledger, every agent presented the same bearer, so the audit log can only tell you "the key" acted — not *which* agent, owned by which team.
- **Least privilege is impossible.** A billing bot that issues refunds and a reporter that only reads dashboards hold identical access, because access is a property of the shared key, not of the agent. The reporter can do everything the billing bot can.
- **Revocation is a redeploy.** Rotate the leaked key and you take down the whole fleet, because every agent trusted the same string.

The fix isn't a bigger key or a cleverer `env` layout. It's giving each agent a *distinct* identity — the same move humans made when they abandoned shared logins for per-user accounts, and the same move platforms already made for workloads with service accounts. The [Agent Identity overview](../../identity/overview.md) frames this as treating agents as a new class of **non-human actor** that deserves its own directory-style identity.

## Give each agent its own AgentIdentity in the build_agent() call

Per-agent identity setup is one object and one argument. `AgentIdentity` takes a stable `agent_id` plus optional `name`, `owner`, and `labels`; you pass it to `build_agent(identity=...)`, turn on `observe=True`, and every turn that agent records is now tagged with *its* id. No infrastructure, no extra keys — a local identity is pure attribution.

This is runnable end-to-end with only an `OPENAI_API_KEY` set:

```python
import asyncio

from promptise import AgentIdentity, build_agent


async def main() -> None:
    # Two agents, two DISTINCT identities — not one shared key.
    billing = AgentIdentity(
        "billing-bot",
        name="Billing Bot",
        owner="payments-team",
        labels={"env": "prod", "scope": "billing"},
    )
    reporter = AgentIdentity(
        "reporting-bot",
        name="Reporting Bot",
        owner="analytics-team",
        labels={"env": "prod", "scope": "read-only"},
    )

    # The id is what the framework stamps onto traces and audit entries.
    print(billing.agent_id, billing.claims())
    print(reporter.agent_id, reporter.claims())

    billing_agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={},
        identity=billing,   # distinct identity, attached right here
        observe=True,       # every turn is now tagged agent_id="billing-bot"
    )
    reporting_agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={},
        identity=reporter,  # a *different* identity — a different blast radius
        observe=True,
    )

    await billing_agent.ainvoke(
        {"messages": [{"role": "user", "content": "Summarize today's invoices."}]}
    )
    await reporting_agent.ainvoke(
        {"messages": [{"role": "user", "content": "Draft the weekly revenue report."}]}
    )

    await billing_agent.shutdown()
    await reporting_agent.shutdown()


asyncio.run(main())
```

`claims()` returns exactly what flows onto the timeline and audit log — `{"agent_id": "billing-bot", "name": "Billing Bot", "owner": "payments-team", "verifiable": False, "labels": {...}}` — and never a credential token. That's the whole `AgentIdentity` build_agent contract: attach an object, get per-agent attribution. The [Identity quickstart](../../identity/quickstart.md) walks the same path in about five minutes, including the `.superagent` YAML variant if you'd rather declare the identity outside code.

Two agents in the same process now each own a distinct identity. That already answers "which agent did this?" on the timeline. The next step is making that identity *bound access*, not just label it.

## Scope each agent's access separately, server-side

Attribution names the actor; least privilege per agent *limits* it. Once the identity is verifiable — backed by your IdP (Entra, AWS, GCP, SPIFFE, or a generic OIDC issuer) — the signed credential is presented to your MCP servers automatically, and the server decides *which* agent may call *which* tool. That's where the billing bot and the reporter finally stop sharing one blast radius.

On the server, verify the caller's token with `JwksAuth` and gate each tool with a guard keyed to the agent's identity:

```python
from promptise.mcp.server import (
    MCPServer, AuthMiddleware, JwksAuth, RequireClientId, HasRole, AuditMiddleware,
)

server = MCPServer(name="billing")

# Verify tokens this IdP issued for THIS resource. audience is required —
# it stops one agent replaying a token minted for a different server.
server.add_middleware(AuthMiddleware(JwksAuth.from_discovery(
    issuer="https://login.microsoftonline.com/<tenant>/v2.0",
    audience="api://billing",
)))

# Each entry records the VERIFIED acting agent inside a tamper-evident HMAC chain.
server.add_middleware(AuditMiddleware(log_path="billing-audit.jsonl", signed=True))


@server.tool(auth=True, guards=[RequireClientId("billing-bot")])
async def issue_refund(ctx, invoice_id: str, amount: float) -> str:
    # ctx.client.subject -> the verified identity of the calling agent
    return f"Refunded {amount} on {invoice_id}"


@server.tool(auth=True, guards=[HasRole("payments-admin")])
async def close_account(ctx, account_id: str) -> str:
    return f"Closed {account_id}"
```

Now the reporter's identity, presented to the same server, is rejected by `RequireClientId("billing-bot")` before `issue_refund` ever runs — the blast radius is drawn per identity, not per shared key. Scope one agent to billing and another to read-only, and revoke either one by disabling it in your directory: its short-lived credentials stop validating everywhere at once, with no server reconfiguration. The full outbound-and-inbound wiring — one identity, two audiences, delegation, and audit — is in the [end-to-end identity guide](../../identity/guide.md).

## What other frameworks do today

Being fair here matters, because most mainstream frameworks *do* give an agent some kind of label — it's just not a security identity you can scope access by. The precise deltas:

- **CrewAI** defines an agent with `role`, `goal`, and `backstory`. Those are descriptive strings that shape the agent's persona in prompts and show up in orchestration — genuinely useful, but a `role` here is a behavior label, not a verified principal. It isn't stamped onto each tool call for authorization, and you can't cryptographically gate a tool to "only the billing role." Credentials for the tools an agent calls are still API keys/bearers you place in `env` or tool config.
- **AutoGen** gives each agent a `name` used to route messages between agents (and, in 0.4, across its distributed runtime). That name is an addressable handle for the conversation graph — self-asserted routing metadata, not a credential the agent presents to a resource or a subject a server verifies before allowing a call.
- **LangGraph / LangChain** let you thread a `RunnableConfig` with `run_name`, `tags`, and `metadata` through a graph and its subgraphs, surfaced in LangSmith tracing. That's real observability, but the values are self-asserted trace labels; they don't automatically become the authorization principal on a tool call, and LangGraph's checkpointer persists graph *state*, not a governing identity. Tool auth remains a bearer/API key you wire in yourself.
- **LlamaIndex** agents and tools authenticate to backends with keys you provide; there's no first-class per-agent identity object stamped across every action.

So the honest gap isn't "nobody has anything." It's that a role, a name, and a run tag are conventions for *prompting and tracing* — you still hand-wire the actual credential out of `env` and reconstruct "who acted" from log strings. Promptise makes the identity a **first-class primitive**: `AgentIdentity` is an object you pass to `build_agent()`, and the framework — not your glue code — guarantees it rides onto every tool call, LLM turn, and tamper-evident audit entry, and is the thing guards scope access by. The difference is structural, which is exactly why you can stop sharing API keys across agents without writing a plumbing layer. For the credential side of this — why a short-lived IdP token beats a static key an agent presents — see [How does an AI agent authenticate to an API? (not API keys)](how-does-an-ai-agent-authenticate-to-an-api.md).

## Which agent did this? Attribution that holds up after an incident

Here's the question a shared key can't cleanly answer, and a per-agent identity can: *three weeks after a bad refund, which of your twenty agents issued it — and can you prove the record wasn't edited?*

Because each agent carries its own `AgentIdentity`, the answer is mechanical rather than forensic:

1. **The timeline** (`observe=True`) already tagged every turn with `agent_id="billing-bot"` — or, for a verifiable identity, the IdP-assigned `subject`. You filter by agent, not by grepping process logs.
2. **The server-side audit** wrote the *verified* subject/issuer/audience/roles into an HMAC-chained JSONL log. Because the chain is signed, an edited or deleted entry breaks the hash chain — so "which agent did this" is not just recorded, it's **tamper-evident**.
3. **Delegation stays attributed.** When one agent hands work to a peer, the peer's timeline stamps `delegated_by` with the caller's `claims()`, so even delegated work traces back to the originator.

None of that requires you to trust a name a process printed about itself; the subject on each entry was cryptographically verified at the door. That's the difference between "the model did it" and "`billing-bot`, verified by your IdP, at 14:03." The dedicated walkthrough of building a fleet-wide, tamper-evident answer to this is [Which AI agent did this? Attribution for agent fleets](ai-agent-action-attribution.md).

## Frequently asked questions

### How is this different from just naming my agents?

A name a framework uses for routing or tracing (CrewAI's `role`, AutoGen's `name`, a LangGraph `run_name`) is self-asserted and lives only in prompts or trace metadata. An `AgentIdentity` is an object attached at `build_agent()` that the framework stamps onto every tool call, LLM turn, and audit entry — and, once verifiable, it's the subject a server checks before authorizing a call. One is a label; the other is a principal you can scope access by.

### Do I need an identity provider to start?

No. A local identity is just an `AgentIdentity("agent-id", owner=..., labels=...)` — no keys, no infrastructure — and it gives you per-agent attribution on the timeline and audit log immediately. You upgrade to a verifiable, IdP-backed credential (Entra, AWS, GCP, SPIFFE, or OIDC) only when a resource must *verify* the caller rather than trust the id. The same object upgrades in place; you swap the constructor, not your code.

### How do I actually scope one agent to less than another?

Make the identities verifiable, then enforce on the server: `RequireClientId("billing-bot")` limits a tool to a specific agent, and `HasRole("payments-admin")` limits it by role from the verified token. Access is decided from the cryptographically verified subject, so least privilege per agent is enforced at the resource, not assumed from a shared key.

### Does per-agent identity survive when one agent delegates to another?

Yes. Promptise inherits the ambient caller context across `ask_peer`/`broadcast`, and the peer stamps `delegated_by` (the originating agent's `claims()`) onto every entry it records during the delegated call — so a delegated action is attributable to both the agent that ran it and the one that caused it.

## Next steps

Give each agent its own identity in one `build_agent()` call — start with the [Identity quickstart](../../identity/quickstart.md), which takes you from a local, attribution-only identity to a verifiable one in about five minutes. Then read the [Agent Identity overview](../../identity/overview.md) to see how attribution, outbound auth, per-agent scoping, and tamper-evident audit fit together, and follow the [end-to-end identity guide](../../identity/guide.md) to wire one agent through two MCP servers with separate audiences and a full audit trail. New here? Add an `identity=` argument to your very first agent and watch the timeline name the actor.
