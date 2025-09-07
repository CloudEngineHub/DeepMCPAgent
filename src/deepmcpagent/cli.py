"""
CLI for deepmcpagent: list tools and run an interactive agent session.

Notes:
    - The CLI path uses provider id strings for models (e.g., "openai:gpt-4.1"),
      which `init_chat_model` handles. In code, you can pass a model instance.
    - Model is REQUIRED (no fallback).
    - Usage for repeated server specs:
        --stdio "name=echo command=python args='-m mypkg.server --port 3333' env.API_KEY=xyz keep_alive=false"
        --stdio "name=tool2 command=/usr/local/bin/tool2"
        --http  "name=remote url=https://example.com/mcp transport=http header.Authorization='Bearer abc'"

      (Repeat --stdio/--http for multiple servers.)
"""

from __future__ import annotations

import asyncio
import shlex
from typing import Dict, List

import typer
from rich.console import Console
from rich.table import Table

from .agent import build_deep_agent
from .config import HTTPServerSpec, ServerSpec, StdioServerSpec
from .version import __version__

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()


@app.callback(invoke_without_command=True)
def _version_callback(
    version: bool = typer.Option(
        None,
        "--version",
        help="Show version and exit",
        is_eager=True,
    )
) -> None:
    """Global callback to support --version printing."""
    if version:
        console.print(__version__)
        raise typer.Exit()


def _parse_kv(opts: List[str]) -> Dict[str, str]:
    """Parse ['k=v', 'x=y', ...] into a dict. Values may contain spaces."""
    out: Dict[str, str] = {}
    for it in opts:
        if "=" not in it:
            raise typer.BadParameter(f"Expected key=value, got: {it}")
        k, v = it.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _merge_servers(stdios: List[str], https: List[str]) -> Dict[str, ServerSpec]:
    """
    Convert flat lists of block strings into server specs.

    Each entry in `stdios` / `https` is a single quoted string like:
      "name=echo command=python args='-m mymod --port 3333' env.API_KEY=xyz cwd=/tmp keep_alive=false"
      "name=remote url=https://example.com transport=sse header.Authorization='Bearer abc'"

    We first shlex-split the string into key=value tokens, then parse.
    """
    servers: Dict[str, ServerSpec] = {}

    # Keep stdio parsing for completeness (see note in StdioServerSpec docstring).
    for block_str in stdios:
        tokens = shlex.split(block_str)
        kv = _parse_kv(tokens)

        name = kv.pop("name", None)
        if not name:
            raise typer.BadParameter("Missing required key: name (in --stdio block)")

        command = kv.pop("command", None)
        if not command:
            raise typer.BadParameter("Missing required key: command (in --stdio block)")

        # args may contain spaces; split them as real argv respecting quotes
        args_value = kv.pop("args", "")
        args_list = shlex.split(args_value) if args_value else []

        env = {k.split(".", 1)[1]: v for k, v in list(kv.items()) if k.startswith("env.")}
        cwd = kv.get("cwd")
        keep_alive = (kv.get("keep_alive", "true").lower() != "false")

        spec = StdioServerSpec(
            command=command,
            args=args_list,
            env=env,
            cwd=cwd,
            keep_alive=keep_alive,
        )
        servers[name] = spec

    for block_str in https:
        tokens = shlex.split(block_str)
        kv = _parse_kv(tokens)

        name = kv.pop("name", None)
        if not name:
            raise typer.BadParameter("Missing required key: name (in --http block)")

        url = kv.pop("url", None)
        if not url:
            raise typer.BadParameter("Missing required key: url (in --http block)")

        transport = kv.pop("transport", "http")  # "http", "streamable-http", or "sse"
        headers = {k.split(".", 1)[1]: v for k, v in list(kv.items()) if k.startswith("header.")}
        auth = kv.get("auth")

        spec = HTTPServerSpec(
            url=url,
            transport=transport,
            headers=headers,
            auth=auth,
        )
        servers[name] = spec

    return servers


@app.command()
def list_tools(
    stdio: List[str] | None = typer.Option(
        None,
        "--stdio",
        help="Block string: \"name=... command=... args='...' [env.X=Y] [cwd=...] [keep_alive=true|false]\". Repeatable.",
    ),  # noqa: B008
    http: List[str] | None = typer.Option(  # noqa: B008
        None,
        "--http",
        help="Block string: \"name=... url=... [transport=http|streamable-http|sse] [header.X=Y] [auth=...]\". Repeatable.",
    ),  # noqa: B008
    model_id: str = typer.Option(
        ...,
        "--model-id",
        help="REQUIRED model provider id string (e.g., 'openai:gpt-4.1', 'anthropic:claude-3-opus').",
    ),  # noqa: B008
    instructions: str = typer.Option("", "--instructions", help="Optional system prompt override."),  # noqa: B008
):
    """List all MCP tools discovered using the provided server specs."""
    servers = _merge_servers(stdio or [], http or [])

    async def _run():
        graph, loader = await build_deep_agent(
            servers=servers,
            model=model_id,
            instructions=instructions or None,
        )
        infos = await loader.list_tool_info()
        table = Table(title="MCP Tools")
        table.add_column("Tool")
        table.add_column("Description")
        table.add_column("Input Schema")
        import json as _json

        for i in infos:
            table.add_row(i.name, i.description or "-", _json.dumps(i.input_schema))
        console.print(table)

    asyncio.run(_run())


@app.command()
def run(
    stdio: List[str] | None = typer.Option(
        None,
        "--stdio",
        help="Block string: \"name=... command=... args='...' [env.X=Y] [cwd=...] [keep_alive=true|false]\". Repeatable.",
    ),  # noqa: B008
    http: List[str] | None = typer.Option(  # noqa: B008
        None,
        "--http",
        help="Block string: \"name=... url=... [transport=http|streamable-http|sse] [header.X=Y] [auth=...]\". Repeatable.",
    ),  # noqa: B008
    model_id: str = typer.Option(
        ...,
        "--model-id",
        help="REQUIRED model provider id string (e.g., 'openai:gpt-4.1', 'anthropic:claude-3-opus').",
    ),  # noqa: B008
    instructions: str = typer.Option("", "--instructions", help="Optional system prompt override."),  # noqa: B008
):
    """Start an interactive agent that uses only MCP tools."""
    servers = _merge_servers(stdio or [], http or [])

    async def _chat():
        graph, _ = await build_deep_agent(
            servers=servers,
            model=model_id,
            instructions=instructions or None,
        )
        console.print("[bold]DeepMCPAgent is ready. Type 'exit' to quit.[/bold]")
        while True:
            try:
                user = input("> ").strip()
            except (EOFError, KeyboardInterrupt):
                console.print("\nExiting.")
                break
            if user.lower() in {"exit", "quit"}:
                break
            result = await graph.ainvoke({"messages": [{"role": "user", "content": user}]})
            console.print(result)

    asyncio.run(_chat())
