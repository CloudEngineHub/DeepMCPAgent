"""Live CLI monitoring dashboard for the MCP server.

When enabled via ``server.run(dashboard=True)``, replaces the static
startup banner with a full-screen, tabbed terminal dashboard with
keyboard navigation.

**Tabs** (switch with arrow keys, 1-6, or Tab):

1. Overview   -- server info, key metrics, recent activity
2. Tools      -- registered tools with per-tool stats
3. Agents     -- connected agents with session details
4. Logs       -- scrolling request log
5. Metrics    -- performance data and error breakdown
6. Raw Logs   -- raw Python logger output

Requires ``rich>=13``, which is already a framework dependency.

Example::

    server = MCPServer(name="my-api", version="1.0.0")
    # ... register tools ...
    server.run(transport="http", dashboard=True)
"""

from __future__ import annotations

import datetime
import logging
import os
import sys
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

from rich.console import Console
from rich.layout import Layout
from rich.live import Live
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

# ------------------------------------------------------------------
# Logo — compact version of the Promptise "P" mark
# ------------------------------------------------------------------

LOGO = [
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

# Keep BANNER as alias for backward compatibility
BANNER = LOGO


# ------------------------------------------------------------------
# Tab definitions
# ------------------------------------------------------------------

TABS: list[tuple[str, str]] = [
    ("1", "Overview"),
    ("2", "Tools"),
    ("3", "Agents"),
    ("4", "Logs"),
    ("5", "Metrics"),
    ("6", "Raw Logs"),
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
# Data types
# ------------------------------------------------------------------


@dataclass
class RequestLog:
    """A single request record captured by :class:`DashboardMiddleware`."""

    timestamp: float
    client_id: str | None
    tool_name: str
    success: bool
    latency_ms: float
    error_code: str | None = None


@dataclass
class DashboardState:
    """Thread-safe shared state between middleware and dashboard renderer.

    Simple counters and ``deque`` are GIL-protected for basic
    operations, making this safe without explicit locks for our
    single-writer (middleware) / single-reader (dashboard thread)
    pattern.
    """

    # Server info
    server_name: str = ""
    version: str = ""
    transport: str = ""
    host: str = ""
    port: int = 0
    started_at: float = field(default_factory=time.time)

    # Registration info
    tools: list[dict[str, Any]] = field(default_factory=list)
    resource_count: int = 0
    prompt_count: int = 0
    middleware_count: int = 0

    # Request log (ring buffer, maxlen enforced by deque)
    recent_requests: deque[RequestLog] = field(default_factory=lambda: deque(maxlen=200))

    # Global counters
    total_requests: int = 0
    total_errors: int = 0

    # Per-tool metrics
    tool_calls: dict[str, int] = field(default_factory=dict)
    tool_errors: dict[str, int] = field(default_factory=dict)
    tool_latency: dict[str, float] = field(default_factory=dict)
    tool_min_latency: dict[str, float] = field(default_factory=dict)
    tool_max_latency: dict[str, float] = field(default_factory=dict)

    # Active sessions (client_id -> session info)
    active_sessions: dict[str, dict[str, Any]] = field(default_factory=dict)

    def record_request(self, log: RequestLog) -> None:
        """Record a completed request (called from middleware)."""
        self.recent_requests.append(log)
        self.total_requests += 1
        if not log.success:
            self.total_errors += 1

        name = log.tool_name
        self.tool_calls[name] = self.tool_calls.get(name, 0) + 1
        if not log.success:
            self.tool_errors[name] = self.tool_errors.get(name, 0) + 1
        self.tool_latency[name] = self.tool_latency.get(name, 0.0) + log.latency_ms

        # Min / max latency
        if name not in self.tool_min_latency:
            self.tool_min_latency[name] = log.latency_ms
        else:
            self.tool_min_latency[name] = min(self.tool_min_latency[name], log.latency_ms)
        if name not in self.tool_max_latency:
            self.tool_max_latency[name] = log.latency_ms
        else:
            self.tool_max_latency[name] = max(self.tool_max_latency[name], log.latency_ms)

        # Track sessions
        if log.client_id:
            if log.client_id not in self.active_sessions:
                self.active_sessions[log.client_id] = {
                    "first_seen": log.timestamp,
                    "last_seen": log.timestamp,
                    "request_count": 0,
                    "error_count": 0,
                    "tools_used": set(),
                }
            session = self.active_sessions[log.client_id]
            session["last_seen"] = log.timestamp
            session["request_count"] += 1
            if not log.success:
                session["error_count"] = session.get("error_count", 0) + 1
            session.setdefault("tools_used", set()).add(log.tool_name)


# ------------------------------------------------------------------
# Log capture handler (for Raw Logs tab)
# ------------------------------------------------------------------


class _LogCapture(logging.Handler):
    """Captures log records into a ring buffer for the Raw Logs tab."""

    def __init__(self, maxlen: int = 500) -> None:
        super().__init__()
        self.records: deque[logging.LogRecord] = deque(maxlen=maxlen)

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


# ------------------------------------------------------------------
# Middleware
# ------------------------------------------------------------------


class DashboardMiddleware:
    """Outermost middleware that records request metrics for the dashboard.

    Must be inserted as the *first* middleware (outermost position) to
    capture all errors including authentication failures.
    """

    def __init__(self, state: DashboardState) -> None:
        self._state = state

    async def __call__(self, ctx: Any, call_next: Any) -> Any:
        start = time.perf_counter()
        error = False
        error_code = None
        try:
            result = await call_next(ctx)
            return result
        except Exception as exc:
            error = True
            error_code = getattr(exc, "code", None) or type(exc).__name__
            raise
        finally:
            latency = (time.perf_counter() - start) * 1000
            self._state.record_request(
                RequestLog(
                    timestamp=time.time(),
                    client_id=ctx.client_id,
                    tool_name=ctx.tool_name,
                    success=not error,
                    latency_ms=latency,
                    error_code=error_code,
                )
            )


# ------------------------------------------------------------------
# Dashboard renderer
# ------------------------------------------------------------------


class Dashboard:
    """Full-screen tabbed terminal dashboard with keyboard navigation.

    Features:

    - **6 tabs** -- Overview, Tools, Agents, Logs, Metrics, Raw Logs
    - **Arrow-key navigation** -- left/right to switch tabs
    - **Number keys** -- 1-6 to jump directly
    - **Tab key** -- cycle to next tab
    - **Live refresh** -- every ~250ms in a daemon thread
    - **Raw log capture** -- Python logger output displayed in tab 6

    Runs its own refresh + keyboard threads (both daemon), safe
    alongside uvicorn.
    """

    def __init__(self, state: DashboardState) -> None:
        self._state = state
        self._console = Console()
        self._live: Live | None = None
        self._refresh_thread: threading.Thread | None = None
        self._key_thread: threading.Thread | None = None
        self._running = False
        self._current_tab = 0
        self._log_capture = _LogCapture(maxlen=500)
        # Stores (handler, original_level) for muted stream handlers
        self._muted_handlers: list[tuple[logging.Handler, int]] = []
        self._orig_root_level: int = logging.WARNING

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

        # Refresh thread
        self._refresh_thread = threading.Thread(target=self._refresh_loop, daemon=True)
        self._refresh_thread.start()

        # Keyboard reader thread
        self._key_thread = threading.Thread(target=self._key_reader, daemon=True)
        self._key_thread.start()

    def stop(self) -> None:
        """Stop the dashboard and restore the terminal."""
        self._running = False
        if self._live:
            try:
                self._live.stop()
            except Exception:
                logging.debug("Error stopping Live display", exc_info=True)
        self._restore_loggers()

    # ------------------------------------------------------------------
    # Logger capture & suppression
    # ------------------------------------------------------------------

    def _suppress_loggers(self) -> None:
        """Redirect log output to internal capture buffer.

        Instead of just silencing loggers, we install a capture handler
        on the root logger and then mute all StreamHandlers so output
        doesn't corrupt the Rich Live display.  The captured records
        are shown in the Raw Logs tab.
        """
        root = logging.getLogger()
        self._orig_root_level = root.level
        # Let records through at DEBUG so we capture everything
        if root.level > logging.DEBUG:
            root.setLevel(logging.DEBUG)
        self._log_capture.setLevel(logging.DEBUG)
        root.addHandler(self._log_capture)

        # Mute all StreamHandlers so they don't write to stdout/stderr
        logger_names = [
            "",
            "promptise.server",
            "uvicorn",
            "uvicorn.access",
            "uvicorn.error",
            "mcp",
            "mcp.server",
            "httpx",
            "httpcore",
        ]
        for name in logger_names:
            lgr = logging.getLogger(name) if name else root
            for handler in lgr.handlers:
                if handler is not self._log_capture and isinstance(handler, logging.StreamHandler):
                    self._muted_handlers.append((handler, handler.level))
                    handler.setLevel(logging.CRITICAL + 1)

    def _restore_loggers(self) -> None:
        """Restore original logger levels and remove capture handler."""
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
        """Background thread that pushes updated renders to Live."""
        while self._running:
            try:
                if self._live:
                    self._live.update(self._render())
            except Exception:
                logging.debug("Dashboard refresh error", exc_info=True)
            time.sleep(0.25)

    def _key_reader(self) -> None:
        """Background thread reading keyboard input for tab navigation."""
        try:
            if sys.platform == "win32":
                self._key_reader_windows()
            else:
                self._key_reader_unix()
        except Exception:
            logging.debug("Keyboard reader failed", exc_info=True)

    def _key_reader_unix(self) -> None:
        """Unix keyboard reader using termios + os.read.

        Uses ``os.read(fd, 1)`` (not ``sys.stdin.read``) for reliable
        raw byte reading.  Escape sequence bytes arrive within
        microseconds so a 20ms timeout is more than enough and keeps
        navigation feeling instant.
        """
        import select
        import termios
        import tty

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        try:
            tty.setcbreak(fd)
            while self._running:
                # Wait for input with short timeout for responsiveness
                if not select.select([fd], [], [], 0.05)[0]:
                    continue

                ch = os.read(fd, 1)

                if ch == b"\x1b":
                    # Start of escape sequence -- remaining bytes arrive
                    # almost instantly.  20ms timeout per byte is plenty.
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
                    # Up/Down arrows ignored for now

                elif ch == b"\t":  # Tab key
                    self._next_tab()
                elif ch in (b"1", b"2", b"3", b"4", b"5", b"6"):
                    self._current_tab = int(ch) - 1
        except Exception:
            logging.debug("Unix key reader error", exc_info=True)
        finally:
            try:
                termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
            except Exception:
                logging.debug("Error restoring terminal settings", exc_info=True)

    def _key_reader_windows(self) -> None:
        """Windows keyboard reader using msvcrt."""
        try:
            import msvcrt
        except ImportError:
            return

        # msvcrt is Windows-only; stubs not available on POSIX → ignore attr checks here.
        while self._running:
            if msvcrt.kbhit():  # type: ignore[attr-defined]
                ch = msvcrt.getch()  # type: ignore[attr-defined]
                if ch in (b"\xe0", b"\x00"):
                    ch2 = msvcrt.getch()  # type: ignore[attr-defined]
                    if ch2 == b"M":  # Right arrow
                        self._next_tab()
                    elif ch2 == b"K":  # Left arrow
                        self._prev_tab()
                elif ch == b"\t":
                    self._next_tab()
                elif ch in (b"1", b"2", b"3", b"4", b"5", b"6"):
                    self._current_tab = int(ch) - 1
            else:
                time.sleep(0.03)

    def _next_tab(self) -> None:
        """Move to the next tab (wraps around)."""
        self._current_tab = (self._current_tab + 1) % len(TABS)

    def _prev_tab(self) -> None:
        """Move to the previous tab (wraps around)."""
        self._current_tab = (self._current_tab - 1) % len(TABS)

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
            self._tab_tools,
            self._tab_agents,
            self._tab_logs,
            self._tab_metrics,
            self._tab_raw_logs,
        ]
        layout["body"].update(tab_renderers[self._current_tab]())

        layout["footer"].update(
            Text(
                "  \u2190 \u2192 Navigate    1-6 Jump    Tab Next    Ctrl+C Quit",
                style="dim",
            )
        )

        return layout

    # ==================================================================
    # Header & Navigation
    # ==================================================================

    def _header(self) -> Panel:
        """Two-column header: P-mark logo left, server metadata right."""
        s = self._state
        uptime = _format_duration(time.time() - s.started_at)
        endpoint = _build_endpoint(s.transport, s.host, s.port)

        # ---- Left column: logo ----
        logo_text = Text()
        for line in LOGO:
            logo_text.append(line + "\n", style="bold white")

        # ---- Right column: metadata (vertically centered) ----
        auth_count = sum(1 for t in s.tools if t.get("auth"))
        public_count = len(s.tools) - auth_count

        # Pad top to vertically center within the 14-line logo
        info = Text()
        info.append("\n")
        info.append("P R O M P T I S E\n", style="bold white")
        info.append("Agentic AI Framework\n\n", style="dim")

        info.append("Server     ", style="dim")
        info.append(s.server_name, style="bold white")
        info.append(f"  v{s.version}\n", style="dim")

        info.append("Endpoint   ", style="dim")
        info.append(f"{endpoint}\n", style="white")

        info.append("Uptime     ", style="dim")
        info.append(f"{uptime}\n\n", style="white")

        info.append("Tools      ", style="dim")
        info.append(f"{len(s.tools)}", style="bold white")
        info.append(f"  ({auth_count} auth, {public_count} public)\n", style="dim")

        info.append("Agents     ", style="dim")
        info.append(f"{len(s.active_sessions)}", style="bold white")
        info.append(" connected\n", style="dim")

        info.append("Requests   ", style="dim")
        info.append(f"{s.total_requests}", style="bold white")
        if s.total_errors:
            info.append(f"  ({s.total_errors} errors)", style="red")
        info.append("\n\n")

        info.append("promptise.com", style="dim underline")
        info.append("  \u00b7  ", style="dim")
        info.append("promptise.com/docs", style="dim underline")

        # ---- Two-column grid ----
        grid = Table.grid(expand=True, padding=(0, 2))
        grid.add_column(width=32)  # logo
        grid.add_column(ratio=1)  # metadata
        grid.add_row(logo_text, info)

        return Panel(
            grid,
            border_style=HEADER_BORDER,
            padding=(0, 1),
        )

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
        """Overview tab -- key metrics + recent activity."""
        s = self._state

        total_lat = sum(s.tool_latency.values())
        avg_lat = total_lat / s.total_requests if s.total_requests > 0 else 0.0
        err_rate = (s.total_errors / s.total_requests * 100) if s.total_requests > 0 else 0.0
        uptime_secs = max(time.time() - s.started_at, 1)
        rps = s.total_requests / uptime_secs
        auth_count = sum(1 for t in s.tools if t.get("auth"))

        # ---- Stats row ----
        stats = Table.grid(expand=True, padding=(0, 3))
        for _ in range(4):
            stats.add_column(ratio=1)

        stats.add_row(
            _kv("Requests", str(s.total_requests), ACCENT),
            _kv("Errors", str(s.total_errors), "red" if s.total_errors else "dim"),
            _kv("Avg latency", f"{avg_lat:.1f}ms", "white"),
            _kv("Throughput", f"{rps:.2f} req/s", "white"),
        )
        stats.add_row(
            _kv("Tools", str(len(s.tools)), "white"),
            _kv("Auth tools", str(auth_count), "white"),
            _kv("Agents", str(len(s.active_sessions)), "white"),
            _kv("Error rate", f"{err_rate:.1f}%", "red" if err_rate > 5 else "dim"),
        )

        # ---- Recent activity ----
        recent = list(s.recent_requests)
        last_8 = list(reversed(recent[-8:])) if recent else []

        activity = Table(
            show_header=True,
            header_style="bold dim",
            expand=True,
            show_edge=False,
            padding=(0, 1),
            show_lines=False,
        )
        activity.add_column("Time", style="dim", width=9)
        activity.add_column("Agent", style="dim", ratio=2)
        activity.add_column("Tool", ratio=3)
        activity.add_column("Status", justify="center", width=8)
        activity.add_column("Latency", justify="right", width=10)

        for log in last_8:
            ts = datetime.datetime.fromtimestamp(log.timestamp).strftime("%H:%M:%S")
            client = log.client_id or "anon"
            status = Text("OK", style="green") if log.success else Text("FAIL", style="red")
            lat = _fmt_lat(log.latency_ms)
            activity.add_row(ts, client, log.tool_name, status, lat)

        if not last_8:
            activity.add_row("", Text("Waiting for requests...", style="dim italic"), "", "", "")

        # ---- Compose ----
        body = Table.grid(expand=True)
        body.add_column(ratio=1)
        body.add_row(Panel(stats, title="[dim]Metrics[/]", border_style=BORDER, padding=(0, 1)))
        body.add_row(
            Panel(activity, title="[dim]Recent Activity[/]", border_style=BORDER, padding=(0, 1))
        )

        return Panel(body, title=f"[bold {ACCENT}]Overview[/]", border_style=BORDER, padding=(0, 0))

    # ==================================================================
    # Tab 2: Tools
    # ==================================================================

    def _tab_tools(self) -> Panel:
        """Tools tab -- registered tools with per-tool performance."""
        s = self._state

        table = Table(
            show_header=True,
            header_style="bold dim",
            expand=True,
            show_edge=False,
            padding=(0, 1),
        )
        table.add_column("Tool", style="white", no_wrap=True, ratio=3)
        table.add_column("Auth", justify="center", width=6)
        table.add_column("Roles", ratio=2)
        table.add_column("Calls", justify="right", width=7, style=ACCENT)
        table.add_column("Errors", justify="right", width=7)
        table.add_column("Avg ms", justify="right", width=8)

        for t in s.tools:
            name = t["name"]

            auth_text = Text("yes", style="yellow") if t.get("auth") else Text("-", style="dim")

            roles = t.get("roles", [])
            roles_text = Text(", ".join(roles) if roles else "-", style="dim")

            calls = s.tool_calls.get(name, 0)
            errors = s.tool_errors.get(name, 0)
            total_lat = s.tool_latency.get(name, 0.0)
            avg_lat = total_lat / calls if calls > 0 else 0.0

            err_text = Text(str(errors), style="red" if errors > 0 else "dim")
            lat_text = Text(
                f"{avg_lat:.1f}" if calls > 0 else "-",
                style="yellow" if avg_lat > 100 else "dim",
            )

            table.add_row(name, auth_text, roles_text, str(calls), err_text, lat_text)

        auth_count = sum(1 for t in s.tools if t.get("auth"))
        public_count = len(s.tools) - auth_count
        summary = Text(
            f"  {len(s.tools)} tools    {auth_count} auth    {public_count} public",
            style="dim",
        )

        body = Table.grid(expand=True)
        body.add_column(ratio=1)
        body.add_row(table)
        body.add_row(Text(""))
        body.add_row(summary)

        return Panel(body, title=f"[bold {ACCENT}]Tools[/]", border_style=BORDER, padding=(0, 1))

    # ==================================================================
    # Tab 3: Agents
    # ==================================================================

    def _tab_agents(self) -> Panel:
        """Agents tab -- connected agent sessions."""
        s = self._state

        if not s.active_sessions:
            empty = Table.grid(expand=True)
            empty.add_column(justify="center")
            empty.add_row("")
            empty.add_row(Text("No agents connected", style="dim"))
            empty.add_row("")
            empty.add_row(Text("Waiting for connections...", style="dim italic"))
            empty.add_row("")
            empty.add_row(Text("Connect with:  python agent.py  or  python client.py", style="dim"))
            empty.add_row("")
            return Panel(
                empty, title=f"[bold {ACCENT}]Agents[/]", border_style=BORDER, padding=(1, 2)
            )

        table = Table(
            show_header=True,
            header_style="bold dim",
            expand=True,
            show_edge=False,
            padding=(0, 1),
        )
        table.add_column("Agent", style="white", no_wrap=True, ratio=3)
        table.add_column("Requests", justify="right", width=9)
        table.add_column("Errors", justify="right", width=7)
        table.add_column("Tools Used", ratio=2)
        table.add_column("First Seen", style="dim", width=10)
        table.add_column("Last Active", width=12)
        table.add_column("Duration", justify="right", width=10)

        now = time.time()
        for client_id, info in sorted(
            s.active_sessions.items(),
            key=lambda x: x[1]["last_seen"],
            reverse=True,
        ):
            elapsed_since = now - info["last_seen"]
            duration = info["last_seen"] - info["first_seen"]
            errors = info.get("error_count", 0)
            tools_used = info.get("tools_used", set())

            first_seen = datetime.datetime.fromtimestamp(info["first_seen"]).strftime("%H:%M:%S")

            if elapsed_since < 60:
                last_text = Text(f"{elapsed_since:.0f}s ago", style="green")
            else:
                last_text = Text(_format_duration(elapsed_since) + " ago", style="yellow")

            tools_str = ", ".join(sorted(tools_used)[:3])
            if len(tools_used) > 3:
                tools_str += f" +{len(tools_used) - 3}"

            table.add_row(
                client_id,
                str(info["request_count"]),
                Text(str(errors), style="red" if errors > 0 else "dim"),
                Text(tools_str or "-", style="dim"),
                first_seen,
                last_text,
                _format_duration(duration),
            )

        total_reqs = sum(i["request_count"] for i in s.active_sessions.values())
        total_errs = sum(i.get("error_count", 0) for i in s.active_sessions.values())
        summary = Text(
            f"  {len(s.active_sessions)} agents    {total_reqs} requests    {total_errs} errors",
            style="dim",
        )

        body = Table.grid(expand=True)
        body.add_column(ratio=1)
        body.add_row(table)
        body.add_row(Text(""))
        body.add_row(summary)

        return Panel(body, title=f"[bold {ACCENT}]Agents[/]", border_style=BORDER, padding=(0, 1))

    # ==================================================================
    # Tab 4: Logs
    # ==================================================================

    def _tab_logs(self) -> Panel:
        """Logs tab -- scrolling request log."""
        s = self._state

        table = Table(
            show_header=True,
            header_style="bold dim",
            expand=True,
            show_edge=False,
            padding=(0, 1),
        )
        table.add_column("Time", style="dim", width=9, no_wrap=True)
        table.add_column("Agent", style="dim", no_wrap=True, ratio=2)
        table.add_column("Tool", no_wrap=True, ratio=3)
        table.add_column("Status", justify="center", width=8)
        table.add_column("Latency", justify="right", width=10)
        table.add_column("Error", ratio=2)

        requests = list(s.recent_requests)
        for log in reversed(requests[-50:]):
            ts = datetime.datetime.fromtimestamp(log.timestamp).strftime("%H:%M:%S")
            client = log.client_id or "anon"

            if log.success:
                status = Text("OK", style="green")
                error_text = Text("-", style="dim")
            else:
                status = Text("FAIL", style="red")
                code = _shorten_error(log.error_code)
                error_text = Text(code, style="red")

            lat = _fmt_lat_colored(log.latency_ms)
            table.add_row(ts, client, log.tool_name, status, lat, error_text)

        if not requests:
            table.add_row("", Text("Waiting for requests...", style="dim italic"), "", "", "", "")

        success = s.total_requests - s.total_errors
        summary = Text(
            f"  {s.total_requests} total    {success} ok    {s.total_errors} errors    "
            f"showing last {min(len(requests), 50)}",
            style="dim",
        )

        body = Table.grid(expand=True)
        body.add_column(ratio=1)
        body.add_row(table)
        body.add_row(Text(""))
        body.add_row(summary)

        return Panel(body, title=f"[bold {ACCENT}]Logs[/]", border_style=BORDER, padding=(0, 1))

    # ==================================================================
    # Tab 5: Metrics
    # ==================================================================

    def _tab_metrics(self) -> Panel:
        """Metrics tab -- performance data and error breakdown."""
        s = self._state

        # ---- Global summary ----
        total_lat = sum(s.tool_latency.values())
        avg_global = total_lat / s.total_requests if s.total_requests > 0 else 0.0
        uptime_secs = max(time.time() - s.started_at, 1)
        rps = s.total_requests / uptime_secs
        err_rate = (s.total_errors / s.total_requests * 100) if s.total_requests > 0 else 0.0

        summary_grid = Table.grid(expand=True, padding=(0, 3))
        for _ in range(4):
            summary_grid.add_column(ratio=1)
        summary_grid.add_row(
            _kv("Throughput", f"{rps:.2f} req/s", ACCENT),
            _kv("Avg latency", f"{avg_global:.1f}ms", "white"),
            _kv("Error rate", f"{err_rate:.1f}%", "red" if err_rate > 5 else "dim"),
            _kv("Uptime", _format_duration(uptime_secs), "white"),
        )

        # ---- Per-tool performance ----
        perf_table = Table(
            show_header=True,
            header_style="bold dim",
            expand=True,
            show_edge=False,
            padding=(0, 1),
        )
        perf_table.add_column("Tool", style="white", no_wrap=True, ratio=3)
        perf_table.add_column("Calls", justify="right", width=7)
        perf_table.add_column("Errors", justify="right", width=7)
        perf_table.add_column("Err%", justify="right", width=7)
        perf_table.add_column("Avg", justify="right", width=8)
        perf_table.add_column("Min", justify="right", width=8)
        perf_table.add_column("Max", justify="right", width=8)

        tool_names = sorted(s.tool_calls.keys())
        for name in tool_names:
            calls = s.tool_calls.get(name, 0)
            errors = s.tool_errors.get(name, 0)
            total_lat_t = s.tool_latency.get(name, 0.0)
            min_lat = s.tool_min_latency.get(name, 0.0)
            max_lat = s.tool_max_latency.get(name, 0.0)
            avg_lat = total_lat_t / calls if calls > 0 else 0.0
            tool_err_rate = (errors / calls * 100) if calls > 0 else 0.0

            err_style = "red" if tool_err_rate > 10 else ("yellow" if tool_err_rate > 0 else "dim")
            lat_style = "red" if avg_lat > 100 else ("yellow" if avg_lat > 10 else "dim")

            perf_table.add_row(
                name,
                str(calls),
                Text(str(errors), style=err_style),
                Text(f"{tool_err_rate:.1f}%", style=err_style),
                Text(f"{avg_lat:.1f}ms", style=lat_style),
                Text(f"{min_lat:.1f}ms", style="dim"),
                Text(f"{max_lat:.1f}ms", style="dim"),
            )

        if not tool_names:
            perf_table.add_row(Text("No metrics yet", style="dim italic"), "", "", "", "", "", "")

        # ---- Error breakdown ----
        error_counts: dict[str, int] = {}
        for log in s.recent_requests:
            if not log.success and log.error_code:
                short = _shorten_error(log.error_code)
                error_counts[short] = error_counts.get(short, 0) + 1

        error_table = Table(
            show_header=True,
            header_style="bold dim",
            expand=True,
            show_edge=False,
            padding=(0, 1),
        )
        error_table.add_column("Error Type", style="white", ratio=2)
        error_table.add_column("Count", justify="right", width=8)
        error_table.add_column("% of Errors", justify="right", width=12)

        for err_type, count in sorted(error_counts.items(), key=lambda x: -x[1]):
            pct = (count / s.total_errors * 100) if s.total_errors > 0 else 0
            error_table.add_row(err_type, str(count), f"{pct:.1f}%")

        if not error_counts:
            error_table.add_row(Text("No errors", style="dim"), "", "")

        # ---- Compose ----
        body = Table.grid(expand=True)
        body.add_column(ratio=1)
        body.add_row(
            Panel(summary_grid, title="[dim]Global[/]", border_style=BORDER, padding=(0, 1))
        )
        body.add_row(
            Panel(
                perf_table,
                title="[dim]Per-Tool Performance[/]",
                border_style=BORDER,
                padding=(0, 1),
            )
        )
        body.add_row(
            Panel(error_table, title="[dim]Error Breakdown[/]", border_style=BORDER, padding=(0, 1))
        )

        return Panel(body, title=f"[bold {ACCENT}]Metrics[/]", border_style=BORDER, padding=(0, 0))

    # ==================================================================
    # Tab 6: Raw Logs
    # ==================================================================

    def _tab_raw_logs(self) -> Panel:
        """Raw Logs tab -- Python logger output captured from all loggers."""
        records = list(self._log_capture.records)
        last_n = list(reversed(records[-80:]))

        lines = Text()
        for rec in last_n:
            ts = datetime.datetime.fromtimestamp(rec.created).strftime("%H:%M:%S.%f")[:-3]
            level = rec.levelname
            name = rec.name
            msg = rec.getMessage()

            # Truncate long logger names and messages
            if len(name) > 28:
                name = name[:25] + "..."
            if len(msg) > 200:
                msg = msg[:197] + "..."

            # Color by level
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
            lines.append("  Logs from uvicorn, MCP SDK, promptise.server, and\n", style="dim")
            lines.append("  other loggers will appear here in real-time.\n\n", style="dim")
            lines.append("  Send a request to see output.\n", style="dim")

        count_text = Text(
            f"  {len(records)} records captured    showing last {min(len(records), 80)}",
            style="dim",
        )

        body = Table.grid(expand=True)
        body.add_column(ratio=1)
        body.add_row(lines)
        body.add_row(Text(""))
        body.add_row(count_text)

        return Panel(body, title=f"[bold {ACCENT}]Raw Logs[/]", border_style=BORDER, padding=(0, 1))


# ------------------------------------------------------------------
# Helpers
# ------------------------------------------------------------------


def _kv(label: str, value: str, style: str) -> Text:
    """Key-value stat cell: value on top, label below."""
    text = Text()
    text.append(f"{value}\n", style=style)
    text.append(label, style="dim")
    return text


def _fmt_lat(ms: float) -> str:
    """Format latency for display."""
    if ms >= 1:
        return f"{ms:.0f}ms"
    return f"{ms:.1f}ms"


def _fmt_lat_colored(ms: float) -> Text:
    """Format latency with color coding (red > 100, yellow > 10)."""
    if ms >= 100:
        return Text(f"{ms:.0f}ms", style="red")
    elif ms >= 10:
        return Text(f"{ms:.0f}ms", style="yellow")
    elif ms >= 1:
        return Text(f"{ms:.0f}ms", style="dim")
    return Text(f"{ms:.1f}ms", style="dim")


def _format_duration(seconds: float) -> str:
    """Format seconds as human-readable duration."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    m, secs = divmod(s, 60)
    if m < 60:
        return f"{m}m {secs}s"
    h, m = divmod(m, 60)
    return f"{h}h {m}m"


def _build_endpoint(transport: str, host: str, port: int) -> str:
    """Build the endpoint URL string."""
    if transport == "stdio":
        return "stdin/stdout"
    elif transport == "sse":
        return f"http://{host}:{port}/sse"
    return f"http://{host}:{port}/mcp"


def _shorten_error(code: str | None) -> str:
    """Shorten error codes for compact display."""
    if not code:
        return "ERROR"
    upper = code.upper()
    if "AUTHENTICATION" in upper:
        return "AUTH"
    if "VALIDATION" in upper:
        return "VALID"
    if "TIMEOUT" in upper:
        return "TIMEOUT"
    if "RATE" in upper:
        return "RATE"
    if "INTERNAL" in upper:
        return "INTERNAL"
    return code[:12]
