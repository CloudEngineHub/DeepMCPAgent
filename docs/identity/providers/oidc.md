# Generic OIDC

Back an agent's identity with any standards-compliant OIDC issuer —
GitLab CI, CircleCI, Azure DevOps, Keycloak, Authentik, Dex, a GitHub
Actions runner — or use it for local development. Supply the issuer and
**exactly one** token source:

- `token_file` — a path the JWT is written to.
- `token_fn` — a zero-argument callable returning the JWT.
- `token_env_var` — an environment variable holding the JWT (re-read on
  every refresh, so CI rotation is picked up).

## Prerequisites

- An OIDC issuer that mints a JWT for your workload (most CI systems do).
- The resource the agent calls must trust the issuer.
- `pip install promptise` — no extra dependency.

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

## Verify

```python
print(identity.credential_provider)        # oidc:https://gitlab.com
credential = identity.get_credential()      # the issuer's JWT
```

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `ProviderConfigError: exactly one token source …` | Zero or more than one of `token_file` / `token_fn` / `token_env_var` supplied. |
| `CredentialAcquisitionError: … environment variable … is not set` | The named env var is missing at refresh time. |
| File mode: file-not-found | The issuer did not write the token, or the path is wrong. |
| Resource rejects the credential | The resource does not trust the issuer, or the audience claim is wrong. |
