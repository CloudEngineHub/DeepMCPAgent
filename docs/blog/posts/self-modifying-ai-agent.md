---
title: "Build Self-Modifying AI Agents with Open Mode"
description: "Self-modifying agents sound reckless — this shows how to do it with guardrails. Walk through the 14 meta-tools (modify_instructions, create_tool…"
keywords: "self-modifying AI agent, agent that rewrites itself, self-improving AI agent, open mode agent, agent meta-tools, runtime agent modification"
date: 2026-07-16
slug: self-modifying-ai-agent
categories:
  - Runtime
---

# Build Self-Modifying AI Agents with Open Mode

A self-modifying AI agent — one that can rewrite its own prompt, author a new tool, or connect to another server while it's still running — sounds like exactly the kind of thing you should never ship to production. The instinct is right: an agent with unrestricted write access to its own configuration is a liability. But "never" is the wrong lesson. The right one is *never without guardrails*. By the end of this post you'll know what Promptise Foundry's **open mode** actually lets an agent do, the concrete safeguards that box it in, and how to enable it in a `.agent` manifest so an agent can safely author its own first tool.

<!-- more -->

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## What "self-modifying" really means

In Promptise, a self-modifying AI agent is an `AgentProcess` running in `ExecutionMode.OPEN`. In strict mode (the default), the agent's configuration is frozen: the instructions, tool list, and connected servers you defined are the ones it lives with for its whole lifetime. Open mode flips one switch — the process now hands the agent a set of **meta-tools** it can call to change itself.

This is not a smarter prompt or a bigger context window. It's a different capability class. A regular tool acts on the outside world (query a database, call an API). A meta-tool acts on the agent itself. When the agent calls one, the process performs a **hot-reload**: it rebuilds the agent graph with the new configuration while preserving the conversation buffer, so the agent keeps its memory of the exchange but wakes up with new powers.

If you're new to the runtime layer that makes this possible — long-running processes, triggers, journaled state — start with [What Is an Autonomous AI Agent Runtime?](autonomous-ai-agent-runtime.md), which frames why an agent needs to be a supervised process before you ever let it modify itself.

## The 14 self-modification meta-tools

Open mode exposes 14 meta-tools, each gated by a permission flag in `OpenModeConfig`. Grouped by what they touch:

- **Identity** — `modify_instructions` rewrites the system prompt.
- **Capabilities** — `create_tool` defines a new Python function tool; `connect_mcp_server` attaches an additional MCP server to gain its tools.
- **Reactivity** — `add_trigger` and `remove_trigger` schedule or tear down cron, event, message, webhook, and file-watch triggers at runtime.
- **Delegation** — `spawn_process` creates and starts a new agent process inside the same runtime; `list_processes` enumerates them.
- **Memory** — `store_memory`, `search_memory`, and `forget_memory` give the agent explicit control over its long-term memory.
- **Introspection & governance** — `get_secret` fetches a credential from the process secret store, `check_budget` reports remaining budget, `check_mission` reports mission progress, and `list_capabilities` returns a full snapshot of the agent's current tools, triggers, and identity.

`list_capabilities` is always available; the rest only appear when you switch on the matching flag. The full input schema for each is documented in the [runtime overview](../../runtime/index.md) and the meta-tools reference — you never wire these up by hand, the process injects the permitted subset automatically.

## Why an agent that rewrites itself isn't reckless — the guardrails

Every safeguard lives on `OpenModeConfig` and is enforced by the process before any modification lands. The ones that matter most:

- **Mandatory sandbox for agent-written code.** `sandbox_custom_tools=True` is the default. Code the agent writes through `create_tool` runs with a restricted set of builtins — `len`, `range`, `sorted`, `sum` and friends are allowed, but `open`, `exec`, `eval`, `__import__`, file I/O, and all network access are blocked. An agent cannot write a tool that reads your filesystem or phones home. Setting it to `False` runs code with full builtins and is only for trusted, reviewed environments.
- **MCP URL whitelist.** `allowed_mcp_urls` restricts `connect_mcp_server`. Leave it empty and the agent can connect anywhere; set it to a list and any URL outside that list is rejected. In production, whitelist explicitly.
- **Hard caps on growth.** `max_custom_tools` (default 20), `max_dynamic_triggers` (default 10), `max_instruction_length` (default 10,000 characters), and `max_spawned_processes` (default 3) bound how far the agent can expand. Exceeding a cap returns an error to the agent instead of mutating it.
- **Rebuild ceiling.** Each self-modification triggers a hot-reload that reconnects servers and re-initializes tools. `max_rebuilds` caps how many times that can happen per lifetime, so a confused agent can't thrash your infrastructure in a loop.
- **Spawning is off by default.** Unlike most permissions, `allow_process_spawn` defaults to `False`. Letting an agent create other agents is powerful — you opt in deliberately.
- **Rollback to original.** The process keeps the original configuration. A single `await process.rollback()` clears all dynamic instructions, custom tools, connected servers, and dynamic triggers, then rebuilds from the config you shipped. It's your undo button, and it only works in open mode.

The design principle is **start restrictive, then expand**: grant the narrowest set of flags the agent actually needs, and treat every additional permission as a decision.

## Let an agent author its own first tool

Here's a complete, runnable self-improving AI agent. It runs in open mode with only tool creation and identity changes enabled, everything else locked down, and the sandbox on. We start the process, then inject a task it has no built-in tool for and let it write one:

```python
import asyncio

from promptise.runtime import (
    AgentProcess,
    ProcessConfig,
    ExecutionMode,
    OpenModeConfig,
)
from promptise.runtime.triggers.base import TriggerEvent


async def main():
    process = AgentProcess(
        name="adaptive-analyst",
        config=ProcessConfig(
            model="openai:gpt-5-mini",
            instructions=(
                "You are an adaptive analyst. When a task needs a capability "
                "you don't have, author it yourself with the create_tool "
                "meta-tool, then call it to finish the job."
            ),
            execution_mode=ExecutionMode.OPEN,
            open_mode=OpenModeConfig(
                allow_tool_creation=True,    # can write its own tools
                allow_identity_change=True,  # can refine its own prompt
                allow_mcp_connect=False,     # cannot reach arbitrary servers
                allow_process_spawn=False,   # cannot fork sub-agents
                sandbox_custom_tools=True,   # agent code runs sandboxed (default)
                max_custom_tools=5,          # hard cap on self-authored tools
                max_rebuilds=10,             # cap hot-reloads per lifetime
            ),
        ),
    )

    await process.start()

    # Wake the agent with a task it has no built-in tool for.
    await process.inject(
        TriggerEvent(
            trigger_id="manual",
            trigger_type="manual",
            payload={"task": "Compute the 10th Fibonacci number, writing a tool if needed."},
        )
    )

    await asyncio.sleep(20)   # let the agent author + run its tool
    print(process.status())   # rebuild count > 0 means it modified itself
    await process.stop()


asyncio.run(main())
```

The agent reads the task, notices it has no arithmetic tool, calls `create_tool` with a `run(**kwargs) -> str` function, and the process hot-reloads with the new tool available. Because `sandbox_custom_tools=True`, that code can't import anything or touch the disk — it can only compute. The `status()` snapshot shows a non-zero rebuild count, your evidence that the agent genuinely rewrote itself rather than just answering.

## Ship it as a `.agent` manifest

You rarely hardcode a `ProcessConfig` in production. The same agent belongs in an `.agent` YAML manifest, where open mode and its guardrails are declared as config and loaded by the runtime. The manifest fields map one-to-one onto `OpenModeConfig`:

```yaml
version: "1.0"
name: adaptive-analyst
model: openai:gpt-5-mini
instructions: |
  You are an adaptive analyst. Author tools with create_tool when a
  task needs a capability you don't have, then use them.

execution_mode: open

open_mode:
  allow_tool_creation: true
  allow_identity_change: true
  allow_mcp_connect: false
  allow_process_spawn: false
  sandbox_custom_tools: true
  max_custom_tools: 5
  max_rebuilds: 10
  allowed_mcp_urls: []   # whitelist URLs here before enabling allow_mcp_connect
```

Manifests are the right home for this: reviewable, diffable, and deployable from the CLI, so the whole surface of what an agent may do to itself sits in one file your team can approve. See the [manifests reference](../../runtime/manifests.md) for the full schema. Pair open mode with the runtime's [budget governance](../../runtime/governance/budget.md) so a self-modifying agent still hits a hard ceiling on tool calls, LLM turns, and irreversible actions per run. Guardrails compose: the open-mode caps bound *what* it can change, the budget bounds *how much* it can do.

## When you should NOT enable open mode

Open mode is a real capability, not a default. Skip it when:

- **The workload is fixed.** If an agent's tools and prompt are known up front and stable, strict mode is simpler, faster (no rebuild latency), and easier to audit. Most production agents should stay strict.
- **You can't tolerate non-determinism.** A self-modifying agent's tool set changes at runtime, which makes reproducing a past run harder. For regulated or high-stakes flows, prefer explicit, version-controlled tools and a [human-in-the-loop approval gate](../../runtime/manifests.md) over letting the agent decide.
- **You'd disable the sandbox.** If your use case needs agent-written code to do file or network I/O, that's a signal to provide those capabilities as reviewed MCP tools instead of turning off `sandbox_custom_tools`. Reaching for `sandbox_custom_tools=False` is a red flag, not a shortcut.

The honest framing: open mode shines for genuinely adaptive agents — long-running research, monitoring, or operations agents that hit tasks you couldn't fully enumerate up front. If you can enumerate them, don't use it. For keeping such agents alive and supervised over days, see [How to Build a Long-Running AI Agent](long-running-ai-agent.md).

## Frequently asked questions

### Is a self-modifying AI agent safe to run in production?

Yes, when it runs in open mode with the guardrails on. The mandatory sandbox blocks agent-written code from touching files or the network, the MCP whitelist restricts what it can connect to, and hard caps plus `max_rebuilds` bound how far and how often it can change. Combine those with budget governance and a rollback path, and self-modification becomes a controlled capability rather than an open door.

### What happens to the conversation when an agent modifies itself?

The process performs a hot-reload: it rebuilds the agent graph with the new configuration but preserves the conversation buffer, so the agent keeps the context of the current exchange. Each rebuild increments a counter you can read from `status()`, and `max_rebuilds` caps the total to prevent runaway loops.

### Can I undo a change an agent made to itself?

Yes. In open mode, calling `await process.rollback()` clears every dynamic modification — instructions, custom tools, connected servers, and dynamic triggers — and rebuilds the agent from the original configuration you shipped. It's the reset button for any self-modifying agent that has drifted.

## Next steps

Enable open mode in a sandboxed `.agent` manifest and let an agent safely author its own first tool — start from the runnable example above, keep `sandbox_custom_tools: true`, and grant only the permissions the task needs. New to the framework? Work through the [Quick Start](../../getting-started/quickstart.md) first, then read the [Agent Runtime overview](../../runtime/index.md) to see where open-mode processes fit alongside triggers, journals, and governance.
