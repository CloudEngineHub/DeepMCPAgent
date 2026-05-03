---
title: Promptise Foundry FAQ — Questions about the Python AI agent framework
description: Common questions about Promptise Foundry — installation, MCP support, security, comparison to LangChain and LangGraph, model support, and production readiness.
keywords: Promptise Foundry FAQ, Python AI agent framework, MCP framework, LangChain alternative, LangGraph alternative, agent framework comparison
faq:
  - q: What is Promptise Foundry?
    a: Promptise Foundry is a production-grade Python framework for building AI agents, MCP (Model Context Protocol) servers, prompt engineering systems, and autonomous runtimes. It is secure by default, model-agnostic, MCP-native, and designed for teams shipping agentic AI to production.
  - q: How is Promptise Foundry different from LangChain?
    a: LangChain is a general-purpose LLM toolkit with hundreds of integrations. Promptise Foundry is a focused production framework — it ships one coherent stack covering agents, MCP servers, prompt engineering, and autonomous runtime, with built-in access control, audit trails, sandboxing, and governance. Promptise has fewer abstractions, no fallbacks (errors raise instead of silently degrading), and is async-first.
  - q: How does Promptise Foundry compare to LangGraph?
    a: LangGraph is an orchestration layer on top of LangChain focused on stateful graphs. Promptise Foundry includes an autonomous Agent Runtime with crash recovery, journals, governance, and triggers (cron, events, webhooks, file watch) — built for long-running agents that survive restarts, not just stateful conversations.
  - q: How does Promptise Foundry compare to CrewAI?
    a: CrewAI focuses on multi-agent role-playing setups. Promptise Foundry is broader — it covers single agents, multi-agent coordination via cross-agent delegation, plus the production infrastructure (MCP servers, governance, observability, sandboxing) that real deployments need.
  - q: What is MCP and why does Promptise Foundry use it?
    a: MCP (Model Context Protocol) is the open standard for connecting LLMs to tools, resources, and prompts. Promptise Foundry is MCP-native — agents auto-discover tools from any MCP server, and the framework includes a production SDK for building your own MCP servers with authentication, middleware, rate limiting, and audit logging.
  - q: Which LLM models does Promptise Foundry support?
    a: Any model. Promptise Foundry is model-agnostic. Use a string like "openai:gpt-5-mini", "anthropic:claude-sonnet-4.5", or "ollama:llama3", or pass any LangChain BaseChatModel directly. Switching providers requires changing one string.
  - q: Is Promptise Foundry production-ready?
    a: Yes. The framework is designed for production from day one — no stub implementations, no half-finished features, no NotImplementedError. Built-in features include access control, capability-based policies, audit logging, encrypted transport, sandboxed code execution, observability with 8 transporters (Prometheus, OpenTelemetry, webhook), and crash-recovery via journals.
  - q: How do I install Promptise Foundry?
    a: Install with pip — `pip install promptise`. Python 3.10 or newer is required. Optional extras for memory backends, sandboxing, and observability are available.
  - q: Does Promptise Foundry support self-hosted or local LLMs?
    a: Yes. Use any Ollama model via the "ollama:model-name" string. Local embeddings, local guardrail models (DeBERTa, GLiNER, Llama Guard), and local memory backends (ChromaDB) make air-gapped deployments fully supported.
  - q: Is Promptise Foundry open source?
    a: Yes — Apache 2.0 licensed. The source code is at github.com/promptise-com/foundry.
  - q: Can I run autonomous agents with Promptise Foundry?
    a: Yes. The Agent Runtime turns stateless LLM calls into persistent, governed processes with cron triggers, event triggers, message triggers, webhook triggers, and file-watch triggers. Includes budget governance, health monitoring, mission tracking, and automatic crash recovery from journaled state.
  - q: Does Promptise Foundry handle multi-user access control?
    a: Yes. The MCP server SDK has JWT and asymmetric JWT authentication, capability-based access policies, per-user audit trails, and CallerContext propagation across the entire agent stack. Built for multi-tenant production deployments.
  - q: How does Promptise Foundry handle prompt injection and security?
    a: The framework includes PromptiseSecurityScanner with six detection heads — DeBERTa-based prompt injection detection, PII detection (69 regex patterns), credential detection (96 patterns), GLiNER NER, content safety via Llama Guard or Azure AI, and custom rules. All models run locally. Input blocking and output redaction are built in.
  - q: Where can I find examples?
    a: Runnable examples are at github.com/promptise-com/foundry/tree/main/examples — covering agents, MCP servers, prompt engineering, and runtime use cases. Every example uses real LLM calls, no mocks.
---

# Frequently Asked Questions

Common questions about Promptise Foundry — the production Python framework for AI agents, MCP servers, and autonomous runtimes.

## What is Promptise Foundry?

Promptise Foundry is a production-grade Python framework for building AI agents, MCP (Model Context Protocol) servers, prompt engineering systems, and autonomous runtimes. It is **secure by default, model-agnostic, MCP-native**, and designed for teams shipping agentic AI to production.

## How is Promptise Foundry different from LangChain?

LangChain is a general-purpose LLM toolkit with hundreds of integrations. Promptise Foundry is a focused production framework — it ships one coherent stack covering agents, MCP servers, prompt engineering, and autonomous runtime, with **built-in access control, audit trails, sandboxing, and governance**. Promptise has fewer abstractions, no fallbacks (errors raise instead of silently degrading), and is async-first throughout.

## How does Promptise Foundry compare to LangGraph?

LangGraph is an orchestration layer on top of LangChain focused on stateful graphs. Promptise Foundry includes an **autonomous Agent Runtime** with crash recovery, journals, governance, and triggers (cron, events, webhooks, file watch) — built for long-running agents that survive restarts, not just stateful conversations.

## How does Promptise Foundry compare to CrewAI?

CrewAI focuses on multi-agent role-playing setups. Promptise Foundry is broader — it covers single agents, multi-agent coordination via cross-agent delegation, plus the production infrastructure (MCP servers, governance, observability, sandboxing) that real deployments need.

## What is MCP and why does Promptise Foundry use it?

MCP ([Model Context Protocol](https://modelcontextprotocol.io)) is the open standard for connecting LLMs to tools, resources, and prompts. Promptise Foundry is **MCP-native** — agents auto-discover tools from any MCP server, and the framework includes a production SDK for building your own MCP servers with authentication, middleware, rate limiting, and audit logging.

## Which LLM models does Promptise Foundry support?

**Any model.** Promptise Foundry is model-agnostic. Use a string like `"openai:gpt-5-mini"`, `"anthropic:claude-sonnet-4.5"`, or `"ollama:llama3"`, or pass any LangChain `BaseChatModel` directly. Switching providers requires changing one string.

## Is Promptise Foundry production-ready?

**Yes.** The framework is designed for production from day one — no stub implementations, no half-finished features, no `NotImplementedError`. Built-in features include access control, capability-based policies, audit logging, encrypted transport, sandboxed code execution, observability with 8 transporters (Prometheus, OpenTelemetry, webhook), and crash-recovery via journals.

## How do I install Promptise Foundry?

```bash
pip install promptise
```

Python 3.10 or newer is required. Optional extras for memory backends, sandboxing, and observability are available — see [Installation Extras](getting-started/installation-extras.md).

## Does Promptise Foundry support self-hosted or local LLMs?

**Yes.** Use any Ollama model via the `"ollama:model-name"` string. Local embeddings, local guardrail models (DeBERTa, GLiNER, Llama Guard), and local memory backends (ChromaDB) make **air-gapped deployments** fully supported.

## Is Promptise Foundry open source?

Yes — **Apache 2.0** licensed. The source code is at [github.com/promptise-com/foundry](https://github.com/promptise-com/foundry).

## Can I run autonomous agents with Promptise Foundry?

Yes. The [Agent Runtime](runtime/processes.md) turns stateless LLM calls into persistent, governed processes with cron triggers, event triggers, message triggers, webhook triggers, and file-watch triggers. Includes budget governance, health monitoring, mission tracking, and automatic crash recovery from journaled state.

## Does Promptise Foundry handle multi-user access control?

Yes. The MCP server SDK has JWT and asymmetric JWT authentication, capability-based access policies, per-user audit trails, and `CallerContext` propagation across the entire agent stack. **Built for multi-tenant production deployments.**

## How does Promptise Foundry handle prompt injection and security?

The framework includes `PromptiseSecurityScanner` with six detection heads — DeBERTa-based prompt injection detection, PII detection (69 regex patterns), credential detection (96 patterns), GLiNER NER, content safety via Llama Guard or Azure AI, and custom rules. All models run locally. Input blocking and output redaction are built in.

## Where can I find examples?

Runnable examples are at [github.com/promptise-com/foundry/tree/main/examples](https://github.com/promptise-com/foundry/tree/main/examples) — covering agents, MCP servers, prompt engineering, and runtime use cases. Every example uses real LLM calls, no mocks.

---

## Still have questions?

- [Open an issue on GitHub](https://github.com/promptise-com/foundry/issues)
- [Read the full documentation](index.md)
- [See examples](resources/examples.md)
