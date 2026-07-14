# Approval Gates (Server-Side HITL)

Human-in-the-loop enforced **where the tool lives**. A tool declared with
`requires_approval=True` cannot execute — for *any* MCP client — until a
human (or a policy acting for one) approves the call. Denied by default on
timeout. Every outcome is visible to the audit chain.

!!! tip "Why server-side"
    Agent-side approval ([`ApprovalPolicy`](../../core/approval.md)) governs
    Promptise agents — but an enterprise MCP server is called by clients the
    server team doesn't control. A gate in the server's middleware chain
    makes approval a property of the *tool*, not a courtesy of the caller.
    This is the dual-control security reviews ask for first.

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
    would reject is denied immediately and **never** reaches a reviewer — so
    unauthorized callers can't spam approvers or fill the pending queue. Pair
    `requires_approval=True` with `auth=True`/guards so there is an identity
    to check; a gated tool left fully unauthenticated can be triggered by any
    client (the server owner's explicit choice).

## Gate semantics — fail closed everywhere

| Event | Outcome |
|-------|---------|
| Decision `approved=True` | Call proceeds; approval logged |
| Decision `approved=False` | `ApprovalDeniedError` (`APPROVAL_DENIED`, not retryable); `reviewer_id` + `approval_request_id` in `details`, the reviewer's reason in the message |
| No decision within `timeout` | **Denied by default** (`on_timeout="allow"` opts out, explicitly) |
| Decision carries `modified_arguments` | **Denied** — the server-side gate cannot rewrite bound arguments, and executing the original args after a reviewer changed them would run something they didn't approve |
| Handler crash | Denied via the error pipeline |

`ApprovalGateMiddleware` options: `timeout` (default 300s),
`on_timeout` (`"deny"`/`"allow"`), `include_arguments` (default `True`;
disable for tools whose arguments are too sensitive to show reviewers).

## The three approvers

### `PendingApprover` — independent four-eyes review

Gated calls block in a pending store. A human holding `approver_role`
reviews them through two auto-registered, role-guarded admin tools:

```text
approvals_list()                          → pending requests: tool, args,
                                            caller, tenant, age
approvals_decide(request_id, approve,     → release or deny; the reviewer's
                 reason)                     client_id is recorded
```

**Separation of duties is enforced:** `approvals_decide` rejects an
*approval* whose reviewer `client_id` equals the request's original caller —
you cannot approve your own call, even if you also hold `approver_role`
(denying your own is always allowed). The store is process-local (like the
in-memory job queue); calls that outlive the gate timeout are denied by
default. `max_pending` (default 100) denies immediately beyond that many
waiting calls.

### `ElicitationApprover` — confirm with the human behind the client

Uses MCP elicitation to ask the *calling* client's user to confirm
(`{"approve": bool, "reason": str}`). Right for destructive-but-personal
operations ("really delete this?"). **Fail-closed:** if the transport has
no live MCP session (e.g. `TestClient`, clients without elicitation
support), the call is denied with a clear reason — never silently allowed.

### Callbacks and existing handlers — bring your own channel

The gate accepts any [`ApprovalHandler`](../../core/approval.md) — the same
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
`client_id`, `tenant_id`, JWT `subject`/`issuer`, and the MCP request id —
enough to decide without grepping logs.

## Audit visibility

Approval outcomes flow through the standard pipeline: a denial surfaces as
a structured `APPROVAL_DENIED` error (recorded by `AuditMiddleware` like
any error outcome, with the approval request id in details), and grants
proceed to the normal audited tool call. The gate also logs every
requested/granted/denied decision with request ids.

## Combining with tenancy

Approval requests carry `tenant_id`, and the admin tools are ordinary
guarded tools — so on a `require_tenant=True` server, reviewers themselves
must present tenant identity, and `approvals_list` shows which tenant each
pending call belongs to.

## Limitations (honest edges)

- **Argument modification is not supported** server-side — approve or deny
  only. (Agent-side `ApprovalPolicy` supports `modified_arguments`.)
- The `PendingApprover` store is **process-local**: pending calls don't
  survive a restart (they are denied by the gate timeout), and replicas
  don't share a queue. A distributed backend is on the roadmap alongside
  the durable job queue.
- Elicitation requires a client + transport that support it; everything
  else fails closed.

## See Also

- [Agent-side Approval (HITL)](../../core/approval.md) — `ApprovalPolicy`, webhook/queue handlers, auto-classification
- [Multi-Tenancy](multi-tenancy.md) — tenant identity the gate records
- [Authentication & Security](auth-security.md) — the identity the gate relies on
