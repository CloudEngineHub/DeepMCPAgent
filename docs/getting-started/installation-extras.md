# Installation Extras

Promptise is modular.  The base install includes the agent core, MCP client, prompt engine, and runtime.  Optional extras add ML models, infrastructure backends, and observability integrations.

## Quick Reference

| Extra | Install Command | What You Get |
|-------|-----------------|--------------|
| **ml** | `pip install promptise[ml]` | ChromaDB vector memory, sentence-transformers embeddings, Mem0 graph memory, GLiNER NER |
| **infra** | `pip install promptise[infra]` | Redis cache/conversations/rate-limiting, Docker sandbox |
| **observability** | `pip install promptise[observability]` | OpenTelemetry spans, Prometheus metrics endpoint |
| **mcp** | `pip install promptise[mcp]` | MCP server SDK (if installing server-side only, without the full agent) |
| **docs** | `pip install promptise[docs]` | MkDocs + Material + mkdocstrings for building documentation |
| **all** | `pip install promptise[all]` | Everything above (ml + infra + observability + docs) |
| **dev** | `pip install promptise[dev]` | All + pytest, mypy, ruff, coverage |

## Detailed Breakdown

### `[ml]` -- Machine Learning

```bash
pip install promptise[ml]
```

| Package | Version | Used By |
|---------|---------|---------|
| `chromadb` | >= 0.5.0 | `ChromaProvider` -- local persistent vector memory |
| `sentence-transformers` | >= 2.2.0 | Semantic tool selection, memory embeddings |
| `transformers` | >= 4.30.0 | DeBERTa prompt-injection detection (guardrails) |
| `numpy` | >= 1.24.0 | Embedding math |
| `mem0ai` | >= 0.1.0 | `Mem0Provider` -- enterprise graph memory |

**When to install:** You need vector memory (`ChromaProvider`), semantic tool optimization (`OptimizationLevel.SEMANTIC`), or ML-based guardrails (`PromptiseSecurityScanner`).

### `[infra]` -- Infrastructure

```bash
pip install promptise[infra]
```

| Package | Version | Used By |
|---------|---------|---------|
| `redis` | >= 5.0.0 | `RedisConversationStore`, `RedisCache` (semantic cache), `RedisConversationStore` |
| `docker` | >= 7.0.0 | `DockerBackend` -- sandboxed code execution |

**When to install:** You need Redis-backed caching or conversations, or Docker-based code sandbox.

### `[observability]` -- Observability

```bash
pip install promptise[observability]
```

| Package | Version | Used By |
|---------|---------|---------|
| `opentelemetry-api` | >= 1.20.0 | `OTelMiddleware` -- distributed tracing |
| `opentelemetry-sdk` | >= 1.20.0 | Span export and configuration |
| `opentelemetry-exporter-otlp` | >= 1.20.0 | OTLP export to Jaeger, Grafana Tempo, etc. |
| `prometheus_client` | >= 0.20.0 | `PrometheusMiddleware` -- `/metrics` endpoint |

**When to install:** You need OpenTelemetry tracing or Prometheus metrics for your MCP servers.

### `[all]` -- Everything

```bash
pip install promptise[all]
```

Installs `ml` + `infra` + `observability` + `docs`.  Recommended for development and evaluation.

## Combining Extras

You can combine any extras:

```bash
# ML + infrastructure, no observability
pip install promptise[ml,infra]

# Full production stack
pip install promptise[ml,infra,observability]
```

## Air-Gapped / Offline Deployments

For environments without internet access:

1. Download wheels on a connected machine: `pip download promptise[all] -d ./wheels`
2. Transfer the `wheels/` directory to the target machine
3. Install from local wheels: `pip install --no-index --find-links=./wheels promptise[all]`

For semantic tool optimization with local models, set the `embedding_model` path in `ToolOptimizationConfig` to a local directory containing the model files.
