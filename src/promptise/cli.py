"""
CLI for promptise: list tools and run an interactive agent session.

Notes:
    - The CLI path uses provider id strings for models (e.g., "openai:gpt-4.1"),
      which `init_chat_model` handles. In code, you can pass a model instance.
    - Model is REQUIRED (no fallback).
    - Usage for repeated server specs:
        --stdio "name=echo command=python args='-m mypkg.server --port 3333' env.API_KEY=xyz keep_alive=false"
        --stdio "name=tool2 command=/usr/local/bin/tool2"
        --http  "name=remote url=http://127.0.0.1:8000/mcp transport=http"

      (Repeat --stdio/--http for multiple servers.)
"""

from __future__ import annotations

import asyncio
import shlex
from importlib.metadata import version as get_version
from typing import Annotated, Any, Literal, cast

import typer
from dotenv import load_dotenv
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .agent import build_agent
from .config import HTTPServerSpec, ServerSpec, StdioServerSpec
from .cross_agent import CrossAgent
from .exceptions import SuperAgentError, SuperAgentValidationError
from .superagent import load_superagent_file

load_dotenv()

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()

# Mount runtime sub-app
from .runtime.cli import runtime_app

app.add_typer(runtime_app, name="runtime")


@app.callback(invoke_without_command=True)
def _version_callback(
    version: Annotated[
        bool | None,
        typer.Option("--version", help="Show version and exit", is_eager=True),
    ] = None,
) -> None:
    """Global callback to support --version printing."""
    if version:
        console.print(get_version("promptise"))
        raise typer.Exit()


def _parse_kv(opts: list[str]) -> dict[str, str]:
    """Parse ['k=v', 'x=y', ...] into a dict. Values may contain spaces."""
    out: dict[str, str] = {}
    for it in opts:
        if "=" not in it:
            raise typer.BadParameter(f"Expected key=value, got: {it}")
        k, v = it.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _merge_servers(stdios: list[str], https: list[str]) -> dict[str, ServerSpec]:
    """
    Convert flat lists of block strings into server specs.

    Each entry in `stdios` / `https` is a single quoted string like:
      "name=echo command=python args='-m mymod --port 3333' env.API_KEY=xyz cwd=/tmp keep_alive=false"
      "name=remote url=http://127.0.0.1:8000/mcp transport=http"

    We first shlex-split the string into key=value tokens, then parse.
    """
    servers: dict[str, ServerSpec] = {}

    # stdio (kept for completeness)
    for block_str in stdios:
        tokens = shlex.split(block_str)
        kv = _parse_kv(tokens)

        name = kv.pop("name", None)
        if not name:
            raise typer.BadParameter("Missing required key: name (in --stdio block)")

        command = kv.pop("command", None)
        if not command:
            raise typer.BadParameter("Missing required key: command (in --stdio block)")

        args_value = kv.pop("args", "")
        args_list = shlex.split(args_value) if args_value else []

        env = {k.split(".", 1)[1]: v for k, v in list(kv.items()) if k.startswith("env.")}
        cwd = kv.get("cwd")
        keep_alive = kv.get("keep_alive", "true").lower() != "false"

        stdio_spec: ServerSpec = StdioServerSpec(
            command=command,
            args=args_list,
            env=env,
            cwd=cwd,
            keep_alive=keep_alive,
        )
        servers[name] = stdio_spec

    # http
    for block_str in https:
        tokens = shlex.split(block_str)
        kv = _parse_kv(tokens)

        name = kv.pop("name", None)
        if not name:
            raise typer.BadParameter("Missing required key: name (in --http block)")

        url = kv.pop("url", None)
        if not url:
            raise typer.BadParameter("Missing required key: url (in --http block)")

        transport_str = kv.pop("transport", "http")  # "http", "streamable-http", or "sse"
        transport = cast(Literal["http", "streamable-http", "sse"], transport_str)

        headers = {k.split(".", 1)[1]: v for k, v in list(kv.items()) if k.startswith("header.")}
        auth = kv.get("auth")

        http_spec: ServerSpec = HTTPServerSpec(
            url=url,
            transport=transport,
            headers=headers,
            auth=auth,
        )
        servers[name] = http_spec

    return servers


def _extract_final_answer(result: Any) -> str:
    """Best-effort extraction of the final text from various executors."""
    try:
        # LangGraph prebuilt returns {"messages": [ ... ]}
        if isinstance(result, dict) and "messages" in result and result["messages"]:
            last = result["messages"][-1]
            content = getattr(last, "content", None)
            if isinstance(content, str) and content:
                return content
            if isinstance(content, list) and content and isinstance(content[0], dict):
                return content[0].get("text") or str(content)
            return str(last)
        return str(result)
    except Exception:
        return str(result)


@app.command(name="list-tools")
def list_tools(
    model_id: Annotated[
        str,
        typer.Option("--model-id", help="REQUIRED model provider id (e.g., 'openai:gpt-4.1')."),
    ],
    stdio: Annotated[
        list[str] | None,
        typer.Option(
            "--stdio",
            help=(
                "Block string: \"name=... command=... args='...' "
                '[env.X=Y] [cwd=...] [keep_alive=true|false]". Repeatable.'
            ),
        ),
    ] = None,
    http: Annotated[
        list[str] | None,
        typer.Option(
            "--http",
            help=(
                'Block string: "name=... url=... [transport=http|streamable-http|sse] '
                '[header.X=Y] [auth=...]". Repeatable.'
            ),
        ),
    ] = None,
    instructions: Annotated[
        str,
        typer.Option("--instructions", help="Optional system prompt override."),
    ] = "",
) -> None:
    """List all MCP tools discovered using the provided server specs."""
    servers = _merge_servers(stdio or [], http or [])

    async def _run() -> None:
        agent = await build_agent(
            servers=servers,
            model=model_id,
            instructions=instructions or None,
        )

        table = Table(title="MCP Tools", show_lines=True)
        table.add_column("Tool", style="cyan", no_wrap=True)
        table.add_column("Description", style="green")
        table.add_column("Input Schema", style="white")

        for tool in agent.tools:
            schema_str = ""
            if hasattr(tool, "args_schema") and tool.args_schema is not None:
                try:
                    import json

                    schema_str = json.dumps(
                        tool.args_schema.model_json_schema(),
                        indent=2,
                    )
                except Exception:
                    schema_str = str(tool.args_schema)
            table.add_row(tool.name, tool.description or "", schema_str)

        console.print(table)

    asyncio.run(_run())


@app.command()
def run(
    model_id: Annotated[
        str,
        typer.Option(..., help="REQUIRED model provider id (e.g., 'openai:gpt-4.1')."),
    ],
    stdio: Annotated[
        list[str] | None,
        typer.Option(
            "--stdio",
            help=(
                "Block string: \"name=... command=... args='...' "
                '[env.X=Y] [cwd=...] [keep_alive=true|false]". Repeatable.'
            ),
        ),
    ] = None,
    http: Annotated[
        list[str] | None,
        typer.Option(
            "--http",
            help=(
                'Block string: "name=... url=... [transport=http|streamable-http|sse] '
                '[header.X=Y] [auth=...]". Repeatable.'
            ),
        ),
    ] = None,
    instructions: Annotated[
        str,
        typer.Option("--instructions", help="Optional system prompt override."),
    ] = "",
    # IMPORTANT: don't duplicate defaults in Option() and the parameter!
    trace: Annotated[
        bool,
        typer.Option("--trace/--no-trace", help="Print tool invocations & results."),
    ] = True,
    raw: Annotated[
        bool,
        typer.Option("--raw/--no-raw", help="Also print raw result object."),
    ] = False,
) -> None:
    """Start an interactive agent that uses only MCP tools."""
    servers = _merge_servers(stdio or [], http or [])

    async def _chat() -> None:
        graph = await build_agent(
            servers=servers,
            model=model_id,
            instructions=instructions or None,
            trace_tools=trace,  # <- enable promptise tool tracing
        )
        console.print("[bold]Promptise Foundry is ready. Type 'exit' to quit.[/bold]")
        while True:
            try:
                user = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                console.print("\nExiting.")
                break
            if user.lower() in {"exit", "quit"}:
                break
            if not user:
                continue
            try:
                result = await graph.ainvoke({"messages": [{"role": "user", "content": user}]})
            except Exception as exc:
                console.print(f"[red]Error during run:[/red] {exc}")
                continue

            final_text = _extract_final_answer(result)
            console.print(
                Panel(final_text or "(no content)", title="Final LLM Answer", style="bold green")
            )
            if raw:
                console.print(result)

    asyncio.run(_chat())


# =============================================================================
# New Commands for .superagent File Support
# =============================================================================


@app.command()
def agent(
    file: Annotated[
        str,
        typer.Argument(help="Path to .superagent configuration file"),
    ],
    model_id: Annotated[
        str | None,
        typer.Option("--model-id", help="Override model from config file"),
    ] = None,
    instructions: Annotated[
        str | None,
        typer.Option("--instructions", help="Override instructions from config file"),
    ] = None,
    trace: Annotated[
        bool | None,
        typer.Option("--trace/--no-trace", help="Override trace setting from config file"),
    ] = None,
    stdio: Annotated[
        list[str] | None,
        typer.Option(
            "--stdio",
            help="Additional stdio server (merged with config file servers)",
        ),
    ] = None,
    http: Annotated[
        list[str] | None,
        typer.Option(
            "--http",
            help="Additional http server (merged with config file servers)",
        ),
    ] = None,
    raw: Annotated[
        bool,
        typer.Option("--raw/--no-raw", help="Also print raw result object."),
    ] = False,
) -> None:
    """Run an agent from a .superagent configuration file.

    This command loads agent configuration from a .superagent file and
    optionally overrides specific settings via CLI flags. CLI flags always
    take precedence over file configuration.

    Examples:
        promptise agent my_agent.superagent
        promptise agent my_agent.superagent --model-id "openai:gpt-4o"
        promptise agent my_agent.superagent --no-trace
    """
    # Load configuration file
    try:
        main_loader, cross_loaders = load_superagent_file(file, resolve_refs=True)
    except SuperAgentValidationError as exc:
        console.print("[red]Configuration validation failed:[/red]")
        console.print(str(exc))
        raise typer.Exit(1)
    except SuperAgentError as exc:
        console.print(f"[red]Failed to load configuration:[/red] {exc}")
        raise typer.Exit(1)

    # Get base config
    config = main_loader.to_agent_config()

    # Apply CLI overrides
    if model_id:
        config.model = model_id
        console.print(f"[yellow]Overriding model:[/yellow] {model_id}")

    if instructions:
        config.instructions = instructions
        console.print("[yellow]Overriding instructions[/yellow]")

    if trace is not None:
        config.trace = trace
        console.print(f"[yellow]Overriding trace:[/yellow] {trace}")

    # Merge additional servers from CLI
    if stdio or http:
        extra_servers = _merge_servers(stdio or [], http or [])
        config.servers.update(extra_servers)
        console.print(f"[yellow]Added {len(extra_servers)} server(s) from CLI[/yellow]")

    # Build cross-agent mapping
    cross_agent_map: dict[str, CrossAgent] | None = None
    if cross_loaders:

        async def _build_cross_agents() -> dict[str, CrossAgent]:
            """Build cross-agent graphs asynchronously."""
            result: dict[str, CrossAgent] = {}
            for name, loader in cross_loaders.items():
                cross_config = loader.to_agent_config()
                # Build the cross-agent graph
                cross_graph = await build_agent(
                    servers=cross_config.servers,
                    model=cross_config.model,
                    instructions=cross_config.instructions,
                    trace_tools=cross_config.trace,
                )
                # Get description from schema
                desc = ""
                if loader.schema.cross_agents and name in loader.schema.cross_agents:
                    desc = loader.schema.cross_agents[name].description
                result[name] = CrossAgent(agent=cross_graph, description=desc)
            return result

        cross_agent_map = asyncio.run(_build_cross_agents())
        console.print(f"[green]Loaded {len(cross_agent_map)} cross-agent(s)[/green]")

    # Run agent
    async def _chat() -> None:
        """Start interactive chat session."""
        build_kwargs = config.to_build_kwargs()
        if cross_agent_map:
            build_kwargs["cross_agents"] = cross_agent_map
        graph = await build_agent(**build_kwargs)

        console.print(f"[bold]Promptise Foundry loaded from {file}. Type 'exit' to quit.[/bold]")

        while True:
            try:
                user = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                console.print("\nExiting.")
                break
            if user.lower() in {"exit", "quit"}:
                break
            if not user:
                continue
            try:
                result = await graph.ainvoke({"messages": [{"role": "user", "content": user}]})
            except Exception as exc:
                console.print(f"[red]Error during run:[/red] {exc}")
                continue

            final_text = _extract_final_answer(result)
            console.print(
                Panel(final_text or "(no content)", title="Final LLM Answer", style="bold green")
            )
            if raw:
                console.print(result)

    asyncio.run(_chat())


@app.command()
def validate(
    file: Annotated[
        str,
        typer.Argument(help="Path to .superagent configuration file to validate"),
    ],
    check_env: Annotated[
        bool,
        typer.Option("--check-env/--no-check-env", help="Check environment variables"),
    ] = True,
    check_refs: Annotated[
        bool,
        typer.Option("--check-refs/--no-check-refs", help="Validate cross-agent references"),
    ] = True,
) -> None:
    """Validate a .superagent configuration file.

    Performs dry-run validation without building the agent:
    - YAML syntax check
    - Schema validation
    - Environment variable availability check (optional)
    - Cross-agent reference validation (optional)

    Examples:
        promptise validate my_agent.superagent
        promptise validate my_agent.superagent --no-check-env
    """
    from .superagent import SuperAgentLoader

    console.print(f"[bold]Validating {file}...[/bold]")

    # Load and parse file
    try:
        loader = SuperAgentLoader.from_file(file)
        console.print("[green]✓[/green] File format and schema valid")
    except SuperAgentValidationError as exc:
        console.print("[red]✗ Schema validation failed:[/red]")
        console.print(str(exc))
        raise typer.Exit(1)
    except SuperAgentError as exc:
        console.print(f"[red]✗ Failed to load file:[/red] {exc}")
        raise typer.Exit(1)

    # Check environment variables
    if check_env:
        missing = loader.validate_env_vars()
        if missing:
            console.print("[yellow]⚠ Missing environment variables:[/yellow]")
            for var in missing:
                console.print(f"  - {var}")
            console.print(
                "[yellow]Note: Set these variables or use defaults "
                "(${VAR:-default}) to resolve.[/yellow]"
            )
        else:
            console.print("[green]✓[/green] All environment variables available")

    # Check cross-agent references
    if check_refs and loader.schema.cross_agents:
        try:
            cross_loaders = loader.resolve_cross_agents(recursive=False)
            console.print(
                f"[green]✓[/green] All {len(cross_loaders)} cross-agent reference(s) valid"
            )
        except SuperAgentError as exc:
            console.print(f"[red]✗ Cross-agent reference error:[/red] {exc}")
            raise typer.Exit(1)

    console.print("[bold green]✓ Validation complete![/bold green]")


@app.command()
def init(
    output: Annotated[
        str,
        typer.Option("--output", "-o", help="Output file path"),
    ] = "agent.superagent",
    template: Annotated[
        str,
        typer.Option(
            "--template",
            "-t",
            help="Template type: basic, http, stdio, cross-agent, advanced",
        ),
    ] = "basic",
    force: Annotated[
        bool,
        typer.Option("--force/--no-force", help="Overwrite existing file"),
    ] = False,
) -> None:
    """Generate a template .superagent configuration file.

    Creates a starter .superagent file with common patterns and best practices.

    Templates:
      basic       - Minimal HTTP server configuration
      http        - HTTP server with auth headers
      stdio       - Local stdio server configuration
      cross-agent - Multi-agent setup with cross-agent communication
      advanced    - Full-featured example with all options

    Examples:
        promptise init
        promptise init --output my_agent.superagent --template http
        promptise init -o advanced.superagent -t advanced --force
    """
    from pathlib import Path

    output_path = Path(output)

    # Check if file exists
    if output_path.exists() and not force:
        console.print(f"[yellow]File already exists: {output}[/yellow]")
        console.print("Use --force to overwrite")
        raise typer.Exit(1)

    # Template content
    templates = {
        "basic": """version: "1.0"

agent:
  model: "openai:gpt-4.1"
  instructions: "You are a helpful assistant."
  trace: true

servers:
  example:
    type: http
    url: "http://127.0.0.1:8000/mcp"
    transport: http
""",
        "http": """version: "1.0"

agent:
  model: "openai:gpt-4.1"
  instructions: "You are a helpful assistant with access to external tools."
  trace: true

servers:
  api_server:
    type: http
    url: "https://api.example.com/mcp"
    transport: http
    headers:
      Authorization: "Bearer ${API_TOKEN}"
      Content-Type: "application/json"
""",
        "stdio": """version: "1.0"

agent:
  model: "openai:gpt-4.1"
  instructions: "You are a helpful assistant with local tools."
  trace: true

servers:
  local_tools:
    type: stdio
    command: python
    args:
      - "-m"
      - "mypackage.server"
    env:
      API_KEY: "${MY_API_KEY}"
      DEBUG: "false"
    cwd: null
    keep_alive: true
""",
        "cross-agent": """version: "1.0"

agent:
  model: "openai:gpt-4.1"
  instructions: "You are a coordinator agent that can delegate to specialists."
  trace: true

servers:
  general_tools:
    type: http
    url: "http://127.0.0.1:8000/mcp"
    transport: http

cross_agents:
  math_specialist:
    file: ./agents/math_agent.superagent
    description: "Specialized agent for mathematical calculations and analysis"

  research_specialist:
    file: ./agents/research_agent.superagent
    description: "Specialized agent for web research and fact-checking"
""",
        "advanced": """version: "1.0"

agent:
  # Detailed model configuration
  model:
    provider: openai
    name: gpt-4.1
    api_key: ${OPENAI_API_KEY}
    temperature: 0.7
    max_tokens: 4096
    timeout: 60

  instructions: |
    You are an advanced AI assistant with access to multiple tools and
    specialist agents. Use available tools to gather information and
    delegate complex tasks to specialist agents when appropriate.

  trace: true

servers:
  # HTTP server with authentication
  remote_api:
    type: http
    url: "https://api.example.com/mcp"
    transport: http
    headers:
      Authorization: "Bearer ${API_TOKEN}"
      X-Custom-Header: "value"
    auth: ${AUTH_SECRET:-default_auth}

  # Local stdio server
  local_tools:
    type: stdio
    command: python
    args: ["-m", "mytools.server", "--port", "3000"]
    env:
      API_KEY: ${TOOL_API_KEY}
      LOG_LEVEL: "info"
    cwd: /tmp
    keep_alive: true

cross_agents:
  specialist_a:
    file: ./agents/specialist_a.superagent
    description: "Domain expert for task A"

  specialist_b:
    file: ./agents/specialist_b.superagent
    description: "Domain expert for task B"
""",
    }

    if template not in templates:
        console.print(f"[red]Unknown template: {template}[/red]")
        console.print(f"Available: {', '.join(templates.keys())}")
        raise typer.Exit(1)

    # Write template
    try:
        output_path.write_text(templates[template], encoding="utf-8")
        console.print(f"[green]✓ Created {output}[/green]")
        console.print(f"[dim]Template: {template}[/dim]")
        console.print("\n[bold]Next steps:[/bold]")
        console.print(f"  1. Edit {output} to customize configuration")
        console.print("  2. Set required environment variables")
        console.print(f"  3. Run: promptise agent {output}")
    except Exception as exc:
        console.print(f"[red]Failed to write file:[/red] {exc}")
        raise typer.Exit(1)
