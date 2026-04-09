# Runtime CLI

The `promptise runtime` command group provides command-line management for agent processes. Start agents from manifest files, monitor status, view journal logs, validate manifests, and generate templates -- all from the terminal.

```bash
# Start an agent from a manifest file
promptise runtime start agents/watcher.agent

# Start all agents in a directory
promptise runtime start agents/

# Validate a manifest
promptise runtime validate agents/watcher.agent

# View journal logs
promptise runtime logs data-watcher --lines 50
```

---

## Concepts

The runtime CLI is built with [Typer](https://typer.tiangolo.com/) and uses [Rich](https://rich.readthedocs.io/) for formatted terminal output. It operates on `.agent` manifest files -- declarative YAML configurations that define an agent process (model, instructions, triggers, etc.).

The CLI wraps the `AgentRuntime` Python API, providing the same capabilities from the command line.

---

## Commands

### promptise runtime start

Start agent process(es) from a `.agent` manifest file or a directory of manifests.

```bash
# Single manifest
promptise runtime start agents/watcher.agent

# Directory of manifests
promptise runtime start agents/

# Override the process name
promptise runtime start agents/watcher.agent --name my-watcher

# Enable the live dashboard
promptise runtime start agents/ --dashboard

# Run in the background
promptise runtime start agents/watcher.agent --detach
```

| Option | Short | Description |
|---|---|---|
| `--name` | `-n` | Override the process name from the manifest |
| `--dashboard/--no-dashboard` | | Enable the live terminal dashboard |
| `--detach/--no-detach` | `-d` | Run in the background (prints PID) |

When running in the foreground (without `--detach`), press `Ctrl+C` to stop all processes gracefully.

### promptise runtime stop

Stop a running agent process.

```bash
promptise runtime stop data-watcher
promptise runtime stop data-watcher --force
```

| Option | Description |
|---|---|
| `--force/--no-force` | Force stop |

!!! tip "Current limitation"
    The stop command currently provides guidance on stopping via `Ctrl+C` or `SIGTERM`. Full daemon-mode stop will be available with distributed transport.

### promptise runtime status

Show the status of agent processes.

```bash
# All processes
promptise runtime status

# Specific process
promptise runtime status data-watcher

# JSON output
promptise runtime status --json
```

| Option | Description |
|---|---|
| `--json` | Output as JSON |

### promptise runtime logs

Show journal entries for a process.

```bash
# Default: last 20 entries
promptise runtime logs data-watcher

# Show more entries
promptise runtime logs data-watcher --lines 50

# Follow new entries (live tail)
promptise runtime logs data-watcher --follow

# Custom journal path
promptise runtime logs data-watcher --journal-path /var/log/agents
```

| Option | Short | Default | Description |
|---|---|---|---|
| `--lines` | `-n` | `20` | Number of entries to show |
| `--follow/--no-follow` | `-f` | `False` | Follow new entries |
| `--journal-path` | | `.promptise/journal` | Journal directory |

The output is a Rich table showing timestamp, entry type, and data for each journal entry.

### promptise runtime restart

Restart a running agent process (stop then start).

```bash
promptise runtime restart data-watcher
```

### promptise runtime validate

Validate a `.agent` manifest file for schema correctness.

```bash
promptise runtime validate agents/watcher.agent
```

The validator checks:

- Schema validation against `AgentManifestSchema`
- Required fields for each trigger type
- Value constraints (port ranges, cron expressions, etc.)
- Produces warnings for potential issues

Output includes a summary table with name, model, version, instructions preview, server count, and trigger count.

### promptise runtime init

Generate a template `.agent` manifest file.

```bash
# Basic template
promptise runtime init

# Specify output file
promptise runtime init --output agents/my-agent.agent

# Choose a template type
promptise runtime init --template cron

# Overwrite existing file
promptise runtime init --output agent.agent --force
```

| Option | Short | Default | Description |
|---|---|---|---|
| `--output` | `-o` | `agent.agent` | Output file path |
| `--template` | `-t` | `basic` | Template type |
| `--force/--no-force` | | `False` | Overwrite existing file |

Available templates:

| Template | Description |
|---|---|
| `basic` | Minimal agent with no triggers |
| `cron` | Cron-scheduled agent |
| `webhook` | Webhook-triggered agent |
| `full` | Full configuration with cron, webhook, file watch, journal |

After generating a template, the CLI prints next steps:

```
1. Edit agent.agent to customize your agent
2. Validate: promptise runtime validate agent.agent
3. Start:    promptise runtime start agent.agent
```

---

## Example Workflow

A typical workflow for creating and running an agent:

```bash
# 1. Generate a template
promptise runtime init --template cron --output agents/monitor.agent

# 2. Edit the manifest (in your editor)
# ...

# 3. Validate
promptise runtime validate agents/monitor.agent

# 4. Start with dashboard
promptise runtime start agents/monitor.agent --dashboard

# 5. In another terminal, check logs
promptise runtime logs data-watcher --lines 50
```

---

## Status Output

The `start` command (without `--dashboard`) displays a Rich table of process status:

```
           Agent Processes
 Name          State    PID       Invocations  Queue  Uptime
 data-watcher  RUNNING  abc123..  5            0      0h 2m 30s
 responder     RUNNING  def456..  3            1      0h 2m 28s
```

---

## API Summary

| Command | Description |
|---|---|
| `promptise runtime start <manifest>` | Start from `.agent` file or directory |
| `promptise runtime stop <name>` | Stop a running process |
| `promptise runtime status [name]` | Show process status |
| `promptise runtime logs <name>` | Show journal entries |
| `promptise runtime restart <name>` | Restart a process |
| `promptise runtime validate <path>` | Validate a `.agent` manifest |
| `promptise runtime init` | Generate a template `.agent` file |

---

## Tips and Gotchas

!!! tip "Use validate before start"
    Always validate your manifest before starting. Schema errors at runtime produce less helpful error messages than validation errors.

!!! tip "Use --dashboard for development"
    The live dashboard gives real-time visibility into trigger firings and process state. It is invaluable during development and testing.

!!! tip "Template as a starting point"
    Use `promptise runtime init --template full` to see all available configuration options, then trim down to what you need.

!!! warning "Detach mode is limited"
    Background mode (`--detach`) starts processes but does not currently implement a daemon with IPC. Full daemon management will be available with distributed transport.

!!! warning "Journal path must exist"
    The `--journal-path` option for `logs` must point to an existing journal directory. If no entries are found, the command prints a message and exits.

---

## What's Next

- [Dashboard](dashboard.md) -- the live terminal UI in detail
- [Runtime Manager](runtime-manager.md) -- the Python API behind the CLI
- [Configuration](configuration.md) -- all configuration options for manifests
