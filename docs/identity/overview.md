# Agent Identity

**Know, trace, and identify the agent that acts.**

Agent Identity gives every agent a stable, traceable identity — a
non-human, service-account-style identity (modelled on things like
Microsoft Entra Agent ID) that answers one question: *which agent did
this?* That identity is stamped onto everything the agent does — tool
calls, LLM turns, audit entries — and, when you want it, presented to
the resources the agent calls (such as MCP servers) so they can
authenticate and attribute the caller too.

```python
from promptise import build_agent
from promptise.identity import AgentIdentity

agent = await build_agent(
    model="anthropic:claude-sonnet-4-5",
    servers={...},
    identity=AgentIdentity("billing-bot", name="Billing Bot", owner="payments"),
)
# Every action this agent records is now attributed to "billing-bot".
```

This is **not** about the LLM credential. The model keeps its own
authentication; identity is about *who is acting*, for attribution and
authorization.

## Two tiers of identity

### Local identity

Just an `agent_id` (plus optional name, owner, and labels). No
infrastructure. It is the value the framework stamps onto the
observability timeline and audit log so you can trace which agent did
what across a fleet.

```python
identity = AgentIdentity("billing-bot", name="Billing Bot", owner="payments")
identity.agent_id        # "billing-bot"
identity.claims()        # {"agent_id": "billing-bot", "name": ..., "owner": ..., ...}
```

### Verifiable identity

Additionally backed by a **credential provider** — Microsoft Entra, AWS
IAM, Google Cloud, SPIFFE/SPIRE, or a generic OIDC issuer — that mints a
short-lived, signed JWT proving the identity. The agent presents this
credential to the resources it calls so they can *verify* the caller
rather than trust a self-asserted id.

```python
identity = AgentIdentity.from_entra(
    "billing-bot", client_id="...", resource="api://my-mcp-server"
)
identity.is_verifiable       # True
identity.get_credential()    # a signed JWT, presented to the resource
identity.auth_header()       # {"Authorization": "Bearer <jwt>"}

# One identity, several resources — a credential per audience:
identity.get_credential("api://billing")   # scoped to the billing resource
identity.get_credential("api://crm")       # scoped to the CRM resource
```

A single verifiable identity can present a resource-scoped credential to
each service it calls — see
[Per-resource credentials](architecture.md#per-resource-credentials).

## Where the identity shows up

- **Observability** — every tool call and LLM turn the agent records is
  tagged with the agent's identifier (its `agent_id`, or the IdP
  `subject`), so the timeline tells you which agent acted.
- **MCP & APIs** — a verifiable identity is presented to MCP servers
  **automatically** (its credential becomes their `bearer_token`); the
  server verifies it with [`JwksAuth`](../mcp/server/auth-security.md#jwksauth)
  and authorizes the agent via its JWT auth and `RequireClientId` / role
  guards.
- **Audit** — the Guardrails audit log records the verified agent
  identity (subject/issuer/audience/roles) inside its tamper-evident HMAC
  chain.
- **Cross-agent** — when one agent delegates to another, the peer's
  observability records *who* delegated (the caller's `claims()`).

The `agent_id` / subject drives attribution; the richer `claims()`
(name, owner, labels) flow to the audit log and cross-agent delegation.

## Credential providers

| Provider | Factory | Identity source |
| --- | --- | --- |
| [Microsoft Entra ID](providers/entra.md) | `AgentIdentity.from_entra()` | IMDS, or `$AZURE_FEDERATED_TOKEN_FILE` (AKS) |
| [AWS IAM](providers/aws.md) | `AgentIdentity.from_aws()` | STS `GetWebIdentityToken`, or an EKS-projected token |
| [Google Cloud](providers/gcp.md) | `AgentIdentity.from_gcp()` | Compute metadata identity token |
| [SPIFFE / SPIRE](providers/spiffe.md) | `AgentIdentity.from_spiffe()` | Workload API socket, or a `spiffe-helper` file |
| [Generic OIDC](providers/oidc.md) | `AgentIdentity.from_oidc()` | File, callable, or environment variable |

Don't know which platform you're on? [`AgentIdentity.auto()`](architecture.md#auto-detection)
detects it from environment markers and picks for you.

## Persistence lives in your IdP

An agent's identity is **durable because it lives in an identity
provider** — a Microsoft Entra **Agent ID**, an app / service principal in
your OIDC provider, an AWS IAM role, a SPIFFE registration. That directory
is the system of record: it persists the identity, and it is where you
create, inventory, govern, and revoke agents. Promptise keeps **no
identity store of its own** — it authenticates against the IdP through a
credential provider and uses the identity the IdP issues.

The authoritative identifier comes *from* the IdP, not from a string you
pass. For a verifiable identity you can omit `agent_id` entirely; the
identity is then read from the credential's `sub` claim (or `oid` for
Entra):

```python
identity = AgentIdentity.from_entra(resource="api://my-mcp-server")
identity.agent_id            # None — no local handle
identity.subject()           # "…" — the IdP-assigned identity (sub / oid)
identity.idp_claims()        # {"sub": …, "oid": …, "iss": …, "aud": …}
identity.resolve_identifier()  # the authoritative id used for attribution
```

`"billing-bot"` is the same identity across restarts because the IdP —
not Promptise — makes it so.

## Next steps

- [Quickstart](quickstart.md) — a traceable agent in five minutes.
- [Architecture](architecture.md) — how identity is stamped and presented.
- [Security](security.md) — the threat model and guarantees.
