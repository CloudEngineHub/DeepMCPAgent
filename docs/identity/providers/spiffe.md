# SPIFFE / SPIRE

Back an agent's identity with a SPIFFE / SPIRE JWT-SVID. Two modes, with
`mode="auto"` (default) choosing between them:

- **File** — reads a JWT-SVID written by `spiffe-helper`. Needs no
  `pyspiffe`.
- **SDK** — fetches a JWT-SVID from the SPIRE agent's Workload API
  socket via `pyspiffe`.

## Prerequisites

- A workload registered with a SPIRE server, served by a local SPIRE
  agent.
- **SDK mode only:** `pip install promptise[identity-spiffe]`.
- The resource the agent calls must trust your SPIRE trust domain and
  accept the requested `audience`.

## Usage

```python
from promptise.identity import AgentIdentity

# File mode (spiffe-helper):
identity = AgentIdentity.from_spiffe(
    "billing-bot", token_file="/run/spiffe/jwt-svid.token"
)

# SDK mode (Workload API):
identity = AgentIdentity.from_spiffe(
    "billing-bot",
    mode="sdk",
    socket_path="unix:///run/spire/agent/api.sock",  # or $SPIFFE_ENDPOINT_SOCKET
    audience="api://my-mcp-server",
)
```

## Verify

```python
print(identity.credential_provider)        # spiffe-file or spiffe-sdk
credential = identity.get_credential()      # a signed JWT-SVID
```

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `ProviderConfigError: … pip install promptise[identity-spiffe]` | SDK mode but `pyspiffe` not installed. Install it, or use file mode. |
| `CredentialAcquisitionError: fetching a JWT-SVID … failed` | No SPIRE agent on the socket, or no registration entry. |
| `CredentialAcquisitionError: … could not be extracted` | The installed `pyspiffe` exposes the token under an unexpected accessor. |
| File mode: file-not-found | `spiffe-helper` not running or writing elsewhere. |
