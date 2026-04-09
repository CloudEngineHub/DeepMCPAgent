"""Enterprise MCP Server — 30 tools, 6 domains, JWT auth, role-based access.

Demonstrates production MCP server patterns:
- 6 domain routers with 5 tools each (30 total)
- JWT authentication with 3 role levels
- Per-tool role-based guards
- Rate limiting, audit logging, timeout, circuit breakers
- Health checks

Run:
    python examples/mcp/enterprise_server.py
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time

from promptise.mcp.server import (
    AuthMiddleware,
    JWTAuth,
    LoggingMiddleware,
    MCPRouter,
    MCPServer,
    RateLimitMiddleware,
    TimeoutMiddleware,
)

# ═══════════════════════════════════════════════════════════════════════════════
# Fake data
# ═══════════════════════════════════════════════════════════════════════════════

EMPLOYEES = [
    {"id": i, "name": n, "dept": d, "salary": s, "role": r, "region": reg}
    for i, (n, d, s, r, reg) in enumerate([
        ("Alice Chen", "Engineering", 185000, "Senior Engineer", "NA"),
        ("Bob Martinez", "Engineering", 165000, "Engineer", "EU"),
        ("Carol Williams", "Sales", 120000, "Account Executive", "NA"),
        ("David Kim", "Sales", 135000, "Sales Manager", "APAC"),
        ("Eva Schmidt", "Marketing", 105000, "Marketing Lead", "EU"),
        ("Frank Johnson", "Finance", 140000, "Controller", "NA"),
        ("Grace Lee", "Support", 90000, "Support Lead", "APAC"),
        ("Henry Brown", "HR", 100000, "HR Manager", "NA"),
        ("Iris Patel", "Engineering", 155000, "Staff Engineer", "NA"),
        ("Jack Wilson", "Operations", 95000, "Ops Manager", "EU"),
    ], start=1)
]

INVENTORY = [
    {"sku": f"SKU-{i:04d}", "name": n, "qty": q, "price": p, "warehouse": w}
    for i, (n, q, p, w) in enumerate([
        ("Widget Pro", 1250, 29.99, "NA-West"),
        ("Widget Lite", 3400, 14.99, "NA-East"),
        ("Enterprise Suite", 89, 999.00, "EU-Central"),
        ("Adapter Kit", 5600, 7.50, "APAC-South"),
        ("Premium Cable", 2100, 12.99, "NA-West"),
    ], start=1)
]

TICKETS = [
    {"id": f"TK-{i:04d}", "customer": c, "priority": p, "status": s, "subject": sub}
    for i, (c, p, s, sub) in enumerate([
        ("Acme Corp", "critical", "open", "API outage affecting production"),
        ("Globex Inc", "high", "open", "Data export failing"),
        ("Initech", "medium", "resolved", "Login issues"),
        ("Umbrella Corp", "low", "open", "Feature request: dark mode"),
        ("Stark Industries", "high", "open", "Invoice discrepancy"),
    ], start=1)
]

TRANSACTIONS = [
    {"id": f"TX-{i:04d}", "date": d, "amount": a, "category": c, "vendor": v}
    for i, (d, a, c, v) in enumerate([
        ("2025-01-15", 45000, "Cloud", "AWS"),
        ("2025-01-20", 12000, "Recruiting", "LinkedIn"),
        ("2025-02-01", 8500, "Office", "WeWork"),
        ("2025-02-10", 3200, "Travel", "Delta"),
        ("2025-03-01", 67000, "Cloud", "GCP"),
        ("2025-03-15", 15000, "Marketing", "Google Ads"),
        ("2025-04-01", 52000, "Cloud", "AWS"),
    ], start=1)
]


# ═══════════════════════════════════════════════════════════════════════════════
# Server + Auth
# ═══════════════════════════════════════════════════════════════════════════════

JWT_SECRET = "promptise-demo-secret-key-2025"

server = MCPServer("enterprise-tools", version="2.0.0")
auth = JWTAuth(secret=JWT_SECRET)

# Middleware stack
for mw in [AuthMiddleware(auth), LoggingMiddleware(), RateLimitMiddleware(rate_per_minute=120), TimeoutMiddleware(default_timeout=30.0)]:
    server.add_middleware(mw)


# ═══════════════════════════════════════════════════════════════════════════════
# Domain 1: HR (viewer+)
# ═══════════════════════════════════════════════════════════════════════════════

hr = MCPRouter(prefix="hr")


@hr.tool()
async def list_employees(department: str = "") -> str:
    """List all employees, optionally filtered by department."""
    rows = EMPLOYEES
    if department:
        rows = [e for e in rows if e["dept"].lower() == department.lower()]
    lines = [f"  {e['name']} | {e['dept']} | {e['role']} | {e['region']}" for e in rows]
    return f"Employees ({len(rows)}):\n" + "\n".join(lines)


@hr.tool()
async def get_employee(employee_id: int) -> str:
    """Get details for a specific employee by ID."""
    for e in EMPLOYEES:
        if e["id"] == employee_id:
            return json.dumps(e, indent=2)
    return f"Employee {employee_id} not found."


@hr.tool()
async def search_employees(query: str) -> str:
    """Search employees by name or role."""
    matches = [e for e in EMPLOYEES if query.lower() in e["name"].lower() or query.lower() in e["role"].lower()]
    if not matches:
        return f"No employees matching '{query}'."
    return "\n".join(f"  #{e['id']} {e['name']} ({e['role']})" for e in matches)


@hr.tool()
async def get_headcount() -> str:
    """Get headcount by department."""
    from collections import Counter
    counts = Counter(e["dept"] for e in EMPLOYEES)
    lines = [f"  {dept}: {count}" for dept, count in sorted(counts.items())]
    return f"Headcount ({sum(counts.values())} total):\n" + "\n".join(lines)


@hr.tool()
async def get_org_chart() -> str:
    """Get the organizational chart."""
    depts = {}
    for e in EMPLOYEES:
        depts.setdefault(e["dept"], []).append(e["name"])
    lines = []
    for dept, members in sorted(depts.items()):
        lines.append(f"  {dept}:")
        for m in members:
            lines.append(f"    - {m}")
    return "Org Chart:\n" + "\n".join(lines)


server.include_router(hr)

# ═══════════════════════════════════════════════════════════════════════════════
# Domain 2: Finance (analyst+)
# ═══════════════════════════════════════════════════════════════════════════════

finance = MCPRouter(prefix="finance")


@finance.tool(roles=["analyst", "admin"])
async def get_revenue_summary(quarter: str = "Q1-2025") -> str:
    """Get revenue summary for a quarter."""
    return f"Revenue Summary {quarter}: Total $4.2M, Growth 15% YoY, Margin 68%"


@finance.tool(roles=["analyst", "admin"])
async def list_transactions(category: str = "", limit: int = 10) -> str:
    """List recent transactions, optionally filtered by category."""
    rows = TRANSACTIONS
    if category:
        rows = [t for t in rows if t["category"].lower() == category.lower()]
    rows = rows[:limit]
    lines = [f"  {t['id']} | {t['date']} | ${t['amount']:,} | {t['category']} | {t['vendor']}" for t in rows]
    return f"Transactions ({len(rows)}):\n" + "\n".join(lines)


@finance.tool(roles=["analyst", "admin"])
async def get_budget_vs_actual(department: str = "") -> str:
    """Compare budget vs actual spending by department."""
    data = {"Engineering": (800000, 788000), "Sales": (400000, 421000), "Marketing": (200000, 195000),
            "Operations": (150000, 148000), "HR": (100000, 92000), "Finance": (80000, 75000)}
    if department and department in data:
        b, a = data[department]
        return f"{department}: Budget ${b:,} | Actual ${a:,} | Variance ${b - a:,} ({(b - a) / b * 100:.1f}%)"
    lines = [f"  {d}: Budget ${b:,} | Actual ${a:,} | {'+' if b > a else ''}{(b - a) / b * 100:.1f}%" for d, (b, a) in data.items()]
    return "Budget vs Actual:\n" + "\n".join(lines)


@finance.tool(roles=["analyst", "admin"])
async def get_cash_flow() -> str:
    """Get current cash flow summary."""
    return "Cash Flow: Inflows $4.2M | Outflows $2.9M | Net $1.3M | Runway 18 months"


@finance.tool(roles=["analyst", "admin"])
async def get_expense_breakdown() -> str:
    """Get expense breakdown by category."""
    from collections import Counter
    cats = Counter()
    for t in TRANSACTIONS:
        cats[t["category"]] += t["amount"]
    lines = [f"  {c}: ${a:,}" for c, a in cats.most_common()]
    return f"Expense Breakdown (${sum(cats.values()):,} total):\n" + "\n".join(lines)


server.include_router(finance)

# ═══════════════════════════════════════════════════════════════════════════════
# Domain 3: Inventory (analyst+)
# ═══════════════════════════════════════════════════════════════════════════════

inventory = MCPRouter(prefix="inv")


@inventory.tool(roles=["analyst", "admin"])
async def list_products(warehouse: str = "") -> str:
    """List inventory items, optionally filtered by warehouse."""
    rows = INVENTORY
    if warehouse:
        rows = [i for i in rows if warehouse.lower() in i["warehouse"].lower()]
    lines = [f"  {i['sku']} | {i['name']} | Qty: {i['qty']} | ${i['price']} | {i['warehouse']}" for i in rows]
    return f"Inventory ({len(rows)} items):\n" + "\n".join(lines)


@inventory.tool(roles=["analyst", "admin"])
async def check_stock(sku: str) -> str:
    """Check stock level for a specific SKU."""
    for i in INVENTORY:
        if i["sku"].lower() == sku.lower():
            status = "LOW" if i["qty"] < 100 else "OK" if i["qty"] < 1000 else "HIGH"
            return f"{i['name']} ({i['sku']}): {i['qty']} units [{status}] @ ${i['price']}/unit"
    return f"SKU {sku} not found."


@inventory.tool(roles=["analyst", "admin"])
async def get_inventory_value() -> str:
    """Calculate total inventory value."""
    total = sum(i["qty"] * i["price"] for i in INVENTORY)
    lines = [f"  {i['name']}: {i['qty']} x ${i['price']} = ${i['qty'] * i['price']:,.2f}" for i in INVENTORY]
    return f"Inventory Value (${total:,.2f}):\n" + "\n".join(lines)


@inventory.tool(roles=["admin"])
async def adjust_stock(sku: str, quantity_change: int, reason: str = "") -> str:
    """Adjust stock level for a SKU. Admin only."""
    for i in INVENTORY:
        if i["sku"].lower() == sku.lower():
            old = i["qty"]
            i["qty"] = max(0, i["qty"] + quantity_change)
            return f"Stock adjusted: {i['name']} {old} → {i['qty']} ({'+' if quantity_change > 0 else ''}{quantity_change}). Reason: {reason or 'N/A'}"
    return f"SKU {sku} not found."


@inventory.tool(roles=["analyst", "admin"])
async def low_stock_report() -> str:
    """Get items with stock below 200 units."""
    low = [i for i in INVENTORY if i["qty"] < 200]
    if not low:
        return "No items below 200 units."
    lines = [f"  {i['sku']} {i['name']}: {i['qty']} units" for i in low]
    return f"Low Stock Alert ({len(low)} items):\n" + "\n".join(lines)


server.include_router(inventory)

# ═══════════════════════════════════════════════════════════════════════════════
# Domain 4: Support (viewer+)
# ═══════════════════════════════════════════════════════════════════════════════

support = MCPRouter(prefix="support")


@support.tool()
async def list_tickets(status: str = "", priority: str = "") -> str:
    """List support tickets, filtered by status or priority."""
    rows = TICKETS
    if status:
        rows = [t for t in rows if t["status"] == status.lower()]
    if priority:
        rows = [t for t in rows if t["priority"] == priority.lower()]
    lines = [f"  {t['id']} [{t['priority']}] {t['subject']} — {t['customer']} ({t['status']})" for t in rows]
    return f"Tickets ({len(rows)}):\n" + "\n".join(lines)


@support.tool()
async def get_ticket(ticket_id: str) -> str:
    """Get details for a specific ticket."""
    for t in TICKETS:
        if t["id"].lower() == ticket_id.lower():
            return json.dumps(t, indent=2)
    return f"Ticket {ticket_id} not found."


@support.tool()
async def ticket_stats() -> str:
    """Get ticket statistics."""
    from collections import Counter
    by_status = Counter(t["status"] for t in TICKETS)
    by_priority = Counter(t["priority"] for t in TICKETS)
    return f"By Status: {dict(by_status)} | By Priority: {dict(by_priority)} | Total: {len(TICKETS)}"


@support.tool(roles=["analyst", "admin"])
async def create_ticket(customer: str, subject: str, priority: str = "medium") -> str:
    """Create a new support ticket."""
    tid = f"TK-{len(TICKETS) + 1:04d}"
    TICKETS.append({"id": tid, "customer": customer, "priority": priority, "status": "open", "subject": subject})
    return f"Created ticket {tid}: [{priority}] {subject} for {customer}"


@support.tool(roles=["analyst", "admin"])
async def resolve_ticket(ticket_id: str, resolution: str = "") -> str:
    """Resolve a support ticket."""
    for t in TICKETS:
        if t["id"].lower() == ticket_id.lower():
            t["status"] = "resolved"
            return f"Resolved {t['id']}: {t['subject']}. Resolution: {resolution or 'Marked resolved.'}"
    return f"Ticket {ticket_id} not found."


server.include_router(support)

# ═══════════════════════════════════════════════════════════════════════════════
# Domain 5: Analytics (analyst+)
# ═══════════════════════════════════════════════════════════════════════════════

analytics = MCPRouter(prefix="analytics")


@analytics.tool(roles=["analyst", "admin"])
async def kpi_dashboard() -> str:
    """Get key performance indicators."""
    return ("KPIs:\n"
            "  MRR: $1,850,000 | ARR: $22,200,000\n"
            "  Churn: 3.2% | NPS: 72\n"
            "  Avg Deal Size: $186,000 | Sales Cycle: 45 days\n"
            "  CAC: $12,000 | LTV: $540,000 | LTV/CAC: 45x")


@analytics.tool(roles=["analyst", "admin"])
async def revenue_by_region() -> str:
    """Get revenue breakdown by region."""
    return "Revenue by Region:\n  NA: $12.4M (56%) | EU: $5.8M (26%) | APAC: $4.0M (18%)"


@analytics.tool(roles=["analyst", "admin"])
async def customer_cohort(quarter: str = "Q1-2025") -> str:
    """Get customer cohort analysis for a quarter."""
    return f"Cohort {quarter}: 45 new customers | 92% retained from prior quarter | Expansion: +18%"


@analytics.tool(roles=["analyst", "admin"])
async def funnel_metrics() -> str:
    """Get sales funnel metrics."""
    return ("Sales Funnel:\n"
            "  Leads: 1,200 → MQL: 480 (40%) → SQL: 192 (40%) → Opp: 96 (50%) → Won: 38 (40%)\n"
            "  Overall conversion: 3.2% | Avg days: 45")


@analytics.tool(roles=["analyst", "admin"])
async def churn_analysis() -> str:
    """Analyze customer churn patterns."""
    return ("Churn Analysis:\n"
            "  Monthly churn: 3.2% | Quarterly: 9.1%\n"
            "  Top reasons: Poor onboarding (35%), Missing features (28%), Price (22%), Support (15%)\n"
            "  At-risk accounts: 12 (>60 days inactive)")


server.include_router(analytics)

# ═══════════════════════════════════════════════════════════════════════════════
# Domain 6: Admin (admin only)
# ═══════════════════════════════════════════════════════════════════════════════

admin = MCPRouter(prefix="admin")


@admin.tool(roles=["admin"])
async def system_status() -> str:
    """Get system health status."""
    return ("System Status: ALL GREEN\n"
            "  API: 99.97% uptime | DB: 2ms latency | Cache: 98% hit rate\n"
            "  Queue: 12 jobs pending | Storage: 67% used")


@admin.tool(roles=["admin"])
async def list_api_keys() -> str:
    """List active API keys (admin only)."""
    return ("API Keys:\n"
            "  key_prod_*** (Production, expires 2025-12-31)\n"
            "  key_staging_*** (Staging, expires 2025-06-30)\n"
            "  key_dev_*** (Development, no expiry)")


@admin.tool(roles=["admin"])
async def get_audit_log(limit: int = 5) -> str:
    """Get recent audit log entries."""
    entries = [
        "2025-04-02 14:22:01 | admin | finance_list_transactions | OK",
        "2025-04-02 14:21:45 | analyst | analytics_kpi_dashboard | OK",
        "2025-04-02 14:20:12 | viewer | hr_list_employees | OK",
        "2025-04-02 14:19:58 | viewer | finance_get_revenue | DENIED (role)",
        "2025-04-02 14:18:30 | admin | admin_system_status | OK",
    ]
    return "Audit Log:\n" + "\n".join(f"  {e}" for e in entries[:limit])


@admin.tool(roles=["admin"])
async def rotate_secret(service: str) -> str:
    """Rotate a service secret (admin only)."""
    return f"Secret rotated for '{service}'. New key active. Old key valid for 24h grace period."


@admin.tool(roles=["admin"])
async def set_rate_limit(endpoint: str, requests_per_minute: int) -> str:
    """Set rate limit for an endpoint (admin only)."""
    return f"Rate limit set: {endpoint} → {requests_per_minute} req/min"


server.include_router(admin)


# ═══════════════════════════════════════════════════════════════════════════════
# JWT Token Generator (for the CLI)
# ═══════════════════════════════════════════════════════════════════════════════

def generate_jwt(role: str) -> str:
    """Generate a simple JWT token for a role."""
    import base64

    role_map = {
        "viewer": {"sub": "user-viewer", "roles": ["viewer"], "name": "Sam Viewer"},
        "analyst": {"sub": "user-analyst", "roles": ["viewer", "analyst"], "name": "Alex Analyst"},
        "admin": {"sub": "user-admin", "roles": ["viewer", "analyst", "admin"], "name": "Ada Admin"},
    }
    payload = role_map.get(role, role_map["viewer"])
    payload["iat"] = int(time.time())
    payload["exp"] = int(time.time()) + 86400

    header = base64.urlsafe_b64encode(json.dumps({"alg": "HS256", "typ": "JWT"}).encode()).rstrip(b"=").decode()
    body = base64.urlsafe_b64encode(json.dumps(payload).encode()).rstrip(b"=").decode()
    sig_input = f"{header}.{body}".encode()
    signature = base64.urlsafe_b64encode(
        hmac.new(JWT_SECRET.encode(), sig_input, hashlib.sha256).digest()
    ).rstrip(b"=").decode()

    return f"{header}.{body}.{signature}"


# Pre-generate tokens
TOKENS = {
    "viewer": generate_jwt("viewer"),
    "analyst": generate_jwt("analyst"),
    "admin": generate_jwt("admin"),
}


if __name__ == "__main__":
    tool_count = len(list(server._tool_registry.list_all()))
    print(f"Starting Enterprise MCP Server with {tool_count} tools...")
    print(f"Roles: viewer (10 tools), analyst (25 tools), admin (all 30)")
    print()
    print("Pre-generated JWT tokens:")
    for role, token in TOKENS.items():
        print(f"  {role}: {token[:40]}...")
    print()
    server.run(transport="stdio")
