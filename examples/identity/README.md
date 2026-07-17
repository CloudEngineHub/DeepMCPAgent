# Agent Identity — examples

Give an agent a stable, traceable identity, and (when you want it) let the
resources it calls **cryptographically verify and attribute** the caller.

## Start here (runs on a laptop, no cloud, no API key)

| Example | What it shows | Requirements |
|---|---|---|
| [`local/app.py`](local/app.py) | A **local identity** — `agent_id`, attribution, `claims()`. The 30-second on-ramp. | none |
| [`verifiable_mcp/app.py`](verifiable_mcp/app.py) | The **headline value** end to end: the agent presents a signed JWT, a server's `JwksAuth` verifies signature + audience + expiry and surfaces the verified subject (and rejects a wrong-audience token). | none (`cryptography` + `PyJWT` are already deps) |

```bash
python examples/identity/local/app.py
python examples/identity/verifiable_mcp/app.py
```

## Platform examples (run on the named platform / CI)

These show the **production** path — the agent's credential is minted by the
real cloud/IdP via `AgentIdentity.from_*` (or `AgentIdentity.auto()`), with no
secrets to manage. The verify-and-attribute half is identical to
`verifiable_mcp/` above.

| Example | Platform | Factory |
|---|---|---|
| [`github_actions/`](github_actions/) | GitHub Actions OIDC | `from_oidc(token_env_var="ACTIONS_ID_TOKEN_...")` |
| [`aws_lambda/`](aws_lambda/) | AWS Lambda / EKS (IAM role) | `from_aws(...)` |
| [`gke_pod/`](gke_pod/) | Google Kubernetes Engine | `from_gcp(...)` |
| [`aks_workload/`](aks_workload/) | Azure Kubernetes Service | `from_entra(...)` |
| [`spire/`](spire/) | SPIFFE / SPIRE mesh | `from_spiffe(...)` |

The `github_actions` example is exercised end-to-end against a **real OIDC
token** by `.github/workflows/identity-integration.yml`.

## Which provider do I use?

Don't pick by hand — call `AgentIdentity.auto()` and it detects the platform.
If you want to be explicit, see the
[provider decision guide](../../docs/identity/overview.md#which-provider) and the
per-provider pages under `docs/identity/providers/`.
