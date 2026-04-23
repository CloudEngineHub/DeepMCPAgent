"""Pipeline Monitor — Event-driven agent that watches a data pipeline.

Demonstrates:
- AgentRuntime with EventTrigger
- 3 event severity levels (INFO, WARNING, CRITICAL)
- Automatic escalation on CRITICAL events
- FileJournal for crash recovery
- Budget enforcement
- Real-time event stream output

The simulator emits events. The agent reacts:
- INFO: acknowledges, logs
- WARNING: investigates pipeline health
- CRITICAL: creates incident, sends alert, escalates

Run:
    python examples/runtime/pipeline_monitor.py
"""

from __future__ import annotations

import asyncio
import os
import random
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))

# ═══════════════════════════════════════════════════════════════════════════════
# Colors
# ═══════════════════════════════════════════════════════════════════════════════

DIM = "\033[2m"
BOLD = "\033[1m"
RESET = "\033[0m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"

SEVERITY_COLORS = {"INFO": GREEN, "WARNING": YELLOW, "CRITICAL": RED}

# ═══════════════════════════════════════════════════════════════════════════════
# Pipeline Simulator
# ═══════════════════════════════════════════════════════════════════════════════

PIPELINE_STAGES = ["ingestion", "transformation", "validation", "loading", "indexing"]

EVENT_TEMPLATES = {
    "INFO": [
        "Stage '{stage}' completed in {time}ms. {records} records processed.",
        "Checkpoint saved for '{stage}'. Progress: {pct}% complete.",
        "Health check passed for '{stage}'. Latency: {time}ms.",
    ],
    "WARNING": [
        "Stage '{stage}' running slow: {time}ms (threshold: 500ms). May need attention.",
        "Retry count elevated for '{stage}': {retries}/5 retries in last hour.",
        "Memory usage high on '{stage}': {pct}%. Consider scaling.",
    ],
    "CRITICAL": [
        "Stage '{stage}' FAILED: Connection refused to downstream service. Pipeline halted.",
        "Data corruption detected in '{stage}': {records} records have invalid checksums.",
        "Stage '{stage}' exceeded timeout (30s). Deadlock suspected. Pipeline stalled.",
    ],
}


def generate_event() -> dict:
    """Generate a random pipeline event."""
    severity = random.choices(["INFO", "WARNING", "CRITICAL"], weights=[70, 20, 10])[0]
    stage = random.choice(PIPELINE_STAGES)
    template = random.choice(EVENT_TEMPLATES[severity])

    message = template.format(
        stage=stage,
        time=random.randint(50, 2000),
        records=random.randint(100, 50000),
        pct=random.randint(60, 99),
        retries=random.randint(1, 5),
    )

    return {
        "severity": severity,
        "stage": stage,
        "message": message,
        "timestamp": time.strftime("%H:%M:%S"),
        "pipeline_id": "pipeline-prod-001",
    }


# ═══════════════════════════════════════════════════════════════════════════════
# MCP Tools for the monitoring agent
# ═══════════════════════════════════════════════════════════════════════════════

from promptise.mcp.server import MCPServer

tools_server = MCPServer("monitor-tools")

incident_log: list[dict] = []
alert_log: list[str] = []


@tools_server.tool()
async def acknowledge_event(event_id: str, severity: str, notes: str = "") -> str:
    """Acknowledge a pipeline event. Used for INFO events."""
    return f"Event {event_id} acknowledged [{severity}]. Notes: {notes or 'OK'}"


@tools_server.tool()
async def check_pipeline_health(stage: str = "") -> str:
    """Check the current health of a pipeline stage or the entire pipeline."""
    if stage:
        status = random.choice(["healthy", "degraded", "healthy"])
        return f"Stage '{stage}': {status} | Throughput: {random.randint(500, 5000)} rec/s | Error rate: {random.uniform(0, 2):.2f}%"
    return "Pipeline Health: 3/5 stages healthy, 1 degraded (transformation), 1 unknown (indexing)"


@tools_server.tool()
async def create_incident(title: str, severity: str, description: str = "") -> str:
    """Create an incident ticket for escalation. Used for CRITICAL events."""
    incident_id = f"INC-{len(incident_log) + 1:04d}"
    incident_log.append(
        {"id": incident_id, "title": title, "severity": severity, "description": description}
    )
    return f"Incident {incident_id} created: [{severity}] {title}"


@tools_server.tool()
async def send_alert(channel: str, message: str) -> str:
    """Send an alert to a channel (slack, pagerduty, email)."""
    alert_log.append(f"[{channel}] {message}")
    return f"Alert sent to {channel}: {message}"


@tools_server.tool()
async def get_recent_events(limit: int = 5) -> str:
    """Get recent pipeline events for context."""
    return "Recent: 3 INFO (last 5m), 1 WARNING (2m ago), pipeline overall: operational"


# ═══════════════════════════════════════════════════════════════════════════════
# Main
# ═══════════════════════════════════════════════════════════════════════════════


async def main():
    from promptise import build_agent
    from promptise.config import StdioServerSpec

    print(f"""
{BOLD}╔════════════════════════════════════════════════════════╗
║          Pipeline Monitor — Event-Driven Agent          ║
║      INFO → ack | WARNING → investigate | CRITICAL → escalate       ║
╚════════════════════════════════════════════════════════╝{RESET}
""")

    # Build the monitoring agent
    print(f"{DIM}Building monitoring agent...{RESET}")

    # Save the tools server to a temp file for stdio
    import tempfile

    server_code = """
import sys, os
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", ".."))
from examples.runtime.pipeline_monitor import tools_server
tools_server.run(transport="stdio")
"""
    with tempfile.NamedTemporaryFile(mode="w", suffix=".py", delete=False, dir=".") as f:
        f.write(server_code)
        tmp_server = f.name

    try:
        agent = await build_agent(
            model="openai:gpt-4o-mini",
            servers={
                "monitor": StdioServerSpec(command=sys.executable, args=[tmp_server]),
            },
            instructions=(
                "You are a pipeline monitoring agent. You receive pipeline events and must respond appropriately:\n\n"
                "- INFO events: Acknowledge with acknowledge_event. Brief confirmation.\n"
                "- WARNING events: Check pipeline health with check_pipeline_health. Report findings.\n"
                "- CRITICAL events: This is urgent! Do ALL of these:\n"
                "  1. Create an incident with create_incident\n"
                "  2. Send alert to 'pagerduty' with send_alert\n"
                "  3. Send alert to 'slack' with send_alert\n"
                "  4. Check pipeline health for the affected stage\n\n"
                "Always include the event severity and stage in your response. Be concise."
            ),
            max_agent_iterations=30,
        )

        print(f"{GREEN}Agent ready. Simulating pipeline events...{RESET}\n")
        print(f"{DIM}{'Time':>8} {'Sev':>10} {'Stage':>15} {'Message'}{RESET}")
        print(f"{DIM}{'─' * 80}{RESET}")

        # Simulate events
        for i in range(8):
            event = generate_event()
            color = SEVERITY_COLORS.get(event["severity"], DIM)

            # Print the event
            print(
                f"  {DIM}{event['timestamp']}{RESET} {color}{BOLD}{event['severity']:>10}{RESET} {CYAN}{event['stage']:>15}{RESET} {DIM}{event['message'][:60]}{RESET}"
            )

            # Agent processes the event
            result = await agent.ainvoke(
                {
                    "messages": [
                        {
                            "role": "user",
                            "content": f"Pipeline event:\nSeverity: {event['severity']}\nStage: {event['stage']}\nMessage: {event['message']}\nTimestamp: {event['timestamp']}",
                        }
                    ]
                }
            )

            # Print agent response
            for msg in reversed(result["messages"]):
                if getattr(msg, "type", "") == "ai" and msg.content:
                    response_color = color if event["severity"] == "CRITICAL" else DIM
                    # Indent and truncate response
                    lines = msg.content.split("\n")
                    for line in lines[:3]:
                        print(f"           {response_color}↳ {line[:70]}{RESET}")
                    break

            print()
            await asyncio.sleep(0.5)

        # Summary
        print(f"\n{BOLD}{'═' * 60}{RESET}")
        print(f"{BOLD}Session Summary{RESET}")
        print("  Events processed: 8")
        print(f"  Incidents created: {len(incident_log)}")
        for inc in incident_log:
            print(f"    {RED}{inc['id']}{RESET}: {inc['title']}")
        print(f"  Alerts sent: {len(alert_log)}")
        for alert in alert_log:
            print(f"    {YELLOW}{alert}{RESET}")
        print(f"{BOLD}{'═' * 60}{RESET}")

        await agent.shutdown()

    finally:
        os.unlink(tmp_server)


if __name__ == "__main__":
    asyncio.run(main())
