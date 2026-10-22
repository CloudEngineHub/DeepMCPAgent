---
seo_title: "Shell Command Context in Prompt Templates"
description: "Inject live system context into prompt templates by running shell commands at render time, off by default, with an allowlist, timeouts and custom executors."
---

# Shell Context Injection in Templates

Inject live system context into prompt templates by running shell commands at render time. The `!`cmd`` syntax executes a command and replaces itself with stdout.

```python
from promptise.prompts import TemplateEngine, SubprocessShellExecutor

engine = TemplateEngine(
    shell_executor=SubprocessShellExecutor(timeout=2.0),
)

out = engine.render(
    "Today is !`date +%Y-%m-%d`. Branch: !`git rev-parse --abbrev-ref HEAD`.",
    {},
)
# "Today is 2026-04-11. Branch: main."
```

---

## Disabled by default

Without an explicit `shell_executor`, the `!`cmd`` syntax is left as **literal text**: there is no implicit shell access. `@prompt` templates, `PromptBuilder` and `.prompt` files render without an executor, so shell interpolation only happens where you call `TemplateEngine` or `render_template(..., shell_executor=...)` yourself.

```python
# No executor → syntax stays literal
engine = TemplateEngine()
out = engine.render("Today is !`date`", {})
# "Today is !`date`"
```

---

## SubprocessShellExecutor

The built-in executor runs commands via the system shell with a configurable timeout:

```python
from promptise.prompts import SubprocessShellExecutor

executor = SubprocessShellExecutor(
    timeout=5.0,          # max seconds per command (default 5)
    shell=True,           # run via /bin/sh -c (default True; ignored with an allowlist)
    cwd="/path/to/repo",  # working directory (default: inherited)
    env={"API_KEY": "x"}, # extra env vars (merged with os.environ)
)
```

With `shell=False` the command line is tokenized like a terminal would
(`shlex.split`) and its executable resolved on `PATH`. Unless `cwd` is set,
the process is started without `fork()` (CPython's `posix_spawn` path): a
forked child of a multithreaded process that has used macOS system frameworks
(any `httpx` client does) can die with SIGSEGV before `exec`, which surfaces
as `exited -11`. Setting `cwd` forces the fork path, so prefer running the
command with an absolute path over changing directory.

### Allowlist

When `allowlist` is set, a command runs only if its first word is one of the allowlisted executable names, and it never runs through a shell, whatever `shell` says. The command line is split into words like a shell would (`shlex.split`, so quotes work), and the program is started directly:

```python
from promptise.prompts import ShellExecutionError, SubprocessShellExecutor, TemplateEngine

executor = SubprocessShellExecutor(allowlist={"git", "date"})
engine = TemplateEngine(shell_executor=executor)

print(engine.render("Branch: !`git rev-parse --abbrev-ref HEAD`", {}))   # Branch: main

for command in ["rm -rf /tmp/x", "git status; rm -rf /tmp/x"]:
    try:
        engine.render(f"!`{command}`", {})
    except ShellExecutionError as exc:
        print(exc)
# command 'rm' not in shell executor allowlist
# shell operator ';' is not allowed with a shell executor allowlist (allowlisted commands run without a shell)
```

What the allowlist enforces:

- **Shell syntax is refused**, not passed on. An unquoted `;`, `|`, `&`, `&&`, `||`, `<`, `>`, `(` or `)` raises `ShellExecutionError`, so a second command can't be chained, piped or substituted (`$(...)`). Quoted, these characters are ordinary text in an argument (`git log --grep 'a;b'`). `$HOME` and globs such as `*` are not expanded.
- **The first word must match exactly**: `{"git"}` allows `git ...` (found on `PATH`) but not `/usr/bin/git` or `./git`.
- **Programs only**: shell builtins can't be allowlisted, since no shell runs them (`echo` works on Linux and macOS, where it is also a program, but not on Windows, where it is a `cmd.exe` builtin).
- **Arguments are not checked.** Allowlist only programs that can't do harm whatever arguments they get: `git -c core.pager=...`, `find -exec` or an interpreter (`python -c`) run other code.

---

## Custom executors

Any synchronous callable `(str) -> str` works as a shell executor (the `ShellExecutor` protocol):

```python
from promptise.prompts import TemplateEngine

# Dry-run executor for testing
engine = TemplateEngine(shell_executor=lambda cmd: f"[would run: {cmd}]")
print(engine.render("Status: !`git status --short`", {}))
# Status: [would run: git status --short]
```

The engine calls the executor synchronously, so an `async def` executor does not work: its coroutine would be inserted as text. Run async work before rendering and pass the result as a variable instead.

---

## Processing order

Shell interpolation runs **after** template includes and **before** conditionals and variable substitution. A variable's value is never executed, because commands run before variables are substituted. This means:

1. `{% include "name" %}`: resolved first (included templates can contain `!`cmd``)
2. `!`cmd``: executed, stdout replaces the block
3. `{% if %}`, `{% for %}`: conditionals and loops
4. `{variable}`: variable interpolation

---

## Error handling

- **Non-zero exit code** → raises `ShellExecutionError`
- **Timeout** → raises `ShellExecutionError`
- **Command not in allowlist, or shell syntax with an allowlist** → raises `ShellExecutionError`

All errors propagate to the caller: there is no silent fallback. A command that can't start (unknown executable with `shell=False`, unbalanced quotes) also raises `ShellExecutionError`. Empty commands render as an empty string.

Command output is literal text: braces in it are kept as they are and never read as `{variable}` or `{% if %}` syntax, so output such as `{"ok": true}` renders unchanged and can't pull a template variable into the prompt.

## Parameter reference

`SubprocessShellExecutor(*, timeout=5.0, shell=True, cwd=None, env=None, allowlist=None)`

| Parameter | Default | Description |
|-----------|---------|-------------|
| `timeout` | `5.0` | Seconds before the command is killed (`ShellExecutionError`) |
| `shell` | `True` | Run through `/bin/sh -c`; `False` splits with `shlex` and runs the executable directly. Ignored with an `allowlist` (never a shell) |
| `cwd` | `None` | Working directory (inherits the caller's) |
| `env` | `None` | Variables merged over `os.environ` |
| `allowlist` | `None` | Set of allowed executable names. The command runs without a shell; any other first word, or an unquoted shell operator, raises `ShellExecutionError` |

The executor returns stdout with surrounding whitespace stripped. A non-zero exit raises `ShellExecutionError` with the exit code and up to 500 characters of stderr.

---

## Related

- [Loader & Templates](loader-templates.md): the full template engine reference
- [Context & Variables](context.md): dynamic context injection via providers
