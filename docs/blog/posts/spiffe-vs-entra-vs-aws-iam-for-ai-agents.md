---
title: "SPIFFE vs Entra vs AWS IAM: pick an agent identity provider"
description: "A provider bake-off, not a second concept overview — it links up to the workload-identity hub. Your platform already issues workload identities, so the real…"
keywords: "SPIFFE vs Entra vs AWS IAM for AI agents, agent identity provider comparison, SPIFFE SPIRE vs managed identity, AWS IAM role vs Entra Agent ID, GCP service account for agents, auto-detect agent identity provider"
date: 2026-07-16
slug: spiffe-vs-entra-vs-aws-iam-for-ai-agents
categories:
  - Identity
---

# SPIFFE vs Entra vs AWS IAM: pick an agent identity provider

Choosing between SPIFFE vs Entra vs AWS IAM for AI agents is not really a question of which identity provider is *best* in the abstract — it is a question of which one your platform already issues workload identities from, because that is the one you can back an agent with today. If you have already decided your agents deserve their own verifiable identity instead of a shared key (the case the [workload-identity hub](workload-identity-for-ai-agents.md) makes), this is the follow-up decision: pick the provider. This post is a bake-off across the five Promptise supports — SPIFFE/SPIRE, Microsoft Entra, AWS IAM, Google Cloud, and generic OIDC — compared on the three things that actually differ between them: how the token is acquired, how you revoke it, and when each one fits.

<!-- more -->

## The choice is "which directory," not "which identity store"

The [Agent Identity overview](../../identity/overview.md) is blunt about one design decision that shapes this entire comparison: **Promptise keeps no identity store of its own.** It does not mint identities, hold a directory of agents, or become a second place you have to provision and revoke. It *consumes* the identity your existing provider already issues, through a credential provider that acquires, caches, and refreshes a short-lived JWT the agent presents to the resources it calls.

That reframes the bake-off. You are not shopping for an identity system — you already own one. Your agents run somewhere, and that somewhere is already a directory of workload identities: an Azure tenant, an AWS account, a GCP project, a SPIFFE trust domain. The right provider is almost always "the one my workload already lives in," and the interesting engineering question is how each one hands a token to your agent and how you take it away. (If the *why* still feels unsettled, [How does an AI agent authenticate to an API? (not API keys)](how-does-an-ai-agent-authenticate-to-an-api.md) is the pillar that argues the case before you pick a provider.)

## The five providers at a glance

Every one of these produces the same end result — a signed, short-lived JWT the agent presents to an MCP server or API — but they get there differently and you revoke them differently.

| Provider | Where it fits | How the token is acquired | How you revoke |
| --- | --- | --- | --- |
| **SPIFFE / SPIRE** (`from_spiffe`) | Kubernetes and service meshes; multi-cloud, platform-agnostic zero-trust | JWT-SVID from the SPIRE agent's Workload API socket, or a file written by `spiffe-helper` | Delete the registration entry at the SPIRE server; the short SVID TTL closes the window |
| **Microsoft Entra** (`from_entra`) | Azure VMs / Container Apps (managed identity), AKS (workload identity), Entra Agent ID | `id_token` from IMDS, or the projected token at `$AZURE_FEDERATED_TOKEN_FILE` | Disable or delete the managed identity / Agent ID in the directory; Continuous Access Evaluation can cut it faster |
| **AWS IAM** (`from_aws`) | Lambda, EC2, ECS (IAM role), EKS (IRSA / pod identity) | STS `GetWebIdentityToken`, or an EKS-projected web-identity token file | Detach the role, or attach a policy denying sessions issued before a cutoff; STS tokens are short-lived |
| **Google Cloud** (`from_gcp`) | Compute Engine, Cloud Run, GKE | Identity token from the compute metadata server | Disable / delete the service account or remove its IAM bindings; identity tokens are short-lived |
| **Generic OIDC** (`from_oidc`) | GitHub Actions, GitLab CI, or any issuer you already run | A JWT from a file, a callable, or an environment variable | Revoke or rotate the federated credential at the issuer |

Read that "how you revoke" column as a group and the common thread jumps out: revocation always happens *in your directory*, never in Promptise. There is no framework-side credential to also clean up, because the framework never held one. That is the practical payoff of keeping no identity store — the exact opposite of a static API key threaded through your app's environment, where "revoke" means "find every copy."

## Token acquisition and revocation, provider by provider

The `from_*` factories are thin, deliberate wrappers over each platform's native token path. The identifier you pass (`"billing-bot"`) is a local handle for attribution; the *authoritative* identity comes from the IdP, from the credential's `sub` claim (or `oid` for Entra).

**SPIFFE / SPIRE** is the mesh-native choice and the only one that is genuinely cloud-agnostic. A workload registered with your SPIRE server gets a JWT-SVID, fetched either from the Workload API socket (`mode="sdk"`, needs `pyspiffe`) or from a file `spiffe-helper` maintains (`mode="file"`, no extra dependency). It shines when your agents already run inside a service mesh and you want one identity fabric spanning clouds:

```python
from promptise.identity import AgentIdentity

identity = AgentIdentity.from_spiffe(
    "billing-bot",
    mode="sdk",
    socket_path="unix:///run/spire/agent/api.sock",  # or $SPIFFE_ENDPOINT_SOCKET
    audience="api://my-mcp-server",
)
```

Revocation is a first-class SPIRE operation: delete the workload's registration entry and, once the current SVID's short TTL lapses, the agent can no longer authenticate. The [SPIFFE / SPIRE provider guide](../../identity/providers/spiffe.md) covers file-versus-SDK setup and the trust-domain prerequisites.

**Microsoft Entra** is the path if you are on Azure. `mode="auto"` reads the AKS-projected token at `$AZURE_FEDERATED_TOKEN_FILE` when present, otherwise falls back to IMDS for VM / managed-service-identity workloads. Crucially, registering the agent — as a managed identity, a federated app credential, or an Entra **Agent ID** — is a one-time directory operation you do in Azure; Promptise only consumes the token it yields.

```python
identity = AgentIdentity.from_entra(
    "billing-bot",
    client_id="<managed-identity-client-id>",  # user-assigned identity (IMDS)
    resource="api://my-mcp-server",
)
```

Revocation lives in the directory — disable or delete the identity, or lean on Conditional Access / Continuous Access Evaluation to shorten the window below the token lifetime. The [Microsoft Entra provider guide](../../identity/providers/entra.md) walks through IMDS versus projected-token modes and the exact environment markers detection keys off.

**AWS IAM** covers the AWS estate. In `mode="auto"` it uses an EKS-projected web-identity token when one is mounted, otherwise STS. The role attached to your Lambda, EC2 instance, ECS task, or EKS pod *is* the identity:

```python
identity = AgentIdentity.from_aws(
    "billing-bot",
    region="us-east-1",
    audience="api://my-mcp-server",
)
```

Revocation is role-shaped: detach the role from the workload, or apply an IAM policy that denies sessions issued before a cutoff time — and because STS tokens are short-lived, the blast radius of any leaked credential is bounded by its TTL. **Google Cloud** (`from_gcp`) mirrors this for GCE / Cloud Run / GKE using the metadata server's identity token, revoked by disabling the service account or stripping its IAM bindings. And **generic OIDC** (`from_oidc`) is the escape hatch for everything else — GitHub Actions, GitLab CI, or any issuer — accepting a token from a file, a zero-argument callable, or an environment variable re-read on each refresh.

## Don't choose by hand: `AgentIdentity.auto()`

Here is where the bake-off has a twist ending. Most of the time you should not choose at all — a workload usually knows which cloud it runs on, because the runtime sets characteristic environment variables. `AgentIdentity.auto()` reads those markers, picks Entra, AWS, GCP, or SPIFFE (in that deterministic order when several are present), and dispatches to the matching factory with platform defaults. Off-platform — a laptop, a generic CI runner — it raises `PlatformDetectionError` so you can fall back to a plain local identity.

This snippet is runnable as-is. On a laptop it prints the local-identity path; deployed on any supported platform, the same code lights up the verifiable path with no edits:

```python
import asyncio

from promptise import build_agent
from promptise.identity import AgentIdentity, PlatformDetectionError


async def main():
    # One call asks the runtime which cloud it is on and picks the
    # matching provider — Entra / AWS / GCP / SPIFFE — with that
    # platform's defaults. Off-platform it raises PlatformDetectionError.
    try:
        identity = AgentIdentity.auto("billing-bot", owner="payments")
        print("verifiable via", identity.credential_provider)
    except PlatformDetectionError:
        identity = AgentIdentity("billing-bot", owner="payments")
        print("local identity — attribution only, no provider present")

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

The detection is environment-variable based, so it is fast, side-effect free, and safe to call offline — it never probes a metadata server. That is what makes "write once, run on any of the five" a real property and not a slogan: the same `build_agent(..., identity=identity)` call ships from your laptop to AKS to EKS unchanged.

## The real differentiator: the framework plugs into your IdP

Now the honest competitive part, because the differentiator here is subtle and easy to overstate. The five providers above are not Promptise inventions — SPIFFE, Entra, AWS IAM, and GCP are industry standards you can reach from any language. The delta is not *the IdP*. It is that Promptise treats consuming one as a **first-class, structural** part of building an agent, and the mainstream agent-orchestration frameworks do not.

Be precise about what those frameworks actually do. LangChain / LangGraph, CrewAI, AutoGen, and LlamaIndex all let you attach authentication to an outbound call — you can set a bearer token or headers when you configure an HTTP tool or an MCP client. What none of them ship is a workload-identity provider for the *acting agent*: a component that detects the platform, performs the SPIFFE Workload API fetch or the Azure IMDS request or the AWS STS exchange, caches the resulting JWT, refreshes it before expiry, and re-mints it per audience. In those frameworks that acquisition-caching-refresh logic is code **you** write and own — a script wrapped around `boto3` or a metadata `curl`, a cache you invalidate yourself, a refresh timer you get right or leak tokens over.

In Promptise it is one factory call, or zero with `.auto()`. The provider handles caching and pre-expiry refresh, `get_credential(audience)` re-mints a resource-scoped token so one identity can present to several services, and the verified subject flows automatically into observability and the tamper-evident audit log. That is the structural difference: not "we have an IdP they lack" — they can reach the same IdPs — but "we make plugging into it a supported seam instead of glue you maintain." If you want to see the seam from the other end, [Give each AI agent its own identity, not a shared key](give-each-ai-agent-its-own-identity.md) shows the same `AgentIdentity` wired through `build_agent` for a whole fleet.

## Frequently asked questions

### Which is better, SPIFFE, Entra, or AWS IAM?

For AI agents, "better" almost always resolves to "which one your workload already lives in." Back an agent with **Entra** on Azure, **AWS IAM** on AWS, **GCP** on Google Cloud, and **SPIFFE/SPIRE** when you run a service mesh or want one platform-agnostic identity fabric across clouds. Generic **OIDC** covers CI runners and anything else. Since Promptise keeps no identity store of its own, there is no lock-in cost to picking the one nearest your infrastructure — and `AgentIdentity.auto()` will usually pick it for you.

### Do I have to pick the provider explicitly?

No. `AgentIdentity.auto()` detects the platform from environment markers and dispatches to the right factory with that platform's defaults. Detection is deterministic (Entra, then AWS, then GCP, then SPIFFE, first match wins) and purely environment-variable based, so it is safe to call offline. Off-platform it raises `PlatformDetectionError`, which is your cue to construct a local `AgentIdentity("id")` or an explicit `from_*` factory instead.

### How does revocation differ across providers?

It differs in *mechanism* but not in *location* — every one of them is revoked in your directory, never in Promptise. SPIFFE: delete the SPIRE registration entry. Entra: disable the managed identity / Agent ID (or use Continuous Access Evaluation). AWS: detach the role or deny sessions before a cutoff. GCP: disable the service account or remove its bindings. OIDC: revoke the federated credential at the issuer. In all five, short token TTLs bound the window, and there is no framework-side copy to also clean up.

### Is this the same as the LLM's API key?

No. The model keeps its own credential — that is *how the model talks* to its provider. Agent identity is about *who is acting*: which agent made a tool call or hit an API, for attribution and authorization. They are separate credentials with separate lifecycles, as the [Agent Identity overview](../../identity/overview.md) spells out.

## Next steps

Compare setup and revocation for each provider against your own infrastructure, then wire the winner into `build_agent`. Start with the [Agent Identity overview](../../identity/overview.md) for the two-tier model and the full provider table, then open the guide for the platform you run — [Microsoft Entra](../../identity/providers/entra.md) if you are on Azure, [SPIFFE / SPIRE](../../identity/providers/spiffe.md) if you run a mesh. If you are still weighing whether verifiable identity is worth it at all, back up to [How does an AI agent authenticate to an API? (not API keys)](how-does-an-ai-agent-authenticate-to-an-api.md) and the [workload-identity hub](workload-identity-for-ai-agents.md) first.
