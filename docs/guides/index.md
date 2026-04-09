# Guides

Practical, end-to-end guides for building real systems with Promptise Foundry. Each guide takes you from concept to working code, covering architecture decisions, implementation patterns, and production considerations.

---

## Available Guides

### [Building AI Agents](building-agents.md)

Build a production-ready AI agent from scratch with MCP tool discovery, persistent memory, full observability, sandboxed code execution, and cross-agent delegation. One function call creates it. Every capability is opt-in.

**You'll learn:** `build_agent()`, model independence, MCP auto-discovery, memory providers, observability transporters, sandbox security, cross-agent delegation, SuperAgent files.

---

### [Building Production MCP Servers](production-mcp-servers.md)

Build a production-grade MCP server that AI agents connect to for tool access. Covers tool registration, Pydantic validation, JWT authentication with structured client context, scope-based authorization, routers, middleware, caching, request tracing, and response metadata.

**You'll learn:** MCPServer, tool/resource/prompt decorators, AuthMiddleware, ClientContext, HasScope guards, on_authenticate hooks, MCPRouter, ToolResponse, request tracing.

---

### [Building Agentic Runtime Systems](agentic-runtime.md)

Build autonomous, long-running AI agents that react to events, persist state, recover from crashes, enforce governance policies, and scale across machines. Goes from a single cron-triggered agent to a governed, mission-driven, distributed multi-agent operations center.

**You'll learn:** AgentProcess, triggers, persistent context, journals, governance (budget, health, mission, secrets), AgentRuntime, distributed coordination.

---

### [Prompt Engineering](prompt-engineering.md)

Build reliable, testable system prompts with typed blocks, priority-based token budgeting, composable reasoning strategies, runtime guards, dynamic context injection, conversation flows, version control, and automated testing.

**You'll learn:** PromptBlocks, strategies, perspectives, guards, context providers, ConversationFlow, PromptBuilder, registry, inspector, chaining, YAML templates, testing.

---

### [Building Multi-User Systems](multi-user-systems.md)

Build a production-ready multi-user AI application with end-to-end identity propagation — JWT authentication flows from your backend through the agent to MCP servers, conversation ownership prevents cross-user access, semantic cache isolates per-user, guardrails protect every input and output, and tamper-evident audit logs record every action with the authenticated identity.

**You'll learn:** CallerContext, JWT/OAuth auth flow, guards (role/scope/client), conversation ownership, per-user cache isolation, guardrails integration, audit logging, session state, complete multi-user architecture.

---

### [Multi-Agent Coordination](multi-agent-teams.md)

Build systems where multiple agents collaborate — sharing tools, delegating tasks, communicating through events, and coordinating through shared state. Covers all four coordination primitives with complete working examples.

**You'll learn:** Shared MCP servers with per-agent roles, `ask_peer()`/`broadcast()` delegation, EventBus pub/sub, shared context with write permissions, fan-out/fan-in, supervisor pattern, pipeline with quality gates, error handling.

---

## Hands-On Labs

Domain-specific, copy-paste-ready tutorials. Each lab includes a pre-built MCP server, a specialized reasoning pattern, and runnable code.

### [Lab: Customer Support Agent](lab-customer-support.md)

Build a support agent with issue classification, knowledge base search, company policy validation, conversation persistence, and human escalation. Uses a custom Classify → Investigate → Draft → Validate → Respond reasoning pattern.

**You'll build:** CRM tool server, KB search, conversation flows, escalation rules, guardrails.

---

### [Lab: Data Analysis Agent](lab-data-analysis.md)

Build an agent that converts questions into SQL queries, cross-references data across tables, and produces accurate reports. The specialized reasoning pattern scored 8/13 accuracy vs generic ReAct's 5/13 in benchmarks.

**You'll build:** SQL analytics server, Plan → Execute → Observe → Verify → Report pattern, semantic cache.

---

### [Lab: Code Review Agent](lab-code-review.md)

Build an agent that reviews code for security vulnerabilities using adversarial self-critique. The CritiqueNode challenges its own findings. JustifyNode requires specific line references for every claim.

**You'll build:** Code analysis tools, Read → Analyze → Critique → Justify → Synthesize pattern, per-node model override.

---

## Guide Structure

Every guide follows the same progression:

1. **What You'll Build** -- A concrete description of the end result
2. **Concepts** -- The key ideas before any code
3. **Step-by-Step** -- Progressive implementation with working code at each step
4. **Complete Example** -- Full working code you can copy and run
5. **What's Next** -- Links to reference docs for deeper exploration

## Prerequisites

All guides assume:

- Python 3.10+
- `pip install promptise` (or `pip install promptise[full]` for all extras)
- An `OPENAI_API_KEY` environment variable set (or another LLM provider)

See [Installation](../index.md) and [Model Setup](../getting-started/model-setup.md) if you need help getting started.
