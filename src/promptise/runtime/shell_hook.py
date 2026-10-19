"""ShellHook — runtime hook backed by an external command.

A :class:`ShellHook` is a runtime lifecycle hook that delegates the
work to an external program. The dispatcher serializes the
:class:`HookContext` to JSON, writes it to the program's stdin, runs
the program with a configurable timeout, and parses one JSON object
back from stdout.

Use this when:

* You want to write a hook in a non-Python language (Bash, Node, Go).
* The hook depends on external state that's easier to manage in a
  separate process.
* You declare hooks in a manifest (``hooks:`` section): each entry with a
  ``command`` becomes a ShellHook.

Safety:

* The command is run directly (``exec``), never through a shell, unless
  you pass ``shell=True`` in code (manifests can't).  The event payload
  is never interpolated into the command line: it arrives on stdin as
  JSON, and the event name and process name as the environment
  variables ``PROMPTISE_HOOK_EVENT`` and ``PROMPTISE_PROCESS``.
* The program gets a minimal environment (``PATH``, ``HOME``, locale and
  temp-dir variables, see :data:`SAFE_ENV_VARS`), plus the variables
  named in ``env_passthrough`` and the values in ``env``.  The rest of
  the host environment (API keys, tokens) is not passed unless
  ``inherit_env=True``.  The :class:`AgentProcess` redacts secret values
  from the payload before any hook sees it.
* ``timeout`` kills the program; stdout is limited to
  ``max_output_bytes`` (the program is killed beyond it) and stderr is
  truncated in error messages.

The JSON contract:

**Stdin (sent to the script):**

::

    {
      "event": "user_prompt_submit",
      "process_id": "support-bot",
      "timestamp": "2026-04-08T10:30:00+00:00",
      "data": {"prompt": "..."},
      "metadata": {}
    }

**Stdout (read from the script, optional):**

::

    {
      "block": false,            // optional, default false
      "reason": "",              // optional, used if block=true
      "data": {"prompt": "..."}, // optional, replaces ctx.data
      "log": "anything"          // optional, free-form
    }

If the script writes nothing, the hook is treated as a no-op success.
If it exits non-zero, times out, writes too much or writes something
that isn't a JSON object, the hook is treated as a *broken hook*: the
runtime logs the error and continues (or blocks, for a ``fail_closed``
hook).  Hook errors never take down a runtime — see
:meth:`HookManager.dispatch`.

To block an action via shell hook, output ``{"block": true,
"reason": "..."}``. The hook raises :class:`HookBlocked` and the hook
manager returns a ``blocked=True`` dispatch result.
"""

from __future__ import annotations

import asyncio
import contextlib
import errno
import json
import logging
import os
import shlex
from dataclasses import dataclass, field
from typing import Any

from .._spawn import resolve_executable, spawn_options
from .hooks import HookBlocked, HookContext

# Transient fork/spawn failures (EAGAIN, etc.) under heavy load are worth a
# brief retry instead of failing the hook outright.
_TRANSIENT_SPAWN_ERRNOS = {errno.EAGAIN, errno.EWOULDBLOCK, errno.ENOMEM}


logger = logging.getLogger(__name__)


#: Host environment variables a shell hook receives by default (when they
#: are set): what programs need to run, nothing that holds credentials.
SAFE_ENV_VARS: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "SHELL",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "LC_MESSAGES",
        "TZ",
        "TMPDIR",
        "TEMP",
        "TMP",
        "TERM",
        # Windows: needed to start most programs
        "SYSTEMROOT",
        "SYSTEMDRIVE",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMDATA",
        "PROGRAMFILES",
    }
)

#: Characters of stderr kept in an error message.
_STDERR_LIMIT = 2000


@dataclass
class ShellHookResult:
    """Parsed result returned by an external shell hook."""

    block: bool = False
    reason: str = ""
    data: dict[str, Any] | None = None
    log: str = ""
    raw: str = ""


@dataclass
class ShellHook:
    """Runtime hook that shells out to an external command.

    The command receives a JSON event on stdin and may write a JSON
    response to stdout. Use ``ShellHook.callback`` as the
    ``callback`` argument to :meth:`HookManager.register`.

    Attributes:
        command: The program and its arguments: a list (used as is) or a
            string (split with :func:`shlex.split`, so quoting works as
            in a terminal).  With ``shell=True`` a string run by
            ``/bin/sh -c``.
        timeout: Max seconds the command may run before being killed.
            Default 5.0.
        cwd: Working directory for the subprocess. Default current
            directory.
        env: Extra environment variables for the program (added to the
            minimal environment, see the module docstring).  Values may be
            ``${VAR}`` / ``${VAR:-default}`` references to the host
            environment, resolved each time the program runs.
        shell: If True, run via the shell instead of an exec.  Only for
            commands written in code: the command string is passed to the
            shell as is (the payload never is).
        encoding: stdin/stdout encoding. Default "utf-8".
        inherit_env: Pass the whole host environment (in 1.4.3 and earlier the
            default).  Off by default so credentials in the environment
            don't reach hook programs.
        env_passthrough: Names of host environment variables to pass
            through (when set), in addition to :data:`SAFE_ENV_VARS`.
        max_output_bytes: Largest stdout accepted (default 64 KiB); the
            program is killed and the hook fails beyond it.

    Example::

        hook = ShellHook(command="./hooks/audit_prompt.sh")
        hooks.register(HookEvent.USER_PROMPT_SUBMIT, hook.callback)

    The script ``audit_prompt.sh`` might look like::

        #!/usr/bin/env bash
        # Read JSON from stdin, log the prompt, allow it through.
        prompt=$(jq -r .data.prompt)
        echo "[$(date)] $prompt" >> /var/log/agent_prompts.log
        echo '{}'
    """

    command: str | list[str]
    timeout: float = 5.0
    cwd: str | None = None
    env: dict[str, str] | None = None
    shell: bool = False
    encoding: str = "utf-8"
    inherit_env: bool = False
    env_passthrough: list[str] = field(default_factory=list)
    max_output_bytes: int = 65_536

    def __post_init__(self) -> None:
        if isinstance(self.command, list):
            if not self.command or not all(isinstance(a, str) for a in self.command):
                raise ValueError("ShellHook.command must be a non-empty list of strings")
            if self.shell:
                raise ValueError("ShellHook: shell=True needs a command string, not a list")
        elif not isinstance(self.command, str) or not self.command.strip():
            raise ValueError("ShellHook.command is empty")
        if self.timeout <= 0:
            raise ValueError(f"ShellHook.timeout must be positive, got {self.timeout!r}")
        if self.max_output_bytes <= 0:
            raise ValueError("ShellHook.max_output_bytes must be positive")

    @property
    def _label(self) -> str:
        if isinstance(self.command, list):
            return shlex.join(self.command)
        return self.command

    async def callback(self, ctx: HookContext) -> None:
        """Async hook callback — pass this to :meth:`HookManager.register`.

        Serializes the context, runs the command, parses any JSON
        reply, and applies its directives (mutate ``ctx.data``, raise
        :class:`HookBlocked`).
        """
        result = await self.run(ctx)

        if result.data is not None:
            # Replace ctx.data in place so downstream code sees mutations
            ctx.data.clear()
            ctx.data.update(result.data)

        if result.log:
            logger.debug("shell hook for %s logged: %s", ctx.event.value, result.log[:1000])

        if result.block:
            raise HookBlocked(result.reason or "blocked by shell hook")

    async def run(self, ctx: HookContext) -> ShellHookResult:
        """Execute the underlying command and return the parsed result.

        Public so it can be unit-tested or invoked manually.

        Raises:
            TimeoutError: The program ran longer than ``timeout``.
            RuntimeError: It exited non-zero.
            ValueError: Its output was too large or not a JSON object.
        """
        payload = json.dumps(
            {
                "event": ctx.event.value,
                "process_id": ctx.process_id,
                "timestamp": ctx.timestamp.isoformat(),
                "data": ctx.data,
                "metadata": ctx.metadata,
            },
            default=str,
        ).encode(self.encoding)

        args: list[str] | None = None
        if not self.shell:
            args = (
                list(self.command) if isinstance(self.command, list) else shlex.split(self.command)
            )
            if not args:
                raise ValueError("ShellHook.command is empty")
            args = resolve_executable(args)
        options = spawn_options(self.cwd)
        env = self._child_env(ctx)

        # Spawn the subprocess, retrying a transient fork/spawn failure (e.g.
        # EAGAIN, "Resource temporarily unavailable") that can occur under heavy
        # concurrent load rather than failing the hook outright.
        proc = None
        for attempt in range(3):
            try:
                if self.shell:
                    proc = await asyncio.create_subprocess_shell(
                        str(self.command),
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=self.cwd,
                        env=env,
                        **options,
                    )
                else:
                    proc = await asyncio.create_subprocess_exec(
                        *args,  # type: ignore[misc]
                        stdin=asyncio.subprocess.PIPE,
                        stdout=asyncio.subprocess.PIPE,
                        stderr=asyncio.subprocess.PIPE,
                        cwd=self.cwd,
                        env=env,
                        **options,
                    )
                break
            except OSError as exc:
                if exc.errno in _TRANSIENT_SPAWN_ERRNOS and attempt < 2:
                    await asyncio.sleep(0.2 * (attempt + 1))
                    continue
                raise
        assert proc is not None  # nosec B101 - loop either sets proc or raises

        try:
            stdout_bytes, stderr_bytes = await asyncio.wait_for(
                self._communicate(proc, payload), timeout=self.timeout
            )
        except asyncio.TimeoutError as exc:
            await _kill(proc)
            raise TimeoutError(
                f"shell hook '{self._label}' exceeded timeout of {self.timeout}s"
            ) from exc
        except BaseException:
            await _kill(proc)
            raise

        if proc.returncode != 0:
            stderr_text = stderr_bytes.decode(self.encoding, errors="replace")
            raise RuntimeError(
                f"shell hook '{self._label}' exited {proc.returncode}: {stderr_text.strip()[:500]}"
            )

        stdout_text = stdout_bytes.decode(self.encoding, errors="replace").strip()
        if not stdout_text:
            return ShellHookResult(raw="")

        try:
            payload_out = json.loads(stdout_text)
        except json.JSONDecodeError as exc:
            raise ValueError(
                f"shell hook '{self._label}' produced invalid JSON: {stdout_text[:200]}"
            ) from exc

        if not isinstance(payload_out, dict):
            raise ValueError(
                f"shell hook '{self._label}' must return a JSON object, got "
                f"{type(payload_out).__name__}"
            )
        data = payload_out.get("data")
        if data is not None and not isinstance(data, dict):
            raise ValueError(
                f"shell hook '{self._label}': 'data' must be a JSON object, got "
                f"{type(data).__name__}"
            )

        return ShellHookResult(
            block=bool(payload_out.get("block", False)),
            reason=str(payload_out.get("reason", "")),
            data=data,
            log=str(payload_out.get("log", "")),
            raw=stdout_text,
        )

    async def _communicate(
        self, proc: asyncio.subprocess.Process, payload: bytes
    ) -> tuple[bytes, bytes]:
        """Write *payload* to stdin; read stdout (capped) and stderr (truncated)."""
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None

        async def write() -> None:
            assert proc.stdin is not None
            try:
                proc.stdin.write(payload)
                await proc.stdin.drain()
            except (BrokenPipeError, ConnectionResetError):
                pass  # the program doesn't read its stdin: fine
            finally:
                with contextlib.suppress(Exception):
                    proc.stdin.close()

        async def read_stdout() -> bytes:
            assert proc.stdout is not None
            chunks: list[bytes] = []
            size = 0
            while True:
                chunk = await proc.stdout.read(65_536)
                if not chunk:
                    return b"".join(chunks)
                size += len(chunk)
                if size > self.max_output_bytes:
                    raise ValueError(
                        f"shell hook '{self._label}' wrote more than "
                        f"{self.max_output_bytes} bytes to stdout"
                    )
                chunks.append(chunk)

        async def read_stderr() -> bytes:
            assert proc.stderr is not None
            kept = bytearray()
            while True:
                chunk = await proc.stderr.read(65_536)
                if not chunk:
                    return bytes(kept)
                if len(kept) < _STDERR_LIMIT:
                    kept.extend(chunk[: _STDERR_LIMIT - len(kept)])

        _, out, err = await asyncio.gather(write(), read_stdout(), read_stderr())
        await proc.wait()
        return out, err

    def _child_env(self, ctx: HookContext) -> dict[str, str]:
        """The program's environment: minimal (or inherited) plus ours."""
        if self.inherit_env:
            env = dict(os.environ)
        else:
            allowed = SAFE_ENV_VARS | set(self.env_passthrough)
            env = {k: v for k, v in os.environ.items() if k.upper() in allowed or k in allowed}
        if self.env:
            from promptise.env_resolver import resolve_env_var

            for key, value in self.env.items():
                env[str(key)] = resolve_env_var(str(value), context=f"hook env {key}")
        env["PROMPTISE_HOOK_EVENT"] = ctx.event.value
        env["PROMPTISE_PROCESS"] = ctx.process_id
        return env


async def _kill(proc: asyncio.subprocess.Process) -> None:
    """Kill *proc* (if still running), close its pipes and reap it.

    Closing the pipes matters: a program the hook started in the
    background (``cmd &``, a pipeline) can keep stdout open after the hook
    itself is killed, and asyncio only reports the exit once every pipe is
    closed.  Those programs get SIGPIPE on their next write.
    """
    if proc.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            proc.kill()
    transport = getattr(proc, "_transport", None)
    if transport is not None:
        with contextlib.suppress(Exception):
            transport.close()
    with contextlib.suppress(Exception):
        await asyncio.wait_for(proc.wait(), timeout=5.0)


__all__ = ["SAFE_ENV_VARS", "ShellHook", "ShellHookResult"]
