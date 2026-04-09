"""Live CLI monitoring dashboard for the Agent Runtime.

When enabled via ``promptise runtime start --dashboard``, replaces the
static status output with a full-screen, tabbed terminal dashboard
providing real-time visibility into every aspect of your running agent
processes.

**Tabs** (switch with arrow keys, 1-7, or Tab):

1. Overview     -- runtime summary, process states, global metrics
2. Processes    -- per-process details: state, invocations, queue, uptime
3. Triggers     -- all triggers with type, config, and fire status
4. Context      -- world state keys/values, writable keys, audit trail
5. Logs         -- journal entries from all processes
6. Events       -- trigger event log (received, processed, queued)
7. Commands     -- interactive command panel for process control

Requires ``rich>=13``, which is already a framework dependency.

Example::

    # Via CLI
    promptise runtime start agents/watcher.agent --dashboard

    # Via Python
    from promptise.runtime._dashboard import RuntimeDashboard, RuntimeDashboardState
    dashboard = RuntimeDashboard(state)
    dashboard.start()
"""

from __future__ import annotations

import datetime
import json
import logging
import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

if TYPE_CHECKING:
    from .runtime import AgentRuntime

logger = logging.getLogger(__name__)


# ------------------------------------------------------------------
# Logo — runtime variant of the Promptise P-mark
# ------------------------------------------------------------------

RUNTIME_LOGO = [
    "█████████████████████████████",
    "█████                  ██████",
    "█████                   █████",
    "█████                   █████",
    "█████                  ██████",
    "██████████████████████████",
    "█████",
    "█████      ░▒▓██████▓▒░",
    "█████         ░▒████▓░",
    " ████░           ░▒██▓",
    "  ░███▓░       ░▒██▓░",
    "     ░███▓░ ░▒▓██▓░",
    "        ░▒▓▓▓▓▒░",
]


# ------------------------------------------------------------------
# Tab definitions
# ------------------------------------------------------------------

TABS: list[tuple[str, str]] = [
    ("1", "Overview"),
    ("2", "Processes"),
    ("3", "Triggers"),
    ("4", "Context"),
    ("5", "Logs"),
    ("6", "Events"),
    ("7", "Commands"),
]

# ------------------------------------------------------------------
# Palette
# ------------------------------------------------------------------

ACCENT = "cyan"
BORDER = "dim"
HEADER_BORDER = "white"
ACTIVE_TAB = "bold black on white"
INACTIVE_TAB = "dim"


# ------------------------------------------------------------------
# State colors for process states
# ------------------------------------------------------------------

STATE_COLORS: dict[str, str] = {
    "running": "bold green",
    "starting": "yellow",
    "created": "dim",
    "stopped": "red",
    "failed": "bold red",
    "suspended": "yellow",
    "stopping": "yellow",
    "awaiting": "cyan",
}


# ------------------------------------------------------------------
# Data types
# ------------------------------------------------------------------


@dataclass
class InvocationLog:
    """Record of a single agent invocation."""

    timestamp: float
    process_name: str
    trigger_type: str
    trigger_id: str
    success: bool
    duration_ms: float
    error: str | None = None


@dataclass
class EventLog:
    """Record of a trigger event received."""

    timestamp: float
    process_name: str
    trigger_type: str
    trigger_id: str
    event_id: str
    payload_summary: str


@dataclass
class CommandResult:
    """Record of a command executed via the dashboard."""

    timestamp: float
    command: str
    result: str
    success: bool


@dataclass
class TriggerInfo:
    """Snapshot of a trigger's configuration and status."""

    process_name: str
    trigger_id: str
    trigger_type: str
    config_summary: str
    fire_count: int = 0
    last_fired: float | None = None


@dataclass
class ProcessSnapshot:
    """Snapshot of a process's status for the dashboard."""

    name: str
    process_id: str
    state: str
    model: str
    invocation_count: int
    consecutive_failures: int
    trigger_count: int
    queue_size: int
    uptime_seconds: float | None
    concurrency: int
    heartbeat_interval: float


@dataclass
class ContextSnapshot:
    """Snapshot of a process's AgentContext state."""

    process_name: str
    state: dict[str, Any]
    writable_keys: list[str] | None
    env_count: int
    file_mount_count: int
    has_memory: bool
    history_counts: dict[str, int]  # key -> number of writes


@dataclass
class RuntimeDashboardState:
    """Thread-safe shared state for the runtime dashboard.

    Mirrors the MCP dashboard's :class:`DashboardState` pattern but
    tracks runtime-specific data: processes, triggers,
    world state, and journal entries.
    """

    # Runtime info
    runtime_name: str = "Agent Runtime"
    started_at: float = field(default_factory=time.time)

    # Process snapshots (refreshed on each render)
    processes: dict[str, ProcessSnapshot] = field(default_factory=dict)

    # Trigger info
    triggers: list[TriggerInfo] = field(default_factory=list)

    # Context snapshots
    contexts: dict[str, ContextSnapshot] = field(default_factory=dict)

    # Invocation log (ring buffer)
    invocations: deque[InvocationLog] = field(default_factory=lambda: deque(maxlen=200))

    # Trigger event log (ring buffer)
    events: deque[EventLog] = field(default_factory=lambda: deque(maxlen=200))

    # Command history
    commands: deque[CommandResult] = field(default_factory=lambda: deque(maxlen=100))

    # Journal entries (ring buffer)
    journal_entries: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=500))

    # Global counters
    total_invocations: int = 0
    total_errors: int = 0
    total_events: int = 0

    # Manifests loaded
    manifest_names: list[str] = field(default_factory=list)

    def record_invocation(self, log: InvocationLog) -> None:
        """Record a completed agent invocation."""
        self.invocations.append(log)
        self.total_invocations += 1
        if not log.success:
            self.total_errors += 1

    def record_event(self, event: EventLog) -> None:
        """Record a trigger event received."""
        self.events.append(event)
        self.total_events += 1

    def record_command(self, result: CommandResult) -> None:
        """Record a command executed."""
        self.commands.append(result)

    def record_journal(self, entry: dict[str, Any]) -> None:
        """Record a journal entry."""
        self.journal_entries.append(entry)


# ------------------------------------------------------------------
# Log capture handler
# ------------------------------------------------------------------


class _LogCapture(logging.Handler):
    """Captures log records for the Raw Logs section."""

    def __init__(self, maxlen: int = 500) -> None:
        super().__init__()
        self.records: deque[logging.LogRecord] = deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


# ------------------------------------------------------------------
# Runtime data collector
# ------------------------------------------------------------------


class RuntimeDataCollector:
    """Collects real-time data from a running AgentRuntime into DashboardState.

    Runs as a background task, periodically polling the runtime for
    process status, context state, and trigger info.
    """

    def __init__(
        self,
        runtime: AgentRuntime,
        state: RuntimeDashboardState,
        interval: float = 0.5,
    ) -> None:
        self._runtime = runtime
        self._state = state
        self._interval = interval
        self._running = False
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """Start the data collection thread."""
        self._running = True
        self._thread = threading.Thread(target=self._collect_loop, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """Stop collecting."""
        self._running = False

    def _collect_loop(self) -> None:
        """Background thread that polls the runtime."""
        while self._running:
            try:
                self._collect_snapshot()
            except Exception:
                logger.debug("Dashboard snapshot collection error", exc_info=True)
            time.sleep(self._interval)

    def _collect_snapshot(self) -> None:
        """Gather a full snapshot from the runtime."""

        for name, process in self._runtime.processes.items():
            # Process snapshot
            status = process.status()
            config = process.config

            self._state.processes[name] = ProcessSnapshot(
                name=name,
                process_id=status.get("process_id", ""),
                state=status.get("state", "unknown"),
                model=config.model,
                invocation_count=status.get("invocation_count", 0),
                consecutive_failures=status.get("consecutive_failures", 0),
                trigger_count=status.get("trigger_count", 0),
                queue_size=status.get("queue_size", 0),
                uptime_seconds=status.get("uptime_seconds"),
                concurrency=config.concurrency,
                heartbeat_interval=config.heartbeat_interval,
            )

            # Context snapshot
            ctx = process.context
            self._state.contexts[name] = ContextSnapshot(
                process_name=name,
                state=ctx.state_snapshot(),
                writable_keys=(sorted(ctx._writable_keys) if ctx._writable_keys else None),
                env_count=len(ctx.env),
                file_mount_count=len(ctx.files),
                has_memory=ctx.memory is not None,
                history_counts={k: len(v) for k, v in ctx._history.items()},
            )

        # Collect trigger info
        triggers: list[TriggerInfo] = []
        for name, process in self._runtime.processes.items():
            for tc in process.config.triggers:
                config_parts: list[str] = []
                if tc.type == "cron" and tc.cron_expression:
                    config_parts.append(f"cron={tc.cron_expression}")
                elif tc.type == "webhook":
                    config_parts.append(f"port={tc.webhook_port} path={tc.webhook_path}")
                elif tc.type == "file_watch" and tc.watch_path:
                    patterns = ", ".join(tc.watch_patterns[:3])
                    config_parts.append(f"path={tc.watch_path} [{patterns}]")
                elif tc.type == "event" and tc.event_type:
                    config_parts.append(f"event={tc.event_type}")
                elif tc.type == "message" and tc.topic:
                    config_parts.append(f"topic={tc.topic}")

                triggers.append(
                    TriggerInfo(
                        process_name=name,
                        trigger_id=f"{name}/{tc.type}",
                        trigger_type=tc.type,
                        config_summary=" ".join(config_parts) or "-",
                    )
                )
        self._state.triggers = triggers

        # Remove stale processes
        runtime_names = set(self._runtime.processes.keys())
        for stale in list(self._state.processes.keys()):
            if stale not in runtime_names:
                del self._state.processes[stale]


# ------------------------------------------------------------------
# Dashboard renderer
# ------------------------------------------------------------------


class RuntimeDashboard:
    """Full-screen tabbed terminal dashboard for the Agent Runtime.

    Follows the same architecture as the MCP server's
    :class:`~promptise.mcp.server._dashboard.Dashboard`:

    - **7 tabs** -- Overview, Processes, Triggers, Context,
      Logs, Events, Commands
    - **Arrow-key / number-key navigation**
    - **Live refresh** -- ~250ms in a daemon thread
    - **Log capture** -- Python logger output in Logs tab
    - **Command input** -- interactive process control

    Args:
        state: :class:`RuntimeDashboardState` instance (shared with
            data collector).
        runtime: Optional :class:`AgentRuntime` for command execution.
    """

    def __init__(
        self,
        state: RuntimeDashboardState,
        runtime: AgentRuntime | None = None,
    ) -> None:
        self._state = state
        self._runtime = runtime
        self._console = Console()
        self._live: Live | None = None
        self._refresh_thread: threading.Thread | None = None
        self._key_thread: threading.Thread | None = None
        self._running = False
        self._current_tab = 0
        self._log_capture = _LogCapture(maxlen=500)
        self._muted_handlers: list[tuple[logging.Handler, int]] = []
        self._orig_root_level: int = logging.WARNING

        # Command input state
        self._command_buffer: str = ""
        self._command_mode = False

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the live dashboard (takes over the terminal)."""
        self._running = True
        self._suppress_loggers()

        self._live = Live(
            self._render(),
            console=self._console,
            refresh_per_second=4,
            screen=True,
        )
        self._live.start()

        self._refresh_thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self._refresh_thread.start()

        self._key_thread = threading.Thread(target=self._key_reader, daemon=True)
        self._key_thread.start()

    def stop(self) -> None:
        """Stop the dashboard and restore the terminal."""
        self._running = False
        if self._live:
            try:
                self._live.stop()
            except Exception:
                logger.debug("Error stopping Rich Live display", exc_info=True)
        self._restore_loggers()

    # ------------------------------------------------------------------
    # Logger suppression
    # ------------------------------------------------------------------

    def _suppress_loggers(self) -> None:
        """Redirect log output to internal capture buffer."""
        root = logging.getLogger()
        self._orig_root_level = root.level
        if root.level > logging.DEBUG:
            root.setLevel(logging.DEBUG)
        self._log_capture.setLevel(logging.DEBUG)
        root.addHandler(self._log_capture)

        logger_names = [
            "",
            "promptise.runtime",
            "promptise.runtime.process",
            "promptise.runtime.triggers",
            "promptise.server",
            "uvicorn",
            "uvicorn.access",
            "uvicorn.error",
            "httpx",
            "httpcore",
            "asyncio",
        ]
        for name in logger_names:
            lgr = logging.getLogger(name) if name else root
            for handler in lgr.handlers:
                if handler is not self._log_capture and isinstance(handler, logging.StreamHandler):
                    self._muted_handlers.append((handler, handler.level))
                    handler.setLevel(logging.CRITICAL + 1)

    def _restore_loggers(self) -> None:
        """Restore original logger levels."""
        for handler, level in self._muted_handlers:
            handler.setLevel(level)
        self._muted_handlers.clear()

        root = logging.getLogger()
        root.removeHandler(self._log_capture)
        root.setLevel(self._orig_root_level)

    # ------------------------------------------------------------------
    # Background threads
    # ------------------------------------------------------------------

    def _refresh_loop(self) -> None:
        """Background thread pushing updated renders to Live."""
        while self._running:
            try:
                if self._live:
                    self._live.update(self._render())
            except Exception:
                logger.debug("Dashboard render refresh error", exc_info=True)
            time.sleep(0.25)

    def _key_reader(self) -> None:
        """Background thread reading keyboard input."""
        try:
            if sys.platform == "win32":
                self._key_reader_windows()
            else:
                self._key_reader_unix()
        except Exception:
            logger.debug("Key reader thread error", exc_info=True)

    def _key_reader_unix(self) -> None:
        """Unix keyboard reader using termios + os.read."""
        import select
        import termios
        import tty

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while self._running:
                if not select.select([fd], [], [], 0.05)[0]:
                    continue

                ch = os.read(fd, 1)

                # Command mode input
                if self._command_mode:
                    if ch == b"\n" or ch == b"\r":
                        # Execute command
                        cmd = self._command_buffer.strip()
                        if cmd:
                            self._execute_command(cmd)
                        self._command_buffer = ""
                        self._command_mode = False
                    elif ch == b"\x1b":
                        # Escape — cancel command mode
                        self._command_buffer = ""
                        self._command_mode = False
                    elif ch == b"\x7f" or ch == b"\x08":
                        # Backspace
                        self._command_buffer = self._command_buffer[:-1]
                    elif ch.isascii() and len(ch) == 1:
                        self._command_buffer += ch.decode("utf-8", errors="replace")
                    continue

                if ch == b"\x1b":
                    buf = b""
                    for _ in range(5):
                        if select.select([fd], [], [], 0.02)[0]:
                            buf += os.read(fd, 1)
                        else:
                            break

                    if buf == b"[C":  # Right arrow
                        self._next_tab()
                    elif buf == b"[D":  # Left arrow
                        self._prev_tab()

                elif ch == b"\t":  # Tab key
                    self._next_tab()
                elif ch in (
                    b"1",
                    b"2",
                    b"3",
                    b"4",
                    b"5",
                    b"6",
                    b"7",
                ):
                    self._current_tab = int(ch) - 1
                elif ch == b":" or ch == b"/":
                    # Enter command mode
                    self._command_mode = True
                    self._command_buffer = ""
                    self._current_tab = 6  # Switch to Commands tab
        except Exception:
            logger.debug("Unix key reader error", exc_info=True)
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            except Exception:
                logger.debug("Error restoring terminal settings", exc_info=True)

    def _key_reader_windows(self) -> None:
        """Windows keyboard reader using msvcrt."""
        try:
            import msvcrt
        except ImportError:
            return

        while self._running:
            if msvcrt.kbhit():
                ch = msvcrt.getch()

                if self._command_mode:
                    if ch == b"\r":
                        cmd = self._command_buffer.strip()
                        if cmd:
                            self._execute_command(cmd)
                        self._command_buffer = ""
                        self._command_mode = False
                    elif ch == b"\x1b":
                        self._command_buffer = ""
                        self._command_mode = False
                    elif ch == b"\x08":
                        self._command_buffer = self._command_buffer[:-1]
                    elif ch.isascii():
                        self._command_buffer += ch.decode("utf-8", errors="replace")
                    continue

                if ch in (b"\xe0", b"\x00"):
                    ch2 = msvcrt.getch()
                    if ch2 == b"M":  # Right arrow
                        self._next_tab()
                    elif ch2 == b"K":  # Left arrow
                        self._prev_tab()
                elif ch == b"\t":
                    self._next_tab()
                elif ch in (
                    b"1",
                    b"2",
                    b"3",
                    b"4",
                    b"5",
                    b"6",
                    b"7",
                ):
                    self._current_tab = int(ch) - 1
                elif ch in (b":", b"/"):
                    self._command_mode = True
                    self._command_buffer = ""
                    self._current_tab = 6
            else:
                time.sleep(0.03)

    def _next_tab(self) -> None:
        """Move to the next tab (wraps around)."""
        self._current_tab = (self._current_tab + 1) % len(TABS)

    def _prev_tab(self) -> None:
        """Move to the previous tab (wraps around)."""
        self._current_tab = (self._current_tab - 1) % len(TABS)

    # ------------------------------------------------------------------
    # Command execution
    # ------------------------------------------------------------------

    def _execute_command(self, command: str) -> None:
        """Execute a command string from the dashboard."""
        import asyncio as _asyncio

        parts = command.strip().split()
        if not parts:
            return

        cmd = parts[0].lower()
        args = parts[1:]
        result_msg = ""
        success = True

        try:
            if cmd == "help":
                result_msg = (
                    "Commands: status, start <name>, stop <name>, "
                    "restart <name>, suspend <name>, resume <name>, "
                    "inject <name> <payload>, list, help"
                )
            elif cmd == "list":
                names = list(self._state.processes.keys())
                result_msg = f"Processes: {', '.join(names) if names else '(none)'}"
            elif cmd == "status":
                if args:
                    proc = self._state.processes.get(args[0])
                    if proc:
                        result_msg = (
                            f"{proc.name}: state={proc.state}, "
                            f"invocations={proc.invocation_count}, "
                            f"queue={proc.queue_size}"
                        )
                    else:
                        result_msg = f"Process {args[0]!r} not found"
                        success = False
                else:
                    count = len(self._state.processes)
                    running = sum(1 for p in self._state.processes.values() if p.state == "running")
                    result_msg = f"{count} processes ({running} running)"
            elif cmd in ("start", "stop", "restart", "suspend", "resume"):
                if not args:
                    result_msg = f"Usage: {cmd} <process_name>"
                    success = False
                elif self._runtime is None:
                    result_msg = "No runtime reference — command not available"
                    success = False
                else:
                    name = args[0]
                    try:
                        proc = self._runtime.get_process(name)
                        if cmd == "start":
                            _asyncio.run_coroutine_threadsafe(
                                proc.start(),
                                _get_event_loop(),
                            )
                            result_msg = f"Starting {name}..."
                        elif cmd == "stop":
                            _asyncio.run_coroutine_threadsafe(
                                proc.stop(),
                                _get_event_loop(),
                            )
                            result_msg = f"Stopping {name}..."
                        elif cmd == "restart":
                            _asyncio.run_coroutine_threadsafe(
                                self._runtime.restart_process(name),
                                _get_event_loop(),
                            )
                            result_msg = f"Restarting {name}..."
                        elif cmd == "suspend":
                            _asyncio.run_coroutine_threadsafe(
                                proc.suspend(),
                                _get_event_loop(),
                            )
                            result_msg = f"Suspending {name}..."
                        elif cmd == "resume":
                            _asyncio.run_coroutine_threadsafe(
                                proc.resume(),
                                _get_event_loop(),
                            )
                            result_msg = f"Resuming {name}..."
                    except KeyError:
                        result_msg = f"Process {name!r} not found"
                        success = False
            elif cmd == "inject":
                if len(args) < 1:
                    result_msg = "Usage: inject <name> [payload_json]"
                    success = False
                elif self._runtime is None:
                    result_msg = "No runtime reference — command not available"
                    success = False
                else:
                    name = args[0]
                    payload_str = " ".join(args[1:]) if len(args) > 1 else "{}"
                    try:
                        payload = json.loads(payload_str)
                    except json.JSONDecodeError:
                        payload = {"raw": payload_str}

                    try:
                        from .triggers.base import TriggerEvent

                        proc = self._runtime.get_process(name)
                        event = TriggerEvent(
                            trigger_id="dashboard",
                            trigger_type="manual",
                            payload=payload,
                        )
                        _asyncio.run_coroutine_threadsafe(
                            proc.inject(event),
                            _get_event_loop(),
                        )
                        result_msg = f"Injected event into {name}"
                    except KeyError:
                        result_msg = f"Process {name!r} not found"
                        success = False
            else:
                result_msg = f"Unknown command: {cmd!r}. Type 'help' for commands."
                success = False
        except Exception as exc:
            result_msg = f"Error: {exc}"
            success = False

        self._state.record_command(
            CommandResult(
                timestamp=time.time(),
                command=command,
                result=result_msg,
                success=success,
            )
        )

    # ==================================================================
    # Layout
    # ==================================================================

    def _render(self) -> Layout:
        """Build the full dashboard layout."""
        layout = Layout()
        layout.split_column(
            Layout(name="header", size=15),
            Layout(name="nav", size=1),
            Layout(name="body", ratio=1),
            Layout(name="footer", size=1),
        )

        layout["header"].update(self._header())
        layout["nav"].update(self._nav_bar())

        tab_renderers = [
            self._tab_overview,
            self._tab_processes,
            self._tab_triggers,
            self._tab_context,
            self._tab_logs,
            self._tab_events,
            self._tab_commands,
        ]
        layout["body"].update(tab_renderers[self._current_tab]())

        footer_text = "  \u2190 \u2192 Navigate    1-7 Jump    Tab Next    : Command    Ctrl+C Quit"
        if self._command_mode:
            footer_text = f"  > {self._command_buffer}\u2588    (Enter to run, Esc to cancel)"

        layout["footer"].update(Text(footer_text, style="dim"))

        return layout

    # ==================================================================
    # Header & Navigation
    # ==================================================================

    def _header(self) -> Panel:
        """Two-column header: logo left, runtime metadata right."""
        s = self._state
        uptime = _format_duration(time.time() - s.started_at)

        # Left column: logo
        logo_text = Text()
        for line in RUNTIME_LOGO:
            logo_text.append(line + "\n", style="bold white")

        # Right column: metadata
        process_count = len(s.processes)
        running = sum(1 for p in s.processes.values() if p.state == "running")
        trigger_count = len(s.triggers)

        info = Text()
        info.append("\n")
        info.append("P R O M P T I S E\n", style="bold white")
        info.append("Agent Runtime\n\n", style="dim")

        info.append("Runtime    ", style="dim")
        info.append(s.runtime_name, style="bold white")
        info.append("\n")

        info.append("Uptime     ", style="dim")
        info.append(f"{uptime}\n\n", style="white")

        info.append("Processes  ", style="dim")
        info.append(f"{process_count}", style="bold white")
        info.append(f"  ({running} running)\n", style="dim")

        info.append("Triggers   ", style="dim")
        info.append(f"{trigger_count}", style="bold white")
        info.append(" configured\n", style="dim")

        info.append("Events     ", style="dim")
        info.append(f"{s.total_invocations}", style="bold white")
        if s.total_errors:
            info.append(f"  ({s.total_errors} errors)", style="red")
        info.append("\n\n")

        info.append("promptise.com", style="dim underline")
        info.append("  \u00b7  ", style="dim")
        info.append("promptise.com/docs", style="dim underline")

        # Two-column grid
        grid = Table.grid(expand=True, padding=(0, 2))
        grid.add_column(width=32)
        grid.add_column(ratio=1)
        grid.add_row(logo_text, info)

        return Panel(grid, border_style=HEADER_BORDER, padding=(0, 1))

    def _nav_bar(self) -> Text:
        """Flat tab navigation bar."""
        nav = Text()
        nav.append("  ")
        for i, (num, label) in enumerate(TABS):
            if i == self._current_tab:
                nav.append(f" {num} {label} ", style=ACTIVE_TAB)
            else:
                nav.append(f" {num} {label} ", style=INACTIVE_TAB)
            nav.append(" ")
        return nav

    # ==================================================================
    # Tab 1: Overview
    # ==================================================================

    def _tab_overview(self) -> Panel:
        """Overview tab — runtime summary and global metrics."""
        s = self._state

        process_count = len(s.processes)
        running = sum(1 for p in s.processes.values() if p.state == "running")
        _stopped = sum(1 for p in s.processes.values() if p.state in ("stopped", "created"))
        failed = sum(1 for p in s.processes.values() if p.state == "failed")
        _suspended = sum(1 for p in s.processes.values() if p.state == "suspended")
        uptime_secs = max(time.time() - s.started_at, 1)
        ips = s.total_invocations / uptime_secs  # invocations per second

        # Stats row
        stats = Table.grid(expand=True, padding=(0, 3))
        for _ in range(4):
            stats.add_column(ratio=1)

        stats.add_row(
            _kv("Processes", str(process_count), ACCENT),
            _kv("Running", str(running), "green" if running else "dim"),
            _kv("Invocations", str(s.total_invocations), "white"),
            _kv("Throughput", f"{ips:.2f}/s", "white"),
        )
        stats.add_row(
            _kv("Triggers", str(len(s.triggers)), "white"),
            _kv("Failed", str(failed), "red" if failed else "dim"),
            _kv("Errors", str(s.total_errors), "red" if s.total_errors else "dim"),
            _kv("Uptime", _format_duration(uptime_secs), "white"),
        )

        # Process summary table
        proc_table = Table(
            show_header=True,
            header_style="bold dim",
            expand=True,
            show_edge=False,
            padding=(0, 1),
        )
        proc_table.add_column("Process", style="white", no_wrap=True, ratio=3)
        proc_table.add_column("State", justify="center", width=12)
        proc_table.add_column("Invocations", justify="right", width=12)
        proc_table.add_column("Queue", justify="right", width=7)
        proc_table.add_column("Uptime", style="dim", width=12)

        for proc in sorted(s.processes.values(), key=lambda p: p.name):
            state_color = STATE_COLORS.get(proc.state, "white")
            state_text = Text(proc.state.upper(), style=state_color)
            uptime_str = _format_duration(proc.uptime_seconds) if proc.uptime_seconds else "-"
            proc_table.add_row(
                proc.name,
                state_text,
                str(proc.invocation_count),
                str(proc.queue_size),
                uptime_str,
            )

        if not s.processes:
            proc_table.add_row(
                Text("No processes registered", style="dim italic"),
                "",
                "",
                "",
                "",
            )

        # Recent activity
        recent = list(s.invocations)
        last_5 = list(reversed(recent[-5:])) if recent else []

        activity = Table(
            show_header=True,
            header_style="bold dim",
            expand=True,
            show_edge=False,
            padding=(0, 1),
        )
        activity.add_column("Time", style="dim", width=9)
        activity.add_column("Process", ratio=2)
        activity.add_column("Trigger", width=10)
        activity.add_column("Status", justify="center", width=8)
        activity.add_column("Duration", justify="right", width=10)

        for log in last_5:
            ts = datetime.datetime.fromtimestamp(log.timestamp).strftime("%H:%M:%S")
            status = Text("OK", style="green") if log.success else Text("FAIL", style="red")
            dur = _fmt_duration_ms(log.duration_ms)
            activity.add_row(ts, log.process_name, log.trigger_type, status, dur)

        if not last_5:
            activity.add_row(
                "",
                Text("Waiting for invocations...", style="dim italic"),
                "",
                "",
                "",
            )

        # Compose
        body = Table.grid(expand=True)
        body.add_column(ratio=1)
        body.add_row(Panel(stats, title="[dim]Metrics[/]", border_style=BORDER, padding=(0, 1)))
        body.add_row(
            Panel(proc_table, title="[dim]Processes[/]", border_style=BORDER, padding=(0, 1))
        )
        body.add_row(
            Panel(activity, title="[dim]Recent Activity[/]", border_style=BORDER, padding=(0, 1))
        )

        return Panel(body, title=f"[bold {ACCENT}]Overview[/]", border_style=BORDER, padding=(0, 0))

    # ==================================================================
    # Tab 2: Processes
    # ==================================================================

    def _tab_processes(self) -> Panel:
        """Processes tab — detailed per-process information."""
        s = self._state

        if not s.processes:
            empty = Table.grid(expand=True)
            empty.add_column(justify="center")
            empty.add_row("")
            empty.add_row(Text("No processes registered", style="dim"))
            empty.add_row("")
            empty.add_row(Text("Load a manifest to get started:", style="dim italic"))
            empty.add_row(Text("  promptise runtime start agents/my.agent", style="dim"))
            return Panel(
                empty,
                title=f"[bold {ACCENT}]Processes[/]",
                border_style=BORDER,
                padding=(1, 2),
            )

        table = Table(
            show_header=True,
            header_style="bold dim",
            expand=True,
            show_edge=False,
            padding=(0, 1),
        )
        table.add_column("Name", style="white", no_wrap=True, ratio=3)
        table.add_column("State", justify="center", width=12)
        table.add_column("PID", style="dim", width=10)
        table.add_column("Model", style="dim", ratio=2)
        table.add_column("Invocations", justify="right", width=12)
        table.add_column("Failures", justify="right", width=9)
        table.add_column("Triggers", justify="right", width=9)
        table.add_column("Queue", justify="right", width=7)
        table.add_column("Conc.", justify="right", width=6)
        table.add_column("Heartbeat", justify="right", width=10)
        table.add_column("Uptime", style="dim", width=12)

        for proc in sorted(s.processes.values(), key=lambda p: p.name):
            state_color = STATE_COLORS.get(proc.state, "white")
            state_text = Text(proc.state.upper(), style=state_color)
            uptime_str = _format_duration(proc.uptime_seconds) if proc.uptime_seconds else "-"
            failures_text = Text(
                str(proc.consecutive_failures),
                style="red" if proc.consecutive_failures > 0 else "dim",
            )

            table.add_row(
                proc.name,
                state_text,
                proc.process_id[:8],
                proc.model,
                str(proc.invocation_count),
                failures_text,
                str(proc.trigger_count),
                str(proc.queue_size),
                str(proc.concurrency),
                f"{proc.heartbeat_interval:.0f}s",
                uptime_str,
            )

        summary = Text(
            f"  {len(s.processes)} processes    Use ':help' for process commands",
            style="dim",
        )

        body = Table.grid(expand=True)
        body.add_column(ratio=1)
        body.add_row(table)
        body.add_row(Text(""))
        body.add_row(summary)

        return Panel(
            body,
            title=f"[bold {ACCENT}]Processes[/]",
            border_style=BORDER,
            padding=(0, 1),
        )

    # ==================================================================
    # Tab 3: Triggers
    # ==================================================================

    def _tab_triggers(self) -> Panel:
        """Triggers tab — all triggers across all processes."""
        s = self._state

        table = Table(
            show_header=True,
            header_style="bold dim",
            expand=True,
            show_edge=False,
            padding=(0, 1),
        )
        table.add_column("Process", style="white", no_wrap=True, ratio=2)
        table.add_column("Type", style=ACCENT, width=12)
        table.add_column("Configuration", ratio=4)
        table.add_column("Fires", justify="right", width=7)
        table.add_column("Last Fired", style="dim", width=16)

        type_icons = {
            "cron": "\u23f0",  # alarm clock
            "webhook": "\U0001f310",  # globe
            "file_watch": "\U0001f4c2",  # folder
            "event": "\u26a1",  # lightning
            "message": "\u2709",  # envelope
        }

        for trigger in s.triggers:
            icon = type_icons.get(trigger.trigger_type, "\u2022")
            type_text = Text(f"{icon} {trigger.trigger_type}")
            last_fired = "-"
            if trigger.last_fired:
                last_fired = datetime.datetime.fromtimestamp(trigger.last_fired).strftime(
                    "%H:%M:%S"
                )

            table.add_row(
                trigger.process_name,
                type_text,
                Text(trigger.config_summary, style="dim"),
                str(trigger.fire_count),
                last_fired,
            )

        if not s.triggers:
            table.add_row(
                Text("No triggers configured", style="dim italic"),
                "",
                "",
                "",
                "",
            )

        # Trigger type summary
        type_counts: dict[str, int] = {}
        for t in s.triggers:
            type_counts[t.trigger_type] = type_counts.get(t.trigger_type, 0) + 1

        summary_parts = [f"{v} {k}" for k, v in sorted(type_counts.items())]
        summary = Text(
            f"  {len(s.triggers)} triggers    "
            + ("    ".join(summary_parts) if summary_parts else ""),
            style="dim",
        )

        body = Table.grid(expand=True)
        body.add_column(ratio=1)
        body.add_row(table)
        body.add_row(Text(""))
        body.add_row(summary)

        return Panel(
            body,
            title=f"[bold {ACCENT}]Triggers[/]",
            border_style=BORDER,
            padding=(0, 1),
        )

    # ==================================================================
    # Tab 4: Context
    # ==================================================================

    def _tab_context(self) -> Panel:
        """Context tab — world state and memory per process."""
        s = self._state

        if not s.contexts:
            empty = Table.grid(expand=True)
            empty.add_column(justify="center")
            empty.add_row("")
            empty.add_row(Text("No context data available", style="dim"))
            return Panel(
                empty,
                title=f"[bold {ACCENT}]Context[/]",
                border_style=BORDER,
                padding=(1, 2),
            )

        panels: list[Any] = []
        for name, ctx in sorted(s.contexts.items()):
            # State table
            state_table = Table(
                show_header=True,
                header_style="bold dim",
                expand=True,
                show_edge=False,
                padding=(0, 1),
            )
            state_table.add_column("Key", style="white", no_wrap=True, ratio=2)
            state_table.add_column("Value", ratio=3)
            state_table.add_column("Writable", justify="center", width=10)
            state_table.add_column("Writes", justify="right", width=8)

            for key, value in sorted(ctx.state.items()):
                val_str = str(value)
                if len(val_str) > 50:
                    val_str = val_str[:47] + "..."

                is_writable = ctx.writable_keys is None or key in ctx.writable_keys
                writable_text = Text(
                    "yes" if is_writable else "no",
                    style="green" if is_writable else "dim",
                )
                write_count = ctx.history_counts.get(key, 0)

                state_table.add_row(
                    key,
                    Text(val_str, style="dim"),
                    writable_text,
                    str(write_count),
                )

            if not ctx.state:
                state_table.add_row(
                    Text("(empty state)", style="dim italic"),
                    "",
                    "",
                    "",
                )

            # Context metadata line
            meta_parts: list[str] = []
            if ctx.env_count > 0:
                meta_parts.append(f"{ctx.env_count} env vars")
            if ctx.file_mount_count > 0:
                meta_parts.append(f"{ctx.file_mount_count} file mounts")
            if ctx.has_memory:
                meta_parts.append("memory: yes")
            meta_str = "  ".join(meta_parts) if meta_parts else "no extra config"

            proc_body = Table.grid(expand=True)
            proc_body.add_column(ratio=1)
            proc_body.add_row(state_table)
            proc_body.add_row(Text(f"  {meta_str}", style="dim"))

            panels.append(
                Panel(
                    proc_body,
                    title=f"[dim]{name}[/]",
                    border_style=BORDER,
                    padding=(0, 1),
                )
            )

        body = Table.grid(expand=True)
        body.add_column(ratio=1)
        for panel in panels:
            body.add_row(panel)

        return Panel(
            body,
            title=f"[bold {ACCENT}]Context[/]",
            border_style=BORDER,
            padding=(0, 0),
        )

    # ==================================================================
    # Tab 5: Logs
    # ==================================================================

    def _tab_logs(self) -> Panel:
        """Logs tab — Python logger output from runtime components."""
        records = list(self._log_capture.records)
        last_n = list(reversed(records[-60:]))

        lines = Text()
        for rec in last_n:
            ts = datetime.datetime.fromtimestamp(rec.created).strftime("%H:%M:%S.%f")[:-3]
            level = rec.levelname
            name = rec.name
            msg = rec.getMessage()

            if len(name) > 28:
                name = name[:25] + "..."
            if len(msg) > 200:
                msg = msg[:197] + "..."

            if rec.levelno >= logging.ERROR:
                lvl_style = "bold red"
                msg_style = "red"
            elif rec.levelno >= logging.WARNING:
                lvl_style = "bold yellow"
                msg_style = "yellow"
            elif rec.levelno >= logging.INFO:
                lvl_style = "bold white"
                msg_style = "white"
            else:
                lvl_style = "dim"
                msg_style = "dim"

            lines.append(f"{ts} ", style="dim")
            lines.append(f"{level:<8}", style=lvl_style)
            lines.append(f" {name:<28} ", style="dim")
            lines.append(f"{msg}\n", style=msg_style)

        if not last_n:
            lines.append("\n", style="dim")
            lines.append("  No log output captured yet.\n\n", style="dim italic")
            lines.append("  Logs from runtime components, triggers,\n", style="dim")
            lines.append("  and agent processes will appear here.\n\n", style="dim")
            lines.append("  Start a process to see output.\n", style="dim")

        # Journal entries section
        journal_lines = Text()
        journal_entries = list(self._state.journal_entries)
        last_journals = list(reversed(journal_entries[-20:]))

        for entry in last_journals:
            ts_val = entry.get("timestamp", "")
            if hasattr(ts_val, "isoformat"):
                ts_str = ts_val.isoformat()[:19]
            elif isinstance(ts_val, str):
                ts_str = ts_val[:19]
            elif isinstance(ts_val, (int, float)):
                ts_str = datetime.datetime.fromtimestamp(ts_val).strftime("%H:%M:%S")
            else:
                ts_str = "?"

            entry_type = entry.get("entry_type", "?")
            proc_id = entry.get("process_id", "?")
            data = entry.get("data", {})
            data_str = json.dumps(data, default=str)
            if len(data_str) > 80:
                data_str = data_str[:77] + "..."

            journal_lines.append(f"{ts_str} ", style="dim")
            journal_lines.append(f"{entry_type:<20}", style="cyan")
            journal_lines.append(f" {proc_id:<16} ", style="dim")
            journal_lines.append(f"{data_str}\n", style="white")

        if not last_journals:
            journal_lines.append("  No journal entries yet.\n", style="dim italic")

        count_text = Text(
            f"  {len(records)} log records    {len(journal_entries)} journal entries",
            style="dim",
        )

        body = Table.grid(expand=True)
        body.add_column(ratio=1)
        body.add_row(
            Panel(
                lines,
                title="[dim]Runtime Logs[/]",
                border_style=BORDER,
                padding=(0, 1),
            )
        )
        body.add_row(
            Panel(
                journal_lines,
                title="[dim]Journal Entries[/]",
                border_style=BORDER,
                padding=(0, 1),
            )
        )
        body.add_row(count_text)

        return Panel(
            body,
            title=f"[bold {ACCENT}]Logs[/]",
            border_style=BORDER,
            padding=(0, 0),
        )

    # ==================================================================
    # Tab 6: Events
    # ==================================================================

    def _tab_events(self) -> Panel:
        """Events tab — trigger event log."""
        s = self._state

        table = Table(
            show_header=True,
            header_style="bold dim",
            expand=True,
            show_edge=False,
            padding=(0, 1),
        )
        table.add_column("Time", style="dim", width=9, no_wrap=True)
        table.add_column("Process", no_wrap=True, ratio=2)
        table.add_column("Trigger", width=10)
        table.add_column("Event ID", style="dim", width=10)
        table.add_column("Payload", ratio=4)

        events = list(s.events)
        for event in reversed(events[-50:]):
            ts = datetime.datetime.fromtimestamp(event.timestamp).strftime("%H:%M:%S")
            eid = event.event_id[:8] if event.event_id else "-"

            table.add_row(
                ts,
                event.process_name,
                event.trigger_type,
                eid,
                Text(event.payload_summary, style="dim"),
            )

        if not events:
            table.add_row(
                "",
                Text("Waiting for trigger events...", style="dim italic"),
                "",
                "",
                "",
            )

        # Invocation results
        inv_table = Table(
            show_header=True,
            header_style="bold dim",
            expand=True,
            show_edge=False,
            padding=(0, 1),
        )
        inv_table.add_column("Time", style="dim", width=9, no_wrap=True)
        inv_table.add_column("Process", no_wrap=True, ratio=2)
        inv_table.add_column("Trigger", width=10)
        inv_table.add_column("Status", justify="center", width=8)
        inv_table.add_column("Duration", justify="right", width=10)
        inv_table.add_column("Error", ratio=3)

        invocations = list(s.invocations)
        for inv in reversed(invocations[-30:]):
            ts = datetime.datetime.fromtimestamp(inv.timestamp).strftime("%H:%M:%S")
            status = Text("OK", style="green") if inv.success else Text("FAIL", style="red")
            error_text = Text(inv.error or "-", style="red" if inv.error else "dim")
            dur = _fmt_duration_ms(inv.duration_ms)

            inv_table.add_row(
                ts,
                inv.process_name,
                inv.trigger_type,
                status,
                dur,
                error_text,
            )

        if not invocations:
            inv_table.add_row(
                "",
                Text("No invocations yet", style="dim italic"),
                "",
                "",
                "",
                "",
            )

        success = s.total_invocations - s.total_errors
        summary = Text(
            f"  {s.total_events} events    {s.total_invocations} invocations    "
            f"{success} ok    {s.total_errors} errors",
            style="dim",
        )

        body = Table.grid(expand=True)
        body.add_column(ratio=1)
        body.add_row(
            Panel(
                table,
                title="[dim]Trigger Events[/]",
                border_style=BORDER,
                padding=(0, 1),
            )
        )
        body.add_row(
            Panel(
                inv_table,
                title="[dim]Invocation Results[/]",
                border_style=BORDER,
                padding=(0, 1),
            )
        )
        body.add_row(summary)

        return Panel(
            body,
            title=f"[bold {ACCENT}]Events[/]",
            border_style=BORDER,
            padding=(0, 0),
        )

    # ==================================================================
    # Tab 7: Commands
    # ==================================================================

    def _tab_commands(self) -> Panel:
        """Commands tab — interactive command input and history."""
        s = self._state

        # Help section
        help_text = Text()
        help_text.append("  Available Commands:\n\n", style="bold white")

        cmds = [
            ("help", "Show this help message"),
            ("list", "List all registered processes"),
            ("status [name]", "Show process status (all or specific)"),
            ("start <name>", "Start a stopped process"),
            ("stop <name>", "Stop a running process"),
            ("restart <name>", "Restart a process"),
            ("suspend <name>", "Suspend a process (triggers still queue)"),
            ("resume <name>", "Resume a suspended process"),
            ("inject <name> [json]", "Inject a manual trigger event"),
        ]
        for cmd, desc in cmds:
            help_text.append(f"    {cmd:<22}", style="cyan")
            help_text.append(f" {desc}\n", style="dim")

        help_text.append(
            "\n  Press ':' or '/' to enter command mode, then type your command.\n",
            style="dim italic",
        )

        # Command input
        input_text = Text()
        if self._command_mode:
            input_text.append("  > ", style="bold cyan")
            input_text.append(self._command_buffer, style="white")
            input_text.append("\u2588", style="bold white")  # cursor
            input_text.append("    (Enter to run, Esc to cancel)\n", style="dim")
        else:
            input_text.append("  Press ':' to enter a command...\n", style="dim italic")

        # Command history
        history_table = Table(
            show_header=True,
            header_style="bold dim",
            expand=True,
            show_edge=False,
            padding=(0, 1),
        )
        history_table.add_column("Time", style="dim", width=9, no_wrap=True)
        history_table.add_column("Command", style="white", ratio=2)
        history_table.add_column("Result", ratio=4)
        history_table.add_column("", width=4)

        commands = list(s.commands)
        for cmd_result in reversed(commands[-20:]):
            ts = datetime.datetime.fromtimestamp(cmd_result.timestamp).strftime("%H:%M:%S")
            status_icon = Text(
                "\u2713" if cmd_result.success else "\u2717",
                style="green" if cmd_result.success else "red",
            )
            result_style = "white" if cmd_result.success else "red"

            history_table.add_row(
                ts,
                cmd_result.command,
                Text(cmd_result.result, style=result_style),
                status_icon,
            )

        if not commands:
            history_table.add_row(
                "",
                Text("No commands executed yet", style="dim italic"),
                "",
                "",
            )

        body = Table.grid(expand=True)
        body.add_column(ratio=1)
        body.add_row(
            Panel(
                help_text,
                title="[dim]Help[/]",
                border_style=BORDER,
                padding=(0, 1),
            )
        )
        body.add_row(
            Panel(
                input_text,
                title="[dim]Command Input[/]",
                border_style=BORDER,
                padding=(0, 1),
            )
        )
        body.add_row(
            Panel(
                history_table,
                title="[dim]Command History[/]",
                border_style=BORDER,
                padding=(0, 1),
            )
        )

        return Panel(
            body,
            title=f"[bold {ACCENT}]Commands[/]",
            border_style=BORDER,
            padding=(0, 0),
        )


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _kv(label: str, value: str, style: str) -> Text:
    """Key-value stat cell: value on top, label below."""
    text = Text()
    text.append(f"{value}\n", style=style)
    text.append(label, style="dim")
    return text


def _format_duration(seconds: float | None) -> str:
    """Format seconds as human-readable duration."""
    if seconds is None:
        return "-"
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, secs = divmod(s, 60)
    if m < 60:
        return f"{m}m {secs}s"
    h, m = divmod(m, 60)
    if h < 24:
        return f"{h}h {m}m"
    d, h = divmod(h, 24)
    return f"{d}d {h}h"


def _fmt_duration_ms(ms: float) -> str:
    """Format a duration in milliseconds."""
    if ms >= 1000:
        return f"{ms / 1000:.1f}s"
    if ms >= 1:
        return f"{ms:.0f}ms"
    return f"{ms:.1f}ms"


def _get_event_loop() -> Any:
    """Get the current running asyncio event loop.

    Falls back to creating a new one if none is running (shouldn't
    happen in normal runtime operation).
    """
    import asyncio

    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.new_event_loop()
