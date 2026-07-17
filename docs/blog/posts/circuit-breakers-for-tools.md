---
title: "Circuit Breakers for AI Agent Tools: Resilience 101"
description: "When a downstream tool degrades, a naive agent retries in a hot loop and burns tokens; this shows a per-tool circuit breaker plus timeout and concurrency…"
keywords: "circuit breakers for tools, circuit breaker middleware, agent tool resilience, tool timeout handling, cascading failure LLM tools, half-open circuit state"
date: 2026-07-16
slug: circuit-breakers-for-tools
categories:
  - Production
---

# Circuit Breakers for AI Agent Tools: Resilience 101

Circuit breakers for tools are the difference between an agent that degrades gracefully and one that burns your token budget while a downstream API is on fire. When a tool starts failing—your payment provider times out, a search API returns 503s—a naive agent does the worst possible thing: it retries in a hot loop, waits out every 30-second timeout, and asks the model to "try again" until your bill and your latency both spike. By the end of this post you'll know how to wrap a flaky tool in a per-tool breaker, pair it with timeout and concurrency limits, and turn a trip into a clean, retryable error the model can reason about.

## Why a degraded tool cascades through an agent

An LLM agent is a retry engine by design. It calls a tool, reads the result, and if the result looks wrong it plans another call. That behavior is exactly what you want when the tool is healthy and exactly what you don't want when it isn't.

Here's the failure mode, step by step:

- A downstream dependency slows down or starts erroring.
- Each call now blocks for the full timeout before failing.
- The model sees an error, "reasons" about it, and calls the same tool again.
- In-flight calls pile up, connections stay open, and the whole server slows.
- Every retry is another LLM turn—more tokens, more latency, no progress.

This is a cascading failure in LLM tools: one sick dependency drags down the agent, the server, and every other request sharing it. The fix isn't to retry harder. It's to **fail fast, stop hammering the dependency, and give it room to recover.** That's what a circuit breaker does, and Promptise Foundry ships one as composable server-side middleware.

## Circuit breakers for tools: closed, open, half-open

A circuit breaker is a small state machine that sits in front of a tool and tracks its recent health. It has three states, and understanding them is most of the battle:

| State | What happens |
|-------|--------------|
| **Closed** | Normal operation. Calls pass through; consecutive failures increment a counter. |
| **Open** | The breaker has tripped. Every call is rejected *immediately* with `CircuitOpenError`—no waiting on the dead dependency. |
| **Half-Open** | After a recovery timeout, the breaker lets one probe call through. Success closes it; failure opens it again. |

The **half-open circuit state** is the clever part. Instead of flapping back to full traffic the instant a timer expires, the breaker admits exactly one request as a canary. If your payment API is genuinely back, that probe succeeds and normal traffic resumes. If it's still down, one failed probe re-opens the circuit and you wait another interval—no thundering herd, no re-triggered outage.

Compare that to plain retries. Retries assume the *next* attempt is independent of the last one, which is false during an outage: the tenth retry fails for the same reason the first did, just later and more expensively. A breaker treats failures as correlated and stops paying for attempts that can't succeed.

## Wire up circuit breaker middleware with timeout and concurrency limits

A breaker alone isn't enough—it needs something to fail fast *against*. In Promptise Foundry you compose three pieces of middleware into one resilience layer:

- `TimeoutMiddleware` — bounds how long any single call can hang, so a slow dependency produces a fast failure instead of a 30-second stall.
- `ConcurrencyLimiter` — caps in-flight calls so a load spike can't exhaust the server while calls back up.
- `CircuitBreakerMiddleware` — counts consecutive failures per tool and trips the circuit when a dependency is clearly unhealthy.

The order matters: shed load, bound each call, then trip on repeated failures. Here's a complete, runnable server:

```python
import asyncio
from promptise.mcp.server import (
    MCPServer,
    CircuitBreakerMiddleware,
    TimeoutMiddleware,
    ConcurrencyLimiter,
    CircuitOpenError,
    ToolError,
    TestClient,
)

server = MCPServer(name="payments")

# 1. Cap in-flight work so a slow dependency can't exhaust the server.
server.add_middleware(ConcurrencyLimiter(max_concurrent=50))

# 2. Bound every call so a hang fails fast instead of holding a connection.
server.add_middleware(TimeoutMiddleware(default_timeout=5.0))

# 3. Trip the breaker after repeated failures; probe recovery after 30s.
breaker = CircuitBreakerMiddleware(
    failure_threshold=5,
    recovery_timeout=30.0,
    excluded_tools={"health"},   # never circuit-break your liveness probe
)
server.add_middleware(breaker)


@server.tool(timeout=3.0, max_concurrent=10)
async def charge_card(customer_id: str, amount_cents: int) -> dict:
    """Charge a customer via the upstream payment API."""
    # Your real provider call goes here.
    return {"customer_id": customer_id, "amount_cents": amount_cents, "status": "succeeded"}


# Turn an open circuit into a structured, retryable error for the agent.
@server.exception_handler(CircuitOpenError)
async def on_open(ctx, exc):
    return ToolError(
        message=f"Payments temporarily unavailable. Retry in {exc.retry_after:.0f}s.",
        code="SERVICE_UNAVAILABLE",
        retryable=True,
    )


async def main():
    client = TestClient(server)
    result = await client.call_tool(
        "charge_card", {"customer_id": "cus_1", "amount_cents": 1999}
    )
    print(result)                            # {'customer_id': 'cus_1', ..., 'status': 'succeeded'}
    print(breaker.get_state("charge_card"))  # CircuitState.CLOSED

asyncio.run(main())
```

Two details worth calling out. The `@server.tool()` decorator takes its own `timeout` and `max_concurrent`, so you can give a specific tool a tighter budget than the server default—`charge_card` fails after 3 seconds even though the server-wide default is 5. And `excluded_tools={"health"}` keeps your Kubernetes liveness probe out of the breaker, so a tripped payment circuit never makes the pod look dead.

## Agent tool resilience: fail fast, shed load, recover

The three middlewares reinforce each other, and that composition is where agent tool resilience actually comes from:

- When the dependency **hangs**, `TimeoutMiddleware` converts a stall into a fast, retryable `ToolError`—and each of those failures is a tick toward the breaker's threshold.
- When traffic **spikes**, `ConcurrencyLimiter` returns a retryable rate-limit error instead of letting blocked calls stack up and eat memory.
- When failures **cluster**, `CircuitBreakerMiddleware` opens after `failure_threshold` consecutive misses and rejects further calls in microseconds until `recovery_timeout` elapses.

Because the breaker raises `CircuitOpenError` with a `retry_after` value, the exception handler above hands the model a message it can act on ("retry in 28s") rather than an opaque stack trace. That closes the loop: the agent stops hammering a dead tool, the tool gets breathing room, and the half-open probe brings it back online automatically. You can also inspect and steer the breaker at runtime—`breaker.get_state("charge_card")` returns the current `CircuitState`, and `breaker.reset()` clears it after you've fixed the dependency by hand.

The full menu of resilience middleware, including health checks and webhook alerting, lives in the [resilience patterns guide](../../mcp/server/resilience-patterns.md). If you're hardening a server for real traffic, read it alongside the broader [production features reference](../../mcp/server/production-features.md), which covers how the middleware chain is pre-compiled so this protection adds effectively zero per-call overhead. A breaker also composes cleanly with response caching—when the circuit is open, a warm cache can still serve recent results, a pattern the [caching and performance guide](../../mcp/server/caching-performance.md) walks through.

## Circuit breaker vs. plain retries: when a breaker is overkill

Being honest about scope matters more than selling the feature. A circuit breaker earns its keep when a tool calls a **shared, remote dependency** that can fail for everyone at once: a third-party API, a database, another microservice. There, correlated failures are the norm and failing fast saves real money.

A breaker is overkill—or actively unhelpful—in a few cases:

- **Pure, local tools.** A function that does math or string formatting has no downstream to protect. Wrapping it adds state for nothing.
- **Expected, per-request errors.** A card that gets declined isn't an outage; it's a normal business result. Counting declines toward a failure threshold would trip the circuit on healthy inputs. Reserve breakers for *infrastructure* failures, not validation ones.
- **Very low traffic.** If a tool is called a few times an hour, plain retries with backoff are simpler and the breaker rarely has enough signal to act on.

For those situations, `TimeoutMiddleware` plus ordinary retry-with-backoff at the call site is the better fit—less machinery, same safety. The breaker is the right tool specifically when repeated fast failures are cheaper than repeated slow ones, and when a struggling dependency benefits from being left alone to recover. For a wider view of what belongs in a production deployment—auth, audit, health probes, and resilience together—see [The Production AI Agent Checklist](production-ai-agent-checklist.md).

## Frequently asked questions

### What is a circuit breaker for AI agent tools?

It's a small state machine that sits in front of a tool and tracks consecutive failures. After a threshold of failures it "opens" and rejects further calls immediately instead of waiting on a dead dependency, then periodically admits one probe call (the half-open state) to test recovery. This stops an agent from retrying a broken tool in a loop and burning tokens.

### How is a circuit breaker different from retries?

Retries assume each attempt is independent, which is false during an outage—every retry fails for the same reason, just slower and more expensively. A circuit breaker treats failures as correlated: once a dependency is clearly down, it stops sending traffic entirely for a recovery window, then tests with a single probe. Use retries for transient blips, and a breaker for sustained failures.

### Does `CircuitBreakerMiddleware` slow down healthy calls?

No. In the closed state it just increments an in-memory counter per tool, and Promptise Foundry pre-compiles the middleware chain so there's effectively no per-call overhead. When the circuit is open, calls are rejected in microseconds—far faster than waiting on a timing-out dependency.

## Next steps

Read the [resilience patterns page](../../mcp/server/resilience-patterns.md) and wrap your flakiest tool in a `CircuitBreakerMiddleware` today—start with `failure_threshold=5` and a `recovery_timeout` that matches how long your dependency typically takes to recover. New to the framework? The [Quick Start](../../getting-started/quickstart.md) gets a server running in a few minutes, and the [production features reference](../../mcp/server/production-features.md) shows how breakers fit alongside auth, rate limiting, and audit logging in a hardened deployment.
