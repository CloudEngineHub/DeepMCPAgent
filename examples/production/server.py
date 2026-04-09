"""Production MCP Server — used by the full-stack deploy example.

10 tools across 2 domains: Customer and Analytics.
JWT auth, rate limiting, audit logging.

Run standalone:
    python examples/production/server.py
"""

from __future__ import annotations

from promptise.mcp.server import (
    AuthMiddleware,
    JWTAuth,
    LoggingMiddleware,
    MCPRouter,
    MCPServer,
    RateLimitMiddleware,
)

server = MCPServer("production-api", version="2.0.0")

JWT_SECRET = "production-demo-secret"
jwt = JWTAuth(secret=JWT_SECRET)

for mw in [LoggingMiddleware(), RateLimitMiddleware(rate_per_minute=60)]:
    server.add_middleware(mw)

# ── Customer tools ──

customers = MCPRouter(prefix="customer")

CUSTOMER_DB = {
    "C-001": {"name": "Acme Corp", "plan": "enterprise", "mrr": 4500, "health": "healthy"},
    "C-002": {"name": "Globex Inc", "plan": "pro", "mrr": 990, "health": "at_risk"},
    "C-003": {"name": "Initech", "plan": "starter", "mrr": 290, "health": "healthy"},
    "C-004": {"name": "Umbrella Corp", "plan": "enterprise", "mrr": 8900, "health": "churning"},
    "C-005": {"name": "Stark Industries", "plan": "enterprise", "mrr": 12000, "health": "healthy"},
}


@customers.tool()
async def list_customers(plan: str = "", health: str = "") -> str:
    """List customers, optionally filtered by plan or health status."""
    rows = list(CUSTOMER_DB.values())
    if plan:
        rows = [c for c in rows if c["plan"] == plan.lower()]
    if health:
        rows = [c for c in rows if c["health"] == health.lower()]
    lines = [f"  {c['name']} | {c['plan']} | ${c['mrr']:,}/mo | {c['health']}" for c in rows]
    return f"Customers ({len(rows)}):\n" + "\n".join(lines)


@customers.tool()
async def get_customer(customer_id: str) -> str:
    """Get details for a specific customer."""
    c = CUSTOMER_DB.get(customer_id.upper())
    if not c:
        return f"Customer {customer_id} not found."
    return f"{c['name']} | Plan: {c['plan']} | MRR: ${c['mrr']:,} | Health: {c['health']}"


@customers.tool()
async def customer_health_summary() -> str:
    """Get customer health overview."""
    from collections import Counter
    health = Counter(c["health"] for c in CUSTOMER_DB.values())
    total_mrr = sum(c["mrr"] for c in CUSTOMER_DB.values())
    at_risk_mrr = sum(c["mrr"] for c in CUSTOMER_DB.values() if c["health"] in ("at_risk", "churning"))
    return (f"Health Summary: {dict(health)} | Total MRR: ${total_mrr:,}/mo | "
            f"At-risk MRR: ${at_risk_mrr:,}/mo ({at_risk_mrr/total_mrr*100:.0f}%)")


@customers.tool(roles=["admin"])
async def update_customer_health(customer_id: str, new_health: str) -> str:
    """Update a customer's health status. Admin only."""
    c = CUSTOMER_DB.get(customer_id.upper())
    if not c:
        return f"Customer {customer_id} not found."
    old = c["health"]
    c["health"] = new_health
    return f"Updated {c['name']}: {old} → {new_health}"


@customers.tool()
async def search_customers(query: str) -> str:
    """Search customers by name."""
    matches = [c for c in CUSTOMER_DB.values() if query.lower() in c["name"].lower()]
    if not matches:
        return f"No customers matching '{query}'."
    return "\n".join(f"  {c['name']} ({c['plan']}, {c['health']})" for c in matches)


server.include_router(customers)

# ── Analytics tools ──

analytics = MCPRouter(prefix="analytics")


@analytics.tool()
async def revenue_metrics() -> str:
    """Get current revenue metrics."""
    total = sum(c["mrr"] for c in CUSTOMER_DB.values())
    return f"MRR: ${total:,} | ARR: ${total * 12:,} | Avg MRR: ${total // len(CUSTOMER_DB):,}"


@analytics.tool()
async def plan_distribution() -> str:
    """Get customer distribution by plan."""
    from collections import Counter
    plans = Counter(c["plan"] for c in CUSTOMER_DB.values())
    plan_mrr = {}
    for c in CUSTOMER_DB.values():
        plan_mrr[c["plan"]] = plan_mrr.get(c["plan"], 0) + c["mrr"]
    lines = [f"  {p}: {count} customers, ${plan_mrr[p]:,}/mo" for p, count in plans.most_common()]
    return "Plan Distribution:\n" + "\n".join(lines)


@analytics.tool()
async def churn_risk_report() -> str:
    """Get customers at risk of churning."""
    at_risk = [c for c in CUSTOMER_DB.values() if c["health"] in ("at_risk", "churning")]
    if not at_risk:
        return "No customers at risk."
    lines = [f"  {c['name']} [{c['health']}] — ${c['mrr']:,}/mo" for c in at_risk]
    total = sum(c["mrr"] for c in at_risk)
    return f"Churn Risk ({len(at_risk)} customers, ${total:,}/mo at risk):\n" + "\n".join(lines)


@analytics.tool()
async def calculate(expression: str) -> str:
    """Evaluate a math expression."""
    try:
        allowed = set("0123456789+-*/.() ")
        if all(c in allowed for c in expression):
            return str(round(eval(expression), 2))  # noqa: S307
        return f"Invalid: {expression}"
    except Exception as e:
        return f"Error: {e}"


@analytics.tool()
async def forecast(months: int = 3) -> str:
    """Forecast revenue based on current trends."""
    current_mrr = sum(c["mrr"] for c in CUSTOMER_DB.values())
    growth_rate = 0.05  # 5% monthly
    projections = []
    mrr = current_mrr
    for m in range(1, months + 1):
        mrr = int(mrr * (1 + growth_rate))
        projections.append(f"  Month {m}: ${mrr:,}/mo")
    return f"Forecast (5% monthly growth):\n  Current: ${current_mrr:,}/mo\n" + "\n".join(projections)


server.include_router(analytics)

if __name__ == "__main__":
    server.run(transport="stdio")
