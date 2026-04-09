# Sandbox

Execute untrusted code safely in isolated Docker containers with resource limits and network controls.

```python
from promptise import build_agent
from promptise.config import HTTPServerSpec

# Simple: enable sandbox with defaults
agent = await build_agent(
    servers={"tools": HTTPServerSpec(url="http://localhost:8000/mcp")},
    model="openai:gpt-5-mini",
    sandbox=True,
)

# Detailed: configure resource limits and network mode
agent = await build_agent(
    servers={"tools": HTTPServerSpec(url="http://localhost:8000/mcp")},
    model="openai:gpt-5-mini",
    sandbox={"network_mode": "restricted", "memory_limit": "512M", "cpu_limit": 2},
)
```

---

## Concepts

The sandbox provides a secure execution environment for agent-generated code. When an agent creates or runs code (especially in [Open Mode](../runtime/meta-tools.md)), the sandbox ensures that code runs inside an isolated container with:

- **Resource limits** -- CPU, memory, and disk quotas
- **Network isolation** -- no access, restricted (DNS-filtered), or full
- **Filesystem isolation** -- read-only root, writable workspace only
- **Capability dropping** -- minimal Linux capabilities
- **Security profiles** -- seccomp and AppArmor enforcement

---

## SandboxConfig

`SandboxConfig` controls every aspect of the sandbox environment.

| Field | Type | Default | Description |
|---|---|---|---|
| `backend` | `str` | `"docker"` | Container backend: `"docker"`, `"gvisor"` |
| `image` | `str` | `"python:3.11-slim"` | Base container image |
| `cpu_limit` | `int` | `2` | Maximum CPU cores (1-32) |
| `memory_limit` | `str` | `"4G"` | Maximum memory (e.g. `"512M"`, `"4G"`) |
| `disk_limit` | `str` | `"10G"` | Maximum disk space |
| `network` | `NetworkMode` | `RESTRICTED` | Network isolation mode |
| `persistent` | `bool` | `False` | Keep workspace between runs |
| `timeout` | `int` | `300` | Max execution time in seconds (1-3600) |
| `tools` | `list[str]` | `["python"]` | Pre-installed tool ecosystems |
| `workdir` | `str` | `"/workspace"` | Working directory inside container |
| `env` | `dict[str, str]` | `{}` | Additional environment variables |
| `allow_sudo` | `bool` | `False` | Allow sudo access in container |
| `runtime` | `str \| None` | `None` | Container runtime (e.g., `"runsc"` for gVisor) |
| `read_only_rootfs` | `bool` | `True` | Read-only root filesystem |

### NetworkMode

| Mode | Description |
|---|---|
| `NetworkMode.NONE` | No network access whatsoever |
| `NetworkMode.RESTRICTED` | Limited network with DNS filtering (default) |
| `NetworkMode.FULL` | Full unrestricted network access |

```python
from promptise.sandbox.config import SandboxConfig, NetworkMode

config = SandboxConfig(
    backend="gvisor",
    cpu_limit=4,
    memory_limit="8G",
    network=NetworkMode.FULL,
    tools=["python", "node", "rust"],
    timeout=600,
)
```

---

## SandboxManager

`SandboxManager` is responsible for creating and managing sandbox sessions. It normalizes configuration from `bool`, `dict`, or `SandboxConfig` and provides an async context manager for lifecycle management.

### Constructor

```python
SandboxManager(config: SandboxConfig | dict | bool)
```

- **`SandboxConfig`** -- used directly.
- **`dict`** -- converted to `SandboxConfig` via field mapping.
- **`bool`** -- `True` creates a `SandboxConfig` with defaults; `False` disables sandboxing.

### Methods

| Method | Return Type | Description |
|---|---|---|
| `create_session()` | `SandboxSession` | Create a new isolated sandbox session (container). |
| `cleanup_all()` | `None` | Stop and remove all sessions created by this manager. |

`SandboxManager` also supports use as an async context manager, which calls `cleanup_all()` on exit.

### Example

```python
from promptise.sandbox import SandboxManager, SandboxConfig

config = SandboxConfig(image="python:3.11-slim", cpu_limit=2, memory_limit="4G")
async with SandboxManager(config) as manager:
    session = await manager.create_session()
    result = await session.execute("python -c 'print(42)'")
    print(result.stdout)  # "42\n"
    await manager.cleanup_all()
```

---

## SandboxSession

`SandboxSession` manages a persistent sandbox session for command execution. It provides a high-level interface for running commands, reading/writing files, and installing packages.

### Creating a Session

Sessions are typically created by `SandboxManager.create_session()`. They support async context managers for automatic cleanup:

```python
async with sandbox_session as session:
    result = await session.execute("python --version")
    print(result.stdout)
# Container is automatically cleaned up on exit
```

### Method Reference

| Method | Signature | Description |
|---|---|---|
| `execute` | `execute(command, timeout=None, workdir=None) -> CommandResult` | Run a shell command inside the container. |
| `read_file` | `read_file(path) -> str` | Read a file from the sandbox filesystem. |
| `write_file` | `write_file(path, content)` | Write a file into the sandbox filesystem. |
| `list_files` | `list_files(directory="/workspace") -> list[str]` | List files in a directory inside the sandbox. |
| `install_package` | `install_package(package, tool="python") -> CommandResult` | Install a package using the specified ecosystem (`python`, `node`, `rust`, `go`). |
| `cleanup` | `cleanup()` | Stop and remove the container. If `persistent=True`, the container keeps running for reuse. |

`SandboxSession` also supports use as an async context manager, which calls `cleanup()` on exit.

### Full Example

```python
async with session:
    # Execute a command
    result = await session.execute("python -c 'print(42)'", timeout=30)
    print(result.stdout)       # "42\n"
    print(result.exit_code)    # 0
    print(result.success)      # True

    # File operations
    await session.write_file("/workspace/script.py", "print('hello')")
    content = await session.read_file("/workspace/script.py")
    files = await session.list_files("/workspace")

    # Install a package
    await session.install_package("requests")
```

### Executing Commands

```python
result = await session.execute("python script.py", timeout=30)

if result.success:
    print(result.stdout)
else:
    print(f"Failed (exit code {result.exit_code}): {result.stderr}")
```

### File Operations

```python
# Write a file into the sandbox
await session.write_file("/workspace/script.py", "print('hello')")

# Read a file from the sandbox
content = await session.read_file("/workspace/script.py")

# List files in a directory
files = await session.list_files("/workspace")
```

### Installing Packages

```python
# Python packages
result = await session.install_package("pandas", tool="python")

# Node.js packages
result = await session.install_package("lodash", tool="node")

# Supported ecosystems: "python", "node", "rust", "go"
```

---

## CommandResult

Every command execution returns a `CommandResult` dataclass.

| Field | Type | Description |
|---|---|---|
| `exit_code` | `int` | Process exit code (0 = success) |
| `stdout` | `str` | Standard output |
| `stderr` | `str` | Standard error |
| `timeout` | `bool` | Whether the command timed out |
| `duration` | `float` | Execution time in seconds |
| `success` | `bool` | Computed property: `True` if `exit_code == 0` and not timed out |

```python
result = await session.execute("python -c 'import sys; sys.exit(1)'")
assert not result.success
assert result.exit_code == 1
```

---

## SandboxBackend

`SandboxBackend` is the abstract base class that defines how containers are managed. The framework ships with `DockerBackend` (which optionally supports gVisor via the `runsc` runtime).

### Abstract Methods

| Method | Description |
|---|---|
| `create_container()` | Create and start a new container from the configured image. |
| `execute_command()` | Execute a command inside a running container. |
| `read_file()` | Read a file from the container filesystem. |
| `write_file()` | Write a file into the container filesystem. |
| `stop_container()` | Stop a running container. |
| `remove_container()` | Remove a stopped container. |
| `health_check()` | Verify the backend is available and functional. |

### DockerBackend

`DockerBackend` is the default backend. It communicates with the Docker daemon to create isolated containers. When the `runtime` field in `SandboxConfig` is set to `"runsc"`, Docker uses gVisor for additional kernel-level isolation.

```python
from promptise.sandbox.config import SandboxConfig

# Standard Docker
config = SandboxConfig(backend="docker")

# Docker with gVisor runtime
config = SandboxConfig(backend="docker", runtime="runsc")
```

---

## Integration with build_agent

### Simple Boolean

```python
agent = await build_agent(
    servers={"tools": HTTPServerSpec(url="http://localhost:8000/mcp")},
    model="openai:gpt-5-mini",
    sandbox=True,  # Uses SandboxConfig defaults
)
```

### Detailed Configuration

Pass a dict to customize sandbox settings:

```python
agent = await build_agent(
    servers={"tools": HTTPServerSpec(url="http://localhost:8000/mcp")},
    model="openai:gpt-5-mini",
    sandbox={
        "network_mode": "restricted",
        "memory_limit": "512M",
        "cpu_limit": 2,
        "timeout": 120,
        "tools": ["python", "node"],
    },
)
```

---

## Security Profiles

The sandbox ships with default security profiles that restrict container capabilities.

### Seccomp Profile

The default seccomp profile uses a whitelist approach: only explicitly allowed syscalls are permitted. This blocks dangerous operations like kernel module loading, raw device access, and privilege escalation.

### AppArmor Profile

The AppArmor profile restricts filesystem access:

- `/workspace/**` and `/tmp/**` -- read-write (agent workspace)
- `/usr/**`, `/lib/**`, `/etc/**` -- read-only (system files)
- `/home/**`, `/root/**` -- denied
- `/dev/mem`, `/dev/kmem` -- denied
- `/proc/sys/kernel/**` -- write denied

### Capability Dropping

By default, most Linux capabilities are dropped, including `CAP_NET_ADMIN`, `CAP_SYS_ADMIN`, `CAP_SYS_PTRACE`, and others. Only the minimal capabilities needed for running user code are retained.

---

## Open Mode Sandboxing

When using [Open Mode](../runtime/meta-tools.md), agent-created tools can be sandboxed automatically:

```python
from promptise.runtime import ProcessConfig, ExecutionMode, OpenModeConfig

config = ProcessConfig(
    model="openai:gpt-5-mini",
    execution_mode=ExecutionMode.OPEN,
    open_mode=OpenModeConfig(
        allow_tool_creation=True,
        sandbox_custom_tools=True,  # Agent-written code runs in sandbox
    ),
)
```

When `sandbox_custom_tools=True`, any Python tools the agent creates at runtime are executed inside the sandbox with restricted builtins, preventing access to the host filesystem, network, and system resources.

!!! warning "Docker required"
    The sandbox requires Docker to be installed and running on the host machine. The `gvisor` backend requires additional setup (install `runsc`).

---

## What's Next?

- [Observability](observability.md) -- track token usage
- [Memory](memory.md) -- persistent memory with vector search
- [Meta-Tools](../runtime/meta-tools.md) -- open mode and agent-created tools
