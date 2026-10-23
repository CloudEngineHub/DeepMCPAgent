"""Reviewer tools are scoped to the reviewer's tenant.

On a multi-tenant server one tenant's approver must not read another tenant's
pending call (its arguments may be private) or release it.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from promptise.mcp.server import (
    ApprovalGateMiddleware,
    AuthMiddleware,
    MCPServer,
    PendingApprover,
    TestClient,
)
from promptise.mcp.server._auth import APIKeyAuth

KEYS = {
    "sk-acme-agent": {"client_id": "acme-agent", "tenant_id": "acme"},
    "sk-acme-rev": {"client_id": "acme-reviewer", "roles": ["approver"], "tenant_id": "acme"},
    "sk-globex-rev": {"client_id": "globex-reviewer", "roles": ["approver"], "tenant_id": "globex"},
    "sk-platform-rev": {"client_id": "platform-reviewer", "roles": ["approver"]},
}


def _build(**approver_kwargs) -> tuple[MCPServer, PendingApprover]:
    server = MCPServer(name="tenants")
    server.add_middleware(AuthMiddleware(APIKeyAuth(keys=KEYS)))
    approver = PendingApprover(server, **approver_kwargs)
    server.add_middleware(ApprovalGateMiddleware(approver, timeout=5.0))

    @server.tool(auth=True, requires_approval=True)
    async def delete_workspace(secret: str) -> str:
        """Delete."""
        return f"deleted with {secret}"

    return server, approver


async def _pending_call(client: TestClient, approver: PendingApprover) -> tuple[asyncio.Task, str]:
    call = asyncio.create_task(
        client.call_tool(
            "delete_workspace", {"secret": "acme-only"}, headers={"x-api-key": "sk-acme-agent"}
        )
    )
    for _ in range(200):
        if approver.pending():
            break
        await asyncio.sleep(0.01)
    return call, approver.pending()[0]["request_id"]


def _json(result) -> object:
    return json.loads(result[0].text)


@pytest.mark.asyncio
async def test_other_tenants_reviewer_cannot_see_or_release_the_call():
    server, approver = _build()
    client = TestClient(server)
    call, rid = await _pending_call(client, approver)

    listed = _json(
        await client.call_tool("approvals_list", {}, headers={"x-api-key": "sk-globex-rev"})
    )
    assert listed == []

    decided = _json(
        await client.call_tool(
            "approvals_decide",
            {"request_id": rid, "approve": True},
            headers={"x-api-key": "sk-globex-rev"},
        )
    )
    assert decided["resolved"] is False
    assert "no pending approval" in decided["error"]
    assert approver.pending()  # still waiting

    # A tenant-less reviewer is not a wildcard either.
    assert (
        _json(
            await client.call_tool("approvals_list", {}, headers={"x-api-key": "sk-platform-rev"})
        )
        == []
    )

    # The tenant's own reviewer sees it and releases it.
    own = _json(await client.call_tool("approvals_list", {}, headers={"x-api-key": "sk-acme-rev"}))
    assert [p["request_id"] for p in own] == [rid]
    await client.call_tool(
        "approvals_decide",
        {"request_id": rid, "approve": True},
        headers={"x-api-key": "sk-acme-rev"},
    )
    result = await asyncio.wait_for(call, timeout=5)
    assert result[0].text == "deleted with acme-only"


@pytest.mark.asyncio
async def test_platform_wide_review_team_opt_in():
    server, approver = _build(same_tenant_reviewers=False)
    client = TestClient(server)
    call, rid = await _pending_call(client, approver)

    listed = _json(
        await client.call_tool("approvals_list", {}, headers={"x-api-key": "sk-platform-rev"})
    )
    assert [p["request_id"] for p in listed] == [rid]
    await client.call_tool(
        "approvals_decide",
        {"request_id": rid, "approve": False, "reason": "no"},
        headers={"x-api-key": "sk-platform-rev"},
    )
    result = await asyncio.wait_for(call, timeout=5)
    assert "APPROVAL_DENIED" in result[0].text


def test_pending_filters_by_tenant():
    approver = PendingApprover()
    assert approver.pending(tenant_id="acme") == []
    assert approver.pending() == []
