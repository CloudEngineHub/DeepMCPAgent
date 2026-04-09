# SuperAgent Files

Define agents declaratively using `.superagent` YAML files -- configure model, servers, memory, sandbox, and cross-agent references in a single file.

## Quick Example

```yaml
# analyst.superagent
version: "1.0"

agent:
  model: "openai:gpt-5-mini"
  instructions: "You are a data analyst. Use available tools to answer questions."
  trace: true

servers:
  database:
    type: http
    url: "http://localhost:9000/mcp"
    headers:
      Authorization: "Bearer ${DB_TOKEN}"
```

Load and run it in Python:

```python
import asyncio
from promptise import build_agent
from promptise.superagent import load_superagent_file

async def main():
    loader, cross_agents = load_superagent_file("analyst.superagent")
    config = loader.to_agent_config()
    agent = await build_agent(**config.to_build_kwargs())

    result = await agent.ainvoke({
        "messages": [{"role": "user", "content": "Show me top 10 customers by revenue"}]
    })
    print(result["messages"][-1].content)
    await agent.shutdown()

asyncio.run(main())
```

## Concepts

A `.superagent` file is a YAML document validated against `SuperAgentSchema`. It replaces the programmatic `build_agent()` call with a declarative configuration file that can be version-controlled, shared across teams, and loaded at runtime.

The loader pipeline works in three steps:

1. **Parse and validate** -- `SuperAgentLoader.from_file()` reads YAML and validates it against the Pydantic schema. Invalid fields are rejected immediately.
2. **Resolve environment variables** -- `resolve_env_vars()` replaces `${VAR}` and `${VAR:-default}` placeholders with actual values from the environment.
3. **Convert to native types** -- `to_agent_config()` produces a `SuperAgentConfig` object whose `to_build_kwargs()` method returns a dict ready for `build_agent(**kwargs)`.

## Full YAML Schema

Here is a `.superagent` file using every available section:

```yaml
version: "1.0"

agent:
  model: "openai:gpt-5-mini"          # or detailed config (see below)
  instructions: "You are a research assistant."
  trace: true

servers:
  search:
    type: http
    url: "https://search.example.com/mcp"
    transport: streamable-http
    headers:
      Authorization: "Bearer ${SEARCH_TOKEN}"
  local_tools:
    type: stdio
    command: python
    args: ["-m", "my_tools.server"]
    env:
      API_KEY: "${MY_API_KEY}"
    cwd: "/opt/tools"
    keep_alive: true

cross_agents:
  math_expert:
    file: "./agents/math.superagent"
    description: "Specialized math and calculation agent"

memory:
  provider: chroma                     # "in_memory", "chroma", or "mem0"
  collection: research_memory
  persist_directory: ".promptise/chroma"

sandbox:
  backend: docker
  image: "python:3.11-slim"
  cpu_limit: 2
  memory_limit: "4G"
  disk_limit: "10G"
  network: restricted
  timeout: 300
  tools: ["python"]
  workdir: "/workspace"
  allow_sudo: false
```

### Section Reference

#### `version`

Always `"1.0"`. Required for forward compatibility.

#### `agent`

| Field | Type | Default | Description |
|---|---|---|---|
| `model` | `str \| DetailedModelConfig` | **required** | Model identifier or detailed configuration. |
| `instructions` | `str \| None` | `None` | System prompt override. |
| `trace` | `bool` | `true` | Print tool invocations to stdout. |

The `model` field accepts either a simple string or a detailed configuration object:

=== "Simple string"

    ```yaml
    agent:
      model: "openai:gpt-5-mini"
    ```

=== "Detailed config"

    ```yaml
    agent:
      model:
        provider: openai
        name: gpt-5-mini
        api_key: "${OPENAI_API_KEY}"
        temperature: 0.7
        max_tokens: 4096
        timeout: 30
        base_url: "https://custom-endpoint.example.com/v1"
    ```

#### `servers`

A dict of named server configurations. Each entry requires a `type` discriminator field.

=== "HTTP server"

    | Field | Type | Default | Description |
    |---|---|---|---|
    | `type` | `"http"` | **required** | Discriminator. |
    | `url` | `str` | **required** | Full MCP endpoint URL. |
    | `transport` | `"http" \| "streamable-http" \| "sse"` | `"http"` | Transport protocol. |
    | `headers` | `dict[str, str]` | `{}` | HTTP headers (values support `${ENV_VAR}`). |
    | `auth` | `str \| None` | `None` | Legacy auth token. |

=== "Stdio server"

    | Field | Type | Default | Description |
    |---|---|---|---|
    | `type` | `"stdio"` | **required** | Discriminator. |
    | `command` | `str` | **required** | Executable command. |
    | `args` | `list[str]` | `[]` | Command arguments. |
    | `env` | `dict[str, str]` | `{}` | Environment variables (values support `${ENV_VAR}`). |
    | `cwd` | `str \| None` | `None` | Working directory. |
    | `keep_alive` | `bool` | `true` | Maintain persistent connection. |

#### `cross_agents`

Optional. Maps a peer name to a file reference.

| Field | Type | Default | Description |
|---|---|---|---|
| `file` | `str` | **required** | Path to the peer's `.superagent` file (relative to this file). |
| `description` | `str` | `""` | Description shown in the auto-generated `ask_agent_<name>` tool. |

#### `memory`

Optional. Configures persistent agent memory.

| Field | Type | Default | Description |
|---|---|---|---|
| `provider` | `"in_memory" \| "chroma" \| "mem0"` | `"in_memory"` | Memory backend. |
| `collection` | `str` | `"agent_memory"` | ChromaDB collection name. |
| `persist_directory` | `str \| None` | `None` | ChromaDB persistence path. |
| `user_id` | `str` | `"default"` | Mem0 user scope. |
| `agent_id` | `str \| None` | `None` | Mem0 agent scope. |

#### `sandbox`

Optional. Can be `true` for defaults or a detailed configuration object.

| Field | Type | Default | Description |
|---|---|---|---|
| `backend` | `"docker" \| "gvisor"` | `"docker"` | Container backend. |
| `image` | `str` | `"python:3.11-slim"` | Base container image. |
| `cpu_limit` | `int` | `2` | Maximum CPU cores (1--32). |
| `memory_limit` | `str` | `"4G"` | Maximum memory. |
| `disk_limit` | `str` | `"10G"` | Maximum disk space. |
| `network` | `"none" \| "restricted" \| "full"` | `"restricted"` | Network isolation mode. |
| `persistent` | `bool` | `false` | Keep workspace between runs. |
| `timeout` | `int` | `300` | Max execution time in seconds (1--3600). |
| `tools` | `list[str]` | `["python"]` | Pre-installed tools. |
| `workdir` | `str` | `"/workspace"` | Working directory inside container. |
| `env` | `dict[str, str]` | `{}` | Additional environment variables. |
| `allow_sudo` | `bool` | `false` | Allow sudo access in container. |

## Environment Variable Resolution

All string values in the YAML support environment variable substitution:

| Syntax | Behavior |
|---|---|
| `${VAR}` | Replaced with the value of `VAR`. Raises an error if not set. |
| `${VAR:-default}` | Replaced with the value of `VAR`, or `"default"` if not set. |

```yaml
servers:
  api:
    type: http
    url: "${API_URL:-http://localhost:8000/mcp}"
    headers:
      Authorization: "Bearer ${API_TOKEN}"
```

Call `loader.validate_env_vars()` to check which variables are missing before resolving:

```python
loader = SuperAgentLoader.from_file("agent.superagent")
missing = loader.validate_env_vars()
if missing:
    print(f"Set these env vars: {', '.join(missing)}")
else:
    loader.resolve_env_vars()
```

## Cross-Agent References and Cycle Detection

The loader recursively resolves cross-agent references. If agent A references agent B and agent B references agent A, the loader raises a `SuperAgentError` with the full reference chain.

```yaml
# main.superagent
cross_agents:
  researcher:
    file: "./researcher.superagent"
    description: "Web research specialist"
  analyst:
    file: "./analyst.superagent"
    description: "Data analysis specialist"
```

```python
loader, cross_loaders = load_superagent_file("main.superagent")

for name, cross_loader in cross_loaders.items():
    print(f"Loaded peer: {name} from {cross_loader.file_path}")
```

## The `SuperAgentLoader` Class

```python
from promptise.superagent import SuperAgentLoader, load_superagent_file

# Step-by-step usage
loader = SuperAgentLoader.from_file("agent.superagent")
loader.resolve_env_vars()
cross_loaders = loader.resolve_cross_agents(recursive=True)

servers = loader.to_server_specs()      # dict[str, ServerSpec]
model   = loader.to_model_string()      # e.g. "openai:gpt-5-mini"
config  = loader.to_agent_config()      # SuperAgentConfig

# Or use the convenience function
loader, cross_loaders = load_superagent_file("agent.superagent")
config = loader.to_agent_config()
kwargs = config.to_build_kwargs()
agent = await build_agent(**kwargs)
```

## API Summary

| Symbol | Import | Description |
|---|---|---|
| `SuperAgentLoader` | `from promptise.superagent import SuperAgentLoader` | Loads, validates, and resolves `.superagent` files. Key methods: `from_file()`, `resolve_env_vars()`, `resolve_cross_agents()`, `to_agent_config()`. |
| `load_superagent_file()` | `from promptise.superagent import load_superagent_file` | Convenience function that loads, resolves env vars, and resolves cross-agent refs in one call. Returns `(loader, cross_loaders)`. |
| `SuperAgentSchema` | `from promptise.superagent_schema import SuperAgentSchema` | Pydantic model for the full `.superagent` YAML schema. |
| `SuperAgentConfig` | `from promptise.superagent import SuperAgentConfig` | Processed config with `to_build_kwargs()` for `build_agent()`. |

!!! tip "File extensions"
    The loader accepts `.superagent`, `.superagent.yaml`, and `.superagent.yml` extensions.

!!! tip "Schema validation"
    All sections use `extra="forbid"`, so misspelled fields (e.g. `instuctions` instead of `instructions`) produce a clear validation error rather than being silently ignored.

!!! warning "Direct API keys in YAML"
    The schema validator warns if an `api_key` field looks like a direct secret (starts with `sk-` or `pk-`). Always use `${ENV_VAR}` syntax for credentials.

!!! warning "At least one capability required"
    The schema requires at least one of `servers`, `cross_agents`, or `sandbox` to be configured. A file with only `agent` and `version` fails validation.

## What's Next?

- [Building Agents](building-agents.md) -- the `build_agent()` function that SuperAgent files feed into.
- [Server Configuration](server-specs.md) -- details on `StdioServerSpec` and `HTTPServerSpec`.
- [Cross-Agent Delegation](cross-agent.md) -- runtime behavior of the delegation tools generated from cross-agent references.
