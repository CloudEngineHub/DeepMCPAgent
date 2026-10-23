"""Minimal template engine for prompt text.

Supports ``{variable}`` interpolation via :meth:`str.format_map`,
conditional blocks, loop blocks, template inclusion, literal brace
escapes, and **opt-in** shell command interpolation via the
``!`cmd``` syntax.

Example::

    rendered = render_template(
        "Summarize in {max_words} words: {text}",
        {"text": "long article...", "max_words": 100},
    )

Shell command interpolation is **disabled by default**. To enable,
pass a ``shell_executor`` (a callable that takes a command string
and returns its stdout) when constructing the
:class:`TemplateEngine`. The framework ships with
:class:`SubprocessShellExecutor` that runs the command via
``subprocess.run`` with a configurable timeout::

    engine = TemplateEngine(
        shell_executor=SubprocessShellExecutor(timeout=2.0),
    )
    out = engine.render(
        "Today is !`date +%Y-%m-%d`. Branch: !`git rev-parse --abbrev-ref HEAD`.",
        {},
    )

Without an explicit ``shell_executor`` the ``!`cmd``` syntax is
left in the rendered output unchanged — there is no implicit
shell access, ever.
"""

from __future__ import annotations

import errno
import re
import shlex
import string
import subprocess
import time
from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from .._spawn import resolve_executable, spawn_options

# fork()/spawn can transiently fail under heavy load ("Resource temporarily
# unavailable"); these are worth a brief retry rather than a hard failure.
_TRANSIENT_SPAWN_ERRNOS = {errno.EAGAIN, errno.EWOULDBLOCK, errno.ENOMEM}

__all__ = [
    "ShellExecutor",
    "ShellExecutionError",
    "SubprocessShellExecutor",
    "TemplateEngine",
    "render_template",
]


# ---------------------------------------------------------------------------
# Shell execution (opt-in)
# ---------------------------------------------------------------------------


class ShellExecutionError(RuntimeError):
    """Raised when a templated shell command fails or times out."""


@runtime_checkable
class ShellExecutor(Protocol):
    """Protocol for objects that can run a templated shell command.

    Implementations must be callable with a single string (the
    command) and return its stdout as a string. They are responsible
    for enforcing any sandboxing, timeouts, or allowlists.
    """

    def __call__(self, command: str) -> str: ...


class SubprocessShellExecutor:
    """Default shell executor backed by :func:`subprocess.run`.

    Runs the command via the system shell with a timeout. Captures
    stdout and returns it stripped. Stderr and a non-zero exit code
    raise :class:`ShellExecutionError`.

    Args:
        timeout: Max seconds the command may run before being killed.
            Default 5.0.
        shell: Whether to invoke through ``/bin/sh -c``. Default True
            (matches the ``!`cmd``` ergonomic). Ignored when an
            *allowlist* is set: allowlisted commands never run through
            a shell.
        cwd: Working directory for the subprocess. Default None
            (inherits caller's cwd).
        env: Optional environment variables to merge in.
        allowlist: Optional set of executable names that are allowed to
            run. When set, the command is split into words like a shell
            would (:func:`shlex.split`), its first word must be one of
            these names exactly (``"git"`` does not allow
            ``/usr/bin/git``), and it is run directly, without a shell.
            Shell syntax is refused rather than passed on: a command
            with an unquoted operator (``;``, ``|``, ``&``, ``<``,
            ``>``, ``(``, ``)``) raises :class:`ShellExecutionError`, so
            ``echo hi; rm -rf ~`` cannot run a second command. The
            arguments of an allowlisted command are not checked: allow
            only commands that cannot do harm whatever arguments they
            get (``git`` with ``-c``, ``find`` with ``-exec`` can run
            other programs).
    """

    def __init__(
        self,
        *,
        timeout: float = 5.0,
        shell: bool = True,
        cwd: str | None = None,
        env: dict[str, str] | None = None,
        allowlist: set[str] | None = None,
    ) -> None:
        self.timeout = timeout
        self.shell = shell
        self.cwd = cwd
        self.env = env
        self.allowlist = allowlist

    def __call__(self, command: str) -> str:
        cmd = command.strip()
        if not cmd:
            return ""
        use_shell = self.shell
        words: list[str] | None = None
        if self.allowlist is not None:
            # Fail closed: an allowlisted command never reaches /bin/sh, and
            # shell syntax is refused instead of silently passed as words.
            use_shell = False
            words = _allowlisted_argv(cmd, self.allowlist)

        merged_env = None
        if self.env is not None:
            import os

            merged_env = dict(os.environ)
            merged_env.update(self.env)

        # Without a shell the command line is tokenized here and its executable
        # resolved to a path, so the spawn can avoid fork() (see spawn_options).
        argv: str | list[str] = cmd
        if not use_shell:
            try:
                argv = resolve_executable(words if words is not None else shlex.split(cmd))
            except (ValueError, OSError) as exc:  # unbalanced quotes, unknown command
                raise ShellExecutionError(f"shell command failed to start: {exc}") from exc
        result = None
        for attempt in range(3):
            try:
                # Shell execution is an OPT-IN feature (requires explicitly wiring
                # a SubprocessShellExecutor into the TemplateEngine). Callers
                # control the commands via template authorship + optional
                # allowlist. This is intentionally a *feature*, not a
                # vulnerability — suppress bandit's generic warning here.
                result = subprocess.run(  # noqa: S602  # nosec B602
                    argv,
                    shell=use_shell,
                    capture_output=True,
                    text=True,
                    timeout=self.timeout,
                    cwd=self.cwd,
                    env=merged_env,
                    check=False,
                    **spawn_options(self.cwd),
                )
                break
            except subprocess.TimeoutExpired as exc:
                raise ShellExecutionError(
                    f"shell command timed out after {self.timeout}s: {cmd}"
                ) from exc
            except OSError as exc:
                # Retry a transient spawn failure (e.g. fork hitting EAGAIN under
                # heavy load); raise on a real error or once retries are spent.
                if exc.errno in _TRANSIENT_SPAWN_ERRNOS and attempt < 2:
                    time.sleep(0.2 * (attempt + 1))
                    continue
                raise ShellExecutionError(f"shell command failed to start: {exc}") from exc
        assert result is not None  # nosec B101 - loop either sets result or raises

        if result.returncode != 0:
            stderr = (result.stderr or "").strip()
            raise ShellExecutionError(
                f"shell command exited {result.returncode}: {cmd}\n{stderr[:500]}"
            )
        return (result.stdout or "").strip()


_SHELL_OPERATOR_CHARS = frozenset("();<>|&")


def _allowlisted_argv(cmd: str, allowlist: set[str]) -> list[str]:
    """Split *cmd* into words, refusing shell operators and executables not in *allowlist*."""
    try:
        lexer = shlex.shlex(cmd, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        tokens = list(lexer)
        words = shlex.split(cmd)
    except ValueError as exc:  # unbalanced quotes
        raise ShellExecutionError(f"shell command could not be parsed: {exc}") from exc
    for token in tokens:
        if token and set(token) <= _SHELL_OPERATOR_CHARS:
            raise ShellExecutionError(
                f"shell operator {token!r} is not allowed with a shell executor allowlist "
                "(allowlisted commands run without a shell)"
            )
    if not words:
        raise ShellExecutionError("empty shell command")
    if words[0] not in allowlist:
        raise ShellExecutionError(f"command {words[0]!r} not in shell executor allowlist")
    return words


class _SafeFormatter(string.Formatter):
    """Formatter that blocks attribute access and item indexing on values.

    Prevents Server-Side Template Injection (SSTI) via patterns like
    ``{obj.__class__.__init__.__globals__}`` which could leak secrets
    or execute code when using plain ``str.format_map()``.
    """

    def get_field(self, field_name: str, args: Any, kwargs: Any) -> Any:
        # Only allow simple field names — no dots, no brackets
        if "." in field_name or "[" in field_name:
            raise ValueError(f"Attribute/index access not allowed in templates: {field_name!r}")
        return super().get_field(field_name, args, kwargs)


# ---------------------------------------------------------------------------
# Regex patterns for template directives
# ---------------------------------------------------------------------------

_IF_PATTERN = re.compile(
    r"\{%\s*if\s+(\w+)\s*%\}(.*?)\{%\s*endif\s*%\}",
    re.DOTALL,
)

_IF_ELSE_PATTERN = re.compile(
    r"\{%\s*if\s+(\w+)\s*%\}(.*?)\{%\s*else\s*%\}(.*?)\{%\s*endif\s*%\}",
    re.DOTALL,
)

_FOR_PATTERN = re.compile(
    r"\{%\s*for\s+(\w+)\s+in\s+(\w+)\s*%\}(.*?)\{%\s*endfor\s*%\}",
    re.DOTALL,
)

_INCLUDE_PATTERN = re.compile(
    r'\{%\s*include\s+"([^"]+)"\s*%\}',
)

# Shell command interpolation: !`some command here`
# Captures the command between the backticks. The leading ``!`` is
# required so that ordinary backtick code blocks (like markdown
# inline code) are NOT interpreted as commands.
_SHELL_PATTERN = re.compile(r"!`([^`\n]+)`")

# Temporary placeholders for literal braces
_LBRACE = "\x00LBRACE\x00"
_RBRACE = "\x00RBRACE\x00"


class TemplateEngine:
    """Template engine with include registry and optional shell interpolation.

    Args:
        includes: Mapping of template names to template text.  Used
            by ``{% include "name" %}`` directives.
        shell_executor: Optional callable that runs a shell command
            and returns its stdout. When provided, occurrences of
            ``!`cmd``` in templates are replaced with the command's
            output. **Disabled by default** — leave as ``None`` to
            keep the syntax literal. Set to a
            :class:`SubprocessShellExecutor` (or any callable) to
            enable. Only enable when the template source is trusted.
    """

    def __init__(
        self,
        includes: dict[str, str] | None = None,
        *,
        shell_executor: ShellExecutor | Callable[[str], str] | None = None,
    ) -> None:
        self._includes: dict[str, str] = includes or {}
        self._shell_executor: ShellExecutor | Callable[[str], str] | None = shell_executor

    def render(self, template: str, variables: dict[str, Any]) -> str:
        """Render *template* with *variables*.

        Processing order:

        1. ``{% include "name" %}`` — resolved from *includes* registry
        2. ``!`shell command``` — replaced with stdout (only if a
           ``shell_executor`` was provided to the engine; otherwise
           the syntax is left as-is).  The output is literal text:
           braces in it are kept as they are, never read as
           ``{variable}`` or ``{% %}`` syntax.
        3. ``{% if condition %}...{% else %}...{% endif %}`` — truthiness
        4. ``{% for item in items %}...{% endfor %}`` — iteration
        5. ``{{`` / ``}}`` — literal brace escapes
        6. ``{variable}`` — interpolation via :meth:`str.format_map`

        Raises:
            KeyError: A referenced variable is missing from *variables*.
            ValueError: An ``{% include %}`` references an unknown name.
            ShellExecutionError: A ``!`cmd``` block fails (only when
                a shell executor is configured).
        """
        text = template

        # 1. Includes
        text = self._resolve_includes(text)

        # 2. Shell command interpolation (opt-in)
        if self._shell_executor is not None:
            text = self._resolve_shell_commands(text)

        # 3. Conditionals (if/else first, then plain if)
        text = self._resolve_if_else(text, variables)
        text = self._resolve_if(text, variables)

        # 4. Loops
        text = self._resolve_for(text, variables)

        # 5. Escape literal braces: {{ → { and }} → }
        text = text.replace("{{", _LBRACE).replace("}}", _RBRACE)

        # 6. Variable interpolation (safe — no attribute/index access)
        text = _SafeFormatter().vformat(text, (), variables)

        # 7. Restore literal braces
        text = text.replace(_LBRACE, "{").replace(_RBRACE, "}")

        return text

    def _resolve_shell_commands(self, text: str) -> str:
        """Replace every ``!`cmd``` occurrence with the command's stdout."""
        executor = self._shell_executor
        assert executor is not None  # caller already checked

        def _replace(m: re.Match[str]) -> str:
            cmd = m.group(1)
            output = executor(cmd)
            # Literal text: braces must not become template syntax (a
            # KeyError, or a {variable} the command's output names).
            return str(output).replace("{", _LBRACE).replace("}", _RBRACE)

        return _SHELL_PATTERN.sub(_replace, text)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _resolve_includes(self, text: str) -> str:
        def _replace(m: re.Match[str]) -> str:
            name = m.group(1)
            if name not in self._includes:
                raise ValueError(f"Template include {name!r} not found in includes registry")
            return self._includes[name]

        return _INCLUDE_PATTERN.sub(_replace, text)

    @staticmethod
    def _resolve_if_else(text: str, variables: dict[str, Any]) -> str:
        def _replace(m: re.Match[str]) -> str:
            condition = m.group(1)
            true_block = m.group(2)
            false_block = m.group(3)
            if variables.get(condition):
                return true_block
            return false_block

        return _IF_ELSE_PATTERN.sub(_replace, text)

    @staticmethod
    def _resolve_if(text: str, variables: dict[str, Any]) -> str:
        def _replace(m: re.Match[str]) -> str:
            condition = m.group(1)
            body = m.group(2)
            if variables.get(condition):
                return body
            return ""

        return _IF_PATTERN.sub(_replace, text)

    @staticmethod
    def _resolve_for(text: str, variables: dict[str, Any]) -> str:
        def _replace(m: re.Match[str]) -> str:
            item_name = m.group(1)
            collection_name = m.group(2)
            body = m.group(3)
            collection = variables.get(collection_name)
            if collection is None:
                raise KeyError(f"Loop variable {collection_name!r} not found in variables")
            # Use regex to match only {item_name} that is a standalone
            # variable reference (not preceded by another { or followed
            # by another }).  This prevents replacing {item} inside
            # escaped braces or unrelated text.
            pattern = re.compile(r"(?<!\{)\{" + re.escape(item_name) + r"\}(?!\})")
            parts: list[str] = []
            for item in collection:
                rendered = pattern.sub(str(item), body)
                parts.append(rendered)
            return "".join(parts)

        return _FOR_PATTERN.sub(_replace, text)


def render_template(
    template: str,
    variables: dict[str, Any],
    includes: dict[str, str] | None = None,
    *,
    shell_executor: ShellExecutor | Callable[[str], str] | None = None,
) -> str:
    """Convenience function — create a :class:`TemplateEngine` and render.

    Args:
        template: Template text with ``{variable}`` placeholders.
        variables: Values to substitute.
        includes: Optional mapping of template names for
            ``{% include "name" %}`` directives.
        shell_executor: Optional callable enabling ``!`cmd``` shell
            interpolation. Disabled (None) by default.

    Returns:
        Rendered text.
    """
    engine = TemplateEngine(includes=includes, shell_executor=shell_executor)
    return engine.render(template, variables)
