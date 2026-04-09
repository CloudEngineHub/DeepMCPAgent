# Changelog

All notable changes to Promptise Foundry are documented here.

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
