---
title: "Multi-Agent Debate for Better LLM Reasoning"
description: "Shows the Proposer-to-Critic-to-Judge graph and is upfront about the economics: debate roughly triples calls for marginal accuracy on capable models. It maps…"
keywords: "multi-agent debate llm, debate reasoning pattern, proposer critic judge, multi-perspective reasoning agents, llm debate accuracy"
date: 2026-07-16
slug: multi-agent-debate-llm
categories:
  - Reasoning
---

# Multi-Agent Debate for Better LLM Reasoning

A multi-agent debate LLM setup runs the same question through three roles — one model proposes an answer, another attacks it, and a judge decides who was right — instead of trusting a single pass. The appeal is obvious: adversarial scrutiny catches mistakes that a lone model, confidently wrong, will happily ship. The catch is just as real: you pay for it in latency and tokens. By the end of this post you'll know exactly what the Proposer → Critic → Judge graph does in Promptise Foundry, how to stand one up in a few lines, and — just as importantly — the narrow set of queries where it earns its keep versus the far larger set where a single verify pass is the smarter buy.

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## What the debate reasoning pattern actually does

Promptise ships `debate` as a prebuilt reasoning graph. It's an adversarial two-agent loop: a **proposer** generates an answer, a **critic** challenges it, and they alternate. When the critic's objections drop below a severity threshold, control passes to a **judge** that renders the final verdict.

```
proposer ──→ critic ──→ (severity high) ──→ proposer
                    ──→ (severity low)  ──→ judge ──→ done
```

Under the hood this is a static multi-perspective reasoning graph — a fixed set of nodes wired with conditional edges, not an open-ended agent improvising its own path. The proposer and critic hold deliberately opposing stances so the answer gets stress-tested from both sides, and the judge only ever sees a position that has already survived at least one round of attack. You can read the full node breakdown in the [engine prebuilts reference](../../core/engine-prebuilts.md), which documents every shipped pattern and its exact graph shape.

## Proposer, critic, judge: standing one up

There's nothing to assemble by hand. Pass `agent_pattern="debate"` to `build_agent()` and the graph is wired for you:

```python
import asyncio
from promptise import build_agent


async def main():
    agent = await build_agent(
        model="openai:gpt-5-mini",
        agent_pattern="debate",
        instructions=(
            "You are answering high-stakes analytical questions. "
            "Argue rigorously and concede when the evidence is against you."
        ),
    )

    question = (
        "A vendor contract auto-renews unless canceled 60 days before the "
        "term ends. The term ends March 31. Today is January 20. Can we "
        "still cancel in time? Show your reasoning."
    )
    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": question}]}
    )
    print(result["messages"][-1].content)

    await agent.shutdown()


asyncio.run(main())
```

That's the whole thing. The proposer drafts an answer, the critic hunts for the flaw (here: is January 20 actually more than 60 days before March 31?), and the judge settles it. Because the critic is prompted to disagree, careless date arithmetic or an unstated assumption gets surfaced instead of silently shipped.

## Tuning rounds and perspective

The prebuilt string is the fast path. When you want control over how long the argument runs, build the graph explicitly with `PromptGraph.debate()` and cap the rounds:

```python
from promptise import build_agent
from promptise.engine import PromptGraph

graph = PromptGraph.debate(
    system_prompt=(
        "Debate whether the proposed answer is correct and complete. "
        "The critic must raise the strongest possible objection each round."
    ),
    max_rounds=3,   # judge decides after at most 3 proposer/critic exchanges
)

agent = await build_agent(model="openai:gpt-5-mini", agent_pattern=graph)
```

`max_rounds` is your primary cost lever. Each round is another proposer turn plus another critic turn, so lowering the cap directly bounds spend. Three is a sensible ceiling for most decision questions — if the critic hasn't landed a decisive objection by then, more rounds rarely change the verdict. If you need to reshape the roles further — swap in your own critique node, change the judge's rubric, or add a tool-using research step before the debate — the [custom reasoning guide](../../guides/custom-reasoning.md) walks through composing graphs node by node.

## The honest economics of multi-agent debate

Here's the part most write-ups skip. Debate is structurally expensive. A single-pass agent makes roughly one LLM call. Debate makes at least three — proposer, critic, judge — and every extra round adds two more. On a capable model that already reasons well internally, that roughly-triple spend buys you a **marginal** accuracy gain, not a transformative one, because the base model wasn't making many catchable errors to begin with.

So don't reach for it by default. Debate earns its cost in a specific shape of problem:

- **Ambiguous judgment calls** — questions with no clean lookup answer, where two defensible readings exist and you want them argued out.
- **Adversarial correctness** — high-stakes claims where being confidently wrong is expensive: contract deadlines, compliance interpretations, financial thresholds, safety sign-offs.
- **Robust argument generation** — when you need the *strongest case on both sides* surfaced, not just a single answer.

For everything else — tool-calling, retrieval, straightforward Q&A, arithmetic — the extra scrutiny is scrutiny you're paying for and not using. The pattern only pays back where a wrong answer costs more than three LLM calls.

Because Promptise only cites the framework's own measured figures, this post won't hand you a "debate boosts accuracy by X%" number. The claim we'll stand behind is structural and verifiable: debate roughly triples the call count, and on strong models the accuracy delta is small. Gate accordingly.

## When a single verify pass is the smarter buy

For most "catch the careless mistake" work, `verify` is the better fit — and it's honest to say so. The `verify` pattern forces the model to **plan, solve, and re-check its own answer inside a single generation**. You get an explicit verification step at one-turn latency, with no extra LLM calls:

```python
# One turn. The node prompt forces PLAN → SOLVE → VERIFY → answer.
agent = await build_agent(model="openai:gpt-5-mini", agent_pattern="verify")
```

`verify` catches the same genre of slip debate is good at — bad arithmetic, a skipped constraint, an unchecked assumption — but at roughly a third of the cost. Its measurable gains show up most on weaker or cheaper models, where the forced self-check recovers errors a single pass would miss. Reach for debate only when you specifically need *two independent perspectives arguing*, not just one model checking itself. When you simply want a cheap self-check, `verify` wins; the tradeoff is laid out in full in the [reasoning patterns reference](../../core/agents/reasoning-patterns.md).

A practical rule of thumb: route your traffic. Send the small slice of genuinely high-stakes, contestable queries to a `debate` agent, and let a `verify` or plain `react` agent handle the rest. Two agents, one gate — that's how you get debate's scrutiny without paying for it on every request. If you're deciding between patterns more broadly, [Agent Reasoning Patterns: The Complete Guide](agent-reasoning-patterns.md) compares all of Promptise's built-in graphs side by side.

## Frequently asked questions

### Does multi-agent debate actually improve LLM accuracy?

It can, but the gain depends heavily on the model and the task. On capable models that already reason well, debate produces a marginal improvement for roughly triple the cost, because the base model wasn't making many catchable errors. The wins concentrate in ambiguous or adversarial questions where a second, opposing perspective surfaces a flaw the proposer missed. For routine tasks, the extra calls buy little.

### How is debate different from the verify pattern?

`verify` is one model checking its own work inside a single generation — one turn, no extra calls. Debate uses separate proposer, critic, and judge roles across multiple turns, so it's at least three calls and often more. Use `verify` for cheap self-correction on arithmetic and logic; use `debate` when you specifically need two independent perspectives argued out before a judge decides.

### How do I limit how expensive a debate agent gets?

Cap `max_rounds` when you build the graph with `PromptGraph.debate(max_rounds=3)`. Each round adds a proposer and a critic call, so the cap directly bounds spend. Beyond that, gate debate to only your high-stakes queries and route everything else to a cheaper pattern.

## Next steps

Stand up a debate agent with `agent_pattern="debate"` and gate it to only your high-stakes queries — the ambiguous judgment calls and adversarial-correctness questions where three LLM calls cost less than a wrong answer. Start from the [Quick Start](../../getting-started/quickstart.md) to get an agent running, then browse the [engine prebuilts reference](../../core/engine-prebuilts.md) to see every reasoning pattern you can swap in with a single parameter.
