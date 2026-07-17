---
title: "Context Window Management for LLM Agents, Explained"
description: "Instead of hand-waving 'just trim your prompt,' this shows exact token counting with tiktoken and priority-based trimming that drops conversation history and…"
keywords: "context window management, LLM context window, token budgeting for LLMs, prompt token limit, context assembly for agents"
date: 2026-07-16
slug: context-window-management
categories:
  - Memory & RAG
---

# Context Window Management for LLM Agents, Explained

Context window management is the difference between an agent that answers the question you asked and one that silently drops it. Every call your agent makes stuffs a system prompt, tool definitions, retrieved memory, and conversation history into a single request — and when that request outgrows the model's window, something has to go. Do nothing and the model either errors out or truncates from wherever the framework happened to stop, which is often the user's latest message. By the end of this post you'll know how to count tokens exactly, assign priorities to every piece of context, and trim gracefully so the parts that matter always survive.

## What context window management actually means

An LLM context window is a hard token ceiling. For `openai:gpt-5-mini` it's 128,000 tokens; for `claude-sonnet-4.5` it's 200,000; for a local `ollama:llama3` it can be as low as 8,192. Everything you send — instructions, tools, memory, history, and the current question — competes for that fixed budget, minus whatever you reserve for the response.

"Context window management" is the discipline of deciding, per request:

- **What goes in** — which layers of context are worth their tokens right now.
- **In what order** — so the model reads the most important material first.
- **What gets cut** — when the total exceeds the budget, and in which order.

The naive answer ("just trim your prompt") skips the only question that matters: *trim what, first?* Get that ordering wrong and you drop the user's question to preserve a stale chat log from ten turns ago.

## Why "just trim your prompt" quietly fails

Most agent stacks assemble context ad hoc. Memory injects its results, the prompt layer injects its blocks, conversation history appends itself, and tool schemas get bolted on — each one independently, none of them aware of the others or of the token budget. That works fine until the day a long conversation plus a fat retrieval result pushes you past the ceiling. Then one of two things happens:

1. The provider rejects the request with a context-length error and your agent 500s.
2. A middleware truncates blindly — usually from the tail — and the model answers a question it can no longer see.

Both are silent failures from the user's perspective. The agent doesn't say "I ran out of room"; it just gets subtly, confidently wrong. Effective **token budgeting for LLMs** replaces that guesswork with an explicit priority order, so trimming is a deliberate policy instead of an accident of insertion order.

## Count tokens exactly before you budget

You can't manage a budget you can't measure, and character counts are not tokens. Promptise Foundry's Context Engine counts exactly with `tiktoken` for OpenAI models and falls back to a well-calibrated character estimate (chars ÷ 3.5, ~90% accurate) for everyone else. You can also plug in your own tokenizer for a model it doesn't recognize.

Exact counting is what lets the engine enforce a real **prompt token limit** rather than a hopeful one. It knows the model's window, subtracts a configurable `response_reserve`, and treats the remainder as the budget every layer must fit inside:

- **GPT-4o / GPT-5 family** → 128K window, `tiktoken` exact counts.
- **Claude 3/4** → 200K window, estimated counts.
- **Llama 3** → 8K window, estimated counts.

That 8K case is where budgeting stops being optional. On a small-context model, one verbose retrieval result can eat your entire window, and you need the engine to make room predictably.

## Priority-based context assembly for agents

This is where the Context Engine earns its place. It registers **13 priority-ranked layers** and assembles them in a single pass, trimming the lowest-priority non-required layers first when the budget is tight. The ranking encodes an opinion you'd otherwise have to hand-code every time:

| Priority | Layer | Trimmed? |
|----------|-------|----------|
| 10 | `identity`, `user_message` | Never (required) |
| 9 | `tools` | Never (required) |
| 8 | `prompt_blocks`, `output_format` | If needed |
| 7 | Your custom layers | If needed |
| 3 | `memory` (recall) | Early |
| 2 | `strategies` | Earlier |
| 1 | `conversation` (history) | First — oldest pairs go first |

The user's current message and the agent's identity sit at priority 10 and are marked required, so they are **never** dropped. Conversation history sits at priority 1, so a long back-and-forth is pruned oldest-first — preserving user/assistant pairs — long before the engine touches the question in front of it. That's the whole point: the agent stops silently truncating the one message that matters.

Wiring it up is one parameter on `build_agent()`. Here's a complete, runnable example that adds a required custom layer and prints per-layer token accounting for the call:

```python
import asyncio
from promptise import build_agent, ContextEngine


async def main():
    # Model-aware budget: auto-detects the 128K window for gpt-5-mini,
    # reserves room for the response, and counts tokens with tiktoken.
    engine = ContextEngine(model="openai:gpt-5-mini", response_reserve=4096)

    # A high-priority company rule that must survive every trim.
    engine.add_layer(
        "company_policy",
        priority=7,
        content="Answer only from approved docs. Never reveal internal pricing.",
        required=True,
    )

    agent = await build_agent(
        model="openai:gpt-5-mini",
        instructions="You are a concise support agent.",
        context_engine=engine,
    )

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "How do I reset my password?"}]}
    )
    print(result["messages"][-1].content)

    # Per-layer token accounting for the call you just made.
    report = engine.get_report()
    if report:
        print(f"Used {report.total_tokens}/{report.budget} tokens "
              f"({report.utilization:.0%})")
        print("Trimmed:", report.trimmed_layers or "nothing")

    await agent.shutdown()


asyncio.run(main())
```

The engine is completely opt-in — leave `context_engine` off and the agent uses its default injection path. Turn it on when you need guaranteed no-overflow assembly, custom priority layers, or the reports below. The full layer table, trim strategies, and custom-tokenizer hooks live in the [Context Engine reference](../../core/context-engine.md).

## Read the assembly report to find your token hogs

Because the engine counts every layer, it hands you a breakdown for free after each assembly via `engine.get_report()`. The `ContextReport` exposes `total_tokens`, `budget`, `utilization`, and `trimmed_layers`, plus a per-layer list. That turns "why is this call so expensive?" from a guessing game into a table you can read:

- **Utilization near 100%** on a big model means you're one long turn away from trimming — time to prune memory recall or cap history.
- **`trimmed_layers` is non-empty** means real content got dropped on that call; if `memory` or `strategies` show up there often, your retrieval is pulling too much.
- **One layer dominating** the token count is your optimization target.

Two of those hogs have dedicated fixes. If `tools` is bloated because your agent connects to dozens of MCP tools, semantic [tool optimization](../../core/tool-optimization.md) selects only the tools relevant to each query — the framework's published figure is 40–70% fewer tokens on the tool layer alone. And if `memory` dominates, the issue is usually recall volume, not the window; our [AI Agent Memory: The Complete Guide for Python Devs](ai-agent-memory.md) covers tuning what gets recalled in the first place. For how these layers are populated, mutated, and cleared across a multi-step run, see the [context lifecycle guide](../../guides/context-lifecycle.md).

## When you don't need a Context Engine

Honest note: precise context assembly for agents is not free complexity you should adopt reflexively. Skip it when:

- **You're on a large-window model with short prompts.** If a GPT-5 agent with a small system prompt and no long history never approaches 128K, the default injection path is simpler and works fine.
- **You want provider-managed trimming.** Some hosted assistant APIs do their own context handling; if you've delegated that entirely, a second budgeter just fights it.
- **Your context is fully static.** If every request sends the same fixed prompt with no memory, history, or dynamic tools, there's nothing to prioritize.

Reach for the Context Engine when context is dynamic and the window is a real constraint — small-context models, long conversations, heavy retrieval, or anywhere silent truncation would be a production incident. That's exactly the regime where trim-by-priority pays for itself.

## Frequently asked questions

### How do I count tokens for an LLM prompt in Python?

Use a real tokenizer, not `len(text)`. For OpenAI models, `tiktoken` gives exact counts; the Context Engine wraps this and exposes `engine.count_tokens(text)`, falling back to a chars÷3.5 estimate (~90% accurate) for non-OpenAI models. That's the same counter it uses to enforce your budget, so your measurements match what actually ships.

### What happens when an agent exceeds the context window?

Without management, the provider either rejects the request with a context-length error or a middleware truncates blindly — often cutting the user's latest message. With priority-based context window management, the engine trims the lowest-priority non-required layers first (conversation history, then strategies, then memory) and never drops the identity, tools, or current user message.

### Is bigger context always better?

No. A larger window buys headroom, but it doesn't fix relevance — sending 100K tokens of loosely related context can dilute the model's attention and raise latency and cost. Good token budgeting sends the *right* layers, which is why per-layer reporting and tool optimization matter even on 200K-token models.

## Next steps

See the [Context Engine reference](../../core/context-engine.md) to cap exactly what your agent sends on every call and get per-layer token reporting for free. Start from the [Quick Start](../../getting-started/quickstart.md) to stand up an agent, then add `context_engine=` and watch the assembly report — pair it with [semantic tool optimization](../../core/tool-optimization.md) to shrink the layer that's usually the biggest.
