---
seo_title: "Agent Identity on Google Cloud with OIDC"
description: "Back an agent's identity with a Google Cloud OIDC token from the metadata server on Compute Engine, GKE, Cloud Run or Cloud Functions, then verify it."
---

# Google Cloud

Back an agent's identity with Google Cloud by reading an OIDC identity
token from the Compute metadata server. Works on Compute Engine, GKE,
Cloud Run, Cloud Functions, and any runtime exposing
`metadata.google.internal`.

## Prerequisites

- A workload on GCP with an attached service account.
- The resource the agent calls must accept the requested `audience`.
- `pip install promptise`. No extra dependency.

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

## Parameters

`AgentIdentity.from_gcp(agent_id=None, *, name=None, owner=None, labels=None, audience="api://promptise-agent", service_account_email="default", request_timeout=5.0)`

| Parameter | Default | Description |
| --- | --- | --- |
| `agent_id` | `None` | Local handle. Omit it to use the credential's `sub` |
| `name`, `owner`, `labels` | `None` | Descriptive metadata |
| `audience` | `"api://promptise-agent"` | Audience requested in the identity token |
| `service_account_email` | `"default"` | The attached service account; `"default"` is the instance's primary one |
| `request_timeout` | `5.0` | Seconds to wait for the metadata server |

The metadata provider is *active*: `get_credential(audience)` requests a token for that audience, and each audience is cached on its own. Transient failures (timeouts, connection errors, `429`, `5xx`) are retried with backoff.

## Provider class

`GcpMetadataProvider(*, audience="api://promptise-agent", service_account_email="default", metadata_endpoint=None, request_timeout=5.0)`, label `gcp-metadata`. `metadata_endpoint` overrides the URL (derived from `service_account_email` when `None`), for tests or a metadata emulator.

```python
from promptise.identity import AgentIdentity, GcpMetadataProvider

identity = AgentIdentity("billing-bot", credential=GcpMetadataProvider(audience="api://billing"))
print(identity.credential_provider)  # gcp-metadata
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

## Related

- [Architecture](../architecture.md): caching, per-resource credentials and auto-detection
- [Security](../security.md): what the identity guarantees, and errors
