# Generic OIDC

Federate from any standards-compliant OIDC issuer — GitLab CI, CircleCI, Azure
DevOps, Keycloak, Authentik, Dex, or a GitHub Actions runner. This is also the
provider to use for **local development and Docker**, because the JWT can come
from an environment variable with no cloud account.

Supply the issuer URL and **exactly one** token source:

- `token_file` — a path the JWT is written to.
- `token_fn` — a zero-argument callable that returns the JWT.
- `token_env_var` — the name of an environment variable holding the JWT
  (re-read on every refresh, so CI rotation is picked up).

## Prerequisites

- An OIDC issuer that mints a JWT for your workload (most CI systems do this per
  job).
- `pip install promptise` — no extra dependency.

## Anthropic Console setup

Create a Workload Identity Federation rule whose issuer is your OIDC provider's
URL and whose subject / audience match the token your issuer mints. Export the
resulting identifiers as `ANTHROPIC_FEDERATION_RULE_ID`,
`ANTHROPIC_ORGANIZATION_ID`, and `ANTHROPIC_SERVICE_ACCOUNT_ID`.

## Usage

=== "Environment variable"

    ```python
    from promptise.identity import AgentIdentity

    identity = AgentIdentity.from_oidc(
        issuer="https://gitlab.com",
        token_env_var="CI_JOB_JWT_V2",
    )
    ```

=== "File"

    ```python
    identity = AgentIdentity.from_oidc(
        issuer="https://token.actions.githubusercontent.com",
        token_file="/var/run/oidc/token",
    )
    ```

=== "Callable"

    ```python
    def mint_jwt() -> str:
        ...  # call your issuer and return the JWT

    identity = AgentIdentity.from_oidc(
        issuer="https://keycloak.example.com/realms/agents",
        token_fn=mint_jwt,
    )
    ```

Wire it into an agent:

```python
from promptise import build_agent

agent = await build_agent(
    model="anthropic:claude-sonnet-4-5",
    servers={},
    identity=AgentIdentity.from_oidc(
        issuer="https://gitlab.com",
        token_env_var="CI_JOB_JWT_V2",
    ),
)
```

## Verify

```python
identity = AgentIdentity.from_oidc(
    issuer="https://gitlab.com",
    token_env_var="CI_JOB_JWT_V2",
)
print(identity.provider_name)         # oidc:https://gitlab.com
assert identity.get_token().startswith("sk-ant-oat01-")
```

## Troubleshooting

| Symptom | Likely cause |
| --- | --- |
| `ProviderConfigError: exactly one token source …` | Zero or more than one of `token_file` / `token_fn` / `token_env_var` was supplied. |
| `TokenAcquisitionError: … environment variable … is not set` | The named env var is missing at refresh time (the CI job's token expired or was never exported). |
| File mode: file-not-found | The issuer did not write the token, or the path is wrong. |
| `TokenExchangeError` | The Console rule's issuer/audience does not match the OIDC token's `iss`/`aud` claims. |
