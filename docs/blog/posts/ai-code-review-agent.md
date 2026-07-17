---
title: "Build an AI Code Review Agent That Cuts False Positives"
description: "The complaint every team has about LLM code review is noise. This build wires an adversarial self-critique pass so the agent challenges its own findings and…"
keywords: "ai code review agent, automated code review ai, llm code review, pull request review bot, security review agent"
date: 2026-07-16
slug: ai-code-review-agent
categories:
  - Use Cases
---

# Build an AI Code Review Agent That Cuts False Positives

An **ai code review agent** is easy to stand up and hard to trust: point a model at a diff, ask for problems, and you get a wall of "consider using parameterized queries" that flags string concatenation in a comment as a SQL injection. The complaint every team has about LLM code review is noise. By the end of this post you'll have built a reviewer that challenges its own findings with an adversarial critique pass, forces a specific line reference behind every claim, and routes cheap triage and deep analysis to different models so the whole thing stays affordable.

## Why LLM code review produces so much noise

A one-shot prompt has no incentive to be right — only to be complete. Ask a model to "find security issues" and it optimizes for coverage, listing every pattern that *could* be a problem regardless of whether it's exploitable in this codebase. That's the failure mode behind most automated code review AI experiments: the signal is real, but it's buried under theoretical concerns, style opinions, and confident hallucinations about lines that don't exist.

The fix isn't a bigger model. It's a second pass whose job is to *disagree* with the first. Three moves cut the noise:

- **Adversarial critique** — a step that asks, for each finding, "is this actually exploitable, or a style concern?"
- **Mandatory evidence** — every finding must cite a file and line number, or it gets dropped.
- **Calibrated severity** — the critique forces an honest CRITICAL/HIGH/MEDIUM/LOW rating instead of flagging everything as urgent.

Promptise Foundry's reasoning engine gives you each of these as a node you drop into a graph, so you're wiring a pipeline rather than tuning one giant prompt.

## The verify pattern: self-checking in one line

The fastest way to see the effect is the built-in `verify` reasoning pattern. It runs a single-pass, self-verifying loop: the model produces an answer, then re-examines it against the evidence before committing. You get it by passing one string to `build_agent()`:

```python
import asyncio
import sys
from promptise import build_agent
from promptise.config import StdioServerSpec

async def main():
    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={
            "code": StdioServerSpec(command=sys.executable, args=["code_server.py"]),
        },
        agent_pattern="verify",   # self-checking reasoning, no custom graph needed
        instructions=(
            "You are a senior security engineer. Report only real, exploitable "
            "issues. Cite a file and line for every finding. Rate severity "
            "CRITICAL, HIGH, MEDIUM, or LOW."
        ),
    )

    result = await agent.ainvoke({
        "messages": [{"role": "user",
                      "content": "Review every file for security and code-quality issues."}]
    })
    print(result["messages"][-1].content)
    await agent.shutdown()

asyncio.run(main())
```

`code_server.py` is a small MCP server that exposes `list_files`, `read_file`, and `search_pattern` tools over your codebase — the full source is in the [code review lab](../../guides/lab-code-review.md). The agent discovers those tools automatically; you never wire schemas by hand.

The `verify` pattern is a great default. But a reviewer that has to survive a pull request review bot in CI deserves explicit control over *how* it critiques itself — which is where a custom graph earns its keep.

## Wire the adversarial self-critique graph

The reasoning engine models an agent as a graph of nodes: `Read → Analyze → Critique → Justify → Synthesize`. Each node is a real class, and the ones you need for llm code review ship in `promptise.engine.reasoning_nodes`:

- `ThinkNode` — pure analysis, no tool calls, focused on the areas you name.
- `CritiqueNode` — adversarial self-review that challenges the previous node's findings.
- `JustifyNode` — rejects any claim that can't point at specific code.
- `SynthesizeNode` — assembles the survivors into a structured report.

```python
import asyncio
import sys
from promptise import build_agent
from promptise.config import StdioServerSpec
from promptise.engine import PromptGraph, PromptNode, NodeFlag
from promptise.engine.reasoning_nodes import (
    ThinkNode, CritiqueNode, JustifyNode, SynthesizeNode,
)

async def main():
    graph = PromptGraph("code-reviewer", nodes=[
        PromptNode(
            "read_code",
            instructions="Read every file with list_files and read_file before analyzing.",
            inject_tools=True,
            is_entry=True,
            flags={NodeFlag.RETRYABLE},
        ),
        ThinkNode(
            "analyze",
            focus_areas=[
                "security vulnerabilities (injection, RCE, auth bypass)",
                "performance issues (unbounded caches, missing backoff)",
                "code quality (eval usage, hardcoded secrets, missing validation)",
            ],
        ),
        CritiqueNode("challenge", severity_threshold=0.3),  # strict: challenge weak findings
        JustifyNode("evidence"),                            # demand a file:line for each claim
        SynthesizeNode("report", is_terminal=True),
    ])

    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={"code": StdioServerSpec(command=sys.executable, args=["code_server.py"])},
        agent_pattern=graph,
        instructions=(
            "Find real vulnerabilities, not theoretical ones. Every finding cites a "
            "file and line. Rate severity CRITICAL, HIGH, MEDIUM, or LOW."
        ),
    )

    result = await agent.ainvoke({
        "messages": [{"role": "user",
                      "content": "Review all files for security and code-quality issues."}]
    })
    print(result["messages"][-1].content)
    await agent.shutdown()

asyncio.run(main())
```

The `CritiqueNode` is the piece that kills false positives. Without it, the agent reports whatever `analyze` produced. With it, each finding has to survive a hostile reviewer before it reaches `SynthesizeNode`, and `JustifyNode` drops anything that can't back itself with real code. That is the difference between a demo and a security review agent you'd actually let comment on a PR.

If you're building a standalone prompt instead of a full reasoning graph, the same idea exists at the prompt layer as the composable `self_critique` strategy — useful when you want one prompt to review its own output without standing up a node graph.

## Per-node model override: cheap triage, strong analysis

Running every step through a frontier model is wasteful — reading files and formatting a report don't need deep reasoning. Promptise lets you set a `model_override` per node, so you pay for intelligence only where it matters:

```python
# Cheap model just fetches file contents
PromptNode("read_code", model_override="openai:gpt-5-mini", inject_tools=True, is_entry=True)

# Strong model does the actual vulnerability analysis and critique
ThinkNode("analyze", model_override="openai:gpt-4o", focus_areas=[...])
CritiqueNode("challenge", model_override="openai:gpt-4o", severity_threshold=0.3)

# Cheap model formats the final report
SynthesizeNode("report", model_override="openai:gpt-5-mini", is_terminal=True)
```

The graph structure is unchanged — you're just annotating which brain runs each step. A large diff might touch the strong model twice and the cheap model three times, instead of five expensive calls. For a broader tour of what the reasoning engine and the rest of the framework can do, the [what you can build](../../resources/showcase.md) page walks through build ideas at three complexity levels.

## When a simpler tool is the better fit

Be honest about scope. If all you want is a lint-style gate — "no `eval`, no hardcoded secrets, no `shell=True`" — a deterministic linter or a Semgrep ruleset is faster, cheaper, and more predictable than any ai code review agent. Rules don't hallucinate, and they cost nothing per run. Reach for an LLM reviewer when the value is in *judgment*: reasoning about auth flows across files, weighing whether a finding is exploitable in context, or explaining a subtle logic bug in prose a linter can't describe. The strongest setups run both — static analysis for the mechanical checks, an agent for the reasoning the rules can't encode. This reviewer is also a natural node inside a larger pipeline; see [how to build multi-agent systems in Python](multi-agent-systems-python.md) for wiring it alongside a fixer and a triage agent.

## Frequently asked questions

### How does an AI code review agent avoid false positives?

By not trusting its first pass. This build adds a `CritiqueNode` that adversarially challenges each finding and a `JustifyNode` that drops any claim without a specific file and line reference. The critique step also forces calibrated severity, so genuine issues aren't drowned out by style nitpicks flagged as urgent.

### Can I run different models for different review stages?

Yes. Each node accepts a `model_override`, so you can route file reading and report formatting to a cheap model and reserve a stronger model for vulnerability analysis and critique. The graph structure stays the same — you only annotate which model runs each step.

### Do I need a full graph, or is the verify pattern enough?

Start with `agent_pattern="verify"` — it adds self-checking with zero extra code and is enough for many reviews. Move to a custom `PromptGraph` when you need explicit control over the critique threshold, mandatory evidence, or per-node model routing for a pull request review bot in CI.

## Next steps

Follow the [code review lab](../../guides/lab-code-review.md) to build a reviewer that justifies every finding, then point it at your own diff. If you're new to the framework, start with the [Quick Start](../../getting-started/quickstart.md) to get `build_agent()` running in a few minutes, then layer in the verify pattern and per-node model override from there.
