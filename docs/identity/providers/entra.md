# Microsoft Entra ID

Back an agent's identity with Microsoft Entra so resources can verify
it. Two modes, with `mode="auto"` (default) choosing between them:

- **IMDS** — for VM / Managed Service Identity workloads. Reads an
  `id_token` from the Azure Instance Metadata Service.
- **Projected token** — for AKS Workload Identity. Reads the token AKS
  projects to `$AZURE_FEDERATED_TOKEN_FILE`.

!!! note "What Promptise does and doesn't do here"
    Registering the agent's identity in Entra — as a managed identity, an
    app with a federated credential, or an Entra **Agent ID** — is an
    Entra-side operation you do once in Azure. Promptise *consumes* that
    identity's token (via IMDS or the projected file); it does not create
    the directory identity.

## Prerequisites

- A workload on Azure with a managed identity (VM/MSI) or AKS Workload
  Identity configured.
- The resource the agent authenticates to (an MCP server / API) must
  trust your Entra tenant and accept the requested `resource` audience.
- `pip install promptise` — no extra dependency.

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

## Verify

```python
print(identity.credential_provider)        # entra-imds or entra-projected
credential = identity.get_credential()      # a signed identity JWT
```

Present `identity.get_credential()` to the resource (for example, as an
MCP server's `bearer_token`); the resource validates it and attributes
the calling agent.

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `CredentialAcquisitionError: could not reach the Azure Instance Metadata Service` | Not on Azure, or egress to `169.254.169.254` blocked. Use `mode="projected"` on AKS. |
| `CredentialAcquisitionError: IMDS returned HTTP …` | The `client_id` / `resource` does not match a managed identity on this VM. |
| Projected mode: file-not-found | `$AZURE_FEDERATED_TOKEN_FILE` unset — AKS Workload Identity not enabled. |
| Resource rejects the credential | The resource does not trust the Entra tenant, or the `resource` audience is wrong. |
