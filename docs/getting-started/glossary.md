---
title: Agentic AI Glossary — Promptise Foundry terms and concepts
description: Definitions for the core concepts in Promptise Foundry and the broader agentic AI ecosystem — agents, MCP, tools, memory, runtime, governance, sandboxing, and more.
keywords: AI agent glossary, agentic AI terms, MCP glossary, Promptise terms, autonomous agent definitions, AI agent vocabulary
schema: DefinedTermSet
---

# Glossary

Definitions for the terms used throughout Promptise Foundry and the broader agentic AI ecosystem. Sorted alphabetically.

## Agent

An LLM-powered process that can use **tools**, **remember context**, and **make decisions** to accomplish a goal. In Promptise Foundry, an agent is created via [`build_agent()`](../core/agents/building-agents.md) and runs as either a stateless `PromptiseAgent` or a long-running `AgentProcess` inside the [Agent Runtime](../runtime/index.md).

## AgentContext

Unified key-value state attached to a runtime process. Tracks write permissions, memory provider, environment variables, and file mounts. Every mutation is recorded with a timestamp for full history.

## AgentProcess

A single agent wrapped with lifecycle, triggers, journals, and governance. States: `CREATED → STARTING → RUNNING → SUSPENDED → STOPPING → STOPPED / FAILED`. The unit of execution in the Agent Runtime.

## AgentRuntime

The manager of multiple `AgentProcess` instances. Centralizes lifecycle control, exposes a shared `EventBus`, and routes inter-agent messages.

## ask_peer

Cross-agent delegation primitive. Sends a question to another agent over HTTP+JWT and awaits the answer. Pairs with `broadcast` for one-to-many.

## Audit log

Tamper-evident record of every tool call, identity, input, and outcome. Promptise Foundry's `AuditMiddleware` chains entries with HMAC for tamper detection. JSONL output.

## Authentication

Verifying *who* is calling. Promptise Foundry's MCP server SDK ships `JWTAuth` (HS256), `AsymmetricJWTAuth` (RS256/ES256), and `APIKeyAuth`. See [Auth & Security](../mcp/server/auth-security.md).

## Authorization

Verifying *whether* the caller is allowed to perform an action. Promptise Foundry uses **capability-based access policies** plus per-tool guards (`HasRole`, `HasAllRoles`, `RequireAuth`, `RequireClientId`).

## broadcast

Cross-agent delegation primitive. Sends the same question to multiple peers in parallel with a timeout. Graceful degradation if a peer fails.

## Budget

A governance subsystem in the Agent Runtime. Per-run and daily limits on tool calls, LLM turns, cost units, and irreversible actions. Enforcement: log, pause, stop, escalate.

## CallerContext

Per-request user identity. Carries `user_id`, `bearer_token`, `roles`, `scopes`, and `metadata`. Propagated via async contextvars to cache, guardrails, conversations, and observability — so every component sees the same caller.

## Capability

A fine-grained permission token in the access policy. More granular than roles. Capabilities can be required by tools, guards, and middleware.

## Circuit breaker

Middleware pattern that opens after N consecutive failures and stops calling the downstream tool until a recovery timeout elapses. Promptise Foundry ships `CircuitBreakerMiddleware`.

## Conversation buffer

Short-term memory across invocations of the same agent process. Configurable max messages. Async-safe.

## ConversationFlow

A system prompt that transforms across phases of a conversation. Each phase has its own active blocks and lifecycle hooks (`on_enter`, `on_exit`). Phase transitions change which blocks are active.

## ConversationStore

Protocol for persisting conversations. Four backends: `InMemoryConversationStore`, `SQLiteConversationStore`, `PostgresConversationStore`, `RedisConversationStore`.

## Cron trigger

Trigger type that fires on a cron expression. One of five built-in trigger types in the Agent Runtime.

## Cross-agent delegation

Pattern where one agent calls another agent (instead of calling a tool). Used to compose specialized agents into a system. Promptise Foundry ships `ask_peer` and `broadcast` primitives over HTTP+JWT.

## Discovery

The process of an MCP client connecting to a server, calling `tools/list`, and registering each tool as a typed function. Promptise Foundry does this automatically — you give it a server URL, it gives you tools.

## Elicitation

MCP primitive where the server requests structured input from the user mid-execution. Lets a tool ask follow-up questions.

## EventBus

Shared pub/sub channel inside the Agent Runtime. Used by `EventTrigger` and by agents to emit/consume events.

## File watch trigger

Trigger type that fires when files matching a glob pattern change in a watched directory.

## Guardrails

Input/output security checks. Promptise Foundry's `PromptiseSecurityScanner` has six detection heads — DeBERTa for prompt injection, regex for PII (69 patterns), regex for credentials (96 patterns), GLiNER for NER, Llama Guard / Azure AI for content safety, and custom rules. All run locally.

## Health (governance)

Anomaly detection in the Agent Runtime. Catches stuck loops (identical calls N times), repeating tool patterns, empty responses, and high error rates over a sliding window. Cooldown between repeated anomalies.

## HMAC

Hash-based message authentication code. Used for webhook signature verification and for chaining audit log entries to detect tampering.

## InMemoryProvider / ChromaProvider / Mem0Provider

The three memory provider implementations. `InMemoryProvider` is for testing. `ChromaProvider` is local vector search with persistence. `Mem0Provider` is enterprise-grade with graph search.

## Journal

Append-only log of every state transition, trigger event, and invocation result. Promptise Foundry ships `InMemoryJournal` and `FileJournal`. The `ReplayEngine` uses journals plus checkpoints to recover state after a crash.

## JWT

JSON Web Token. Signed token format used for authentication. Promptise Foundry supports symmetric (`JWTAuth`, HS256) and asymmetric (`AsymmetricJWTAuth`, RS256/ES256) signing.

## LLM-as-judge

Pattern where an LLM evaluates whether some output meets criteria. Promptise Foundry's Mission governance uses this pattern to score progress against success criteria.

## LangChain BaseChatModel

LangChain's abstract base class for chat models. Promptise Foundry accepts any `BaseChatModel` instance directly in `build_agent(model=...)` for full LangChain interop.

## MCP

[Model Context Protocol](what-is-mcp.md). Open standard for connecting LLMs to tools, resources, and prompts. Promptise Foundry is MCP-native — both client and server.

## MCPClient

Promptise Foundry's native MCP client for a single server. Built from scratch, no third-party dependencies.

## MCPMultiClient

Promptise Foundry's MCP client that connects to N servers, presents a unified tool list, and auto-routes calls to the right server.

## MCPQueue

Built-in priority job queue for MCP servers. Auto-registers `queue_submit`, `queue_status`, `queue_result`, `queue_cancel`, `queue_list` tools.

## MCPRouter

Modular tool grouping with namespace prefixes and shared middleware. Like FastAPI's `APIRouter`. Mount into a parent server.

## MCPServer

The main MCP server class in Promptise Foundry. `@server.tool()`, `@server.resource()`, `@server.prompt()` decorators. Schema auto-generated from type hints.

## MCPToolAdapter

Converts MCP tools into LangChain `BaseTool` objects with recursive schema handling. For interop with LangChain-based agents.

## Memory provider

Pluggable backend for long-term memory. Auto-searched before every invocation; results injected into the system prompt with prompt-injection mitigation.

## Message trigger

Trigger type that fires when a message matches a topic pattern (with wildcards). Topic-based pub/sub.

## Middleware

Composable handler in the MCP server pipeline. Runs before/after each tool call. Pre-compiled at server start for zero-overhead dispatch.

## Mission

Governance subsystem for objective-driven agents. Defines an objective and success criteria, evaluates progress every N invocations using LLM-as-judge, supports programmatic `success_check` callables, confidence thresholds, timeouts, and auto-completion on success.

## Open Mode

Configuration that enables an agent to **modify itself** at runtime via 14 meta-tools (modify instructions, create tools, connect MCP servers, add triggers, spawn processes, store/search/forget memory, etc.). Guardrails enforce limits and require sandbox for agent-written code.

## Perspective

Prompt engineering primitive that frames the agent's voice. Four built-in: `Analyst`, `Critic`, `Advisor`, `Creative`. Orthogonal to strategies. Customizable via `CustomPerspective`.

## Prompt block

Reusable, priority-ordered building block for system prompts. Eight types: `Identity`, `Rules`, `OutputFormat`, `ContextSlot`, `Section`, `Examples`, `Conditional`, `Composite`. Priority-based token budgeting drops lowest-priority blocks first when the token budget is tight.

## PromptInspector

Debugging tool that traces prompt assembly step-by-step — which blocks were included or excluded, estimated tokens per block, render time, context provider execution, guard results.

## PromptiseAgent

The core single-agent class. Created via `build_agent()`. Stateless between calls unless conversation persistence is configured.

## PromptiseSecurityScanner

The unified guardrails scanner with six detection heads (see Guardrails).

## RAG

Retrieval-Augmented Generation. The pattern of injecting retrieved context into a prompt. Promptise Foundry's memory providers automatically retrieve and inject before each invocation, with prompt-injection mitigation built in.

## Rate limiter

Token-bucket rate limiter, per-client and per-tool granularity. Burst capacity. Returns `Retry-After` headers.

## ReplayEngine

Reconstructs an `AgentProcess` state from a checkpoint plus the journal. Used for crash recovery — when a process restarts, it replays journal entries from the last checkpoint to reach the last known good state.

## RuntimeCoordinator

Multi-node coordinator for distributed Agent Runtime deployments. Pairs with `StaticDiscovery` or `RegistryDiscovery`. No etcd or Consul dependency.

## Sampling (MCP)

MCP primitive where the server asks the client's LLM for a completion. Lets a server-side tool call back to the orchestrator's model.

## Sandbox

Isolated execution environment for agent-written or untrusted code. Promptise Foundry's sandbox is Docker-based with seccomp, capability dropping (~40 caps), read-only rootfs, resource limits, and network isolation. Optional gVisor for stronger isolation.

## Secrets (governance)

Per-process credential context with TTL expiry, rotation without restart, access logging in the journal, and zero-fill revocation on stop. Values are never serialized to journals or status output.

## Semantic cache

Cache that returns a stored response when the new query is **semantically similar** to a cached one. Promptise Foundry's `SemanticCache` supports in-memory or Redis backends, scope isolation (per-user, per-session, shared), and re-runs output guardrails on cached responses.

## Session state

Per-client key-value storage that persists across multiple tool calls in a session. Managed by `SessionManager` in the MCP server SDK.

## Strategy

Reasoning pattern wrapper. Five built-in: `ChainOfThought`, `StructuredReasoning`, `SelfCritique`, `PlanAndExecute`, `Decompose`. Composable with `+` (e.g., `chain_of_thought + self_critique`).

## SuperAgent

A `.superagent` YAML file declaring a complete agent — model, instructions, MCP servers, memory, sandbox, observability, cache, guardrails, cross-agents. Loadable via the CLI.

## Tool

A callable function exposed to the LLM. In Promptise Foundry, tools come from MCP servers (auto-discovered) or are defined directly via decorators.

## Tool optimization

Selecting the relevant subset of tools for a given query to reduce token usage. Promptise Foundry has four levels: `NONE`, `MINIMAL`, `STANDARD`, `SEMANTIC`. Semantic mode uses local embeddings — typically 40–70% fewer tokens.

## Trigger

What launches an `AgentProcess` invocation. Five built-in types: `CronTrigger`, `EventTrigger`, `MessageTrigger`, `WebhookTrigger`, `FileWatchTrigger`. Multiple triggers can compose on one process.

## Versioned tool registry

Allows multiple versions of the same tool name to coexist (`search` and `search@1.0`). Clients see the latest by default.

## Webhook trigger

Trigger type that fires on HTTP POST with HMAC signature verification.

---

**Next:** [Quick Start →](quickstart.md) · [What is MCP? →](what-is-mcp.md) · [FAQ →](../faq.md)
