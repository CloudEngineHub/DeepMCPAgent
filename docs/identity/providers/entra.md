# Microsoft Entra ID

Federate from Microsoft Entra ID (formerly Azure AD). Two modes are supported
and `mode="auto"` (the default) picks between them:

- **IMDS** — for VM / Managed Service Identity workloads. Reads an `id_token`
  from the Azure Instance Metadata Service.
- **Projected token** — for AKS Workload Identity. Reads the token file Azure
  projects into the pod at `$AZURE_FEDERATED_TOKEN_FILE`.

## Prerequisites

- A workload running on Azure with either a managed identity (VM/MSI) or AKS
  Workload Identity configured.
- `pip install promptise` — no extra dependency; Entra uses plain HTTP and the
  projected file, no Azure SDK.

## Anthropic Console setup

Create a Workload Identity Federation rule whose issuer is your Entra tenant
(`https://login.microsoftonline.com/<tenant-id>/v2.0`) and whose subject /
audience match your managed identity or workload-identity federated credential.
Record the `fdrl_…`, organization UUID, and `svac_…` values and export them as
`ANTHROPIC_FEDERATION_RULE_ID`, `ANTHROPIC_ORGANIZATION_ID`, and
`ANTHROPIC_SERVICE_ACCOUNT_ID`.

## Usage

=== "Auto (recommended)"

    ```python
    from promptise.identity import AgentIdentity

    # Projected mode if $AZURE_FEDERATED_TOKEN_FILE is set, else IMDS.
    identity = AgentIdentity.from_entra()
    ```

=== "IMDS (VM / MSI)"

    ```python
    identity = AgentIdentity.from_entra(
        mode="imds",
        client_id="...",          # or rely on $AZURE_CLIENT_ID
    )
    ```

=== "Projected (AKS)"

    ```python
    identity = AgentIdentity.from_entra(
        mode="projected",
        # token_file defaults to $AZURE_FEDERATED_TOKEN_FILE
    )
    ```

Wire it into an agent:

```python
from promptise import build_agent

agent = await build_agent(
    model="anthropic:claude-sonnet-4-5",
    servers={},
    identity=AgentIdentity.from_entra(),
)
```

## Verify

```python
identity = AgentIdentity.from_entra()
print(identity.provider_name)         # entra-imds or entra-projected
assert identity.get_token().startswith("sk-ant-oat01-")
```

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `TokenAcquisitionError: could not reach the Azure Instance Metadata Service` | Not running on Azure, or egress to `169.254.169.254` is blocked. Use `mode="projected"` on AKS. |
| `TokenAcquisitionError: IMDS returned HTTP …` | The `client_id` / `resource` does not match a managed identity on this VM. |
| Projected mode: file-not-found | `$AZURE_FEDERATED_TOKEN_FILE` is unset — AKS Workload Identity is not enabled for the pod. |
| `TokenExchangeError` | The Console federation rule's issuer/audience does not match the Entra token. |
