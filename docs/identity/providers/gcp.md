# Google Cloud

Federate from Google Cloud. A single provider reads an OIDC identity token from
the Compute metadata server, so it works on Compute Engine, GKE, Cloud Run,
Cloud Functions, and any other GCP runtime that exposes
`metadata.google.internal`.

The metadata identity endpoint returns the JWT as a **plain string** (not a JSON
wrapper), so there is nothing to parse and no SDK to install.

## Prerequisites

- A workload running on GCP with an attached service account.
- `pip install promptise` — no extra dependency.

## Anthropic Console setup

Create a Workload Identity Federation rule whose issuer is Google
(`https://accounts.google.com`) and whose subject matches the attached service
account. Export the resulting identifiers as `ANTHROPIC_FEDERATION_RULE_ID`,
`ANTHROPIC_ORGANIZATION_ID`, and `ANTHROPIC_SERVICE_ACCOUNT_ID`.

## Usage

=== "Default service account"

    ```python
    from promptise.identity import AgentIdentity

    identity = AgentIdentity.from_gcp()
    ```

=== "Specific service account"

    ```python
    identity = AgentIdentity.from_gcp(
        service_account_email="agent@my-project.iam.gserviceaccount.com",
    )
    ```

Wire it into an agent:

```python
from promptise import build_agent

agent = await build_agent(
    model="anthropic:claude-sonnet-4-5",
    servers={},
    identity=AgentIdentity.from_gcp(),
)
```

## Verify

```python
identity = AgentIdentity.from_gcp()
print(identity.provider_name)         # gcp-metadata
assert identity.get_token().startswith("sk-ant-oat01-")
```

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `TokenAcquisitionError: could not reach the Google Compute metadata server` | Not running on GCP compute, or egress to `metadata.google.internal` is blocked. |
| `TokenAcquisitionError: … HTTP 404` | The named `service_account_email` is not attached to this instance, or the audience is not permitted. |
| `TokenAcquisitionError: … empty body` | A transient metadata-server issue or a malformed audience. |
| `TokenExchangeError` | The Console rule's issuer/audience does not match the GCP identity token. |
