---
seo_title: "Agent Identity with SPIFFE and SPIRE"
description: "Back an agent's identity with a SPIFFE JWT-SVID, read from a file written by spiffe-helper or fetched from the SPIRE agent's Workload API with py-spiffe."
---

# SPIFFE / SPIRE

Back an agent's identity with a SPIFFE / SPIRE JWT-SVID. Two modes, with
`mode="auto"` (default) choosing between them:

- **File**: reads a JWT-SVID written by `spiffe-helper`. Needs no
  SPIFFE library.
- **SDK**: fetches a JWT-SVID from the SPIRE agent's Workload API
  socket with [py-spiffe](https://github.com/HewlettPackard/py-spiffe)
  (the `spiffe` package on PyPI).

`AgentIdentity.auto()` picks SDK mode when `SPIFFE_ENDPOINT_SOCKET` is set,
so it needs py-spiffe installed too.

## Prerequisites

- A workload registered with a SPIRE server, served by a local SPIRE
  agent.
- **SDK mode only:** `pip install "promptise[identity-spiffe]"`, which
  installs `spiffe` (py-spiffe 0.2 or later) and its gRPC dependencies.
  Promptise 1.5.0 and earlier named a `pyspiffe` package that does not
  exist on PyPI, so this extra could not be installed; upgrade to 1.5.1 or later.
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

## Parameters

`AgentIdentity.from_spiffe(agent_id=None, *, name=None, owner=None, labels=None, mode="auto", token_file=None, socket_path=None, audience="api://promptise-agent")`

| Parameter | Default | Description |
| --- | --- | --- |
| `agent_id` | `None` | Local handle. Omit it to use the SVID's `sub` (the SPIFFE ID) |
| `name`, `owner`, `labels` | `None` | Descriptive metadata |
| `mode` | `"auto"` | `"auto"`, `"file"` or `"sdk"`; anything else raises `ProviderConfigError` |
| `token_file` | `None` | JWT-SVID file (file mode). Required for `mode="file"` |
| `socket_path` | `None` | Workload API address (SDK mode). Falls back to `$SPIFFE_ENDPOINT_SOCKET`, then `unix:///tmp/spire-agent/public/api.sock` |
| `audience` | `"api://promptise-agent"` | Audience requested in the JWT-SVID (SDK mode) |

`mode="auto"` picks file mode when `token_file` is given and SDK mode otherwise. The SDK provider is *active* (one SVID per requested audience, transient Workload API failures retried); the file provider is *passive* (the audience `spiffe-helper` requested) and re-reads the file on every refresh.

## Provider classes

| Class | Parameters (defaults, keyword-only) | Label |
| --- | --- | --- |
| `SpiffeFileProvider` | `token_file` (required) | `spiffe-file` |
| `SpiffeSdkProvider` | `socket_path=None` (as above), `audience="api://promptise-agent"` | `spiffe-sdk` |

`py-spiffe` is imported only when the SDK provider fetches its first SVID, so constructing it works without the package.

```python
from promptise.identity import AgentIdentity, SpiffeFileProvider

identity = AgentIdentity("billing-bot", credential=SpiffeFileProvider(token_file="/run/spiffe/jwt-svid.token"))
print(identity.credential_provider)  # spiffe-file
```

## Verify

```python
print(identity.credential_provider)        # spiffe-file or spiffe-sdk
credential = identity.get_credential()      # a signed JWT-SVID
```

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `ProviderConfigError: … pip install promptise[identity-spiffe]` | SDK mode but py-spiffe (`spiffe`) is not installed. Install it, or use file mode. |
| `CredentialAcquisitionError: fetching a JWT-SVID … failed` | No SPIRE agent on the socket, the socket address is not a `unix://` path, or no registration entry. |
| `CredentialAcquisitionError: … could not be extracted` | The Workload API answered without a usable token (py-spiffe exposes it as `JwtSvid.token`). |
| File mode: `projected token file not found` | `spiffe-helper` not running or writing elsewhere. |

## Related

- [Architecture](../architecture.md): caching, per-resource credentials and auto-detection
- [Security](../security.md): what the identity guarantees, and errors
