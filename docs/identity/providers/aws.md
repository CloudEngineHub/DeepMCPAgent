# AWS IAM

Federate from AWS IAM. Two modes are supported and `mode="auto"` (the default)
picks between them:

- **STS** — for Lambda, EC2, ECS, and EKS. Calls STS
  `GetWebIdentityToken` via `boto3`.
- **EKS-projected** — for EKS pods using a projected service-account token. Reads
  the token file (default `/var/run/secrets/anthropic.com/token`, override with
  `$ANTHROPIC_IDENTITY_TOKEN_FILE`). Needs no `boto3`.

## Prerequisites

- A workload running on AWS with an attached IAM role (Lambda execution role,
  EC2 instance profile, ECS task role, or EKS IRSA / Pod Identity).
- **STS mode only:** `pip install promptise[identity-aws]` (adds `boto3`) and a
  region (from the `region=` argument, `$AWS_REGION`, or `$AWS_DEFAULT_REGION`).
- EKS-projected mode needs no extra dependency.

## Anthropic Console setup

Create a Workload Identity Federation rule whose issuer is AWS STS
(`https://sts.amazonaws.com` for the web-identity token, or your EKS OIDC
provider URL for projected tokens) and whose subject matches the IAM role / service
account. Export the resulting identifiers as `ANTHROPIC_FEDERATION_RULE_ID`,
`ANTHROPIC_ORGANIZATION_ID`, and `ANTHROPIC_SERVICE_ACCOUNT_ID`.

## Usage

=== "Auto (recommended)"

    ```python
    from promptise.identity import AgentIdentity

    # EKS-projected if $ANTHROPIC_IDENTITY_TOKEN_FILE is set, else STS.
    identity = AgentIdentity.from_aws()
    ```

=== "STS (Lambda / EC2 / ECS)"

    ```python
    identity = AgentIdentity.from_aws(
        mode="sts",
        region="us-east-1",       # or rely on $AWS_REGION
    )
    ```

=== "EKS-projected"

    ```python
    identity = AgentIdentity.from_aws(
        mode="projected",
        # token_file defaults to $ANTHROPIC_IDENTITY_TOKEN_FILE
        # or /var/run/secrets/anthropic.com/token
    )
    ```

Wire it into an agent:

```python
from promptise import build_agent

agent = await build_agent(
    model="anthropic:claude-sonnet-4-5",
    servers={},
    identity=AgentIdentity.from_aws(),
)
```

## Verify

```python
identity = AgentIdentity.from_aws(mode="sts", region="us-east-1")
print(identity.provider_name)         # aws-sts or aws-eks-projected
assert identity.get_token().startswith("sk-ant-oat01-")
```

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `ProviderConfigError: … pip install promptise[identity-aws]` | STS mode selected but `boto3` is not installed. Install the extra, or use `mode="projected"`. |
| `ProviderConfigError: AWS STS is regional …` | No region supplied. Pass `region=` or set `$AWS_REGION`. |
| `TokenAcquisitionError: STS GetWebIdentityToken failed` | The role is not assumable from this context, or the audience is not permitted. |
| EKS-projected: file-not-found | The projected token volume is not mounted, or `$ANTHROPIC_IDENTITY_TOKEN_FILE` points at the wrong path. |
| `TokenExchangeError` | The Console rule's issuer/audience does not match the AWS token. |
