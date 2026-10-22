---
seo_title: "Agent Identity with Any OIDC Issuer"
description: "Back an agent's identity with any OIDC issuer, such as GitLab CI, GitHub Actions, Keycloak or Dex, reading the JWT from a file, a callable or an env variable."
---

# Generic OIDC

Back an agent's identity with any OIDC issuer (GitLab CI, CircleCI,
Azure DevOps, Keycloak, Authentik, Dex, a GitHub Actions runner), or use
it for local development. Supply the issuer and **exactly one** token source:

- `token_file`: a path the JWT is written to.
- `token_fn`: a zero-argument callable returning the JWT.
- `token_env_var`: an environment variable holding the JWT (re-read on
  every refresh, so CI rotation is picked up).

## Prerequisites

- An OIDC issuer that mints a JWT for your workload (most CI systems do).
- The resource the agent calls must trust the issuer.
- `pip install promptise`. No extra dependency.

## Usage

```python
from promptise.identity import AgentIdentity

identity = AgentIdentity.from_oidc(
    "release-bot",
    issuer="https://gitlab.com",
    token_env_var="CI_JOB_JWT_V2",
    # or token_file="/var/run/oidc/token"
    # or token_fn=mint_jwt
)
```

## Parameters

`AgentIdentity.from_oidc(agent_id=None, *, issuer, name=None, owner=None, labels=None, token_file=None, token_fn=None, token_env_var=None)`

| Parameter | Default | Description |
| --- | --- | --- |
| `agent_id` | `None` | Local handle. Omit it to use the token's `sub` |
| `issuer` | required | The issuer URL (the `iss` claim of your tokens). Used in the provider label and in error messages |
| `name`, `owner`, `labels` | `None` | Descriptive metadata |
| `token_file` | `None` | File the issuer writes the JWT to; read again on every refresh |
| `token_fn` | `None` | Zero-argument callable returning the JWT; called on every refresh |
| `token_env_var` | `None` | Environment variable holding the JWT; read on every refresh |

Zero or several token sources raise `ProviderConfigError`. All three are *passive*: the token's audience is what the issuer put in it, and a per-call `audience` is ignored. The provider label is `oidc:<issuer>` in every mode.

## Provider classes

| Class | Parameters (keyword-only) | Used for |
| --- | --- | --- |
| `OidcFileProvider` | `issuer`, `token_file` (both required) | `token_file` |
| `OidcCallableProvider` | `issuer`, `token_fn` (both required) | `token_fn`, and `token_env_var` (with a function that reads the variable) |

Prefer `AgentIdentity.from_oidc()`; construct these directly only to wrap them yourself.

```python
import os

from promptise.identity import AgentIdentity, OidcCallableProvider

provider = OidcCallableProvider(issuer="https://token.actions.githubusercontent.com",
                                token_fn=lambda: os.environ.get("MY_OIDC_TOKEN", ""))
identity = AgentIdentity("release-bot", credential=provider)
print(identity.credential_provider)  # oidc:https://token.actions.githubusercontent.com
```

## Verify

```python
print(identity.credential_provider)        # oidc:https://gitlab.com
credential = identity.get_credential()      # the issuer's JWT
```

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `ProviderConfigError: exactly one token source …` | Zero or more than one of `token_file` / `token_fn` / `token_env_var` supplied. |
| `CredentialAcquisitionError: OIDC token environment variable … is not set` | The named env var is missing or empty at refresh time. |
| File mode: `projected token file not found` | The issuer did not write the token, or the path is wrong. |
| Resource rejects the credential | The resource does not trust the issuer, or the audience claim is wrong. |

## Related

- [Architecture](../architecture.md): caching, per-resource credentials and auto-detection
- [Security](../security.md): what the identity guarantees, and errors
