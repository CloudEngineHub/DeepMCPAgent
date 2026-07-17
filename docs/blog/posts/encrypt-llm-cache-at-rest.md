---
title: "Encrypt Your LLM Response Cache at Rest in Redis"
description: "A semantic cache is a plaintext log of every prompt and answer sitting in Redis, so a snapshot or a rogue read leaks conversations. Mainstream LLM caches…"
keywords: "encrypt llm cache at rest, aes encrypt redis llm cache, encrypt semantic cache values, fernet cache key, secure llm response cache, llm cache encryption at rest"
date: 2026-07-16
slug: encrypt-llm-cache-at-rest
categories:
  - Cost & Efficiency
---

# Encrypt Your LLM Response Cache at Rest in Redis

Learning to **encrypt LLM cache at rest** starts with reckoning with what a semantic cache actually is on disk: a running log of every prompt a user typed and every answer your model gave back, sitting in Redis as plaintext JSON. The feature that makes the cache cheap — remembering answers so you can serve them again — is exactly what turns it into a liability. An RDB snapshot copied to a backup bucket, a replica tapped by a read-only credential, a `KEYS *` from a misconfigured `requirepass`, or an instance accidentally bound to a public interface: any one of those hands over conversations verbatim. Promptise Foundry ships a one-flag answer to this. Set `encrypt_values=True` on `SemanticCache` and the prompt-and-answer payload is encrypted application-side with AES (via Fernet) before it ever reaches Redis, keyed by `PROMPTISE_CACHE_KEY` so it stays readable across restarts. This post walks the threat, the exact toggle, and how to verify ciphertext is really what landed at rest.

<!-- more -->

!!! warning "Not legal or compliance advice"
    The information here is general technical information, not legal, regulatory, or compliance advice. Descriptions of any law, regulation, or standard (such as the GDPR, the EU AI Act, HIPAA, SOC 2, or PCI DSS) are simplified and may be incomplete, out of date, or inaccurate, and requirements vary by jurisdiction and situation. Promptise Foundry makes no warranty as to the accuracy or completeness of this content and is not responsible for how you use or rely on it. Using Promptise does not by itself make you or your product compliant with any law or standard. Consult a qualified lawyer or compliance professional before acting on anything here.


## What actually sits in Redis when you cache LLM responses

When Promptise stores a cache entry in the Redis backend, it serializes a small JSON object and writes it into a Redis hash. That object holds the fields you'd expect a cache to need — and every one of them is sensitive:

- `query_text` — the user's prompt, in full
- `response_text` — the model's answer, in full
- `output` — the serialized LangGraph message list, including tool calls and their arguments
- `model_id`, `instruction_hash`, `context_fingerprint`, `checksum`, `created_at`, `ttl`

Without encryption, that blob is UTF-8 JSON. Anyone who can read the key reads the conversation. That is not a hypothetical: Redis persistence writes RDB snapshots and AOF files to disk by default in most deployments, replication streams the same data to replicas, and `redis-cli HGETALL promptise:cache:entries:<scope>` returns it in one command. The blast radius of a leaked snapshot is "every question every user asked, plus every answer" — which for a support or internal-knowledge agent is often the most sensitive text in the whole system.

The usual mitigation is infrastructure-level encryption at rest: an encrypted EBS volume, a cloud KMS-backed disk, TLS between app and Redis. Those are real controls and you should keep them. But they protect against exactly one thing — someone walking off with the physical disk. They do nothing against a *live* read. A process with a Redis connection, a leaked read-only ACL, a replication credential, or an RDB dump restored on a laptop sees plaintext, because volume encryption is transparent to anything that goes through the database. To keep conversations unreadable to whatever can talk to Redis, the value has to be ciphertext *inside* Redis. That is application-side encryption at rest.

## What other frameworks do today

Be precise and fair here, because the popular caches are competent at what they were built for — they just draw the encryption boundary at the infrastructure, not at the cache value.

- **LangChain's `RedisSemanticCache` and `RedisCache`** store the prompt and the LLM output as strings in Redis. The classes expose connection settings, an embedding, and a similarity threshold — we're not aware of an application-side option on these cache classes that writes the value as ciphertext. To encrypt at rest you configure Redis itself: TLS in transit, disk/volume encryption, or Redis Enterprise's at-rest features. All valid, all provisioned by you at the infra layer, and all transparent to a live read.
- **GPTCache** — used directly, via LangChain's `GPTCache` wrapper, or through **LlamaIndex's** GPTCache integration — splits storage into a scalar store (SQLite or similar, holding the prompt and answer text) and a vector store. Encryption of that scalar store is left to whatever database you point it at; there is no built-in toggle that encrypts the cached answer text before it's written.
- **LlamaIndex's** own KV/cache stores (`SimpleKVStore` and friends) persist plaintext JSON to disk. Again, encryption is delegated to the filesystem or database underneath.

So the honest delta is not "these tools can't be encrypted" — they can, at the disk or database layer, and that's a legitimate design. The precise gap is that none of them, as far as their current cache APIs go, offer an **application-side toggle that makes the cached value ciphertext inside the store**. The plaintext is exposed to anything holding a database connection, and hardening it is separate infrastructure you remember to configure per environment. Promptise's edge is to make value-level encryption a first-class, one-parameter property of the cache object itself, so ciphertext-at-rest is a code decision that travels with the agent — not a checklist item on the Redis box. If you find a value-encryption flag has since shipped in one of these libraries, the delta narrows to "on by a single flag, and re-scanned by guardrails on read," which is where Promptise sits.

## Turn on encryption at rest with `encrypt_values=True`

The toggle is one keyword argument. `SemanticCache(encrypt_values=True)` initializes a Fernet cipher and encrypts the serialized entry — the prompt, the answer, and the tool-call payload — before writing it to Redis. On a cache hit the value is decrypted, re-validated, and (crucially) re-scanned by output guardrails before it is ever returned.

```python
import asyncio
from promptise import build_agent, SemanticCache, CallerContext
from promptise.config import HTTPServerSpec


async def main():
    # Encrypted, Redis-backed semantic cache.
    # encrypt_values=True → cached prompt+answer payloads are AES (Fernet)
    # ciphertext at rest. Reads PROMPTISE_CACHE_KEY from the environment.
    cache = SemanticCache(
        backend="redis",
        redis_url="redis://localhost:6379",
        encrypt_values=True,
        similarity_threshold=0.92,
    )
    cache.warmup()  # pre-load the local embedding model at startup

    agent = await build_agent(
        model="openai:gpt-5-mini",
        servers={"billing": HTTPServerSpec(url="http://localhost:8000/mcp")},
        instructions="You are a support agent. Answer from the billing tools.",
        cache=cache,
    )

    caller = CallerContext(user_id="alice")

    # First call → LLM runs, and the response is ENCRYPTED before it's
    # stored in Redis. The plaintext prompt/answer never touches the DB.
    await agent.ainvoke(
        {"messages": [{"role": "user",
                       "content": "What is my plan's monthly usage limit?"}]},
        caller=caller,
    )

    # Second, similar call → cache hit: the ciphertext is decrypted,
    # re-scanned by output guardrails, then served instantly — no LLM call.
    result = await agent.ainvoke(
        {"messages": [{"role": "user",
                       "content": "How many units does my plan allow each month?"}]},
        caller=caller,
    )
    print(result["messages"][-1].content)

    await agent.shutdown()


asyncio.run(main())
```

Three things are worth stating explicitly about how this works, because they change how you operate it:

**The key lives outside Redis, in your environment.** Encryption reads `PROMPTISE_CACHE_KEY` — a Fernet key, which is 32 url-safe base64 bytes. Fernet is AES-128-CBC with an HMAC-SHA256 authentication tag, so it's *authenticated* encryption: a tampered or truncated ciphertext fails to decrypt rather than yielding garbage. Generate a key once and store it in your secrets manager alongside your other app secrets — never in Redis itself, which is the whole point.

```bash
# Generate a persistent Fernet key for PROMPTISE_CACHE_KEY
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
```

**A missing key means no persistence.** If `PROMPTISE_CACHE_KEY` is unset, Promptise auto-generates a per-process key and logs a warning: `"PROMPTISE_CACHE_KEY not set — auto-generated encryption key. Cache will not survive restarts."` That's a safe default for a quick local test, but in production a per-process key means every restart or every worker gets a different key and can't read what the last one wrote — so the cache silently stops hitting. Set the env var to a fixed key and the ciphertext stays readable across restarts and across every worker sharing the Redis instance.

**Encryption is a Redis-backend property.** The in-memory backend keeps entries in process RAM only — there's no "at rest" to protect — so `encrypt_values` applies to `backend="redis"`. You need `pip install redis` for the backend and `pip install cryptography` for the cipher; the constructor raises a clear `ImportError` if either is missing rather than silently degrading. That "no fallbacks" behavior is deliberate: an encrypted cache that quietly writes plaintext would be worse than no encryption at all.

## Verify ciphertext is really at rest

Encryption you can't observe is encryption you shouldn't trust. The verification is short: run the agent once to populate the cache, then read the raw hash straight from Redis and confirm you can't read it back.

```bash
# List the entry hashes Promptise wrote for this scope
redis-cli --scan --pattern 'promptise:cache:entries:*'

# Dump the raw stored values for one scope's hash
redis-cli HGETALL promptise:cache:entries:user:alice
```

With `encrypt_values=True`, each value comes back as a Fernet token — a `gAAAAA…` base64 blob — with no readable prompt or answer text anywhere in it. Flip the flag off, repeat, and you'll see the JSON in the clear: `{"query_text":"What is my plan's monthly usage limit?", ...}`. That before/after is the proof. The Fernet token starts with a version byte and a timestamp and is HMAC-signed, so you can also confirm tamper-evidence: corrupt one byte in Redis and the next lookup treats the entry as unreadable and evicts it rather than serving a mangled answer.

One honest caveat so you scope this correctly: `encrypt_values` encrypts the *value* — the prompt, answer, and tool payload. The similarity index (the embedding vectors used for the nearest-neighbor search) is stored alongside as raw float32, because the search has to compute dot products over it. Embeddings are numeric vectors, not text, but they are not zero-information, so treat them as you would any derived-data store. The high-sensitivity payload — the actual conversation text — is what becomes ciphertext, which is the leak the description at the top of this post is about: a snapshot or a rogue read no longer yields conversations.

## Where the encrypted cache fits with the rest of your security posture

At-rest encryption is one control among several the cache already applies, and they compose rather than compete:

- **Output guardrails re-scan cached responses on the way out.** A cache hit isn't a bypass of your safety layer — the decrypted value passes back through output guardrails before it's returned, so a response that would be redacted today is redacted even if it was cached yesterday. The full ordering is in the [Semantic Cache docs](../../core/cache.md).
- **Per-user isolation is the default.** Encryption protects the value; scoping controls *who can match it at all*. The default `per_user` scope keys every entry under the caller's partition, and the cache fails closed when no `CallerContext` is present. If your worry is a paraphrase surfacing another tenant's answer rather than a disk leak, that's a distinct failure mode covered in [Can a Paraphrase Leak Another Tenant's Cached Answer?](semantic-cache-cross-tenant-leak.md).
- **The embedding runs locally.** `SemanticCache` uses the same on-device model as [tool optimization](../../core/tool-optimization.md), so encrypting the cache costs no extra API calls and query text never leaves your environment to be embedded by a third party — the plaintext exposure surface shrinks in transit as well as at rest.
- **GDPR erase still works on ciphertext.** `await cache.purge_user("alice")` removes a user's entries whether or not they're encrypted; you don't have to decrypt to delete.

If you turned encryption on because you're running one Redis across many customers, the cost and isolation levers reinforce each other — correct partitioning is exactly what lets you cache aggressively without turning the cache into a liability. [How to Cut Token Cost for a Multi-Tenant AI Agent](cut-token-cost-multi-tenant-ai-agent.md) covers the cache alongside the other savings levers.

## Frequently asked questions

### What algorithm does `encrypt_values=True` actually use?

Fernet, from the Python `cryptography` library — AES-128 in CBC mode with PKCS7 padding and an HMAC-SHA256 authentication tag over the ciphertext. Because it's authenticated, decryption fails cleanly on any tampering or corruption instead of returning wrong plaintext; Promptise treats that failure as a cache miss and evicts the entry. The key is a 32-byte url-safe base64 value read from `PROMPTISE_CACHE_KEY`.

### Do I still need Redis TLS and disk encryption if I set `encrypt_values=True`?

Keep them — they're complementary. Application-side value encryption keeps the prompt and answer as ciphertext inside Redis, defending against a live read, a leaked ACL, or a copied RDB snapshot. TLS protects the value in transit between your app and Redis, and disk encryption protects the whole volume against physical theft. Defense in depth: `encrypt_values` closes the gap those two leave open, which is anything that reads Redis through the front door.

### What happens if I lose or rotate `PROMPTISE_CACHE_KEY`?

The cache becomes unreadable and behaves as a cold cache — decryption fails, entries are treated as misses and evicted, and fresh LLM calls repopulate under the new key. Nothing crashes and no plaintext is exposed; you just lose your existing hit rate until the cache warms again. Because the cache is disposable by design (it's an optimization, never a system of record), key rotation is safe: rotate the env var, accept a temporary drop in hit rate, and move on.

### Does encryption slow down cache hits?

Negligibly. Fernet encrypt/decrypt on a few kilobytes of JSON is microseconds — far below the network round-trip to Redis, let alone the LLM call a hit avoids. The similarity search itself is unaffected because embeddings are stored separately and aren't encrypted, so nearest-neighbor lookup runs at the same speed with the flag on or off.

### Can I encrypt the in-memory cache too?

The `encrypt_values` flag applies to the Redis backend, because "at rest" means "written to a store outside your process." The in-memory backend holds entries only in your application's RAM and never persists them, so there's no on-disk artifact to encrypt; when the process exits, the cache is gone. Use the Redis backend with `encrypt_values=True` when you need persistence and shared workers with at-rest protection.

## Next steps

Follow the Redis encryption setup end to end: install `redis` and `cryptography`, generate a Fernet key with `python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"`, set it as a persistent `PROMPTISE_CACHE_KEY`, and construct `SemanticCache(backend="redis", redis_url=..., encrypt_values=True)`. Then prove it: run one agent invocation, `redis-cli HGETALL` the entry hash, and confirm you see `gAAAAA…` ciphertext instead of prompt text. Read the Redis and encryption sections of the [Semantic Cache docs](../../core/cache.md) for the full parameter reference, and if you're operating one cache across many tenants, pair this with the isolation model in [Can a Paraphrase Leak Another Tenant's Cached Answer?](semantic-cache-cross-tenant-leak.md).
