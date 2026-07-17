---
title: "Build Your First AI Agent with Promptise in 10 Minutes"
description: "The fastest possible on-ramp: pip install, set one API key, and go from empty file to a running tool-using agent in under ten minutes — every step…"
keywords: "build ai agent with promptise, promptise quickstart, promptise tutorial, first ai agent python, pip install promptise"
date: 2026-07-16
slug: build-ai-agent-with-promptise
categories:
  - Building Agents
---

# Build Your First AI Agent with Promptise in 10 Minutes

You can build an AI agent with Promptise in about ten minutes, starting from an empty file and ending with a tool-using agent you can chat with from your terminal. No orchestration boilerplate, no schema wiring, no abstractions to learn before you write the first useful line. This Promptise tutorial walks through every step — install, one API key, a first agent, real tools, and launching it from the CLI — and everything here is copy-pasteable. By the end you'll have a running agent and know exactly which parameter to flip when you're ready for production.

## Install Promptise (pip install promptise)

Promptise is a single package. Install it and set one provider key:

```bash
pip install promptise
export OPENAI_API_KEY=sk-...   # any supported provider works
```

That's the entire setup. `pip install promptise` pulls in the core agent, the native MCP client and server SDK, and the CLI — you don't assemble a stack from a dozen libraries. If you'd rather run Claude, Gemini, a local Ollama model, or an air-gapped checkpoint, the model string is the only thing that changes; the [Model Setup guide](../../getting-started/model-setup.md) lists every provider prefix and the env var each one expects.

## Write your first AI agent in Python

The smallest useful agent is an LLM with instructions. `build_agent()` is the one factory function you need — it handles model initialization, message formatting, and execution, and returns something you can immediately call:

```python
import asyncio
from promptise import build_agent

async def main():
    agent = await build_agent(
        model="openai:gpt-5-mini",
        instructions="You are a helpful assistant. Be concise.",
    )

    result = await agent.ainvoke({
        "messages": [{"role": "user", "content": "What is 42 * 17?"}]
    })
    print(result["messages"][-1].content)   # "42 * 17 = 714"
    await agent.shutdown()

asyncio.run(main())
```

Save it as `agent.py`, run `python agent.py`, and you have a working first AI agent in Python. Notice the shape of the API: everything is `async`, you talk to the agent through `ainvoke()` with a messages list, and you call `shutdown()` when you're done so MCP connections close cleanly. That single pattern scales all the way up — the difference between this toy and a production agent is which parameters you pass to `build_agent()`, not a different framework.

## Give your agent tools with an MCP server

An agent that can only talk is a chatbot. Agents become useful when they can *do* things, and in Promptise that means MCP tools. You don't hand-write JSON schemas — you write a normal typed Python function, decorate it, and the agent discovers it automatically:

```python
import asyncio
import sys
from promptise import build_agent
from promptise.config import StdioServerSpec
from promptise.mcp.server import MCPServer

server = MCPServer("my-tools")

@server.tool()
async def get_weather(city: str) -> str:
    """Get the current weather for a city."""
    return f"Sunny, 22°C in {city}"   # call a real API in production

async def main():
    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={
            "tools": StdioServerSpec(command=sys.executable, args=["tools.py"]),
        },
        instructions="You are a helpful assistant with access to tools.",
    )

    result = await agent.ainvoke({
        "messages": [{"role": "user", "content": "What's the weather in Berlin?"}]
    })
    print(result["messages"][-1].content)
    await agent.shutdown()

if __name__ == "__main__":
    if "--serve" in sys.argv:
        server.run(transport="stdio")   # start the MCP server
    else:
        asyncio.run(main())
```

Save this as `tools.py`. The `@server.tool()` decorator reads the type hints (`city: str -> str`) and the docstring to generate the tool schema; the agent connects to the server, discovers `get_weather`, and calls it when the user's question needs it. Add a second decorated function and it shows up too — there's no registry to maintain and no manual tool-to-LLM plumbing. When a step needs two independent tools, the agent runs those calls in parallel, so more tools rarely means a slower turn. And as your tool count grows, semantic tool optimization can select only the relevant tools per query, which the framework reports cuts tool tokens by 40–70% — you keep adding capabilities without paying for all of them on every request.

## Launch your agent from the CLI: promptise run and promptise serve

You don't have to write a `main()` loop to try things out. The Promptise CLI ships two commands that cover the two most common launch shapes, and this is where the ten-minute path really pays off.

**`promptise run`** starts an interactive chat session backed by your MCP tools — no Python glue at all:

```bash
promptise run --model-id openai:gpt-5-mini \
  --stdio "name=tools command=python args='tools.py --serve'"
```

That spawns `tools.py` as a stdio MCP server, wires its tools into an agent, and drops you at a `>` prompt. Type a question, watch the tool calls, iterate. It's the fastest way to sanity-check a tool server you just wrote.

**`promptise serve`** does the inverse: it hosts an `MCPServer` object so other clients — agents, IDEs, or teammates — can reach it over HTTP:

```bash
promptise serve tools:server --transport http --port 8080 --dashboard
```

The `tools:server` target is `module:attribute`, so it resolves the `server` object inside `tools.py` and serves it. Add `--dashboard` for a live terminal UI, or `--reload` during development to hot-reload on source changes. Between `run` and `serve`, you can go from an idea to a running, reachable agent without leaving the shell.

## Add production features with one parameter each

The reason to start here instead of a bare provider SDK is what happens next. When your prototype needs memory, caching, persistence, security, or observability, you don't rebuild the agent — you add a keyword argument. Each capability is one parameter on the same `build_agent()` call:

```python
from promptise import build_agent, CallerContext
from promptise.memory import ChromaProvider
from promptise.cache import SemanticCache
from promptise.conversations import SQLiteConversationStore

agent = await build_agent(
    model="openai:gpt-5-mini",
    servers=my_servers,
    memory=ChromaProvider(persist_directory="./memory"),   # recall past context
    cache=SemanticCache(),                                  # reuse similar answers
    conversation_store=SQLiteConversationStore("chat.db"),  # persist chat history
    guardrails=True,                                        # block injection, redact PII
    observe=True,                                           # trace every turn & tool call
)

reply = await agent.chat(
    "Analyze last quarter's revenue",
    session_id="s1",
    caller=CallerContext(user_id="analyst-42", roles=["analyst"]),
)
```

The `chat()` method loads the session, invokes the agent, and persists the result for you, while `CallerContext` scopes memory, cache, and guardrails to a specific user. The semantic cache serves responses for similar queries, which the framework reports as a 30–50% cost reduction on repetitive workloads. Guardrails run six local detection heads to block prompt injection and redact PII and secrets, and `observe=True` gives you a timeline of every LLM turn and tool call for debugging. Every feature you don't enable has zero overhead — features are opt-in, not baked into a heavy base class, so the ten-line agent from earlier stays exactly as cheap as it looks. For copy-paste snippets of each of these (auth, approval gates, multi-tenancy, and more), the [Cookbook](../../getting-started/cookbook.md) is the fastest reference.

## When another framework is a better fit

Promptise is opinionated, and that's worth being honest about. If all you need is a single LLM call with no tools, no memory, and no governance, the provider's own SDK is lighter and has one less dependency — reach for Promptise when the agent has to *do* work safely, not just answer once. And if your team has already invested heavily in a specific orchestration ecosystem with a lot of existing graph or chain code, migrating for a greenfield feature may be more churn than it's worth; Promptise interoperates through MCP, so you can expose tools to your current stack instead of rewriting it. Where Promptise wins is the combination this tutorial showed: MCP-native tool discovery, secure-by-default production features behind single parameters, and a CLI that gets you running in minutes. For a deeper, framework-agnostic look at the tradeoffs, see [What Is a Python AI Agent Framework? (And When You Need One)](python-ai-agent-framework.md).

## Frequently asked questions

### How long does it really take to build an AI agent with Promptise?

The first working agent takes a couple of minutes — install, set a key, and run the ten-line script above. Adding a tool server and launching it through `promptise run` fits comfortably inside ten minutes, because there's no schema wiring or manual tool registration to slow you down.

### Do I need an MCP server to use Promptise?

No. A tool-less agent is a valid starting point and is often all a summarization or classification task needs. You add an MCP server only when the agent has to take actions or fetch live data, and even then you write plain typed functions rather than protocol code.

### Can I use a model other than OpenAI?

Yes. The `model` argument is a provider-prefixed string, so `anthropic:claude-sonnet-4.5`, `ollama:llama3`, and others work by changing that one value. See the [Model Setup guide](../../getting-started/model-setup.md) for the full list and the env var each provider expects.

## Next steps

Run `pip install promptise` and follow the Quickstart to ship your first agent today — the [Quick Start](../../getting-started/quickstart.md) expands each step above with runnable variations, and when you're ready to go deeper, [How to Build an AI Agent in Python: The Complete Guide](how-to-build-an-ai-agent-in-python.md) takes you from this ten-minute agent to a production deployment.
