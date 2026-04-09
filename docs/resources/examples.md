# Examples Gallery

Complete, runnable examples demonstrating every capability of Promptise Foundry. All examples use real LLM calls -- no mocks or stubs.

Every example is designed to run end-to-end with just an API key set. The default model across all examples is `openai:gpt-5-mini` (fast, affordable, reliable).

---

## Running Examples

```bash
# 1. Set your API key
export OPENAI_API_KEY=sk-...

# 2. Run any example directly
python examples/prompts/01_blocks_composition.py
python examples/mcp/agent.py
```

Some examples require a running MCP server. Where needed, start the server in a separate terminal first:

```bash
# Terminal 1 -- start the server
python examples/mcp/server.py

# Terminal 2 -- run the client or agent
python examples/mcp/agent.py
```

---

## MCP Server & Client

Build production MCP servers with authentication, middleware, and Pydantic validation, then connect via raw clients or LLM agents.

| File | Description | Difficulty |
|------|-------------|------------|
| `examples/mcp/server.py` | Production MCP server with JWT auth, middleware stack, Pydantic models, caching, routers, background tasks, and live dashboard | Intermediate |
| `examples/mcp/agent.py` | LLM agent connecting to an MCP server with JWT authentication and natural-language tool invocation | Beginner |
| `examples/mcp/client.py` | Raw MCP client with multi-server routing, token acquisition, LangChain tool adapters, and tracing callbacks | Intermediate |

**Quick start:**

```bash
# Terminal 1
python examples/mcp/server.py

# Terminal 2 -- agent (requires OPENAI_API_KEY)
python examples/mcp/agent.py

# Terminal 2 -- or raw client (no LLM required)
python examples/mcp/client.py
```

**What you will learn:**

- Creating an `MCPServer` with `@server.tool()` decorators
- JWT authentication with `AuthMiddleware` and role-based guards
- Pydantic model validation for tool parameters (nested models, constraints)
- `MCPRouter` for grouping tools under prefixes
- `MCPClient` and `MCPMultiClient` for programmatic server access
- `MCPToolAdapter` for converting MCP tools to LangChain `BaseTool` instances

---

## Prompt Engineering

Compose prompts from blocks, evolve them across conversation turns, and enhance them with strategies, guards, and chaining.

| File | Description | Difficulty |
|------|-------------|------------|
| `examples/prompts/01_blocks_composition.py` | Composable prompt blocks, priority-based assembly, conditional blocks, `@blocks` decorator | Beginner |
| `examples/prompts/02_conversation_flow.py` | Multi-phase customer support agent where the system prompt evolves per turn | Intermediate |
| `examples/prompts/04_inspector_debugging.py` | Full prompt tracing with `PromptInspector` -- see blocks included, tokens used, and execution path | Intermediate |
| `examples/prompts/05_full_integration.py` | Both layers combined (blocks + flow + inspector) in a research analysis agent | Advanced |

### Labs

| Directory | Description | Difficulty |
|-----------|-------------|------------|
| `examples/prompts/content_studio_lab/` | AI Content Creation Studio demonstrating prompt blocks, flows, guards, strategies, context providers, chain operators, registry, inspector, and templates -- 9 runnable demos with real LLM calls | Advanced |

**Quick start:**

```bash
python examples/prompts/01_blocks_composition.py

# Flagship lab -- all prompt features in one studio
python examples/prompts/content_studio_lab/main.py
```

**Two-layer architecture:**

```
Layer 2: ConversationFlow     Turn-aware prompt evolution
         |
Layer 1: PromptBlocks         Composable prompt components
```

Each layer is independent. Use one or both depending on your needs.

---

## Agent Runtime

Turn stateless LLM agents into persistent, autonomous processes with triggers, crash recovery, and distributed coordination.

| File | Description | Difficulty |
|------|-------------|------------|
| `examples/runtime/pipeline_watcher.agent` | Declarative `.agent` manifest defining an autonomous pipeline watchdog process | Beginner |
| `examples/runtime/autonomous_agent.agent` | Another agent manifest example for autonomous operation | Beginner |
| `examples/runtime/server.py` | MCP server with pipeline monitoring tools (health checks, metrics, alerts, repairs) | Intermediate |
| `examples/runtime/main.py` | Full runtime API walkthrough covering `AgentProcess`, triggers, context, journal, and distributed coordination | Advanced |

### Labs

| Directory | Description | Difficulty |
|-----------|-------------|------------|
| `examples/runtime/data_pipeline_lab/` | Data Pipeline Monitoring System with 8 examples covering process lifecycle, triggers (cron, event, custom SQS), journal system (InMemory, File, ReplayEngine), AgentContext, ConversationBuffer, multi-process AgentRuntime, `.agent` manifest loading, and open mode with meta-tools -- all with real LLM calls | Advanced |

**Quick start:**

```bash
# Terminal 1 -- start the MCP server (tools for the agent)
python examples/runtime/server.py

# Terminal 2 -- run the full walkthrough
python examples/runtime/main.py

# Flagship lab -- all runtime features in one pipeline monitoring system
# Terminal 1 -- start the lab MCP server
python examples/runtime/data_pipeline_lab/tools_server.py
# Terminal 2 -- run the lab
python examples/runtime/data_pipeline_lab/main.py

# Or use the CLI directly
promptise runtime validate examples/runtime/pipeline_watcher.agent
promptise runtime start examples/runtime/pipeline_watcher.agent
```

**What you will learn:**

- Defining agent processes with `.agent` YAML manifests
- `AgentProcess` lifecycle: CREATED, STARTING, RUNNING, SUSPENDED, STOPPED
- Triggers: `CronTrigger`, `WebhookTrigger`, `FileWatchTrigger`, `EventTrigger`, `MessageTrigger`
- `AgentContext` for unified state, environment variables, and file mounts
- `Journal` and `ReplayEngine` for crash recovery
- `AgentRuntime` for managing multiple agent processes
- Open mode with meta-tools for self-modifying agents
- Distributed coordination with `RuntimeTransport` and `RuntimeCoordinator`

