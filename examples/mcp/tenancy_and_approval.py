"""Server-side multi-tenancy + approval gates — self-contained, runnable demo.

Demonstrates two enterprise MCP-server features end to end, in-process via
``TestClient`` (no network, no API key needed):

  1. First-class multi-tenancy — ``ClientContext.tenant_id`` from the API-key
     config, tenant-qualified rate-limit buckets, and the ``require_tenant``
     server invariant. Two tenants with the SAME client id are isolated.
  2. Server-side approval gates — ``@server.tool(requires_approval=True)`` +
     ``ApprovalGateMiddleware``. A destructive tool blocks until a human
     approves, guards run before a reviewer is bothered, and a caller cannot
     approve their own request (four-eyes separation of duties).

Run:
    .venv/bin/python examples/mcp/tenancy_and_approval.py
"""

from __future__ import annotations

import asyncio

from promptise.mcp.server import (
    ApprovalGateMiddleware,
    AuthMiddleware,
    HasRole,
    MCPServer,
    PendingApprover,
    TestClient,
)
from promptise.mcp.server._auth import APIKeyAuth
from promptise.mcp.server._context import RequestContext


def build_server() -> tuple[MCPServer, PendingApprover]:
    # require_tenant=True → every tool authenticates AND must carry a tenant.
    server = MCPServer(name="billing", require_tenant=True)

    # API keys map to (client_id, roles, tenant_id). Two tenants deliberately
    # share the client id "svc-agent" to prove isolation is by tenant, not id.
    auth = APIKeyAuth(
        keys={
            "sk-acme": {"client_id": "svc-agent", "roles": ["billing"], "tenant_id": "acme"},
            "sk-globex": {"client_id": "svc-agent", "roles": ["billing"], "tenant_id": "globex"},
            "sk-approver": {"client_id": "dana", "roles": ["approver"], "tenant_id": "acme"},
        }
    )
    server.add_middleware(AuthMiddleware(auth))

    approver = PendingApprover(server, approver_role="approver")
    server.add_middleware(ApprovalGateMiddleware(approver, timeout=10.0))

    @server.tool(rate_limit="1/min", guards=[HasRole("billing")])
    async def get_invoice(invoice_id: str, ctx: RequestContext) -> dict:
        """Look up an invoice (rate-limited per tenant)."""
        return {"invoice": invoice_id, "tenant": ctx.client.tenant_id}

    @server.tool(guards=[HasRole("billing")], requires_approval=True)
    async def issue_refund(order_id: str, amount: float, ctx: RequestContext) -> dict:
        """Issue a refund — requires human approval before it executes."""
        return {"refunded": order_id, "amount": amount, "tenant": ctx.client.tenant_id}

    return server, approver


async def main() -> None:
    server, approver = build_server()
    client = TestClient(server)

    print("=== 1. Tenant isolation in rate limiting ===")
    print("Both tenants use client id 'svc-agent'; declared limit is 1/min.")
    r = await client.call_tool(
        "get_invoice", {"invoice_id": "INV-1"}, headers={"x-api-key": "sk-acme"}
    )
    print(f"  acme   call 1 -> {r[0].text}")
    r = await client.call_tool(
        "get_invoice", {"invoice_id": "INV-1"}, headers={"x-api-key": "sk-acme"}
    )
    print(f"  acme   call 2 -> {'RATE LIMITED' if 'RATE_LIMIT' in r[0].text else r[0].text}")
    r = await client.call_tool(
        "get_invoice", {"invoice_id": "INV-9"}, headers={"x-api-key": "sk-globex"}
    )
    print(f"  globex call 1 -> {r[0].text}  (own bucket — unaffected by acme)")

    print("\n=== 2. require_tenant invariant ===")
    # No x-api-key at all → unauthenticated → denied before the handler.
    r = await client.call_tool("get_invoice", {"invoice_id": "INV-1"})
    print(
        f"  unauthenticated -> {'DENIED' if 'error' in r[0].text.lower() or 'denied' in r[0].text.lower() else r[0].text}"
    )

    print("\n=== 3. Server-side approval gate (four-eyes) ===")
    call = asyncio.create_task(
        client.call_tool(
            "issue_refund",
            {"order_id": "ORD-42", "amount": 250.0},
            headers={"x-api-key": "sk-acme"},
        )
    )
    # Wait for the request to land in the pending store
    for _ in range(200):
        if approver.pending():
            break
        await asyncio.sleep(0.01)
    pending = approver.pending()
    print(
        f"  refund is blocked, awaiting approval: {pending[0]['tool']} "
        f"{pending[0]['arguments']} from tenant={pending[0]['tenant_id']}"
    )

    # dana (a different human with the approver role) releases it
    decide = await client.call_tool(
        "approvals_decide",
        {"request_id": pending[0]["request_id"], "approve": True},
        headers={"x-api-key": "sk-approver"},
    )
    print(f"  approver 'dana' decides -> {decide[0].text}")

    result = await asyncio.wait_for(call, timeout=10)
    print(f"  refund now executes -> {result[0].text}")

    print("\n=== 4. Guards run before approval (no reviewer spam) ===")
    # An 'approver'-only caller lacks the 'billing' role the tool guards on,
    # so the gate rejects it WITHOUT ever creating a pending approval.
    r = await client.call_tool(
        "issue_refund",
        {"order_id": "ORD-99", "amount": 1.0},
        headers={"x-api-key": "sk-approver"},
    )
    print(
        f"  wrong-role caller -> {'DENIED before approval' if 'ACCESS_DENIED' in r[0].text else r[0].text}"
    )
    print(f"  pending queue still empty: {approver.pending() == []}")


if __name__ == "__main__":
    asyncio.run(main())
