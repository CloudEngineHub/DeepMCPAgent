"""State files for ``promptise runtime start`` (used by the runtime CLI).

A runtime started with ``promptise runtime start`` serves the
:class:`~promptise.runtime.api.OrchestrationAPI` on a loopback port with a
random bearer token, and records how to reach it in a *state file*:
``<state dir>/<runtime name>.json``.  ``promptise runtime status | stop |
restart | logs`` read that file to find the runtime.

The state directory is ``~/.promptise/runtime`` (or the directory named by
``$PROMPTISE_RUNTIME_STATE_DIR``).  It holds bearer tokens, so:

* the directory is created with mode ``0700`` and each file with ``0600``;
* on POSIX, a directory or file owned by another user, or one other users
  can read or write, is refused (a directory you own is tightened to
  ``0700`` instead);
* a state file whose process is gone (a crash, ``kill -9``) is *stale*: it
  is removed the next time the directory is listed.

The runtime removes its own file when it exits cleanly.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import signal
import stat
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

#: Windows refuses to delete a file another process has open.
_WINDOWS = os.name == "nt"

__all__ = [
    "LAUNCH_ENV",
    "SHUTDOWN_PATH",
    "STATE_DIR_ENV",
    "TOKEN_ENV",
    "RuntimeState",
    "StateError",
    "detach_popen_kwargs",
    "find_state",
    "kill_process",
    "list_states",
    "log_path",
    "pid_alive",
    "remove_state",
    "safe_runtime_name",
    "state_dir",
    "state_recorded",
    "terminate_process",
    "validate_runtime_name",
    "wait_for_exit",
    "write_state",
]

#: Overrides the state directory (default ``~/.promptise/runtime``).
STATE_DIR_ENV = "PROMPTISE_RUNTIME_STATE_DIR"

#: Bearer token: used by ``start`` for its API (instead of a random one) and
#: by the client commands for ``--url`` targets.
TOKEN_ENV = "PROMPTISE_RUNTIME_TOKEN"

#: Set by ``start --detach`` on the background child, which records it in its
#: state file so the parent knows the file is the child's.
LAUNCH_ENV = "PROMPTISE_RUNTIME_LAUNCH_ID"

#: Route the CLI-started runtime adds to its API: a graceful shutdown of the
#: whole runtime (stop every process, close the API, exit).
SHUTDOWN_PATH = "/api/v1/runtime/shutdown"

_STATE_VERSION = 1
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


class StateError(Exception):
    """A state directory or file problem, with a message for the user."""


@dataclass
class RuntimeState:
    """What a running ``promptise runtime start`` records about itself.

    Attributes:
        name: Runtime name (the state file is ``<name>.json``).
        pid: Process ID of the ``promptise runtime start`` process.
        host: Address the Orchestration API listens on.
        port: Port the Orchestration API listens on.
        token: Bearer token of the Orchestration API.
        manifests: Absolute paths of the loaded ``.agent`` files.
        processes: ``process name -> {"journal": {...} | None}``; the
            journal entry holds ``backend`` and the absolute ``path``.
        started_at: ISO 8601 start time (UTC).
        ready: ``False`` while the processes are still starting.
        launch_id: Set when started by ``start --detach``.
        log_file: The log file of a detached runtime.
    """

    name: str
    pid: int
    host: str
    port: int
    token: str
    manifests: list[str] = field(default_factory=list)
    processes: dict[str, Any] = field(default_factory=dict)
    started_at: str = ""
    ready: bool = False
    launch_id: str | None = None
    log_file: str | None = None
    version: int = _STATE_VERSION

    @property
    def url(self) -> str:
        """Base URL of the runtime's Orchestration API."""
        host = self.host
        if host in ("0.0.0.0", ""):  # nosec B104 - maps a wildcard bind to loopback, binds nothing
            host = "127.0.0.1"
        elif host == "::":
            host = "::1"
        if ":" in host and not host.startswith("["):
            host = f"[{host}]"
        return f"http://{host}:{self.port}"

    @staticmethod
    def now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Names
# ---------------------------------------------------------------------------


def validate_runtime_name(name: str) -> str:
    """Return *name*, or raise :class:`StateError` when it isn't usable.

    Runtime names become file names: 1-64 letters, digits, ``_``, ``.`` or
    ``-``, starting with a letter or digit.
    """
    if not _NAME_RE.match(name):
        raise StateError(
            f"Invalid runtime name {name!r}: use 1-64 letters, digits, '_', '.' or '-', "
            "starting with a letter or digit"
        )
    return name


def safe_runtime_name(raw: str) -> str:
    """Turn *raw* (a manifest or directory name) into a valid runtime name."""
    cleaned = re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-._")[:64]
    return cleaned or "runtime"


# ---------------------------------------------------------------------------
# Directory and files
# ---------------------------------------------------------------------------


def _check_private(path: Path, *, is_dir: bool) -> None:
    """Refuse a state directory/file another user owns or can access (POSIX).

    A directory you own that is too open is tightened to ``0700``.
    """
    if os.name != "posix":
        return
    try:
        st = path.stat()
    except OSError as exc:
        raise StateError(f"Cannot read {path}: {exc}") from exc
    kind = "directory" if is_dir else "file"
    if is_dir and not stat.S_ISDIR(st.st_mode):
        raise StateError(f"Runtime state path {path} is not a directory")
    if not is_dir and not stat.S_ISREG(st.st_mode):
        raise StateError(f"Runtime state file {path} is not a regular file")
    if hasattr(os, "geteuid") and st.st_uid != os.geteuid():
        raise StateError(
            f"Refusing to use runtime state {kind} {path}: it is owned by uid {st.st_uid}, "
            f"not by you (uid {os.geteuid()}). Remove it, or set {STATE_DIR_ENV} to a "
            "directory of your own."
        )
    if stat.S_IMODE(st.st_mode) & 0o077:
        if is_dir:
            try:
                os.chmod(path, 0o700)
            except OSError as exc:
                raise StateError(
                    f"Runtime state directory {path} is accessible by other users "
                    f"(mode {stat.S_IMODE(st.st_mode):o}) and could not be fixed: {exc}. "
                    f"Run: chmod 700 {path}"
                ) from exc
        else:
            raise StateError(
                f"Refusing to use runtime state file {path}: other users can access it "
                f"(mode {stat.S_IMODE(st.st_mode):o}), so its token may be known. "
                "Delete it; the runtime writes a new one when it starts."
            )


def state_dir(*, create: bool = False) -> Path:
    """The state directory (checked; created with ``0700`` when *create*)."""
    override = os.environ.get(STATE_DIR_ENV)
    path = Path(override).expanduser() if override else Path.home() / ".promptise" / "runtime"
    if create:
        try:
            path.mkdir(mode=0o700, parents=True, exist_ok=True)
        except OSError as exc:
            raise StateError(f"Cannot create runtime state directory {path}: {exc}") from exc
    if path.exists():
        _check_private(path, is_dir=True)
    return path


def _state_file(name: str, directory: Path | None = None) -> Path:
    return (directory or state_dir()) / f"{validate_runtime_name(name)}.json"


def log_path(name: str) -> Path:
    """Log file of a detached runtime called *name*."""
    return state_dir(create=True) / f"{validate_runtime_name(name)}.log"


def _write_private(path: Path, text: str) -> None:
    """Write *text* to a new ``0600`` file at *path* (which must not exist)."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(text)
        fh.flush()
        os.fsync(fh.fileno())


def write_state(state: RuntimeState, *, exclusive: bool) -> Path:
    """Write *state* atomically to ``<state dir>/<name>.json`` (mode ``0600``).

    Args:
        state: What to record.
        exclusive: Claim the name: fail when a live runtime already holds
            it (a stale file is replaced).  ``False`` updates our own file.

    Raises:
        StateError: The name is taken by a running runtime, or the file
            can't be written.
    """
    directory = state_dir(create=True)
    target = _state_file(state.name, directory)
    tmp = directory / f".{state.name}.{os.getpid()}.{time.monotonic_ns()}.tmp"
    try:
        _write_private(tmp, json.dumps(asdict(state), indent=2))
        if not exclusive:
            for attempt in range(20):
                try:
                    os.replace(tmp, target)
                    return target
                except PermissionError:  # Windows: a reader has it open
                    if attempt == 19:
                        raise
                    time.sleep(0.05)
        for _attempt in range(2):
            try:
                if os.name == "posix":
                    os.link(tmp, target)  # fails when target exists
                else:
                    os.rename(tmp, target)  # Windows: fails when target exists
                    tmp = target
                return target
            except FileExistsError:
                other = _read_state_file(target)
                if other is not None and pid_alive(other.pid):
                    raise StateError(
                        f"A runtime named {state.name!r} is already running (pid {other.pid}). "
                        f"Stop it with 'promptise runtime stop {state.name}', or pick another "
                        "name with --runtime-name."
                    ) from None
                with contextlib.suppress(FileNotFoundError):
                    target.unlink()
        raise StateError(f"Could not claim runtime name {state.name!r}; try again")
    except OSError as exc:
        raise StateError(f"Cannot write runtime state file {target}: {exc}") from exc
    finally:
        if tmp != target:
            with contextlib.suppress(OSError):
                tmp.unlink()


def _read_state_file(path: Path) -> RuntimeState | None:
    """Parse a state file; ``None`` when it is unreadable or malformed."""
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict):
        return None
    try:
        known = {k: raw[k] for k in RuntimeState.__dataclass_fields__ if k in raw}
        state = RuntimeState(**known)
        state.pid = int(state.pid)
        state.port = int(state.port)
    except (TypeError, ValueError):
        return None
    return state


def _unlink(path: Path) -> bool:
    """Delete *path*; ``False`` when it can't be deleted right now.

    On Windows a file another process has open (a ``status`` or ``logs``
    call reading it) can't be deleted, so retry briefly.  A file left
    behind belongs to a dead process, which :func:`list_states` treats as
    stale and removes later, so callers never need to fail over it.
    """
    for attempt in range(20):
        try:
            path.unlink()
            return True
        except FileNotFoundError:
            return True
        except PermissionError:
            if not _WINDOWS or attempt == 19:
                return False
            time.sleep(0.05)
    return False


def remove_state(state: RuntimeState) -> None:
    """Remove *state*'s file, if it still belongs to *state*'s process."""
    try:
        path = _state_file(state.name)
    except StateError:
        return
    current = _read_state_file(path)
    if current is not None and current.pid != state.pid:
        return
    if not _unlink(path):
        logger.warning("Could not remove runtime state file %s; it is cleaned up later", path)


def list_states(*, cleanup: bool = True) -> tuple[list[RuntimeState], list[str]]:
    """The runtimes recorded in the state directory.

    Args:
        cleanup: Remove stale files (their process is gone, or they can't
            be parsed).

    Returns:
        ``(live states sorted by name, names of the stale files removed)``.

    Raises:
        StateError: The directory or a file fails the ownership checks.
    """
    directory = state_dir()
    if not directory.is_dir():
        return [], []
    live: list[RuntimeState] = []
    stale: list[str] = []
    for path in sorted(directory.glob("*.json")):
        _check_private(path, is_dir=False)
        state = _read_state_file(path)
        if state is not None and state.name == path.stem and pid_alive(state.pid):
            live.append(state)
            continue
        stale.append(path.stem)
        if cleanup:
            _unlink(path)
    return live, stale


def state_recorded(state: RuntimeState) -> bool:
    """Whether *state*'s file still exists and belongs to its process.

    A runtime removes its file as the last step of a clean shutdown, so
    ``False`` means it is done (or died) even while its PID is still
    visible (a zombie its parent hasn't reaped yet).
    """
    try:
        current = _read_state_file(_state_file(state.name))
    except StateError:
        return False
    return current is not None and current.pid == state.pid


def find_state(name: str) -> RuntimeState | None:
    """The live runtime called *name*, or ``None``."""
    for state in list_states()[0]:
        if state.name == name:
            return state
    return None


# ---------------------------------------------------------------------------
# Processes
# ---------------------------------------------------------------------------


def pid_alive(pid: int) -> bool:
    """Whether a process with *pid* exists (without signalling it)."""
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return bool(kernel32.GetLastError() == 5)  # ACCESS_DENIED: exists
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False
    return True


def wait_for_exit(pid: int, timeout: float, state: RuntimeState | None = None) -> bool:
    """Poll until *pid* is gone; ``True`` when it exited within *timeout*.

    With *state*, the runtime also counts as gone once it removed its state
    file (the last step of its shutdown).
    """

    def gone() -> bool:
        return not pid_alive(pid) or (state is not None and not state_recorded(state))

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if gone():
            return True
        time.sleep(0.1)
    return gone()


def terminate_process(pid: int) -> bool:
    """Ask *pid* to shut down gracefully (SIGTERM).

    Returns ``False`` where that isn't possible: on Windows, where
    terminating a process is always a hard kill (use :func:`kill_process`).
    """
    if sys.platform == "win32":
        return False
    with contextlib.suppress(ProcessLookupError):
        os.kill(pid, signal.SIGTERM)
    return True


def kill_process(pid: int) -> None:
    """Kill *pid* at once (SIGKILL; ``TerminateProcess`` on Windows)."""
    sig = getattr(signal, "SIGKILL", signal.SIGTERM)
    with contextlib.suppress(ProcessLookupError, OSError):
        os.kill(pid, sig)


def detach_popen_kwargs() -> dict[str, Any]:
    """``subprocess.Popen`` arguments that detach the child from this terminal."""
    if sys.platform == "win32":
        import subprocess

        flags = getattr(subprocess, "DETACHED_PROCESS", 0x8) | getattr(
            subprocess, "CREATE_NEW_PROCESS_GROUP", 0x200
        )
        return {"creationflags": flags}
    return {"start_new_session": True}
