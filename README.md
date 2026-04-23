<!-- Hero -->
<div align="center">
  <br/>
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/assets/logo-light.png">
    <img src="docs/assets/logo-dark.png" width="112" alt="Promptise Foundry"/>
  </picture>

  <h1>Promptise Foundry</h1>

  <p>
    <strong>The foundation layer for agentic intelligence.</strong>
  </p>

  <p>
    <em>Every other framework gives you an LLM wrapper.</em><br/>
    <em>Promptise Foundry gives you the stack behind it.</em>
  </p>

  <br/>

  <p>
    <a href="https://pypi.org/project/promptise/"><img alt="PyPI" src="https://img.shields.io/pypi/v/promptise?color=%23a855f7&label=pypi&logo=pypi&logoColor=white"></a>
    <a href="https://pypi.org/project/promptise/"><img alt="Python" src="https://img.shields.io/badge/python-3.10%2B-%233b82f6?logo=python&logoColor=white"></a>
    <a href="https://pypi.org/project/promptise/"><img alt="Downloads" src="https://img.shields.io/pypi/dm/promptise?color=%2322c55e&label=downloads"></a>
    <a href="https://github.com/promptise-com/foundry/actions/workflows/test.yml"><img alt="CI" src="https://github.com/promptise-com/foundry/actions/workflows/test.yml/badge.svg"></a>
    <a href="https://github.com/promptise-com/foundry/blob/main/LICENSE"><img alt="License" src="https://img.shields.io/badge/license-Apache%202.0-%23f59e0b"></a>
    <a href="https://promptise.github.io/foundry"><img alt="Docs" src="https://img.shields.io/badge/docs-latest-%2306b6d4"></a>
  </p>

  <p>
    <img alt="Async" src="https://img.shields.io/badge/100%25-async-%230ea5e9">
    <img alt="Typed" src="https://img.shields.io/badge/mypy-strict-%238b5cf6">
    <img alt="Security" src="https://img.shields.io/badge/bandit-0%20HIGH-%2322c55e">
    <img alt="MCP" src="https://img.shields.io/badge/MCP-native-%23f97316">
    <img alt="Tests" src="https://img.shields.io/badge/tests-3148-%2306b6d4">
  </p>

  <br/>

  <p>
    <a href="https://www.promptise.com"><strong>Website</strong></a>
    &nbsp;·&nbsp;
    <a href="https://promptise.github.io/foundry/"><strong>Documentation</strong></a>
    &nbsp;·&nbsp;
    <a href="https://promptise.github.io/foundry/getting-started/quickstart/"><strong>Quick Start</strong></a>
    &nbsp;·&nbsp;
    <a href="https://promptise.github.io/foundry/resources/showcase/"><strong>Showcase</strong></a>
    &nbsp;·&nbsp;
    <a href="https://github.com/promptise-com/foundry/discussions"><strong>Discussions</strong></a>
  </p>

  <br/>
</div>

<hr/>

<br/>

<div align="center">

<h3>Agents that survive production need more than a prompt and a tool list.</h3>

</div>

They need MCP-native tool discovery. A reasoning engine you can shape. Memory you can trust. Guardrails that actually fire. Governance that enforces budgets. A runtime that recovers from crashes. Promptise Foundry ships all of it as one coherent framework — built for engineering teams who are done assembling AI infrastructure from ten half-finished libraries.

<br/>

## &nbsp;

<br/>

<div align="center">
  <h2>Get started in 30 seconds</h2>
</div>

<br/>

```bash
pip install promptise
```

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

<br/>

<div align="center">
<sub>
One call. Auto tool discovery from MCP servers. Memory auto-searched before every invocation.<br/>
Guardrails block injection and redact PII. Semantic cache serves similar queries instantly. Full observability.
</sub>
</div>

<br/>

## &nbsp;

<br/>

<div align="center">
  <h2>Five pillars. One framework.</h2>
  <p><sub>Each pillar replaces an entire category of libraries you would otherwise assemble yourself.</sub></p>
</div>

<br/>

<table align="center">
<tr>
<td width="80" align="center" valign="top">
<br/>
<h1>01</h1>
<sub>🤖</sub>
</td>
<td valign="top">

### Agent

Turn any LLM into a production-ready agent with one function call.

*Replaces:* LangChain + a guardrails library + an output validator + a vector-store wrapper + a retry helper.

`build_agent()` · auto MCP tool discovery · semantic tool optimization (40–70% fewer tokens) · 3 memory providers with auto-injection · 4 conversation stores · 6-head security scanner · semantic cache with per-user isolation · sandboxed code execution · auto-approval classifier · pluggable RAG · streaming · model fallback · adaptive strategy.

[Agent docs →](https://promptise.github.io/foundry/core/agents/building-agents/)

</td>
</tr>
<tr><td colspan="2"><br/></td></tr>
<tr>
<td width="80" align="center" valign="top">
<br/>
<h1>02</h1>
<sub>🧠</sub>
</td>
<td valign="top">

### Reasoning Engine

Compose reasoning the way you compose code. Not a black box.

*Replaces:* hand-rolled LangGraph wiring, bespoke planner/executor loops, ReAct-from-scratch.

`PromptGraph` with **20 node types** — 10 standard (`PromptNode`, `ToolNode`, `RouterNode`, `GuardNode`, `ParallelNode`, `LoopNode`, `HumanNode`, `TransformNode`, `SubgraphNode`, `AutonomousNode`) and 10 reasoning (`ThinkNode`, `PlanNode`, `ReflectNode`, `CritiqueNode`, `SynthesizeNode`, `ValidateNode`, `ObserveNode`, `JustifyNode`, `RetryNode`, `FanOutNode`). **7 prebuilt patterns** (`react`, `peoatr`, `research`, `autonomous`, `deliberate`, `debate`, `pipeline`). **18 node flags** for typed capabilities. Agent-assembled paths from a node pool. Lifecycle hooks. Skill registry. JSON serialization.

[Reasoning docs →](https://promptise.github.io/foundry/core/engine/)

</td>
</tr>
<tr><td colspan="2"><br/></td></tr>
<tr>
<td width="80" align="center" valign="top">
<br/>
<h1>03</h1>
<sub>🔧</sub>
</td>
<td valign="top">

### MCP Server SDK

Production server and native client for the Model Context Protocol.

*Replaces:* rolling your own tool server. What FastAPI is to REST, this is to MCP.

`@server.tool()` with auto-schema from type hints · JWT + OAuth2 + API key auth · role/scope guards · **12+ middleware** (rate limit, circuit breaker, audit, cache, OTel) · HMAC-chained audit logs · priority job queue with retries and progress · versioning + transforms · OpenAPI import · `MCPMultiClient` federation · live 6-tab dashboard · `TestClient` for in-process testing · **3 transports** (stdio, HTTP, SSE).

[MCP docs →](https://promptise.github.io/foundry/mcp/)

</td>
</tr>
<tr><td colspan="2"><br/></td></tr>
<tr>
<td width="80" align="center" valign="top">
<br/>
<h1>04</h1>
<sub>⚡</sub>
</td>
<td valign="top">

### Agent Runtime

The operating system for autonomous agents.

*Replaces:* Celery + cron + a state store + your own crash recovery + a governance layer.

**5 trigger types** (cron, webhook, file watch, event, message) · crash recovery via journal replay · **5 rewind modes** · 14 lifecycle hooks · budget enforcement with tool costs · health monitoring (stuck, loop, empty, error rate) · mission tracking with LLM-as-judge · secret scoping with TTL and zero-fill revocation · **14 meta-tools** for self-modifying agents · **37-endpoint REST API** with typed client · live agent inbox · distributed multi-node coordination.

[Runtime docs →](https://promptise.github.io/foundry/runtime/)

</td>
</tr>
<tr><td colspan="2"><br/></td></tr>
<tr>
<td width="80" align="center" valign="top">
<br/>
<h1>05</h1>
<sub>✨</sub>
</td>
<td valign="top">

### Prompt Engineering

Prompts built like software. Not strings.

*Replaces:* f-strings + `instructor` + ad-hoc few-shot files + prompt sprawl across a codebase.

**8 block types** with priority-based token budgeting · conversation flows that evolve per phase · **5 composable strategies** (`chain_of_thought + self_critique`) · 4 perspectives · **14 context providers** auto-injected every turn · SSTI-safe template engine with opt-in shell · 5 guards · SemVer registry with rollback · inspector that traces every assembly decision · test helpers (`mock_llm()`, `assert_schema()`) · `chain`, `parallel`, `branch`, `retry`, `fallback`.

[Prompts docs →](https://promptise.github.io/foundry/prompting/)

</td>
</tr>
</table>

<br/>

## &nbsp;

<br/>

<div align="center">
  <h2>Why Promptise Foundry?</h2>
  <p><sub>Honest comparison. ✅ native &nbsp;·&nbsp; ⚠️ partial or via adapter &nbsp;·&nbsp; ❌ not supported</sub></p>
</div>

<br/>

<div align="center">

| | **Promptise** | LangChain | LangGraph | CrewAI | AutoGen | PydanticAI |
|---|:---:|:---:|:---:|:---:|:---:|:---:|
| **MCP-first tool discovery** | ✅ Native | ⚠️ via adapter | ⚠️ via adapter | ⚠️ via adapter | ⚠️ via adapter | ⚠️ via adapter |
| **Native MCP server SDK** (auth · middleware · queue · audit) | ✅ Full | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Composable reasoning graph** | ✅ 20 nodes · 7 patterns · agent-assembled | ❌ | ✅ Graph-native | ⚠️ Crew/Flow | ⚠️ GroupChat | ❌ |
| **Semantic tool optimization** (ML selects relevant tools per query) | ✅ 40–70% savings | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Local ML security guardrails** (prompt-injection · PII · creds · NER · content) | ✅ 6 heads | ❌ external | ❌ external | ❌ | ❌ | ❌ |
| **Semantic response cache** | ✅ Per-user isolated | ⚠️ Basic (shared) | ⚠️ via LangChain | ❌ | ❌ | ❌ |
| **Human-in-the-loop** | ✅ 3 handlers + ML classifier | ⚠️ Basic | ✅ interrupt_before/after | ⚠️ `human_input=True` | ✅ UserProxyAgent | ❌ |
| **Sandboxed code execution** | ✅ Docker · seccomp · gVisor | ⚠️ PythonREPL | ❌ | ❌ | ✅ Docker executor | ❌ |
| **Crash recovery / replay** | ✅ 5 rewind modes | ❌ | ✅ Checkpointer | ❌ | ❌ | ❌ |
| **Autonomous runtime** (triggers · lifecycle · messaging) | ✅ Full OS | ❌ | ⚠️ Persistence only | ❌ | ❌ | ❌ |
| **Budget / health / mission governance** | ✅ Built-in | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Live agent conversation** (inbox · ask) | ✅ | ❌ | ❌ | ❌ | ❌ | ❌ |
| **Orchestration REST API** | ✅ 37 endpoints + typed client | ❌ | ❌ | ❌ | ❌ | ❌ |

</div>

<br/>

<div align="center">
<sub>
LangGraph's checkpointer gives it genuine replay; AutoGen ships a real Docker code executor; LangChain has a basic semantic cache.<br/>
Promptise unifies every row above — one dependency, one type-checked API, one runtime.
</sub>
</div>

<br/>

## &nbsp;

<br/>

<div align="center">
  <h2>Model-agnostic</h2>
  <p><sub>Any LLM, one string. Or any LangChain <code>BaseChatModel</code>. Or a <code>FallbackChain</code> across providers.</sub></p>
</div>

<br/>

```python
build_agent(model="openai:gpt-5-mini", ...)
build_agent(model="anthropic:claude-sonnet-4-20250514", ...)
build_agent(model="ollama:llama3", ...)
build_agent(model="google:gemini-2.0-flash", ...)
```

<br/>

## &nbsp;

<br/>

<div align="center">
  <h2>Deploy autonomous agents</h2>
  <p><sub>Triggers, budgets, health checks, missions, secrets — all in Python.</sub></p>
</div>

<br/>

```python
from promptise.runtime import (
    AgentRuntime, ProcessConfig, TriggerConfig,
    BudgetConfig, HealthConfig, MissionConfig,
)

async with AgentRuntime() as runtime:
    await runtime.add_process("monitor", ProcessConfig(
        model="openai:gpt-5-mini",
        instructions="Monitor data pipelines. Escalate anomalies.",
        triggers=[
            TriggerConfig(type="cron", cron_expression="*/5 * * * *"),
            TriggerConfig(type="webhook", webhook_path="/alerts"),
        ],
        budget=BudgetConfig(max_tool_calls_per_day=500, on_exceeded="pause"),
        health=HealthConfig(detect_loops=True, detect_stuck=True, on_anomaly="escalate"),
        mission=MissionConfig(
            objective="Keep uptime above 99.9%",
            success_criteria="No P1 unresolved for more than 15 minutes",
            evaluate_every_n=10,
        ),
    ))
    await runtime.start_all()
```

<br/>

## &nbsp;

<br/>

<div align="center">
  <h2>Documentation</h2>
</div>

<br/>

<div align="center">

| Section | What it covers |
|---------|---------------|
| [**Quick Start**](https://promptise.github.io/foundry/getting-started/quickstart/) | Your first agent in 5 minutes |
| [**Key Concepts**](https://promptise.github.io/foundry/getting-started/concepts/) | Architecture, design principles, the five pillars |
| [**Building Agents**](https://promptise.github.io/foundry/guides/building-agents/) | Step-by-step, simple to production |
| [**Reasoning Engine**](https://promptise.github.io/foundry/core/engine/) | Graphs, nodes, flags, patterns |
| [**MCP Servers**](https://promptise.github.io/foundry/guides/production-mcp-servers/) | Production tool servers with auth and middleware |
| [**Agent Runtime**](https://promptise.github.io/foundry/guides/agentic-runtime/) | Autonomous agents with governance |
| [**Prompt Engineering**](https://promptise.github.io/foundry/prompting/) | Blocks, strategies, flows, guards |
| [**Showcase**](https://promptise.github.io/foundry/resources/showcase/) | Working patterns, end-to-end |
| [**API Reference**](https://promptise.github.io/foundry/api/agent/) | Every class, method, parameter |

</div>

<br/>

## &nbsp;

<br/>

<div align="center">
  <h2>Ecosystem</h2>
  <p><sub>Promptise plugs into what your team already runs.</sub></p>
</div>

<br/>

<div align="center">

#### &nbsp;&nbsp;Models&nbsp;&nbsp;

<a href="https://openai.com"><img alt="OpenAI" src="https://img.shields.io/badge/OpenAI-412991?style=for-the-badge&logo=openai&logoColor=white"></a>
<a href="https://www.anthropic.com"><img alt="Anthropic" src="https://img.shields.io/badge/Anthropic-D97757?style=for-the-badge&logo=anthropic&logoColor=white"></a>
<a href="https://ai.google.dev"><img alt="Gemini" src="https://img.shields.io/badge/Gemini-4285F4?style=for-the-badge&logo=googlegemini&logoColor=white"></a>
<a href="https://ollama.com"><img alt="Ollama" src="https://img.shields.io/badge/Ollama-000000?style=for-the-badge&logo=ollama&logoColor=white"></a>
<a href="https://mistral.ai"><img alt="Mistral" src="https://img.shields.io/badge/Mistral-FA520F?style=for-the-badge&logoColor=white"></a>
<a href="https://huggingface.co"><img alt="Hugging Face" src="https://img.shields.io/badge/Hugging%20Face-FFD21E?style=for-the-badge&logo=huggingface&logoColor=black"></a>

<sub>+ any LangChain <code>BaseChatModel</code> · <code>FallbackChain</code> for automatic failover</sub>

<br/><br/>

#### &nbsp;&nbsp;Memory &amp; Vectors&nbsp;&nbsp;

<a href="https://www.trychroma.com"><img alt="ChromaDB" src="https://img.shields.io/badge/ChromaDB-FF6B6B?style=for-the-badge&logoColor=white"></a>
<a href="https://mem0.ai"><img alt="Mem0" src="https://img.shields.io/badge/Mem0-111111?style=for-the-badge&logoColor=white"></a>
<a href="https://www.sbert.net"><img alt="Sentence Transformers" src="https://img.shields.io/badge/Sentence--Transformers-EE4C2C?style=for-the-badge"></a>

<sub>Local embeddings · air-gapped model paths · prompt-injection mitigation built in</sub>

<br/><br/>

#### &nbsp;&nbsp;Conversation Storage&nbsp;&nbsp;

<a href="https://www.postgresql.org"><img alt="PostgreSQL" src="https://img.shields.io/badge/PostgreSQL-4169E1?style=for-the-badge&logo=postgresql&logoColor=white"></a>
<a href="https://redis.io"><img alt="Redis" src="https://img.shields.io/badge/Redis-DC382D?style=for-the-badge&logo=redis&logoColor=white"></a>
<a href="https://sqlite.org"><img alt="SQLite" src="https://img.shields.io/badge/SQLite-003B57?style=for-the-badge&logo=sqlite&logoColor=white"></a>

<sub>Session ownership enforced · per-user isolation for cache and guardrails</sub>

<br/><br/>

#### &nbsp;&nbsp;Observability&nbsp;&nbsp;

<a href="https://opentelemetry.io"><img alt="OpenTelemetry" src="https://img.shields.io/badge/OpenTelemetry-425CC7?style=for-the-badge&logo=opentelemetry&logoColor=white"></a>
<a href="https://prometheus.io"><img alt="Prometheus" src="https://img.shields.io/badge/Prometheus-E6522C?style=for-the-badge&logo=prometheus&logoColor=white"></a>
<a href="https://slack.com"><img alt="Slack" src="https://img.shields.io/badge/Slack-4A154B?style=for-the-badge&logo=slack&logoColor=white"></a>
<a href="https://www.pagerduty.com"><img alt="PagerDuty" src="https://img.shields.io/badge/PagerDuty-06AC38?style=for-the-badge&logo=pagerduty&logoColor=white"></a>

<sub>8 transporters: OTel · Prometheus · Slack · PagerDuty · Webhook · HTML · JSON · Console</sub>

<br/><br/>

#### &nbsp;&nbsp;Sandbox &amp; Infrastructure&nbsp;&nbsp;

<a href="https://www.docker.com"><img alt="Docker" src="https://img.shields.io/badge/Docker-2496ED?style=for-the-badge&logo=docker&logoColor=white"></a>
<a href="https://gvisor.dev"><img alt="gVisor" src="https://img.shields.io/badge/gVisor-4285F4?style=for-the-badge&logoColor=white"></a>
<a href="https://kubernetes.io"><img alt="Kubernetes" src="https://img.shields.io/badge/Kubernetes-326CE5?style=for-the-badge&logo=kubernetes&logoColor=white"></a>
<a href="https://en.wikipedia.org/wiki/Seccomp"><img alt="seccomp" src="https://img.shields.io/badge/seccomp-111111?style=for-the-badge"></a>

<sub>Docker + seccomp + gVisor + capability dropping · Kubernetes-native health probes</sub>

<br/><br/>

#### &nbsp;&nbsp;Protocols&nbsp;&nbsp;

<a href="https://modelcontextprotocol.io"><img alt="MCP" src="https://img.shields.io/badge/MCP-native-F97316?style=for-the-badge"></a>
<a href="https://www.openapis.org"><img alt="OpenAPI" src="https://img.shields.io/badge/OpenAPI-6BA539?style=for-the-badge&logo=openapiinitiative&logoColor=white"></a>
<a href="https://datatracker.ietf.org/doc/html/rfc7519"><img alt="JWT" src="https://img.shields.io/badge/JWT-000000?style=for-the-badge&logo=jsonwebtokens&logoColor=white"></a>
<a href="https://oauth.net/2/"><img alt="OAuth 2.0" src="https://img.shields.io/badge/OAuth%202.0-1E78D4?style=for-the-badge"></a>

<sub>stdio · streamable HTTP · SSE · HMAC-chained audit logs</sub>

</div>

<br/>

---

<br/>

<div align="center">

  [**Contributing**](CONTRIBUTING.md) &nbsp;·&nbsp; [**Security**](SECURITY.md) &nbsp;·&nbsp; [**License: Apache 2.0**](LICENSE)

  <br/>
  <br/>

  <sub>Built by <a href="https://promptise.dev"><strong>Promptise</strong></a></sub>

  <br/>
</div>
