"""CLI sub-app for the agent runtime.

Provides the ``promptise runtime`` command group with subcommands for
managing agent processes from the command line.

Commands::

    promptise runtime start <manifest>   Start from .agent file/directory
    promptise runtime stop <name>        Stop a running process
    promptise runtime status [name]      Show process status
    promptise runtime logs <name>        Show journal entries
    promptise runtime restart <name>     Restart a process
    promptise runtime validate <path>    Validate a .agent file
    promptise runtime init               Generate template .agent file

Usage::

    # Start an agent from a manifest
    promptise runtime start agents/watcher.agent

    # Start all agents in a directory
    promptise runtime start agents/

    # Check status
    promptise runtime status

    # View logs
    promptise runtime logs data-watcher --lines 50

    # Validate a manifest
    promptise runtime validate agents/watcher.agent
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Annotated

import typer
from rich.console import Console
from rich.table import Table

from .exceptions import ManifestError, ManifestValidationError
from .manifest import load_manifest, validate_manifest

runtime_app = typer.Typer(
    name="runtime",
    no_args_is_help=True,
    help="Manage long-running agent processes.",
)
console = Console()


# ---------------------------------------------------------------------------
# runtime start
# ---------------------------------------------------------------------------


@runtime_app.command()
def start(
    manifest: Annotated[
        str,
        typer.Argument(
            help="Path to .agent file or directory of .agent files",
        ),
    ],
    name: Annotated[
        str | None,
        typer.Option("--name", "-n", help="Override process name"),
    ] = None,
    detach: Annotated[
        bool,
        typer.Option(
            "--detach/--no-detach",
            "-d",
            help="Run in the background (prints PID)",
        ),
    ] = False,
    dashboard: Annotated[
        bool,
        typer.Option(
            "--dashboard/--no-dashboard",
            help="Enable live monitoring dashboard",
        ),
    ] = False,
) -> None:
    """Start agent process(es) from a .agent manifest or directory."""
    path = Path(manifest)

    if not path.exists():
        console.print(f"[red]Not found: {manifest}[/red]")
        raise typer.Exit(1)

    async def _run() -> None:
        from .runtime import AgentRuntime

        runtime = AgentRuntime()

        if path.is_dir():
            loaded = await runtime.load_directory(path)
            if not dashboard:
                console.print(
                    f"[green]Loaded {len(loaded)} process(es):[/green] " + ", ".join(loaded)
                )
        else:
            process = await runtime.load_manifest(path, name_override=name)
            if not dashboard:
                console.print(f"[green]Loaded process:[/green] {process.name}")

        await runtime.start_all()

        # Dashboard mode
        if dashboard and not detach:
            _dashboard_obj = None
            _collector = None
            try:
                from ._dashboard import (
                    RuntimeDashboard,
                    RuntimeDashboardState,
                    RuntimeDataCollector,
                )

                # Create dashboard state
                dash_state = RuntimeDashboardState(
                    runtime_name=path.name,
                    manifest_names=[p.name for p in runtime.processes.values()],
                )

                # Start data collector
                _collector = RuntimeDataCollector(
                    runtime,
                    dash_state,
                    interval=0.5,
                )
                _collector.start()

                # Start dashboard
                _dashboard_obj = RuntimeDashboard(
                    dash_state,
                    runtime=runtime,
                )
                _dashboard_obj.start()

                # Run until interrupted
                while True:
                    await asyncio.sleep(1)
            except (KeyboardInterrupt, asyncio.CancelledError):
                pass
            finally:
                if _dashboard_obj:
                    _dashboard_obj.stop()
                if _collector:
                    _collector.stop()

            await runtime.stop_all()
            return

        # Non-dashboard mode
        _print_status_table(runtime.status())

        if not detach:
            console.print("\n[dim]Press Ctrl+C to stop all processes.[/dim]")
            try:
                # Run until interrupted
                while True:
                    await asyncio.sleep(1)
            except (KeyboardInterrupt, asyncio.CancelledError):
                console.print("\n[yellow]Shutting down...[/yellow]")
        else:
            console.print("[dim]Running in background.[/dim]")
            return

        await runtime.stop_all()
        console.print("[green]All processes stopped.[/green]")

    try:
        asyncio.run(_run())
    except ManifestValidationError as exc:
        console.print(f"[red]Manifest validation failed:[/red] {exc}")
        raise typer.Exit(1)
    except ManifestError as exc:
        console.print(f"[red]Manifest error:[/red] {exc}")
        raise typer.Exit(1)
    except Exception as exc:
        console.print(f"[red]Error:[/red] {exc}")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# runtime stop
# ---------------------------------------------------------------------------


@runtime_app.command()
def stop(
    name: Annotated[
        str,
        typer.Argument(help="Process name to stop"),
    ],
    force: Annotated[
        bool,
        typer.Option("--force/--no-force", help="Force stop"),
    ] = False,
) -> None:
    """Stop a running agent process."""
    console.print(
        "[yellow]The stop command requires distributed mode, which is "
        "not available for foreground processes. Use Ctrl+C to stop a "
        "foreground process.[/yellow]"
    )


# ---------------------------------------------------------------------------
# runtime status
# ---------------------------------------------------------------------------


@runtime_app.command()
def status(
    name: Annotated[
        str | None,
        typer.Argument(help="Process name (omit for all)"),
    ] = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON"),
    ] = False,
) -> None:
    """Show status of agent processes."""
    console.print(
        "[yellow]The status command requires a running runtime to query. "
        "Process status is displayed when using 'promptise runtime start'.[/yellow]"
    )


# ---------------------------------------------------------------------------
# runtime logs
# ---------------------------------------------------------------------------


@runtime_app.command()
def logs(
    name: Annotated[
        str,
        typer.Argument(help="Process name"),
    ],
    lines: Annotated[
        int,
        typer.Option("--lines", "-n", help="Number of entries to show"),
    ] = 20,
    follow: Annotated[
        bool,
        typer.Option("--follow/--no-follow", "-f", help="Follow new entries"),
    ] = False,
    journal_path: Annotated[
        str,
        typer.Option("--journal-path", help="Journal directory"),
    ] = ".promptise/journal",
) -> None:
    """Show journal entries for a process."""
    from .journal.file import FileJournal

    async def _show() -> None:
        journal = FileJournal(base_path=journal_path)

        entries = await journal.read(name, limit=lines)

        if not entries:
            console.print(f"[dim]No journal entries for {name!r}[/dim]")
            return

        table = Table(title=f"Journal: {name}", show_lines=False)
        table.add_column("Time", style="dim", width=23)
        table.add_column("Type", style="cyan", width=20)
        table.add_column("Data", style="white")

        for entry in entries:
            ts = entry.timestamp
            if hasattr(ts, "isoformat"):
                time_str = ts.isoformat()[:23]
            elif isinstance(ts, str):
                time_str = ts[:23]
            else:
                time_str = "?"
            data_str = json.dumps(entry.data, default=str)
            if len(data_str) > 80:
                data_str = data_str[:77] + "..."
            table.add_row(time_str, entry.entry_type, data_str)

        console.print(table)

        await journal.close()

    asyncio.run(_show())


# ---------------------------------------------------------------------------
# runtime restart
# ---------------------------------------------------------------------------


@runtime_app.command()
def restart(
    name: Annotated[
        str,
        typer.Argument(help="Process name to restart"),
    ],
) -> None:
    """Restart a running agent process."""
    console.print(
        "[yellow]The restart command requires distributed mode, which is "
        "not available for foreground processes. Use Ctrl+C to stop a "
        "foreground process, then start it again.[/yellow]"
    )


# ---------------------------------------------------------------------------
# runtime validate
# ---------------------------------------------------------------------------


@runtime_app.command()
def validate(
    path: Annotated[
        str,
        typer.Argument(help="Path to .agent manifest file"),
    ],
) -> None:
    """Validate a .agent manifest file."""
    file_path = Path(path)

    if not file_path.exists():
        console.print(f"[red]Not found: {path}[/red]")
        raise typer.Exit(1)

    console.print(f"[bold]Validating {path}...[/bold]")

    try:
        warnings = validate_manifest(file_path)
        console.print("[green]✓[/green] Schema validation passed")

        if warnings:
            for w in warnings:
                console.print(f"[yellow]⚠ {w}[/yellow]")
        else:
            console.print("[green]✓[/green] No warnings")

        # Show parsed manifest summary
        manifest = load_manifest(file_path)
        table = Table(title="Manifest Summary", show_lines=False)
        table.add_column("Field", style="cyan")
        table.add_column("Value", style="white")

        table.add_row("Name", manifest.name)
        table.add_row("Model", manifest.model)
        table.add_row("Version", manifest.version)
        table.add_row(
            "Instructions",
            (manifest.instructions[:60] + "...")
            if manifest.instructions and len(manifest.instructions) > 60
            else (manifest.instructions or "(none)"),
        )
        table.add_row("Servers", str(len(manifest.servers)))
        table.add_row("Triggers", str(len(manifest.triggers)))

        console.print(table)
        console.print("[bold green]✓ Validation complete![/bold green]")

    except ManifestValidationError as exc:
        console.print("[red]✗ Schema validation failed:[/red]")
        console.print(str(exc))
        raise typer.Exit(1)
    except ManifestError as exc:
        console.print(f"[red]✗ Manifest error:[/red] {exc}")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# runtime init
# ---------------------------------------------------------------------------


@runtime_app.command(name="init")
def init_manifest(
    output: Annotated[
        str,
        typer.Option("--output", "-o", help="Output file path"),
    ] = "agent.agent",
    template: Annotated[
        str,
        typer.Option(
            "--template",
            "-t",
            help="Template type: basic, cron, webhook, full",
        ),
    ] = "basic",
    force: Annotated[
        bool,
        typer.Option("--force/--no-force", help="Overwrite existing file"),
    ] = False,
) -> None:
    """Generate a template .agent manifest file."""
    output_path = Path(output)

    if output_path.exists() and not force:
        console.print(f"[yellow]File already exists: {output}[/yellow]")
        console.print("Use --force to overwrite")
        raise typer.Exit(1)

    templates = {
        "basic": """\
version: "1.0"
name: my-agent
model: openai:gpt-5-mini
instructions: |
  You are a helpful assistant.

servers: {}

triggers: []
""",
        "cron": """\
version: "1.0"
name: data-watcher
model: openai:gpt-5-mini
instructions: |
  You monitor data pipelines and alert on anomalies.
  Check the current pipeline status and report any issues.

servers:
  data_tools:
    type: http
    url: http://localhost:8000/mcp

triggers:
  - type: cron
    cron_expression: "*/5 * * * *"

world:
  pipeline_status: healthy

config:
  concurrency: 1
  heartbeat_interval: 30
""",
        "webhook": """\
version: "1.0"
name: webhook-handler
model: openai:gpt-5-mini
instructions: |
  You process incoming webhook events and take appropriate actions.

servers:
  api_tools:
    type: http
    url: http://localhost:8000/mcp

triggers:
  - type: webhook
    webhook_path: /webhook
    webhook_port: 9090

config:
  concurrency: 3
""",
        "full": """\
version: "1.0"
name: full-agent
model: openai:gpt-5-mini
instructions: |
  You are a production agent with full configuration.
  You monitor data, respond to webhooks, and watch files.

servers:
  data_tools:
    type: http
    url: http://localhost:8000/mcp
  file_tools:
    type: stdio
    command: python
    args:
      - "-m"
      - "mytools.server"

triggers:
  - type: cron
    cron_expression: "*/5 * * * *"
  - type: webhook
    webhook_path: /events
    webhook_port: 9090
  - type: file_watch
    watch_path: /data/inbox
    watch_patterns:
      - "*.csv"
      - "*.json"

world:
  pipeline_status: healthy
  last_check: null

journal:
  level: full
  backend: file
  path: .promptise/journal

config:
  concurrency: 3
  heartbeat_interval: 15
  idle_timeout: 3600
  max_lifetime: 86400
  max_consecutive_failures: 5
  restart_policy: on_failure
""",
    }

    if template not in templates:
        console.print(f"[red]Unknown template: {template}[/red]")
        console.print(f"Available: {', '.join(templates.keys())}")
        raise typer.Exit(1)

    try:
        output_path.write_text(templates[template], encoding="utf-8")
        console.print(f"[green]✓ Created {output}[/green]")
        console.print(f"[dim]Template: {template}[/dim]")
        console.print("\n[bold]Next steps:[/bold]")
        console.print(f"  1. Edit {output} to customize your agent")
        console.print("  2. Validate: promptise runtime validate " + output)
        console.print("  3. Start:    promptise runtime start " + output)
    except Exception as exc:
        console.print(f"[red]Failed to write file:[/red] {exc}")
        raise typer.Exit(1)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _print_status_table(status: dict) -> None:
    """Print a Rich status table."""
    processes = status.get("processes", {})
    if not processes:
        console.print("[dim]No processes registered.[/dim]")
        return

    table = Table(title="Agent Processes", show_lines=False)
    table.add_column("Name", style="cyan", no_wrap=True)
    table.add_column("State", style="bold")
    table.add_column("PID", style="dim")
    table.add_column("Invocations", justify="right")
    table.add_column("Queue", justify="right")
    table.add_column("Uptime", style="dim")

    state_colors = {
        "running": "green",
        "starting": "yellow",
        "created": "dim",
        "stopped": "red",
        "failed": "bold red",
        "suspended": "yellow",
        "stopping": "yellow",
        "awaiting": "cyan",
    }

    for name, info in processes.items():
        state = info.get("state", "unknown")
        color = state_colors.get(state, "white")
        state_display = f"[{color}]{state.upper()}[/{color}]"

        uptime = info.get("uptime_seconds")
        if uptime is not None:
            hours = int(uptime // 3600)
            minutes = int((uptime % 3600) // 60)
            seconds = int(uptime % 60)
            uptime_str = f"{hours}h {minutes}m {seconds}s"
        else:
            uptime_str = "-"

        table.add_row(
            name,
            state_display,
            info.get("process_id", "-")[:8],
            str(info.get("invocation_count", 0)),
            str(info.get("queue_size", 0)),
            uptime_str,
        )

    console.print(table)
