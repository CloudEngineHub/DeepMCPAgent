---
title: "Rotate an AI Agent's Secret Without Restarting It"
description: "If your agent reads its API key from the environment at boot, rotating that key means a restart — which kills a long-running process and loses its…"
keywords: "rotate ai agent secret without restart, ai agent secrets rotation, hot rotate api key agent, agent credential ttl, secrets management ai agent, rotate api key without downtime"
date: 2026-07-16
slug: rotate-ai-agent-secret-without-restart
categories:
  - Runtime
---

# Rotate an AI Agent's Secret Without Restarting It

To rotate an AI agent's secret without restart, the running process has to be able to swap the credential it holds in memory *while it keeps running* — and that is precisely the operation most agent stacks make impossible. The usual pattern is to read `OPENAI_API_KEY` or a downstream service token from the environment once, at construction time, and hand it to a model client or tool. That value is now frozen inside a live object. When the key leaks, expires, or hits its scheduled rotation, your only lever is to rebuild the client — and for a long-running, self-triggering agent, rebuilding means a restart that tears down the accumulated context, the conversation buffer, and the lifecycle state you worked so hard to keep durable. This post shows how Promptise Foundry's runtime makes the secret a per-process governance concern instead: `${ENV_VAR}` resolution at start, TTL expiry, hot rotation with no restart, and zero-fill revocation on stop — with the value never written to the journal, checkpoint, or status output.

<!-- more -->

The thesis in one line: a credential captured at boot can only be rotated by a reboot. A credential owned by the process — with its own TTL and a `rotate()` you can call live — is rotated in place, and the agent never stops.

## Why reading the key at boot forces a restart

Consider a payments agent on a cron trigger that runs for days, reconciling disputes and issuing refunds through Stripe. It read `STRIPE_API_KEY` from the environment when it started. Three days in, security rotates that key — a routine event, or an urgent one after a suspected leak. The old value is now dead at the provider, but it is still the value your live agent holds. Every tool call it makes from this moment fails with a `401`.

What are your options if the key lives in the environment? An environment variable is fixed for the lifetime of a process: changing `STRIPE_API_KEY` in your deployment does nothing to a process that already booted, because that process copied the value into memory at construction. So you restart. Restarting a long-running [supervised process](../../runtime/processes.md) walks it back through `STOPPING → STOPPED → STARTING → RUNNING`, and unless every scrap of state was checkpointed, the in-memory conversation buffer and any un-journaled working context are gone. You rotated a key by throwing away the agent's short-term memory. For a chatbot a human is watching, that is a shrug. For an autonomous process that has spent an hour building context on an incident, it is a real loss.

There is a second, quieter problem. The environment is *shared*. Every agent, tool, and child process on that host can read `os.environ`, and the value sits in `/proc/<pid>/environ` for anything with access to the box. If agent A needs a Stripe key and agent B needs a GitHub token, both processes can see both secrets. Rotation and isolation are two faces of the same gap: the credential does not belong to the process that uses it, so the process can neither scope it nor swap it.

## What other frameworks do today

To be fair and precise: every major framework can *use* a secret, and most read it from an environment variable or an explicit constructor argument. What they don't ship is a per-process governance layer that owns the credential — with TTL expiry, in-place rotation, access logging, and zero-fill revocation. The credential is captured when you build the client, so "rotating" it means building a new one.

- **LangChain / LangGraph** — a model client like `ChatOpenAI` takes `api_key` (or reads `OPENAI_API_KEY`) at instantiation and holds it for the object's life. There is no rotate-in-place call; to use a new key you construct a new client and rewire it into the chain or graph. LangGraph checkpointers persist graph *state* faithfully, but they do not manage the credential — the key is not part of what a checkpoint owns.
- **CrewAI** — the LLM is configured with a key (argument or env) at `Agent`/`Crew` construction. There is no per-agent credential TTL or a hot-rotation primitive; a new key means reconstructing the agent.
- **AutoGen** — model clients such as `OpenAIChatCompletionClient` take `api_key` at construction. There is no built-in mechanism to expire or swap that credential on a live agent without building a new client.
- **Pydantic AI** — a provider/model is created with an API key (or reads env) up front; there is no in-process credential TTL or rotation API on a running agent.

None of that is a bug in those tools — capturing a key at construction is the obvious default. The point is what it costs a *long-running* agent: rotation requires reconstruction, and reconstruction of a supervised process is a restart, which is exactly the state loss you were trying to avoid.

It is worth naming the other half of the ecosystem honestly, because it is a genuine partial: **secret managers**. HashiCorp Vault, AWS Secrets Manager, and similar tools do rotate the stored value, and Vault's dynamic secrets even issue short-lived, leased credentials with a real TTL. That solves rotation *at the store*. What it does not do is make a running agent adopt the new value — the process is still holding the string it read at boot, and something in-process has to notice the rotation and replace that string without a restart. That "something" is the exact gap Promptise fills. The two compose cleanly: Vault rotates the value and hands you the new one; you push it into the live process with a single `rotate()` call. Promptise's edge is not "we rotate secrets and Vault doesn't" — it is that Promptise makes the *consumer side* of rotation a first-class, structural property of the process, so a rotated key takes effect without ever stopping the agent.

## Secrets as a per-process governance concern

Promptise's runtime gives each process an optional `SecretScope`: an isolated, in-memory credential vault that lives and dies with the process. It is documented in full under [Secret Scoping](../../runtime/governance/secrets.md); here is what makes it the right tool for rotation.

- **`${ENV_VAR}` resolution at start, not capture.** You declare secrets as literal values or `${ENV_VAR}` references. References are resolved from the environment exactly once, when the process starts — after that the value is owned by the scope, not by `os.environ`. Changing the env var later cannot silently poison a running agent, and the resolved value is not visible to sibling processes.
- **TTL expiry.** Each secret gets a TTL — a `default_ttl` for the scope and per-secret `ttls` overrides. When it expires, the next `get()` returns `None` and drops the entry. That gives you a `agent credential ttl` you can align to a lease from Vault or a short-lived cloud token, so a stale credential fails closed instead of lingering.
- **Rotation without restart.** `await scope.rotate(name, new_value)` replaces the value in place. The next `get()` returns the new key. No client is rebuilt, no process transitions, no conversation buffer is lost. This is the whole feature.
- **Zero-fill revocation on stop.** With `revoke_on_stop=True`, stopping the process overwrites every secret's bytes with null and clears the vault, then rotates the internal encryption key so the old ciphertext is undecryptable. The credential does not outlive the process in memory.
- **Never serialized.** The value is Fernet-encrypted in memory and is *never* written to the journal, a checkpoint, or the status output. Every `get()` and `rotate()` logs an entry — but only the secret's **name** and the action (`access`, `rotation`, `expired`, `revoke_all`), never the value. Process status exposes just an `active_secrets` count. This is the sharp contrast with the [AgentContext](../../runtime/context.md), whose key-value state *is* recorded with full mutation history: secrets are deliberately the one thing kept out of everything durable, so an access is auditable while the credential itself never lands on disk.

That last property is what lets rotation be observable without being dangerous. You get a tamper-evident trail of *when the key was read and rotated* — the audit story that durable, journal-backed agents depend on, covered in [Durable Execution for AI Agents in Python](durable-execution-for-ai-agents.md) — while the secret value stays out of the event stream entirely.

## Runnable: rotate a live key and watch it take effect

Here is a complete, runnable script. Every symbol is a real, exported runtime API (`SecretScope` and `SecretScopeConfig` come straight from `promptise.runtime`). It resolves a key from the environment, reads it, rotates it live, proves the value is redacted from any log line, then zero-fills everything on revocation — no restart anywhere in sight.

```python
import asyncio
import os

from promptise.runtime import SecretScope, SecretScopeConfig


async def main() -> None:
    # In production this comes from your secret manager; here we seed the env.
    os.environ["STRIPE_API_KEY"] = "sk_live_OLD_ROTATE_ME"

    scope = SecretScope(
        config=SecretScopeConfig(
            enabled=True,
            secrets={"stripe_key": "${STRIPE_API_KEY}"},
            default_ttl=3600,          # secrets auto-expire after 1 hour
            ttls={"stripe_key": 1800}, # this one after 30 minutes
            revoke_on_stop=True,
        ),
        process_id="payments-1",
    )

    # 1. Resolve ${ENV_VAR} references and arm the TTL timers.
    await scope.resolve_initial()
    print("initial :", scope.get("stripe_key"))
    print("active  :", scope.active_secret_names)

    # 2. The key leaked. Rotate it LIVE — no restart, no lost state.
    await scope.rotate("stripe_key", "sk_live_NEW_ROTATED")
    print("rotated :", scope.get("stripe_key"))

    # 3. The value never leaks into logs or status: sanitize any text.
    line = "charge failed with key sk_live_NEW_ROTATED"
    print("sanitized:", scope.sanitize_text(line))

    # 4. On stop, zero-fill and drop every secret.
    await scope.revoke_all()
    print("after revoke:", scope.get("stripe_key"), "| active:", scope.active_secret_names)


asyncio.run(main())
```

Running it prints:

```text
initial : sk_live_OLD_ROTATE_ME
active  : ['stripe_key']
rotated : sk_live_NEW_ROTATED
sanitized: charge failed with key [REDACTED]
after revoke: None | active: []
```

The agent went from the old key to the new one between two `get()` calls, with nothing torn down in between. The `sanitize_text()` call is what the runtime uses internally to keep secret values out of conversation buffers and status — feed it any string and current secret values come back as `[REDACTED]`.

TTL expiry works the same way, and you can watch it without any provider at all:

```python
import asyncio

from promptise.runtime import SecretScope, SecretScopeConfig


async def main() -> None:
    scope = SecretScope(
        config=SecretScopeConfig(
            enabled=True,
            secrets={"session_token": "tok-abc123"},  # literal value, no env needed
            default_ttl=1.0,                            # expires 1 second after resolve
        ),
        process_id="proc-ttl",
    )
    await scope.resolve_initial()
    print("before expiry:", scope.get("session_token"))
    await asyncio.sleep(1.1)
    print("after expiry :", scope.get("session_token"))  # None — auto-expired
    print("active       :", scope.active_secret_names)


asyncio.run(main())
```

A stale credential turns into `None` on its own, so a forgotten key fails closed instead of quietly working past its lease.

## Wiring it into a long-running process

You rarely construct a `SecretScope` by hand in production — you declare it on the process and let the lifecycle drive it. Set `secrets` on the `ProcessConfig`, and the runtime resolves them right before the agent is built and revokes them on stop:

```python
from promptise.runtime import AgentProcess, ProcessConfig, TriggerConfig, SecretScopeConfig

config = ProcessConfig(
    model="openai:gpt-5-mini",
    instructions="Reconcile open payment disputes until the queue is clear.",
    triggers=[TriggerConfig(type="cron", cron_expression="*/5 * * * *")],
    secrets=SecretScopeConfig(
        enabled=True,
        secrets={
            "stripe_key": "${STRIPE_API_KEY}",
            "db_password": "${DB_PASSWORD}",
        },
        default_ttl=3600,
        ttls={"stripe_key": 1800},   # tighter TTL for the payment key
        revoke_on_stop=True,
    ),
)

process = AgentProcess(name="payments", config=config)
```

The same thing lives in a `.agent` manifest when you want the whole process declared as config:

```yaml
name: payments
secrets:
  enabled: true
  secrets:
    stripe_key: "${STRIPE_API_KEY}"
    db_password: "${DB_PASSWORD}"
  default_ttl: 3600
  ttls:
    stripe_key: 1800
  revoke_on_stop: true
```

Now the [process lifecycle](../../runtime/processes.md) does the work: `start()` calls `resolve_initial()` before the agent is built, and `stop()` calls `revoke_all()` after the agent is torn down — so credentials exist only while the process is genuinely running. When you enable open mode, the agent additionally gets a `get_secret` meta-tool: it never sees keys in its system prompt and must explicitly request one, and every request is logged by name. Because the whole thing rides the same journal, the security posture composes with the rest of the runtime's durability guarantees — the same journal that, as [LangGraph Checkpointing vs Journal-Replay Explained](langgraph-checkpointing-vs-journaling.md) details, deliberately does not restore a conversation buffer on replay. That is one more reason not to throw the buffer away for something as routine as a key rotation.

## Frequently asked questions

### How do I rotate an AI agent's key without restarting the process?

Give the process a `SecretScope` (via `SecretScopeConfig` on its `ProcessConfig`) instead of reading the key from the environment directly. When the key changes, call `await scope.rotate(name, new_value)`. The value is replaced in memory in place; the next `scope.get(name)` returns the new key. No client is rebuilt and no process transition occurs, so the conversation buffer and accumulated context survive.

### Does rotating a secret reset its TTL?

No. `rotate()` replaces the value and marks its source as a rotation, but it keeps the slot's existing expiry. If you resolved a key with a 30-minute TTL and rotate it 20 minutes in, the new value inherits the remaining 10 minutes. If you need a fresh lease window, expire or re-declare the secret rather than relying on rotation to extend it.

### Where do secret values get logged?

Nowhere. Values are Fernet-encrypted in memory and never written to the journal, a checkpoint, or status output. Every `get()`, `rotate()`, expiry, and `revoke_all()` writes a journal entry containing only the secret's **name** and the action — never the value. Process status reports just an `active_secrets` count. That gives you an auditable "who read what, when" trail without ever persisting the credential.

### What happens to secrets when the process stops?

With `revoke_on_stop=True` (the default), `stop()` runs `revoke_all()`: every secret's bytes are overwritten with null, the vault is cleared, and the internal encryption key is rotated so any old ciphertext becomes undecryptable. Secrets do not outlive the process they belonged to.

### Does this replace HashiCorp Vault or AWS Secrets Manager?

No — it composes with them. A secret manager rotates and stores the value; Promptise's scope is the in-process consumer that expires it on a TTL and lets you push a rotated value into a *live* agent without a restart. Point `${ENV_VAR}` (or your fetch code) at Vault, and call `rotate()` when Vault hands you a new lease. The store owns the source of truth; the scope owns adoption inside the running process.

### Can each agent on a host have different secrets?

Yes. Each process owns its own `SecretScope`, resolved from `${ENV_VAR}` references at start and held in that process's memory only. Two agents on the same host no longer both see each other's keys the way they do through shared environment variables — isolation and rotation are the same per-process guarantee.

## Next steps

Wire a `SecretScopeConfig` onto your process with a `default_ttl`, then rotate the key live with `await scope.rotate(...)` and watch a `get()` return the new value with no restart and no lost conversation buffer — start from the runnable script above and swap in your own secret manager as the source. Read the [Secret Scoping reference](../../runtime/governance/secrets.md) for the full API surface, the [Agent Runtime processes guide](../../runtime/processes.md) to see exactly where resolution and revocation hook into the lifecycle, and the [AgentContext reference](../../runtime/context.md) to understand what the runtime *does* persist — so you can see why the credential is the one thing it deliberately never writes down.
