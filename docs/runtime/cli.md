---
seo_title: "promptise runtime CLI: Run Agent Processes"
description: "Run agent processes from .agent manifests and control them from another shell: promptise runtime start, status, stop, restart, logs, rewind, validate, init."
---

# Runtime CLI

The `promptise runtime` command group runs agent processes from `.agent` manifests and controls them from another terminal. `start` runs a runtime (in the foreground or in the background); `status`, `stop`, `restart`, `logs` and `rewind` find it on their own and talk to its control API. `validate` and `init` work on manifest files.

```bash
# Start an agent in the background
promptise runtime start agents/watcher.agent --detach

# From any terminal on this machine
promptise runtime status
promptise runtime logs data-watcher --follow
promptise runtime restart
promptise runtime stop
```

---

## How the commands find a runtime

`promptise runtime start` serves the [Orchestration API](api.md) on `127.0.0.1` (a free port unless `--api-port` is given) with a random bearer token, and records how to reach it in a **state file**: `~/.promptise/runtime/<runtime name>.json`. The file holds the PID, host, port, token, the manifest paths, each process's journal location, the start time and whether startup finished. `status`, `stop`, `restart`, `logs` and `rewind` read it. With one runtime running they need no name.

The state directory holds tokens, so it is private:

- the directory is created with mode `0700`, each file with `0600`;
- on POSIX, a directory or file owned by another user is refused, and so is a state file that other users can read or write; a directory you own that is too open is tightened to `0700`;
- a state file whose process is gone (a crash, `kill -9`) is stale and removed the next time a command lists the directory;
- the runtime removes its own file when it exits.

| Environment variable | Effect |
|---|---|
| `PROMPTISE_RUNTIME_STATE_DIR` | Use this state directory instead of `~/.promptise/runtime` |
| `PROMPTISE_RUNTIME_TOKEN` | `start`: use this API token instead of a random one (`start` then removes the variable from its own environment, so agents, hooks and stdio MCP servers don't inherit it). Client commands: the token for `--url` |

### A runtime on another machine

Every client command also takes `--url` (the base URL of any Orchestration API) and `--token`. Prefer `PROMPTISE_RUNTIME_TOKEN` over `--token`: a token on the command line is visible to other users in `ps`. `--token` without `--url` is an error, because a local runtime's token comes from its state file.

```bash
export PROMPTISE_RUNTIME_TOKEN=...       # the token the remote runtime was started with
promptise runtime status --url http://10.0.0.5:9100
promptise runtime logs worker --url http://10.0.0.5:9100
```

To control a runtime remotely, start it with `--api-host` (for example `0.0.0.0`) and a known `PROMPTISE_RUNTIME_TOKEN`, and put it behind TLS when the token crosses an untrusted network.

---

## promptise runtime start

Start agent process(es) from a `.agent` manifest or a directory of them.

```bash
promptise runtime start agents/watcher.agent                 # foreground
promptise runtime start agents/                              # every .agent file in a directory
promptise runtime start agents/watcher.agent --name my-watcher
promptise runtime start agents/ --runtime-name prod --detach # background
promptise runtime start agents/ --dashboard                  # live terminal dashboard
```

| Argument / option | Short | Default | Description |
|---|---|---|---|
| `MANIFEST` | | required | Path to a `.agent` file or a directory of `.agent` files |
| `--name` | `-n` | -- | Override the process name (single manifest file only) |
| `--runtime-name` | `-r` | process name (file) or directory name | Name of this runtime for `status`, `stop`, `restart`, `logs` |
| `--detach/--no-detach` | `-d` | off | Run in the background; print the runtime name, PID and log file |
| `--dashboard/--no-dashboard` | | off | Live monitoring dashboard (foreground only) |
| `--api-host` | | `127.0.0.1` | Address the control API listens on |
| `--api-port` | | `0` | Port of the control API (`0`: a free port) |
| `--allow-command-deploys` | | off | Let the control API deploy processes with stdio MCP servers, command hooks or open-mode tool creation |
| `--allow-env-references` | | off | Let control-API deploys use `${VAR}` references to any environment variable of this machine |
| `--allow-env-var NAME` | | -- | Let control-API deploys reference this one variable as `${NAME}`; repeatable |

In the foreground, `start` loads the manifests, starts the control API, writes the state file, starts every process and prints a status table:

```
Loaded process: notes
                        Agent Processes
┏━━━━━━━┳━━━━━━━━━┳━━━━━━━━━━┳━━━━━━━━━━━━━┳━━━━━━━┳━━━━━━━━━━┓
┃ Name  ┃ State   ┃ ID       ┃ Invocations ┃ Queue ┃ Uptime   ┃
┡━━━━━━━╇━━━━━━━━━╇━━━━━━━━━━╇━━━━━━━━━━━━━╇━━━━━━━╇━━━━━━━━━━┩
│ notes │ RUNNING │ 51a7550e │           0 │     0 │ 0h 0m 0s │
└───────┴─────────┴──────────┴─────────────┴───────┴──────────┘

Runtime notes (pid 23006), control API on http://127.0.0.1:53367
Press Ctrl+C to stop all processes, or run 'promptise runtime stop notes' from another terminal.
```

It runs until Ctrl+C, SIGTERM (`docker stop`, a process manager) or `promptise runtime stop`. Each stops every process gracefully (final checkpoint, dead letters, `process.stopped`), closes the API, removes the state file and exits with 0. A second Ctrl+C or SIGTERM during the shutdown exits at once with code 130.

A process that fails to start is reported and the others keep running. When none can start, the runtime shuts down with exit code 1.

**Runtime names** are 1-64 letters, digits, `_`, `.` or `-`, starting with a letter or digit; a default taken from a file or directory name is cleaned up to fit. A name is claimed when the runtime starts: a second `start` with the name of a running runtime fails (`A runtime named 'notes' is already running (pid 18457)`), so pick another with `--runtime-name`.

**`--detach`** starts the same command in the background (a new session, detached from the terminal), with its output in a log file next to the state file: `~/.promptise/runtime/<runtime name>.log` (mode `0600`, overwritten by the next detached start). It waits until startup has finished (every process started or reported as failed), then prints:

```
Started runtime notes in the background
  PID:         18457
  Processes:   notes
  Control API: http://127.0.0.1:52276
  Log file:    /home/me/.promptise/runtime/notes.log
  Status:      promptise runtime status notes
  Stop it:     promptise runtime stop notes
```

When the background runtime exits during startup, `start` prints the last 20 lines of its log and exits with 1. When it isn't ready after 60 seconds, `start` says so and exits with 0 (the runtime keeps starting; check `status` and the log). `--detach` can't be combined with `--dashboard`.

**`--allow-command-deploys`.** By default the control API refuses `POST /api/v1/processes` for a config with a stdio MCP server, a command [hook](hooks.md) or open mode with `allow_tool_creation` (`403 COMMANDS_NOT_ALLOWED`): anyone with the token could otherwise run any command on the machine. The flag lifts that; use it only when every token holder may run commands there. The manifests you start are never affected.

**`--allow-env-references` and `--allow-env-var NAME`.** By default the control API refuses a deploy (or a trigger or `PATCH` body) with a `${VAR}` reference (`403 ENV_REFERENCES_NOT_ALLOWED`), because the deployed agent could read the value back through a secret. `--allow-env-var SEARCH_API_KEY` (repeatable) allows those variables only; `--allow-env-references` allows any. Both are passed on to a `--detach` runtime. Paths in deployed configs (journal, memory, watch paths) must lie in `./.promptise` under the directory you start the runtime in (`403 PATH_NOT_ALLOWED`). See [Securing the control API](api.md#securing-the-control-api).

---

## promptise runtime status

Show the processes of running runtime(s), or the details of one process.

```bash
promptise runtime status                 # every runtime on this machine
promptise runtime status prod            # one runtime
promptise runtime status data-watcher    # a process name works too
promptise runtime status prod --process data-watcher
promptise runtime status --json
```

| Argument / option | Short | Description |
|---|---|---|
| `NAME` | | Runtime name, or a process name in a running runtime (omit for every runtime) |
| `--process` | `-p` | Show this one process instead of the whole runtime |
| `--json` | | Output as JSON: an object for one runtime or process, a list of runtimes otherwise |
| `--url`, `--token` | | Talk to an Orchestration API instead of a local runtime |

Each runtime is shown with its name, PID, API URL and start time (`(starting)` while processes are still starting), then its process table. The JSON output has `runtime` (name, pid, url, started_at, ready, manifests, log_file; never the token) and `status` (or `process`).

---

## promptise runtime stop

Stop a running runtime (every process, then the runtime exits), or one process.

```bash
promptise runtime stop                      # the only running runtime
promptise runtime stop prod
promptise runtime stop prod --process data-watcher
promptise runtime stop prod --timeout 60 --force
```

| Argument / option | Short | Default | Description |
|---|---|---|---|
| `NAME` | | -- | Runtime to stop, or a process name in a running runtime; may be omitted when only one runtime runs |
| `--process` | `-p` | -- | Stop only this process (the runtime keeps running) |
| `--force/--no-force` | | off | Kill the runtime if it hasn't stopped gracefully |
| `--timeout` | | `30.0` | Seconds to wait for a graceful shutdown |
| `--url`, `--token` | | -- | Talk to an Orchestration API instead of a local runtime |

How a runtime is stopped:

1. `stop` asks it to shut down over its control API (`POST /api/v1/runtime/shutdown`) and waits up to `--timeout` seconds for it to exit.
2. When the API can't be reached or the runtime is still running, it sends SIGTERM and waits again (up to 10 more seconds after a successful request, `--timeout` otherwise). Windows has no catchable SIGTERM, so this step is skipped there.
3. Still running: with `--force` the process is killed (SIGKILL) and its state file removed; without it, `stop` exits with 1 and suggests `--timeout` or `--force`.

With `--url`, `stop` requests the shutdown and returns without waiting. A plain `OrchestrationAPI` (one not started by `promptise runtime start`, so without the shutdown route) gets `stop-all` instead: its processes stop, its server keeps running.

---

## promptise runtime restart

Restart every process of a running runtime, or one process.

```bash
promptise runtime restart
promptise runtime restart prod --process data-watcher
```

| Argument / option | Short | Description |
|---|---|---|
| `NAME` | | Runtime to restart, or a process name in a running runtime; may be omitted when only one runtime runs |
| `--process` | `-p` | Restart only this process |
| `--url`, `--token` | | Talk to an Orchestration API instead of a local runtime |

The runtime itself keeps running, with the same control API and token. Restarted processes keep their context state and, unless `context.conversation_clear_on_stop` is set, their conversation. To load edited manifests, stop the runtime and start it again.

---

## promptise runtime logs

Show the newest journal entries of a process, each with its entry ID (the target `promptise runtime rewind` takes).

```bash
promptise runtime logs data-watcher                 # newest 20 entries
promptise runtime logs data-watcher --lines 50
promptise runtime logs data-watcher --follow        # keep printing new ones (Ctrl+C ends)
promptise runtime logs data-watcher --journal-path /var/lib/agents/journal
```

| Argument / option | Short | Default | Description |
|---|---|---|---|
| `NAME` | | required | Process name |
| `--lines` | `-n` | `20` | Number of most recent entries to show |
| `--follow/--no-follow` | `-f` | off | Keep printing new entries |
| `--journal-path` | | see below | Journal directory |
| `--url`, `--token` | | -- | Read the journal through an Orchestration API |

Where the entries come from:

- `--url`: the API's `GET /processes/{name}/journal` (at most 500 entries per request; `--follow` polls every second);
- `--journal-path`: `<path>/<name>.jsonl`;
- otherwise, a process in a runtime on this machine: its journal file (read directly, so it works while the runtime is busy), or its API for a memory journal. A process with `journal.level: none` is an error that says so;
- otherwise: `.promptise/journal/<name>.jsonl` in the current directory.

The output is a table of time, entry ID, type and data (cut at 80 characters). With `--follow`, new entries are printed one per line as they are written; a file that is truncated or replaced is read again from the start.

```
                                Journal: notes
 Time                     Entry ID                              Type               Data
 2026-10-19T20:25:02.077  b354d9ca-f7b7-4783-964a-970f9ae156b3  invocation_result  {"invocation": 1, ...
 2026-10-19T20:25:02.077  38b220ec-6b42-4c81-a0ca-f27f4f1a4767  checkpoint         {"state": {"context_state": {}, ...
```

---

## promptise runtime rewind

Roll a process's conversation and/or context state back to just before a journal entry. See [Rewind](journal/rewind.md) for what each mode does.

```bash
promptise runtime rewind notes                                  # list recent entries with their IDs
promptise runtime rewind notes 7c7bee77-f518-41de-b250-d1b586819d7a --plan
promptise runtime rewind notes 7c7bee77-f518-41de-b250-d1b586819d7a        # plan, confirm, apply
promptise runtime rewind notes 7c7bee77-... --mode conversation_only --yes
```

| Argument / option | Short | Default | Description |
|---|---|---|---|
| `PROCESS` | | required | Process to rewind |
| `ENTRY_ID` | | -- | Journal entry to go back to; omit it to list recent entries |
| `--mode` | `-m` | `both` | `both`, `conversation_only`, `code_only` (context state) or `summarize` (rewind nothing, add a summary to the conversation) |
| `--plan` | | off | Only show what the rewind would change |
| `--yes` | `-y` | off | Apply without asking for confirmation |
| `--runtime` | `-r` | -- | Runtime the process runs in (needed when several runtimes run) |
| `--lines` | `-n` | `20` | Entries to list when no entry ID is given (1-500) |
| `--url`, `--token` | | -- | Talk to an Orchestration API instead of a local runtime |

The command shows the plan first (target entry, mode, how many entries are affected), then asks `Rewind <process>? [y/N]`. A running process is suspended for the rewind and resumed afterwards. The rewind is journaled with actor `cli`. It goes through the control API, so the process must run in a runtime and have a journal.

---

## promptise runtime validate

Validate a `.agent` manifest file, or every `.agent` file in a directory.

```bash
promptise runtime validate agents/watcher.agent
promptise runtime validate agents/
```

| Argument | Description |
|---|---|
| `PATH` | A `.agent` manifest file, or a directory of them |

Each file is checked against the manifest schema (field types, trigger settings, hooks, unknown keys) and warnings are printed for likely mistakes. A valid file gets a summary table: name, model, version, the start of the instructions, server count and trigger count. For a directory, a final line says how many manifests failed.

---

## promptise runtime init

Generate a template `.agent` manifest file.

```bash
promptise runtime init                                    # agent.agent, basic template
promptise runtime init --template cron --output agents/monitor.agent
promptise runtime init --output agent.agent --force       # overwrite
```

| Option | Short | Default | Description |
|---|---|---|---|
| `--output` | `-o` | `agent.agent` | Output file path |
| `--template` | `-t` | `basic` | `basic`, `cron`, `webhook` or `full` |
| `--force/--no-force` | | off | Overwrite an existing file |

| Template | Contents |
|---|---|
| `basic` | Model and instructions, no servers or triggers |
| `cron` | An HTTP MCP server and a cron trigger every 5 minutes |
| `webhook` | An HTTP MCP server and a webhook trigger |
| `full` | HTTP and stdio servers, cron, webhook and file-watch triggers, a full journal, restart and lifetime settings |

---

## Exit codes

| Code | Meaning |
|---|---|
| `0` | Success (also a foreground runtime stopped with Ctrl+C, SIGTERM or `stop`) |
| `1` | An error with a message on stderr: no runtime running, unknown runtime or process, the API refused the request or the token, a manifest that fails to load or validate, no process could start, a runtime that didn't stop in time, a rewind that was not confirmed, an existing file for `init` |
| `2` | Usage error: an unknown option, a missing argument, a value out of range |
| `130` | A second Ctrl+C or SIGTERM while the runtime was shutting down (exits without a clean shutdown) |

---

## Example workflow

```bash
# 1. Generate and edit a manifest
promptise runtime init --template cron --output agents/monitor.agent

# 2. Validate it
promptise runtime validate agents/monitor.agent

# 3. Start it in the background
promptise runtime start agents/monitor.agent --detach

# 4. Watch it
promptise runtime status
promptise runtime logs data-watcher --follow

# 5. Stop it
promptise runtime stop
```

---

## Common pitfalls

- **Commands that can't find the runtime.** The state directory is per user (and per `PROMPTISE_RUNTIME_STATE_DIR`). A runtime started by another user, under `sudo`, or with a different state directory is invisible; use `--url` with its token.
- **Two runtimes running.** `stop`, `restart` and `rewind` then need a name (or `--runtime`); the error lists the running ones.
- **Expecting `restart` to reload manifests.** It restarts the processes with the loaded configuration. Stop and start the runtime to pick up edits.
- **`logs` reading the wrong journal.** A relative `journal.path` is relative to the directory `start` ran in. Run `logs` with the runtime running (it then uses the recorded absolute path) or pass `--journal-path`.
- **A remote API without a known token.** With `--api-host 0.0.0.0` and no `PROMPTISE_RUNTIME_TOKEN`, the token is random and only in the local state file. Set the variable before `start` so remote clients can use it.
- **A killed runtime.** After `kill -9` the state file stays until the next command notices the PID is gone and removes it; the detached log file is kept.

---

## What's Next

- [Agent Manifests](manifests.md) -- the `.agent` file format
- [Orchestration API](api.md) -- the control API the commands talk to
- [Journal](journal/index.md) and [Rewind](journal/rewind.md) -- what `logs` and `rewind` work on
- [Dashboard](dashboard.md) -- the live terminal UI
- [Runtime Manager](runtime-manager.md) -- the Python API behind the CLI

!!! tip "Learn the concept"

    - [Run Multiple AI Agents: One Runtime, Many Processes](https://www.promptise.com/guides/view/run-multiple-ai-agents) -- Run several agents from manifests and control them from the terminal.
    - [AI Agent Control API: Manage Running Agents Over HTTP](https://www.promptise.com/guides/view/ai-agent-control-api) -- The API every runtime command talks to.
    - [Durable AI Agents: Crash Recovery and Safe Resume](https://www.promptise.com/guides/view/durable-ai-agent-crash-recovery) -- Journals, recovery and rewind behind `logs` and `rewind`.
