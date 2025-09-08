# CLI

DeepMCPAgent includes a command-line interface (`deepmcpagent`) for exploring and running agents with MCP tools.  
This is useful for quick testing, debugging, or building automation pipelines.

---

## Usage

```bash
deepmcpagent [OPTIONS] COMMAND [ARGS]...
```

Run `--help` on any command for details.

---

## Global Options

- `--version`
  Print the current version of DeepMCPAgent and exit.

- `--help`
  Show the help message.

---

## Commands

### `list-tools`

Discover available MCP tools from one or more servers.

**Usage:**

```bash
deepmcpagent list-tools --model-id MODEL --http "name=math url=http://127.0.0.1:8000/mcp transport=http"
```

**Options:**

- `--model-id <str>` (required)
  The model provider id, e.g. `"openai:gpt-4.1"`.

- `--stdio <block>` (repeatable)
  Start an MCP server over stdio.
  Example:

  ```bash
  --stdio "name=echo command=python args='-m mypkg.server --port 3333' env.API_KEY=xyz keep_alive=false"
  ```

- `--http <block>` (repeatable)
  Connect to an MCP server over HTTP/SSE.
  Example:

  ```bash
  --http "name=math url=http://127.0.0.1:8000/mcp transport=http"
  ```

- `--instructions <str>`
  Optional override for the system prompt.

**Example:**

```bash
deepmcpagent list-tools --model-id "openai:gpt-4.1" \
  --http "name=math url=http://127.0.0.1:8000/mcp transport=http"
```

---

### `run`

Start an interactive agent session that uses only MCP tools.

**Usage:**

```bash
deepmcpagent run --model-id MODEL --http "name=math url=http://127.0.0.1:8000/mcp transport=http"
```

**Options:**

- `--model-id <str>` (required)
  The model provider id, e.g. `"openai:gpt-4.1"`.

- `--stdio <block>` (repeatable)
  Start an MCP server over stdio.

- `--http <block>` (repeatable)
  Connect to an MCP server over HTTP/SSE.

- `--instructions <str>`
  Optional override for the system prompt.

**Example:**

```bash
deepmcpagent run --model-id "openai:gpt-4.1" \
  --http "name=math url=http://127.0.0.1:8000/mcp transport=http"
```

**Interactive session:**

```text
DeepMCPAgent is ready. Type 'exit' to quit.
> What is 2 + 2?
2 + 2 = 4.
```

---

## Example Server Setup

To test locally, run the sample math server:

```bash
python examples/servers/math_server.py
```

Then connect:

```bash
deepmcpagent run --model-id "openai:gpt-4.1" \
  --http "name=math url=http://127.0.0.1:8000/mcp transport=http"
```

---

## Notes

- MCP servers can be mixed: use both `--stdio` and `--http`.
- Multiple servers can be provided by repeating the flags.
- The agent falls back gracefully if no servers are available, but won’t have tools.
- For automation, `list-tools` is useful in CI/CD pipelines to validate server contracts.
