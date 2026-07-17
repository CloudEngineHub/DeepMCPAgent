---
title: "Route AI Agent Approvals to Slack with Signed Webhooks"
description: "A hands-on integration guide no existing post covers: wire the same server-side gate to an external approvals channel with WebhookApprovalHandler. It POSTs…"
keywords: "route ai agent approvals to slack, webhook approval handler mcp, hmac-signed approval request, pagerduty approval ai agent, external approval service integration"
date: 2026-07-16
slug: route-ai-agent-approvals-to-slack
categories:
  - Approvals & HITL
---

# Route AI Agent Approvals to Slack with Signed Webhooks

To **route AI agent approvals to Slack** — or PagerDuty, or an internal review app — the decision has to leave your agent's process entirely and land in front of a human who lives in that channel, not at the terminal that happens to be running the agent. That is a different problem from the usual "pop a confirm dialog in the calling app." The reviewer for a $5,000 refund is on call in an ops channel; they are never going to be watching a Python REPL. This post wires the same server-side approval gate that enforces on every MCP client to an external channel using `WebhookApprovalHandler`: it POSTs an HMAC-signed request to your own service, polls for the verdict, and denies by default if the clock runs out — a one-line swap for the in-process reviewers, with no change to the tool.

## The approval that has to leave your process

Most human-in-the-loop wiring surfaces the prompt wherever the agent is running. That is exactly right when the reviewer is the person driving the app — but a real approval channel is somewhere else. Refund sign-off happens in a `#finance-approvals` Slack channel with a Block Kit message and two buttons. A production deletion pages the on-call engineer in PagerDuty. A contract action opens a ticket in your internal review tool. None of those reviewers can see, or should see, the process that hosts the agent.

Promptise Foundry already makes approval a property of the *tool* rather than the caller: a tool declared `@server.tool(requires_approval=True)` cannot run for any MCP client until an approver decides, and the server refuses to build if you declare the requirement without installing a gate. That build-time invariant is covered in [An MCP Approval Gate That Refuses to Ship Ungated](build-time-enforced-approval-gate-mcp.md), and where each framework physically runs its check is mapped in [LangGraph vs CrewAI vs AutoGen: Where HITL Runs](langgraph-vs-crewai-vs-autogen-human-in-the-loop.md). What's left is the piece those posts point at but don't build: pushing that gate's decision to an independent service and getting a trustworthy answer back.

## One handler, one line: point the gate at your service

The gate accepts any [`ApprovalHandler`](../../core/approval.md) — the same protocol behind the in-process reviewers. `PendingApprover` blocks the call for a four-eyes review inside the server; `ElicitationApprover` asks the calling client's own user; `WebhookApprovalHandler` sends the request to a URL you own. All three are interchangeable, so routing to Slack is a one-line change to which handler you construct — the gated tool underneath does not move:

```python
import os
from promptise.mcp.server import (
    ApprovalGateMiddleware, AuthMiddleware, JWTAuth, MCPServer,
    PendingApprover, ElicitationApprover,   # the in-process reviewers
)
from promptise.approval import WebhookApprovalHandler

auth = JWTAuth(secret=os.environ["JWT_SECRET"])
server = MCPServer(name="billing")
server.add_middleware(AuthMiddleware(auth))            # identity first

# Pick ONE approver — all three satisfy the same ApprovalHandler protocol,
# so this is a one-line swap and the gated tool below never changes:
#   PendingApprover(server, approver_role="approver")  # four-eyes, in-process
#   ElicitationApprover()                              # ask the calling client's user
approver = WebhookApprovalHandler(                      # route to your own service
    url="https://approvals.acme.com/mcp/refund",       # public URL (private IPs blocked)
    secret=os.environ["APPROVAL_SECRET"],              # HMAC secret, shared with the relay
    headers={"Authorization": f"Bearer {os.environ['RELAY_TOKEN']}"},
    poll_interval=2.0,
)
server.add_middleware(ApprovalGateMiddleware(approver, timeout=600))  # deny on timeout

@server.tool(auth=True, roles=["clerk"], requires_approval=True)
async def refund(order_id: str, amount: float) -> dict:
    """Refund an order — a human in Slack must approve before this runs."""
    return {"order_id": order_id, "amount": amount, "status": "refunded"}
```

The handler does two things per gated call. It POSTs the approval request as JSON to `url`, and it then polls `poll_url` (defaulting to `{url}/{request_id}`) every `poll_interval` seconds until your service returns a decision or `timeout` expires. Your endpoint answers the POST with `202 Accepted`, kicks off the Slack message, and returns `202` on each poll while the request is still pending; the moment a human clicks a button it returns `200` with `{"approved": true, "reviewer_id": "...", "reason": "..."}`. The URL is validated against a private-IP and loopback blocklist at construction time (SSRF protection), so the handler is pointed at your relay's real public hostname, not `localhost`; `headers` carry whatever auth your relay expects, and `http_client` lets you hand in a pre-configured `httpx.AsyncClient` for a corporate proxy or mTLS.

## Verify the HMAC before you trust the request

An endpoint that fires a Slack approval must first prove the request actually came from your gate — otherwise anyone who learns the URL can page your reviewers with attacker-chosen arguments. Every POST carries an `X-Promptise-Signature` header: an HMAC-SHA256 over the signed fields of the request, computed with the shared `secret`. Your service recomputes it over the JSON body and compares in constant time. The round trip is fully deterministic, so you can exercise it with no network and no API key:

```python
import hashlib
import hmac
import json

from promptise.approval import ApprovalRequest

SECRET = "shared-approval-secret"  # the same string on the agent and your relay

# Exactly what WebhookApprovalHandler builds and sends: the JSON body
# (request.to_dict()) plus an X-Promptise-Signature header (request.compute_hmac).
request = ApprovalRequest(
    request_id="6b1e4c0f9a2d4e7b8c1f0a3d5e6f7a8b",
    tool_name="refund",
    arguments={"order_id": "A-1", "amount": 5000.0},
    caller_user_id="user-42",
    timestamp=1_752_600_000.0,
)
body = request.to_dict()                     # the POST body your endpoint receives
signature = request.compute_hmac(SECRET)     # the X-Promptise-Signature header


def verify(body: dict, received_signature: str, secret: str) -> bool:
    """Recompute the HMAC over the signed fields; compare in constant time."""
    signed = json.dumps(
        {
            "request_id": body["request_id"],
            "tool_name": body["tool_name"],
            "arguments": body["arguments"],
            "agent_id": body["agent_id"],
            "caller_user_id": body["caller_user_id"],
            "timestamp": body["timestamp"],
        },
        sort_keys=True,
        default=str,
    )
    expected = hmac.new(secret.encode(), signed.encode(), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, received_signature)


assert verify(body, signature, SECRET) is True             # genuine request accepted
assert verify(body, signature, "attacker-guess") is False  # wrong secret rejected
print("signature ok:", verify(body, signature, SECRET))
```

The signature covers `request_id`, `tool_name`, `arguments`, `agent_id`, `caller_user_id`, and `timestamp` — so it binds the *identity of the caller and the exact arguments* a reviewer is about to see, not just the tool name. Reject anything that fails `hmac.compare_digest` with a `401` before it ever reaches Slack. Because the `request_id` is 128 bits of `secrets.token_hex(16)`, you can also dedupe on it to shrug off replays.

## Relay to Slack and fail closed on timeout

With the signature verified, your relay is a small stateful service: store the pending request keyed by `request_id`, post the Block Kit message to your approvals channel with Approve/Deny buttons, and answer polls. Return `202` while no human has clicked; when Slack's interaction webhook fires, record `{"approved": bool, "reviewer_id": <slack_user>, "reason": <text>}` against that `request_id` and start returning `200` with that body. The handler's poll picks it up on its next tick and the gate releases or denies the tool call accordingly — the full POST/poll contract is documented on the [agent-side Approval page](../../core/approval.md), which the server-side gate reuses verbatim.

The part competitors leave to you is the failure path, and it is where a home-grown poll loop usually leaks. If nobody clicks within `timeout`, `WebhookApprovalHandler` stops polling and raises `asyncio.TimeoutError`; the gate treats that as **deny by default** (you opt out only with an explicit `on_timeout="allow"`). A network blip during a single poll is logged and retried on the next interval rather than crashing the wait. A malformed or missing decision body is simply "still pending" — never an accidental approve. Every one of those outcomes flows to the audit chain with the approval `request_id` attached, exactly as `PendingApprover` denials do. The gate semantics — deny on timeout, deny on handler crash, deny when a reviewer tries to rewrite arguments — are identical no matter which handler you plug in, and are laid out in full in the [Approval Gates guide](../../mcp/server/approval-gates.md). Swapping to a webhook changes *where the human is*, not *how safe the default is*.

## What other frameworks do today

Human-in-the-loop is not a Promptise invention, and it would be wrong to imply the alternatives can't do this. The precise delta is whether routing an approval to an *independent external service* — signed, polled, fail-closed, and enforced for every client — is a pluggable primitive or something you assemble yourself.

- **LangGraph** has a genuinely strong in-process pause: `interrupt()` with a checkpointer saves durable graph state and resumes on `Command(resume=...)`. It is not, however, a signed-webhook approval channel; to page an external Slack/PagerDuty reviewer you write the HTTP round trip, the HMAC, and the poll loop around the interrupt yourself.
- **CrewAI** offers `human_input=True` on a `Task`, which collects feedback on the console inside the crew process. Real and simple, but local to that run — there is no built-in handler that ships the request to an outside service and waits on a signed reply.
- **AutoGen** routes approval through a `UserProxyAgent` with `human_input_mode`, and separately ships a distributed agent runtime for multi-process agent messaging. That runtime moves *agent-to-agent* traffic; it is not an HMAC-signed HITL approval handler, so an external approvals endpoint is still yours to build and secure.
- **Pydantic AI** models approval as a deferred tool call resolved app-side. The decision happens in your own control flow — again, the external channel and its signing are on you.

None of these frameworks is missing HITL, and with enough glue any of them can call a webhook. The structural difference is that Promptise ships the external-service path as a first-class `ApprovalHandler`: the signing, the poll loop, and the deny-by-default timeout are the framework's, the handler drops into the *same* server-side gate that enforces for every MCP client, and it is interchangeable with `PendingApprover` and `ElicitationApprover` by construction. The question competitors can't cleanly answer — "how do I route this exact approval to my own Slack relay, verify it, and have it fail closed, without hand-rolling the transport and hoping I got the timeout right?" — is a constructor argument here.

## Frequently asked questions

### What exactly does my endpoint have to send back?

Answer the initial POST with `202 Accepted`. On each poll (`GET {url}/{request_id}` by default), return `202` while the request is pending. When a reviewer decides, return `200` with a JSON body containing at least `{"approved": true}` or `{"approved": false}`; `reviewer_id`, `reason`, and `modified_arguments` are optional. The handler reads the first response that includes an `approved` key and stops polling.

### How do I verify the HMAC signature server-side?

Read the `X-Promptise-Signature` header, then recompute HMAC-SHA256 with your shared `secret` over a `json.dumps(..., sort_keys=True, default=str)` of the request's `request_id`, `tool_name`, `arguments`, `agent_id`, `caller_user_id`, and `timestamp` — all present in the POST body — and compare with `hmac.compare_digest`. The runnable block above is that verification end to end. Reject mismatches with `401` before touching Slack.

### What happens if no one approves in time?

`WebhookApprovalHandler` polls until `timeout`, then raises `asyncio.TimeoutError`, and the gate denies the call by default. The tool body never runs and the denial is audited with the request id. You can set `on_timeout="allow"` on the gate for genuinely low-risk tools, but that is an explicit opt-out of the safe default, not something you can fall into.

### Can I point it at localhost or an internal IP for testing?

No. `WebhookApprovalHandler` validates the URL against private IP ranges, loopback, and link-local addresses at construction time (SSRF protection), so `localhost` and `10.x`/`192.168.x` hosts are rejected. Test against a real external hostname — a tunneling service or a staging relay — or exercise the signing logic offline with the deterministic block above, which needs no server at all.

## Next steps

Swap `PendingApprover` or `ElicitationApprover` for `WebhookApprovalHandler` on your gate, drop the `verify()` function into your relay so every request is checked before it reaches your approvals channel, and route your riskiest tool's sign-off to Slack or PagerDuty. Start from the [Approval Gates guide](../../mcp/server/approval-gates.md) for the full gate option surface, read the [agent-side Approval page](../../core/approval.md) for the complete POST/poll contract and handler reference, and see [An MCP Approval Gate That Refuses to Ship Ungated](build-time-enforced-approval-gate-mcp.md) for why the gate that carries your webhook can't be silently dropped in the first place.
