---
title: "Pydantic AI vs Promptise Foundry: Typed Agents"
description: "Pydantic AI nails typed, structured output with minimal ceremony, and for a typed wrapper it's a great pick. Promptise pulls ahead when structured output is…"
keywords: "Pydantic AI vs Promptise, Pydantic AI alternative, typed agent framework, structured output agents, Pydantic AI production"
date: 2026-07-16
slug: pydantic-ai-vs-promptise
categories:
  - Comparisons
---

# Pydantic AI vs Promptise Foundry: Typed Agents

If you're weighing **Pydantic AI vs Promptise** for a project that lives or dies on structured output, you've already found the crux of the decision: both frameworks give you typed, validated responses, but they draw the box around the problem very differently. Pydantic AI treats an agent as a typed function — clean, minimal, Pydantic all the way down. Promptise Foundry treats structured output as one layer beside memory, semantic cache, a sandbox, multi-tenant MCP, and runtime governance. By the end of this post you'll know which shape fits your stack, and you'll have runnable code for schema-strict output with automatic retry.

## Where Pydantic AI is the better fit

Let's be fair before we compare. Pydantic AI is an excellent **typed agent framework**, and for a large class of projects it's the right call. Reach for it when:

- **You already live in Pydantic.** If your codebase models everything with `BaseModel`, an agent that returns your existing models with zero glue is a natural extension. The output *is* your domain type.
- **You want minimal ceremony.** Pydantic AI is small and focused. There's little to learn, few moving parts, and the mental model — "an LLM call that returns a validated object" — is easy to reason about.
- **Structured output is the whole job.** If your product is fundamentally "call a model, get typed data back, hand it to the next function," you don't need a platform. A typed wrapper is less to maintain.
- **You like its dependency-injection style.** Pydantic AI's typed `deps` system is ergonomic for passing database handles and config into tools.

None of that is faint praise. If those bullets describe your project, a `Pydantic AI alternative` may be solving problems you don't have. Choosing Promptise Foundry over Pydantic AI only makes sense when structured output is one requirement among several — and the others are the ones that usually hurt in production.

## Beyond typed output: what a production stack needs

Here's the honest framing. Every serious agent framework can return a typed object. The differences show up *around* that call: how tools reach the agent, who is allowed to run them, what happens when a user pastes a prompt-injection payload, and whether responses are cached and audited.

Promptise Foundry is **MCP-native and secure by default**. You point `build_agent()` at Model Context Protocol servers and it discovers tools automatically — no hand-wired adapters. In the same `build_agent()` call you turn on memory, a semantic cache, guardrails, a sandbox, and observability as one-parameter flags. The [Why Promptise Foundry](../../getting-started/why-promptise.md) page lays out exactly what ships in the box and, just as importantly, when other frameworks are a better fit.

The practical question for a `Pydantic AI vs Promptise` decision is: how many of these will you end up building yourself?

- Tool discovery from external servers, not per-tool Python glue
- Per-request user and tenant identity (`CallerContext`) propagated to cache, guardrails, and audit
- Local security scanning of every input and output
- Conversation persistence and semantic caching for cost control
- A hardened execution sandbox for model-written code

If the answer is "most of them," a typed wrapper becomes the thin top layer of a platform you assemble by hand.

## Structured output agents with schema-strict guards

Now the feature at the heart of this comparison: schema-strict output. Promptise's prompt-engineering layer ships a `SchemaStrictGuard` that validates the model's response is well-formed JSON and rejects it otherwise, and a `retry()` primitive that re-runs the prompt on failure. Together they give you **structured output agents** with automatic retry — the model doesn't get to hand you broken JSON and move on.

The guard runs after generation. If the output doesn't parse, it raises a `GuardError`; wrapping the prompt in `retry()` catches that and asks the model again with exponential backoff.

```python
import asyncio
from promptise.prompts import prompt, guard
from promptise.prompts.chain import retry
from promptise.prompts.guards import schema_strict


@prompt(model="openai:gpt-5-mini")
@guard(schema_strict())
async def extract_contact(text: str) -> str:
    """Extract the contact details from the text below.
    Respond with ONLY a JSON object with keys "name", "email", "company".

    Text: {text}
    """


# Retry up to 3 times if the model returns malformed JSON.
robust_extract = retry(extract_contact, max_retries=3, backoff=1.0)


async def main():
    result = await robust_extract(
        "Reach Ada Lovelace at ada@analytical.io — she runs Engine Labs."
    )
    print(result)  # guaranteed well-formed JSON, or GuardError after retries


asyncio.run(main())
```

Two things worth calling out honestly:

- `SchemaStrictGuard` validates that the output *parses* as JSON. For field-level rules (required keys, value ranges), compose it with an `OutputValidatorGuard` that runs your own check — or validate the parsed dict against a Pydantic model yourself. Promptise doesn't hide the fact that these are separate, composable pieces.
- Because guards are just objects implementing `check_input`/`check_output`, you can stack a content filter or length bound on the same prompt. The [building agents guide](../../guides/building-agents.md) walks through wiring guards, strategies, and context providers onto a prompt end to end.

This is the layer where Pydantic AI and Promptise genuinely overlap. The divergence is what sits beside it.

## Guardrails Pydantic AI leaves to you

Structured output tells you the *shape* is right. It says nothing about whether the input was a jailbreak attempt or the output leaked a customer's email. That's a separate problem, and it's the second half of the feature this post showcases.

Promptise ships the `PromptiseSecurityScanner` — six local detection heads covering prompt injection, PII, credential leakage, named entities, content safety, and custom rules. Every model runs locally, so nothing about your prompts leaves the process. You enable the whole thing with one flag on `build_agent()`:

```python
import asyncio
from promptise import build_agent, CallerContext


async def main():
    agent = await build_agent(
        model="openai:gpt-5-mini",
        instructions="You extract structured data from support tickets.",
        guardrails=True,   # PromptiseSecurityScanner: injection, PII, secrets, NER, content, custom
        observe=True,      # timeline of every LLM turn and guardrail decision
    )

    caller = CallerContext(user_id="alice", roles=["analyst"], tenant_id="acme")
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Summarize ticket #4021"}]},
        caller=caller,
    )
    print(result["messages"][-1].content)
    await agent.shutdown()


asyncio.run(main())
```

Input scanning can *block* a malicious request; output scanning can *redact* leaked secrets before the response reaches the caller. With Pydantic AI you'd integrate a scanning library yourself and thread it through every call site. Neither approach is wrong — but for **Pydantic AI production** workloads handling untrusted input, "it ships in the box and is on by default" is a meaningful difference.

## Pydantic AI vs Promptise: a side-by-side

| Concern | Pydantic AI | Promptise Foundry |
|---|---|---|
| Typed / structured output | First-class, core design | `SchemaStrictGuard` + `retry()`, composable guards |
| Footprint | Small, focused wrapper | Full platform, opt-in layers |
| Tool integration | Python functions + typed deps | MCP-native discovery, no per-tool glue |
| Guardrails | Bring your own | `PromptiseSecurityScanner`, 6 local heads, on by default |
| Memory / cache | Bring your own | Built in (Chroma/Mem0, semantic cache) |
| Multi-tenancy & identity | Bring your own | `CallerContext`, tenant isolation across the stack |
| Sandbox for model code | Bring your own | Hardened Docker sandbox flag |

Read that table honestly: if the right column is a list of things you *don't* need, the extra surface area is a cost, not a benefit, and Pydantic AI is the leaner choice. If it's a list of things you'd otherwise build, Promptise is the shorter path. For a broader field than just these two, our [honest guide to the best AI agent framework in 2026](best-ai-agent-framework-2026.md) puts both in context.

## Frequently asked questions

### Is Promptise Foundry a drop-in Pydantic AI alternative?

Not a drop-in — the APIs differ. Promptise uses `build_agent()` with MCP tool discovery and a prompt-engineering layer, rather than typed function-agents. If your only requirement is typed output, migrating adds surface area you may not need. The `Pydantic AI vs Promptise` choice is really "typed wrapper" versus "full platform," so match it to how much of the surrounding stack you'd otherwise build.

### Does Promptise validate output against a JSON schema like Pydantic models?

`SchemaStrictGuard` validates that the output is well-formed JSON and retries when it isn't. For field-level rules, compose it with an `OutputValidatorGuard` or validate the parsed object against a Pydantic model in your own code. It's deliberately composable rather than a single monolithic validator.

### Can I keep using Pydantic with Promptise?

Yes. Pydantic is a general data-validation library, not tied to any one agent framework. Parse the schema-strict JSON output into your `BaseModel` exactly as you would elsewhere. Promptise doesn't replace Pydantic — it adds the tool, guardrail, and runtime layers around it.

## Next steps

Pick the typed wrapper or the full platform based on the table above, then try schema-strict guards on a real prompt — start with the [Quick Start](../../getting-started/quickstart.md) to get an agent running, then follow the [building agents guide](../../guides/building-agents.md) to add guards, retries, and guardrails. Still weighing options across the field? Work through our [2026 framework checklist](choosing-an-agent-framework.md) before you commit.
