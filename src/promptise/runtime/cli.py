"""CLI sub-app for the agent runtime.

Provides the ``promptise runtime`` command group with subcommands for
managing agent processes from the command line.

Commands::

    promptise runtime start <manifest>   Start from .agent file/directory
    promptise runtime status [name]      Show a running runtime's processes
    promptise runtime stop [name]        Stop a runtime (or one process)
    promptise runtime restart [name]     Restart a runtime's processes (or one)
    promptise runtime logs <process>     Show (or follow) journal entries
    promptise runtime validate <path>    Validate .agent file(s)
    promptise runtime init               Generate template .agent file

How the commands find a runtime: ``start`` serves the Orchestration API on
``127.0.0.1`` (a free port unless ``--api-port`` is given) with a random
bearer token, and writes a state file (pid, port, token, manifests) to
``~/.promptise/runtime/<runtime name>.json`` (directory ``0700``, file
``0600``; ``$PROMPTISE_RUNTIME_STATE_DIR`` moves it).  ``status``, ``stop``,
``restart`` and ``logs`` read it; with one runtime running they need no name.
The file is removed when the runtime exits, and a file left by a runtime
that died is cleaned up the next time a command looks.  ``--url`` (and
``--token`` or ``$PROMPTISE_RUNTIME_TOKEN``) targets any Orchestration API
instead, for example one on another machine.

``start`` shuts down gracefully on Ctrl+C and on SIGTERM (``docker stop``).

Usage::

    # Start an agent from a manifest (in the foreground)
    promptise runtime start agents/watcher.agent

    # Start all agents in a directory, in the background
    promptise runtime start agents/ --detach

    # Check status, view logs, stop
    promptise runtime status
    promptise runtime logs data-watcher --lines 50 --follow
    promptise runtime stop

    # Validate a manifest
    promptise runtime validate agents/watcher.agent
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import signal
import sys
import threading
import time
from collections import deque
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Annotated, Any, NoReturn, TypeVar

import typer
from rich.console import Console
from rich.markup import escape
from rich.table import Table

from ._cli_state import (
    LAUNCH_ENV,
    SHUTDOWN_PATH,
    TOKEN_ENV,
    RuntimeState,
    StateError,
    detach_popen_kwargs,
    find_state,
    kill_process,
    list_states,
    log_path,
    pid_alive,
    remove_state,
    safe_runtime_name,
    state_recorded,
    terminate_process,
    validate_runtime_name,
    wait_for_exit,
    write_state,
)
from .exceptions import ManifestError, ManifestValidationError
from .manifest import load_manifest, validate_manifest

if TYPE_CHECKING:
    from .api_client import OrchestrationClient
    from .runtime import AgentRuntime

runtime_app = typer.Typer(
    name="runtime",
    no_args_is_help=True,
    help=(
        "Manage long-running agent processes: start a runtime from .agent "
        "manifests, then status / stop / restart / logs it from another terminal."
    ),
)
console = Console()
err_console = Console(stderr=True, soft_wrap=True)

_T = TypeVar("_T")

#: Seconds ``start --detach`` waits for the background runtime to be ready.
_DETACH_READY_TIMEOUT = 60.0

#: Seconds ``stop`` waits after SIGTERM (the fallback) before giving up.
_SIGTERM_GRACE = 10.0


def _fail(message: str, code: int = 1) -> NoReturn:
    """Print an error (to stderr) and exit with *code*."""
    err_console.print(f"[red]Error:[/red] {escape(message)}")
    raise typer.Exit(code)


class _CLIError(Exception):
    """An error with a message for the user (raised inside ``start``)."""


# ---------------------------------------------------------------------------
# Shared options for commands that talk to a running runtime
# ---------------------------------------------------------------------------

_UrlOption = Annotated[
    str | None,
    typer.Option(
        "--url",
        help=(
            "Base URL of an Orchestration API to talk to (e.g. http://host:9100) "
            "instead of a runtime started on this machine."
        ),
    ),
]
_TokenOption = Annotated[
    str | None,
    typer.Option(
        "--token",
        help=(
            f"Bearer token for --url (default: ${TOKEN_ENV}; prefer the environment "
            "variable, a command-line token is visible to other users in ps)."
        ),
    ),
]
_ProcessOption = Annotated[
    str | None,
    typer.Option("--process", "-p", help="Act on this one process instead of the whole runtime."),
]


@dataclass
class _Target:
    """A runtime to talk to: a local one (from its state file) or ``--url``."""

    url: str
    token: str | None
    state: RuntimeState | None = None

    @property
    def label(self) -> str:
        return f"runtime {self.state.name!r}" if self.state else f"the runtime at {self.url}"


def _local_states() -> list[RuntimeState]:
    try:
        return list_states()[0]
    except StateError as exc:
        _fail(str(exc))


def _no_runtime_message() -> str:
    return (
        "No runtime is running on this machine (as this user). Start one with "
        "'promptise runtime start <manifest>', or pass --url to reach a remote one."
    )


def _resolve_targets(
    name: str | None,
    process: str | None,
    url: str | None,
    token: str | None,
    *,
    allow_many: bool = False,
) -> tuple[list[_Target], str | None]:
    """Find the runtime(s) a command targets, and the process (if any).

    *name* is a runtime name; for convenience it may also be the name of a
    process in exactly one running runtime (the command then acts on that
    process).  With *url*, *name* is a process name.
    """
    if url is None and token is not None:
        _fail(
            "--token only applies with --url; a runtime on this machine is reached "
            "with the token from its state file"
        )
    if url is not None:
        if not url.startswith(("http://", "https://")):
            _fail(f"--url must start with http:// or https:// (got {url!r})")
        if name and process and name != process:
            _fail("With --url, give the process name once (as NAME or --process)")
        token = token if token is not None else (os.environ.get(TOKEN_ENV) or None)
        return [_Target(url=url, token=token)], process or name
    states = _local_states()
    if not states:
        _fail(_no_runtime_message())
    by_name = {s.name: s for s in states}
    if name is not None:
        if name in by_name:
            return [_Target(by_name[name].url, by_name[name].token, by_name[name])], process
        holders = [s for s in states if name in s.processes]
        if len(holders) == 1 and process is None:
            state = holders[0]
            return [_Target(state.url, state.token, state)], name
        if len(holders) > 1:
            _fail(
                f"Process {name!r} runs in several runtimes ("
                + ", ".join(s.name for s in holders)
                + "); name the runtime and pass --process"
            )
        _fail(f"No runtime named {name!r} is running. Running: " + ", ".join(sorted(by_name)))
    if len(states) == 1 or (allow_many and process is None):
        return [_Target(s.url, s.token, s) for s in states], process
    _fail(
        "Several runtimes are running; name one: " + ", ".join(sorted(by_name)),
    )


class _Unreachable(Exception):
    """The runtime's API could not be reached."""


async def _call(
    target: _Target,
    action: Callable[[OrchestrationClient], Awaitable[_T]],
    *,
    timeout: float = 30.0,
) -> _T:
    """Run *action* with a client for *target*.

    Raises:
        _Unreachable: Connection refused, timed out, ...
        OrchestrationAPIError: The API answered with an error.
    """
    import httpx

    from .api_client import OrchestrationClient

    try:
        async with OrchestrationClient(target.url, token=target.token, timeout=timeout) as client:
            return await action(client)
    except httpx.HTTPStatusError:
        raise
    except httpx.HTTPError as exc:
        detail = str(exc) or type(exc).__name__
        raise _Unreachable(f"Cannot reach {target.label} at {target.url}: {detail}") from exc


def _api_error_message(target: _Target, exc: Any, process: str | None = None) -> str:
    status = getattr(exc, "status_code", None)
    code = getattr(exc, "code", None)
    message = getattr(exc, "error_message", str(exc))
    if status == 401:
        hint = (
            f"pass --token or set {TOKEN_ENV}"
            if target.state is None
            else "its state file is out of date; restart the runtime"
        )
        return f"{target.label} refused the token (401 UNAUTHORIZED): {hint}"
    if code == "PROCESS_NOT_FOUND":
        return f"No process named {process!r} in {target.label}" if process else message
    label = f"{status} {code}" if code else str(status)
    return f"{target.label} answered {label}: {message}"


def _api(
    target: _Target,
    action: Callable[[OrchestrationClient], Awaitable[_T]],
    *,
    process: str | None = None,
    timeout: float = 30.0,
) -> _T:
    """:func:`_call` from synchronous code; errors become a CLI error."""
    from .api_client import OrchestrationAPIError

    try:
        return asyncio.run(_call(target, action, timeout=timeout))
    except OrchestrationAPIError as exc:
        _fail(_api_error_message(target, exc, process))
    except _Unreachable as exc:
        hint = ""
        if target.state is not None:
            hint = f" (pid {target.state.pid} is alive; it may still be starting or be hung)"
        _fail(f"{exc}{hint}")


def _echo_json(data: Any) -> None:
    typer.echo(json.dumps(data, indent=2, default=str))


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
        typer.Option(
            "--name",
            "-n",
            help="Override the process name (single manifest file only)",
        ),
    ] = None,
    runtime_name: Annotated[
        str | None,
        typer.Option(
            "--runtime-name",
            "-r",
            help=(
                "Name of this runtime for status/stop/restart/logs (default: the "
                "process name for a file, the directory name for a directory)"
            ),
        ),
    ] = None,
    detach: Annotated[
        bool,
        typer.Option(
            "--detach/--no-detach",
            "-d",
            help=(
                "Run in the background: prints the runtime name, PID and log file; "
                "stop it with 'promptise runtime stop'"
            ),
        ),
    ] = False,
    dashboard: Annotated[
        bool,
        typer.Option(
            "--dashboard/--no-dashboard",
            help="Enable live monitoring dashboard (foreground only)",
        ),
    ] = False,
    api_host: Annotated[
        str,
        typer.Option(
            "--api-host",
            help=(
                "Address the control API listens on. Keep the loopback default unless "
                f"you need remote control (then set {TOKEN_ENV} so clients know the token)"
            ),
        ),
    ] = "127.0.0.1",
    api_port: Annotated[
        int,
        typer.Option(
            "--api-port",
            min=0,
            max=65535,
            help="Port of the control API (default 0: a free port)",
        ),
    ] = 0,
) -> None:
    """Start agent process(es) from a .agent manifest or directory.

    Runs in the foreground until Ctrl+C or SIGTERM (``docker stop``), or
    until ``promptise runtime stop``; both stop every process gracefully.
    The control API's bearer token is random unless $PROMPTISE_RUNTIME_TOKEN
    is set.
    """
    path = Path(manifest)

    if not path.exists():
        _fail(f"Not found: {manifest}")
    if name is not None and path.is_dir():
        _fail(
            "--name renames the process of a single manifest file and can't apply to a "
            "directory; use --runtime-name to name the runtime"
        )
    if detach and dashboard:
        _fail("--dashboard needs a terminal and can't be combined with --detach")

    try:
        rname = _runtime_name_for(path, name, runtime_name)
        existing = find_state(rname)
    except StateError as exc:
        _fail(str(exc))
    except ManifestValidationError as exc:
        _fail(f"Manifest validation failed: {exc}")
    except ManifestError as exc:
        _fail(f"Manifest error: {exc}")
    if existing is not None:
        _fail(
            f"A runtime named {rname!r} is already running (pid {existing.pid}). "
            f"Stop it with 'promptise runtime stop {rname}', or pick another name "
            "with --runtime-name."
        )

    if detach:
        _start_detached(path, name, rname, api_host, api_port)
        return

    try:
        code = asyncio.run(_run_foreground(path, name, rname, dashboard, api_host, api_port))
    except ManifestValidationError as exc:
        _fail(f"Manifest validation failed: {exc}")
    except ManifestError as exc:
        _fail(f"Manifest error: {exc}")
    except (_CLIError, StateError) as exc:
        _fail(str(exc))
    except Exception as exc:
        _fail(f"{type(exc).__name__}: {exc}")
    if code:
        raise typer.Exit(code)


def _runtime_name_for(path: Path, name: str | None, runtime_name: str | None) -> str:
    if runtime_name is not None:
        return validate_runtime_name(runtime_name)
    if path.is_dir():
        return safe_runtime_name(path.resolve().name)
    return safe_runtime_name(name or load_manifest(path).name)


class _SignalCatcher:
    """Turns SIGINT / SIGTERM (and SIGBREAK on Windows) into an event.

    POSIX uses ``loop.add_signal_handler``.  Windows has no loop signal
    handlers, so there a plain ``signal.signal`` handler hands the signal to
    the loop with ``call_soon_threadsafe``.  A second signal while shutting
    down exits at once (after *on_force*).
    """

    def __init__(
        self,
        loop: asyncio.AbstractEventLoop,
        event: asyncio.Event,
        on_force: Callable[[], None],
    ) -> None:
        self._loop = loop
        self._event = event
        self._on_force = on_force
        self._loop_signals: list[int] = []
        self._previous: dict[int, Any] = {}
        self.received: str | None = None

    def _signals(self) -> list[int]:
        names = ["SIGINT", "SIGTERM"] + (["SIGBREAK"] if sys.platform == "win32" else [])
        return [getattr(signal, n) for n in names if hasattr(signal, n)]

    def install(self) -> None:
        if threading.current_thread() is not threading.main_thread():
            return
        for sig in self._signals():
            if sys.platform != "win32":
                try:
                    self._loop.add_signal_handler(sig, self._fire, sig)
                    self._loop_signals.append(sig)
                    continue
                except (NotImplementedError, RuntimeError, ValueError):
                    pass
            try:
                self._previous[sig] = signal.signal(sig, self._from_signal)
            except (ValueError, OSError):
                pass

    def uninstall(self) -> None:
        for sig in self._loop_signals:
            with contextlib.suppress(Exception):
                self._loop.remove_signal_handler(sig)
        self._loop_signals.clear()
        for sig, previous in self._previous.items():
            with contextlib.suppress(ValueError, OSError, TypeError):
                signal.signal(sig, previous)
        self._previous.clear()

    def _from_signal(self, signum: int, frame: Any) -> None:
        with contextlib.suppress(RuntimeError):  # loop already closed
            self._loop.call_soon_threadsafe(self._fire, signum)

    def _fire(self, signum: int) -> None:
        if self._event.is_set():
            err_console.print("[red]Second signal: exiting without a clean shutdown.[/red]")
            with contextlib.suppress(Exception):
                self._on_force()
            os._exit(130)
        try:
            self.received = signal.Signals(signum).name
        except ValueError:
            self.received = str(signum)
        self._event.set()


def _make_api(
    runtime: AgentRuntime, host: str, port: int, token: str, shutdown: asyncio.Event
) -> Any:
    """An Orchestration API for *runtime*, plus ``POST /api/v1/runtime/shutdown``.

    The extra route (authenticated like every other) sets *shutdown*: the
    ``stop`` command uses it to shut the runtime down gracefully on every
    platform (Windows has no SIGTERM a process can catch).
    """
    from aiohttp import web

    from .api import OrchestrationAPI

    class _RuntimeCLIAPI(OrchestrationAPI):
        def _setup_routes(self) -> None:
            super()._setup_routes()
            assert self._app is not None
            self._app.router.add_post(SHUTDOWN_PATH, self._handle_shutdown)

        async def _handle_shutdown(self, request: web.Request) -> web.Response:
            asyncio.get_running_loop().call_soon(shutdown.set)
            return web.json_response({"status": "shutting_down"}, status=202)

    return _RuntimeCLIAPI(runtime, host=host, port=port, auth_token=token)


def _journal_info(process: Any) -> dict[str, Any] | None:
    journal = getattr(getattr(process, "config", None), "journal", None)
    if journal is None or getattr(journal, "level", "none") == "none":
        return None
    info: dict[str, Any] = {"backend": journal.backend, "level": journal.level}
    if journal.backend == "file":
        info["path"] = str(Path(journal.path).resolve())
    return info


async def _start_processes(runtime: AgentRuntime) -> dict[str, str]:
    """Start every registered process; return ``name -> error`` for failures."""
    failures: dict[str, str] = {}
    for pname in list(runtime.processes):
        try:
            await runtime.start_process(pname)
        except Exception as exc:
            failures[pname] = str(exc) or type(exc).__name__
    return failures


async def _wait_for_shutdown(
    runtime: AgentRuntime, shutdown: asyncio.Event, announce: Callable[[], None]
) -> bool:
    """Wait until a signal or the shutdown route sets *shutdown*.

    With ``AgentRuntime.run_forever`` (and ``request_shutdown``) the runtime
    runs under it and shuts itself down (stop and close every process);
    returns ``True`` then.  ``False``: the caller still has to stop the
    processes.

    The CLI keeps its own signal handlers (``install_signal_handlers=False``)
    instead of run_forever's: they also work on Windows, stay installed
    through the shutdown, and a second Ctrl+C / SIGTERM there exits at once.
    """
    run_forever = getattr(runtime, "run_forever", None)
    request_shutdown = getattr(runtime, "request_shutdown", None)
    if run_forever is None or request_shutdown is None or shutdown.is_set():
        await shutdown.wait()
        return False
    forever = asyncio.ensure_future(run_forever(install_signal_handlers=False))
    waiter = asyncio.ensure_future(shutdown.wait())
    try:
        await asyncio.wait({forever, waiter}, return_when=asyncio.FIRST_COMPLETED)
        announce()
        if not forever.done():
            request_shutdown()
        await forever
    finally:
        if not waiter.done():
            waiter.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await waiter
    return True


async def _run_foreground(
    path: Path,
    name: str | None,
    rname: str,
    dashboard: bool,
    api_host: str,
    api_port: int,
) -> int:
    """Run the runtime until shutdown; return the exit code."""
    import secrets

    from .runtime import AgentRuntime

    launch_id = os.environ.pop(LAUNCH_ENV, None)
    loop = asyncio.get_running_loop()
    shutdown = asyncio.Event()
    state: RuntimeState | None = None

    def _forget_state() -> None:
        if state is not None:
            remove_state(state)

    catcher = _SignalCatcher(loop, shutdown, _forget_state)
    catcher.install()
    announced = False

    def _announce() -> None:
        nonlocal announced
        if announced or dashboard:
            return
        announced = True
        if catcher.received:
            reason = f" ({catcher.received})"
        elif shutdown.is_set():
            reason = " (stop requested)"
        else:
            reason = ""
        console.print(f"\n[yellow]Shutting down{reason}...[/yellow]")

    runtime = AgentRuntime()
    api: Any = None
    shut_down = False
    dashboard_obj: Any = None
    collector: Any = None
    try:
        if path.is_dir():
            loaded = await runtime.load_directory(path)
            if not loaded:
                raise _CLIError(f"No .agent files in {path}")
            manifests = [str(p.resolve()) for p in sorted(path.glob("*.agent"))]
            if not dashboard:
                console.print(
                    f"[green]Loaded {len(loaded)} process(es):[/green] " + ", ".join(loaded)
                )
        else:
            process = await runtime.load_manifest(path, name_override=name)
            manifests = [str(path.resolve())]
            if not dashboard:
                console.print(f"[green]Loaded process:[/green] {process.name}")

        token = os.environ.get(TOKEN_ENV) or secrets.token_urlsafe(32)
        try:
            api = _make_api(runtime, api_host, api_port, token, shutdown)
            await api.start()
        except (OSError, ValueError) as exc:
            api = None
            raise _CLIError(
                f"Cannot start the control API on {api_host}:{api_port}: {exc}"
            ) from exc

        state = RuntimeState(
            name=rname,
            pid=os.getpid(),
            host=api_host,
            port=api.port,
            token=token,
            manifests=manifests,
            processes={p.name: {"journal": _journal_info(p)} for p in runtime.processes.values()},
            started_at=RuntimeState.now(),
            launch_id=launch_id,
            log_file=str(log_path(rname)) if launch_id else None,
        )
        write_state(state, exclusive=True)

        failures = await _start_processes(runtime)
        for pname, error in failures.items():
            err_console.print(
                f"[red]Process {escape(pname)} failed to start:[/red] {escape(error)}"
            )
        if failures and len(failures) == len(runtime.processes):
            err_console.print("[red]No process could start; shutting down.[/red]")
            return 1

        state.ready = True
        write_state(state, exclusive=False)

        if dashboard and not shutdown.is_set():
            from ._dashboard import (
                RuntimeDashboard,
                RuntimeDashboardState,
                RuntimeDataCollector,
            )

            dash_state = RuntimeDashboardState(
                runtime_name=path.name,
                manifest_names=[p.name for p in runtime.processes.values()],
            )
            collector = RuntimeDataCollector(runtime, dash_state, interval=0.5)
            collector.start()
            dashboard_obj = RuntimeDashboard(dash_state, runtime=runtime)
            dashboard_obj.start()
        elif not dashboard:
            _print_status_table(runtime.status())
            console.print(
                f"\n[bold]Runtime[/bold] {escape(rname)} (pid {state.pid}), "
                f"control API on {state.url}"
            )
            console.print(
                "[dim]Press Ctrl+C to stop all processes, or run "
                f"'promptise runtime stop {escape(rname)}' from another terminal.[/dim]"
            )

        shut_down = await _wait_for_shutdown(runtime, shutdown, _announce)
        return 0
    finally:
        if dashboard_obj is not None:
            with contextlib.suppress(Exception):
                dashboard_obj.stop()
        if collector is not None:
            with contextlib.suppress(Exception):
                collector.stop()
        _announce()
        try:
            if not shut_down:
                close = getattr(runtime, "shutdown", None) or runtime.stop_all
                await close()
        finally:
            if api is not None:
                with contextlib.suppress(Exception):
                    await api.stop()
            _forget_state()
            catcher.uninstall()
        if not dashboard:
            console.print("[green]All processes stopped.[/green]")


def _start_detached(path: Path, name: str | None, rname: str, api_host: str, api_port: int) -> None:
    """Start the runtime as a detached background process and wait until it is ready."""
    import secrets
    import subprocess

    try:
        log = log_path(rname)
    except StateError as exc:
        _fail(str(exc))
    launch_id = secrets.token_hex(8)
    cmd = [
        sys.executable,
        "-m",
        "promptise",
        "runtime",
        "start",
        str(path.resolve()),
        "--runtime-name",
        rname,
        "--api-host",
        api_host,
        "--api-port",
        str(api_port),
        "--no-detach",
    ]
    if name is not None:
        cmd += ["--name", name]
    env = dict(os.environ)
    env[LAUNCH_ENV] = launch_id
    env["PYTHONUNBUFFERED"] = "1"
    env.setdefault("PYTHONIOENCODING", "utf-8")

    fd = os.open(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    if os.name == "posix":
        os.fchmod(fd, 0o600)
    with os.fdopen(fd, "wb") as log_file:
        proc = subprocess.Popen(
            cmd,
            stdin=subprocess.DEVNULL,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=os.getcwd(),
            env=env,
            close_fds=True,
            **detach_popen_kwargs(),
        )

    deadline = time.monotonic() + _DETACH_READY_TIMEOUT
    state: RuntimeState | None = None
    while time.monotonic() < deadline:
        code = proc.poll()
        if code is not None:
            tail = _tail_text(log, 20)
            err_console.print(f"[red]The runtime exited during startup (exit code {code}).[/red]")
            if tail:
                err_console.print(f"[dim]Last lines of {escape(str(log))}:[/dim]")
                err_console.print(escape(tail), highlight=False)
            raise typer.Exit(1)
        try:
            state = find_state(rname)
        except StateError as exc:
            _fail(str(exc))
        if state is not None and state.launch_id == launch_id and state.ready:
            break
        time.sleep(0.2)
    else:
        console.print(
            f"[yellow]Runtime {escape(rname)} (pid {proc.pid}) is still starting after "
            f"{_DETACH_READY_TIMEOUT:.0f}s; check 'promptise runtime status {escape(rname)}' "
            f"and the log {escape(str(log))}.[/yellow]"
        )
        return

    assert state is not None
    for line in (
        f"[green]Started runtime[/green] {escape(rname)} in the background",
        f"  PID:         {state.pid}",
        f"  Processes:   {escape(', '.join(state.processes) or '-')}",
        f"  Control API: {state.url}",
        f"  Log file:    {escape(str(log))}",
        f"  Status:      promptise runtime status {escape(rname)}",
        f"  Stop it:     promptise runtime stop {escape(rname)}",
    ):
        console.print(line, soft_wrap=True, highlight=False)


def _tail_text(path: Path, lines: int) -> str:
    try:
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            return "".join(deque(fh, maxlen=lines)).rstrip()
    except OSError:
        return ""


# ---------------------------------------------------------------------------
# runtime stop
# ---------------------------------------------------------------------------


@runtime_app.command()
def stop(
    name: Annotated[
        str | None,
        typer.Argument(
            help=(
                "Runtime to stop (or a process name in a running runtime); "
                "may be omitted when only one runtime is running"
            ),
        ),
    ] = None,
    process: _ProcessOption = None,
    force: Annotated[
        bool,
        typer.Option(
            "--force/--no-force",
            help="Kill the runtime if it hasn't stopped gracefully within --timeout",
        ),
    ] = False,
    timeout: Annotated[
        float,
        typer.Option("--timeout", min=0.0, help="Seconds to wait for a graceful shutdown"),
    ] = 30.0,
    url: _UrlOption = None,
    token: _TokenOption = None,
) -> None:
    """Stop a running runtime (every process, then the runtime exits) or one process.

    The runtime is asked over its control API; if that fails or it hasn't
    exited within --timeout, it gets SIGTERM; with --force it is killed
    after that.
    """
    targets, proc_name = _resolve_targets(name, process, url, token)
    target = targets[0]

    if proc_name is not None:
        result = _api(target, lambda c: c.stop_process(proc_name), process=proc_name)
        console.print(
            f"[green]Stopped process[/green] {escape(proc_name)} "
            f"({escape(str(result.get('state', 'stopped')))})"
        )
        return

    async def _shutdown(client: OrchestrationClient) -> dict[str, Any]:
        return await client._post(SHUTDOWN_PATH)

    from .api_client import OrchestrationAPIError

    requested = False
    try:
        asyncio.run(_call(target, _shutdown, timeout=10.0))
        requested = True
    except OrchestrationAPIError as exc:
        if exc.status_code == 404 and exc.code in (None, "NOT_FOUND"):
            # A plain OrchestrationAPI (not 'promptise runtime start'):
            # stop its processes; the server itself keeps running.
            _api(target, lambda c: c.stop_all())
            console.print(
                f"[green]Stopped every process[/green] of {escape(target.label)}. Its API "
                "server keeps running (it wasn't started with 'promptise runtime start')."
            )
            return
        if target.state is None:
            _fail(_api_error_message(target, exc))
        err_console.print(f"[yellow]{escape(_api_error_message(target, exc))}[/yellow]")
    except _Unreachable as exc:
        if target.state is None:
            _fail(str(exc))
        err_console.print(f"[yellow]{escape(str(exc))}[/yellow]")

    state = target.state
    if state is None:
        console.print(f"[green]Shutdown requested[/green] for {escape(target.label)}.")
        return

    if requested and wait_for_exit(state.pid, timeout, state):
        _stopped(state)
        return

    if pid_alive(state.pid) and terminate_process(state.pid):
        err_console.print(f"[yellow]Sending SIGTERM to pid {state.pid}...[/yellow]")
        if wait_for_exit(state.pid, timeout if not requested else _SIGTERM_GRACE, state):
            _stopped(state)
            return

    if not pid_alive(state.pid) or not state_recorded(state):
        _stopped(state)
        return
    if force:
        err_console.print(f"[yellow]Killing pid {state.pid}...[/yellow]")
        kill_process(state.pid)
        wait_for_exit(state.pid, 5.0)
        remove_state(state)
        if pid_alive(state.pid):
            _fail(f"Could not kill pid {state.pid}")
        console.print(f"[yellow]Killed runtime[/yellow] {escape(state.name)} (pid {state.pid})")
        return
    _fail(
        f"Runtime {state.name!r} (pid {state.pid}) did not stop within {timeout:g}s. "
        "Wait longer with --timeout, or kill it with --force."
    )


def _stopped(state: RuntimeState) -> None:
    remove_state(state)  # normally gone already; a crash can leave it
    console.print(f"[green]Stopped runtime[/green] {escape(state.name)} (pid {state.pid})")


# ---------------------------------------------------------------------------
# runtime status
# ---------------------------------------------------------------------------


@runtime_app.command()
def status(
    name: Annotated[
        str | None,
        typer.Argument(
            help=(
                "Runtime name, or a process name in a running runtime "
                "(omit for every runtime on this machine)"
            ),
        ),
    ] = None,
    process: _ProcessOption = None,
    as_json: Annotated[
        bool,
        typer.Option("--json", help="Output as JSON"),
    ] = False,
    url: _UrlOption = None,
    token: _TokenOption = None,
) -> None:
    """Show the processes of running runtime(s), or details of one process."""
    targets, proc_name = _resolve_targets(name, process, url, token, allow_many=True)
    results: list[dict[str, Any]] = []
    for target in targets:
        info: dict[str, Any] = {}
        if target.state is not None:
            info["runtime"] = _runtime_info(target.state)
        else:
            info["url"] = target.url
        if proc_name is not None:
            info["process"] = _api(target, lambda c: c.get_process(proc_name), process=proc_name)
        else:
            info["status"] = _api(target, lambda c: c.runtime_status())
        results.append(info)

    if as_json:
        # One runtime or process asked for: an object; otherwise a list of runtimes.
        single = name is not None or url is not None or process is not None
        _echo_json(results[0] if single else results)
        return
    for i, info in enumerate(results):
        if i:
            console.print()
        runtime_info = info.get("runtime")
        if runtime_info is not None:
            console.print(
                f"[bold]Runtime[/bold] [cyan]{escape(runtime_info['name'])}[/cyan]  "
                f"pid {runtime_info['pid']}  API {runtime_info['url']}  "
                f"started {escape(runtime_info['started_at'])}"
                + ("" if runtime_info["ready"] else "  [yellow](starting)[/yellow]")
            )
        else:
            console.print(f"[bold]Runtime[/bold] at {escape(info['url'])}")
        if "process" in info:
            _print_process_detail(info["process"])
        else:
            _print_status_table(info["status"])


def _runtime_info(state: RuntimeState) -> dict[str, Any]:
    """What ``status`` shows about a local runtime (never the token)."""
    return {
        "name": state.name,
        "pid": state.pid,
        "url": state.url,
        "started_at": state.started_at,
        "ready": state.ready,
        "manifests": state.manifests,
        "log_file": state.log_file,
    }


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
        typer.Option("--lines", "-n", min=1, help="Number of (most recent) entries to show"),
    ] = 20,
    follow: Annotated[
        bool,
        typer.Option("--follow/--no-follow", "-f", help="Keep printing new entries (Ctrl+C ends)"),
    ] = False,
    journal_path: Annotated[
        str | None,
        typer.Option(
            "--journal-path",
            help=(
                "Journal directory. Default: the process's journal path when it runs in a "
                "runtime on this machine, else .promptise/journal"
            ),
        ),
    ] = None,
    url: _UrlOption = None,
    token: _TokenOption = None,
) -> None:
    """Show journal entries for a process (the newest --lines; --follow for more)."""
    target: _Target | None = None
    file_dir: Path | None = None
    if url is not None:
        target = _resolve_targets(None, None, url, token)[0][0]
    elif journal_path is not None:
        file_dir = Path(journal_path)
    else:
        holders = [s for s in _local_states() if name in s.processes]
        if len(holders) > 1:
            _fail(
                f"Process {name!r} runs in several runtimes ("
                + ", ".join(s.name for s in holders)
                + "); pass --journal-path"
            )
        if holders:
            state = holders[0]
            journal = (state.processes.get(name) or {}).get("journal")
            if journal is None:
                _fail(
                    f"Process {name!r} (runtime {state.name!r}) has journaling off "
                    "(journal.level: none in its manifest)"
                )
            if journal.get("backend") == "file" and journal.get("path"):
                file_dir = Path(journal["path"])
            else:  # memory journal: only the running process has it
                target = _Target(state.url, state.token, state)
        else:
            file_dir = Path(".promptise/journal")

    if target is not None:
        _logs_from_api(target, name, lines, follow)
    else:
        assert file_dir is not None
        _logs_from_file(file_dir / f"{_journal_file_id(name)}.jsonl", name, lines, follow)


def _journal_file_id(name: str) -> str:
    """The journal file stem for *name* (as :class:`FileJournal` names it)."""
    if "\x00" in name:
        _fail("Null bytes are not allowed in a process name")
    return name.replace("/", "_").replace("\\", "_")[:200]


def _entry_row(entry: dict[str, Any]) -> tuple[str, str, str]:
    ts = entry.get("timestamp")
    time_str = str(ts)[:23] if ts is not None else "?"
    data_str = json.dumps(entry.get("data", {}), default=str)
    if len(data_str) > 80:
        data_str = data_str[:77] + "..."
    return time_str, str(entry.get("entry_type", "?")), data_str


def _print_entries(name: str, entries: list[dict[str, Any]]) -> None:
    table = Table(title=f"Journal: {name}", show_lines=False)
    table.add_column("Time", style="dim", width=23)
    table.add_column("Type", style="cyan", width=20)
    table.add_column("Data", style="white")
    for entry in entries:
        table.add_row(*(escape(v) for v in _entry_row(entry)))
    console.print(table)


def _print_entry_line(entry: dict[str, Any]) -> None:
    time_str, entry_type, data_str = _entry_row(entry)
    console.print(
        f"[dim]{escape(time_str)}[/dim]  [cyan]{escape(entry_type)}[/cyan]  {escape(data_str)}",
        highlight=False,
    )


def _parse_line(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line:
        return None
    try:
        entry = json.loads(line)
    except ValueError:
        return None
    return entry if isinstance(entry, dict) and "entry_type" in entry else None


def _logs_from_file(path: Path, name: str, lines: int, follow: bool) -> None:
    position = 0
    if path.is_file():
        try:
            with path.open("r", encoding="utf-8", errors="replace") as fh:
                parsed = (_parse_line(line) for line in fh)
                entries = list(deque((e for e in parsed if e is not None), maxlen=lines))
                position = fh.tell()
        except OSError as exc:
            _fail(f"Cannot read journal {path}: {exc}")
        if entries:
            _print_entries(name, entries)
        else:
            console.print(f"[dim]No journal entries for {name!r}[/dim]")
    else:
        console.print(
            f"[dim]No journal entries for {name!r}[/dim] [dim]({escape(str(path))})[/dim]"
        )
    if not follow:
        return

    console.print(f"[dim]Following {escape(str(path))} (Ctrl+C to stop)...[/dim]")
    buffer = ""
    try:
        while True:
            try:
                size = path.stat().st_size
            except OSError:
                size = 0
            if size < position:  # truncated or replaced
                position = 0
            if size > position:
                with path.open("r", encoding="utf-8", errors="replace") as fh:
                    fh.seek(position)
                    buffer += fh.read()
                    position = fh.tell()
                *complete, buffer = buffer.split("\n")
                for line in complete:
                    entry = _parse_line(line)
                    if entry is not None:
                        _print_entry_line(entry)
            time.sleep(0.25)
    except KeyboardInterrupt:
        return


def _logs_from_api(target: _Target, name: str, lines: int, follow: bool) -> None:
    limit = min(lines, 500)
    entries = _api(target, lambda c: c.get_journal(name, limit=limit), process=name)
    if entries:
        _print_entries(name, entries)
    else:
        console.print(f"[dim]No journal entries for {name!r}[/dim]")
    if not follow:
        return

    def _key(entry: dict[str, Any]) -> str:
        return json.dumps(entry, sort_keys=True, default=str)

    seen = {_key(e) for e in entries}
    console.print(
        f"[dim]Following {escape(name)} on {escape(target.url)} (Ctrl+C to stop)...[/dim]"
    )
    try:
        while True:
            time.sleep(1.0)
            batch = _api(target, lambda c: c.get_journal(name, limit=500), process=name)
            keys = [_key(e) for e in batch]
            for entry, key in zip(batch, keys, strict=True):
                if key not in seen:
                    _print_entry_line(entry)
            seen = set(keys)
    except KeyboardInterrupt:
        return


# ---------------------------------------------------------------------------
# runtime restart
# ---------------------------------------------------------------------------


@runtime_app.command()
def restart(
    name: Annotated[
        str | None,
        typer.Argument(
            help=(
                "Runtime to restart (or a process name in a running runtime); "
                "may be omitted when only one runtime is running"
            ),
        ),
    ] = None,
    process: _ProcessOption = None,
    url: _UrlOption = None,
    token: _TokenOption = None,
) -> None:
    """Restart every process of a running runtime, or one process.

    The runtime itself keeps running (same control API and token).  To load
    edited manifests, stop the runtime and start it again.
    """
    targets, proc_name = _resolve_targets(name, process, url, token)
    target = targets[0]
    if proc_name is not None:
        result = _api(target, lambda c: c.restart_process(proc_name), process=proc_name)
        console.print(
            f"[green]Restarted process[/green] {escape(proc_name)} "
            f"({escape(str(result.get('state', '?')))})"
        )
        return

    async def _restart_all(client: OrchestrationClient) -> dict[str, Any]:
        for entry in await client.list_processes():
            await client.restart_process(entry["name"])
        return await client.runtime_status()

    result = _api(target, _restart_all, timeout=120.0)
    console.print(f"[green]Restarted every process[/green] of {escape(target.label)}")
    _print_status_table(result)


# ---------------------------------------------------------------------------
# runtime validate
# ---------------------------------------------------------------------------


@runtime_app.command()
def validate(
    path: Annotated[
        str,
        typer.Argument(help="Path to a .agent manifest file, or a directory of them"),
    ],
) -> None:
    """Validate a .agent manifest file (or every .agent file in a directory)."""
    file_path = Path(path)

    if not file_path.exists():
        console.print(f"[red]Not found: {path}[/red]")
        raise typer.Exit(1)

    if file_path.is_dir():
        files = sorted(file_path.glob("*.agent"))
        if not files:
            console.print(f"[red]No .agent files in {escape(path)}[/red]")
            raise typer.Exit(1)
        failed = [f for f in files if not _validate_one(f)]
        if failed:
            console.print(
                f"[red]✗ {len(failed)} of {len(files)} manifest(s) failed:[/red] "
                + escape(", ".join(f.name for f in failed))
            )
            raise typer.Exit(1)
        console.print(f"[bold green]✓ All {len(files)} manifest(s) valid[/bold green]")
        return

    if not _validate_one(file_path):
        raise typer.Exit(1)


def _validate_one(file_path: Path) -> bool:
    """Validate one manifest and print the result; ``True`` when it is valid."""
    console.print(f"[bold]Validating {escape(str(file_path))}...[/bold]")

    try:
        warnings = validate_manifest(file_path)
        console.print("[green]✓[/green] Schema validation passed")

        if warnings:
            for w in warnings:
                console.print(f"[yellow]⚠ {escape(w)}[/yellow]")
        else:
            console.print("[green]✓[/green] No warnings")

        # Show parsed manifest summary
        manifest = load_manifest(file_path)
        table = Table(title="Manifest Summary", show_lines=False)
        table.add_column("Field", style="cyan")
        table.add_column("Value", style="white")

        table.add_row("Name", escape(manifest.name))
        table.add_row("Model", escape(manifest.model))
        table.add_row("Version", escape(manifest.version))
        table.add_row(
            "Instructions",
            escape(
                (manifest.instructions[:60] + "...")
                if manifest.instructions and len(manifest.instructions) > 60
                else (manifest.instructions or "(none)")
            ),
        )
        table.add_row("Servers", str(len(manifest.servers)))
        table.add_row("Triggers", str(len(manifest.triggers)))

        console.print(table)
        console.print("[bold green]✓ Validation complete![/bold green]")
        return True

    except ManifestValidationError as exc:
        console.print("[red]✗ Schema validation failed:[/red]")
        console.print(escape(str(exc)))
        return False
    except ManifestError as exc:
        console.print(f"[red]✗ Manifest error:[/red] {escape(str(exc))}")
        return False


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


def _print_status_table(status: dict[str, Any]) -> None:
    """Print a Rich status table (``AgentRuntime.status()`` shape)."""
    processes = status.get("processes", {})
    if not processes:
        console.print("[dim]No processes registered.[/dim]")
        return

    table = Table(title="Agent Processes", show_lines=False)
    table.add_column("Name", style="cyan", no_wrap=True)
    table.add_column("State", style="bold")
    table.add_column("ID", style="dim")
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
        state = str(info.get("state", "unknown"))
        color = state_colors.get(state, "white")
        state_display = f"[{color}]{escape(state.upper())}[/{color}]"

        uptime = info.get("uptime_seconds")
        uptime_str = _format_uptime(uptime) if isinstance(uptime, (int, float)) else "-"

        table.add_row(
            escape(str(name)),
            state_display,
            str(info.get("process_id") or "-")[:8],
            str(info.get("invocation_count", 0)),
            str(info.get("queue_size", 0)),
            uptime_str,
        )

    console.print(table)


def _print_process_detail(info: dict[str, Any]) -> None:
    """Print one process's status (from ``GET /processes/{name}``) as a table."""
    table = Table(title=f"Process: {info.get('name', '?')}", show_lines=False)
    table.add_column("Field", style="cyan", no_wrap=True)
    table.add_column("Value", style="white")
    for key, value in info.items():
        if isinstance(value, (dict, list)):
            text = json.dumps(value, default=str)
        elif key == "uptime_seconds" and isinstance(value, (int, float)):
            text = _format_uptime(value)
        else:
            text = "-" if value is None else str(value)
        table.add_row(escape(str(key)), escape(text))
    console.print(table)


def _format_uptime(uptime: float) -> str:
    hours = int(uptime // 3600)
    minutes = int((uptime % 3600) // 60)
    seconds = int(uptime % 60)
    return f"{hours}h {minutes}m {seconds}s"
