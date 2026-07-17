---
title: "Does Your AI Agent Phone Home? Telemetry & Data Egress"
description: "For a sovereign or regulated deployment, 'anonymous telemetry' and hosted tracing are still data egress you have to justify to a security review. This audits…"
keywords: "AI agent no data egress, disable agent telemetry, no-data-egress agent, agent framework telemetry, self-hosted agent observability"
date: 2026-07-16
slug: ai-agent-no-data-egress
categories:
  - Air-Gapped & Sovereign
---

# Does Your AI Agent Phone Home? Telemetry & Data Egress

The phrase **AI agent no data egress** shows up on every sovereign and regulated procurement checklist, and it means something precise: the agent — and the framework wrapped around it — must not send data outside your network without your explicit say-so. Most teams read that as a statement about the model endpoint. It isn't only that. Long before you invoke an LLM, the framework you chose may already open outbound connections for "anonymous telemetry" or route your execution traces through a hosted service. To a security reviewer, both are egress you have to justify, whether the payload is anonymous or not. This post audits what agent frameworks send outbound by default, and shows how to keep full observability — every LLM turn, tool call, latency, and cache hit — on transporters that never leave your network.

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## What "no data egress" actually means for a framework

For a normal SaaS app, "no egress" is a firewall rule. For an agent framework it's a broader claim, because a framework has three separate channels that can reach the internet:

- **The model call.** Obvious, and the one everyone plans for. If you call `openai:...` you are talking to OpenAI; if you want the LLM itself on-prem you run a local model.
- **Framework telemetry.** Usage pings the maintainers add to understand adoption. Often anonymous, often on by default, and easy to miss because it isn't in your code — it's in theirs.
- **Observability and tracing.** The pipeline that records what your agent did. If that pipeline points at a hosted tracing SaaS, then every prompt, tool argument, and output your agent handled is now leaving your perimeter for a third party — and that is usually the most sensitive channel of the three.

A sovereign deployment has to close or account for all three. The model channel is a deliberate choice you make per project. The other two are defaults you inherit, and defaults are exactly where audits get uncomfortable. The honest question for any framework is: *with a stock configuration and no special flags, what packets leave the box?*

## The audit: what other frameworks do today

A fair comparison names actual behavior, not a strawman. Here is where the popular frameworks stand, as precisely as I can state it.

**CrewAI** collects anonymous telemetry by default. It gathers usage and feature statistics — not your prompts or customer data — and you turn it off by setting an environment variable (`CREWAI_TELEMETRY_OPT_OUT`; recent versions also honor the standard `OTEL_SDK_DISABLED`). The important nuance for a security review: the data is anonymous, but the *connection* is real and it is on until you opt out. In an air-gapped network that outbound attempt simply fails; in a monitored one it's a finding you have to document and suppress. The delta is defaulting: egress-on with an opt-out, versus egress-off to begin with.

**LangChain and LangGraph** do not send telemetry from the core libraries by default — that's worth stating plainly, because it's often misrepresented. The gap is elsewhere: the ecosystem's first-class observability path is **LangSmith**, a hosted tracing service. Tracing is opt-in (you set `LANGCHAIN_TRACING_V2=true` and an API key), but it is the recommended, best-supported way to see what your chains and agents did, and when you enable it, run data — including inputs and outputs — is sent off-host to LangSmith's cloud. A self-hosted LangSmith exists, but it's a paid enterprise offering; the default road leads to the SaaS. You *can* stay local by wiring OpenTelemetry callbacks to your own backend instead — it just isn't the paved path. The delta here is coupling: production-grade observability is steered toward a hosted dependency unless you deliberately choose otherwise.

The honest summary is that both frameworks *can* be brought to a no-egress state — CrewAI with an opt-out, LangChain by declining LangSmith and wiring your own sink. Neither is a trap. But in both cases "stay on-prem" is a subtraction you perform against the default, and subtractions are the things that quietly regress when a dependency updates or a new team member forgets the flag. Promptise's design goal was to make the safe state the *default* state, so there's nothing to remember to turn off.

## Promptise's default: nothing phones home

Promptise Foundry ships no telemetry. There is no anonymous ping, no usage beacon, no "help us improve" callback — the maintainers get zero packets from your deployment, so there is nothing to opt out of. Observability is off until you ask for it, and when you do ask, it routes to **local-first transporters**. Five of the eight built-in transporters are local sinks by construction:

| Transporter | Where the data goes |
|---|---|
| `HTML` | A self-contained report file on disk (the default) |
| `JSON` | A JSON / NDJSON file on disk |
| `STRUCTURED_LOG` | JSONL lines to a local file for your own ELK/Datadog/Splunk agent to ship |
| `CONSOLE` | Your terminal |
| `PROMETHEUS` | A pull-based metrics endpoint *your* Prometheus scrapes |

Only two transporters can ever open an outbound connection — `OTLP` (OpenTelemetry) and `WEBHOOK` — and both require you to name the destination. Point OTLP at a collector inside your own cluster and even that stays on your network. Out of the box, with `observe=True`, the entire pipeline is a single local HTML report. Nothing leaves.

There's a subtler egress vector Promptise closes by design: it does **no external cost or pricing tracking**. Some tooling calls a provider's pricing API to convert token counts into dollar estimates — a small outbound request per run that most people never notice. Promptise records the token counts (`total_prompt_tokens`, `total_completion_tokens`, `total_tokens`) locally and stops there. You get the raw numbers; you don't get a background call to a pricing service you'd have to whitelist.

Here is a full, runnable configuration that captures everything — full traces, token counts, latencies — with five local transporters and not a single outbound-capable one:

```python
import asyncio

from promptise import build_agent
from promptise.observability_config import (
    ObservabilityConfig,
    ObserveLevel,
    TransporterType,
)

# Every transporter here writes to a LOCAL sink. None of them opens an
# outbound connection: HTML/JSON are files on disk, STRUCTURED_LOG is a
# local JSONL file, CONSOLE prints to your terminal, and PROMETHEUS is a
# pull-based endpoint your own Prometheus scrapes. The only transporters
# that can egress -- OTLP and WEBHOOK -- are deliberately left out.
observe = ObservabilityConfig(
    level=ObserveLevel.FULL,          # every LLM turn, tool call, token count
    session_name="sovereign-audit",
    record_prompts=True,              # full prompt/response text, kept on-prem
    transporters=[
        TransporterType.HTML,
        TransporterType.JSON,
        TransporterType.STRUCTURED_LOG,
        TransporterType.CONSOLE,
        TransporterType.PROMETHEUS,
    ],
    output_dir="./observability",
    log_file="./logs/agent.jsonl",
    prometheus_port=9090,
)


async def main() -> None:
    agent = await build_agent(
        # Point this at your own internal MCP servers; empty here so the
        # snippet runs with zero external dependencies.
        servers={},
        # A local model keeps even the LLM call on your network.
        model="ollama:llama3",
        observe=observe,
        instructions="You are an internal operations assistant.",
    )

    await agent.ainvoke(
        {"messages": [{"role": "user", "content": "Summarize today's incidents."}]}
    )

    # Token counts and latencies -- computed locally, never sent to a
    # pricing API or a hosted tracing service.
    stats = agent.get_stats()
    print("total tokens:", stats["total_tokens"])
    print("LLM calls:", stats["llm_call_count"])


asyncio.run(main())
```

Swap `ollama:llama3` for a cloud provider string and only the model channel opens up — the observability pipeline stays exactly as local as it was. The full transporter catalog, field reference, and per-tenant attribution options are documented in the [observability guide](../../core/observability.md), and if you want the LLM itself to stay in-network too, the [model setup guide](../../getting-started/model-setup.md) covers Ollama and any other local `provider:model` string.

## Verifying the boundary for a security review

A claim of no egress is only worth what you can demonstrate. The point of keeping the default local isn't marketing — it's that the demonstration is short:

- **Run it under a network monitor.** Build an agent with a local model and the config above, invoke it, and watch the connection table. With OTLP and WEBHOOK absent and a local model, there should be no outbound connections at all. This is a test you can hand a reviewer, not a promise.
- **Grep the config for the two egress transporters.** A policy check as simple as "reject any `ObservabilityConfig` containing `TransporterType.OTLP` or `TransporterType.WEBHOOK` without an approved internal destination" is enforceable in CI. Because egress is opt-in and named, the audit surface is two enum values, not a dependency tree.
- **Confirm the traces are complete.** Local-first doesn't mean low-fidelity. At `ObserveLevel.FULL` with `record_prompts=True`, the HTML report and JSONL log carry every LLM turn, tool call, latency, retry, and (with a cache configured) hit/miss — the same detail a hosted service would show, held on your disk.

The result is an observability story you can defend line by line: the raw data never leaves, the token counts are computed without an outside call, and the only paths that could egress are ones you consciously wired to infrastructure you control. For the wider on-prem picture — pinned dependencies, local embedding and guardrail models, offline installs — see [Air-Gapped AI Agent Framework: The On-Prem Guide](air-gapped-agent-framework.md), and for the failure modes that catch teams off guard, [Why AI Agent Frameworks Fail in Air-Gapped Networks](air-gapped-ai-agent.md).

## Frequently asked questions

### Does Promptise send any telemetry by default?

No. Promptise ships with no telemetry of any kind — no anonymous usage ping, no beacon, no callback to the maintainers. There is nothing to opt out of because there was never an outbound default. Observability is off until you enable it, and when enabled it defaults to a single local HTML report.

### Isn't anonymous telemetry harmless? Why does it matter for a security review?

The payload may be anonymous, but the *connection* is the finding. In an air-gapped network the attempt fails and shows up in logs; in a monitored one it's an unexplained outbound flow you have to trace, document, and justify. A reviewer isn't only asking "is this data sensitive" — they're asking "why is this box talking to the internet at all." Removing the connection entirely is cleaner than explaining an anonymous one.

### Can I still use OpenTelemetry or ship traces to a central system?

Yes — that's what the `OTLP` and `WEBHOOK` transporters are for. The difference is that they require you to name the destination, so you can point OTLP at a collector *inside* your own network and keep everything on-prem, or send to a central SIEM you operate. Egress becomes a deliberate, auditable choice rather than a default.

### How is this different from just disabling telemetry in another framework?

Functionally you can reach a similar end state by opting out of CrewAI's telemetry or declining LangSmith in the LangChain ecosystem. The difference is structural: in those frameworks staying local is a subtraction you perform and must keep performing across upgrades and new team members. In Promptise the local state is the default, and the only egress-capable transporters are explicit and greppable — so the safe configuration is the one you get by doing nothing.

### Do I lose token or cost visibility by keeping everything local?

You keep full token visibility — prompt, completion, and total counts are recorded locally on every run. What you don't get is an automatic dollar figure, because Promptise makes no external call to a pricing API. That's deliberate: it avoids an outbound request per run, and provider prices change often enough that a number computed against your own current rate card is more trustworthy than one fetched from a third party.

## Next steps

Wire local-only observability transporters so every trace, token count, and tool call stays inside your network. Start from the [observability guide](../../core/observability.md) to pick your transporters, pin a local model with the [model setup guide](../../getting-started/model-setup.md) so even the LLM channel stays on-prem, then run the configuration above under a network monitor and confirm the connection table stays empty. The goal isn't to promise no egress — it's to make no egress the state you can demonstrate on demand.
