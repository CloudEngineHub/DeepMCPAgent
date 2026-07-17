---
title: "Stop Secrets Leaking into LLM Prompts & Tool Calls"
description: "A deep dive into the credential-exfiltration path nobody instruments: API keys, DB URLs, and private keys leaking through prompts, tool arguments, or…"
keywords: "secret detection for llm, credential detection ai, api key leak prevention llm, detect secrets in prompts, gitleaks for ai"
date: 2026-07-16
slug: secret-detection-for-llm
categories:
  - Guardrails
---

# Stop Secrets Leaking into LLM Prompts and Tool Calls

Secret detection for LLM systems is the guardrail almost nobody instruments, and it is the one that quietly turns a helpful agent into an exfiltration channel. Everyone hardens the obvious surfaces — auth on the API, TLS on the wire, secrets in a vault — and then pipes raw user input, model output, and tool arguments straight past all of it. An API key pasted into a support chat, a `DATABASE_URL` echoed back in a debugging answer, an RSA private key baked into code the sandbox is about to run: each one is a credential leaving your trust boundary in plain text. By the end of this article you will know exactly where those leaks happen and how to layer a local, zero-network `CredentialDetector` over your agent so keys get caught the moment they appear.

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## The credential-exfiltration path nobody instruments

Traditional secret scanning lives in CI. Tools like gitleaks and trufflehog scan commits, so a key that lands in git history gets flagged before merge. That is necessary, but agents opened four new leak paths that never touch a repository:

- **Inbound prompts.** A user pastes a stack trace, a config file, or a `.env` snippet into the chat to ask for help. The key is now in your prompt log, your vector memory, and your LLM provider's request payload.
- **Model output.** The model, trying to be helpful, reconstructs or echoes a connection string it saw earlier in the conversation, and hands it straight back to the user.
- **Tool arguments.** An agent calls a tool with `{"query": "select * where token = 'sk-live-...'"}`. That argument is logged, traced, and sometimes forwarded to a third-party MCP server.
- **Sandboxed code.** An agent writes a script containing a hardcoded key, and your executor runs it — potentially with network access to the very service that key unlocks.

None of these paths pass through git, so CI scanning never sees them. Credential detection AI systems have to move the check to runtime, sitting directly in the request/response path of the agent. That is a different job from repository scanning, and it needs a detector wired into agent I/O rather than a pre-commit hook.

## Gitleaks for AI: 96 credential patterns that run locally

Promptise Foundry ships `CredentialDetector`, one of six composable detection heads in the [security guardrails](../../core/guardrails.md) scanner. It brings the accuracy of repo-scanning tooling to runtime traffic by reusing the same battle-tested rules: 96 regex patterns derived from [gitleaks](https://github.com/gitleaks/gitleaks) and [trufflehog](https://github.com/trufflesecurity/trufflehog), covering 62 credential categories across 60+ services.

That includes the credentials most likely to show up in an agent conversation:

- **Cloud providers** — AWS access and secret keys, GCP service accounts and API keys, Azure storage and AD client secrets.
- **Model providers** — OpenAI, Anthropic, and Hugging Face tokens (including legacy formats).
- **Source control and CI** — GitHub PATs (classic and fine-grained), GitLab CI and deploy tokens.
- **Payments and comms** — Stripe live/test/restricted keys, Slack bot tokens and webhooks, Twilio, SendGrid.
- **Infrastructure** — HashiCorp Vault tokens, private keys (RSA, DSA, EC, SSH, PGP), and database connection strings for PostgreSQL, MySQL, MongoDB, and Redis.

The important property is where this runs. Every pattern is plain regex with a 0 MB footprint, executed in-process on your own hardware. There is no model download and no API call, so scanning for secrets never ships those secrets anywhere. That distinction matters: a hosted moderation endpoint that "checks for secrets" has to receive the secret to check it. Local `gitleaks for AI` detection never does.

## Wire secret detection into your agent in three lines

Detectors compose into a `PromptiseSecurityScanner`, and the scanner drops straight onto `build_agent()` via the `guardrails` parameter. Input is scanned before it reaches the model; output is scanned before it reaches the user. The default action for credentials is redaction — the span is replaced with a descriptive label, so the conversation continues but the raw key never crosses the boundary.

```python
import asyncio

from promptise import build_agent, PromptiseSecurityScanner, CredentialDetector
from promptise.config import HTTPServerSpec


async def main():
    # One regex-only detection head — no model download, sub-millisecond scans.
    scanner = PromptiseSecurityScanner(detectors=[CredentialDetector()])

    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={"api": HTTPServerSpec(url="http://localhost:8000/mcp")},
        instructions="You are a support assistant. Never repeat secrets back to users.",
        guardrails=scanner,
    )

    # A user pastes a key into the chat. It is redacted before the model sees it,
    # and redacted again on the way out if the model tries to echo it.
    result = await agent.ainvoke({
        "messages": [{
            "role": "user",
            "content": "My deploy is failing with key AKIAIOSFODNN7EXAMPLE — what's wrong?",
        }]
    })
    print(result["messages"][-1].content)

    await agent.shutdown()


asyncio.run(main())
```

You can inspect exactly what a scan found without an agent in the loop, which is handy for tests and audit tooling. `scan_text` returns a `ScanReport` with the redacted string and structured findings:

```python
report = await scanner.scan_text("Here is my key: AKIAIOSFODNN7EXAMPLE")

print(report.passed)                # False
print(report.redacted_text)         # "Here is my key: [AWS_ACCESS_KEY]"
print(report.findings[0].category)  # "aws_access_key"
print(report.scanners_run)          # ["credential"]
```

Each `SecurityFinding` carries the detector, category, severity, matched span, character offsets, and action, so you can route findings into your own audit log or alerting. If you prefer hard failures over redaction, set `CredentialDetector(action=Action.BLOCK)` and the scanner raises `GuardrailViolation` instead of masking — the message never reaches the agent at all.

## API key leak prevention for tool calls and sandboxed code

The most dangerous leak path is the one that runs code. When you enable Promptise's [Docker sandbox](../../core/sandbox.md), an agent can write and execute scripts — and a script with a hardcoded secret is both a leak and a live credential inside an execution environment. The same scanner defends this surface, because it operates on the text the agent produces, and the code an agent writes is just text before the sandbox runs it.

A practical layered setup looks like this:

- **On agent I/O:** `guardrails=scanner` catches keys in prompts and responses, as shown above.
- **On generated code:** run `await scanner.scan_text(code)` on any snippet before it reaches the executor, and refuse or redact when a credential pattern fires. This is your last line of `API key leak prevention` for LLM-authored code.
- **On the sandbox itself:** keep network isolation set to `none` so that even a key that slips through cannot phone home. The sandbox's default posture — seccomp filtering, dropped capabilities, no network — means a leaked key inside the container has nowhere to go.

Combining the two heads is the whole point of composable guardrails: `CredentialDetector` decides *what* is a secret, and the sandbox decides *what a leaked secret can do*. For the broader picture of how these detection heads fit together, the [LLM Guardrails in Python guide](llm-guardrails-python.md) walks through all six risk classes end to end.

## Tune categories, exclusions, and standalone scans

Running all 96 patterns on every message is cheap, but you can narrow the surface when you know your threat model. Pass a set of `CredentialCategory` values to scan only what matters, or `exclude` specific pattern names that produce noise in your domain:

```python
from promptise import PromptiseSecurityScanner, CredentialDetector
from promptise import CredentialCategory

# Only the credentials this app could plausibly touch.
scanner = PromptiseSecurityScanner(
    detectors=[
        CredentialDetector(categories={
            CredentialCategory.AWS,
            CredentialCategory.OPENAI,
            CredentialCategory.DATABASE_URL,
        }),
    ],
)

# Discover every pattern name available for fine-grained control.
print(PromptiseSecurityScanner.list_credential_patterns())
# ['aws_access_key', 'github_pat', 'stripe_live', 'openai_key', ...]
```

Because the scanner also detects prompt injection with a local model, credential detection pairs naturally with the injection head — an attacker who tries to trick the agent into dumping its environment variables trips both. If injection is your primary concern, [How to Detect Prompt Injection Attacks in Python](prompt-injection-detection.md) covers that head in depth.

## When gitleaks or trufflehog is the better fit

Runtime detection does not replace repository scanning, and it would be dishonest to pretend otherwise. If your goal is to keep secrets out of git history, out of build artifacts, or out of Terraform state, run gitleaks or trufflehog in CI and pre-commit hooks — they are purpose-built for filesystem and version-control scanning, support entropy-based detection of unknown key shapes, and can verify whether a leaked key is still live. `CredentialDetector` deliberately reuses their published patterns, but it is scoped to a different job: catching secrets in the live request/response and tool-call path of an agent, where CI never runs.

Use both. CI scanning stops secrets from being committed; runtime detection stops secrets from being *spoken* — pasted into a prompt, echoed in an answer, or embedded in code an agent is about to execute. They cover disjoint surfaces, and mature systems need coverage on both.

## Frequently asked questions

### Does scanning for secrets send my prompts to a third party?

No. `CredentialDetector` is 96 local regex patterns with a 0 MB footprint, executed in-process. Nothing about a scan leaves your infrastructure — which is the entire point, since a hosted "secret checker" would have to receive the secret to inspect it. This makes it safe for air-gapped and regulated environments.

### What happens when a credential is detected?

By default the matched span is redacted and replaced with a descriptive label (for example `AKIAIOSFODNN7EXAMPLE` becomes `[AWS_ACCESS_KEY]`), so the conversation continues without the raw key. Set `CredentialDetector(action=Action.BLOCK)` to reject the message outright with a `GuardrailViolation` instead. Every match is recorded as a structured `SecurityFinding` you can log or alert on.

### Can I detect secrets in code an agent writes before it runs?

Yes. Call `await scanner.scan_text(code)` on any generated snippet and inspect `report.passed` before handing it to the [sandbox](../../core/sandbox.md). Combined with the sandbox's default no-network isolation, this stops a hardcoded key from both leaking and being used inside the execution environment.

## Next steps

Layer `CredentialDetector` over your agent I/O and sandbox to catch key leaks in real time — it is three lines to add and runs entirely on your own hardware. Start with the [Quick Start](../../getting-started/quickstart.md) to stand up an agent, then follow the [security guardrails guide](../../core/guardrails.md) to compose credential detection with injection, PII, and content-safety heads into a single local scanner.
