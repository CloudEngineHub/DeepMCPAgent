---
title: "Llama Guard vs Azure AI: LLM Content Moderation"
description: "An honest local-vs-cloud breakdown: Azure AI Content Safety is the pragmatic pick when you're already cloud-native and want zero model downloads and lower…"
keywords: "llm content moderation, llama guard python, azure ai content safety, local vs cloud content moderation, harmful content detection llm"
date: 2026-07-16
slug: llm-content-moderation
categories:
  - Guardrails
---

# Llama Guard vs Azure AI: LLM Content Moderation

Picking a backend for LLM content moderation usually forces an early, sticky decision: run a safety model locally, or call a hosted API. Get it wrong and you either ship a 4.9 GB model into an environment with no GPU budget, or route every user prompt through a cloud endpoint that your compliance team has not signed off on. This post gives you an honest local-vs-cloud breakdown of the two most common choices — Meta's Llama Guard and Azure AI Content Safety — and shows how Promptise Foundry puts both behind a single `ContentSafetyDetector` interface so the choice becomes a config flag, not a rewrite.

<!-- more -->

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## What LLM content moderation actually has to catch

Toxicity detection is the part everyone remembers, but it is a small slice of the real surface. Promptise Foundry's `ContentSafetyDetector` classifies text against the 13-category MLCommons AI Safety taxonomy — the same taxonomy Llama Guard was trained on — so both backends produce comparable, category-labeled results:

- **Violent and non-violent crimes** (S1, S2) — planning harm, fraud, phishing instructions.
- **Sex-related crimes and child exploitation** (S3, S4) — the categories you are legally required to block.
- **Weapons, hate speech, self-harm** (S9, S10, S11) — the high-severity harm classes.
- **Specialized advice, privacy, IP, defamation, sexual content, elections** (S5–S8, S12, S13) — the long tail that plain toxicity filters miss entirely.

A finding carries a category code, a label, and a confidence score, so harmful content detection for your LLM is auditable rather than a mysterious boolean. Both providers speak this same vocabulary, which is exactly what makes them swappable.

## Local vs cloud content moderation: the real tradeoff

The distinction that matters is not accuracy — both backends are strong. It is where the data goes and what you have to operate:

| | Local (Llama Guard) | Cloud (Azure AI Content Safety) |
|---|---|---|
| Where prompts go | Never leave your network | Sent to the Azure endpoint |
| Setup cost | Ollama + a ~4.9 GB model | An endpoint URL and an API key |
| Runtime footprint | Local RAM/GPU to hold the model | Zero model downloads on your side |
| Best for | Air-gapped, data-residency, on-prem | Cloud-native apps that want low ops overhead |
| Failure mode | Ollama must be up locally | Network dependency on Azure |

If you are already cloud-native and want zero model downloads, Azure is the pragmatic pick. If you have air-gapped or data-residency requirements, local Llama Guard keeps every prompt inside your perimeter. The trap is hard-coding one of them into your call sites — because the day your deployment target changes, so does your entire moderation layer. Promptise removes that trap.

## Llama Guard in Python: the local backend

The local backend runs Llama Guard through [Ollama](https://ollama.com/), so no prompt ever leaves your machine. Setup is two commands and one line of Python:

```bash
# one-time: pull the safety model
ollama pull llama-guard3
```

```python
from promptise import ContentSafetyDetector

# provider="local" is the default — Llama Guard via Ollama
detector = ContentSafetyDetector(provider="local")
```

That is the whole local path. The model is quantized to roughly 4.9 GB, runs on CPU or GPU, and its default `action` is `BLOCK`, so flagged input is stopped before it reaches your agent. Because the classification happens on your own hardware, there is no per-call API cost and nothing to negotiate with a data-processing addendum. This is the same "runs entirely on your infrastructure" posture that the rest of Promptise's [security guardrails](../../core/guardrails.md) share — injection, PII, and credential detection all run locally too.

## Azure AI Content Safety: the cloud backend

The Azure backend is for teams that would rather not host a multi-gigabyte model. You point the detector at your Azure AI Content Safety resource and pass a key — the value supports `${ENV_VAR}` syntax so secrets stay out of source:

```python
from promptise import ContentSafetyDetector

detector = ContentSafetyDetector(
    provider="azure",
    azure_endpoint="https://your-resource.cognitiveservices.azure.com",
    azure_key="${AZURE_CONTENT_SAFETY_KEY}",  # resolved from the environment
)
```

Nothing to download, nothing to keep warm in memory, and Microsoft maintains the category models for you. The tradeoff is the obvious one: prompts and responses travel to the Azure endpoint, so this is the right call when you are comfortable with cloud processing and want the lowest operational overhead.

## One interface, one config flag

Here is the payoff. Both backends are the same `ContentSafetyDetector` class, so you can select between local Llama Guard and Azure AI Content Safety with a single environment variable, drop the detector into a `PromptiseSecurityScanner`, and wire it into any agent with `guardrails=`. Nothing else in your app changes.

```python
import asyncio
import os

from promptise import build_agent, PromptiseSecurityScanner, ContentSafetyDetector


def content_safety(backend: str) -> ContentSafetyDetector:
    """Same interface, either backend — the only thing that varies is config."""
    if backend == "azure":
        return ContentSafetyDetector(
            provider="azure",
            azure_endpoint="https://your-resource.cognitiveservices.azure.com",
            azure_key="${AZURE_CONTENT_SAFETY_KEY}",
        )
    return ContentSafetyDetector(provider="local")  # Ollama + llama-guard3


async def main():
    backend = os.environ.get("MODERATION_BACKEND", "azure")
    scanner = PromptiseSecurityScanner(detectors=[content_safety(backend)])

    agent = await build_agent(
        model="openai:gpt-5-mini",
        instructions="You are a helpful support assistant.",
        guardrails=scanner,
    )

    result = await agent.ainvoke(
        {"messages": [{"role": "user", "content": "How do I reset my password?"}]}
    )
    print(result["messages"][-1].content)
    await agent.shutdown()


asyncio.run(main())
```

Flip `MODERATION_BACKEND` from `azure` to `local` and the same code runs air-gapped. When the scanner flags harmful input, it raises `GuardrailViolation` before the message reaches the model — you catch it, log the offending category, and return a safe message to the user. You can also compose `ContentSafetyDetector` alongside the other detection heads (injection, PII, credentials) in one scanner, so a single pipeline covers every risk class. The full detector catalog and `ScanReport` fields are documented on the [security guardrails](../../core/guardrails.md) page.

## Defense in depth: moderation plus a sandbox

Content moderation decides what text is *allowed in and out*. It does not decide what your agent is *allowed to do*. Those are two different security boundaries, and production agents need both. If your agent generates or runs code — a common pattern for data-analysis and coding assistants — pair the moderation layer with Promptise's [sandboxed execution](../../core/sandbox.md), which runs code in a hardened Docker container with seccomp syscall filtering, dropped capabilities, a read-only root filesystem, and no network by default. Guardrails stop a malicious prompt from ever reaching the model; the sandbox contains the blast radius of anything the model then tries to execute. Neither layer replaces the other, and turning both on is a one-parameter change (`guardrails=scanner`, `sandbox=True`) on `build_agent()`.

## When each backend is the better fit

This is a real choice, and either answer can be correct.

**Azure AI Content Safety is the better fit when** you are already cloud-native, you do not want to provision GPU or RAM for a local model, and you value Microsoft maintaining the category models over owning the stack yourself. It is the lower-friction starting point for most teams.

**Local Llama Guard is the better fit when** you have air-gapped, on-premise, or data-residency requirements, when sending user prompts to a third party is a compliance non-starter, or when you want zero per-call cost and no external network dependency in your moderation path.

And to be fair about scope: if you are *not* building on Promptise agents and only need cloud moderation for a single endpoint, calling the Azure Content Safety REST API directly is perfectly reasonable and skips a dependency. Promptise's advantage shows up when moderation is one of several guardrails, when you want local and cloud to be interchangeable, and when the same scanner needs to protect an agent, its tools, and its memory consistently. For the broader picture of how content safety sits next to injection and PII detection, see [LLM Guardrails in Python: The Complete Guide](llm-guardrails-python.md), and for the injection-specific head, [How to Detect Prompt Injection Attacks in Python](prompt-injection-detection.md).

## Frequently asked questions

### Is local Llama Guard as accurate as Azure AI Content Safety?

Both classify against the same 13-category safety taxonomy and produce category-labeled findings with confidence scores, so their outputs are directly comparable. Accuracy differences are workload-specific rather than one being categorically better — the deciding factors in practice are data residency, latency, and operational overhead, not raw detection quality.

### Do I need a GPU to run Llama Guard locally?

No. The `llama-guard3` model is quantized to roughly 4.9 GB and runs through Ollama on CPU, though a GPU lowers latency. If your hardware budget is tight, start with the Azure backend — it requires zero local model resources — and move to local only when data-residency requirements demand it.

### Can I run both backends in the same app?

Yes. `ContentSafetyDetector` is one class with a `provider` argument, so you select the backend at construction time from config or an environment variable. You can even instantiate different detectors for different tenants or environments while keeping the rest of your `PromptiseSecurityScanner` and agent code identical.

## Next steps

Start with the Azure backend to ship fast with zero model downloads, then switch `ContentSafetyDetector` to local Llama Guard the day you go air-gapped — it is a one-line change. Follow the [Quick Start](../../getting-started/quickstart.md) to stand up your first agent, then read the [security guardrails](../../core/guardrails.md) guide to compose content safety with injection, PII, and credential detection in a single scanner.
