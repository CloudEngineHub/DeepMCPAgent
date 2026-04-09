"""CLI ``serve`` command for running MCP servers from the command line.

Allows running any Python module that exposes an ``MCPServer`` instance
without writing boilerplate.

Usage::

    # Run a server from a module path
    promptise serve myapp.server:server --transport http --port 8080

    # With dashboard
    promptise serve myapp.server:server --dashboard

    # With hot reload (development)
    promptise serve myapp.server:server --reload
"""

from __future__ import annotations

import argparse
import importlib
import sys
from typing import Any


def build_serve_parser(subparsers: Any = None) -> argparse.ArgumentParser:
    """Build the ``serve`` subcommand parser.

    Args:
        subparsers: Parent subparser group (from argparse).
            If ``None``, creates a standalone parser.
    """
    if subparsers is not None:
        parser = subparsers.add_parser(
            "serve",
            help="Run an MCP server from a Python module",
        )
    else:
        parser = argparse.ArgumentParser(
            prog="promptise serve",
            description="Run an MCP server from a Python module",
        )

    parser.add_argument(
        "target",
        help="Server target in module:attribute format (e.g. myapp.server:server)",
    )
    parser.add_argument(
        "--transport",
        "-t",
        choices=["stdio", "http", "sse"],
        default="stdio",
        help="Transport type (default: stdio)",
    )
    parser.add_argument(
        "--host",
        default="127.0.0.1",
        help="Bind host for HTTP/SSE (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        "-p",
        type=int,
        default=8080,
        help="Bind port for HTTP/SSE (default: 8080)",
    )
    parser.add_argument(
        "--dashboard",
        action="store_true",
        help="Enable live terminal dashboard",
    )
    parser.add_argument(
        "--reload",
        action="store_true",
        help="Enable hot reload (development only)",
    )
    return parser


def resolve_server(target: str) -> Any:
    """Import and return the server instance from a target string.

    Args:
        target: ``module_path:attribute`` (e.g. ``"myapp.server:server"``).

    Returns:
        The resolved ``MCPServer`` instance.
    """
    if ":" not in target:
        raise ValueError(
            f"Invalid target format: {target!r}. "
            f"Expected 'module.path:attribute' (e.g. 'myapp.server:server')"
        )

    module_path, attr_name = target.rsplit(":", 1)

    # Add CWD to sys.path for local module imports
    import os

    cwd = os.getcwd()
    if cwd not in sys.path:
        sys.path.insert(0, cwd)

    try:
        module = importlib.import_module(module_path)
    except ImportError as e:
        raise ImportError(f"Cannot import module {module_path!r}: {e}") from e

    try:
        server = getattr(module, attr_name)
    except AttributeError:
        raise AttributeError(f"Module {module_path!r} has no attribute {attr_name!r}")

    return server


def run_serve(args: argparse.Namespace) -> None:
    """Execute the ``serve`` command.

    Args:
        args: Parsed command-line arguments.
    """
    server = resolve_server(args.target)

    if args.reload:
        from ._hot_reload import hot_reload

        hot_reload(
            server,
            transport=args.transport,
            host=args.host,
            port=args.port,
            dashboard=args.dashboard,
        )
    else:
        server.run(
            transport=args.transport,
            host=args.host,
            port=args.port,
            dashboard=args.dashboard,
        )
