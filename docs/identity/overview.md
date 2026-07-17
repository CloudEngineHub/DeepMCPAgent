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

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## Why this matters

Agents are a new class of **non-human actor** — they call tools, hit APIs, and
act continuously, often with no human in the loop. Most teams run them with **no
real identity**: a shared API key, or a name a process asserts about itself in a
log. That breaks down fast:

- **Attribution** — across a fleet, *"which agent did this?"* has no reliable
  answer. A shared key, or a string a process printed about itself, can't be
  trusted after the fact.
- **Least privilege** — without a verifiable identity, every agent holding the
  shared key has the **same** access. You can't scope one agent to billing and
  another to read-only.
- **Audit & compliance** — SOC 2 / ISO / internal review ask *who* performed
  each action. "The model" is not an answer; "`billing-bot`, verified by your
  IdP" is.
- **Blast radius** — an over-privileged or unattributable agent is exactly the
  risk reviewers worry about. The 2025–2026 wave of agent deployments has run
  ahead of the identity controls that would govern them.

Agent Identity closes the gap with the model enterprises already trust for
service accounts: a **stable identity** for attribution, plus an optional
**verifiable credential** minted by your existing IdP — so resources can
authenticate and authorize the agent with **no new secrets to manage**.

**Who needs it, and when:**

| If you… | Use |
|---|---|
| Run more than one agent, or one across a fleet | a **local identity** for attribution — start today, zero infrastructure |
| Have agents call protected MCP servers / internal APIs | a **verifiable identity** backed by your IdP |
| Have audit/compliance requirements (who did what) | a **verifiable identity** (the verified subject is recorded in the tamper-evident audit log) |
| Run multi-tenant or accept untrusted input | a **verifiable identity** + least-privilege scoping — the control that bounds the blast radius |

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

## Which provider

The fastest answer: call [`AgentIdentity.auto()`](architecture.md#auto-detection)
and let it detect the platform. If you'd rather be explicit, pick by **where your
workload runs**:

| Where the agent runs | Use | Mode |
| --- | --- | --- |
| Azure VM / VMSS / Container Apps (managed identity) | `from_entra()` | IMDS |
| Azure Kubernetes Service (AKS workload identity) | `from_entra()` | projected token file |
| AWS Lambda / EC2 / ECS (IAM role) | `from_aws()` | STS `GetWebIdentityToken` |
| AWS EKS (IRSA / pod identity) | `from_aws()` | EKS projected token (no boto3) |
| Google Compute Engine / Cloud Run / GKE | `from_gcp()` | metadata server |
| Any SPIFFE/SPIRE mesh | `from_spiffe()` | Workload API socket, or `spiffe-helper` file |
| GitHub Actions, GitLab CI, or any OIDC issuer | `from_oidc()` | env var / file / callable |
| Local laptop, tests, or "just attribution" | `AgentIdentity("id")` | **local** — no provider |

Optional cloud SDKs are needed only for some modes (boto3 for AWS STS, `pyspiffe`
for the SPIFFE SDK); the metadata/IMDS/projected-token and OIDC paths need none.
See the per-provider pages linked in the table above for setup and prerequisites.

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
