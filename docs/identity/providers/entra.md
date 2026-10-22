---
seo_title: "Agent Identity with Microsoft Entra ID"
description: "Back an agent's identity with Microsoft Entra ID through Azure IMDS managed identity or AKS Workload Identity, so the resources it calls can verify it."
---

# Microsoft Entra ID

Back an agent's identity with Microsoft Entra so resources can verify
it. Two modes, with `mode="auto"` (default) choosing between them:

- **IMDS**: for VM / Managed Service Identity workloads. Reads an
  `id_token` from the Azure Instance Metadata Service.
- **Projected token**: for AKS Workload Identity. Reads the token AKS
  projects to `$AZURE_FEDERATED_TOKEN_FILE`.

!!! note "What Promptise does and doesn't do here"
    Registering the agent's identity in Entra (as a managed identity, an
    app with a federated credential, or an Entra **Agent ID**) is an
    Entra-side operation you do once in Azure. Promptise *consumes* that
    identity's token (via IMDS or the projected file); it does not create
    the directory identity.

## Prerequisites

- A workload on Azure with a managed identity (VM/MSI) or AKS Workload
  Identity configured.
- The resource the agent authenticates to (an MCP server / API) must
  trust your Entra tenant and accept the requested `resource` audience.
- `pip install promptise`. No extra dependency: IMDS is a plain HTTP call.

## Usage

```python
from promptise.identity import AgentIdentity

identity = AgentIdentity.from_entra(
    "billing-bot",
    name="Billing Bot",
    resource="api://my-mcp-server",   # the resource the credential targets
    # mode="auto": projected if $AZURE_FEDERATED_TOKEN_FILE is set, else IMDS
    # client_id="..."  for a user-assigned managed identity (IMDS)
)
```

## Parameters

`AgentIdentity.from_entra(agent_id=None, *, name=None, owner=None, labels=None, mode="auto", client_id=None, token_file=None, resource="api://promptise-agent")`

| Parameter | Default | Description |
| --- | --- | --- |
| `agent_id` | `None` | Local handle. Omit it to use the credential's `oid` / `sub` as the identifier |
| `name`, `owner`, `labels` | `None` | Descriptive metadata, as on [`AgentIdentity`](../architecture.md#agentidentity-reference) |
| `mode` | `"auto"` | `"auto"`, `"imds"` or `"projected"`; anything else raises `ProviderConfigError` |
| `client_id` | `None` | Client ID of a user-assigned managed identity (IMDS only). Omit for the system-assigned identity |
| `token_file` | `None` | Projected-token path (projected only). Default `$AZURE_FEDERATED_TOKEN_FILE`, else `/var/run/secrets/azure/tokens/azure-identity-token` |
| `resource` | `"api://promptise-agent"` | The audience requested from IMDS. Set it to the resource the agent calls |

`mode="auto"` picks projected when `AZURE_FEDERATED_TOKEN_FILE` is set and IMDS otherwise. IMDS is an *active* provider: `get_credential(audience)` requests a token for that resource. The projected token is *passive*: its audience is the one AKS stamped, and a per-call audience is ignored.

## Provider classes

`from_entra()` returns one of these; construct one yourself to change what the factory doesn't expose (all keyword-only):

| Class | Parameters (defaults) | Label |
| --- | --- | --- |
| `EntraManagedIdentityProvider` | `client_id=None`, `resource="api://promptise-agent"`, `api_version="2018-02-01"`, `imds_endpoint="http://169.254.169.254/metadata/identity/oauth2/token"`, `request_timeout=5.0` | `entra-imds` |
| `EntraProjectedTokenProvider` | `token_file=None` (as above) | `entra-projected` |

```python
from promptise.identity import AgentIdentity, EntraManagedIdentityProvider

identity = AgentIdentity(
    "billing-bot",
    credential=EntraManagedIdentityProvider(resource="api://billing", request_timeout=2.0),
)
print(identity.credential_provider)  # entra-imds
```

Transient IMDS failures (timeouts, connection errors, `429`, `5xx`) are retried with backoff; a `4xx` is not.

## Verify

```python
print(identity.credential_provider)        # entra-imds or entra-projected
credential = identity.get_credential()      # a signed identity JWT
```

Present `identity.get_credential()` to the resource (for example, as an
MCP server's `bearer_token`; `build_agent(identity=...)` does it for you); the resource validates it and attributes
the calling agent.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `CredentialAcquisitionError: could not reach the Azure Instance Metadata Service` | Not on Azure, or egress to `169.254.169.254` blocked. Use `mode="projected"` on AKS. |
| `CredentialAcquisitionError: IMDS returned HTTP …` | The `client_id` / `resource` does not match a managed identity on this VM. |
| Projected mode: `projected token file not found` | `$AZURE_FEDERATED_TOKEN_FILE` unset: AKS Workload Identity not enabled. |
| Resource rejects the credential | The resource does not trust the Entra tenant, or the `resource` audience is wrong. |

## Related

- [Architecture](../architecture.md): caching, per-resource credentials and auto-detection
- [Security](../security.md): what the identity guarantees, and errors
