# Changelog

All notable changes to Promptise Foundry are documented here.

---

## Unreleased

### Added

- **Engine: `verify` reasoning pattern** -- `build_agent(agent_pattern="verify")` runs a single-pass self-verifying reasoning node (plan -> solve -> self-check -> final answer) at **one-turn** latency. Validated on `benchmarks/reasoning`: it matches a plain prompt on models that already reason internally, and recovers errors a single pass would miss on weaker/mainstream models -- a good default when you want a self-checking answer without a multi-stage pipeline.
- **Engine: `PromptNode(context_scope="scoped")`** -- context-lifecycle management for multi-stage reasoning graphs. A scoped node sees only its own working set -- its system prompt (carrying any inherited/distilled state), the task, and its in-progress tool loop -- not the verbose messages produced by other stages, so token usage does not grow super-linearly across stages. Opt-in; `context_scope="full"` (the default) preserves existing behavior.
- **Benchmarks: `benchmarks/reasoning`** -- a from-scratch, truthful-and-fair reasoning benchmark (deterministic scoring, identical instrumentation for every contender) comparing Promptise reasoning patterns against LangGraph `create_react_agent`. Honest, mixed results are documented in `benchmarks/reasoning/RESULTS.md`.

### Fixed

- **Engine: routing-hint noise** -- a linear node (a single `default_next` with no real branch) no longer receives a "choose the next step" instruction. The hint could be echoed as the answer by weaker models; it now appears only at genuine branch points (two or more distinct targets).

---

## v0.6.1

### Fixed

- **Runtime: cross-task MCP client issue** -- `AgentProcess._build_agent()` now pre-discovers MCP tools before agent construction, resolving `CancelledError` when the MCP SDK's cancel scopes crossed asyncio task boundaries. Tools are pre-discovered and passed as `extra_tools` instead of relying on the session-bound MCP client.
- **Runtime: manifest server conversion** -- `manifest_to_process_config()` now converts plain server dicts from `.agent` manifests into `HTTPServerSpec`/`StdioServerSpec` objects, fixing `AttributeError: 'dict' object has no attribute 'transport'` when starting processes from manifest files.

### Added

- **Data Pipeline Monitoring Lab** (`examples/runtime/data_pipeline_lab/`) -- 8 production examples demonstrating process lifecycle, triggers, journals, AgentContext, ConversationBuffer, multi-process AgentRuntime, manifest loading, and open mode with meta-tools. All examples use real LLM calls.
- **AI Content Creation Studio** (`examples/prompts/content_studio_lab/`) -- 9 runnable demos covering prompt blocks, flows, guards, strategies, context providers, chain operators, registry, inspector, and templates. All demos use real LLM calls.

---

## v0.6.0

**Renamed package from `deepmcpagent` to `promptise`.**

### Added

- **Prompt Engineering framework** -- 2-layer system for composing production prompts
    - Layer 1: `PromptBlocks` -- composable blocks with priority-based assembly
    - Layer 2: `ConversationFlow` -- turn-aware prompt evolution across conversation phases
    - `PromptInspector` for full prompt tracing and debugging
- **Agent Runtime** -- lifecycle container for autonomous agent processes
    - `AgentProcess` with state machine (CREATED, RUNNING, SUSPENDED, STOPPED, FAILED)
    - Triggers: `CronTrigger`, `WebhookTrigger`, `FileWatchTrigger`, `EventTrigger`, `MessageTrigger`
    - `AgentContext` for unified state, environment, and file mounts
    - `Journal` and `ReplayEngine` for crash recovery
    - `.agent` YAML manifest format for declarative process definition
    - `AgentRuntime` multi-process manager with distributed coordination
    - CLI: `promptise runtime validate|start|logs|init`
- **MCP Server framework** -- build production MCP servers
    - `MCPServer` with `@server.tool()` decorator and Pydantic model validation
    - `MCPRouter` for grouping tools under shared prefixes and policies
    - `AuthMiddleware` with `JWTAuth` and role-based guards
    - `LoggingMiddleware`, `TimeoutMiddleware`, `ConcurrencyLimiter`
    - `BackgroundTasks` via dependency injection
    - `@cached` decorator with `InMemoryCache` backend
    - `ServerSettings` for typed, environment-backed configuration
    - Exception handlers for structured MCP error responses
    - Lifecycle hooks (`on_startup`, `on_shutdown`)
    - Live monitoring dashboard
    - `require_auth` option for fully authenticated servers
- **MCP Client library**
    - `MCPClient` for single-server connections with `fetch_token()` helper
    - `MCPMultiClient` for multi-server routing with automatic tool discovery
    - `MCPToolAdapter` for converting MCP tools to LangChain `BaseTool` instances
    - Tracing callbacks (`on_before`, `on_after`, `on_error`)
### Changed

- Package name: `deepmcpagent` renamed to `promptise`
- CLI command: `deepmcpagent` renamed to `promptise`
- All import paths: `from deepmcpagent` changed to `from promptise`

---

## v0.5.0

Initial release as `deepmcpagent`.

### Added

- Core agent building with `build_agent()`
- MCP integration with `HTTPServerSpec` and `StdioServerSpec`
- Cross-agent communication and delegation via `CrossAgent`
- SuperAgent `.superagent` YAML configuration format
- CLI: `deepmcpagent agent|validate|init|list-tools`
- Sandbox execution with `SandboxConfig`
