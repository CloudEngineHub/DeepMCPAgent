# Roadmap

The direction for Promptise Foundry. This is a living document — open an issue or a discussion if you want to influence what lands when.

---

## v1.0.0 — April 2026 (current)

The foundation release. Production-ready from line one.

- Agent factory with 18 opt-in parameters (memory, cache, guardrails, approvals, events, streaming, fallback, adaptive strategy)
- Reasoning Engine with 20+ node types, 7 prebuilt patterns, custom nodes, per-node models
- Native MCP server and client — both written from scratch, no wrappers
- Agent Runtime with 5 trigger types, crash recovery, multi-granularity rewind, budget/health/mission governance, 14 lifecycle hooks
- Prompt Engineering with 8 typed blocks, ConversationFlow, 5 composable strategies, 4 perspectives, 11 context providers, ContextEngine
- RAG foundation with pluggable loader / chunker / embedder / store
- AutoApprovalClassifier with 5-layer decision hierarchy
- 37-endpoint orchestration REST API with typed Python client
- Distributed runtime coordinator for multi-node deployments
- Apache 2.0, 161 source modules, 3,400+ tests

---

## v1.1.x — Q3 2026

Quality-of-life, ecosystem reach, and observability depth.

- **First-class provider presets** — one-line configs for common stacks (Supabase + OpenAI, Neon + Anthropic, local Ollama + Chroma, enterprise Azure)
- **Built-in evaluation harness** — unit tests for agents: golden-run replay, semantic assertions, regression tracking
- **Remote agent debugger** — connect to a running agent process, inspect state, pause at node boundaries, inject state mutations
- **Prompt A/B framework** — ship two prompt variants, route traffic, auto-rollback on quality drop
- **OpenTelemetry out-of-the-box** — zero-config traces via env vars
- **Streaming improvements** — cancellation propagation, partial-tool-result streaming
- **Broader language support** — expand embedded guardrail languages (currently English-centric)

---

## v1.2.x — Q4 2026

Scale, fleets, and cost control.

- **Agent fleet manager** — deploy and orchestrate hundreds of agent processes across a cluster with budget-aware scheduling
- **Cross-agent consensus** — multi-agent vote, debate, and agreement primitives on top of `ask_peer` / `broadcast`
- **Cost budgets at the node level** — per-reasoning-node budget enforcement, not just per-run
- **Shared memory across agents** — namespace-scoped memory with access policies, cross-agent retrieval
- **Web-based dashboard** — extend the terminal dashboard to a hosted web UI with multi-tenant views

---

## v2.0.0 — 2027 vision

The "agents as a first-class operational primitive" release.

- **Agent-native package manager** — discover, install, and compose agents (and their MCP servers, prompts, skills) from a registry
- **Policy language** — declarative `.agentpolicy` files for budget, approval, security, observability — applied across a fleet
- **Hot migration** — move an in-flight agent process between nodes without dropping state
- **First-class simulation** — deterministic replay of an entire agent run from the journal, including LLM responses, for regression and debugging
- **Zero-trust agent-to-agent auth** — short-lived mutual TLS certificates issued per call, per scope

---

## How to influence the roadmap

- Open a **[GitHub Discussion](https://github.com/promptise-com/foundry/discussions)** with `[roadmap]` in the title — that's the main signal for what we prioritize
- For concrete feature requests, use the **[feature request issue template](https://github.com/promptise-com/foundry/issues/new?template=feature_request.yml)** — be specific about the use case, not the solution
- For real production pain, open a discussion under **Show and tell** with what you're building — the roadmap gets reshaped by real users far more than by hypotheticals

---

## Release cadence

- **Patch releases** (1.0.x) — weekly to fortnightly, bug fixes and doc updates only
- **Minor releases** (1.x.0) — every 2-3 months, additive features, no breaking changes
- **Major releases** (x.0.0) — ~yearly, breaking changes allowed, migration guide mandatory
