"""CLI for deepmcpagent: list tools and run an interactive agent session.

Notes:
    - The CLI path uses provider id strings for models (e.g., "openai:gpt-4.1"),
      which `init_chat_model` handles. In code, you can pass a model instance.
    - Model is REQUIRED (no fallback).
"""

from __future__ import annotations

import asyncio
from typing import Dict, List

import typer
from rich.console import Console
from rich.table import Table

from .agent import build_deep_agent
from .config import HTTPServerSpec, ServerSpec, StdioServerSpec

app = typer.Typer(no_args_is_help=True, add_completion=False)
console = Console()


def _parse_kv(opts: List[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for it in opts:
        if "=" not in it:
            raise typer.BadParameter(f"Expected key=value, got: {it}")
        k, v = it.split("=", 1)
        out[k.strip()] = v.strip()
    return out


def _merge_servers(stdios: List[List[str]], https: List[List[str]]) -> Dict[str, ServerSpec]:
    servers: Dict[str, ServerSpec] = {}

    # Keep stdio parsing for completeness (see note in StdioServerSpec docstring).
    for block in stdios:
        kv = _parse_kv(block)
        name = kv.pop("name")
        args = kv.pop("args", "")
        spec = StdioServerSpec(
            command=kv.pop("command"),
            args=[x for x in args.split(" ") if x] if args else [],
            env={k.split(".", 1)[1]: v for k, v in kv.items() if k.startswith("env.")},
            cwd=kv.get("cwd"),
            keep_alive=(kv.get("keep_alive", "true").lower() != "false"),
        )
        servers[name] = spec

    for block in https:
        kv = _parse_kv(block)
        name = kv.pop("name")
        headers = {k.split(".", 1)[1]: v for k, v in kv.items() if k.startswith("header.")}
        spec = HTTPServerSpec(
            url=kv.pop("url"),
            transport=kv.pop("transport", "http"),  # "http", "streamable-http", or "sse"
            headers=headers,
            auth=kv.get("auth"),
        )
        servers[name] = spec

    return servers


@app.command()
def list_tools(
    stdio: List[List[str]] = typer.Option(None, "--stdio", help="Block: name=... command=... args='...'", multiple=True),
    http: List[List[str]] = typer.Option(
        None, "--http", help="Block: name=... url=... [transport=http|streamable-http|sse] [header.X=Y]", multiple=True
    ),
    model_id: str = typer.Option(
        ...,
        "--model-id",
        help="REQUIRED model provider id string (e.g., 'openai:gpt-4.1', 'anthropic:claude-3-opus').",
    ),
    instructions: str = typer.Option("", "--instructions", help="Optional system prompt override."),
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
    stdio: List[List[str]] = typer.Option(None, "--stdio", help="Block: name=... command=... args='...'", multiple=True),
    http: List[List[str]] = typer.Option(
        None, "--http", help="Block: name=... url=... [transport=http|streamable-http|sse] [header.X=Y]", multiple=True
    ),
    model_id: str = typer.Option(
        ...,
        "--model-id",
        help="REQUIRED model provider id string (e.g., 'openai:gpt-4.1', 'anthropic:claude-3-opus').",
    ),
    instructions: str = typer.Option("", "--instructions", help="Optional system prompt override."),
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
