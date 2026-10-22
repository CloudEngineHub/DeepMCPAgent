---
seo_title: "Promptise Public API: Every Export"
description: "Every public class, function and constant Promptise Foundry exports, by module, with a one-line summary and the page that documents it."
---

# Public API Index

Every name in the `__all__` of `promptise` and its public subpackages in 1.6.0, with
a one-line summary and the page that explains it. Import a name from the module
it is listed under. Names that are also exported from the top-level `promptise`
package are listed there only. The [API Reference](../api/agent.md) pages
render the full docstrings.

## `promptise`

Top level. `from promptise import ...`

| Name | Kind | Summary | Documented in |
|---|---|---|---|
| `Action` | enum | Action to take when a finding is detected. | [Security Guardrails](../core/guardrails.md) |
| `AdaptiveLesson` | class | A stored lesson, as returned by `list_lessons`. | [Adaptive Strategy](../core/adaptive-strategy.md) |
| `AdaptiveStrategyConfig` | class | Configuration for the adaptive strategy system. | [Adaptive Strategy](../core/adaptive-strategy.md) |
| `AdaptiveStrategyManager` | class | Manages adaptive strategy learning for an agent. | [Adaptive Strategy](../core/adaptive-strategy.md) |
| `AgentContext` | class | Unified context layer for an agent process. | [Agent Context](../runtime/context.md) |
| `AgentEvent` | class | A structured notification event from the Promptise framework. | [Agent Events & Notifications](../core/events.md) |
| `AgentIdentity` | class | The identity of an agent: for tracing and attributing its actions. | [Agent Identity Architecture](../identity/architecture.md) |
| `AgentProcess` | class | Lifecycle container for a long-running agent process. | [Agent Processes](../runtime/processes.md) |
| `AgentRuntime` | class | Multi-process agent runtime manager. | [Runtime Manager](../runtime/runtime-manager.md) |
| `AgentSection` | class | Agent-level configuration section. | [SuperAgent Files](../core/agents/superagent-files.md) |
| `approval_elicitation_callback` | function | Build an MCP `elicitation_callback` that asks an approval handler. | [Human-in-the-Loop Approval](../core/approval.md) |
| `ApprovalDecision` | class | The decision on an approval request. | [Human-in-the-Loop Approval](../core/approval.md) |
| `ApprovalHandler` | class | Protocol for approval handlers. | [Human-in-the-Loop Approval](../core/approval.md) |
| `ApprovalPolicy` | class | Configuration for human-in-the-loop approval. | [Human-in-the-Loop Approval](../core/approval.md) |
| `ApprovalRequest` | class | A request for human approval of a tool call. | [Human-in-the-Loop Approval](../core/approval.md) |
| `ApprovalRule` | class | A single deny, ask or allow rule for the classifier. | [AutoApprovalClassifier](../core/approval-classifier.md) |
| `AutoApprovalClassifier` | class | Decision-hierarchy wrapper for approval handlers. | [AutoApprovalClassifier](../core/approval-classifier.md) |
| `BaseContext` | class | Extensible context container. | [Prompt Context System](../prompting/context.md) |
| `BaseNode` | class | Concrete base class for graph nodes. | [Reasoning Graph Nodes](../core/engine-nodes.md) |
| `BudgetConfig` | class | Autonomy budget configuration (unknown keys warn and are ignored). | [Runtime Configuration](../runtime/configuration.md) |
| `build_agent` | function | Build an MCP-first agent and return a `PromptiseAgent`. | [Building Agents](../core/agents/building-agents.md) |
| `build_superagent` | function | Build the agent in a `.superagent` file together with its whole team. | [SuperAgent Files](../core/agents/superagent-files.md) |
| `CacheStats` | class | Cache performance statistics. | [Semantic Cache](../core/cache.md) |
| `CallbackApprovalHandler` | class | Approval handler that delegates to an async Python callable. | [Human-in-the-Loop Approval](../core/approval.md) |
| `CallbackSink` | class | Deliver events to a Python callable. | [Agent Events & Notifications](../core/events.md) |
| `CallerContext` | class | Identity and metadata for the caller of an agent invocation. | [Building Agents](../core/agents/building-agents.md) |
| `check_model` | function | Diagnose a model string without calling anything. | [Models & Providers](../core/agents/models.md) |
| `ChromaProvider` | class | Adapter for `ChromaDB <https://www.trychroma.com/>`_. | [Agent Memory](../core/memory.md) |
| `Chunk` | class | A chunk of a document ready for embedding and retrieval. | [RAG (Retrieval-Augmented Generation)](../core/rag.md) |
| `Chunker` | class | Splits a document into chunks suitable for embedding. | [RAG (Retrieval-Augmented Generation)](../core/rag.md) |
| `ClassifierDecisionTrace` | class | Diagnostic record of which layer decided a request. | [Human-in-the-Loop Approval](../core/approval.md) |
| `ClassifierStats` | class | Cumulative counts of decisions by hierarchy layer. | [AutoApprovalClassifier](../core/approval-classifier.md) |
| `classify_failure` | function | Classify an error as infrastructure, strategy, or unknown. | [Adaptive Strategy](../core/adaptive-strategy.md) |
| `content_hash` | function | Return a short deterministic hash of a text string. | [RAG (Retrieval-Augmented Generation)](../core/rag.md) |
| `ContentSafetyDetector` | class | Detect harmful content across 13 safety categories. | [Security Guardrails](../core/guardrails.md) |
| `ContextEngine` | class | Unified context assembly with token budgeting. | [Context Engine](../core/context-engine.md) |
| `ConversationMessage` | class | A single conversation message. | [Conversation Persistence](../core/conversations.md) |
| `ConversationStore` | class | Interface for conversation persistence backends. | [Conversation Persistence](../core/conversations.md) |
| `CredentialCategory` | enum | Credential detection categories. Pass a set of these to `PromptiseSecurityScanner(enable_credentials={...})`. | [Security Guardrails](../core/guardrails.md) |
| `CredentialDetector` | class | Detect leaked credentials using 96 regex patterns from gitleaks/trufflehog. | [Security Guardrails](../core/guardrails.md) |
| `CrossAgent` | class | Metadata wrapper for a peer agent to be exposed as a tool. | [Cross-Agent Delegation](../core/agents/cross-agent.md) |
| `CrossAgentConfig` | class | Cross-agent reference configuration. | [SuperAgent Files](../core/agents/superagent-files.md) |
| `CustomRule` | class | A developer-defined regex detection rule. | [Security Guardrails](../core/guardrails.md) |
| `DEFAULT_DESTRUCTIVE_VERBS` | constant | tuple of 63 values | [AutoApprovalClassifier](../core/approval-classifier.md) |
| `DEFAULT_READ_ONLY_PREFIXES` | constant | tuple of 15 values | [AutoApprovalClassifier](../core/approval-classifier.md) |
| `DelegationError` | exception | A cross-agent call was refused before it reached the peer. | [Cross-Agent Delegation](../core/agents/cross-agent.md) |
| `DetailedModelConfig` | class | Detailed model configuration with provider-specific parameters. | [SuperAgent Files](../core/agents/superagent-files.md) |
| `Document` | class | A raw document loaded from a data source. | [RAG (Retrieval-Augmented Generation)](../core/rag.md) |
| `DocumentLoader` | class | Fetches raw documents from a data source. | [RAG (Retrieval-Augmented Generation)](../core/rag.md) |
| `DoneEvent` | class | The agent has finished processing. | [Streaming with Tool Visibility](../core/streaming.md) |
| `Edge` | class | A directed edge between two nodes. | [Reasoning Graph](../core/engine.md) |
| `Embedder` | class | Converts text into vector embeddings. | [RAG (Retrieval-Augmented Generation)](../core/rag.md) |
| `EmbeddingProvider` | class | Protocol for embedding providers. | [Semantic Cache](../core/cache.md) |
| `EnvVarNotFoundError` | exception | Raised when a required environment variable is not found. | [Configuration Exceptions](../core/exceptions.md) |
| `ErrorEvent` | class | An error occurred during processing. | [Streaming with Tool Visibility](../core/streaming.md) |
| `EventBusSink` | class | Bridge events to the runtime's EventBus for inter-process notifications. | [Agent Events & Notifications](../core/events.md) |
| `EventNotifier` | class | Central event coordinator that routes events to configured sinks. | [Agent Events & Notifications](../core/events.md) |
| `EventSink` | class | Protocol for event notification sinks. | [Agent Events & Notifications](../core/events.md) |
| `ExportFormat` | enum | Available backends for receiving observability events. | [Observability API Reference](../api/observability.md) |
| `FailureCategory` | enum | Classification of a tool failure. | [Adaptive Strategy](../core/adaptive-strategy.md) |
| `FailureLog` | class | A single tool failure record. | [Adaptive Strategy](../core/adaptive-strategy.md) |
| `FallbackChain` | class | Chain of LLM models with automatic failover. | [Model Fallback](../core/fallback.md) |
| `generate_session_id` | function | Generate a cryptographically secure session identifier. | [Conversation Persistence](../core/conversations.md) |
| `get_current_caller` | function | Return the `CallerContext` for the current invocation. | [Building Agents](../core/agents/building-agents.md) |
| `GraphState` | class | The complete state of a graph execution. | [Reasoning Graph](../core/engine.md) |
| `GuardrailViolation` | exception | Raised when input or output is blocked by guardrails. | [Security Guardrails](../core/guardrails.md) |
| `HealthConfig` | class | Behavioral health monitoring configuration. | [Runtime Configuration](../runtime/configuration.md) |
| `HTTPServerConfig` | class | HTTP/SSE MCP server configuration. | [SuperAgent Files](../core/agents/superagent-files.md) |
| `HTTPServerSpec` | class | Specification for a remote MCP server reachable via HTTP/SSE. | [MCP Server Configuration for Agents](../core/agents/server-specs.md) |
| `IdentityError` | exception | Base exception for every Agent Identity failure. | [Agent Identity Security](../identity/security.md) |
| `IndexReport` | class | Summary of an `index` operation. | [RAG (Retrieval-Augmented Generation)](../core/rag.md) |
| `InjectionDetector` | class | Detect prompt injection attacks using a local DeBERTa model. | [Security Guardrails](../core/guardrails.md) |
| `InMemoryConversationStore` | class | Dict-backed conversation store for testing and prototyping. | [Conversation Persistence](../core/conversations.md) |
| `InMemoryProvider` | class | Keyword-search memory provider for testing. | [Agent Memory](../core/memory.md) |
| `InMemoryVectorStore` | class | Reference vector store that keeps everything in memory. | [RAG (Retrieval-Augmented Generation)](../core/rag.md) |
| `load_superagent_file` | function | Convenience function to load a .superagent file with all resolutions. | [SuperAgent Files](../core/agents/superagent-files.md) |
| `LocalEmbeddingProvider` | class | Local embedding via sentence-transformers. | [Semantic Cache](../core/cache.md) |
| `LogSink` | class | Deliver events to Python's logging system. | [Agent Events & Notifications](../core/events.md) |
| `MCPClient` | class | Production-grade MCP client for a single server. | [MCP Client](../mcp/client/index.md) |
| `MCPClientError` | exception | Raised when an MCP client operation fails. | [MCP Client](../mcp/client/index.md) |
| `MCPConnectionRejectedError` | exception | Raised when an HTTP MCP server rejects the session with a 4xx status. | [MCP Client](../mcp/client/index.md) |
| `MCPCredentialError` | exception | Raised when `bearer_token_provider` cannot supply a credential. | [MCP Client](../mcp/client/index.md) |
| `MCPMultiClient` | class | Connect to multiple MCP servers and aggregate their tools. | [MCP Client](../mcp/client/index.md) |
| `MCPToolAdapter` | class | Discover MCP tools and convert them to LangChain `BaseTool` instances. | [MCP Tool Adapter](../mcp/client/tool-adapter.md) |
| `Mem0Provider` | class | Adapter for `mem0 <https://github.com/mem0ai/mem0>`_. | [Agent Memory](../core/memory.md) |
| `MemoryAgent` | class | Wraps an agent with automatic memory context injection. | [Agent Memory](../core/memory.md) |
| `MemoryProvider` | class | Interface for memory backends. | [Agent Memory](../core/memory.md) |
| `MemoryResult` | class | A single memory search result. | [Agent Memory](../core/memory.md) |
| `MemorySection` | class | Memory configuration section for .superagent files. | [SuperAgent Files](../core/agents/superagent-files.md) |
| `MissionConfig` | class | Mission-oriented process configuration. | [Runtime Configuration](../runtime/configuration.md) |
| `MissionState` | enum | Mission lifecycle states. | [Mission-Oriented Process Model](../runtime/governance/mission.md) |
| `Model` | class | Provider, model and credentials **in code**: the LangChain shape. | [Models & Providers](../core/agents/models.md) |
| `ModelConfig` | constant | Represent a PEP 604 union type | [SuperAgent Files](../core/agents/superagent-files.md) |
| `ModelSetupError` | exception | A model cannot be used yet: the message says exactly what to do. | [Models & Providers](../core/agents/models.md) |
| `NERDetector` | class | Detect unstructured PII using GLiNER zero-shot NER model. | [Security Guardrails](../core/guardrails.md) |
| `NodeResult` | class | Everything that happened during a single node execution. | [Reasoning Graph](../core/engine.md) |
| `ObservabilityConfig` | class | Configuration for the observability system. | [Agent Observability](../core/observability.md) |
| `ObserveLevel` | enum | Controls how much detail the observability system captures. | [Agent Observability](../core/observability.md) |
| `OpenAIEmbeddingProvider` | class | Embedding via OpenAI or Azure OpenAI API. | [Semantic Cache](../core/cache.md) |
| `OptimizationLevel` | enum | Preset optimization levels. | [Tool Optimization](../core/tool-optimization.md) |
| `PIICategory` | enum | PII detection categories. Pass a set of these to `PromptiseSecurityScanner(enable_pii={...})` to enable only specific PII types. | [Security Guardrails](../core/guardrails.md) |
| `PIIDetector` | class | Detect PII using regex patterns with Luhn validation for credit cards. | [Security Guardrails](../core/guardrails.md) |
| `PostgresConversationStore` | class | PostgreSQL-backed conversation store using `asyncpg`. | [Conversation Persistence](../core/conversations.md) |
| `ProcessConfig` | class | Configuration for a single agent process. | [Runtime Configuration](../runtime/configuration.md) |
| `ProcessState` | enum | Agent process lifecycle states. | [Process Lifecycle](../runtime/lifecycle.md) |
| `prompt` | function | Decorator that turns a function into a `Prompt`. | [Prompt Engineering Overview](../prompting/index.md) |
| `Prompt` | class | Dual-mode prompt: standalone LLM caller + agent instruction source. | [Prompt Engineering Overview](../prompting/index.md) |
| `PromptBuilder` | class | Fluent builder for constructing `Prompt` instances at runtime. | [PromptBuilder](../prompting/builder.md) |
| `PromptContext` | class | The agent's complete world during prompt execution. | [Prompt Context System](../prompting/context.md) |
| `PromptGraph` | class | A directed graph of reasoning nodes. | [Reasoning Graph](../core/engine.md) |
| `PromptGraphEngine` | class | Adaptive graph traversal engine. | [Reasoning Graph](../core/engine.md) |
| `PromptiseAgent` | class | The unified Promptise agent. | [Building Agents](../core/agents/building-agents.md) |
| `PromptiseCallbackHandler` | class | LangChain callback handler → ObservabilityCollector bridge. | [Callback Handler](../core/callback-handler.md) |
| `PromptiseSecurityScanner` | class | Unified security scanner for agent input and output. | [Security Guardrails](../core/guardrails.md) |
| `PromptNode` | class | A complete reasoning unit in the graph. | [Reasoning Graph Nodes](../core/engine-nodes.md) |
| `PromptSuite` | class | Group of prompts sharing world configuration. | [Prompt Suite & Registry](../prompting/suite-registry.md) |
| `PROVIDERS` | constant | tuple of 19 values | [Models & Providers](../core/agents/models.md) |
| `QueueApprovalHandler` | class | Approval handler using async queues for in-process UIs. | [Human-in-the-Loop Approval](../core/approval.md) |
| `rag_to_tool` | function | Wrap a `RAGPipeline` as a LangChain tool the agent can call. | [RAG (Retrieval-Augmented Generation)](../core/rag.md) |
| `RAGPipeline` | class | Orchestrates the full RAG flow: load → chunk → embed → store → retrieve. | [RAG (Retrieval-Augmented Generation)](../core/rag.md) |
| `RecursiveTextChunker` | class | Split text into fixed-size chunks with overlap. | [RAG (Retrieval-Augmented Generation)](../core/rag.md) |
| `RedisConversationStore` | class | Redis-backed conversation store using `redis.asyncio`. | [Conversation Persistence](../core/conversations.md) |
| `resolve_model` | function | Turn a `"provider:model"` string (plus optional words) into a chat model. | [Models & Providers](../core/agents/models.md) |
| `RetrievalResult` | class | A single hit returned by `search`. | [RAG (Retrieval-Augmented Generation)](../core/rag.md) |
| `RuntimeConfig` | class | Top-level runtime configuration (composite). | [Runtime Configuration](../runtime/configuration.md) |
| `SandboxConfigSection` | class | Sandbox configuration section for .superagent files. | [SuperAgent Files](../core/agents/superagent-files.md) |
| `sanitize_memory_content` | function | Sanitize memory content before injection into agent messages. | [Agent Memory](../core/memory.md) |
| `ScanReport` | class | Complete scan result with all findings and metadata. | [Security Guardrails](../core/guardrails.md) |
| `SecretScopeConfig` | class | Per-process secret scoping configuration. | [Runtime Configuration](../runtime/configuration.md) |
| `SecurityFinding` | class | A single detection result from a scanner. | [Security Guardrails](../core/guardrails.md) |
| `SemanticCache` | class | Semantic cache for agent responses. | [Semantic Cache](../core/cache.md) |
| `ServerConfig` | callable | Runtime representation of an annotated type. | [SuperAgent Files](../core/agents/superagent-files.md) |
| `servers_to_mcp_config` | function | Convert programmatic server specs to a configuration dict. | [Config & Server Specs](../core/config.md) |
| `ServerSpec` | constant | Represent a PEP 604 union type | [Config & Server Specs](../core/config.md) |
| `SessionAccessDenied` | exception | Raised when a user attempts to access a session they do not own. | [Conversation Persistence](../core/conversations.md) |
| `SessionInfo` | class | Metadata about a conversation session. | [Conversation Persistence](../core/conversations.md) |
| `Severity` | enum | Severity level of a security finding. | [Security Guardrails](../core/guardrails.md) |
| `SQLiteConversationStore` | class | SQLite-backed conversation store using `aiosqlite`. | [Conversation Persistence](../core/conversations.md) |
| `StdioServerConfig` | class | Stdio (local process) MCP server configuration. | [SuperAgent Files](../core/agents/superagent-files.md) |
| `StdioServerSpec` | class | Specification for a local MCP server launched via stdio. | [MCP Server Configuration for Agents](../core/agents/server-specs.md) |
| `StreamEvent` | class | Base event yielded by `astream_with_tools()`. | [Streaming with Tool Visibility](../core/streaming.md) |
| `SuperAgentConfig` | class | Processed configuration ready for agent building. | [SuperAgent Files](../core/agents/superagent-files.md) |
| `SuperAgentError` | exception | Base exception for all SuperAgent-related errors. | [Configuration Exceptions](../core/exceptions.md) |
| `SuperAgentLoader` | class | Load and parse .superagent configuration files. | [SuperAgent Files](../core/agents/superagent-files.md) |
| `SuperAgentSchema` | class | Root schema for .superagent YAML files. | [SuperAgent Files](../core/agents/superagent-files.md) |
| `SuperAgentValidationError` | exception | Raised when .superagent schema validation fails. | [Configuration Exceptions](../core/exceptions.md) |
| `TokenEvent` | class | An LLM token has been generated. | [Streaming with Tool Visibility](../core/streaming.md) |
| `tool` | function | Make a Python function a tool an agent can call. | [Tools](../core/tools.md) |
| `ToolEndEvent` | class | A tool has finished executing. | [Streaming with Tool Visibility](../core/streaming.md) |
| `ToolError` | exception | Error raised inside a tool handler. | [MCP Server Resilience Patterns](../mcp/server/resilience-patterns.md) |
| `ToolInfo` | class | Human-friendly metadata for a discovered MCP tool. | [Tools](../core/tools.md) |
| `ToolOptimizationConfig` | class | Configuration for MCP tool token optimization. | [Tool Optimization](../core/tool-optimization.md) |
| `ToolStartEvent` | class | A tool has started executing. | [Streaming with Tool Visibility](../core/streaming.md) |
| `TransporterType` | enum | Available backends for receiving observability events. | [Agent Observability](../core/observability.md) |
| `VectorStore` | class | Persists embedded chunks and performs similarity search. | [RAG (Retrieval-Augmented Generation)](../core/rag.md) |
| `verify_event_signature` | function | Check the `X-Promptise-Signature` header of a webhook delivery. | [Agent Events & Notifications](../core/events.md) |
| `verify_webhook_signature` | function | Check the `X-Promptise-Signature` of a webhook approval request. | [Human-in-the-Loop Approval](../core/approval.md) |
| `WebhookApprovalHandler` | class | Approval handler that POSTs to a webhook URL and polls for a decision. | [Human-in-the-Loop Approval](../core/approval.md) |
| `WebhookSink` | class | Deliver events via HTTP POST to a webhook URL. | [Agent Events & Notifications](../core/events.md) |

## `promptise.engine`

Reasoning graph. `from promptise.engine import ...`

| Name | Kind | Summary | Documented in |
|---|---|---|---|
| `AutonomousNode` | class | Meta-node: the LLM receives a pool of configured nodes and dynamically composes its own reasoning path at runtime. | [Reasoning Graph Nodes](../core/engine-nodes.md) |
| `BudgetHook` | class | Enforces a token and/or cost budget for each graph run. | [Graph Hooks & Observability](../core/engine-hooks.md) |
| `CodeActionBudgetError` | exception | A code-action run could not do the whole job within `max_tool_calls`. | [Reasoning Graph](../core/engine.md) |
| `CodeActionConfig` | class | Tuning knobs for the `code-action` pattern. | [Code-Action: one program beats a 20-call tool loop](../guides/code-action.md) |
| `CodeActionNode` | class | A node that solves a task by writing and running one sandboxed program. | [Reasoning Graph Nodes](../core/engine-nodes.md) |
| `ContextCompaction` | class | Settings for compacting a long tool loop. | [Context Lifecycle Management](../guides/context-lifecycle.md) |
| `CritiqueNode` | class | Challenges the current answer/plan with counter-arguments. | [Reasoning Graph Nodes](../core/engine-nodes.md) |
| `CycleDetectionHook` | class | Detect infinite loops by tracking visit patterns. | [Graph Hooks & Observability](../core/engine-hooks.md) |
| `EdgeCondition` | class | A built-in edge condition, described by data instead of a lambda. | [Graph Serialization & YAML](../core/engine-serialization.md) |
| `ExecutionReport` | class | Summary of a complete graph execution. | [Reasoning Graph](../core/engine.md) |
| `FanOutNode` | class | Sends different sub-questions to parallel child nodes. | [Reasoning Graph Nodes](../core/engine-nodes.md) |
| `GraphBudgetExceededError` | exception | A `BudgetHook` stopped a run over its token or cost budget before the model gave an answer. | [Reasoning Graph](../core/engine.md) |
| `GraphExecutionError` | exception | A graph run ended on a node failure the graph did not recover from. | [Reasoning Graph](../core/engine.md) |
| `GraphIterationLimitError` | exception | A run hit an iteration limit in the middle of a tool loop. | [Reasoning Graph](../core/engine.md) |
| `GraphMutation` | class | A single graph modification requested during execution. | [Reasoning Graph](../core/engine.md) |
| `GraphStoppedError` | exception | A hook ended a run in the middle of a tool loop. | [Reasoning Graph](../core/engine.md) |
| `GuardNode` | class | Validate state against guards and route based on pass/fail. | [Reasoning Graph Nodes](../core/engine-nodes.md) |
| `Hook` | class | Protocol for engine hooks. | [Graph Hooks & Observability](../core/engine-hooks.md) |
| `HumanNode` | class | Pause execution and wait for human input. | [Reasoning Graph Nodes](../core/engine-nodes.md) |
| `JustifyNode` | class | Forces explicit justification of the last decision/action. | [Reasoning Graph Nodes](../core/engine-nodes.md) |
| `LoggingHook` | class | Log every node execution with timing and token counts. | [Graph Hooks & Observability](../core/engine-hooks.md) |
| `LoopNode` | class | Repeat a subgraph until a condition is met. | [Reasoning Graph Nodes](../core/engine-nodes.md) |
| `MetricsHook` | class | Collects per-node metrics: tokens, latency, errors, call counts. | [Graph Hooks & Observability](../core/engine-hooks.md) |
| `node` | function | Decorator that turns an async function into a graph node. | [Reasoning Graph Nodes](../core/engine-nodes.md) |
| `NodeEvent` | class | An event emitted during streaming execution. | [Reasoning Graph](../core/engine.md) |
| `NodeFlag` | enum | Typed flags that declare a node's role and capabilities. | [Reasoning Graph Nodes](../core/engine-nodes.md) |
| `NodeProtocol` | class | Minimal protocol that every graph node must satisfy. | [Reasoning Graph Nodes](../core/engine-nodes.md) |
| `ObserveNode` | class | Processes raw tool results into structured, actionable data. | [Reasoning Graph Nodes](../core/engine-nodes.md) |
| `ParallelNode` | class | Run multiple child nodes concurrently and merge results. | [Reasoning Graph Nodes](../core/engine-nodes.md) |
| `parse_frontmatter` | function | Extract a YAML-ish frontmatter dict from a Python source file. | [Path-Scoped Skill Activation](../core/skill-registry.md) |
| `PlanNode` | class | Creates structured plans with self-evaluation and subgoal tracking. | [Reasoning Graph Nodes](../core/engine-nodes.md) |
| `ReflectNode` | class | Reviews what happened, identifies mistakes, generates corrections. | [Reasoning Graph Nodes](../core/engine-nodes.md) |
| `RetryNode` | class | Wraps another node with retry logic and error enrichment. | [Reasoning Graph Nodes](../core/engine-nodes.md) |
| `RouterNode` | class | Lightweight LLM call that decides which path to take. | [Reasoning Graph Nodes](../core/engine-nodes.md) |
| `Skill` | class | A registered skill with optional path-scoped activation. | [Path-Scoped Skill Activation](../core/skill-registry.md) |
| `SkillRegistry` | class | In-process registry of `Skill` objects. | [Path-Scoped Skill Activation](../core/skill-registry.md) |
| `SubgraphNode` | class | Embed a complete sub-graph as a single node. | [Reasoning Graph Nodes](../core/engine-nodes.md) |
| `SynthesizeNode` | class | Combines all gathered data into a comprehensive final answer. | [Reasoning Graph Nodes](../core/engine-nodes.md) |
| `ThinkNode` | class | Analyzes current state, identifies gaps, recommends next step. | [Reasoning Graph Nodes](../core/engine-nodes.md) |
| `TimingHook` | class | Enforce per-node time budgets. | [Graph Hooks & Observability](../core/engine-hooks.md) |
| `ToolNode` | class | Execute a specific tool or let the engine pick from available tools. | [Reasoning Graph Nodes](../core/engine-nodes.md) |
| `TransformNode` | class | Transform state data without calling the LLM. | [Reasoning Graph Nodes](../core/engine-nodes.md) |
| `ValidateNode` | class | LLM evaluates whether output meets specified criteria. | [Reasoning Graph Nodes](../core/engine-nodes.md) |

## `promptise.mcp.server`

MCP server SDK. `from promptise.mcp.server import ...`

| Name | Kind | Summary | Documented in |
|---|---|---|---|
| `APIKeyAuth` | class | API key-based authentication provider. | [MCP Server Authentication & Security](../mcp/server/auth-security.md) |
| `ApprovalDeniedError` | exception | Raised when a server-side approval gate denies (or times out on) a call. | [Approval Gates (Server-Side HITL)](../mcp/server/approval-gates.md) |
| `ApprovalGateMiddleware` | class | Enforces human approval on tools declared with `requires_approval=True`. | [Approval Gates (Server-Side HITL)](../mcp/server/approval-gates.md) |
| `AsymmetricJWTAuth` | class | JWT authentication using asymmetric algorithms (RS256, ES256). | [MCP Server Authentication & Security](../mcp/server/auth-security.md) |
| `AuditIssue` | class | One finding of `verify_audit_log`. | [MCP Server Observability & Monitoring](../mcp/server/observability.md) |
| `AuditMiddleware` | class | HMAC-chained audit log middleware. | [MCP Server Observability & Monitoring](../mcp/server/observability.md) |
| `AuditVerification` | class | Result of `verify_audit_log`. | [MCP Server Observability & Monitoring](../mcp/server/observability.md) |
| `AuthenticationError` | exception | Raised when authentication fails. | [MCP Server Authentication & Security](../mcp/server/auth-security.md) |
| `AuthMiddleware` | class | Middleware that enforces authentication on tools with `auth=True`. | [MCP Server Authentication & Security](../mcp/server/auth-security.md) |
| `AuthProvider` | class | Protocol for authentication providers. | [MCP Server Authentication & Security](../mcp/server/auth-security.md) |
| `BackgroundTasks` | class | Collect and execute background tasks after the handler returns. | [MCP Server Resilience Patterns](../mcp/server/resilience-patterns.md) |
| `build_manifest` | function | Build a JSON-serialisable manifest from a server's registrations. | [Advanced MCP Server Patterns](../mcp/server/advanced-patterns.md) |
| `build_serve_parser` | function | Build the `serve` subcommand parser. | [MCP Server Deployment](../mcp/server/deployment.md) |
| `CacheBackend` | class | Protocol for cache backends. | [MCP Server Caching & Performance](../mcp/server/caching-performance.md) |
| `cached` | function | Decorator that caches tool handler results. | [MCP Server Caching & Performance](../mcp/server/caching-performance.md) |
| `CacheMiddleware` | class | Server-wide caching middleware. | [MCP Server Caching & Performance](../mcp/server/caching-performance.md) |
| `CacheScope` | callable |  | [MCP Server Caching & Performance](../mcp/server/caching-performance.md) |
| `CancellationToken` | class | Token for checking and responding to client cancellation requests. | [MCP Server Resilience Patterns](../mcp/server/resilience-patterns.md) |
| `CancelledError` | exception | Raised when a tool call is cancelled by the client. | [MCP Server Resilience Patterns](../mcp/server/resilience-patterns.md) |
| `CircuitBreakerMiddleware` | class | Circuit breaker middleware. | [MCP Server Resilience Patterns](../mcp/server/resilience-patterns.md) |
| `CircuitOpenError` | exception | Raised when a tool call is rejected by an open circuit. | [MCP Server Resilience Patterns](../mcp/server/resilience-patterns.md) |
| `CircuitState` | enum | Circuit breaker states. | [MCP Server Resilience Patterns](../mcp/server/resilience-patterns.md) |
| `clear_request_headers` | function | Clear HTTP request headers after the request completes. | [MCP Server Routers & Middleware](../mcp/server/routers-middleware.md) |
| `ClientContext` | class | Structured information about the authenticated client. | [MCP Server Authentication & Security](../mcp/server/auth-security.md) |
| `ConcurrencyLimiter` | class | Middleware that limits concurrent in-flight tool executions. | [MCP Server Caching & Performance](../mcp/server/caching-performance.md) |
| `ConcurrencyLimitError` | exception | Raised when a tool (or the server) is already running its maximum number of concurrent calls. | [MCP Server Caching & Performance](../mcp/server/caching-performance.md) |
| `Content` | constant | Represent a PEP 604 union type | [Security Guardrails](../core/guardrails.md) |
| `CORSConfig` | class | CORS configuration for HTTP and SSE transports. | [MCP Server Deployment](../mcp/server/deployment.md) |
| `Dashboard` | class | Full-screen tabbed terminal dashboard with keyboard navigation. | [MCP Server Observability & Monitoring](../mcp/server/observability.md) |
| `DashboardMiddleware` | class | Outermost middleware that records request metrics for the dashboard. | [MCP Server Observability & Monitoring](../mcp/server/observability.md) |
| `DashboardState` | class | Thread-safe shared state between middleware and dashboard renderer. | [MCP Server Observability & Monitoring](../mcp/server/observability.md) |
| `DeclaredRateLimitMiddleware` | class | Enforces per-tool rate limits declared via `@server.tool(rate_limit=...)`. | [MCP Server API Reference](../api/mcp-server.md) |
| `Depends` | function | Mark a parameter for dependency injection. | [MCP Server Fundamentals](../mcp/server/building-servers.md) |
| `dereference_schema` | function | Inline all `$ref` / `$defs` in a JSON Schema. | [MCP Server Fundamentals](../mcp/server/building-servers.md) |
| `ElicitationApprover` | class | Approves by asking the human behind the *calling* MCP client. | [Approval Gates (Server-Side HITL)](../mcp/server/approval-gates.md) |
| `Elicitor` | class | Request structured input from the user mid-execution. | [Advanced MCP Server Patterns](../mcp/server/advanced-patterns.md) |
| `ExceptionHandlerRegistry` | class | Registry mapping exception types to handler functions. | [MCP Server Resilience Patterns](../mcp/server/resilience-patterns.md) |
| `get_context` | function | Return the current request context. | [MCP Server Routers & Middleware](../mcp/server/routers-middleware.md) |
| `get_request_headers` | function | Return HTTP request headers for the current async context. | [MCP Server Routers & Middleware](../mcp/server/routers-middleware.md) |
| `Guard` | class | Protocol for tool access guards. | [MCP Server Authentication & Security](../mcp/server/auth-security.md) |
| `HasAllRoles` | class | Guard that requires **all** of the given roles. | [MCP Server Authentication & Security](../mcp/server/auth-security.md) |
| `HasAllScopes` | class | Guard that requires **all** of the given OAuth2 scopes. | [MCP Server Authentication & Security](../mcp/server/auth-security.md) |
| `HasRole` | class | Guard that requires **any** of the given roles. | [MCP Server Authentication & Security](../mcp/server/auth-security.md) |
| `HasScope` | class | Guard that requires **any** of the given OAuth2 scopes. | [MCP Server Authentication & Security](../mcp/server/auth-security.md) |
| `HasTenant` | class | Guard that requires the client to belong to one of the given tenants. | [MCP Server Authentication & Security](../mcp/server/auth-security.md) |
| `HealthCheck` | class | Health and readiness probe manager. | [MCP Server Resilience Patterns](../mcp/server/resilience-patterns.md) |
| `hot_reload` | function | Run an MCP server with automatic restart on file changes. | [Advanced MCP Server Patterns](../mcp/server/advanced-patterns.md) |
| `ImageContent` | class | Helper for returning image content from tool handlers. | [MCP Server Fundamentals](../mcp/server/building-servers.md) |
| `InMemoryCache` | class | In-process cache with TTL-based expiry and background cleanup. | [MCP Server Caching & Performance](../mcp/server/caching-performance.md) |
| `InMemoryQueueBackend` | class | In-process queue backend using asyncio primitives. | [MCP Server Job Queue & Background Jobs](../mcp/server/queue.md) |
| `is_upstream_failure` | function | Default failure classifier for `CircuitBreakerMiddleware`. | [MCP Server Resilience Patterns](../mcp/server/resilience-patterns.md) |
| `JwksAuth` | class | JWT authentication against an identity provider's JWKS endpoint. | [MCP Server Authentication & Security](../mcp/server/auth-security.md) |
| `JWTAuth` | class | JWT-based authentication provider. | [MCP Server Authentication & Security](../mcp/server/auth-security.md) |
| `LoggingMiddleware` | class | Log every tool call with timing. | [MCP Server Routers & Middleware](../mcp/server/routers-middleware.md) |
| `MCPError` | exception | Base class for structured MCP server errors. | [MCP Server Fundamentals](../mcp/server/building-servers.md) |
| `MCPQueue` | class | MCP-native job queue for asynchronous background work. | [MCP Server Job Queue & Background Jobs](../mcp/server/queue.md) |
| `MCPRouter` | class | Modular grouping for tools, resources, and prompts. | [MCP Server Routers & Middleware](../mcp/server/routers-middleware.md) |
| `MCPServer` | class | Production-grade MCP server with decorator-based tool registration. | [MCP Server Fundamentals](../mcp/server/building-servers.md) |
| `MetricsCollector` | class | Collects per-tool metrics: call counts, latency, errors. | [MCP Server Observability & Monitoring](../mcp/server/observability.md) |
| `MetricsMiddleware` | class | Middleware that records per-tool call metrics. | [MCP Server Observability & Monitoring](../mcp/server/observability.md) |
| `Middleware` | class | Protocol for middleware callables. | [MCP Server Routers & Middleware](../mcp/server/routers-middleware.md) |
| `MiddlewareChain` | class | Executes a stack of middleware around a handler. | [MCP Server Routers & Middleware](../mcp/server/routers-middleware.md) |
| `mount` | function | Mount a child server's tools, resources, and prompts into a parent. | [Advanced MCP Server Patterns](../mcp/server/advanced-patterns.md) |
| `NamespaceTransform` | class | Prefix all tool names with a namespace. | [Advanced MCP Server Patterns](../mcp/server/advanced-patterns.md) |
| `OnAuthenticateHook` | callable |  | [MCP Server Authentication & Security](../mcp/server/auth-security.md) |
| `OpenAPIProvider` | class | Generate MCP tools from an OpenAPI specification. | [Advanced MCP Server Patterns](../mcp/server/advanced-patterns.md) |
| `OTelMiddleware` | class | OpenTelemetry tracing middleware. | [MCP Server Observability & Monitoring](../mcp/server/observability.md) |
| `parse_rate_limit` | function | Parse a declared rate-limit string like `"100/min"`. | [MCP Server API Reference](../api/mcp-server.md) |
| `PendingApprover` | class | Blocks gated calls in a pending store until a human decides. | [Approval Gates (Server-Side HITL)](../mcp/server/approval-gates.md) |
| `PerToolConcurrencyLimiter` | class | Middleware that enforces per-tool concurrency limits. | [MCP Server Caching & Performance](../mcp/server/caching-performance.md) |
| `ProgressReporter` | class | Report progress during long-running tool calls. | [MCP Server Resilience Patterns](../mcp/server/resilience-patterns.md) |
| `PrometheusMiddleware` | class | Prometheus metrics middleware for MCP tool calls. | [MCP Server Observability & Monitoring](../mcp/server/observability.md) |
| `PromptDef` | class | Internal definition of a registered prompt. | [MCP Server API Reference](../api/mcp-server.md) |
| `PromptError` | exception | Error raised inside a prompt handler. | [MCP Server API Reference](../api/mcp-server.md) |
| `QueueBackend` | class | Protocol for pluggable queue storage backends. | [MCP Server Job Queue & Background Jobs](../mcp/server/queue.md) |
| `QueueCaller` | class | Identity a queue operation is performed for. | [MCP Server Job Queue & Background Jobs](../mcp/server/queue.md) |
| `RateLimitError` | exception | Raised when rate limit is exceeded. | [MCP Server Caching & Performance](../mcp/server/caching-performance.md) |
| `RateLimitMiddleware` | class | Middleware that enforces rate limits. | [MCP Server Caching & Performance](../mcp/server/caching-performance.md) |
| `RedisCache` | class | Redis-backed cache implementing the `CacheBackend` protocol. | [MCP Server Caching & Performance](../mcp/server/caching-performance.md) |
| `register_batch_tool` | function | Register a batch meta-tool on the server. | [Advanced MCP Server Patterns](../mcp/server/advanced-patterns.md) |
| `register_manifest` | function | Register a `docs://manifest` resource on the server. | [Advanced MCP Server Patterns](../mcp/server/advanced-patterns.md) |
| `RequestContext` | class | Per-request context available to handlers and middleware. | [MCP Server Fundamentals](../mcp/server/building-servers.md) |
| `RequireAuth` | class | Guard that requires an authenticated `client_id` on the context. | [MCP Server Authentication & Security](../mcp/server/auth-security.md) |
| `RequireClientId` | class | Guard that requires a specific `client_id`. | [MCP Server Authentication & Security](../mcp/server/auth-security.md) |
| `RequireTenant` | class | Guard that requires the client to belong to *some* tenant. | [MCP Server Authentication & Security](../mcp/server/auth-security.md) |
| `resolve_server` | function | Import and return the server instance from a target string. | [MCP Server Deployment](../mcp/server/deployment.md) |
| `ResourceDef` | class | Internal definition of a registered resource or resource template. | [MCP Server API Reference](../api/mcp-server.md) |
| `ResourceError` | exception | Error raised inside a resource handler. | [MCP Server API Reference](../api/mcp-server.md) |
| `run_serve` | function | Execute the `serve` command. | [MCP Server Deployment](../mcp/server/deployment.md) |
| `Sampler` | class | Request LLM completions from the client. | [Advanced MCP Server Patterns](../mcp/server/advanced-patterns.md) |
| `ServerLogger` | class | Send log messages from the server to the connected MCP client. | [MCP Server Observability & Monitoring](../mcp/server/observability.md) |
| `ServerSettings` | class | Base server settings with sensible defaults. | [MCP Server Fundamentals](../mcp/server/building-servers.md) |
| `SessionManager` | class | Manages per-session state stores. | [Advanced MCP Server Patterns](../mcp/server/advanced-patterns.md) |
| `SessionState` | class | Key-value state scoped to the current MCP session. | [Advanced MCP Server Patterns](../mcp/server/advanced-patterns.md) |
| `set_request_headers` | function | Store HTTP request headers for the current async context. | [MCP Server Routers & Middleware](../mcp/server/routers-middleware.md) |
| `StreamingResult` | class | Collects partial results for streaming-style tool responses. | [Advanced MCP Server Patterns](../mcp/server/advanced-patterns.md) |
| `StructuredLoggingMiddleware` | class | Emit structured JSON log entries for every tool call. | [MCP Server Observability & Monitoring](../mcp/server/observability.md) |
| `TagFilterTransform` | class | Only expose tools that have at least one of the required tags. | [Advanced MCP Server Patterns](../mcp/server/advanced-patterns.md) |
| `TestClient` | class |  | [MCP Server Testing](../mcp/server/testing.md) |
| `TimeoutMiddleware` | class | Enforce per-call timeout. | [MCP Server Routers & Middleware](../mcp/server/routers-middleware.md) |
| `TokenBucketLimiter` | class | Token bucket rate limiter. | [MCP Server Caching & Performance](../mcp/server/caching-performance.md) |
| `TokenEndpointConfig` | class | Configuration for the built-in OAuth 2.0 token issuer. | [MCP Server Authentication & Security](../mcp/server/auth-security.md) |
| `ToolAnnotations` | class | MCP tool annotations describing tool behaviour hints. | [MCP Server Fundamentals](../mcp/server/building-servers.md) |
| `ToolDef` | class | Internal definition of a registered tool. | [MCP Server Fundamentals](../mcp/server/building-servers.md) |
| `ToolResponse` | class | Response wrapper that lets handlers return content with metadata. | [MCP Server Fundamentals](../mcp/server/building-servers.md) |
| `ToolTransform` | class | Protocol for tool transforms. | [Advanced MCP Server Patterns](../mcp/server/advanced-patterns.md) |
| `TransportType` | enum | Supported MCP transport protocols. | [MCP Server Deployment](../mcp/server/deployment.md) |
| `ValidationError` | exception | Raised when input validation fails. | [Code Sandbox for Agents](../core/sandbox.md) |
| `verify_audit_log` | function | Verify the HMAC chain of an audit log file written by `AuditMiddleware`. | [MCP Server Observability & Monitoring](../mcp/server/observability.md) |
| `VersionedToolRegistry` | class | Overlay registry that manages versioned tools. | [Advanced MCP Server Patterns](../mcp/server/advanced-patterns.md) |
| `VisibilityTransform` | class | Hide tools based on a predicate. | [Advanced MCP Server Patterns](../mcp/server/advanced-patterns.md) |
| `WebhookMiddleware` | class | Fire webhooks on tool call events. | [MCP Server Production Features](../mcp/server/production-features.md) |

## `promptise.mcp.client`

MCP client. `from promptise.mcp.client import ...`

| Name | Kind | Summary | Documented in |
|---|---|---|---|
| `BearerTokenProvider` | callable |  | [MCP Client](../mcp/client/index.md) |
| `InFlightToolCall` | class | A `call_tool` request awaiting its result. | [MCP Client](../mcp/client/index.md) |
| `MCPToolError` | exception | An MCP tool ran and reported a failure. | [MCP Tool Adapter](../mcp/client/tool-adapter.md) |

## `promptise.mcpcast`

MCPcast. `from promptise.mcpcast import ...`

| Name | Kind | Summary | Documented in |
|---|---|---|---|
| `api_name_from_spec` | function | A slug for the API, from `info.title`. | [MCPcast an Existing API](../mcp/server/mcpcast.md) |
| `ApiPlan` | class | The upstream API and how the generated server talks to it. | [MCPcast a Real API: Recipes](../mcp/server/mcpcast-recipes.md) |
| `apply_curation` | function | Turn an accepted proposal into a validated `MCPcastPlan`. | [MCPcast API Reference](../api/mcpcast.md) |
| `ApprovalMode` | enum | Who approves approval-gated tool calls. | [MCPcast API Reference](../api/mcpcast.md) |
| `AuthMode` | enum | How the generated server authenticates against the upstream API. | [MCPcast an Existing API](../mcp/server/mcpcast.md) |
| `build_plan` | function | Map operations 1:1 onto tools, filtered by *profile*. | [MCPcast an Existing API](../mcp/server/mcpcast.md) |
| `CallRecorder` | class | Records every tool call the agent makes, grouped by task id. | [Lab: MCPcast Your SaaS API](../guides/lab-mcpcast-storefront.md) |
| `check_document` | function | Refuse whole-document defects before anything walks or copies *document*. | [MCPcast API Reference](../api/mcpcast.md) |
| `check_postconditions` | function | Every post-condition violation in *result* (empty means it passed). | [MCPcast an Existing API](../mcp/server/mcpcast.md) |
| `Classification` | class | The outcome of classifying one operation, with its reasoning. | [Adaptive Strategy](../core/adaptive-strategy.md) |
| `classify` | function | Classify *op* and explain the decision. | [MCPcast an Existing API](../mcp/server/mcpcast.md) |
| `classify_operation` | function | The `RiskClass` for *op* (see `classify` for the reasoning). | [MCPcast API Reference](../api/mcpcast.md) |
| `Completer` | callable |  | [MCPcast an Existing API](../mcp/server/mcpcast.md) |
| `ConfusedPair` | class | The agent chose *chosen* first when *expected* was the target. | [MCPcast API Reference](../api/mcpcast.md) |
| `credential_slot` | function | The one slot the plan's server presents its credential in, from the spec's schemes. | [MCPcast API Reference](../api/mcpcast.md) |
| `CredentialSlot` | class | Where the generated server presents the upstream credential. | [MCPcast API Reference](../api/mcpcast.md) |
| `curate` | function | Design the tool surface with a model, enforcing every post-condition in code. | [MCPcast an Existing API](../mcp/server/mcpcast.md) |
| `CuratedParam` | class | The model's adjustments to one parameter. | [MCPcast API Reference](../api/mcpcast.md) |
| `CuratedTool` | class | One tool as proposed by the model. | [MCPcast API Reference](../api/mcpcast.md) |
| `CurationResult` | class | The model's complete proposal. | [MCPcast API Reference](../api/mcpcast.md) |
| `CurationViolation` | exception | A curation proposal broke one or more post-conditions. | [MCPcast API Reference](../api/mcpcast.md) |
| `derive_tool_name` | function | `getPetById` → `get_pet_by_id` (unique within *taken*, if given). | [MCPcast API Reference](../api/mcpcast.md) |
| `describe_written` | function | `"bookshelf_mcp/ (7 modules), tests/ (2), server.py, README.md, pyproject.toml…"`. | [MCPcast API Reference](../api/mcpcast.md) |
| `DroppedOp` | class | An operation deliberately left out of the tool surface. | [MCPcast API Reference](../api/mcpcast.md) |
| `EvalReport` | class | The Agent Readiness Score with its evidence. | [MCPcast API Reference](../api/mcpcast.md) |
| `EvalTask` | class | One user request and the tool it is designed to exercise. | [MCPcast an Existing API](../mcp/server/mcpcast.md) |
| `evaluate` | function | Run the Agent Readiness evaluation. | [MCPcast an Existing API](../mcp/server/mcpcast.md) |
| `example_mismatch` | function | Why *value* does not fit *schema*, or `None` if it plausibly does. | [MCPcast API Reference](../api/mcpcast.md) |
| `example_value` | function | A plausible example for one JSON Schema (used for tool examples and mocks). | [MCPcast API Reference](../api/mcpcast.md) |
| `expanded_nodes` | function | Count the nodes of *document* as extraction would visit them, stopping past *limit*. | [MCPcast API Reference](../api/mcpcast.md) |
| `extract_operations` | function | Extract every operation from *spec*, in document order. | [MCPcast an Existing API](../mcp/server/mcpcast.md) |
| `generate_tasks` | function | Generate up to *count* tasks from the plan's tool set. | [MCPcast API Reference](../api/mcpcast.md) |
| `grade_for` | function | Letter grade for a score in `[0, 1]`. | [MCPcast API Reference](../api/mcpcast.md) |
| `is_plan_document` | function | `True` if a parsed YAML/JSON document looks like an `MCPcastPlan` rather than an OpenAPI spec. | [MCPcast API Reference](../api/mcpcast.md) |
| `is_url` | function | `True` if *source* is an `http(s)://` URL string (the scheme is case-insensitive). | [MCPcast API Reference](../api/mcpcast.md) |
| `load_generated_server` | function | Import a generated project's `server.py` launcher as a module. | [MCPcast an Existing API](../mcp/server/mcpcast.md) |
| `load_spec` | function | Load an OpenAPI document from a URL, file path, raw JSON/YAML text, or dict. | [MCPcast an Existing API](../mcp/server/mcpcast.md) |
| `make_example` | function | One worked example covering every required, visible parameter. | [MCPcast API Reference](../api/mcpcast.md) |
| `MAX_DOCUMENT_NODES` | constant |  | [MCPcast API Reference](../api/mcpcast.md) |
| `mcpcast` | function | The deterministic pipeline: load → extract → classify → plan. | [MCPcast an Existing API](../mcp/server/mcpcast.md) |
| `MCPcastError` | exception | Raised for an invalid spec, plan, or curation result. | [MCPcast an Existing API](../mcp/server/mcpcast.md) |
| `MCPcastPlan` | class | The complete, validated tool plan for one API. | [MCPcast an Existing API](../mcp/server/mcpcast.md) |
| `mock_transport` | function | An httpx transport: live `GET`/`HEAD` (optional), spec-derived mocks otherwise. | [MCPcast an Existing API](../mcp/server/mcpcast.md) |
| `Operation` | class | One HTTP operation extracted from the spec. | [MCPcast an Existing API](../mcp/server/mcpcast.md) |
| `package_name` | function | The import package for an API name: `bookshelf` → `bookshelf_mcp`. | [MCPcast API Reference](../api/mcpcast.md) |
| `ParamPlan` | class | The agent-facing shape of one tool parameter. | [Lab: MCPcast Your SaaS API](../guides/lab-mcpcast-storefront.md) |
| `ParamSpec` | class | One input of an operation, with its wire location. | [MCPcast API Reference](../api/mcpcast.md) |
| `plain_http_hosts` | function | Hosts the generated server would send its credential to over plain `http://`. | [MCPcast an Existing API](../mcp/server/mcpcast.md) |
| `public_url` | function | *url* with its userinfo, query string and fragment removed. | [MCPcast API Reference](../api/mcpcast.md) |
| `render_curation_prompt` | function | The user prompt: the operation catalogue plus the budget. | [MCPcast API Reference](../api/mcpcast.md) |
| `render_project` | function | Every generated file for *plan*, keyed by path relative to the project root. | [MCPcast an Existing API](../mcp/server/mcpcast.md) |
| `render_readme` | function | Render `README.md` for *plan*. | [MCPcast an Existing API](../mcp/server/mcpcast.md) |
| `RESERVED_TOOL_NAMES` | constant | frozenset of 18 values | [MCPcast API Reference](../api/mcpcast.md) |
| `resolve_base_url` | function | The plan's API base URL: *override*, else the document-level server, else the first operation's own base. | [MCPcast API Reference](../api/mcpcast.md) |
| `risk_floor` | function | The lowest class `classify` could assign to an operation with this wire mapping. | [MCPcast API Reference](../api/mcpcast.md) |
| `RiskClass` | enum | What calling a tool can do to the world. | [MCPcast an Existing API](../guides/mcpcast-existing-api.md) |
| `RouteParam` | class | Where one parameter of a route travels on the wire. | [MCPcast API Reference](../api/mcpcast.md) |
| `RoutePlan` | class | One upstream HTTP operation a tool can execute. | [MCPcast API Reference](../api/mcpcast.md) |
| `SafetyProfile` | enum | How much of the API an agent is allowed to reach. | [MCPcast an Existing API](../mcp/server/mcpcast.md) |
| `SCAFFOLD_ONCE` | constant | frozenset of 4 values | [MCPcast API Reference](../api/mcpcast.md) |
| `score` | function | Compute the report from task results (pure; no model, no network). | [MCPcast an Existing API](../mcp/server/mcpcast.md) |
| `scrub_strings` | function | *document* with every string: keys and values, at any depth: passed through `scrub_text`. | [MCPcast API Reference](../api/mcpcast.md) |
| `SecurityScheme` | class | One `components.securitySchemes` entry (Swagger 2: `securityDefinitions`), resolved. | [MCPcast API Reference](../api/mcpcast.md) |
| `spec_base_url` | function | The API base URL: *override*, else `servers[0].url`, else Swagger 2 host. | [MCPcast API Reference](../api/mcpcast.md) |
| `spec_description` | function | The spec's `info.description`, stripped (empty if absent or if `info` is not a mapping). | [MCPcast API Reference](../api/mcpcast.md) |
| `spec_summary_line` | function | One line for the plan's `api.description` (and the server's `instructions`): the title, plus the first line of the description when it adds something (`"Bookshelf API: Books,... | [MCPcast API Reference](../api/mcpcast.md) |
| `spec_title` | function | The spec's `info.title`, stripped (empty if absent or if `info` is not a mapping). | [MCPcast API Reference](../api/mcpcast.md) |
| `TaskResult` | class | What happened when the agent attempted one task. | [MCPcast API Reference](../api/mcpcast.md) |
| `tool_group` | function | The tools module a tool lives in: its first tag, else the resource in its path. | [MCPcast API Reference](../api/mcpcast.md) |
| `ToolCall` | class | One tool invocation observed during a task. | [MCPcast API Reference](../api/mcpcast.md) |
| `ToolPlan` | class | One generated MCP tool. | [Lab: MCPcast Your SaaS API](../guides/lab-mcpcast-storefront.md) |
| `tools_from_server` | function | LangChain tools that call *server* in-process through `TestClient`. | [Lab: MCPcast Your SaaS API](../guides/lab-mcpcast-storefront.md) |
| `unmappable_reason` | function | Why *op* cannot become a tool, or `None` if it can. | [MCPcast API Reference](../api/mcpcast.md) |
| `valid_tool_name` | function | Why *name* cannot be a tool name, or `None` if it can. | [MCPcast API Reference](../api/mcpcast.md) |
| `write_eval` | function | Write `eval/tasks.yaml` and `eval/report.md` under *out_dir*. | [MCPcast an Existing API](../mcp/server/mcpcast.md) |
| `write_project` | function | Write the project for *plan* into *out_dir*. | [MCPcast an Existing API](../mcp/server/mcpcast.md) |

## `promptise.prompts`

Prompting. `from promptise.prompts import ...`

| Name | Kind | Summary | Documented in |
|---|---|---|---|
| `advisor` | constant | Balanced advisory perspective. | [Prompt Strategies & Perspectives](../prompting/strategies.md) |
| `analyst` | constant | Evidence-based analysis perspective. | [Prompt Strategies & Perspectives](../prompting/strategies.md) |
| `AssembledPrompt` | class | Result of assembling prompt blocks. | [Layer 1: PromptBlocks](../prompting/blocks.md) |
| `Block` | class | A composable unit of prompt content. | [Layer 1: PromptBlocks](../prompting/blocks.md) |
| `block` | function | Decorator that turns a function into a Block. | [Layer 1: PromptBlocks](../prompting/blocks.md) |
| `BlockContext` | class | Runtime context available to blocks during rendering. | [Layer 1: PromptBlocks](../prompting/blocks.md) |
| `blocks` | function | Decorator that attaches blocks to a `Prompt`. | [Layer 1: PromptBlocks](../prompting/blocks.md) |
| `branch` | function | Route to different prompts based on a condition. | [Prompt Chaining](../prompting/chaining.md) |
| `chain` | function | Create a sequential chain of prompts. | [Prompt Chaining](../prompting/chaining.md) |
| `chain_of_thought` | constant | Step-by-step reasoning with an answer marker. | [Prompt Strategies & Perspectives](../prompting/strategies.md) |
| `Composite` | class | Groups multiple blocks as a single unit. | [Layer 1: PromptBlocks](../prompting/blocks.md) |
| `Conditional` | class | Block that renders only when a condition is true. | [Layer 1: PromptBlocks](../prompting/blocks.md) |
| `constraint` | function | Decorator that attaches a constraint to a `Prompt`. | [Prompt Engineering Overview](../prompting/index.md) |
| `context` | function | Decorator that attaches context providers to a `Prompt`. | [Prompt Context System](../prompting/context.md) |
| `ContextProvider` | class | Pluggable source of dynamic context for prompts. | [Prompt Context System](../prompting/context.md) |
| `ContextSlot` | class | Dynamic injection point, filled at runtime (priority configurable). | [Layer 1: PromptBlocks](../prompting/blocks.md) |
| `ConversationContext` | class | Conversation history and state. | [Prompt Context System](../prompting/context.md) |
| `ConversationFlow` | class | Base class for conversation flow state machines. | [Layer 2: ConversationFlow](../prompting/flows.md) |
| `creative` | constant | Creative exploration perspective. | [Prompt Strategies & Perspectives](../prompting/strategies.md) |
| `critic` | constant | Critical evaluation perspective. | [Prompt Strategies & Perspectives](../prompting/strategies.md) |
| `decompose` | constant | Decompose into subproblems, solve each, then combine. | [Prompt Strategies & Perspectives](../prompting/strategies.md) |
| `EnvironmentContext` | class | Runtime environment. Extend with deployment-specific fields. | [Prompt Context System](../prompting/context.md) |
| `ErrorContext` | class | Previous errors for retry and recovery. | [Prompt Context System](../prompting/context.md) |
| `Examples` | class | Few-shot examples with auto-truncation (priority 4). | [Layer 1: PromptBlocks](../prompting/blocks.md) |
| `fallback` | function | Try prompts in order until one succeeds. | [Prompt Chaining](../prompting/chaining.md) |
| `Guard` | class | Protocol for prompt input/output guards. | [Prompt Guards](../prompting/guards.md) |
| `guard` | function | Decorator that attaches guards to a `Prompt`. | [Prompt Guards](../prompting/guards.md) |
| `GuardError` | exception | Raised when a guard rejects input or output. | [Prompt Guards](../prompting/guards.md) |
| `Identity` | class | Who the agent is: always included (priority 10). | [Layer 1: PromptBlocks](../prompting/blocks.md) |
| `load_directory` | function | Load all `.prompt` files from a directory into a registry. | [Prompt Loader & Templates](../prompting/loader-templates.md) |
| `load_prompt` | function | Load a prompt from a `.prompt` YAML file. | [Prompt Loader & Templates](../prompting/loader-templates.md) |
| `load_url` | function | Load a prompt from a URL (e.g. GitHub raw file). | [Prompt Loader & Templates](../prompting/loader-templates.md) |
| `ObservationBlock` | class | Inject recent tool results into the prompt. | [Layer 1: PromptBlocks](../prompting/blocks.md) |
| `OutputContext` | class | Expected output characteristics. | [Prompt Context System](../prompting/context.md) |
| `OutputFormat` | class | Response structure specification (priority 8). | [Layer 1: PromptBlocks](../prompting/blocks.md) |
| `parallel` | function | Execute multiple prompts concurrently. | [Prompt Chaining](../prompting/chaining.md) |
| `Perspective` | class | Protocol for cognitive perspectives. | [Prompt Strategies & Perspectives](../prompting/strategies.md) |
| `perspective` | function | Create a `CustomPerspective`. | [Prompt Strategies & Perspectives](../prompting/strategies.md) |
| `Phase` | class | A named phase in a conversation flow. | [Layer 2: ConversationFlow](../prompting/flows.md) |
| `phase` | function | Decorator that marks a method as a phase handler. | [Layer 2: ConversationFlow](../prompting/flows.md) |
| `PhaseBlock` | class | Stage-specific instructions that change per reasoning phase. | [Layer 1: PromptBlocks](../prompting/blocks.md) |
| `plan_and_execute` | constant | Plan first, then execute each step. | [Prompt Strategies & Perspectives](../prompting/strategies.md) |
| `PlanBlock` | class | Render the current plan with subgoal progress. | [Layer 1: PromptBlocks](../prompting/blocks.md) |
| `PromptAssembler` | class | Assembles blocks into a final prompt with optional token budgeting. | [Layer 1: PromptBlocks](../prompting/blocks.md) |
| `PromptFileError` | exception | Error loading or saving a `.prompt` file. | [Prompt Loader & Templates](../prompting/loader-templates.md) |
| `PromptInspector` | class | Collects and displays prompt assembly and execution traces. | [Prompt Inspector & Observability](../prompting/inspector.md) |
| `PromptObserver` | class | Bridge between prompt execution and ObservabilityCollector. | [Prompt Inspector & Observability](../prompting/inspector.md) |
| `PromptRegistry` | class | Singleton registry for versioned prompts. | [Prompt Suite & Registry](../prompting/suite-registry.md) |
| `PromptStats` | class | Execution statistics for a single prompt call. | [Prompt Engineering Overview](../prompting/index.md) |
| `PromptTestCase` | class | Base class for prompt tests. | [Prompt Testing](../prompting/testing.md) |
| `PromptTrace` | class | Complete trace of a prompt assembly + execution. | [Prompt Inspector & Observability](../prompting/inspector.md) |
| `PromptValidationError` | exception | Schema validation failed for a `.prompt` file. | [Prompt Loader & Templates](../prompting/loader-templates.md) |
| `ReflectionBlock` | class | Inject past learnings from reflection/evaluation. | [Layer 1: PromptBlocks](../prompting/blocks.md) |
| `registry` | constant | Singleton registry for versioned prompts. | [Prompt Suite & Registry](../prompting/suite-registry.md) |
| `render_template` | function | Convenience function: create a `TemplateEngine` and render. | [Prompt Loader & Templates](../prompting/loader-templates.md) |
| `retry` | function | Wrap a prompt with exponential backoff retry. | [Prompt Chaining](../prompting/chaining.md) |
| `Rules` | class | Behavioral constraints (priority 9). | [Layer 1: PromptBlocks](../prompting/blocks.md) |
| `save_prompt` | function | Save a `Prompt` to a YAML file. | [Prompt Loader & Templates](../prompting/loader-templates.md) |
| `Section` | class | Custom named section (priority configurable). | [Layer 1: PromptBlocks](../prompting/blocks.md) |
| `self_critique` | constant | Generate, critique, and improve. | [Prompt Strategies & Perspectives](../prompting/strategies.md) |
| `ShellExecutionError` | exception | Raised when a templated shell command fails or times out. | [Shell Context Injection in Templates](../prompting/shell-interpolation.md) |
| `ShellExecutor` | class | Protocol for objects that can run a templated shell command. | [Shell Context Injection in Templates](../prompting/shell-interpolation.md) |
| `SimpleBlock` | class | A block created from a string or callable. | [Layer 1: PromptBlocks](../prompting/blocks.md) |
| `Strategy` | class | Protocol for reasoning strategies. | [Prompt Strategies & Perspectives](../prompting/strategies.md) |
| `structured_reasoning` | constant | Multi-step structured reasoning with customizable phases. | [Prompt Strategies & Perspectives](../prompting/strategies.md) |
| `SubprocessShellExecutor` | class | Default shell executor backed by `run`. | [Shell Context Injection in Templates](../prompting/shell-interpolation.md) |
| `TeamContext` | class | Other agents in the team. | [Prompt Context System](../prompting/context.md) |
| `TemplateEngine` | class | Template engine with include registry and optional shell interpolation. | [Prompt Loader & Templates](../prompting/loader-templates.md) |
| `ToolsBlock` | class | Render available tool schemas for the LLM. | [Layer 1: PromptBlocks](../prompting/blocks.md) |
| `TurnContext` | class | Mutable context passed to `@phase` handlers each turn. | [Layer 2: ConversationFlow](../prompting/flows.md) |
| `UserContext` | class | Who the agent is serving. Extend with any user-specific fields. | [Prompt Context System](../prompting/context.md) |
| `version` | function | Decorator that registers a `Prompt` in the global registry. | [Prompt Suite & Registry](../prompting/suite-registry.md) |

## `promptise.runtime`

Agent Runtime. `from promptise.runtime import ...`

| Name | Kind | Summary | Documented in |
|---|---|---|---|
| `AgentManifestSchema` | class | Root schema for `.agent` manifest files. | [Agent Manifests](../runtime/manifests.md) |
| `Anomaly` | class | A detected behavioral anomaly. | [Behavioral Health Monitoring](../runtime/governance/health.md) |
| `AnomalyType` | enum | Types of behavioral anomaly. | [Behavioral Health Monitoring](../runtime/governance/health.md) |
| `BaseTrigger` | class | Protocol for all trigger implementations. | [Triggers Overview](../runtime/triggers/index.md) |
| `BLOCKABLE_EVENTS` | constant | frozenset of 6 values | [Runtime Lifecycle Hooks](../runtime/hooks.md) |
| `BudgetEnforcer` | class | Handles budget violations according to the configured policy. | [Autonomy Budget](../runtime/governance/budget.md) |
| `BudgetExceededError` | exception | Raised before a tool call that would exceed an autonomy budget limit. | [Agent Runtime](../runtime/index.md) |
| `BudgetState` | class | Thread-safe budget counters with per-run and daily tracking. | [Autonomy Budget](../runtime/governance/budget.md) |
| `BudgetViolation` | class | Describes a budget limit that was exceeded. | [Autonomy Budget](../runtime/governance/budget.md) |
| `BusEvent` | class | An event delivered by `InProcessEventBus`. | [Event and Webhook Triggers](../runtime/triggers/event-webhook.md) |
| `ClusterNode` | class | This runtime's membership in a cluster (see the module docstring). | [Service Discovery and Transport](../runtime/distributed/discovery-transport.md) |
| `ContextConfig` | class | AgentContext configuration. | [Runtime Configuration](../runtime/configuration.md) |
| `ConversationBuffer` | class | Rolling buffer of conversation messages (short-term memory). | [Conversation Buffer](../runtime/conversation.md) |
| `create_trigger` | function | Factory: create a trigger from a `TriggerConfig`. | [Triggers Overview](../runtime/triggers/index.md) |
| `CronTrigger` | class | Fires at scheduled intervals defined by a cron expression. | [Cron Trigger](../runtime/triggers/cron.md) |
| `DEFAULT_DEVELOPMENT_CONFIG` | constant | Top-level runtime configuration (composite). | [Runtime Configuration](../runtime/configuration.md) |
| `DEFAULT_PRODUCTION_CONFIG` | constant | Top-level runtime configuration (composite). | [Runtime Configuration](../runtime/configuration.md) |
| `DiscoveredNode` | class | A discovered runtime node. | [Service Discovery and Transport](../runtime/distributed/discovery-transport.md) |
| `DispatchResult` | class | Outcome of a `dispatch` call. | [Runtime Lifecycle Hooks](../runtime/hooks.md) |
| `DistributedConfig` | class | Distributed runtime coordination (`RuntimeConfig.distributed`). | [Runtime Configuration](../runtime/configuration.md) |
| `EscalationTarget` | class | Where to send escalation notifications. | [Runtime Configuration](../runtime/configuration.md) |
| `EventTrigger` | class | Trigger that fires when an EventBus event matches. | [Triggers Overview](../runtime/triggers/index.md) |
| `ExecutionMode` | enum | Agent execution mode. | [Runtime Configuration](../runtime/configuration.md) |
| `FileJournal` | class | JSONL-based journal for durable process audit logging. | [Journal Backends](../runtime/journal/backends.md) |
| `HealthMonitor` | class | Lightweight behavioral health analysis. | [Behavioral Health Monitoring](../runtime/governance/health.md) |
| `HookBlocked` | exception | Raised by a hook to block the action that triggered it. | [Runtime Lifecycle Hooks](../runtime/hooks.md) |
| `HookCallable` | callable |  | [Runtime Lifecycle Hooks](../runtime/hooks.md) |
| `HookConfig` | class | One runtime lifecycle hook of a process (`ProcessConfig.hooks`). | [Runtime Configuration](../runtime/configuration.md) |
| `HookContext` | class | Context passed to every hook callback. | [Runtime Lifecycle Hooks](../runtime/hooks.md) |
| `HookEvent` | enum | Lifecycle events that a `HookManager` can dispatch. | [Runtime Lifecycle Hooks](../runtime/hooks.md) |
| `HookManager` | class | Event-keyed dispatcher for runtime lifecycle hooks. | [Runtime Lifecycle Hooks](../runtime/hooks.md) |
| `InboxConfig` | class | Configuration for the human-to-agent message inbox. | [Runtime Configuration](../runtime/configuration.md) |
| `InboxMessage` | class | A message from a human to a running agent. | [Orchestration API](../runtime/api.md) |
| `InboxResponse` | class | An agent's response to a question. | [Orchestration API](../runtime/api.md) |
| `InMemoryJournal` | class | In-memory journal for testing and development. | [Journal Backends](../runtime/journal/backends.md) |
| `InProcessEventBus` | class | An event bus that delivers to subscribers in the same Python process. | [Event and Webhook Triggers](../runtime/triggers/event-webhook.md) |
| `JournalConfig` | class | Journal (durable audit log) configuration. | [Runtime Configuration](../runtime/configuration.md) |
| `JournalEntry` | class | Single journal record. | [Journal Backends](../runtime/journal/backends.md) |
| `JournalError` | exception | Raised when journal read, write, or replay fails. | [Agent Runtime](../runtime/index.md) |
| `JournalLevel` | enum | Journal detail level. | [Journal System](../runtime/journal/index.md) |
| `JournalProvider` | class | Protocol for journal backends. | [Journal Backends](../runtime/journal/backends.md) |
| `load_manifest` | function | Load and validate a `.agent` manifest file. | [Agent Manifests](../runtime/manifests.md) |
| `manifest_to_process_config` | function | Convert a manifest into a `ProcessConfig`. | [Agent Manifests](../runtime/manifests.md) |
| `ManifestEnvVarError` | exception | A `${VAR}` in a `.agent` manifest names an unset environment variable. | [Agent Runtime](../runtime/index.md) |
| `ManifestError` | exception | Raised when `.agent` manifest loading fails (I/O or parse error). | [Agent Runtime](../runtime/index.md) |
| `ManifestValidationError` | exception | Raised when `.agent` manifest schema validation fails. | [Agent Runtime](../runtime/index.md) |
| `MessageInbox` | class | Async-safe inbox for human-to-agent messages. | [Orchestration API](../runtime/api.md) |
| `MessageTrigger` | class | Trigger that fires when a message arrives on a broker topic. | [Triggers Overview](../runtime/triggers/index.md) |
| `MessageType` | enum | Types of human-to-agent messages. | [Orchestration API](../runtime/api.md) |
| `MissionEvaluation` | class | Result of an evaluation. | [Mission-Oriented Process Model](../runtime/governance/mission.md) |
| `MissionEvidence` | class | Bundle of evidence passed to the evaluator. | [Mission-Oriented Process Model](../runtime/governance/mission.md) |
| `MissionTracker` | class | Tracks mission progress across invocations. | [Mission-Oriented Process Model](../runtime/governance/mission.md) |
| `NodeInfo` | class | Information about a runtime node in the cluster. | [Distributed Coordinator](../runtime/distributed/coordinator.md) |
| `OpenModeConfig` | class | Guardrails for open execution mode. | [Runtime Configuration](../runtime/configuration.md) |
| `OrchestrationAPI` | class | REST API server for managing an AgentRuntime. | [Orchestration API](../runtime/api.md) |
| `OrchestrationAPIError` | exception | An error answer from the Orchestration API. | [Orchestration API](../runtime/api.md) |
| `OrchestrationClient` | class | Async client for the Promptise Orchestration API. | [Orchestration API](../runtime/api.md) |
| `ProcessLifecycle` | class | State machine for process lifecycle. | [Process Lifecycle](../runtime/lifecycle.md) |
| `ProcessStateError` | exception | Raised when a lifecycle state transition is invalid. | [Agent Runtime](../runtime/index.md) |
| `ProcessTransition` | class | Record of a single state transition. | [Process Lifecycle](../runtime/lifecycle.md) |
| `register_trigger_type` | function | Register a custom trigger type. | [Triggers Overview](../runtime/triggers/index.md) |
| `registered_trigger_types` | function | Return the names of all registered trigger types. | [Triggers Overview](../runtime/triggers/index.md) |
| `RegistryDiscovery` | class | In-process registry for node discovery. | [Service Discovery and Transport](../runtime/distributed/discovery-transport.md) |
| `ReplayEngine` | class | Replays journal entries to reconstruct process state. | [Replay Engine](../runtime/journal/replay.md) |
| `RewindEngine` | class | Multi-granularity rewind over a journal. | [Agent Process Rewind](../runtime/journal/rewind.md) |
| `RewindMode` | enum | Selectivity of a `apply` operation. | [Agent Process Rewind](../runtime/journal/rewind.md) |
| `RewindPlan` | class | Result of `plan`: preview without committing. | [Agent Process Rewind](../runtime/journal/rewind.md) |
| `RewindResult` | class | State produced by `apply`. | [Agent Process Rewind](../runtime/journal/rewind.md) |
| `RuntimeBaseError` | exception | Base exception for all runtime module errors. | [Agent Runtime](../runtime/index.md) |
| `RuntimeCoordinator` | class | Cluster coordinator for distributed runtime management. | [Distributed Coordinator](../runtime/distributed/coordinator.md) |
| `RuntimeDashboard` | class | Full-screen tabbed terminal dashboard for the Agent Runtime. | [Runtime Dashboard](../runtime/dashboard.md) |
| `RuntimeDashboardState` | class | Thread-safe shared state for the runtime dashboard. | [Runtime Dashboard](../runtime/dashboard.md) |
| `RuntimeDataCollector` | class | Collects real-time data from a running AgentRuntime into DashboardState. | [Runtime Dashboard](../runtime/dashboard.md) |
| `save_manifest` | function | Write a manifest to a YAML file. | [Agent Manifests](../runtime/manifests.md) |
| `SecretScope` | class | Per-process secret vault with TTL, access logging, and rotation. | [Secret Scoping](../runtime/governance/secrets.md) |
| `ShellHook` | class | Runtime hook that shells out to an external command. | [Runtime Lifecycle Hooks](../runtime/hooks.md) |
| `ShellHookResult` | class | Parsed result returned by an external shell hook. | [Runtime Lifecycle Hooks](../runtime/hooks.md) |
| `StateEntry` | class | Audit record for a single state write. | [Agent Context](../runtime/context.md) |
| `StateError` | exception | Raised when an invalid state transition is attempted. | [Process Lifecycle](../runtime/lifecycle.md) |
| `StaticDiscovery` | class | Static discovery from a known list of node URLs. | [Service Discovery and Transport](../runtime/distributed/discovery-transport.md) |
| `ToolCostAnnotation` | class | Cost and reversibility annotation for a tool. | [Runtime Configuration](../runtime/configuration.md) |
| `TriggerConfig` | class | Configuration for a single trigger. | [Runtime Configuration](../runtime/configuration.md) |
| `TriggerDeliveryConfig` | class | How a process handles trigger events that can't be processed. | [Runtime Configuration](../runtime/configuration.md) |
| `TriggerError` | exception | Raised when a trigger fails to start, stop, or produce events. | [Agent Runtime](../runtime/index.md) |
| `TriggerEvent` | class | Event produced by a trigger. | [Triggers Overview](../runtime/triggers/index.md) |
| `VALID_TRANSITIONS` | constant | mapping of 8 entries | [Process Lifecycle](../runtime/lifecycle.md) |
| `validate_manifest` | function | Validate a manifest file, returning a list of warnings. | [Agent Manifests](../runtime/manifests.md) |

## `promptise.runtime.triggers`

Runtime triggers. `from promptise.runtime.triggers import ...`

| Name | Kind | Summary | Documented in |
|---|---|---|---|
| `BaseTrigger` | class | Protocol for all trigger implementations. | [Triggers Overview](../runtime/triggers/index.md) |
| `BusEvent` | class | An event delivered by `InProcessEventBus`. | [Event and Webhook Triggers](../runtime/triggers/event-webhook.md) |
| `compile_filter` | function | Turn a `filter_expression` into a predicate over trigger events. | [Triggers Overview](../runtime/triggers/index.md) |
| `create_trigger` | function | Factory: create a trigger from a `TriggerConfig`. | [Triggers Overview](../runtime/triggers/index.md) |
| `CronTrigger` | class | Fires at scheduled intervals defined by a cron expression. | [Cron Trigger](../runtime/triggers/cron.md) |
| `EventTrigger` | class | Trigger that fires when an EventBus event matches. | [Triggers Overview](../runtime/triggers/index.md) |
| `FilterExpressionError` | exception | A filter expression is malformed or uses a construct that is not allowed. | [Triggers Overview](../runtime/triggers/index.md) |
| `InProcessEventBus` | class | An event bus that delivers to subscribers in the same Python process. | [Event and Webhook Triggers](../runtime/triggers/event-webhook.md) |
| `MessageTrigger` | class | Trigger that fires when a message arrives on a broker topic. | [Triggers Overview](../runtime/triggers/index.md) |
| `register_trigger_type` | function | Register a custom trigger type. | [Triggers Overview](../runtime/triggers/index.md) |
| `registered_trigger_types` | function | Return the names of all registered trigger types. | [Triggers Overview](../runtime/triggers/index.md) |
| `TriggerEvent` | class | Event produced by a trigger. | [Triggers Overview](../runtime/triggers/index.md) |
| `unregister_trigger_type` | function | Remove a registered trigger type. | [Triggers Overview](../runtime/triggers/index.md) |

## `promptise.runtime.journal`

Runtime journal. `from promptise.runtime.journal import ...`

| Name | Kind | Summary | Documented in |
|---|---|---|---|
| `FileJournal` | class | JSONL-based journal for durable process audit logging. | [Journal Backends](../runtime/journal/backends.md) |
| `InMemoryJournal` | class | In-memory journal for testing and development. | [Journal Backends](../runtime/journal/backends.md) |
| `JournalEntry` | class | Single journal record. | [Journal Backends](../runtime/journal/backends.md) |
| `JournalLevel` | enum | Journal detail level. | [Journal System](../runtime/journal/index.md) |
| `JournalProvider` | class | Protocol for journal backends. | [Journal Backends](../runtime/journal/backends.md) |
| `RedactingJournal` | class | A `JournalProvider` that redacts before it writes. | [Journal Backends](../runtime/journal/backends.md) |
| `ReplayEngine` | class | Replays journal entries to reconstruct process state. | [Replay Engine](../runtime/journal/replay.md) |
| `RewindEngine` | class | Multi-granularity rewind over a journal. | [Agent Process Rewind](../runtime/journal/rewind.md) |
| `RewindMode` | enum | Selectivity of a `apply` operation. | [Agent Process Rewind](../runtime/journal/rewind.md) |
| `RewindPlan` | class | Result of `plan`: preview without committing. | [Agent Process Rewind](../runtime/journal/rewind.md) |
| `RewindResult` | class | State produced by `apply`. | [Agent Process Rewind](../runtime/journal/rewind.md) |

## `promptise.runtime.distributed`

Distributed runtime. `from promptise.runtime.distributed import ...`

| Name | Kind | Summary | Documented in |
|---|---|---|---|
| `ClusterNode` | class | This runtime's membership in a cluster (see the module docstring). | [Service Discovery and Transport](../runtime/distributed/discovery-transport.md) |
| `DiscoveredNode` | class | A discovered runtime node. | [Service Discovery and Transport](../runtime/distributed/discovery-transport.md) |
| `NodeInfo` | class | Information about a runtime node in the cluster. | [Distributed Coordinator](../runtime/distributed/coordinator.md) |
| `NodeUnreachableError` | exception | A node could not be reached (connection refused, DNS, timeout...). | [Distributed Coordinator](../runtime/distributed/coordinator.md) |
| `ProcessDiscovery` | class | Protocol for node discovery mechanisms. | [Service Discovery and Transport](../runtime/distributed/discovery-transport.md) |
| `RegistryDiscovery` | class | In-process registry for node discovery. | [Service Discovery and Transport](../runtime/distributed/discovery-transport.md) |
| `RuntimeCoordinator` | class | Cluster coordinator for distributed runtime management. | [Distributed Coordinator](../runtime/distributed/coordinator.md) |
| `RuntimeTransport` | class | HTTP management API for an `AgentRuntime`. | [Service Discovery and Transport](../runtime/distributed/discovery-transport.md) |
| `StaticDiscovery` | class | Static discovery from a known list of node URLs. | [Service Discovery and Transport](../runtime/distributed/discovery-transport.md) |

## `promptise.identity`

Agent identity. `from promptise.identity import ...`

| Name | Kind | Summary | Documented in |
|---|---|---|---|
| `AwsEksProjectedProvider` | class | AWS EKS projected-token credential source. | [AWS IAM](../identity/providers/aws.md) |
| `AwsStsProvider` | class | AWS STS web-identity-token credential source (lazily imports boto3). | [AWS IAM](../identity/providers/aws.md) |
| `CachedCredential` | class | One cached identity credential (a verifiable JWT) and its expiry. | [Agent Identity Architecture](../identity/architecture.md) |
| `CallableTokenProvider` | class | Invokes a user-supplied callable to acquire the identity JWT. | [Agent Identity Architecture](../identity/architecture.md) |
| `CREDENTIAL_REFRESH_BUFFER_SECONDS` | constant |  | [Agent Identity Architecture](../identity/architecture.md) |
| `CredentialAcquisitionError` | exception | A verifiable credential provider could not produce an identity JWT. | [Agent Identity Architecture](../identity/architecture.md) |
| `decode_jwt_claims` | function | Best-effort decode of a JWT's claims payload, without verifying. | [Agent Identity Architecture](../identity/architecture.md) |
| `decode_jwt_expiry` | function | Best-effort read of the `exp` claim from a JWT, without verifying. | [Agent Identity Architecture](../identity/architecture.md) |
| `EntraManagedIdentityProvider` | class | Entra managed-identity credential source backed by Azure IMDS. | [Microsoft Entra ID](../identity/providers/entra.md) |
| `EntraProjectedTokenProvider` | class | Entra projected-token credential source for AKS Workload Identity. | [Microsoft Entra ID](../identity/providers/entra.md) |
| `FileTokenProvider` | class | Reads the identity JWT from a file on disk on every refresh. | [Agent Identity Architecture](../identity/architecture.md) |
| `GcpMetadataProvider` | class | GCP credential source backed by the Compute metadata server. | [Google Cloud](../identity/providers/gcp.md) |
| `IdentityProvider` | class | Abstract base for a verifiable agent-identity credential source. | [Agent Identity Architecture](../identity/architecture.md) |
| `OidcCallableProvider` | class | Generic OIDC credential source that invokes a callable for the JWT. | [Generic OIDC](../identity/providers/oidc.md) |
| `OidcFileProvider` | class | Generic OIDC credential source that reads the JWT from a file path. | [Generic OIDC](../identity/providers/oidc.md) |
| `PlatformDetectionError` | exception | `auto` could not detect a supported platform. | [Agent Identity Architecture](../identity/architecture.md) |
| `ProviderConfigError` | exception | A credential provider was misconfigured at construction time. | [Agent Identity Architecture](../identity/architecture.md) |
| `SpiffeFileProvider` | class | SPIFFE credential source reading a JWT-SVID from `spiffe-helper`. | [SPIFFE / SPIRE](../identity/providers/spiffe.md) |
| `SpiffeSdkProvider` | class | SPIFFE credential source using the Workload API via py-spiffe. | [SPIFFE / SPIRE](../identity/providers/spiffe.md) |

## `promptise.sandbox`

Sandbox. `from promptise.sandbox import ...`

| Name | Kind | Summary | Documented in |
|---|---|---|---|
| `cleanup_on_exit` | function | Ensure session is cleaned up on process exit. | [Sandbox API Reference](../api/sandbox.md) |
| `CommandResult` | class | Result of a command execution in the sandbox. | [Code Sandbox for Agents](../core/sandbox.md) |
| `DockerBackend` | class | Docker-based sandbox backend with gVisor support. | [Code Sandbox for Agents](../core/sandbox.md) |
| `NetworkMode` | enum | Network isolation modes for sandbox. | [Code Sandbox for Agents](../core/sandbox.md) |
| `SandboxBackend` | class | Abstract base class for sandbox backends. | [Code Sandbox for Agents](../core/sandbox.md) |
| `SandboxConfig` | class | Configuration for sandbox environment. | [Code Sandbox for Agents](../core/sandbox.md) |
| `SandboxContainerManager` | class | Utility for managing and monitoring sandbox containers. | [Sandbox API Reference](../api/sandbox.md) |
| `SandboxManager` | class | Manages sandbox lifecycle and provides integration with agents. | [Code Sandbox for Agents](../core/sandbox.md) |
| `SandboxSession` | class | Manages a persistent sandbox session for command execution. | [Code Sandbox for Agents](../core/sandbox.md) |

## `promptise.run`

promptise run. `from promptise.run import ...`

| Name | Kind | Summary | Documented in |
|---|---|---|---|
| `find_manifest` | function | The nearest manifest from *start* (default: the cwd) upwards, or `None`. | [Projects & promptise run](../core/projects.md) |
| `load_manifest` | function | Read and validate the manifest at *path* (`promptise.toml` or `pyproject.toml`). | [Projects & promptise run](../core/projects.md) |
| `ManifestError` | exception | The manifest cannot be found, read or validated. | [Projects & promptise run](../core/projects.md) |
| `ProjectManifest` | class | A parsed `promptise.toml` (or `[tool.promptise]`). | [Projects & promptise run](../core/projects.md) |
| `RunContext` | class | What an agent factory may want to know about the run. | [Projects & promptise run](../core/projects.md) |

