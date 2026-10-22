---
seo_title: "Agent Identity on AWS IAM: STS and EKS"
description: "Back an agent's identity with AWS IAM, using STS GetWebIdentityToken on Lambda, EC2, ECS and EKS or an EKS-projected token file, and verify it."
---

# AWS IAM

Back an agent's identity with AWS IAM. Two modes, with `mode="auto"`
(default) choosing between them:

- **STS**: for Lambda, EC2, ECS, EKS. Calls STS `GetWebIdentityToken`
  via `boto3`.
- **EKS-projected**: reads a projected token file (default
  `/var/run/secrets/promptise/token`, override with
  `$PROMPTISE_IDENTITY_TOKEN_FILE`). Needs no `boto3`.

## Prerequisites

- A workload on AWS with an attached IAM role.
- **STS mode only:** `pip install promptise[identity-aws]` (adds
  `boto3`) and a region (`region=`, `$AWS_REGION`, or
  `$AWS_DEFAULT_REGION`).
- The resource the agent calls must accept the requested `audience`.

!!! note "Region is validated eagerly (STS mode)"
    Because STS is regional, `from_aws()` in STS mode resolves and validates the
    region **at construction** and raises `ProviderConfigError` if none is found,
    unlike the other providers, which construct lazily and only reach out at
    `get_credential()`. Pass `region=` or set `$AWS_REGION` where you build the
    identity. (The EKS-projected mode needs no region and constructs lazily.)
    `boto3` itself is imported only when a credential is requested.

## Usage

```python
from promptise.identity import AgentIdentity

identity = AgentIdentity.from_aws(
    "billing-bot",
    name="Billing Bot",
    region="us-east-1",               # STS mode
    audience="api://my-mcp-server",   # the resource the credential targets
    # mode="auto": EKS-projected if $PROMPTISE_IDENTITY_TOKEN_FILE is set, else STS
)
```

## Parameters

`AgentIdentity.from_aws(agent_id=None, *, name=None, owner=None, labels=None, mode="auto", region=None, token_file=None, audience="api://promptise-agent", signing_algorithm="RS256")`

| Parameter | Default | Description |
| --- | --- | --- |
| `agent_id` | `None` | Local handle. Omit it to use the credential's `sub` |
| `name`, `owner`, `labels` | `None` | Descriptive metadata |
| `mode` | `"auto"` | `"auto"`, `"sts"` or `"projected"`; anything else raises `ProviderConfigError` |
| `region` | `None` | STS region. Falls back to `$AWS_REGION`, then `$AWS_DEFAULT_REGION` |
| `token_file` | `None` | Projected-token path. Default `$PROMPTISE_IDENTITY_TOKEN_FILE`, else `/var/run/secrets/promptise/token` |
| `audience` | `"api://promptise-agent"` | Audience of the STS web-identity token (STS only) |
| `signing_algorithm` | `"RS256"` | JWT signing algorithm STS uses (STS only) |

`mode="auto"` picks the projected mode when `PROMPTISE_IDENTITY_TOKEN_FILE` is set, otherwise STS. On EKS with IRSA, STS runs with the pod's role; IRSA's own `$AWS_WEB_IDENTITY_TOKEN_FILE` (audience `sts.amazonaws.com`) is never presented. STS is *active* (one credential per requested audience); the projected token is *passive* (fixed audience).

## Provider classes

| Class | Parameters (defaults, keyword-only) | Label |
| --- | --- | --- |
| `AwsStsProvider` | `region=None`, `audience="api://promptise-agent"`, `signing_algorithm="RS256"`. Raises `ProviderConfigError` at construction without a region | `aws-sts` |
| `AwsEksProjectedProvider` | `token_file=None` (as above) | `aws-eks-projected` |

```python
from promptise.identity import AgentIdentity, AwsStsProvider

identity = AgentIdentity("billing-bot", credential=AwsStsProvider(region="eu-central-1", audience="api://billing"))
print(identity.credential_provider)  # aws-sts
```

## Verify

```python
print(identity.credential_provider)        # aws-sts or aws-eks-projected
credential = identity.get_credential()      # a signed identity JWT
```

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `ProviderConfigError: … pip install promptise[identity-aws]` | STS mode but `boto3` not installed. Install it, or use `mode="projected"`. |
| `ProviderConfigError: AWS STS is regional …` | No region. Pass `region=` or set `$AWS_REGION`. |
| `CredentialAcquisitionError: STS GetWebIdentityToken failed` | No assumable role from this context, or the audience is not permitted. |
| EKS-projected: `projected token file not found` | Projected volume not mounted, or `$PROMPTISE_IDENTITY_TOKEN_FILE` wrong. |

## Related

- [Architecture](../architecture.md): caching, per-resource credentials and auto-detection
- [Security](../security.md): what the identity guarantees, and errors
