"""MCP server with 40 tools across 8 business domains.

Run via stdio for e2e testing of semantic tool selection.
Each tool returns a deterministic string identifying itself.

Usage:
    python tests/_e2e_40tool_server.py
"""

from __future__ import annotations

from promptise.mcp.server import MCPRouter, MCPServer

server = MCPServer(name="e2e-40-tools", version="1.0.0")

# ── Domain 1: HR (5 tools) ──────────────────────────────────────────

hr = MCPRouter(prefix="hr")


@hr.tool()
async def list_employees(department: str | None = None) -> str:
    """List all employees, optionally filtered by department."""
    return f"[hr_list_employees] department={department}"


@hr.tool()
async def get_employee(employee_id: str) -> str:
    """Get detailed information about a specific employee by ID."""
    return f"[hr_get_employee] id={employee_id}"


@hr.tool()
async def create_employee(name: str, email: str, department: str) -> str:
    """Create a new employee record in the HR system."""
    return f"[hr_create_employee] name={name} email={email} dept={department}"


@hr.tool()
async def update_employee(employee_id: str, field: str, value: str) -> str:
    """Update a field on an existing employee record."""
    return f"[hr_update_employee] id={employee_id} {field}={value}"


@hr.tool()
async def terminate_employee(employee_id: str, reason: str) -> str:
    """Terminate an employee and initiate offboarding."""
    return f"[hr_terminate_employee] id={employee_id} reason={reason}"


server.include_router(hr)

# ── Domain 2: Finance (5 tools) ─────────────────────────────────────

finance = MCPRouter(prefix="finance")


@finance.tool()
async def get_invoice(invoice_id: str) -> str:
    """Retrieve a specific invoice by its ID."""
    return f"[finance_get_invoice] id={invoice_id}"


@finance.tool()
async def create_invoice(client_name: str, amount: float, description: str) -> str:
    """Create a new invoice for a client."""
    return f"[finance_create_invoice] client={client_name} amount={amount}"


@finance.tool()
async def list_payments(status: str | None = None) -> str:
    """List all payments, optionally filtered by status (pending, completed, failed)."""
    return f"[finance_list_payments] status={status}"


@finance.tool()
async def process_refund(payment_id: str, amount: float, reason: str) -> str:
    """Process a refund for a specific payment."""
    return f"[finance_process_refund] payment={payment_id} amount={amount}"


@finance.tool()
async def revenue_report(quarter: str, year: int) -> str:
    """Generate a revenue report for a specific quarter and year."""
    return f"[finance_revenue_report] quarter={quarter} year={year}"


server.include_router(finance)

# ── Domain 3: IT Operations (5 tools) ───────────────────────────────

it = MCPRouter(prefix="it")


@it.tool()
async def list_servers(environment: str | None = None) -> str:
    """List all servers, optionally filtered by environment (prod, staging, dev)."""
    return f"[it_list_servers] env={environment}"


@it.tool()
async def restart_server(server_name: str, force: bool = False) -> str:
    """Restart a specific server by name."""
    return f"[it_restart_server] name={server_name} force={force}"


@it.tool()
async def deploy_service(service_name: str, version: str, environment: str) -> str:
    """Deploy a service version to the specified environment."""
    return f"[it_deploy_service] service={service_name} version={version} env={environment}"


@it.tool()
async def check_health(service_name: str) -> str:
    """Check the health status of a service."""
    return f"[it_check_health] service={service_name}"


@it.tool(name="create_ticket")
async def it_create_ticket(title: str, priority: str, assignee: str | None = None) -> str:
    """Create an IT support ticket."""
    return f"[it_create_ticket] title={title} priority={priority}"


server.include_router(it)

# ── Domain 4: Marketing (5 tools) ───────────────────────────────────

marketing = MCPRouter(prefix="marketing")


@marketing.tool()
async def send_campaign(campaign_name: str, audience: str) -> str:
    """Send a marketing email campaign to a target audience segment."""
    return f"[marketing_send_campaign] name={campaign_name} audience={audience}"


@marketing.tool()
async def list_campaigns(status: str | None = None) -> str:
    """List all marketing campaigns, optionally filtered by status."""
    return f"[marketing_list_campaigns] status={status}"


@marketing.tool()
async def get_analytics(campaign_id: str) -> str:
    """Get performance analytics for a specific marketing campaign."""
    return f"[marketing_get_analytics] campaign={campaign_id}"


@marketing.tool()
async def create_ab_test(name: str, variant_a: str, variant_b: str) -> str:
    """Create an A/B test for comparing two marketing variants."""
    return f"[marketing_create_ab_test] name={name}"


@marketing.tool()
async def schedule_post(platform: str, content: str, scheduled_time: str) -> str:
    """Schedule a social media post on a specific platform."""
    return f"[marketing_schedule_post] platform={platform} time={scheduled_time}"


server.include_router(marketing)

# ── Domain 5: Inventory (5 tools) ───────────────────────────────────

inventory = MCPRouter(prefix="inventory")


@inventory.tool()
async def check_stock(product_id: str) -> str:
    """Check current stock level for a specific product."""
    return f"[inventory_check_stock] product={product_id}"


@inventory.tool()
async def reorder(product_id: str, quantity: int) -> str:
    """Place a reorder for a product to replenish inventory."""
    return f"[inventory_reorder] product={product_id} qty={quantity}"


@inventory.tool()
async def list_warehouses() -> str:
    """List all warehouses with their locations and capacity."""
    return "[inventory_list_warehouses]"


@inventory.tool()
async def transfer_stock(
    product_id: str, from_warehouse: str, to_warehouse: str, quantity: int
) -> str:
    """Transfer stock of a product between warehouses."""
    return (
        f"[inventory_transfer_stock] product={product_id} from={from_warehouse} to={to_warehouse}"
    )


@inventory.tool()
async def get_product(product_id: str) -> str:
    """Get detailed product information including name, price, and category."""
    return f"[inventory_get_product] product={product_id}"


server.include_router(inventory)

# ── Domain 6: Customer Support (5 tools) ────────────────────────────

support = MCPRouter(prefix="support")


@support.tool(name="create_ticket")
async def support_create_ticket(customer_email: str, subject: str, description: str) -> str:
    """Create a customer support ticket."""
    return f"[support_create_ticket] email={customer_email} subject={subject}"


@support.tool()
async def list_tickets(status: str | None = None, customer_email: str | None = None) -> str:
    """List support tickets, optionally filtered by status or customer."""
    return f"[support_list_tickets] status={status} email={customer_email}"


@support.tool()
async def assign_agent(ticket_id: str, agent_name: str) -> str:
    """Assign a support agent to a ticket."""
    return f"[support_assign_agent] ticket={ticket_id} agent={agent_name}"


@support.tool()
async def escalate_ticket(ticket_id: str, reason: str) -> str:
    """Escalate a support ticket to a higher tier."""
    return f"[support_escalate_ticket] ticket={ticket_id} reason={reason}"


@support.tool()
async def close_ticket(ticket_id: str, resolution: str) -> str:
    """Close a support ticket with a resolution summary."""
    return f"[support_close_ticket] ticket={ticket_id}"


server.include_router(support)

# ── Domain 7: Analytics (5 tools) ───────────────────────────────────

analytics = MCPRouter(prefix="analytics")


@analytics.tool()
async def run_query(sql: str) -> str:
    """Run an analytics SQL query against the data warehouse."""
    return f"[analytics_run_query] sql={sql}"


@analytics.tool()
async def create_dashboard(name: str, widgets: str) -> str:
    """Create a new analytics dashboard with specified widgets."""
    return f"[analytics_create_dashboard] name={name}"


@analytics.tool()
async def export_report(report_name: str, format: str = "pdf") -> str:
    """Export an analytics report in the specified format (pdf, csv, xlsx)."""
    return f"[analytics_export_report] name={report_name} format={format}"


@analytics.tool()
async def list_metrics(category: str | None = None) -> str:
    """List all available analytics metrics, optionally by category."""
    return f"[analytics_list_metrics] category={category}"


@analytics.tool()
async def set_alert(metric_name: str, threshold: float, direction: str) -> str:
    """Set an alert on a metric when it crosses a threshold."""
    return f"[analytics_set_alert] metric={metric_name} threshold={threshold}"


server.include_router(analytics)

# ── Domain 8: Calendar (5 tools) ────────────────────────────────────

calendar = MCPRouter(prefix="calendar")


@calendar.tool()
async def create_event(title: str, date: str, time: str, duration_minutes: int = 60) -> str:
    """Create a new calendar event."""
    return f"[calendar_create_event] title={title} date={date} time={time}"


@calendar.tool()
async def list_events(date: str | None = None) -> str:
    """List calendar events, optionally for a specific date."""
    return f"[calendar_list_events] date={date}"


@calendar.tool()
async def cancel_event(event_id: str, notify_attendees: bool = True) -> str:
    """Cancel a calendar event and optionally notify attendees."""
    return f"[calendar_cancel_event] id={event_id}"


@calendar.tool()
async def find_free_slots(date: str, duration_minutes: int = 30) -> str:
    """Find available time slots on a given date."""
    return f"[calendar_find_free_slots] date={date} duration={duration_minutes}"


@calendar.tool()
async def send_invite(event_id: str, attendees: str) -> str:
    """Send calendar invitations to attendees for an event."""
    return f"[calendar_send_invite] event={event_id} attendees={attendees}"


server.include_router(calendar)

# ── Entry point ─────────────────────────────────────────────────────

if __name__ == "__main__":
    server.run(transport="stdio")
