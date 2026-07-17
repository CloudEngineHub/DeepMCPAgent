---
title: "Long-Running MCP Tools Without Client Timeouts"
description: "MCP is request/response, so a ten-minute report-generation tool times the client out and loses the result. The protocol has no async job primitive and…"
keywords: "long-running mcp tool job queue, async mcp tool, mcp job queue, mcp tool timeout, background mcp task, queue_submit mcp"
date: 2026-07-16
slug: long-running-mcp-tool-job-queue
categories:
  - MCP
---

# Long-Running MCP Tools Without Client Timeouts

A long-running MCP tool job queue exists because of one hard fact about the Model Context Protocol: a tool call is a single request/response exchange, so a report that takes ten minutes to build times the client out and loses its result long before your handler returns. The agent sends `tools/call`, the connection holds open waiting for a reply, and somewhere around the 30-, 60-, or 120-second mark — whatever your transport, proxy, or client library decided — the request is abandoned. Your handler may still be running server-side, dutifully finishing the PDF, but there is no longer anyone listening for the answer. The work happened; the result evaporated.

<!-- more -->

This post walks through why that failure is structural to MCP, what other frameworks do about it today, and how Promptise Foundry's `MCPQueue` turns any slow tool into a durable job with `queue_submit` / `queue_status` / `queue_result` / `queue_cancel` / `queue_list`, priority scheduling, exponential-backoff retry, and live progress reporting.

## Why a request/response tool call can't survive a long job

MCP is, at its core, JSON-RPC over a live transport. When an agent calls a tool, that call is bound to the open connection for its entire lifetime. The protocol does give you two useful primitives around long work — a progress notification stream (a server can push `notifications/progress` while it runs) and request cancellation (`notifications/cancelled`) — but both of those ride on the *same in-flight request*. They make a long call more observable; they do not make it durable. The moment the connection drops, the client times out, or the agent's HTTP library gives up, the request and everything it would have returned go with it.

That is the gap. There is no MCP primitive that says "accept this work, hand me back a ticket, and let me come back for the answer later." A synchronous `tools/call` cannot outlive its own socket. So the naive fix — "just make the handler faster" — is not always available. Report generation, data-pipeline runs, model training, batch document processing, large exports: this work is genuinely slow, and no amount of tuning collapses ten minutes into the two seconds your client is willing to wait.

The correct shape for slow work is the same one every mature system converges on: submit the job, get an identifier, poll (or subscribe) for status, and fetch the result when it is ready. The job's lifetime is decoupled from any single request. What MCP lacks is a *native* way to express that shape at the tool layer — and that is exactly the hole `MCPQueue` fills.

## What other frameworks do today

It is worth being precise about where the gap is, because "no one has this" would be both untrue and unfair. The honest version is that the pieces exist in adjacent layers — just not, in most stacks, as an MCP-native durable job that any client can drive.

**The MCP protocol itself** has progress notifications and cancellation, as noted above, but no durable job handle. A call is one request bound to one connection; there is no submit-now-fetch-later primitive in the base spec.

**FastMCP** (the widely used Python MCP framework, parts of which now live in the official `mcp` SDK) supports async tool handlers and progress reporting through its request context — you can `await ctx.report_progress(...)` inside a slow tool. That is a real, partial overlap, and it is worth acknowledging plainly: FastMCP makes a long call *observable*. What it does not ship is a durable job store with submit/poll/result-by-id semantics exposed as MCP tools. If your report needs to outlive the request, you reach for Celery, RQ, or a Redis-backed worker of your own, and then you hand-write and register the `submit`/`status`/`result` tools that let an agent drive it. The capability is reachable; it is not built in. For a fuller side-by-side, see [FastMCP vs Promptise: The Production MCP Stack Compared](fastmcp-alternative-for-production.md).

**The agent frameworks** address async execution one layer up, at orchestration rather than at the MCP tool. LangGraph's platform/server, for instance, genuinely does offer background runs, a task queue, and cron scheduling — but those queue *graph invocations* on the deployment layer, not *MCP tool calls*. A third-party MCP client pointed at your server sees none of that machinery; it still just calls a tool and waits. LangChain, CrewAI, and AutoGen tools are in-process Python callables with no wire contract at all, so "durable job" is something you assemble yourself with an external queue and glue code.

None of that is carelessness — it is where each framework drew its boundary. The delta is one of *layer*: everyone can queue work *somewhere*, but the durability rarely lives *inside the MCP tool surface*, where a plain MCP client can reach it through the protocol. Promptise's contribution is to make it structural there: the queue auto-registers five real MCP tools, so any MCP client — a Promptise agent, Claude Desktop, your own — drives durable jobs with no bespoke plumbing.

## Turn any tool into a durable job with MCPQueue

`MCPQueue` attaches to an `MCPServer`, runs a pool of background workers, and registers five job-control tools on that server automatically. You define a job the same way you define a tool — a decorated async function — but instead of blocking the caller, the queue hands back a `job_id` immediately and runs the work on a worker task. The following example is fully self-contained and runs as-is against the public `promptise.mcp.server` API:

```python
import asyncio

from promptise.mcp.server import MCPServer, MCPQueue

server = MCPServer(name="analytics")
queue = MCPQueue(server, max_workers=4)


@queue.job(name="generate_report", timeout=600, max_retries=2, backoff_base=2.0)
async def generate_report(department: str) -> dict:
    """Generate a quarterly report. Stands in for a 10-minute pipeline."""
    await asyncio.sleep(1)  # real work: query warehouse, render PDF, upload...
    return {"department": department, "rows": 1250, "status": "ready"}


async def main() -> None:
    # server.run() starts these workers for you; the demo does it by hand.
    await queue.start()
    try:
        # queue_submit: hand back a job_id immediately, do not block.
        ticket = await queue.submit("generate_report", {"department": "Engineering"})
        job_id = ticket["job_id"]
        print("submitted :", ticket)

        # queue_status: poll from anywhere; the caller is free in between.
        terminal = {"completed", "failed", "cancelled", "timeout"}
        while True:
            state = await queue.status(job_id)
            print("status    :", state["status"], "progress", state["progress"])
            if state["status"] in terminal:
                break
            await asyncio.sleep(0.25)

        # queue_result: the durable payload, retrievable long after submit.
        done = await queue.get_result(job_id)
        print("result    :", done["result"])
    finally:
        await queue.stop()


asyncio.run(main())
```

Run it and you see the job move through its lifecycle while the caller stays free:

```text
submitted : {'job_id': '82064472c28dc184', 'status': 'pending', 'job_type': 'generate_report'}
status    : pending progress 0.0
status    : running progress 0.0
status    : running progress 0.0
status    : running progress 0.0
status    : completed progress 1.0
result    : {'department': 'Engineering', 'rows': 1250, 'status': 'ready'}
```

The key move is that `submit` returned in microseconds with a ticket, not in ten minutes with a payload. Nothing about the caller's connection is holding the job open, so nothing about the caller's timeout can lose it. In production you never call `queue.start()` yourself — `server.run(...)` starts and stops the workers through the server lifecycle. The example drives the methods directly so it runs end-to-end in a single file, but those methods are exactly what the five auto-registered MCP tools call under the hood.

## The five tools your agents actually call

`MCPQueue` registers these on the server with no extra wiring. An agent (or any MCP client) discovers them alongside your normal tools:

| Tool | Purpose |
|------|---------|
| `queue_submit` | Submit a job — returns a `job_id` immediately |
| `queue_status` | Check status and progress |
| `queue_result` | Retrieve a completed job's return value |
| `queue_cancel` | Cancel a pending or running job |
| `queue_list` | List jobs, filterable by status |

From the agent's side, the workflow reads exactly like the human-facing shape of async work — submit, poll, collect:

```text
queue_submit(job_type="generate_report", args={"department": "Engineering"}, priority="high")
  -> {"job_id": "82064472...", "status": "pending", "job_type": "generate_report"}

queue_status(job_id="82064472...")
  -> {"status": "running", "progress": 0.7, "progress_message": "Generating charts"}

queue_result(job_id="82064472...")
  -> {"status": "completed", "result": {"department": "Engineering", "rows": 1250}}
```

Four capabilities make this production-grade rather than a toy:

- **Priority scheduling.** Submissions carry one of four levels — `critical`, `high`, `normal`, `low` — and higher-priority jobs are dequeued first, so an urgent export jumps ahead of routine maintenance work already in the queue.
- **Exponential-backoff retry.** Set `max_retries` and `backoff_base` on the job. A failing handler is retried after `backoff_base * 2^(attempt - 1)` seconds — with `backoff_base=2.0` that is 2s, then 4s, then 8s — so a transient downstream blip does not lose the job.
- **Progress reporting.** Annotate a parameter with the job progress reporter and call `await progress.report(step, total=..., message=...)`; the fraction and message surface directly in `queue_status`, which is how the agent watches a long run in real time.
- **Cooperative cancellation.** Annotate a parameter with `CancellationToken` and call `cancel.check()` at safe points. When an agent calls `queue_cancel`, the token is signaled and the next check raises cleanly, so a job the user no longer wants stops instead of burning a worker.

Because these are real MCP tools, the durability is not a Promptise-agent-only feature — it is part of the tool contract your server advertises to every client. The full API, including progress and cancellation signatures, priority levels, and the job state machine, is documented in the [Queue & Background Jobs](../../mcp/server/queue.md) guide.

## Wiring it into a production server

The in-memory default backend uses an `asyncio.PriorityQueue` and is ideal for a single-process server or tests. When you need the queue to outlive a process or span replicas, implement the `QueueBackend` protocol against Redis or Postgres — the same submit/dequeue/get/update surface, backed by durable storage — and pass it as `backend=`. Everything above the backend stays identical.

A few knobs matter under load. `max_workers` caps concurrent jobs so a burst of submissions cannot exhaust the box. `default_timeout` and per-job `timeout` bound how long any single run may take before it is marked `timeout`. `result_ttl` controls how long a completed result stays fetchable, so `queue_result` has a defined window rather than growing unbounded. And you should register the queue with your health check — `queue.register_health(health)` adds a readiness signal that trips if the backlog grows past a threshold, which is exactly what you want a Kubernetes readiness probe to see.

The queue slots in next to the rest of the server hardening stack — auth, rate limiting, circuit breakers, caching, audit logging — laid out in the [Production Features](../../mcp/server/production-features.md) overview. It also pairs naturally with tool versioning: if a job's argument shape changes over time, the same discipline from [Why a Small MCP Tool Change Broke Every Connected Agent](version-mcp-tools-without-breaking-clients.md) applies to the job type, so agents that learned the old shape keep working while new ones opt in.

## Frequently asked questions

### Does the MCP protocol have a built-in async job or task primitive?

No. The base spec models a tool call as a single request/response exchange bound to the live connection. It does define progress notifications and request cancellation, but both ride on that same in-flight request — they make a long call observable, not durable. There is no native "submit now, fetch the result later by id" primitive. `MCPQueue` supplies that shape by auto-registering five job-control tools on top of the normal tool list.

### How is this different from just reporting progress from a slow FastMCP tool?

Progress reporting keeps the caller informed while a call runs, and FastMCP does support it — but the call is still one request tied to the connection, so a client timeout or dropped socket still loses the work and its result. A job queue decouples the job's lifetime from any request: `queue_submit` returns instantly with a ticket, the worker runs independently, and `queue_result` retrieves the answer whenever the client comes back. Progress and durability solve different halves of the problem; `MCPQueue` gives you both.

### Can a non-Promptise MCP client use the queue?

Yes. The five tools — `queue_submit`, `queue_status`, `queue_result`, `queue_cancel`, `queue_list` — are ordinary MCP tools advertised on the server. Any MCP client that can discover and call tools can drive durable jobs through them; there is no Promptise-specific client requirement. That is the point of making the capability structural at the tool layer rather than at an agent or orchestration layer.

### What happens to a job if it fails or times out?

If the handler raises and retries remain, the job is re-queued after an exponential-backoff delay (`backoff_base * 2^(attempt - 1)`). Once retries are exhausted it lands in `failed` with the error recorded; a run that exceeds its `timeout` lands in `timeout`. Both are terminal states you can observe via `queue_status` or `queue_result`, so the agent can decide whether to resubmit, escalate, or surface the failure to a human.

### Do I have to manage the workers myself?

No. `server.run(...)` starts the worker pool and the cleanup sweeper on startup and stops them on shutdown through the server lifecycle. The `queue.start()` / `queue.stop()` calls in the runnable example above are only there so the single-file demo can execute without a running transport.

## Next steps

If you have a tool that can take longer than a client is willing to wait — report generation, exports, pipelines, training — move it behind a job before the next timeout eats a result. Define it with `@queue.job(...)`, let `MCPQueue` register the five control tools, and point `server.run(...)` at your transport. Read the [Queue & Background Jobs](../../mcp/server/queue.md) guide for the full progress, cancellation, and backend API; fit the queue into the rest of your hardening work with the [Production Features](../../mcp/server/production-features.md) overview; and if you are still choosing a stack, [FastMCP vs Promptise: The Production MCP Stack Compared](fastmcp-alternative-for-production.md) shows where a first-class, MCP-native job queue changes the calculus.
