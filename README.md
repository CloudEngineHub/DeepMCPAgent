<!-- Banner -->
<div align="center">
  <img src="docs/assets/logo.png" width="120" alt="Promptise Foundry"/>

  <h1>Promptise Foundry</h1>
  <h3>The production framework for agentic AI systems.</h3>

  <p>
    <a href="https://pypi.org/project/promptise/"><img alt="PyPI" src="https://img.shields.io/pypi/v/promptise?color=%23a855f7&label=pypi"></a>
    <a href="https://pypi.org/project/promptise/"><img alt="Python" src="https://img.shields.io/pypi/pyversions/promptise?color=%233b82f6"></a>
    <a href="https://pypi.org/project/promptise/"><img alt="Downloads" src="https://img.shields.io/pypi/dm/promptise?color=%2322c55e&label=downloads"></a>
    <a href="https://github.com/promptise-com/foundry/actions/workflows/test.yml"><img alt="CI" src="https://github.com/promptise-com/foundry/actions/workflows/test.yml/badge.svg"></a>
    <a href="https://github.com/promptise-com/foundry/blob/main/LICENSE"><img alt="License" src="https://img.shields.io/badge/license-Apache%202.0-%23f59e0b"></a>
    <a href="https://promptise.github.io/foundry"><img alt="Docs" src="https://img.shields.io/badge/docs-latest-%2306b6d4"></a>
  </p>
  <p>
    <a href="https://github.com/promptise-com/foundry/stargazers"><img alt="Stars" src="https://img.shields.io/github/stars/promptise-com/foundry?style=social"></a>
    <a href="https://github.com/promptise-com/foundry/discussions"><img alt="Discussions" src="https://img.shields.io/github/discussions/promptise-com/foundry?color=%238b5cf6"></a>
  </p>

  <p>
    <a href="https://promptise.github.io/foundry/">Documentation</a> &bull;
    <a href="https://promptise.github.io/foundry/getting-started/quickstart/">Quick Start</a> &bull;
    <a href="https://promptise.github.io/foundry/resources/showcase/">What You Can Build</a> &bull;
    <a href="https://github.com/promptise-com/foundry/discussions">Discussions</a>
  </p>
</div>

<hr/>

<br/>

Agentic AI will transform every industry. Building it requires more than wrappers and demos. Promptise Foundry gives engineering teams the complete framework for production agentic systems — from intelligent agents and secure tool infrastructure to autonomous agent operations and prompts built like software.

<br/>

## Install

```bash
pip install promptise
```

<br/>

## One function. Production-ready agent.

```python
import asyncio
from promptise import build_agent, PromptiseSecurityScanner, SemanticCache
from promptise.config import HTTPServerSpec
from promptise.memory import ChromaProvider

async def main():
    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={
            "tools": HTTPServerSpec(url="http://localhost:8000/mcp"),
        },
        instructions="You are a helpful assistant.",
        memory=ChromaProvider(persist_directory="./memory"),
        guardrails=PromptiseSecurityScanner.default(),
        cache=SemanticCache(),
        observe=True,
    )

    result = await agent.ainvoke({
        "messages": [{"role": "user", "content": "What's the status of our pipeline?"}]
    })
    print(result["messages"][-1].content)
    await agent.shutdown()

asyncio.run(main())
```

The agent discovers tools from MCP servers automatically. Memory recalls relevant context before every invocation. Guardrails block injection attacks and redact PII. Cache serves similar queries instantly. Observability tracks every decision. **One function call. All of it.**

<br/>

---

<br/>

## Four pillars. One framework.

<br/>

<table>
<tr>
<td width="50%" valign="top">

### 🤖 Agent

Turn any LLM into a production-ready agent with a single function call.

- **Auto tool discovery** from MCP servers
- **Semantic optimization** — 40-70% fewer tokens
- **3 memory providers** with auto-injection
- **RAG foundation** — pluggable loader/chunker/embedder/store pipeline
- **Multi-user conversations** (Postgres, SQLite, Redis)
- **Security guardrails** — 6 detection heads, all local
- **Semantic cache** — 30-50% cost savings
- **AutoApproval classifier** — 5-layer decision hierarchy for tool calls
- **Event notifications** to Slack, PagerDuty, webhooks
- **Streaming** with real-time tool visibility
- **Model fallback** across providers
- **Adaptive strategy** — learns from failures

[Agent docs →](https://promptise.github.io/foundry/core/agents/building-agents/)

</td>
<td width="50%" valign="top">

### 🔧 MCP

Production server and native client for the Model Context Protocol.

- **`@server.tool()`** — auto-schema from type hints
- **JWT + OAuth** authentication + role/scope authorization
- **Typed RequestContext** per call — client, claims, session state
- **12+ middleware types** — rate limit, circuit breaker, audit, cache
- **HMAC-chained audit logs** — tamper-evident
- **Job queue** with priority, retries, progress, cancellation
- **MCPMultiClient** — federate N servers into one tool suite
- **Live dashboard** — 6-tab terminal UI
- **OpenAPI import** — existing REST → MCP tools
- **3 transports** — stdio, HTTP, SSE

[MCP docs →](https://promptise.github.io/foundry/mcp/)

</td>
</tr>
<tr>
<td width="50%" valign="top">

### ⚡ Agent Runtime

The operating system for autonomous agents.

- **5 trigger types** — cron, webhook, file watch, event, message
- **Crash recovery** — journal checkpoint + replay
- **Multi-granularity rewind** — 5 modes (full, conversation, code, summarize, preview)
- **14 lifecycle hooks** — `once: true`, priority, ShellHook for external scripts
- **Budget enforcement** — per-run and daily limits
- **Health monitoring** — stuck, loop, empty, error rate
- **Mission tracking** — LLM-as-judge evaluation
- **Secret scoping** — TTL, zero-fill revocation
- **14 meta-tools** — self-modifying agents with guardrails
- **37-endpoint REST API** — manage agents without code
- **Distributed** — multi-node over HTTP

[Runtime docs →](https://promptise.github.io/foundry/runtime/)

</td>
<td width="50%" valign="top">

### 🧠 Prompt Engineering

Prompts built like software. Not strings.

- **8 block types** with priority-based budgeting
- **Conversation flows** — prompts that evolve per phase
- **5 composable strategies** — chain + self-critique
- **Shell context injection** — opt-in `!`cmd`` in templates
- **Path-scoped skills** — activate by directory glob
- **11 context providers** — auto-inject everything
- **Inspector** — trace every assembly decision
- **Version control** — SemVer registry + rollback
- **YAML loader** — `.prompt` files in git
- **Testing** — `mock_llm()`, `assert_schema()`
- **Guards** — content filter, length, JSON schema

[Prompts docs →](https://promptise.github.io/foundry/prompting/)

</td>
</tr>
</table>

<br/>

---

<br/>

## Why Promptise Foundry?

<br/>

| | Promptise | LangChain | CrewAI | AutoGen |
|---|---|---|---|---|
| MCP-first tool discovery | ✅ Native | ❌ Manual | ❌ | ❌ |
| Semantic tool optimization | ✅ 40-70% savings | ❌ | ❌ | ❌ |
| Security guardrails (ML + regex) | ✅ 6 heads, local | ❌ | ❌ | ❌ |
| Semantic response cache | ✅ Per-user isolated | ❌ | ❌ | ❌ |
| Human-in-the-loop approval | ✅ 3 handlers | ❌ | ❌ | ❌ |
| Autonomous agent runtime | ✅ Full OS | ❌ | ❌ Limited | ❌ |
| Budget + health governance | ✅ Built-in | ❌ | ❌ | ❌ |
| Mission-oriented execution | ✅ LLM-as-judge | ❌ | ❌ | ❌ |
| Live agent conversation | ✅ Inbox + ask | ❌ | ❌ | ❌ |
| 37-endpoint orchestration API | ✅ + typed client | ❌ | ❌ | ❌ |
| Native MCP server + client | ✅ Auth, middleware, audit | ❌ | ❌ | �� |
| RAG foundation | ✅ Pluggable pipeline | ❌ Built-in | �� | ❌ |
| Auto-approval classifier | ✅ 5-layer hierarchy | ❌ | ❌ | ❌ |

<br/>

---

<br/>

## Model support

Any LLM provider, one string:

```python
build_agent(model="openai:gpt-5-mini", ...)
build_agent(model="anthropic:claude-sonnet-4-20250514", ...)
build_agent(model="ollama:llama3", ...)
build_agent(model="google:gemini-2.0-flash", ...)
```

Or pass any LangChain `BaseChatModel` directly. Or use `FallbackChain` for automatic provider failover.

<br/>

---

<br/>

## Deploy autonomous agents

```python
from promptise.runtime import AgentRuntime, ProcessConfig, TriggerConfig, BudgetConfig

async with AgentRuntime() as runtime:
    await runtime.add_process("monitor", ProcessConfig(
        model="openai:gpt-5-mini",
        instructions="Monitor data pipelines. Escalate anomalies.",
        triggers=[TriggerConfig(type="cron", cron_expression="*/5 * * * *")],
        budget=BudgetConfig(max_tool_calls_per_day=500, on_exceeded="pause"),
    ))
    await runtime.start_all()
```

Or define everything in YAML:

```yaml
model: openai:gpt-5-mini
instructions: Monitor data pipelines. Escalate anomalies.
triggers:
  - type: cron
    expression: "*/5 * * * *"
budget:
  max_tool_calls_per_day: 500
  on_exceeded: pause
```

<br/>

---

<br/>

## Documentation

| Section | What it covers |
|---------|---------------|
| [Quick Start](https://promptise.github.io/foundry/getting-started/quickstart/) | Build your first agent in 5 minutes |
| [Key Concepts](https://promptise.github.io/foundry/getting-started/concepts/) | Architecture, design principles, all 4 pillars |
| [Building Agents](https://promptise.github.io/foundry/guides/building-agents/) | Step-by-step from simple to production |
| [Building MCP Servers](https://promptise.github.io/foundry/guides/production-mcp-servers/) | Production tool servers with auth and middleware |
| [Building Runtime Systems](https://promptise.github.io/foundry/guides/agentic-runtime/) | Autonomous agents with governance |
| [What You Can Build](https://promptise.github.io/foundry/resources/showcase/) | 14 build ideas with working code |
| [API Reference](https://promptise.github.io/foundry/api/agent/) | Every class, method, and parameter |

<br/>

---

<br/>

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for development setup, testing, and submission guidelines.

## Security

See [SECURITY.md](SECURITY.md) for reporting vulnerabilities.

## License

Apache 2.0 — see [LICENSE](LICENSE) for details.

<br/>

<div align="center">
  <p><strong>Built by <a href="https://promptise.dev">Promptise</a></strong></p>
</div>
