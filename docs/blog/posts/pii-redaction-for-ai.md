---
title: "PII Redaction for AI: Mask Sensitive Data in Prompts"
description: "Ranks by covering both directions developers forget: redacting PII in outgoing prompts AND in model responses (and in redacted approval-request arguments)…"
keywords: "pii redaction for ai, redact pii llm, mask sensitive data ai, gdpr llm compliance, pii detection python"
date: 2026-07-16
slug: pii-redaction-for-ai
categories:
  - Guardrails
---

# PII Redaction for AI: Mask Sensitive Data in Prompts

PII redaction for AI is the control that keeps credit card numbers, national IDs, and email addresses from leaking into an LLM prompt, into the model's response, or into the logs in between. Most teams add it in one direction — they scrub what the user types — and quietly ship the other two. By the end of this post you'll know why redaction has to run on outgoing prompts, on model responses, and on the arguments of any tool call a human is asked to approve, and you'll have runnable Python that does all three offline with Promptise Foundry's `PIIDetector`.

<!-- more -->

The failure mode is boring and expensive. A support agent pastes a full customer record into a chat, the model echoes an SSN back verbatim, and now that SSN is sitting in your conversation store, your observability timeline, and possibly a third-party model provider's logs. Redaction that only guards the input never catches it.

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## Redaction is a two-way street (and then some)

When people say "mask sensitive data for AI," they usually picture one arrow: user text going into the model. That's the easy half. Real systems leak PII in at least three places:

- **Inbound prompts.** The user (or an upstream tool) supplies raw PII. You don't want it in the prompt the provider sees, and you don't want it in memory or the semantic cache.
- **Outbound responses.** The model summarizes a record and repeats a card number, a phone, or a diagnosis code. This is the direction most guardrail setups miss, because the danger is in text your own model produced.
- **Approval-request arguments.** When a human reviews a pending tool call — "send this email," "issue this refund" — the arguments they see can themselves contain PII. A reviewer shouldn't need to see a customer's full card number to approve a charge.

Promptise treats all three as one scanning surface. The [security guardrails](../../core/guardrails.md) module scans input **before** it reaches the agent and scans output **after** the agent responds, replacing detected spans with descriptive labels like `[CREDIT_CARD_VISA]` or `[EMAIL]`. The original value never reaches the user, and — for inbound text — never reaches the model.

## Inside PIIDetector: 69 regex patterns plus Luhn validation

`PIIDetector` is the PII head of the scanner. It's deliberately not an ML model — it's 69 regex patterns with a validation layer, which is what makes reliable **PII detection in Python** cheap enough to run on every message:

- **Zero model weight.** The detector is 0 MB and fully offline. No download, no GPU, no data leaving your infrastructure.
- **Luhn-validated cards.** Credit and debit card patterns (Visa, Mastercard, Amex, Discover, Diners, JCB, UnionPay, Maestro) are checked with the Luhn checksum, so a random 16-digit order number doesn't get flagged as a card.
- **22+ countries of government IDs.** US SSN/ITIN/EIN, UK NINO and NHS numbers, Canada SIN, France INSEE, Germany Personalausweis, India Aadhaar/PAN, Singapore NRIC, and more.
- **Contextual patterns to kill false positives.** Ambiguous formats (passport numbers, ZIP codes, IBANs) only match when a keyword precedes them. `passport: AB1234567` matches; `reference: AB1234567` does not.

You enable everything by default, or narrow it with the `categories` and `exclude` parameters:

```python
from promptise import PIIDetector, PIICategory

PIIDetector()                                                       # all 69 patterns
PIIDetector(categories={PIICategory.CREDIT_CARDS, PIICategory.SSN})  # only these
PIIDetector(exclude={"blood_type", "ip_address"})                   # drop noisy ones
```

## Redact PII in LLM prompts and responses

Here's the part that matters: wiring redaction into a real agent so it runs on every turn. The scanner is a composable set of detectors; you pass the ones you want and hand the scanner to `build_agent(...)` as `guardrails`. This example is runnable end-to-end — the only requirement is an `OPENAI_API_KEY`.

```python
import asyncio
from promptise import (
    build_agent,
    PromptiseSecurityScanner,
    PIIDetector,
    CredentialDetector,
    PIICategory,
)

async def main():
    # PII redaction + credential redaction, both offline.
    scanner = PromptiseSecurityScanner(
        detectors=[
            PIIDetector(categories={
                PIICategory.CREDIT_CARDS,
                PIICategory.SSN,
                PIICategory.EMAIL,
                PIICategory.PHONE,
            }),
            CredentialDetector(),
        ],
    )
    scanner.warmup()  # pre-load; safe to call at startup

    agent = await build_agent(
        model="openai:gpt-5-mini",
        instructions="You summarize customer records for support agents.",
        guardrails=scanner,   # scans input before, output after
    )

    result = await agent.ainvoke({"messages": [{"role": "user", "content":
        "Summarize this account: Jane Doe, card 4532015112830366, "
        "SSN 512-74-8291, email jane@acme.com"}]})

    # Any PII the model repeats is replaced with a label before you see it.
    print(result["messages"][-1].content)
    await agent.shutdown()

asyncio.run(main())
```

To **redact PII from an LLM** response outside of a full agent — say, before you write a model completion to a log — you can call the scanner directly. `check_output` returns the redacted string:

```python
clean = await scanner.check_output(
    "Confirmation for jane@acme.com, card ending 4532015112830366"
)
# → "Confirmation for [EMAIL], card ending [CREDIT_CARD_VISA]"
```

Two things to note. First, PII is **redacted**, not blocked — the response still flows, minus the sensitive spans. Injection attempts, by contrast, are blocked outright. Second, because redaction happens inside the guardrail layer, it also covers cached responses: the [semantic cache](../../core/guardrails.md) re-scans anything it serves, so a poisoned cache entry can't route around your policy.

## Redacted approval requests: the direction everyone misses

The third leak is subtle. When your agent uses human-in-the-loop approval for sensitive actions, the reviewer is shown the tool call's arguments — and those arguments routinely contain PII. Promptise's [agent-side approval](../../core/approval.md) closes this gap with a single flag: `redact_sensitive=True` runs every argument through the same PII and credential detection before the request reaches a human.

```python
from promptise import ApprovalPolicy, CallbackApprovalHandler

policy = ApprovalPolicy(
    tools=["send_email", "issue_refund"],
    handler=CallbackApprovalHandler(my_review_fn),
    include_arguments=True,      # show args to the reviewer
    redact_sensitive=True,       # but mask PII/credentials first
)
```

Now the reviewer sees `{"to": "[EMAIL]", "amount": 49.99}` instead of the raw address. They can still make the call — approve, deny, or edit the arguments — without ever being exposed to data they don't need. `redact_sensitive` requires the guardrails module and reuses the exact detectors above, so your redaction rules stay consistent across prompts, responses, and approvals. (The approval doc describes the full policy surface, including `include_arguments=False` when reviewers shouldn't see arguments at all.)

## GDPR LLM compliance, and when a cloud DLP is a better fit

Redaction is a data-minimization control, and that framing is what makes it useful for **GDPR LLM compliance**. Article 5's minimization principle says you shouldn't process more personal data than you need; a support-summary agent almost never needs a raw SSN to do its job. Running `PIIDetector` on input means the model — and any third-party provider behind it — processes `[SSN]`, not the number. Because everything is offline and deterministic (regex plus Luhn, no model call), you also avoid shipping personal data to yet another vendor just to detect personal data. Pair this with the broader defensive stack in [LLM Guardrails in Python: The Complete Guide](llm-guardrails-python.md) to cover injection and credential leakage in the same pass.

Be honest about the limits, though. `PIIDetector` is pattern-based, so it excels at structured identifiers (cards, SSNs, IBANs, emails) and is weaker on free-form entities like a person's name or a street address buried in prose — that's what the optional GLiNER-based `NERDetector` is for. If you need contractual data-processing guarantees, enterprise audit workflows, or classification across images and files, a dedicated cloud DLP service (Google Cloud DLP, AWS Macie, Microsoft Purview) is the better fit and integrates alongside Promptise rather than competing with it. Reach for `PIIDetector` when you want fast, offline, per-request masking with no vendor round-trip; reach for a managed DLP when compliance scope extends beyond the prompt path.

## Frequently asked questions

### Does PII redaction slow down every request?

For `PIIDetector` and `CredentialDetector`, the cost is regex matching over the message text — effectively free, and fully offline. There's no model load and no network call, so you can run them on every input and output without a latency budget. Only the ML-based heads (injection, NER, content safety) carry model-load cost, which `warmup()` pays once at startup.

### What does a redacted value look like to the model or user?

Detected spans are replaced with descriptive labels rather than deleted, so structure is preserved. A Visa number becomes `[CREDIT_CARD_VISA]`, an email becomes `[EMAIL]`, and an SSN becomes `[SSN]`. The surrounding text stays intact, so the model still understands the sentence — it just never sees the sensitive value.

### Can I redact only specific PII types?

Yes. Pass a `categories` set of `PIICategory` members to enable just those groups, or use `exclude` to drop noisy patterns like `ip_address` or `blood_type`. This keeps false positives low when you only care about, say, cards and national IDs.

## Next steps

Enable `PIIDetector` in your scanner so users never see leaked data and the agent never sees raw PII — it's one detector in a list and a single `guardrails=` argument on `build_agent()`. Start with the [Quick Start](../../getting-started/quickstart.md) to stand up an agent, then read the full [security guardrails guide](../../core/guardrails.md) to add injection blocking, credential detection, and output redaction across every direction your data flows.
