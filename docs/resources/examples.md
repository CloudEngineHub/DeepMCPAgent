---
seo_title: "Runnable Examples: Agents, MCP and Runtime"
description: "Runnable Promptise Foundry examples in the repository: tools, MCP servers and clients, MCPcast, identity, memory, reasoning patterns, prompts, runtime and security."
---

# Promptise Foundry Examples

Runnable scripts in the [`examples/`](https://github.com/promptise-com/foundry/tree/main/examples)
directory of the repository. Most make real model calls with `openai:gpt-5-mini`;
the tables say which run without a key, and which need Docker or extras.

## Running examples

```bash
git clone https://github.com/promptise-com/foundry.git
cd foundry
pip install -e ".[all]"
echo "OPENAI_API_KEY=sk-..." > .env    # loaded automatically
python examples/identity/local/app.py
```

Each script's docstring says what it shows and how to run it.

---

## MCP Server & Client

Build production MCP servers with authentication, middleware, and Pydantic validation, then connect via raw clients or LLM agents.

| File | Description | Difficulty |
|------|-------------|------------|
| `examples/mcp/enterprise_server.py` | A server with 30 tools in 6 routers: JWT auth with three role levels, per-tool guards, rate limiting, audit logging, timeouts, circuit breakers and health checks | Intermediate |
| `examples/mcp/enterprise_cli.py` | An interactive agent against `enterprise_server.py` that switches roles, so you see which tools each role may call (`--auto` runs a scripted session) | Intermediate |
| `examples/mcp/queue_server.py` | Background jobs with `MCPQueue`: progress, cancellation and per-client ownership. No LLM needed | Intermediate |
| `examples/mcp/tenancy_and_approval.py` | Server-side multi-tenancy and approval gates in-process with `TestClient`: tenant isolation, roles, four-eyes approval. No API key needed | Advanced |
| `examples/mcp/approve_server_gates.py` | A Promptise client answering a server's approval gate through MCP elicitation: denied without a handler, approved and denied with one. No LLM needed | Intermediate |
| `examples/mcp/client_auth_errors.py` | What a client sees when an MCP server rejects its credentials: a typed `MCPConnectionRejectedError` (401) without and with the wrong API key, then a successful call with the right one. No LLM needed | Beginner |
| `examples/mcp/stream_tool_calls.py` | Stream an agent the way a chat UI receives it: two order lookups starting together and running in parallel (each `tool_end` with its own index and duration), a server `ToolError` streamed as `success: false` with its message, and each answer token by token after the tools | Beginner |
| `examples/mcp/multi_tenant_agent.py` | One agent serving two tenants concurrently over a real HTTP server: each caller's token reaches the server, a token for another service is refused, cached results stay per caller, and each tenant lists only its tools. Runs offline (scripted model) | Intermediate |
| `examples/mcp/http_server_restart.py` | A client that survives a server redeploy: the restarted server forgets the session, the client re-initialises, retries the call and re-discovers the new tool set. Also the `/health` and `/health/ready` probes and the typed 404 for a URL without `/mcp`. No LLM needed | Intermediate |
| `examples/mcp/mcpcast_petstore/run.py` | Generate an MCP server from an OpenAPI spec with `promptise mcpcast`, drive it with a real agent over stdio against the live Petstore API, and show a destructive tool blocked by the server-side approval gate | Intermediate |
| `examples/mcp/mcpcast_storefront_lab/run.py` | A SaaS storefront API MCPcast end to end: generated tools with risk classes, a real agent completing a business task, a refund held by the server-side approval gate until a second human approves, and an Agent Readiness Score before and after fixing the plan | Advanced |
| `examples/mcp/mcpcast_wizard_lab/run.py` | The guided setup as a lab: start a real Helpdesk API, drive `promptise mcpcast` in the terminal (detection, model, profile counts, auth, review), then let the lab prove the result: computed review warnings on what the model wrote, a real agent over MCP stdio against the live app, a write denied by the approval gate, a refund tool that the `standard` profile never generated, and an Agent Readiness Score | Intermediate |
| `examples/mcp/mcpcast_fastapi_app/run.py` | Make your own FastAPI app MCP-ready: start the app, generate an MCP server from its live `/openapi.json` with `promptise mcpcast` (reads open, writes gated, admin/deprecated/health dropped with reasons), drive it with a real agent over MCP stdio, and show a write denied fail-closed then executed once a human approves | Intermediate |

```bash
# Terminal 1: the server
python examples/mcp/enterprise_server.py
# Terminal 2: the agent
python examples/mcp/enterprise_cli.py
```

---

## Agent Identity

Give an agent a stable, traceable identity, and let the resources it calls
cryptographically verify and attribute the caller. The first two run on a laptop
with no cloud and no API key.

| Example | What it demonstrates | Level |
| --- | --- | --- |
| `examples/identity/local/app.py` | A **local identity** (`agent_id`, attribution, `claims()`): the 30-second on-ramp | Beginner |
| `examples/identity/verifiable_mcp/app.py` | The **headline value** end-to-end: agent presents a signed JWT, a server's `JwksAuth` verifies + attributes it (and rejects a wrong-audience token) | Intermediate |
| `examples/identity/github_actions/` | Production OIDC from GitHub Actions (`from_oidc`): exercised in CI against a real token | Intermediate |
| `examples/identity/{aws_lambda,gke_pod,aks_workload,spire}/` | Production credential federation on each platform (`from_aws` / `from_gcp` / `from_entra` / `from_spiffe`) | Advanced |

```bash
python examples/identity/local/app.py
python examples/identity/verifiable_mcp/app.py
```

See [`examples/identity/README.md`](https://github.com/promptise-com/foundry/blob/main/examples/identity/README.md) for the full map and the [Agent Identity docs](../identity/overview.md).

---

## Agents

| File | What it shows | Needs |
|------|---------------|-------|
| `examples/memory/multiuser_chat.py` | `SQLiteConversationStore` sessions owned per user, `ChromaProvider(scope=PER_USER)` memory, `chat(user_id=...)`, memory limits on `build_agent()`, and a CLI that switches users | key, `[all]` |
| `examples/rag/basic_rag.py` | A `RAGPipeline` with your own `DocumentLoader` and `Embedder`, `RecursiveTextChunker` and `InMemoryVectorStore`, exposed to the agent with `rag_to_tool()` | key |
| `examples/adaptive/learning_agent.py` | Adaptive strategy: unreliable tools, failure classification (infrastructure or strategy), lessons that improve later attempts, human corrections | key |
| `examples/approval/auto_classifier.py` | `AutoApprovalClassifier`: deny, ask and allow rules, read-only auto-allow (and a destructive verb that blocks it), a simulated LLM classifier, the human fallback, stats | none |
| `examples/security/financial_agent.py` | Guardrails and approval together: all detection heads, custom rules for large transactions, approval for transfers over 10,000, PII redaction, injection blocking, five scripted scenarios | key, `[all]` |
| `examples/sandbox/code_executor.py` | The Docker sandbox: the agent writes, runs and fixes code for three challenges, with network isolation, resource limits and a read-only root file system | key, Docker |
| `examples/production/demo.py` | The whole stack together: an MCP server with auth, a custom reasoning graph, memory, conversations, semantic cache, guardrails, per-user `CallerContext` and an HTML report (`server.py` is its MCP server) | key, `[all]` |

## Reasoning patterns

| File | What it shows | Needs |
|------|---------------|-------|
| `examples/reasoning/verify_and_managed.py` | The `verify` pattern (plan, solve and check in one turn) and the `managed` pattern (a tool loop with a facts ledger) | key |
| `examples/reasoning/plan_act_verify.py` | A custom `PromptGraph`: `inject_tools=True`, Pydantic `output_schema` for routing, `max_iterations` on a check, and `save_graph` / `load_graph` | key |
| `examples/reasoning/research_agent.py` | A five-node graph with `FanOutNode` parallel research, `ValidateNode`, `ReflectNode`, per-node models and node flags | key |
| `examples/reasoning/code_action_agent.py` | `agent_pattern="code-action"`: the model writes one program over the tools, run in the sandbox | key, Docker |

## Prompt engineering

| File | What it shows | Needs |
|------|---------------|-------|
| `examples/prompts/conversation_flow.py` | A `ConversationFlow` with four phases, blocks per phase, `chain_of_thought + self_critique`, `PromptInspector`, `PromptBuilder` and the version registry | key |

## Projects and `promptise run`

| File | What it shows | Needs |
|------|---------------|-------|
| `examples/run/support_bot/` | A project with `promptise.toml`, an agent factory (`agent.py`) and a program (`main.py`), run with `promptise run`, `--devtools`, `--tui` or `--serve` | key |

```bash
cd examples/run/support_bot
promptise run --devtools
```

## Agent Runtime

| File | What it shows | Needs |
|------|---------------|-------|
| `examples/runtime/pipeline_monitor.py` | An `AgentRuntime` with an event trigger: three severity levels, escalation on critical events, a `FileJournal` and a budget | key |
| `examples/runtime/multi_agent_pipeline.py` | Three agents (researcher, analyst, writer) coordinating through events on a shared MCP server with roles | key |
| `examples/hooks/lifecycle_hooks.py` | `HookManager` hooks: `@hooks.on()`, `once=True`, priorities, changing data through `HookContext`, `ShellHook` and blocking with `HookBlocked` | none |

See [Building Agentic Runtime Systems](../guides/agentic-runtime.md) and the
[Pipeline Observer lab](../guides/lab-pipeline-observer.md) for step-by-step versions.
