# Changelog

## 1.1.0 — 2026-07-08

### Identity

- **Agent Identity subsystem (`promptise.identity`)** — every agent gets a stable, traceable identity (*who is acting*), so its tool calls, audit entries, and outbound requests are all attributable. An identity can be **local** (just an `agent_id`) or **verifiable** — backed by a credential provider that mints a signed JWT the agent presents to the resources it calls (e.g. MCP servers). One user-facing class, `AgentIdentity`, with `from_*` factories and `AgentIdentity.auto()` platform auto-detection.
- **Workload-identity-federation providers** — Microsoft Entra ID (managed identity + projected token), AWS IAM (STS + EKS projected), Google Cloud (metadata), SPIFFE/SPIRE (file + SDK), and a generic OIDC issuer (file + callable). Per-resource credentials, token caching with refresh buffer, and declarative configuration via `.superagent` / `.agent` manifests.
- **Wired through the stack** — `build_agent(identity=...)`, cross-agent calls, MCP server auth + HMAC-chained audit, runtime processes, and observability all carry the agent identity. No third-party identity SDKs required for the core; cloud SDKs are optional per provider.
- **Production hardening** — providers now retry transient credential-acquisition failures (timeout / connection / 429 / 5xx, STS throttling) with jittered backoff and never retry a 4xx auth failure (fixes a real bug where one metadata blip silently degraded a verifiable agent to *unauthenticated*); server-side JWT verification tolerates a configurable clock-skew `leeway` (default 60s); thread-safe per-audience credential caching is now covered by a concurrency test.
- **Easier to adopt** — `AgentIdentity` and `IdentityError` are re-exported from top-level `promptise`; two **laptop-runnable** examples (no cloud, no API key) — a local-identity on-ramp and an end-to-end *verifiable identity → MCP server verifies + attributes the caller* demo; and opt-in, platform-gated live integration smoke tests (`tests/identity/integration/`) that mint a real token per provider so the live cloud round-trip is confirmable in your environment.
- **Comprehensive docs** — an enterprise "why this matters" (problem → risk → who/when), a "which provider" decision guide, an honest "verification status" (unit-mocked vs live-smoke-tested), and a corrected architecture diagram.

### Engine

- **Automatic context handling by default (`context_scope="auto"`)** — the default ReAct agent (and thus `build_agent()`) now manages context automatically: it behaves exactly like `"full"` while a tool loop is short (zero change to simple tasks), and switches to the bounded, deduplicated facts-ledger once the loop grows past `auto_ledger_after` (default 6) tool results. Deep tool loops stay token-efficient and context-bounded with no pattern to choose. It's an efficiency/context primitive — not an accuracy claim (for accurate aggregation over data, use `code-action`).
- **`code-action` reasoning pattern** — `build_agent(agent_pattern="code-action")`: for aggregation / data-traversal tasks the model writes **one Python program** over your tools (in a single LLM turn) instead of chaining dozens of conversational tool calls. The program runs in the hardened Docker sandbox (read-only rootfs, dropped capabilities, **no network**); its tool calls bridge back to the real host tools over a filesystem-RPC channel, where each tool keeps its protections — approval gates if configured, plus budget/health/audit hooks when the Agent Runtime has attached them — and the node enforces a hard per-run `max_tool_calls` cap regardless. Sandbox is auto-enabled (requires Docker). Bounded self-repair on a crash. Best with tools that return structured data (lists/dicts/numbers).
- **`verify` reasoning pattern** — `build_agent(agent_pattern="verify")` runs single-pass self-verifying reasoning (plan → solve → self-check → final answer) at one-turn latency. Matches a plain prompt on models that already reason internally; recovers errors a single pass would miss on weaker/mainstream models.
- **`context_scope="scoped"` on `PromptNode`** — context-lifecycle management: a scoped stage sees only its working set (system prompt + task + its own tool loop), not the whole transcript, bounding token growth across multi-stage reasoning graphs. Opt-in; `default="full"` preserves existing behavior.
- **`managed` tool pattern + `context_scope="ledger"`** — `build_agent(agent_pattern="managed")`: a context-managed tool loop for deep multi-tool tasks. Instead of an ever-growing transcript (where the model re-queries the same facts), the node keeps a compact deduplicated "facts gathered" ledger and serves identical `(tool, args)` calls from cache. Cuts redundant tool calls and bounds token growth at equal accuracy — an efficiency primitive, not an accuracy claim.
- **Fix: routing-hint noise** — linear nodes (single `default_next`, no real branch) no longer receive a spurious "choose the next step" instruction that weaker models could emit as their answer.

### MCP Server

- **First-class multi-tenancy** — `tenant_id` becomes a structural isolation invariant across the stack. Server side: `ClientContext.tenant_id` populated by `AuthMiddleware` from a configurable JWT claim (default `tenant_id`) or the API-key config; tenant-qualified rate-limit buckets (one tenant's traffic can never exhaust another's quota); `tenant_id` in every audit entry; `RequireTenant`/`HasTenant` guards; and `MCPServer(require_tenant=True)` to force authentication + tenant identity on every tool. Agent side: `CallerContext.tenant_id` + one derivation (`isolation_key`, `tenant::user`) feeds semantic-cache scope keys, memory scoping, and conversation ownership — two tenants with the same `user_id` can never see each other's data (`SessionAccessDenied` on cross-tenant session access, structurally impossible cache hits, provider-level memory isolation). `SemanticCache.purge_user(user_id, tenant_id=...)` purges exactly the tenant's scope.
- **Server-side approval gates (HITL where the tool lives)** — `@server.tool(requires_approval=True)` + `ApprovalGateMiddleware` enforce human approval for **any** MCP client, not just Promptise agents. Fail-closed semantics: denied by default on timeout, denied on handler crash, denied if a decision carries `modified_arguments` (the gate cannot rewrite bound arguments). An ungated `requires_approval` declaration **refuses to build** rather than silently not enforcing. Three approvers: `PendingApprover` (blocking pending store + auto-registered role-guarded `approvals_list`/`approvals_decide` admin tools — independent four-eyes review with **enforced separation of duties**: a caller cannot approve their own request), `ElicitationApprover` (MCP elicitation confirms with the human behind the calling client; denies fail-closed without a live session), and any existing `promptise.approval` handler (callback, HMAC-signed webhook) via the shared `ApprovalRequest`/`ApprovalDecision`/`ApprovalHandler` protocol. The gate evaluates the tool's guards **before** requesting approval (unauthorized callers never reach a reviewer), and `requires_approval` survives `include_router`/`mount` composition. Approval requests carry client id, tenant, and JWT subject; outcomes surface as structured `APPROVAL_DENIED` errors visible to the audit chain.

### Dependencies & CI

- **Eight Dependabot updates consolidated** into this single release and tested together, so the whole upgrade lands and verifies as one unit rather than eight separate merges — pydantic ≥2.13.3, cryptography ≥46, orjson ≥3.11.8, numpy ≥2.2.6, and the latest langchain / langchain-openai lines, plus GitHub Actions bumps (checkout v7, setup-python v6, codecov v7, codeql v4).
- **Verified against the latest majors a clean install resolves** — langchain **1.x** (langchain-core 1.4.8), numpy 2.5, mypy 2.x, pytest 9.x — the framework is runtime-compatible (full suite green) with **no dependency caps**. Compatibility fixes: dependency-resolution unblock (drop `pyspiffe` → `grpcio` from the `dev` extra), an import-cycle break (`cross_agent` ↔ `observability`), a numpy-2.x stub `mypy` override, and a widened `on_llm_new_token` override for langchain-core 1.x.
- **Cross-platform CI green** — two POSIX-only identity tests made platform-agnostic for Windows; the real-model ML-guardrail tests are skipped on **macOS hosted CI only** (gated on `darwin && $CI`), where the DeBERTa injection scores flap below the 0.85 threshold — they still run on Linux CI, Windows CI, and local macOS dev. The full 30-check matrix passes.

### Fixed

- **CLI: `promptise serve` now ships** — the documented deployment command (`promptise serve myapp:server --transport http --port 8080 --dashboard --reload`) existed in the docs but was never registered in the CLI. It now resolves the `module:attribute` target, validates it is an `MCPServer`, and serves over stdio / HTTP / SSE, with `--dashboard` and `--reload` support and clean stderr errors (stdout stays protocol-clean for stdio).
- **MCP server: declared per-tool rate limits are enforced** — `@server.tool(rate_limit="100/min")` was accepted and stored but never read. The spec is now parsed at registration (a malformed spec raises `ValueError` immediately) and enforced automatically via an auto-inserted middleware — per client when authentication populates `client_id`, a shared bucket otherwise — raising `RateLimitError` with a `retry_after_seconds` hint. `TestClient` enforces the same contract. New public API: `parse_rate_limit`, `DeclaredRateLimitMiddleware`.
- **Core: `CallerContext` survives cross-agent delegation** — a peer agent invoked via `ask_peer`/`broadcast` overwrote the ambient caller with `None`, dropping the original user's identity at every delegation hop. `PromptiseAgent.ainvoke` now inherits the ambient `CallerContext` when no explicit `caller` is passed, so a peer's cache scoping, memory search, guardrail tagging, and conversation ownership stay attributed to the original human principal (an explicit `caller=` still takes precedence).
- **Docs: removed the phantom `AgentAccessPolicy`** — design docs referenced a class that never existed in the codebase. Replaced with the real layered access-control model: transport-level auth providers, per-tool `Guard`s, per-request `CallerContext`, and runtime `OpenModeConfig` guardrails.

## 1.0.0 — 2026-03-26

### Promptise Foundry — Production Release

**The complete framework for building production agentic AI systems.** This release marks the transition from DeepMCPAgent to Promptise Foundry with a ground-up rebuild of every module.

---

### Core Agent

The `build_agent()` factory now accepts 18 opt-in parameters, each enabling a production capability with zero overhead when disabled:

- **Semantic Tool Optimization** — `optimize_tools="semantic"` sends only relevant tools per query using local embeddings (40-70% token savings). Configurable embedding model, supports local paths for air-gapped deployments.
- **Conversation Persistence** — `conversation_store=` with 4 backends (InMemory, SQLite, PostgreSQL, Redis). Session ownership enforcement prevents cross-user access. `chat()` method handles load/save/ownership automatically.
- **Semantic Cache** — `cache=SemanticCache()` serves similar queries from cache (30-50% cost savings). Per-user isolation by default, GDPR `purge_user()`, optional Redis backend with AES encryption at rest.
- **Security Guardrails** — `guardrails=PromptiseSecurityScanner.default()` with 6 detection heads: prompt injection (DeBERTa ML model), PII detection (69 regex patterns + Luhn validation), credential detection (96 patterns), Named Entity Recognition (GLiNER), content safety (Llama Guard / Azure AI), custom rules. All models run locally.
- **Human-in-the-Loop Approval** — `approval=ApprovalPolicy(tools=["send_*"])` pauses execution on sensitive tool calls, sends approval request to webhook/callback/queue, waits for human decision. HMAC-signed requests, replay protection, max pending limits.
- **Event Notifications** — `events=EventNotifier(sinks=[WebhookSink(...)])` emits structured events on 20 event types across 9 categories (invocation, tool, guardrail, budget, approval, mission, health, process, cache). 4 sink types: webhook (HMAC-signed, retries, SSRF-protected), callback, log, EventBus.
- **Streaming with Tool Visibility** — `astream_with_tools()` yields 5 event types (ToolStartEvent, ToolEndEvent, TokenEvent, DoneEvent, ErrorEvent) for real-time chat UIs. Auto-generated tool display names, argument redaction via guardrails.
- **Model Fallback Chain** — `model=FallbackChain(["openai:gpt-5-mini", "anthropic:claude-sonnet-4-20250514"])` with per-model circuit breakers, global timeout, configurable failure threshold and recovery window.
- **Adaptive Strategy** — `adaptive=True` captures tool failures, classifies them (infrastructure vs strategy vs unknown), synthesizes actionable strategies via LLM after threshold failures, injects learnings as context. Human feedback with LLM-as-judge verification.
- **Context Engine** — `context_engine=ContextEngine(budget=128000)` provides token-budgeted context assembly. Register layers by priority (identity, rules, memory, strategies, conversation, user message). Exact token counting via tiktoken. Trims lowest-priority content first. Snapshot/restore prevents permanent mutation.
- **Invocation Timeout** — `max_invocation_time=30` enforces maximum seconds per invocation with `asyncio.wait_for`, emits `invocation.timeout` event.
- **CallerContext** — Per-request identity (user_id, roles, scopes, metadata) propagated via contextvars to cache, guardrails, conversations, events, memory scoping.

### MCP Server SDK

Production framework for building MCP tool servers:

- **Authentication** — JWTAuth (HS256), AsymmetricJWTAuth (RS256/ES256), APIKeyAuth. Token caching with LRU eviction. TokenEndpointConfig for OAuth2 client_credentials.
- **Guards** — Per-tool authorization: HasRole, HasAllRoles, HasScope, HasAllScopes, RequireAuth, RequireClientId. Custom guards via protocol.
- **8 Middleware Types** — Logging, Timeout, RateLimit, CircuitBreaker, ConcurrencyLimiter, PerToolConcurrencyLimiter, StructuredLogging, Audit (HMAC-chained tamper-evident entries).
- **Caching** — InMemoryCache (LRU+TTL), RedisCache, @cached decorator, CacheMiddleware.
- **Job Queue** — MCPQueue with priority scheduling, retry, progress reporting, cancellation. 5 auto-registered tools.
- **Health Checks** — Liveness, readiness, startup probes. Kubernetes-native.
- **Metrics** — Prometheus /metrics endpoint, OpenTelemetry spans.
- **Dashboard** — Live terminal UI with 6 tabs.
- **OpenAPI Import** — OpenAPIProvider auto-generates MCP tools from OpenAPI specs.
- **Streaming** — StreamingResult for chunked responses. ProgressReporter for real-time updates.
- **Elicitation & Sampling** — Request user input or LLM completions mid-execution.
- **TestClient** — Full pipeline testing in-process (no network).
- **3 Transports** — stdio, streamable HTTP, SSE. CORS configurable.
- **MCP Client** — MCPClient (single), MCPMultiClient (N servers, auto-routing), MCPToolAdapter (MCP → LangChain BaseTool).

### Agent Runtime

Operating system for autonomous agents:

- **AgentProcess** — 6-state lifecycle (CREATED → STARTING → RUNNING → SUSPENDED → STOPPING → STOPPED/FAILED). Deterministic state machine with logged transitions.
- **5 Trigger Types** — Cron, Webhook (HMAC verified), File Watch (glob patterns), Event (EventBus), Message (topic pub/sub with wildcards). Custom trigger types via `register_trigger_type()`.
- **AgentContext** — Key-value state with write permissions, mutation history, memory access, environment variables, file mounts.
- **Journals** — InMemoryJournal, FileJournal. ReplayEngine for crash recovery from checkpoint + replay.
- **Governance: Budget** — Per-run and daily limits (tool calls, LLM turns, cost, irreversible actions). ToolCostAnnotation per tool. Warning at 80% threshold. Enforcement: pause, stop, or escalate.
- **Governance: Health** — Behavioral anomaly detection: stuck (identical calls N times), loop (repeating patterns), empty response, high error rate. Cooldown between alerts. Recovery detection.
- **Governance: Mission** — Objective + success criteria. LLM-as-judge evaluation every N invocations. Confidence thresholds. Timeout and invocation limits. Auto-complete on success.
- **Governance: Secrets** — Per-process credential context. ${ENV_VAR} resolution. TTL-based expiry. Zero-fill revocation. Access logging. Values never serialized.
- **Open Mode** — 14 meta-tools for self-modifying agents: modify_instructions, create_tool, connect_mcp_server, add/remove_trigger, spawn/list_processes, store/search/forget_memory, list_capabilities, get_secret, check_budget, check_mission. Guardrails: max instruction length, max custom tools, MCP URL whitelist, mandatory sandbox.
- **Live Agent Conversation** — MessageInbox with TTL, priority, rate limiting. `send_message()` and `ask()` methods. Messages injected into agent context. Answer extraction from agent responses.
- **Orchestration API** — 37 REST endpoints for managing deployed agents without code changes. Deploy, start, stop, restart, suspend, resume. Update instructions, budget, health, mission at runtime. Trigger management. Secret rotation. Journal reading. Context inspection. All endpoints authenticated. OrchestrationClient typed Python SDK with 37 matching methods.
- **Distributed** — RuntimeTransport (HTTP management API with auth), RuntimeCoordinator (cluster membership), StaticDiscovery / RegistryDiscovery. No etcd/Consul dependency.
- **.agent Manifests** — Declarative YAML for model, instructions, servers, triggers, context, journal, budget, health, mission, secrets, open mode.
- **Dashboard** — Live terminal UI with process states, invocation counts, trigger status.

### Prompt & Context Engineering

Prompts as software:

- **8 Block Types** — Identity (priority 10), Rules (9), OutputFormat (8), ContextSlot (configurable), Section (configurable), Examples (4), Conditional, Composite. Priority-based token budgeting drops lowest-priority blocks first.
- **ConversationFlow** — Phase-based system prompt evolution. Phases with active blocks and lifecycle hooks.
- **5 Strategies** — ChainOfThought, StructuredReasoning, SelfCritique, PlanAndExecute, Decompose. Composable: `chain_of_thought + self_critique`.
- **4 Perspectives** — Analyst, Critic, Advisor, Creative. CustomPerspective for domain-specific framing.
- **5 Guards** — ContentFilter, Length, SchemaStrict (JSON validation with retry), InputValidator, OutputValidator.
- **11 Context Providers** — Tool, Memory, Task, User, Environment, Conversation, Team, Error, Output, Static, Callable, Conditional, World.
- **PromptBuilder** — Fluent API for runtime construction.
- **Registry** — Semantic versioning. Rollback. Duplicate detection.
- **PromptInspector** — Traces assembly: blocks included/excluded, tokens per block, guard results.
- **Chaining** — chain(), parallel(), branch(), retry(), fallback().
- **YAML Loader** — .prompt files with templates, blocks, strategies, guards.
- **Testing** — mock_llm(), mock_context(), assert_schema(), assert_contains().

### Security (68 findings audited, 27+ fixed)

- SSRF protection on all URL inputs (_validate_url_not_private blocks private IPs, loopback, metadata endpoints)
- JWT algorithm validation (rejects alg confusion attacks)
- Timing-safe comparisons on all secret checks (hmac.compare_digest)
- Shell injection fix in sandbox read_file (shlex.quote)
- CAP_SYS_ADMIN removed from allow_sudo (container escape prevention)
- Null byte rejection in all path validation
- Safe template formatter (blocks attribute access, prevents SSTI)
- exec() code injection prevention in PromptBuilder (regex-validated names)
- Generic error messages to clients (no internal details leaked)
- Batch call auth bypass fixed (parent context propagated)
- Escalation webhook SSRF protection
- Distributed transport auth token + default localhost binding
- Audit chain race condition fixed (asyncio.Lock)
- Rate limiter thread safety + stale bucket eviction
- Memory content sanitization (12 injection patterns, case-insensitive)

### RAG Foundation

Pluggable base classes for retrieval-augmented generation:

- **4 Base Classes** — DocumentLoader, Chunker, Embedder, VectorStore. Subclass for your provider, plug into the pipeline.
- **RAGPipeline** — Orchestrator: `index()` ingests documents, `retrieve()` queries, `delete_document()` cleans up. Returns structured `IndexReport`.
- **Built-in Implementations** — RecursiveTextChunker (separator-aware splitting with overlap), InMemoryVectorStore (cosine similarity, metadata filtering). Zero external dependencies.
- **rag_to_tool()** — Wraps a pipeline as a LangChain tool the agent can call. Markdown, JSON, or text output. Configurable limit.
- **content_hash()** — Stable 12-character hash for dedup and incremental indexing.

### Runtime Lifecycle Hooks

Event-driven hook system for agent processes:

- **HookManager** — 14 lifecycle events: SESSION_START/END, USER_PROMPT_SUBMIT, PERMISSION_REQUEST/DENIED, SUBAGENT_START/STOP, PRE/POST_COMPACT, FILE_CHANGED, CONFIG_CHANGE, TASK_CREATED/COMPLETED.
- **`once: true`** — Hook auto-deregisters after first invocation. One-shot setup, first-run greetings, "next time X happens" patterns.
- **Priority ordering** — Higher priority hooks run first. Exception isolation: one broken hook never blocks the rest.
- **HookBlocked** — Raise to short-circuit and cancel the action that triggered the hook.
- **ShellHook** — Runtime hook backed by an external command. JSON event on stdin, JSON response on stdout. Supports blocking via `{"block": true}` and data mutation via `{"data": {...}}`. Configurable timeout, cwd, env.

### Shell Context Injection in Templates

Opt-in `!`cmd`` syntax in prompt templates:

- **SubprocessShellExecutor** — Runs shell commands with configurable timeout and allowlist. Only active when explicitly passed to the TemplateEngine.
- **Disabled by default** — Without a shell_executor, the syntax is left as literal text. Zero security exposure.
- **Allowlist support** — Restrict which commands are permitted for hardened environments.

### Multi-Granularity Rewind

Non-destructive rollback over the journal:

- **RewindEngine** — 5 modes: BOTH (full rollback), CONVERSATION_ONLY (keep tool results), CODE_ONLY (keep chat), SUMMARIZE (inject summary instead of dropping), CANCEL (dry-run preview).
- **Non-destructive** — Original journal entries stay on disk. The rewind itself is recorded as a `rewind` entry.
- **plan()** — Preview what would happen before committing.

### Path-Scoped Skill Activation

Skills that only activate when the codebase matches:

- **SkillRegistry** — Register skills with `paths: ["**/*.tsx", "src/**/*.ts"]` globs. `activate_for(cwd)` returns only skills whose globs match files under the working directory.
- **File-based loading** — Write a `.py` file with YAML frontmatter (`name`, `description`, `paths`) and a `create()` function. Load an entire directory with `load_directory()`.
- **Frontmatter parser** — Minimal YAML-ish parser, no external dependencies.

### AutoApprovalClassifier

Explicit decision hierarchy for approval requests:

- **5-layer hierarchy** — (1) Allow rules → (2) deny rules → (3) read-only auto-allow → (4) optional LLM classifier → (5) fallback to human handler.
- **ApprovalRule** — Match by tool glob, user ID, argument substring, or async predicate.
- **Drop-in replacement** — Implements the ApprovalHandler protocol. One-line swap in any existing ApprovalPolicy.
- **ClassifierStats** — Per-layer hit counts for audit and tuning.

### SuperAgent YAML

All features configurable via .superagent files:

- memory, observability, optimize_tools, cache, approval, events, adaptive, guardrails, max_invocation_time

### Simplified Install — Two Choices

Old multi-extra matrix (`[ml]`, `[infra]`, `[observability]`, `[mcp]`, `[docs]`, `[deep]`) has been collapsed into two clear extras:

- `pip install promptise` — base install is now complete (agent, MCP server + client, runtime, prompts, CLI, OpenAI, `aiohttp`, `watchdog`, cryptography)
- `pip install "promptise[all]"` — everything production-ready: ChromaDB, Mem0, sentence-transformers, transformers, numpy, Redis, Docker, OpenTelemetry, Prometheus
- `pip install "promptise[dev]"` — contributors only: everything in `[all]` plus pytest, mypy, ruff, mkdocs tooling

The `[mcp]` extra is removed — MCP is core. The `[deep]` extra is removed — install `deepagents` separately if you need it.

### Breaking Changes from DeepMCPAgent

- **Package**: `deepmcpagent` → `promptise`
- **Imports**: `from deepmcpagent` → `from promptise`
- **CLI**: `deepmcpagent` → `promptise`
- **Factory**: `build_deep_agent()` → `build_agent()`
- **Servers**: Dict format → `HTTPServerSpec` / `StdioServerSpec` objects

### Statistics

- 161 source files
- 120+ test files, 3400+ tests
- 130+ documentation pages, 0 build warnings
- Apache 2.0 license

---

## 0.5.0 — 2025-10-18

### Added

- Cross-Agent Communication (in-process) with `cross_agent.py`.
- `CrossAgent`, `make_cross_agent_tools`, `ask_agent_<name>`, and `broadcast_to_agents`.

---

## 0.4.1 — 2025-10-17

### Fixed

- Fixed `TypeError` when falling back to `create_react_agent()` with `langgraph>=0.6`.

---

## 0.4.0

### Added

- CLI with pretty console output, `--trace/--no-trace`, and `--raw` modes.
- HTTP server specs with block string syntax.
- Tool tracing hooks integrated into agent layer.

---

## 0.3.0

### Added

- Improved JSON Schema → Pydantic mapping.
- PyPI Trusted Publishing workflow.

---

## 0.1.0

- Initial MCP client edition.
