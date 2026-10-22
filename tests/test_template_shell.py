"""Tests for opt-in shell command interpolation in TemplateEngine."""

from __future__ import annotations

import shlex
import sys

import pytest

from promptise.prompts.template import (
    ShellExecutionError,
    SubprocessShellExecutor,
    TemplateEngine,
    render_template,
)


class TestShellInterpolationDisabledByDefault:
    def test_syntax_left_alone_when_no_executor(self):
        """Without a shell_executor, !`cmd` is just literal text."""
        out = render_template("Today is !`date`", {})
        assert out == "Today is !`date`"  # untouched

    def test_render_template_helper_default(self):
        out = render_template("hello !`whoami`", {})
        assert "!`whoami`" in out


# These tests check what a command returns, not how fast: a loaded Windows
# runner can take seconds just to start cmd.exe.
_GENEROUS = 30.0


class TestShellInterpolationOptIn:
    def test_explicit_executor_runs_command(self):
        engine = TemplateEngine(
            shell_executor=lambda cmd: f"<{cmd}>",
        )
        out = engine.render("Today is !`date`. Branch: !`git branch`.", {})
        assert out == "Today is <date>. Branch: <git branch>."

    def test_subprocess_executor_basic_echo(self):
        engine = TemplateEngine(shell_executor=SubprocessShellExecutor(timeout=_GENEROUS))
        out = engine.render("hello !`echo world`", {})
        assert out == "hello world"

    def test_subprocess_executor_multiple_commands(self):
        engine = TemplateEngine(shell_executor=SubprocessShellExecutor(timeout=_GENEROUS))
        out = engine.render("a=!`echo 1`, b=!`echo 2`", {})
        assert out == "a=1, b=2"

    def test_subprocess_executor_failure_raises(self):
        engine = TemplateEngine(shell_executor=SubprocessShellExecutor(timeout=_GENEROUS))
        with pytest.raises(ShellExecutionError, match="exited"):
            engine.render("oops: !`false`", {})

    def test_subprocess_executor_timeout(self):
        engine = TemplateEngine(shell_executor=SubprocessShellExecutor(timeout=0.1))
        with pytest.raises(ShellExecutionError, match="timed out"):
            engine.render("!`sleep 5`", {})

    def test_subprocess_executor_allowlist_blocks_others(self):
        executor = SubprocessShellExecutor(timeout=_GENEROUS, allowlist={_PYTHON})
        engine = TemplateEngine(shell_executor=executor)

        # Allowed command works
        assert engine.render(f"!`{_PYTHON_CMD} -c 'print(\"ok\")'`", {}) == "ok"

        # Disallowed command is blocked
        with pytest.raises(ShellExecutionError, match="not in shell executor allowlist"):
            engine.render("!`whoami`", {})


# An allowlisted command runs without a shell, so it must be a real program
# (``echo`` is a cmd.exe builtin on Windows). Forward slashes: shlex treats
# backslashes as escapes.
_PYTHON = sys.executable.replace("\\", "/")
_PYTHON_CMD = shlex.quote(_PYTHON)


class TestAllowlistCannotBeBypassed:
    """With an allowlist, nothing reaches a shell (security regression tests)."""

    def _engine(self) -> TemplateEngine:
        return TemplateEngine(
            shell_executor=SubprocessShellExecutor(timeout=_GENEROUS, allowlist={_PYTHON})
        )

    @pytest.mark.parametrize(
        "suffix",
        ["; whoami", "&& whoami", "|| whoami", "| whoami", "> owned.txt", "& whoami", "$(whoami)"],
    )
    def test_shell_operators_are_refused(self, suffix: str, tmp_path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        with pytest.raises(ShellExecutionError, match="shell operator"):
            self._engine().render(f"!`{_PYTHON_CMD} -c 'print(1)' {suffix}`", {})
        assert not (tmp_path / "owned.txt").exists()

    def test_quoted_operators_are_plain_arguments(self) -> None:
        out = self._engine().render(
            f"!`{_PYTHON_CMD} -c 'import sys; print(sys.argv[1])' 'a; b | c'`", {}
        )
        assert out == "a; b | c"

    def test_variables_are_not_expanded(self) -> None:
        out = self._engine().render(
            f"!`{_PYTHON_CMD} -c 'import sys; print(sys.argv[1])' $HOME`", {}
        )
        assert out == "$HOME"

    def test_shell_true_is_ignored_with_an_allowlist(self) -> None:
        executor = SubprocessShellExecutor(timeout=_GENEROUS, shell=True, allowlist={"echo"})
        with pytest.raises(ShellExecutionError, match="shell operator"):
            executor("echo hi; echo injected")

    def test_path_to_an_allowlisted_name_is_refused(self) -> None:
        executor = SubprocessShellExecutor(allowlist={"python"})
        with pytest.raises(ShellExecutionError, match="not in shell executor allowlist"):
            executor("/tmp/python -c 1")

    def test_unbalanced_quotes_are_refused(self) -> None:
        with pytest.raises(ShellExecutionError, match="could not be parsed"):
            SubprocessShellExecutor(allowlist={"echo"})("echo 'oops")


class TestCommandOutputIsLiteral:
    def test_braces_in_output_are_kept(self) -> None:
        engine = TemplateEngine(shell_executor=lambda cmd: '{"a": 1} {secret}')
        assert engine.render("out: !`x` {name}", {"name": "n", "secret": "S"}) == (
            'out: {"a": 1} {secret} n'
        )

    def test_output_is_not_template_syntax(self) -> None:
        engine = TemplateEngine(shell_executor=lambda cmd: "{% if flag %}yes{% endif %}")
        assert engine.render("!`x`", {"flag": True}) == "{% if flag %}yes{% endif %}"


class TestShellInterpolationOrderOfOperations:
    def test_runs_after_includes(self):
        # Include resolves first, then shell command from inside the include
        engine = TemplateEngine(
            includes={"banner": "[!`echo BANNER`]"},
            shell_executor=lambda cmd: f"X{cmd}X",
        )
        out = engine.render('{% include "banner" %}', {})
        assert out == "[Xecho BANNERX]"

    def test_does_not_break_variable_interpolation(self):
        engine = TemplateEngine(shell_executor=lambda cmd: "RESULT")
        out = engine.render("user={name}, time=!`date`", {"name": "alice"})
        assert out == "user=alice, time=RESULT"

    def test_only_bang_backtick_is_intercepted(self):
        # A bare backtick (e.g. inline markdown code) should NOT be touched.
        engine = TemplateEngine(shell_executor=lambda cmd: "EXEC")
        out = engine.render("Use the `git status` command, not !`git status`.", {})
        # Backtick code stays, bang-backtick is replaced
        assert out == "Use the `git status` command, not EXEC."
