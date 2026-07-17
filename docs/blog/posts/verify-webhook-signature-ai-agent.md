---
title: "Verify Webhook Signatures Before Waking Your Agent"
description: "Focused purely on the signature-verification mechanics (the reactive-trigger overview is a separate post). What a correct HMAC check actually requires\u2026"
keywords: "verify webhook signature ai agent, hmac webhook verification python, timing-safe signature comparison, constant time hmac compare, authenticate webhook before trigger, secure webhook triggered agent"
date: 2026-07-16
slug: verify-webhook-signature-ai-agent
categories:
  - Runtime
---

# Verify Webhook Signatures Before Waking Your Agent

The code that runs when a webhook hits your service has one deceptively small job: **verify webhook signature ai agent** triggers before you let them wake anything, because an unauthenticated POST is an open door straight into an autonomous process. This post is not the tour of reactive triggers — that lives in the [triggers overview](../../runtime/triggers/index.md). It is about the one mechanism that decides whether the request is real: HMAC-SHA256 over the raw body, read from `X-Webhook-Signature`, compared in constant time, and rejected *before* the agent is ever invoked. Get that check right and the rest of your pipeline inherits a trustworthy entry point. Get it wrong and everything downstream is running on forged input.

## What a correct signature check actually requires

A shared-secret HMAC scheme has exactly three moving parts, and each one has a subtle failure mode.

**1. Compute over the raw bytes, not the parsed object.** The sender computes `HMAC-SHA256(secret, raw_body)` over the exact bytes on the wire and puts the hex digest in a header. Your check must run over those same bytes. If you parse JSON first and re-serialize it to hash, key ordering, whitespace, and Unicode escaping will differ from what the sender signed, and every legitimate request fails. The `WebhookTrigger` reads `await request.read()` and hashes that buffer directly — before any JSON parsing — so the bytes it verifies are the bytes that arrived.

**2. Read the signature from the agreed header.** Promptise uses `X-Webhook-Signature: sha256=<hex-digest>` — the same `sha256=`-prefixed shape GitHub uses for `X-Hub-Signature-256`. A missing header, or one that does not start with `sha256=`, is rejected outright with `401` before any comparison happens.

**3. Compare the digests safely.** This is the step that makes or breaks the whole thing, and it is the subject of the next section. For hmac webhook verification python developers reach for the standard library — `hmac` and `hashlib`, no dependency — which is exactly what the trigger does internally:

```python
expected = hmac.new(secret, raw_body, hashlib.sha256).hexdigest()
```

None of this is exotic. What matters is that all three parts are correct *together*, on every request, with no path that skips them.

## The line most implementations get wrong: constant-time comparison

Here is the bug that hides in a huge fraction of hand-rolled webhook handlers. Once you have the expected digest, you compare it against the one the caller sent. The obvious way is:

```python
if expected == actual:   # DANGEROUS
    ...
```

A plain `==` on strings short-circuits: it returns `False` at the first byte that differs. That means a request whose signature shares the first three bytes of the real one takes measurably longer to reject than one that differs at byte zero. An attacker who can send many requests and time the responses can recover the correct signature one byte at a time — a classic timing side channel. The defense is a **timing-safe signature comparison** that always inspects the full length regardless of where the mismatch is. Python ships one for this exact purpose, `hmac.compare_digest`, and the trigger uses it:

```python
if not hmac.compare_digest(expected, actual):
    return web.json_response({"status": "error", "message": "Invalid signature"}, status=401)
```

Is a remote timing attack across the public internet hard to pull off? Yes — network jitter buries the signal, so this is defense in depth rather than a five-minute exploit. But the point of a **constant time hmac compare** is that it costs nothing to do right and removes the class of bug entirely. The failure mode of `==` is not "sometimes slow"; it is "silently exploitable, and no test will ever catch it." Making the safe comparison the *only* comparison — with no un-safe path available — is the difference between a security property and a code-review reminder.

## Reject a tampered request before your agent runs

The most convincing demonstration is to watch a forged request bounce off the door. The script below starts a `WebhookTrigger` with an `hmac_secret`, sends one correctly signed request and one whose body has been tampered with after signing, and shows that only the authenticated one ever reaches the consumer. It uses only the real trigger API and needs no API key or LLM call — `aiohttp` ships with the base `pip install promptise`.

```python
import asyncio
import hashlib
import hmac

import aiohttp

from promptise.runtime.triggers.webhook import WebhookTrigger

SECRET = "whsec_keep_me_out_of_git"


def sign(secret: str, body: bytes) -> str:
    """Compute the value a genuine sender puts in X-Webhook-Signature."""
    digest = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
    return f"sha256={digest}"


async def post(url: str, body: bytes, signature: str) -> int:
    async with aiohttp.ClientSession() as session:
        async with session.post(
            url, data=body, headers={"X-Webhook-Signature": signature}
        ) as resp:
            return resp.status


async def main() -> None:
    # hmac_secret turns on verification. host defaults to loopback (127.0.0.1);
    # set it to "0.0.0.0" only behind a reverse proxy, and never without a secret.
    trigger = WebhookTrigger(
        path="/deploy",
        port=9099,
        host="127.0.0.1",
        hmac_secret=SECRET,
    )
    await trigger.start()
    url = "http://127.0.0.1:9099/deploy"

    payload = b'{"repo": "acme/api", "ref": "main"}'

    # 1) A genuine sender signs the exact bytes it sends.
    good = await post(url, payload, sign(SECRET, payload))
    print("valid signature ->", good)  # 202

    # 2) An attacker rewrites the body but cannot forge the HMAC for it.
    tampered = payload.replace(b"main", b"attacker-branch")
    bad = await post(url, tampered, sign(SECRET, payload))  # signature is for the ORIGINAL body
    print("tampered body   ->", bad)  # 401

    # Only the authenticated request ever enqueued an event.
    event = await trigger.wait_for_next()
    print("woke the agent for ->", event.payload)

    # The tampered request never enqueued anything, so a second wait times out.
    try:
        await asyncio.wait_for(trigger.wait_for_next(), timeout=0.5)
        print("ERROR: tampered request reached the agent")
    except asyncio.TimeoutError:
        print("tampered request never reached the agent")

    await trigger.stop()


asyncio.run(main())
```

Running it prints:

```
valid signature -> 202
tampered body   -> 401
woke the agent for -> {'repo': 'acme/api', 'ref': 'main'}
tampered request never reached the agent
```

Read the last two lines together. The valid POST returns `202 Accepted` and its payload surfaces from `wait_for_next()`, which is what an [agent process](../../runtime/processes.md) consumes to invoke the agent. The tampered POST returns `401` and *never enqueues a `TriggerEvent` at all* — so when you wait for a second event, there is nothing to wait for. This is what it means to **authenticate webhook before trigger**: verification is not a step the agent does after waking up, it is the gate that decides whether the agent wakes up. Full reference for the class, including its `X-Webhook-Signature` handling and the stripped sensitive headers, is on the [event and webhook triggers](../../runtime/triggers/event-webhook.md) page.

## What other frameworks do today

Be fair about the landscape, because "nobody else does this" is almost never true and always erodes trust.

**Hand-rolled Flask or FastAPI handlers.** This is the honest comparison, because it is what most webhook-triggered agents actually are: a route someone wrote. Two failure patterns dominate. Either the handler skips verification entirely — the endpoint accepts any POST and hands it to the agent — or it verifies with a plain `==`, reintroducing the timing side channel described above. The building blocks are all in the standard library, so a careful author *can* get this right; the problem is that "careful" is doing a lot of work, and the unsafe version looks identical in a diff. Mature webhook providers document exactly this: GitHub's `X-Hub-Signature-256` docs tell you to compare with a constant-time function and not `==`, and Stripe's `Stripe-Signature` scheme adds a signed timestamp on top to blunt replay. Those are good models — the gap is that with a hand-rolled agent you re-implement one of them, per service, and own every subtle mistake.

**LangGraph.** Credit where it is due: this is a real, well-engineered framework, and its durability story is genuine (we compare the mechanics in [LangGraph Checkpointing vs Journal-Replay Explained](langgraph-checkpointing-vs-journaling.md)). But the open-source library ships no webhook trigger — you invoke the graph yourself from whatever web server you stand up, so signature verification is entirely your code again. LangGraph Platform, the hosted product, does expose webhook endpoints, so this is a partial feature that lives in the managed service rather than the library you self-host. The precise delta is not "LangGraph can't verify webhooks" — it is *where* the verification lives: in your bespoke ingress, or in a platform you pay to host, rather than in a library primitive you attach in code.

Promptise's edge is structural, not a longer feature list. The signature check is a first-class part of the `WebhookTrigger` itself: you pass `hmac_secret` to the constructor and correct HMAC-SHA256-over-raw-body with `hmac.compare_digest` is *the only* code path. There is no version of the trigger that verifies with `==`, and no version that forwards an unauthenticated body to the agent. Securing a **secure webhook triggered agent** becomes a constructor argument you either set or don't — not a security review you hope someone remembered to do.

## Deny by default, and the limits worth naming

The posture is fail-closed on both ends. With no secret configured, the trigger does not silently run open — it logs a loud warning at startup that any HTTP client can trigger it, so an unauthenticated webhook is a decision you can see in your logs rather than an accident. With a secret configured, a missing, malformed, or mismatched signature is rejected with `401` before the request body is ever enqueued; sensitive headers (`Authorization`, `Cookie`, `Set-Cookie`) are stripped from the event metadata; and the server binds to `127.0.0.1` by default so it is not exposed to the network until you deliberately set `host="0.0.0.0"` behind a proxy.

Now the honest limits, because naming them is what makes the guarantee trustworthy. The scheme is a plain body HMAC without an embedded timestamp, so — like GitHub's and unlike Stripe's — it does not by itself defend against *replay* of a request that was validly signed; if you need that, pair it with a nonce or short-TTL check in the handler. TLS is your reverse proxy's job, not the trigger's. And the shared secret has to reach the sender out of band and stay out of your git history. What the trigger removes is the largest and most common class of mistake — no verification, or non-constant-time verification — and it removes it structurally. One useful side effect for durable pipelines: because a rejected request produces no `TriggerEvent`, it produces no journal entry either, so a forged POST leaves no phantom wakeup to replay after a crash. That tie-in to crash-safe execution is covered in [Durable Execution for AI Agents in Python](durable-execution-for-ai-agents.md).

## Frequently asked questions

### Why hash the raw body instead of the parsed JSON?

Because HMAC is defined over exact bytes. The sender signs the literal request body; if you parse it to a dict and re-serialize before hashing, differences in key order, spacing, or Unicode escaping change the bytes and every valid signature fails. The `WebhookTrigger` reads the raw buffer and verifies it *before* attempting to parse JSON, so the bytes it checks are the bytes that were signed.

### Is a timing attack on `==` really exploitable over a network?

In practice it is hard: network jitter tends to bury the per-byte timing difference, so treat constant-time comparison as defense in depth rather than a trivially exploitable hole. But that is an argument *for* doing it, not against — `hmac.compare_digest` costs nothing extra and eliminates the entire bug class, including the easier local and same-datacenter cases where the signal is far cleaner. There is no scenario where `==` is the better choice.

### Does verifying the signature also stop replay attacks?

No, and it is important to be precise about that. A valid signature proves the body was produced by someone holding the secret and was not altered in transit. It does not prove the request is *fresh*. Promptise's scheme is a plain body HMAC without a timestamp, so a captured-and-resent valid request would still verify. If replay is in your threat model, add a nonce or a timestamp-freshness check in your handler, the way Stripe's timestamped signature does.

## Next steps

Prove it to yourself: set an `hmac_secret` on your `WebhookTrigger`, then send a request whose body you tampered with after signing — the constant-time check rejects it with `401` before your agent ever runs, exactly as the script above shows. From there, read the [event and webhook triggers](../../runtime/triggers/event-webhook.md) reference for the full `WebhookTrigger` API, browse the [triggers overview](../../runtime/triggers/index.md) to see how webhooks sit alongside cron, file-watch, and event triggers, and wire the verified trigger into a durable [agent process](../../runtime/processes.md) so the only requests that ever wake your agent are the ones you can prove came from someone holding the secret.
