# CLI Reference

The `promptise` CLI provides commands for listing tools, running interactive agent sessions, and managing runtime processes.

```bash
promptise --version
```

---

## Global Options

| Flag | Description |
|---|---|
| `--version` | Print version and exit |
| `--help` | Show help for any command |

---

## `promptise list-tools` -- List Available Tools

Discover tools exposed by MCP servers without writing code.

```bash
# List tools from an HTTP MCP server
promptise list-tools --model-id openai:gpt-5-mini --http "name=my_tools url=http://localhost:8000/mcp"

# List tools from a stdio MCP server
promptise list-tools --model-id openai:gpt-5-mini --stdio "name=echo command=python args='-m mytools.server'"
```

### Options

| Flag | Description |
|---|---|
| `--model-id` | LLM model identifier (required, e.g. `openai:gpt-5-mini`) |
| `--stdio` | Add a stdio MCP server (repeatable) |
| `--http` | Add an HTTP MCP server (repeatable) |
| `--instructions` | Optional system prompt override |

### Server Spec Syntax

Both `--stdio` and `--http` accept a quoted string of `key=value` pairs:

**HTTP servers:**

```bash
--http "name=my_tools url=http://localhost:8000/mcp"
```

| Key | Required | Description |
|---|---|---|
| `name` | yes | Server name (used as identifier) |
| `url` | yes | HTTP endpoint URL |

**Stdio servers:**

```bash
--stdio "name=echo command=python args='-m mytools.server --port 3333' env.API_KEY=xyz"
```

| Key | Required | Description |
|---|---|---|
| `name` | yes | Server name |
| `command` | yes | Executable to run |
| `args` | no | Command arguments (quote for spaces) |
| `env.*` | no | Environment variables (e.g. `env.API_KEY=xyz`) |
| `cwd` | no | Working directory |

Repeat `--stdio` or `--http` for multiple servers.

---

## `promptise run` -- Interactive REPL Session

Start an interactive REPL session with inline MCP server specs (no `.superagent` file needed).

```bash
promptise run --model-id openai:gpt-5-mini \
    --http "name=tools url=http://localhost:8000/mcp"
```

### Options

| Flag | Description |
|---|---|
| `--model-id` | LLM model identifier (required, e.g. `openai:gpt-5-mini`) |
| `--stdio` | Add a stdio MCP server (repeatable) |
| `--http` | Add an HTTP MCP server (repeatable) |
| `--instructions` | Optional system prompt override |
| `--trace/--no-trace` | Print tool invocations and results (default: `--trace`) |
| `--raw/--no-raw` | Also print raw result object (default: `--no-raw`) |

---

## `promptise agent` -- Agent from .superagent File

Run an interactive agent session from a `.superagent` configuration file. CLI flags override file settings.

```bash
# From a .superagent file
promptise agent config.superagent

# Override model
promptise agent config.superagent --model-id openai:gpt-5-mini

# With additional servers from CLI
promptise agent config.superagent --http "name=extra url=http://localhost:9000/mcp"
```

### Options

| Flag | Description |
|---|---|
| `--model-id` | Override model from config file |
| `--instructions` | Override instructions from config file |
| `--trace/--no-trace` | Override trace setting from config file |
| `--stdio` | Additional stdio server (merged with config file servers) |
| `--http` | Additional HTTP server (merged with config file servers) |
| `--raw/--no-raw` | Also print raw result object |

---

## `promptise validate` -- Validate .superagent File

Validate a `.superagent` configuration file without building the agent.

```bash
# Full validation
promptise validate my_agent.superagent

# Skip environment variable checks
promptise validate my_agent.superagent --no-check-env

# Skip cross-agent reference checks
promptise validate my_agent.superagent --no-check-refs
```

### Options

| Flag | Description |
|---|---|
| `--check-env/--no-check-env` | Check environment variable availability (default: `--check-env`) |
| `--check-refs/--no-check-refs` | Validate cross-agent references (default: `--check-refs`) |

Performs YAML syntax checks, schema validation, environment variable availability checks, and cross-agent reference validation.

---

## `promptise init` -- Generate Template .superagent File

Create a starter `.superagent` configuration file with common patterns.

```bash
# Basic template
promptise init

# HTTP server template with auth headers
promptise init --output api_agent.superagent --template http

# Full-featured template
promptise init -o advanced.superagent -t advanced --force
```

### Options

| Flag | Description |
|---|---|
| `--output`, `-o` | Output file path (default: `agent.superagent`) |
| `--template`, `-t` | Template type: `basic`, `http`, `stdio`, `cross-agent`, `advanced` |
| `--force/--no-force` | Overwrite existing file |

---

## `promptise runtime` -- Process Management

The `runtime` subcommand group manages long-running agent processes.

### `promptise runtime start` -- Start Processes

Start agent process(es) from a `.agent` manifest file or directory.

```bash
# Start a single manifest
promptise runtime start agents/watcher.agent

# Start all manifests in a directory
promptise runtime start agents/

# Override process name
promptise runtime start agents/watcher.agent --name custom-watcher

# Run in the background
promptise runtime start agents/watcher.agent --detach

# Start with live monitoring dashboard
promptise runtime start agents/ --dashboard
```

| Flag | Description |
|---|---|
| `--name`, `-n` | Override process name from manifest |
| `--detach`, `-d` | Run in the background |
| `--dashboard` | Enable live terminal monitoring dashboard |

Press `Ctrl+C` to stop all processes (in foreground mode).

### `promptise runtime stop` -- Stop a Process

```bash
promptise runtime stop data-watcher
promptise runtime stop data-watcher --force
```

| Flag | Description |
|---|---|
| `--force` | Force stop the process |

### `promptise runtime status` -- Show Status

```bash
# Show status of all processes
promptise runtime status

# Show status of a specific process
promptise runtime status data-watcher

# Output as JSON
promptise runtime status --json
```

| Flag | Description |
|---|---|
| `--json` | Output status as JSON |

### `promptise runtime logs` -- View Journal

Show journal entries for a running process.

```bash
# Show last 20 entries
promptise runtime logs data-watcher

# Show last 50 entries
promptise runtime logs data-watcher --lines 50

# Follow new entries
promptise runtime logs data-watcher --follow

# Custom journal path
promptise runtime logs data-watcher --journal-path .promptise/journal
```

| Flag | Description |
|---|---|
| `--lines`, `-n` | Number of entries to show (default: 20) |
| `--follow`, `-f` | Follow new entries in real time |
| `--journal-path` | Journal directory (default: `.promptise/journal`) |

### `promptise runtime restart` -- Restart a Process

```bash
promptise runtime restart data-watcher
```

### `promptise runtime validate` -- Validate a Manifest

Check a `.agent` manifest file for schema errors and warnings.

```bash
promptise runtime validate agents/watcher.agent
```

Outputs a summary table with name, model, version, instructions, server count, and trigger count. Warnings are printed for common issues like missing instructions, triggers, or servers.

### `promptise runtime init` -- Generate a Template

Create a template `.agent` manifest file to get started quickly.

```bash
# Basic template
promptise runtime init -o my-agent.agent

# Cron-based template
promptise runtime init --template cron -o watcher.agent

# Webhook template
promptise runtime init --template webhook -o handler.agent

# Full-featured template
promptise runtime init --template full -o production.agent

# Overwrite existing file
promptise runtime init --template cron -o watcher.agent --force
```

| Flag | Description |
|---|---|
| `--output`, `-o` | Output file path (default: `agent.agent`) |
| `--template`, `-t` | Template type: `basic`, `cron`, `webhook`, `full` |
| `--force` | Overwrite existing file |

---

## Common Workflows

### Develop and Test an Agent Process

```bash
# 1. Generate a template manifest
promptise runtime init --template cron -o watcher.agent

# 2. Edit the manifest with your instructions and servers
# (edit watcher.agent)

# 3. Validate the manifest
promptise runtime validate watcher.agent

# 4. Start the process
promptise runtime start watcher.agent

# 5. View logs in another terminal
promptise runtime logs pipeline-watcher --follow
```

### Discover Available Tools

```bash
# Check what tools an MCP server exposes
promptise list-tools --model-id openai:gpt-5-mini \
    --http "name=my_tools url=http://localhost:8000/mcp"
```

### Run a Quick Interactive Session

```bash
# Inline servers (no .superagent file)
promptise run --model-id openai:gpt-5-mini \
    --http "name=tools url=http://localhost:8000/mcp"

# From a .superagent file
promptise agent config.superagent
```

---

!!! tip "Environment variables"
    The CLI loads `.env` files automatically via `python-dotenv`. Set `OPENAI_API_KEY` or `ANTHROPIC_API_KEY` in your `.env` file for seamless usage.

---

## What's Next?

- [Agent Runtime Overview](../runtime/index.md) -- architecture and lifecycle concepts
- [Agent Manifests](../runtime/manifests.md) -- `.agent` YAML format reference
- [Observability](observability.md) -- `--observe` flag and observability
