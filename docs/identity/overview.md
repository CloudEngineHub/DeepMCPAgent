# Agent Identity

**Federated authentication for AI agents — zero static credentials in agent code.**

Promptise Agent Identity lets a workload prove who it is to Anthropic using the
identity its platform *already* gives it — an AWS IAM role, a GCP service
account, a Microsoft Entra managed identity, a SPIFFE SVID, or any OIDC issuer —
instead of a long-lived `ANTHROPIC_API_KEY` baked into the agent.

```python
from promptise import build_agent
from promptise.identity import AgentIdentity

agent = await build_agent(
    model="anthropic:claude-sonnet-4-5",
    servers={},
    identity=AgentIdentity.from_aws(),   # zero arguments, reads the environment
)
```

Those lines work unchanged on AWS Lambda, EKS, GKE, AKS, a Kubernetes pod with
SPIRE, and a GitHub Actions runner. No API key is stored, printed, or committed.

## How it works

Every provider produces a short-lived **OIDC JWT** from the platform it runs on.
The framework exchanges that JWT for a short-lived Anthropic access token using
the [RFC 7523](https://datatracker.ietf.org/doc/html/rfc7523)
`urn:ietf:params:oauth:grant-type:jwt-bearer` grant, caches the token in memory,
and refreshes it transparently before it expires. The static-credential problem
disappears: the only thing on disk is a token the platform rotates for you.

See [Architecture](architecture.md) for the full request path and the
[two-tier refresh](architecture.md#token-lifecycle) model.

## When to use it

- **Cloud-hosted agents.** The workload runs on a platform that issues an
  identity (AWS/GCP/Azure/Kubernetes). You want to stop managing API keys.
- **CI/CD agents.** GitLab CI, GitHub Actions, CircleCI, Azure DevOps — any
  runner that mints an OIDC token per job.
- **Zero-trust / SPIFFE environments.** A SPIRE agent already issues SVIDs to
  your workloads.
- **Audit requirements.** You need every action traceable to a federated
  service account rather than a shared key.

## When *not* to use it

- **Local development on a laptop** with no platform identity — a plain
  `ANTHROPIC_API_KEY` is simpler. (You can still use the
  [OIDC env-var path](providers/oidc.md) with a token you mint by hand.)
- **Human SSO.** This is workload-to-Claude authentication, not a workforce
  identity solution.
- **A credential store.** The framework holds tokens in memory only;
  persistence is the platform's job.

## Supported providers

| Provider | Factory | JWT source |
| --- | --- | --- |
| [Microsoft Entra ID](providers/entra.md) | `AgentIdentity.from_entra()` | IMDS, or `$AZURE_FEDERATED_TOKEN_FILE` (AKS Workload Identity) |
| [AWS IAM](providers/aws.md) | `AgentIdentity.from_aws()` | STS `GetWebIdentityToken`, or an EKS-projected token file |
| [Google Cloud](providers/gcp.md) | `AgentIdentity.from_gcp()` | Compute metadata server identity token |
| [SPIFFE / SPIRE](providers/spiffe.md) | `AgentIdentity.from_spiffe()` | Workload API socket, or a `spiffe-helper` file |
| [Generic OIDC](providers/oidc.md) | `AgentIdentity.from_oidc()` | File, callable, or environment variable |

Don't know which one you're on? [`AgentIdentity.auto()`](architecture.md#auto-detection)
detects the platform from environment markers and picks for you.

## Next steps

- [Quickstart](quickstart.md) — a working federated agent in five minutes, with
  no cloud account required.
- [Architecture](architecture.md) — the exchange engine, the cache, and the
  refresh model.
- [Security](security.md) — the threat model and the framework's
  secret-handling guarantees.
- [Migration](migration.md) — move off `ANTHROPIC_API_KEY` with no downtime.
