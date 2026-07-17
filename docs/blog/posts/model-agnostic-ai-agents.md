---
title: "Model-Agnostic AI Agents: Claude, GPT, Ollama & Local"
description: "Shows that one string swaps the entire model — openai:gpt-5-mini to anthropic:claude-sonnet-4.5 to ollama:llama3 — with zero code rewrites, and that you can…"
keywords: "model-agnostic ai agents, switch llm provider python, run agent with ollama local, swap claude gpt agent, air-gapped llm agent python"
date: 2026-07-16
slug: model-agnostic-ai-agents
categories:
  - Building Agents
---

# Model-Agnostic AI Agents: Claude, GPT, Ollama & Local

Model-agnostic AI agents let you change the model behind your agent without touching the rest of your code. In Promptise Foundry the model is just a string you pass to `build_agent()`, so the same agent — same tools, same memory, same guardrails — can run on `openai:gpt-5-mini` today, `anthropic:claude-sonnet-4.5` tomorrow, and a local `ollama:llama3` in an air-gapped deployment, with no rewrite in between. By the end of this post you'll know exactly how that swap works, how to run entirely offline, and when pinning to one provider is actually the smarter choice.

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## One string swaps the entire model

The whole idea rests on a single design decision: `build_agent()` is a model-agnostic factory. You describe *what* your agent does — instructions, MCP servers, memory — and pass *which* model to run it on as a `"provider:model-name"` string. Everything downstream is provider-neutral.

Here is a complete, runnable script that runs the *same* agent logic across three providers. Only the string changes:

```python
import asyncio
from promptise import build_agent

async def run(model: str) -> str:
    agent = await build_agent(
        model=model,
        instructions="You are a concise research assistant.",
    )
    try:
        result = await agent.ainvoke(
            {"messages": [{"role": "user", "content": "Name one benefit of MCP in one sentence."}]}
        )
        return result["messages"][-1].content
    finally:
        await agent.shutdown()

async def main():
    for model in ("openai:gpt-5-mini", "anthropic:claude-sonnet-4.5", "ollama:llama3"):
        print(model, "->", await run(model))

asyncio.run(main())
```

Notice what *didn't* change: the invocation, the message shape, the tool wiring, the shutdown. When you swap Claude for GPT for a local model, you edit one argument. That is the entire promise of model-agnostic AI agents, and it holds all the way from a toy script to a production agent with sandboxing, semantic caching, and observability enabled. If you're building your first agent from scratch, the [How to Build an AI Agent in Python: The Complete Guide](how-to-build-an-ai-agent-in-python.md) post walks the same factory from zero.

## Switch your LLM provider in Python without a rewrite

Because the string carries the provider, to switch your LLM provider in Python you change a prefix, not an integration. Promptise resolves the string through LangChain's `init_chat_model`, so any provider LangChain supports works out of the box:

| Provider | String | Env variable |
|----------|--------|--------------|
| OpenAI | `openai:gpt-5-mini` | `OPENAI_API_KEY` |
| Anthropic | `anthropic:claude-sonnet-4.5` | `ANTHROPIC_API_KEY` |
| Google | `google:gemini-2.5-pro` | `GOOGLE_API_KEY` |
| Ollama | `ollama:llama3` | _(local, no key)_ |

Set the environment variable for whichever provider you point at, and the agent authenticates itself. To swap a Claude agent for GPT, you flip `anthropic:...` to `openai:...` and make sure `OPENAI_API_KEY` is set — nothing about your tool servers, prompt blocks, or runtime governance moves.

This portability is what makes provider choice a *configuration* decision instead of an *architecture* decision. You can develop against a cheap, fast model, run evals against a frontier model, and ship on whichever one wins — all from the same codebase. The full matrix of supported providers, formats, and environment variables lives in the [Model Setup](../../getting-started/model-setup.md) guide. If you're still deciding which model to standardize on, the [best LLMs for agents](../../getting-started/best-llms-for-agents.md) breakdown compares them on tool-calling reliability, latency, and cost.

## Run an agent with Ollama locally — the air-gapped path

The portability story only matters if it includes the case competitors tend to gloss over: running with no cloud provider at all. To run an agent with Ollama locally, point the model string at an Ollama tag and drop the API key entirely:

```python
import asyncio
from promptise import build_agent

async def main():
    agent = await build_agent(
        model="ollama:llama3",          # served by your local Ollama daemon — no API key
        instructions="You answer strictly from the provided context.",
    )
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Summarize the CAP theorem in two sentences."}]}
    )
    print(result["messages"][-1].content)
    await agent.shutdown()

asyncio.run(main())
```

With Ollama running on the same host, no request leaves the machine. That is the foundation for an air-gapped LLM agent in Python: the model is local, and Promptise's other production features are designed to stay local alongside it. Semantic tool optimization uses local embeddings, the guardrail scanner runs its detection heads locally, and the memory providers can persist to disk — so a full agent stack can operate with zero outbound network calls. This is the deployment mode regulated and offline environments actually need, and it's the same three-line change as any other provider.

Because the abstraction is uniform, you can also mix modes across an agent's lifecycle: prototype against a hosted frontier model for quality, then move the exact same agent behind a local model for a sensitive workload. The agent definition doesn't know or care which side of the network boundary its model lives on.

## Bring your own model: any LangChain BaseChatModel

A provider string is the common case, but sometimes you need finer control — a custom temperature, a private base URL, a proxy, or a self-hosted OpenAI-compatible endpoint. For that, `build_agent()` accepts any LangChain `BaseChatModel` instance directly, so you keep full control of the client while still getting Promptise's tool discovery, memory, and runtime:

```python
import asyncio
from langchain_openai import ChatOpenAI
from promptise import build_agent

async def main():
    llm = ChatOpenAI(model="gpt-5-mini", temperature=0.2, max_tokens=2048)
    agent = await build_agent(
        model=llm,                       # a pre-configured BaseChatModel, not a string
        instructions="You are a careful analyst.",
    )
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "List two risks of unbounded tool loops."}]}
    )
    print(result["messages"][-1].content)
    await agent.shutdown()

asyncio.run(main())
```

The same slot accepts any LangChain chat model or `Runnable`, so a provider LangChain supports but Promptise doesn't shortcut with a string prefix is still one import away. Everything else about building the agent — attaching MCP servers, choosing an agent pattern, enabling guardrails — is identical whether you passed a string or an object, as the [building agents guide](../../guides/building-agents.md) covers end to end.

## When pinning to a single provider is the better fit

Model-agnostic is the right default, but it isn't a religion. Being honest about the trade-off: if you know you'll only ever run on one provider, wiring that provider's SDK directly is a perfectly reasonable choice, and it's the better fit when:

- **You depend on provider-exclusive features.** Prompt caching quirks, extended thinking, provider-specific structured-output modes, or a beta endpoint may not have a portable equivalent yet.
- **You're squeezing the last few points of quality.** Prompts, few-shot examples, and stop sequences that are hand-tuned for one model don't always transfer cleanly to another.
- **Your ops surface is deliberately narrow.** One provider means one bill, one status page, one set of rate limits to reason about.

Promptise doesn't block any of this — you can pass a fully configured `BaseChatModel` and lean on a single provider all you want. The point of the model-agnostic layer is that the *option* to move stays cheap. You get portability insurance without giving up provider-specific power, and you decide later whether to cash it in.

## Frequently asked questions

### How do I switch an agent from Claude to GPT?

Change the model string from `anthropic:claude-sonnet-4.5` to `openai:gpt-5-mini` (and set `OPENAI_API_KEY`). Nothing else in your agent — tools, memory, guardrails, or runtime — needs to change, because `build_agent()` keeps the model provider-neutral.

### Can I run a Promptise agent completely offline?

Yes. Use an `ollama:` model string so inference runs on your local Ollama daemon with no API key, and keep memory and guardrails local. No request leaves the host, which is what an air-gapped deployment requires.

### What if my provider isn't available as a string?

Pass a LangChain `BaseChatModel` instance to the `model` parameter instead of a string. Any chat model or `Runnable` LangChain supports works, so you're never limited to the prefixes Promptise resolves directly.

## Next steps

Follow [Model Setup](../../getting-started/model-setup.md) to point the same agent at OpenAI, Anthropic, or a local model with a one-line change, then keep the [Quick Start](../../getting-started/quickstart.md) open as you build. If you're weighing whether an agent framework is even the right layer for your project, the [What Is a Python AI Agent Framework?](python-ai-agent-framework.md) post frames that decision honestly.
