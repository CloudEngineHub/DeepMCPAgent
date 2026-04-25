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
    <em>Ship full-stack agentic systems the way they're meant to be built —</em><br/>
    <em>production-ready, secure by default, with the developer experience modern Python deserves.</em>
  </p>

  <br/>

  <p>
    <a href="https://github.com/promptise-com/foundry/stargazers"><img alt="Stars" src="https://img.shields.io/github/stars/promptise-com/foundry?style=flat&color=%23eab308&label=%E2%98%85%20stars"></a>
    <a href="https://pypi.org/project/promptise/"><img alt="PyPI" src="https://img.shields.io/pypi/v/promptise?color=%23a855f7&label=pypi&logo=pypi&logoColor=white"></a>
    <a href="https://pypi.org/project/promptise/"><img alt="Python" src="https://img.shields.io/badge/python-3.10%2B-%233b82f6?logo=python&logoColor=white"></a>
    <a href="https://pypi.org/project/promptise/"><img alt="Downloads" src="https://img.shields.io/pypi/dm/promptise?color=%2322c55e&label=downloads"></a>
    <a href="https://github.com/promptise-com/foundry/actions/workflows/test.yml"><img alt="CI" src="https://github.com/promptise-com/foundry/actions/workflows/test.yml/badge.svg"></a>
    <a href="https://github.com/promptise-com/foundry/commits/main"><img alt="Last commit" src="https://img.shields.io/github/last-commit/promptise-com/foundry?color=%2306b6d4&label=last%20commit"></a>
    <a href="https://github.com/promptise-com/foundry/blob/main/LICENSE"><img alt="License" src="https://img.shields.io/badge/license-Apache%202.0-%23f59e0b"></a>
    <a href="https://docs.promptise.com"><img alt="Docs" src="https://img.shields.io/badge/docs-latest-%2306b6d4"></a>
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
    <a href="https://docs.promptise.com/"><strong>Documentation</strong></a>
    &nbsp;·&nbsp;
    <a href="https://docs.promptise.com/getting-started/quickstart/"><strong>Quick Start</strong></a>
    &nbsp;·&nbsp;
    <a href="https://docs.promptise.com/resources/showcase/"><strong>Showcase</strong></a>
    &nbsp;·&nbsp;
    <a href="https://github.com/promptise-com/foundry/discussions"><strong>Discussions</strong></a>
  </p>

  <br/>
</div>

<hr/>

<br/>

### One Python framework. The full agentic stack.

<sub>From your first agent to a fleet running in production — without gluing libraries together.</sub>

- **Agents** that discover tools and remember context.
- **MCP servers** with access control, governance, and audit trails — multi-user, scalable, secure by default.
- **Reasoning engine** you can shape, like bricks.
- **Autonomous runtime** that recovers from crashes and stays within budget.

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

`build_agent()` · auto MCP tool discovery · semantic tool optimization (40–70% fewer tokens) · 3 memory providers with auto-injection · 4 conversation stores · 6-head security scanner · semantic cache with per-user isolation · sandboxed code execution · auto-approval classifier · pluggable RAG · streaming · model fallback · adaptive strategy.

[Agent docs →](https://docs.promptise.com/core/agents/building-agents/)

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

`PromptGraph` with **20 node types** — 10 standard (`PromptNode`, `ToolNode`, `RouterNode`, `GuardNode`, `ParallelNode`, `LoopNode`, `HumanNode`, `TransformNode`, `SubgraphNode`, `AutonomousNode`) and 10 reasoning (`ThinkNode`, `PlanNode`, `ReflectNode`, `CritiqueNode`, `SynthesizeNode`, `ValidateNode`, `ObserveNode`, `JustifyNode`, `RetryNode`, `FanOutNode`). **7 prebuilt patterns** (`react`, `peoatr`, `research`, `autonomous`, `deliberate`, `debate`, `pipeline`). **18 node flags** for typed capabilities. Agent-assembled paths from a node pool. Lifecycle hooks. Skill registry. JSON serialization.

[Reasoning docs →](https://docs.promptise.com/core/engine/)

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

Production server and native client for the Model Context Protocol — multi-user, scalable, secure by default.

`@server.tool()` with auto-schema from type hints · JWT + OAuth2 + API-key access control · role/scope guards · **12+ middleware** (rate limit, circuit breaker, audit, cache, OTel) · HMAC-chained audit logs · priority job queue with retries and progress · versioning + transforms · OpenAPI import · `MCPMultiClient` federation · live 6-tab dashboard · `TestClient` for in-process testing · **3 transports** (stdio, HTTP, SSE).

[MCP docs →](https://docs.promptise.com/mcp/)

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

**5 trigger types** (cron, webhook, file watch, event, message) · crash recovery via journal replay · **5 rewind modes** · 14 lifecycle hooks · budget enforcement with tool costs · health monitoring (stuck, loop, empty, error rate) · mission tracking with LLM-as-judge · secret scoping with TTL and zero-fill revocation · **14 meta-tools** for self-modifying agents · **37-endpoint REST API** with typed client · live agent inbox · distributed multi-node coordination.

[Runtime docs →](https://docs.promptise.com/runtime/)

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

**8 block types** with priority-based token budgeting · conversation flows that evolve per phase · **5 composable strategies** (`chain_of_thought + self_critique`) · 4 perspectives · **14 context providers** auto-injected every turn · SSTI-safe template engine with opt-in shell · 5 guards · SemVer registry with rollback · inspector that traces every assembly decision · test helpers (`mock_llm()`, `assert_schema()`) · `chain`, `parallel`, `branch`, `retry`, `fallback`.

[Prompts docs →](https://docs.promptise.com/prompting/)

</td>
</tr>
</table>

<br/>

## &nbsp;

<br/>

## Documentation

<table>
<tr>
<td colspan="2" width="66%" valign="top">

### <a href="https://docs.promptise.com/getting-started/quickstart/">Quick Start →</a>

<sub>Your first agent in 5 minutes. <code>pip install</code>, point at an MCP server, ship.</sub>

</td>
<td valign="top">

### <a href="https://docs.promptise.com/getting-started/concepts/">Key Concepts →</a>

<sub>Architecture, design principles, the five pillars.</sub>

</td>
</tr>

<tr>
<td valign="top">

### <a href="https://docs.promptise.com/guides/building-agents/">Building Agents →</a>

<sub>Step-by-step, simple to production.</sub>

</td>
<td valign="top">

### <a href="https://docs.promptise.com/core/engine/">Reasoning Engine →</a>

<sub>Graphs, nodes, flags, patterns.</sub>

</td>
<td valign="top">

### <a href="https://docs.promptise.com/guides/production-mcp-servers/">MCP Servers →</a>

<sub>Production tool servers with access control, middleware, and audit trails.</sub>

</td>
</tr>

<tr>
<td valign="top">

### <a href="https://docs.promptise.com/guides/agentic-runtime/">Agent Runtime →</a>

<sub>Autonomous agents with governance, triggers, and crash recovery.</sub>

</td>
<td colspan="2" valign="top">

### <a href="https://docs.promptise.com/prompting/">Prompt Engineering →</a>

<sub>Composable blocks, strategies, flows, and guards. Prompts built like software, not strings.</sub>

</td>
</tr>

<tr>
<td colspan="2" valign="top">

### <a href="https://docs.promptise.com/resources/showcase/">Showcase →</a>

<sub>End-to-end working patterns and reference implementations.</sub>

</td>
<td valign="top">

### <a href="https://docs.promptise.com/api/agent/">API Reference →</a>

<sub>Every class, method, parameter.</sub>

</td>
</tr>
</table>

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

## &nbsp;

<br/>

<div align="center">
  <h2>Star history</h2>
  <p><sub>Promptise Foundry is open-source and growing fast. If it saves you time, drop a ⭐ — it genuinely helps.</sub></p>
</div>

<br/>

<div align="center">
  <a href="https://star-history.com/#promptise-com/foundry&Date">
    <picture>
      <source media="(prefers-color-scheme: dark)" srcset="https://api.star-history.com/svg?repos=promptise-com/foundry&type=Date&theme=dark">
      <source media="(prefers-color-scheme: light)" srcset="https://api.star-history.com/svg?repos=promptise-com/foundry&type=Date">
      <img alt="Star History Chart" src="https://api.star-history.com/svg?repos=promptise-com/foundry&type=Date" width="640">
    </picture>
  </a>
</div>

<br/>

<div align="center">
  <sub>
    Want to ship with us? See <a href="CONTRIBUTING.md">CONTRIBUTING.md</a> · join <a href="https://github.com/promptise-com/foundry/discussions">Discussions</a> · file an <a href="https://github.com/promptise-com/foundry/issues/new/choose">issue</a>.
  </sub>
</div>

<br/>

---

<br/>

<div align="center">

  [**Contributing**](CONTRIBUTING.md) &nbsp;·&nbsp; [**Security**](SECURITY.md) &nbsp;·&nbsp; [**License: Apache 2.0**](LICENSE)

  <br/>
  <br/>

  <sub>Built by <a href="https://www.promptise.com"><strong>Promptise</strong></a></sub>

  <br/>
  <br/>

  <sub><sup>Formerly known as <a href="https://github.com/cryxnet/DeepMCPAgent">DeepMCPAgent</a> — a public preview of one sliver of this framework (MCP-native agent tooling). Promptise Foundry is the full system it was a teaser for: reasoning engine, agent runtime, prompt engineering, sandboxed execution, governance, and observability.</sup></sub>

  <br/>
</div>
