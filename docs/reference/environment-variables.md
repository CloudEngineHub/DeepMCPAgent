---
seo_title: "Environment Variables Reference"
description: "Every environment variable Promptise Foundry reads or sets: model keys, .env loading, cache encryption, audit keys, promptise run, the runtime CLI, identity and MCPcast."
---

# Environment Variables

Every environment variable Promptise reads, what it does and its default. Variables
are read from the process environment, and from a `.env` file that Promptise loads
for scripts and the CLI (see [Configuration & Secrets](../getting-started/configuration.md)).
A `.env` file never overrides a variable that is already set.

## Model providers

Each provider reads its own key and endpoint variables, for example
`OPENAI_API_KEY`, `ANTHROPIC_API_KEY`, `AZURE_OPENAI_API_KEY` with
`AZURE_OPENAI_ENDPOINT` and `OPENAI_API_VERSION`, `GROQ_API_KEY`, `GEMINI_API_KEY`
or `GOOGLE_API_KEY`, `MISTRAL_API_KEY`, `DEEPSEEK_API_KEY`, `OPENROUTER_API_KEY`,
`TOGETHER_API_KEY`, `XAI_API_KEY`, `FIREWORKS_API_KEY`, `AWS_BEARER_TOKEN_BEDROCK`
with `AWS_REGION`, `GOOGLE_CLOUD_PROJECT` with `GOOGLE_CLOUD_LOCATION`,
`AZURE_INFERENCE_ENDPOINT` with `AZURE_INFERENCE_CREDENTIAL`, `HF_TOKEN` (Hugging Face
Inference Providers), `OLLAMA_HOST`, and
`OPENAI_BASE_URL` for any OpenAI-compatible endpoint.

The exact set for every provider is in
[Every provider's variables](../getting-started/configuration.md#every-providers-variables).
Two commands print it for you:

```bash
promptise models env azure            # the lines a provider needs, ready for .env
promptise models check azure:chat-prod   # what is still missing for a model string
```

A key passed in code with `Model(..., api_key=...)` wins over the environment.

## Loading

| Variable | Default | Effect |
|---|---|---|
| `PROMPTISE_NO_DOTENV` | unset | Any non-empty value turns off automatic `.env` loading, for scripts and the CLI. Use it when your platform injects secrets and a stray `.env` must not be read. |

## Agent features

| Variable | Default | Effect |
|---|---|---|
| `PROMPTISE_CACHE_KEY` | unset | Fernet key for `SemanticCache(encrypt_values=True)`. Without it a key is generated at start-up (a warning is logged) and cached values cannot be read after a restart. Create one with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`. See [Semantic Cache](../core/cache.md). |

## MCP servers

| Variable | Default | Effect |
|---|---|---|
| `PROMPTISE_AUDIT_SECRET` | unset | HMAC key `AuditMiddleware` signs the audit chain with when you pass no `hmac_secret`, and the key `promptise audit verify` checks with (`--key-env` names another variable). See [Observability & Monitoring](../mcp/server/observability.md). |

## `promptise run`

| Variable | Default | Effect |
|---|---|---|
| `PROMPTISE_SERVE_TOKEN` | unset | Bearer token clients must send to `promptise run --serve`. The `[serve]` table's `auth_token_env` names this variable by default; a token is required when the server binds a non-loopback host. |
| `PROMPTISE_RUN_SCRIPT` | set by Promptise | The name of the `[scripts]` entry being run. |
| `PROMPTISE_PROJECT_ROOT` | set by Promptise | The directory that holds the manifest (`promptise.toml` or `pyproject.toml`). |
| `PROMPTISE_INIT_CWD` | set by Promptise | The directory you ran `promptise run` from (the program itself runs in the project root). |

See [Projects & promptise run](../core/projects.md).

## Agent Runtime CLI

| Variable | Default | Effect |
|---|---|---|
| `PROMPTISE_RUNTIME_STATE_DIR` | `~/.promptise/runtime` | Where `promptise runtime start` records running runtimes (name, API address, token) for `status`, `stop`, `logs` and `restart`. |
| `PROMPTISE_RUNTIME_TOKEN` | random per start | Bearer token for the control API that `promptise runtime start` serves, and the token the other `runtime` commands send to a `--url` target. |
| `PROMPTISE_HOOK_EVENT` | set by Promptise | Given to a [shell hook](../runtime/hooks.md) command: the event that fired it. |
| `PROMPTISE_PROCESS` | set by Promptise | Given to a shell hook command: the process name. |

See [Runtime CLI Commands](../runtime/cli.md).

## Agent identity

`AgentIdentity.auto()` picks a platform from the first of these groups that has a
variable set:

| Order | Platform | Variables that select it |
|---|---|---|
| 1 | Microsoft Entra ID | `AZURE_FEDERATED_TOKEN_FILE`, `AZURE_CLIENT_ID` |
| 2 | AWS | `AWS_LAMBDA_FUNCTION_NAME`, `AWS_EXECUTION_ENV`, `EKS_POD_NAME`, `AWS_WEB_IDENTITY_TOKEN_FILE`, `PROMPTISE_IDENTITY_TOKEN_FILE` |
| 3 | Google Cloud | `GOOGLE_CLOUD_PROJECT`, `K_SERVICE`, `GCE_METADATA_IP` |
| 4 | SPIFFE / SPIRE | `SPIFFE_ENDPOINT_SOCKET` |

`PROMPTISE_IDENTITY_TOKEN_FILE` is also the token file the AWS projected mode presents:
`AgentIdentity.from_aws(mode="auto")` (the default) uses projected mode when it is set,
and STS otherwise. See [Agent Identity](../identity/overview.md)
and [Architecture](../identity/architecture.md).

## MCPcast

Read by `promptise mcpcast` while it parses a spec and runs the readiness eval:

| Variable | Default | Effect |
|---|---|---|
| `MCPCAST_FETCH_SECONDS` | `60` | Wall-clock limit for downloading a spec from a URL. |
| `MCPCAST_MAX_SPEC_BYTES` | 20 MiB | Largest spec download. |
| `MCPCAST_MAX_SPEC_NODES` | `2000000` | Most YAML/JSON nodes after aliases are expanded. |
| `MCPCAST_EVAL_AUTHORIZATION` | placeholder | Credential the `--eval` live reads send upstream, for example `Bearer <token>`. |
| `MCPCAST_EVAL_HEADERS` | unset | JSON object of headers for the `--eval` live reads; replaces the default header. |

Read by a **generated** server (`<name>-mcp/config.py`):

| Variable | Default | Effect |
|---|---|---|
| `MCPCAST_UPSTREAM_TOKEN` | unset | Upstream credential in `--auth env-token` mode, for example `Bearer <token>`. Read on every call, so rotating it needs no restart. |
| `MCPCAST_UPSTREAM_TOKENS` | unset | JSON object `{"<tenant_id>": "<credential>"}` for `--auth api-key` mode. Read on every call. |
| `MCPCAST_CLIENT_KEYS` | unset | JSON object mapping each client API key to `{"client_id", "tenant_id", "roles"}` in `api-key` mode. Read at start-up. |
| `MCPCAST_BASE_URL` | the spec's server | Points the server at another environment without regenerating. Must be a plain `http(s)://` URL without credentials, query or fragment. |
| `MCPCAST_TIMEOUT` | `30` | Seconds one upstream call may take in total. |
| `MCPCAST_APPROVAL_TIMEOUT` | `300` | Seconds a gated call waits for a reviewer. |
| `MCPCAST_MAX_RESPONSE_BYTES` | `1048576` | Largest upstream response body passed to the agent. |
| `MCPCAST_ERROR_EXCERPT_CHARS` | `500` | How much of an upstream error body the agent sees (`0` hides it). |
| `MCPCAST_MAX_PENDING` | `100` | Gated calls waiting for review at once, in approval mode `pending`. |
| `MCPCAST_MAX_PENDING_PER_TENANT` | `40` | The same cap per tenant. |
| `MCPCAST_MAX_PENDING_PER_CLIENT` | `20` | The same cap per client key. The caps must nest (client, tenant, store) or the server refuses to start. |
| `MCPCAST_ALLOW_INSECURE_HTTP` | unset | `1` sends the upstream credential to a plain `http://` host that is not loopback. |
| `MCPCAST_PUBLIC` | unset | `1` lets a server without MCP-level authentication bind a non-loopback address (what `--public` sets, for containers). |

See the [MCPcast reference](../mcp/server/mcpcast.md).

## Set by Promptise, do not set them yourself

Promptise sets these on the processes it starts to coordinate with them:
`PROMPTISE_RUN_CHILD`, `PROMPTISE_RUN_DEPTH`, `PROMPTISE_RUN_IPC`, `PROMPTISE_RUN_TUI`
(`promptise run`), `PROMPTISE_RUNTIME_LAUNCH_ID` (`promptise runtime start --detach`),
`PROMPTISE_EXEC_ID` (sandbox commands) and `_PROMPTISE_RELOAD_CHILD`
(MCP server hot reload).

## Your own variables in config files

`.superagent` files and `.agent` manifests resolve `${VAR}` and `${VAR:-default}`
from the environment, so any name you choose works there. See
[Environment Resolver](../core/env-resolver.md).
