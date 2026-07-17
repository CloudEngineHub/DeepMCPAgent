# Google Cloud

Back an agent's identity with Google Cloud by reading an OIDC identity
token from the Compute metadata server. Works on Compute Engine, GKE,
Cloud Run, Cloud Functions, and any runtime exposing
`metadata.google.internal`.

## Prerequisites

- A workload on GCP with an attached service account.
- The resource the agent calls must accept the requested `audience`.
- `pip install promptise` — no extra dependency.

## Usage

```python
from promptise.identity import AgentIdentity

identity = AgentIdentity.from_gcp(
    "billing-bot",
    name="Billing Bot",
    audience="api://my-mcp-server",   # the resource the credential targets
    # service_account_email="agent@project.iam.gserviceaccount.com"  # or "default"
)
```

## Verify

```python
print(identity.credential_provider)        # gcp-metadata
credential = identity.get_credential()      # a signed identity JWT
```

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `CredentialAcquisitionError: could not reach the Google Compute metadata server` | Not on GCP compute, or egress to `metadata.google.internal` blocked. |
| `CredentialAcquisitionError: … HTTP 404` | The service account is not attached, or the audience is not permitted. |
| `CredentialAcquisitionError: … empty body` | A transient metadata-server issue or a malformed audience. |
| Resource rejects the credential | The resource does not trust Google as issuer, or the audience is wrong. |
