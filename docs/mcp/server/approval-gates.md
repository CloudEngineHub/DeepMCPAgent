---
seo_title: "Human Approval Gates for MCP Server Tools"
description: "Enforce human approval on the MCP server: tools with requires_approval=True wait for a reviewer before they run, for any MCP client, and deny on timeout."
---

# Approval Gates (Server-Side HITL)

Human-in-the-loop enforced **where the tool lives**. A tool declared with
`requires_approval=True` cannot execute (for *any* MCP client) until a
human (or a policy acting for one) approves the call. Denied by default on
timeout. Every outcome is visible to the audit chain.

!!! tip "Why server-side"
    Agent-side approval ([`ApprovalPolicy`](../../core/approval.md)) governs
    Promptise agents, but an enterprise MCP server is called by clients the
    server team doesn't control. A gate in the server's middleware chain
    makes approval a property of the *tool*, not a courtesy of the caller.

## Declare, gate, done

```python
from promptise.mcp.server import (
    ApprovalGateMiddleware, AuthMiddleware, MCPServer, PendingApprover,
)

server = MCPServer(name="billing")
server.add_middleware(AuthMiddleware(auth))          # identity first
approver = PendingApprover(server, approver_role="approver")
server.add_middleware(ApprovalGateMiddleware(approver, timeout=300))

@server.tool(auth=True, requires_approval=True)
async def refund(order_id: str, amount: float) -> dict:
    """Refund an order — requires human sign-off."""
    ...
```

!!! warning "An ungated declaration refuses to build"
    If any tool declares `requires_approval=True` and no
    `ApprovalGateMiddleware` is installed, the server **raises at build
    time** (and `TestClient` raises on call). A declared approval that
    silently doesn't enforce would be worse than none.

Install the gate **after** `AuthMiddleware` so approval requests carry the
verified caller identity (client id, tenant, JWT subject).

!!! note "The gate checks the tool's guards first"
    Before requesting a human decision, the gate evaluates the tool's own
    guards (`RequireAuth`, `HasRole`, `HasTenant`, …). A caller the guards
    would reject is denied immediately and **never** reaches a reviewer, so
    unauthorized callers can't spam approvers or fill the pending queue. Pair
    `requires_approval=True` with `auth=True`/guards so there is an identity
    to check; a gated tool left fully unauthenticated can be triggered by any
    client (the server owner's explicit choice).

## Gate semantics: fail closed everywhere

| Event | Outcome |
|-------|---------|
| Decision `approved=True` | Call proceeds; approval logged |
| Decision `approved=False` | `ApprovalDeniedError` (`APPROVAL_DENIED`, not retryable); `reviewer_id` + `approval_request_id` in `details`, the reviewer's reason in the message |
| No decision within `timeout` | **Denied by default** (`on_timeout="allow"` opts out, explicitly) |
| Decision carries `modified_arguments` | **Denied**: the server-side gate cannot rewrite bound arguments, and executing the original args after a reviewer changed them would run something they didn't approve |
| Handler crash | Denied via the error pipeline |
| A second decision for the same request id | **Denied** (`APPROVAL_DENIED`) and logged; each request id takes one decision |

`ApprovalGateMiddleware(handler, *, timeout=300.0, on_timeout="deny", include_arguments=True)`:

| Parameter | Default | Description |
|---|---|---|
| `handler` | required | A `PendingApprover`, an `ElicitationApprover`, any [`ApprovalHandler`](../../core/approval.md), or a bare callable returning `bool` / `ApprovalDecision` |
| `timeout` | `300.0` | Seconds to wait for a decision |
| `on_timeout` | `"deny"` | `"allow"` lets the call run when no decision arrives in time |
| `include_arguments` | `True` | Show the validated arguments to reviewers; `False` for tools whose arguments are too sensitive |

The gate only acts on tools declared with `requires_approval=True`; every other call passes through.

## The three approvers

### `PendingApprover`: independent four-eyes review

`PendingApprover(server=None, *, approver_role="approver", max_pending=100, max_pending_per_client=None, max_pending_per_tenant=None, same_tenant_reviewers=True)`. Gated calls block in a pending store. A human holding `approver_role`
reviews them through two auto-registered, role-guarded admin tools:

```text
approvals_list()                          → pending requests: tool, args,
                                            caller, tenant, age
approvals_decide(request_id, approve,     → release or deny; the reviewer's
                 reason)                     client_id is recorded
```

**Separation of duties is enforced:** `approvals_decide` rejects an
*approval* whose reviewer `client_id` equals the request's original caller:
you cannot approve your own call, even if you also hold `approver_role`
(denying your own is always allowed).

**Reviewers see only their own tenant:** `approvals_list` returns only the
requests whose `tenant_id` matches the reviewer's, and `approvals_decide`
answers another tenant's request id exactly like an unknown one. A reviewer
can neither read another tenant's arguments nor release its call. A reviewer
without a tenant sees only requests without one. For one platform-wide review
team that decides for every tenant, pass `same_tenant_reviewers=False`.

The store is process-local (like the
in-memory job queue); calls that outlive the gate timeout are denied by
default. Three caps bound the store, and beyond any of them a call is denied
immediately rather than parked:

| Cap | Counts | Default |
|---|---|---|
| `max_pending` | every waiting call | `100` |
| `max_pending_per_tenant` | calls whose `tenant_id` metadata (stamped by the gate from the authenticated client) is the same; requests without a tenant share one bucket | none |
| `max_pending_per_client` | calls from one caller: `caller_user_id`, else the request's `client_id` / `tenant_id` metadata, else `agent_id`; anonymous callers share one bucket | none |

`max_pending_per_client` stops a single client from filling the queue for
everyone else; `max_pending_per_tenant` is the cap that matters on a
multi-tenant server, because the per-client cap counts client ids and a
tenant holding several API keys could otherwise still take the whole store.
The caps nest (`max_pending_per_client` ≤ `max_pending_per_tenant` ≤
`max_pending`, else the constructor raises `ValueError`) and the denial
reason names the cap that was hit (`per-tenant pending approval cap reached
for tenant 'acme' (40 per tenant)`, `per-client pending approval cap reached
for 'alice' (20 per client)`).

**Each request id takes one decision.** A second `decide()` for an id that
was already decided returns `False` and logs a warning on the
`promptise.server.approval` logger; `approvals_decide` answers
`{"resolved": false, "error": "approval request '...' was already decided; ..."}`,
and the first decision stands. `approver.was_decided(request_id)` tells you
whether an id already had its decision. Decided ids are remembered in memory
for the request's `timeout` (at most 10,000 of them).

### `ElicitationApprover`: confirm with the human behind the client

`ElicitationApprover(*, message_template=None)`. The message shown to the user defaults to `"Approval required: call tool '{tool_name}' with arguments {arguments}. Approve?"`; pass `message_template` with the same `{tool_name}` and `{arguments}` placeholders to change it. Uses MCP elicitation (`ServerSession.elicit`) to ask the *calling* client's
user to confirm (`{"approve": bool, "reason": str}`). Right for
destructive-but-personal operations ("really delete this?"). **Fail-closed:**
if the transport has no live MCP session (e.g. `TestClient`), the client does
not support elicitation, the user declines or cancels, or the request times
out, the call is denied with a clear reason, never silently allowed. The
denial is logged at `WARNING` with the cause, so an unexpected "client
declined" is diagnosable.

Promptise's own clients answer through an approval handler.
`build_agent(approval=...)` routes the request to the agent's handler, and
`MCPClient(elicitation_callback=approval_elicitation_callback(handler))` does
the same without an agent. Without one they declare no elicitation support, and
the call is denied. See
[Server-side approval gates](../../core/approval.md#server-side-approval-gates).

### Callbacks and existing handlers: bring your own channel

The gate accepts any [`ApprovalHandler`](../../core/approval.md): the same
protocol as agent-side approval, so the existing handlers plug in directly:

```python
# Bare callable (bool or ApprovalDecision, sync or async)
ApprovalGateMiddleware(lambda request: request.arguments.get("amount", 0) < 100)

# The agent-side handlers work as-is:
from promptise.approval import WebhookApprovalHandler   # HMAC-signed + polling
ApprovalGateMiddleware(WebhookApprovalHandler(url="https://approvals.internal/hook",
                                              secret=os.environ["APPROVAL_SECRET"]))
```

## What reviewers see

Each request is a `promptise.approval.ApprovalRequest`: tool name, the
validated arguments (unless `include_arguments=False`), and metadata with
`client_id`, `tenant_id`, JWT `subject`/`issuer`, and the MCP request id:
enough to decide without grepping logs.

## Audit visibility

Approval outcomes flow through the standard pipeline: a denial surfaces as
a structured `APPROVAL_DENIED` error, and grants proceed to the normal
audited tool call. With `AuditMiddleware` installed before the gate, a
denial is recorded like any error outcome: `"status": "error"` and
`"error": "ApprovalDeniedError: Approval denied for tool 'issue_refund'"`
(plus the reviewer's reason, when one was given). The approval request id
and reviewer id are in the error's `details`, which the **client** receives;
they are not written to the audit entry. To correlate the two, use the
gate's own log lines (logger `promptise.server`), which record every
requested/granted/denied decision with its `request_id`, tool, client and
reviewer.

## Combining with tenancy

Approval requests carry `tenant_id`, and the admin tools are ordinary
guarded tools, so on a `require_tenant=True` server, reviewers themselves
must present tenant identity. Each reviewer then lists and decides only their
own tenant's calls (see `same_tenant_reviewers` above), so give every tenant
its own approver keys.

## Limitations

- **Argument modification is not supported** server-side: approve or deny
  only. (Agent-side `ApprovalPolicy` supports `modified_arguments`.)
- The `PendingApprover` store is **process-local**: pending calls don't
  survive a restart (they are denied by the gate timeout), and replicas
  don't share a queue. Run one replica for gated tools, or use an
  `ApprovalHandler` backed by your own shared store (for example a
  `WebhookApprovalHandler` to an approval service).
- Elicitation requires a client + transport that support it; everything
  else fails closed.

## See Also

- [Agent-side Approval (HITL)](../../core/approval.md): `ApprovalPolicy`, webhook/queue handlers, auto-classification
- [Multi-Tenancy](multi-tenancy.md): tenant identity the gate records
- [Authentication & Security](auth-security.md): the identity the gate relies on

!!! tip "Learn the concept"

    - [AI Agent Human in the Loop: Approve Tool Calls in Python](https://www.promptise.com/guides/view/ai-agent-human-in-the-loop-approval): Why and when a human should approve a tool call.
