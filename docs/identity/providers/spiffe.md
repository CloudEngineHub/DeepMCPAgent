# SPIFFE / SPIRE

Federate from a SPIFFE / SPIRE deployment using a JWT-SVID. Two modes are
supported and `mode="auto"` (the default) picks between them:

- **File** — reads a JWT-SVID written to disk by `spiffe-helper`. Needs no
  `pyspiffe`.
- **SDK** — connects to the SPIRE agent's Workload API Unix socket via
  `pyspiffe` and fetches a JWT-SVID for the configured audience.

## Prerequisites

- A workload registered with a SPIRE server, receiving SVIDs from a local SPIRE
  agent.
- **SDK mode only:** `pip install promptise[identity-spiffe]` (adds `pyspiffe`).
  File mode (with `spiffe-helper`) needs no extra dependency.

## Anthropic Console setup

Create a Workload Identity Federation rule whose issuer is your SPIRE trust
domain and whose subject matches the workload's SPIFFE ID. Export the resulting
identifiers as `ANTHROPIC_FEDERATION_RULE_ID`, `ANTHROPIC_ORGANIZATION_ID`, and
`ANTHROPIC_SERVICE_ACCOUNT_ID`.

## Usage

=== "Auto"

    ```python
    from promptise.identity import AgentIdentity

    # File mode if token_file is given, else SDK mode.
    identity = AgentIdentity.from_spiffe()
    ```

=== "File (spiffe-helper)"

    ```python
    identity = AgentIdentity.from_spiffe(
        token_file="/run/spiffe/jwt-svid.token",
    )
    ```

=== "SDK (Workload API)"

    ```python
    identity = AgentIdentity.from_spiffe(
        mode="sdk",
        socket_path="unix:///run/spire/agent/api.sock",
        # or rely on $SPIFFE_ENDPOINT_SOCKET
    )
    ```

Wire it into an agent:

```python
from promptise import build_agent

agent = await build_agent(
    model="anthropic:claude-sonnet-4-5",
    servers={},
    identity=AgentIdentity.from_spiffe(),
)
```

## Verify

```python
identity = AgentIdentity.from_spiffe(token_file="/run/spiffe/jwt-svid.token")
print(identity.provider_name)         # spiffe-file or spiffe-sdk
assert identity.get_token().startswith("sk-ant-oat01-")
```

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `ProviderConfigError: … pip install promptise[identity-spiffe]` | SDK mode selected but `pyspiffe` is not installed. Install the extra, or use file mode with `spiffe-helper`. |
| `TokenAcquisitionError: fetching a JWT-SVID … failed` | No SPIRE agent is listening on the socket, or the workload has no registration entry. |
| `TokenAcquisitionError: … serialized token could not be extracted` | The installed `pyspiffe` exposes the token under an unexpected accessor — upgrade or switch to file mode. |
| File mode: file-not-found | `spiffe-helper` is not running or writes to a different path. |
| `TokenExchangeError` | The Console rule's issuer/audience does not match the SVID. |
