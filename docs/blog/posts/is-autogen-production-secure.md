---
title: "Is AutoGen Production-Secure? The Audit Trail Gap"
description: "AutoGen is a capable multi-agent research framework, but 'can it pass a compliance review?' is a different question — there is no tamper-evident audit trail…"
keywords: "is autogen production secure, autogen audit logging, tamper-evident agent audit trail, hmac chained audit ai agent, compliance audit ai agents"
date: 2026-07-16
slug: is-autogen-production-secure
categories:
  - Comparisons
---

# Is AutoGen Production-Secure? The Audit Trail Gap

If you are asking **is AutoGen production secure** enough to survive a compliance review, the honest answer starts with a compliment: Microsoft's AutoGen is a genuinely capable multi-agent research framework, and the 0.4 line ships real infrastructure — a Docker code executor and a distributed gRPC runtime that coordinates agents across processes and machines. That is not a toy. But "capable in production" and "can pass an auditor's questions" are two different bars, and the gap between them is narrow, specific, and unforgiving: when your agents call tools that move money or touch regulated data, an auditor asks *who did it, prove the record wasn't edited, and prove nothing went missing* — and AutoGen has no answer built in. This post credits what AutoGen actually gives you, then shows the primitive it leaves to you: an HMAC-chained, tamper-evident audit trail with verified per-principal and per-tenant attribution.

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## Production-capable is not audit-ready

The confusion is understandable, because AutoGen looks production-shaped from the outside. It has a runtime, a distributed topology, and a container for untrusted code. Those are exactly the boxes you tick when you ask "can it run at scale?"

The trouble is that a compliance reviewer is not asking whether it *runs*. They are asking whether, six months after an incident, you can hand them a record that (a) attributes every consequential tool call to a verified principal, (b) proves that record has not been altered since it was written, and (c) demonstrates that nothing was silently dropped. Those three properties — attribution, integrity, completeness — are the entire content of a SOC 2 monitoring control, a HIPAA §164.312(b) audit control, and an EU AI Act Article 12 logging obligation. A framework can be an excellent execution engine and still supply none of them, because they were never the design goal. AutoGen was built to make agents *reason together*, and it does that well. It was not built to make their actions *auditable*, and it does not pretend otherwise. That is the honest starting point behind the whole [Why Promptise Foundry](../../getting-started/why-promptise.md) argument: production agents need the boring, verifiable governance to be default behavior, not a research afterthought.

## What other frameworks do today

Being precise here matters, because it is easy to caricature a research framework and lose the reader's trust. Let me name AutoGen's actual behavior exactly, then state the exact delta.

- **AutoGen ships real code isolation.** `DockerCommandLineCodeExecutor` (in `autogen-ext`) runs model-written code inside a container rather than your host process. That is a genuine security boundary, and it is more than many stacks give you. It is not an *audit* mechanism, and it is not claimed to be.
- **AutoGen 0.4 ships a distributed runtime.** A gRPC host plus `GrpcWorkerAgentRuntime` workers coordinate agents across processes and machines. This is real distributed infrastructure — so the fair statement is *not* "AutoGen can't scale." It can. What that message-passing runtime does not do is emit a cryptographically chained, per-principal record of tool calls that an outsider can verify.
- **AutoGen produces logs and traces.** The 0.4 runtime emits structured events through Python's `logging` and can be wired to OpenTelemetry — excellent for debugging and replay. The properties to be precise about: that telemetry lands in a *mutable* store, and any identity attribute on an event is written by the emitting process, so the "who" is self-reported, not checked against an identity provider. Superb observability; not tamper-evidence.
- **AutoGen has human-in-the-loop, not capability authorization.** `UserProxyAgent` can pause for human input, which is a real approval affordance. It is a different thing from *capability-based per-tool authorization* — a declarative rule that a given tool may only be called by a principal holding a specific role, scope, or tenant, enforced server-side and recorded when it denies.

None of that is a knock. For prototyping and research it is exactly right. The delta an auditor lives in is narrow and concrete: a mutable telemetry store is not a record whose *edits and deletions are detectable*, a self-asserted attribute is not a *verified per-principal, per-tenant* attribution you can map to a control, and a human-in-the-loop prompt is not a *capability gate* whose denials are themselves logged. In AutoGen those three controls are entirely the developer's responsibility to research, build, and maintain. Promptise's edge is not "nobody else logs anything" — it is that tamper-evidence, verified attribution, and per-tool authorization are *first-class primitives*, structural rather than a metadata convention you have to remember to set correctly on every call site.

## The primitive: an audit trail an auditor can verify

Here is the whole mechanism in one runnable file. It stands up a ledger server, verifies a caller's token server-side, records a money-moving tool call, prints the *verified* principal and tenant the audit captured, confirms the chain, shows a caller *without* the required capability being denied *and that denial being recorded*, then simulates an insider rewriting history — and watches the chain catch it. Every API is real, and it runs in-process with `TestClient`: no network, no LLM key.

```python
# audit_gap.py — what an auditor can actually verify.
import asyncio

from promptise.mcp.server import (
    MCPServer, AuthMiddleware, JWTAuth, AuditMiddleware,
    HasScope, TestClient, RequestContext,
)

SECRET = "rotate-me-in-prod"        # prod: PROMPTISE_AUDIT_SECRET from a vault

server = MCPServer(name="ledger-api")

# 1. Verify the caller's JWT server-side. subject/tenant/scopes are CHECKED,
#    not self-reported. tenant_id is read from the `tenant_id` claim.
#    (Use JwksAuth against your IdP's keys in production for audience checks.)
auth = JWTAuth(secret=SECRET)
server.add_middleware(AuthMiddleware(auth, tenant_claim="tenant_id"))

# 2. One HMAC-chained, tamper-evident trail. Args stay out of the log by default.
audit = AuditMiddleware(log_path="ledger-audit.jsonl", signed=True, hmac_secret=SECRET)
server.add_middleware(audit)


# 3. Capability-based authorization is per tool, not a global flag.
@server.tool(auth=True, guards=[HasScope("ledger:write")])
async def post_transfer(amount: int, to: str, ctx: RequestContext) -> dict:
    """Move money. Only a caller holding the ledger:write scope may call it."""
    return {"amount": amount, "to": to, "posted_by": ctx.client.subject}


async def main() -> None:
    # An IdP-issued token for one agent in tenant "acme", holding the write scope.
    token = auth.create_token({
        "sub": "settlement-agent",
        "iss": "https://login.example.com",
        "aud": "api://ledger",
        "scope": "ledger:write",
        "tenant_id": "acme",
    })
    client = TestClient(server, meta={"authorization": f"Bearer {token}"})

    await client.call_tool("post_transfer", {"amount": 500, "to": "vendor-42"})

    entry = audit.entries[-1]
    print("identity:", entry["identity"])
    # -> {'subject': 'settlement-agent', 'issuer': 'https://login.example.com',
    #     'audience': 'api://ledger', 'tenant_id': 'acme'}
    print("chain valid:", audit.verify_chain())          # True

    # A caller WITHOUT the scope is denied at the guard — and the denial is
    # itself recorded, attributed, and chained.
    weak = auth.create_token({"sub": "intern-agent", "tenant_id": "acme"})
    denied = TestClient(server, meta={"authorization": f"Bearer {weak}"})
    await denied.call_tool("post_transfer", {"amount": 999999, "to": "self"})
    print("denied status:", audit.entries[-1]["status"])          # error
    print("denied subject:", audit.entries[-1]["identity"]["subject"])  # intern-agent

    # An insider edits the trail to blame someone else...
    audit.entries[0]["identity"]["subject"] = "cleanup-bot"
    print("chain valid:", audit.verify_chain())           # False — tamper caught


asyncio.run(main())
```

Two lines of middleware produce the whole primitive. `AuditMiddleware(signed=True)` writes one JSON line per call, each carrying an HMAC-SHA256 computed over its own fields *plus* the previous entry's hash (`prev_hash`) — the same chaining idea behind git commits. Edit any field, delete a line, or reorder two entries and `verify_chain()` returns `False`, with the break localized. The `identity` block is not a string the agent typed about itself; it is what the server extracted *after* validating the token signature, so it names the principal the resource actually authenticated. And because `HasScope("ledger:write")` is a per-tool guard, the intern's over-privileged attempt never reaches the handler — yet the *denial* still lands in the trail as an attributed `error` entry, which is exactly the "attempted-but-blocked" evidence a reviewer looks for. For the full field reference and recommended middleware ordering, see the [Observability & Audit page](../../mcp/server/observability.md).

## What the trail proves that a chat runtime can't

Line the output up against the three questions and the shape of the answer becomes obvious.

- **Attribution — whose action was it?** The `identity` block records the verified `subject`, `issuer`, `audience`, and `tenant_id`. That `tenant_id` is the field that turns a shared multi-tenant deployment into one you can defend: `AuthMiddleware(auth, tenant_claim="tenant_id")` lifts the tenant from a configurable JWT claim onto `ctx.client.tenant_id`, it lands in every audit entry, and it is the same key `RequireTenant` / `HasTenant` guards enforce — so one tenant's agent can be *proven* never to have touched another's tools. (If you have hit this wall on another stack, the honest walkthrough in [Does LangChain Support Multi-Tenancy? The Honest Answer](does-langchain-support-multi-tenancy.md) covers why "just add a `tenant_id` column" isn't the same guarantee.)
- **Integrity — was the record altered?** `verify_chain()` returns a plain boolean you can wire into a periodic alarm. The insider edit in the example flips it to `False` and the break is localized to the tampered entry.
- **Completeness — was anything dropped?** Chain linkage: delete an entry and the *next* entry's `prev_hash` no longer matches, so a silent removal is detectable rather than invisible.

There is a second attribution layer that answers a question a single-process chat loop can't even pose: *which agent asked another agent to do this?* When a Promptise agent delegates to a peer over HTTP+JWT, the delegating agent's identity rides along, and the peer's observability timeline stamps every event it records during that call with `delegated_by`. So a consequential action taken by a downstream specialist traces back to the coordinator that requested it — attribution across a delegation hop, not just within one process. That is complementary to the server-side audit's verified `subject`, and it is the kind of end-to-end wiring the [Secure Multi-Tenant Agent Platform](../../guides/secure-multi-tenant-platform.md) guide assembles from scratch.

## What this isn't: a log primitive, not a compliance program

Overclaiming on compliance is worse than saying nothing, so here is the boundary stated plainly. `AuditMiddleware` is a *log primitive*. It gives you attributable, tamper-evident, automatically-generated records with a mechanical `verify_chain()` proof, and capability guards give you enforceable per-tool authorization whose denials are recorded. It does not, by itself, make you SOC 2, HIPAA, or EU AI Act compliant — those are programs, not a middleware. It does not write your policies, run your risk assessments, or set your retention schedules. It does not make your storage immutable: pair it with write-once (WORM) or append-only storage and keep the HMAC secret out of the log-writer's reach, or an attacker who holds the key can re-sign a forged chain. And a verified `subject` is only as sound as the auth provider behind it — use `JwksAuth` against your IdP's published keys in production, where the `audience` check is load-bearing.

It is equally fair to AutoGen to say that none of this is *impossible* on top of it. You can fork the executor, hand-roll a signing pipeline, and bolt authorization onto every tool. The point is the delta: with AutoGen those are yours to build and keep correct forever; in Promptise they are the default posture. Two more honest notes complete the picture. First, the audit trail is deliberately PII-minimal — `include_args` and `include_result` default to `False`, so tool payloads never enter the log unless you opt in, and the identity block records descriptors only. Second, GDPR erasure therefore targets the *mutable* stores where personal data actually lives — `purge_user()` is a first-class method on memory providers, the semantic cache (with a `tenant_id=` scope), and the observability recorder — leaving the PII-free, tamper-evident access record intact and verifiable. For the full list of what a framework hands you versus what stays your job, the [Enterprise-Ready Agent Framework Checklist: What's Left to You](enterprise-ready-agent-framework-checklist.md) is the companion read.

## Frequently asked questions

### Is AutoGen production secure enough to pass a compliance audit?

AutoGen gives you real infrastructure — a Docker code executor and a 0.4 distributed gRPC runtime — but no tamper-evident audit trail, no verified per-principal or per-tenant attribution on tool calls, and no capability-based per-tool authorization primitive. Those three are precisely what a SOC 2, HIPAA, or EU AI Act reviewer asks for, and in AutoGen they are the developer's job to build. It can *run* in production; passing an audit is additional work you own.

### Doesn't AutoGen already log what agents do?

Yes — AutoGen 0.4 emits structured runtime events through Python `logging` and can export OpenTelemetry traces, which is excellent for debugging and replay. The gap is narrow and specific: that telemetry is a *mutable* store with *self-asserted* identity attributes. It is not a cryptographically chained record whose edits and deletions are detectable, nor a verified attribution checked against an identity provider. Promptise makes tamper-evidence and verified attribution structural fields inside the audit itself.

### What is the difference between human-in-the-loop and capability-based authorization?

AutoGen's `UserProxyAgent` can pause a run for human input — a real approval affordance. Capability-based authorization is different: a declarative, server-side rule (`guards=[HasScope("ledger:write")]`, `HasRole(...)`, `RequireTenant()`) that a tool may only be called by a principal holding a specific scope, role, or tenant — enforced before the handler runs, and recorded as an attributed entry when it denies. You typically want both.

### How do I honor a data-subject erasure request without breaking the audit chain?

Erase from the mutable stores, not the immutable log. Call `purge_user()` on your memory provider, semantic cache (with the `tenant_id=` scope where relevant), and observability recorder. The tamper-evident access trail — PII-minimal by design, since `include_args`/`include_result` default to `False` — is retained intact, which is exactly what retention rules expect.

### Can I migrate an AutoGen workflow to Promptise?

Yes, usually incrementally. Keep your agent logic and instructions, re-express each agent with `build_agent()`, move tools onto MCP servers so they are discovered automatically, and add `AuthMiddleware` + `AuditMiddleware` plus per-tool guards to the servers. Because Promptise agents are services, you can migrate one agent at a time rather than rewriting the whole system.

## Next steps

AutoGen is a strong multi-agent research framework; making it audit-ready is a project you own. In Promptise it is two lines of middleware and a per-tool guard. Add `AuthMiddleware(JWTAuth(...), tenant_claim="tenant_id")` and `AuditMiddleware(signed=True)` to your MCP server, gate consequential tools with `HasScope`/`HasRole`/`RequireTenant`, and run `verify_chain()` on a schedule. Start from the [Observability & Audit reference](../../mcp/server/observability.md) for the full field list, wire tenant isolation and audit into one governed deployment with the [Secure Multi-Tenant Agent Platform](../../guides/secure-multi-tenant-platform.md) guide, and read [Why Promptise Foundry](../../getting-started/why-promptise.md) to see how auth, audit, and tenancy stay default-on as your system grows. See what an auditor can actually verify — then `pip install promptise`.
