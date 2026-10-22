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
    shell=True,           # run via /bin/sh -c (default True)
    cwd="/path/to/repo",  # working directory (default: inherited)
    env={"API_KEY": "x"}, # extra env vars (merged with os.environ)
    allowlist={"echo", "date", "git"},  # restrict allowed commands
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

When `allowlist` is set, only commands whose first token matches are permitted:

```python
from promptise.prompts import ShellExecutionError, SubprocessShellExecutor, TemplateEngine

executor = SubprocessShellExecutor(allowlist={"echo", "git"})
engine = TemplateEngine(shell_executor=executor)

print(engine.render("!`echo hello`", {}))   # hello

try:
    engine.render("!`rm -rf /tmp/x`", {})
except ShellExecutionError as exc:
    print(exc)                               # command 'rm' not in shell executor allowlist
```

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
- **Command not in allowlist** → raises `ShellExecutionError`

All errors propagate to the caller: there is no silent fallback. A command that can't start (unknown executable with `shell=False`, unbalanced quotes) also raises `ShellExecutionError`. Empty commands render as an empty string.

!!! warning "Braces in command output"
    Command output is inserted before variable interpolation, so `{` and `}` in the output are read as template syntax: output such as `{"ok": true}` raises `KeyError` or `ValueError`. Pass such output as a variable instead (run the command yourself and use `{status}`).

!!! warning "The allowlist checks the first word only"
    With `shell=True` (the default) the whole line goes to `/bin/sh`, so `echo hi; rm -rf /tmp/x` passes an allowlist containing `echo`. When templates are not fully trusted, use `shell=False` together with an allowlist: the command is split with `shlex` and run without a shell, so `;`, `|` and `&&` are plain arguments.

## Parameter reference

`SubprocessShellExecutor(*, timeout=5.0, shell=True, cwd=None, env=None, allowlist=None)`

| Parameter | Default | Description |
|-----------|---------|-------------|
| `timeout` | `5.0` | Seconds before the command is killed (`ShellExecutionError`) |
| `shell` | `True` | Run through `/bin/sh -c`; `False` splits with `shlex` and runs the executable directly |
| `cwd` | `None` | Working directory (inherits the caller's) |
| `env` | `None` | Variables merged over `os.environ` |
| `allowlist` | `None` | Set of allowed first words; any other command raises `ShellExecutionError` |

The executor returns stdout with surrounding whitespace stripped. A non-zero exit raises `ShellExecutionError` with the exit code and up to 500 characters of stderr.

---

## Related

- [Loader & Templates](loader-templates.md): the full template engine reference
- [Context & Variables](context.md): dynamic context injection via providers
