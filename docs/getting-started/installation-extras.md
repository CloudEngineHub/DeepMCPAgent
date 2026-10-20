---
title: Install Promptise Foundry — pip install with optional extras
description: Install Promptise Foundry with pip. Python 3.10+. Optional extras for memory backends (Chroma, Mem0), sandboxing (Docker, gVisor), observability (Prometheus, OpenTelemetry), and more. The base install ships a complete framework.
keywords: install promptise, pip install promptise, Promptise Foundry install, Python AI agent install
---

# Installation

Promptise ships a complete framework in the base install. You only need an extra when you want the full production stack.

## Two choices

```bash
pip install promptise              # core — start here
pip install "promptise[all]"       # production-ready
```

That's it.

## What you get

### `pip install promptise`

The base install. Everything you need to build, run, and deploy agents that call MCP tools.

- Core agent factory (`build_agent()`)
- Reasoning engine (graph + 20+ node types)
- MCP server SDK + native MCP client
- Agent runtime (triggers, journal, governance, hooks)
- Prompt engineering (blocks, flows, strategies, guards, ContextEngine)
- CLI (`promptise ...`)
- Orchestration REST API
- Every model provider: OpenAI, Azure OpenAI and Anthropic natively; Groq, Gemini, Vertex AI, Bedrock, Mistral, Ollama, the Azure AI Foundry catalog and more through their OpenAI-compatible endpoints — no per-provider packages (see [Model Setup](model-setup.md))

### `pip install "promptise[all]"`

Adds the heavy dependencies that unlock everything optional. Recommended for production and for evaluating the full feature set.

| Category | Package | Unlocks |
|---|---|---|
| **Vector memory** | `chromadb`, `mem0ai` | `ChromaProvider`, `Mem0Provider` |
| **Embeddings** | `sentence-transformers`, `numpy` | Semantic tool optimization, `SemanticCache` |
| **ML guardrails** | `transformers` | DeBERTa prompt-injection detection (`InjectionDetector`, `guardrails=True`). GLiNER NER (`NERDetector`) needs `pip install gliner` on top |
| **Infrastructure** | `redis`, `docker` | `RedisConversationStore`, `RedisCache`, Docker sandbox |
| **Observability** | `opentelemetry-*`, `prometheus_client` | OTel tracing, Prometheus `/metrics` |

### `pip install "promptise[sandbox]"`

Only the Docker client, for the code-execution sandbox (`build_agent(sandbox=...)`
and `agent_pattern="code-action"`) without the rest of `[all]`. Docker itself must
be installed and running. See [Sandbox](../core/sandbox.md).

### `pip install "promptise[identity-aws]"`, `[identity-spiffe]`, `[identity-all]`

Cloud SDKs for some [agent identity](../identity/overview.md) modes: `boto3` for AWS STS
federation, and [py-spiffe](https://github.com/HewlettPackard/py-spiffe) (the `spiffe`
package) for the SPIFFE Workload API (`AgentIdentity.from_spiffe(mode="sdk")`, and
`AgentIdentity.auto()` when `SPIFFE_ENDPOINT_SOCKET` is set). `[identity-all]` installs
both. Before 1.5.1 the SPIFFE extra named a `pyspiffe` package that is not on PyPI, so it
could not be installed.

## MCP SDK versions

Promptise works with both major versions of the official MCP Python SDK, `mcp>=1.9,<3`.
A new install gets the current 2.x SDK, so Promptise installs next to other packages
that need it, and MCP 2.x clients reach a Promptise server with the 2026-07-28 protocol
revision (1.x clients still use the 2025 handshake). Promptise 1.5.0 and earlier
required `mcp<2`.

To stay on the 1.x SDK, pin it:

```bash
pip install promptise "mcp>=1.9,<2"
```

Promptise's own APIs are the same with either SDK. Your own code that reads MCP SDK
objects, such as the `Tool` and `CallToolResult` objects `MCPClient` and `TestClient`
return, follows the SDK: 2.x names the fields in snake_case (`tool.input_schema`,
`result.is_error`) where 1.x has the wire names (`tool.inputSchema`, `result.isError`).
See the SDK's [migration guide](https://py.sdk.modelcontextprotocol.io/migration/).

## Contributors

```bash
pip install "promptise[dev]"
```

Everything in `[all]` plus test runners (`pytest`, `pytest-asyncio`, `pytest-cov`), lint (`mypy`, `ruff`, `types-PyYAML`), and docs tooling (`mkdocs`, `mkdocs-material`, `mkdocstrings`).

## Air-gapped deployments

For environments without internet access:

```bash
# On a connected machine
pip download "promptise[all]" -d ./wheels

# Transfer the wheels/ directory

# On the target machine
pip install --no-index --find-links=./wheels "promptise[all]"
```

For semantic tool optimization with local embedding models, set `embedding_model` to a local directory path in `ToolOptimizationConfig`.

## Migrating from older extras

Earlier versions had `[ml]`, `[infra]`, `[observability]`, `[mcp]`, `[deep]`, and `[docs]` as separate extras. All of their dependencies are now in `[all]`.

| Old | New |
|---|---|
| `pip install "promptise[ml]"` | `pip install "promptise[all]"` |
| `pip install "promptise[infra]"` | `pip install "promptise[all]"` |
| `pip install "promptise[observability]"` | `pip install "promptise[all]"` |
| `pip install "promptise[mcp]"` | `pip install promptise` (MCP is core) |
| `pip install "promptise[docs]"` | `pip install "promptise[dev]"` |
| `pip install "promptise[deep]"` | `pip install deepagents` (installed separately) |
