# AWS IAM

Back an agent's identity with AWS IAM. Two modes, with `mode="auto"`
(default) choosing between them:

- **STS** — for Lambda, EC2, ECS, EKS. Calls STS `GetWebIdentityToken`
  via `boto3`.
- **EKS-projected** — reads a projected token file (default
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
    region **at construction** and raises `ProviderConfigError` if none is found
    — unlike the other providers, which construct lazily and only reach out at
    `get_credential()`. Pass `region=` or set `$AWS_REGION` where you build the
    identity. (The EKS-projected mode needs no region and constructs lazily.)

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
| EKS-projected: file-not-found | Projected volume not mounted, or `$PROMPTISE_IDENTITY_TOKEN_FILE` wrong. |
