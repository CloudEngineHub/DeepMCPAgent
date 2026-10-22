---
seo_title: "Code Sandbox for Agents: Docker Isolation"
description: "Run agent-generated code in isolated Docker containers with resource limits, network modes, a read-only root, dropped capabilities and optional gVisor."
---

# Code Sandbox for Agents

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

# Detailed: configure resource limits (the network stays off unless you set it)
agent = await build_agent(
    servers={"tools": HTTPServerSpec(url="http://localhost:8000/mcp")},
    model="openai:gpt-5-mini",
    sandbox={"memory_limit": "512M", "cpu_limit": 2},
)
```

!!! note "Install"
    The sandbox needs the Docker Python client: `pip install "promptise[sandbox]"`.
    Docker itself must be installed and running on the host.

!!! warning "No silent fallback"
    If the sandbox cannot be started (the `docker` package is missing, the
    Docker daemon is not running, gVisor is requested but `runsc` is not
    installed, or `network="restricted"` cannot be enforced), `build_agent`
    raises instead of building an agent without it. To run without a sandbox,
    leave `sandbox` unset (or pass `None`/`False`).

---

## Concepts

The sandbox provides a secure execution environment for agent-generated code. When an agent creates or runs code (especially in [Open Mode](../runtime/meta-tools.md)), the sandbox ensures that code runs inside an isolated container with:

- **Resource limits** -- CPU, memory, process count, workspace size, and execution time
- **Network isolation** -- no network by default; restricted (DNS and ports 80/443 to any host) or full when you ask for it, optionally on a Docker network of your own (for an egress proxy that allows only some hosts)
- **Filesystem isolation** -- read-only root, size-capped writable workspace
- **A non-root user** -- code runs as `nobody` (uid 65534) unless you set `user="root"`
- **Capability dropping** -- ~40 Linux capabilities dropped, `no-new-privileges` always set
- **Syscall filtering** -- Docker's default seccomp profile; optional gVisor kernel
- **Timeouts that stop the code** -- a command that exceeds its timeout is killed

---

## SandboxConfig

`SandboxConfig` controls every aspect of the sandbox environment.

| Field | Type | Default | Description |
|---|---|---|---|
| `backend` | `str` | `"docker"` | Container backend: `"docker"`, `"gvisor"` |
| `image` | `str` | `"python:3.11-slim"` | Base container image |
| `cpu_limit` | `int` | `2` | Maximum CPU cores (1-32) |
| `memory_limit` | `str` | `"4G"` | Maximum memory (e.g. `"512M"`, `"4G"`) |
| `disk_limit` | `str` | `"1G"` | Size of the writable workspace (a tmpfs at `workdir`; counts toward `memory_limit`). `/tmp` and `/var/tmp` are capped at the same size or less. |
| `pids_limit` | `int` | `256` | Maximum processes and threads (contains fork bombs) |
| `network` | `NetworkMode` | `NONE` | Network isolation mode |
| `docker_network` | `str \| None` | `None` | Name of an existing Docker network to attach the container to instead of Docker's default bridge. Needs `network` `"full"` or `"restricted"`. See [Allowing only some hosts](#allowing-only-some-hosts). |
| `persistent` | `bool` | `False` | Keep the container after the session ends |
| `timeout` | `int` | `300` | Default per-command timeout in seconds (1-3600) |
| `workdir` | `str` | `"/workspace"` | Working directory inside container |
| `env` | `dict[str, str]` | `{}` | Additional environment variables (`HOME` defaults to `workdir`) |
| `allow_sudo` | `bool` | `False` | Keep `CAP_SETUID`/`CAP_SETGID` so `sudo` works in images that ship it |
| `user` | `str` | `"65534:65534"` | User the container and every command run as: `"uid[:gid]"` or a user name from the image. The default is `nobody`. Set `"root"` to run as root. |
| `runtime` | `str \| None` | `None` | Docker runtime (e.g., `"runsc"` for gVisor) |
| `read_only_rootfs` | `bool` | `True` | Read-only root filesystem |

Unknown keys are rejected with a hint, so a misspelled option fails loudly
instead of being ignored:

```python
SandboxConfig.from_dict({"network_mode": "none"})
# ValidationError: Unknown sandbox option(s): 'network_mode': use 'network'
# ("none", "restricted" or "full"). Valid options: backend, image, ...
```

There is no option to pre-install tool ecosystems: use an `image` that already
contains what the code needs (for example `node:22-slim`, or your own image
built `FROM python:3.11-slim` with the packages baked in).

### NetworkMode

| Mode | Description |
|---|---|
| `NetworkMode.NONE` | No network interface besides loopback (default) |
| `NetworkMode.RESTRICTED` | Outbound DNS (port 53) to any host and TCP 80/443 to any host outside the link-local range; everything else dropped, IPv4 and IPv6 |
| `NetworkMode.FULL` | Full unrestricted network access |

The network is `"none"` unless you set `network` explicitly, for `sandbox=True`,
for a custom dict, and for `agent_pattern="code-action"`.

`"restricted"` is enforced with `iptables`/`ip6tables` rules installed in the
container before any sandboxed code runs. It **fails closed**: if the image
does not ship `iptables` and `ip6tables` (the default `python:3.11-slim` does
not), the container is removed and the session refuses to start. Use an image
that includes them, or choose `"none"` or `"full"` explicitly.

`"restricted"` filters **ports, not hosts**. Exactly what it allows:

- loopback;
- DNS (UDP and TCP port 53) to any server, so DNS queries can carry data out;
- TCP ports 80 and 443 to any host on the internet (`example.com`, `pypi.org`,
  an attacker's server alike), and to private addresses the container can
  route to;
- nothing on the link-local ranges `169.254.0.0/16` and `fe80::/10` and on
  `fd00:ec2::254` (other than DNS), so code can't read cloud instance
  metadata and credentials from `169.254.169.254`.

Everything else (other ports, UDP other than DNS, ICMP) is dropped. Use it to
keep code off databases, SSH and other services on non-web ports, not to stop
it from sending data to a web server.

### Allowing only some hosts

To let sandboxed code reach only some hosts (a package index, one API), put an
egress proxy that allows those hosts between the sandbox and the internet, and
attach the sandbox to a Docker network whose only way out is that proxy:

```bash
# A network with no route out, and a proxy on it and on the default bridge
docker network create --internal agent-egress
docker run -d --name egress-proxy --network bridge my-allowlist-proxy
docker network connect agent-egress egress-proxy
```

```python
config = SandboxConfig(
    network="full",                 # no port filter; the network has no route out
    docker_network="agent-egress",  # created above
    env={"HTTPS_PROXY": "http://egress-proxy:3128", "HTTP_PROXY": "http://egress-proxy:3128"},
)
```

On an `--internal` network the container has no route to the internet, so
code that ignores the proxy settings can't connect anywhere directly; the
proxy decides which hosts a `CONNECT` may reach. The network must exist
before the sandbox starts (the session refuses to start otherwise).
`docker_network` also works with `network="restricted"`, which adds the port
filter on top (then the proxy must listen on port 80 or 443). The sandbox
never joins the host's network or another container's: `docker_network`
can't be `"host"`, `"none"` or `"container:…"`.

```python
from promptise.sandbox.config import SandboxConfig, NetworkMode

config = SandboxConfig(
    backend="gvisor",
    cpu_limit=4,
    memory_limit="8G",
    pids_limit=512,
    network=NetworkMode.FULL,
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
| `write_file` | `write_file(path, content)` | Write a file into the writable workspace (or `/tmp`). Missing parent directories are created. |
| `list_files` | `list_files(directory="/workspace") -> list[str]` | List files in a directory inside the sandbox. |
| `install_package` | `install_package(package, tool="python") -> CommandResult` | Install a package using the specified ecosystem (`python`, `node`, `rust`, `go`). |
| `cleanup` | `cleanup()` | Kill and remove the container (it is removed at once, so it gets no graceful-stop grace period). If `persistent=True`, the container keeps running for reuse. |

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

When a command exceeds its timeout, every process it started is killed inside
the container (children and background jobs included) and the result has
`timeout=True`. If the processes cannot be killed individually (for example a
fork bomb has used up `pids_limit`), the container is restarted, which also
empties the workspace.

### File Operations

```python
# Write a file into the sandbox (works with the read-only root filesystem)
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

Packages are installed into the writable workspace (`HOME` points at it, so
`pip` falls back to a user install under `/workspace/.local`; `npm` installs
into `/workspace/node_modules`). Downloading needs network access, which the
default `network="none"` does not provide. For packages the code always needs,
build a custom image with them pre-installed instead.

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
| `health_check()` | Return whether the backend is available and functional. |

`kill_container()` (not abstract) stops a container without a grace period;
`SandboxSession.cleanup()` uses it before removing a non-persistent container.
The default calls `stop_container()`; `DockerBackend` sends `SIGKILL` (the
container's `/bin/bash` ignores `SIGTERM`, so a graceful `docker stop` always
waited out its timeout).

`ensure_available()` (not abstract) raises with the actual reason the backend
cannot run containers; `SandboxManager.create_session()` calls it, so a missing
gVisor runtime is reported as such rather than as "Docker is not running".

### DockerBackend

`DockerBackend` is the default backend. It communicates with the Docker daemon to create isolated containers. When the `runtime` field in `SandboxConfig` is set to `"runsc"`, Docker uses gVisor for additional kernel-level isolation.

```python
from promptise.sandbox.config import SandboxConfig

# Standard Docker
config = SandboxConfig(backend="docker")

# Docker with gVisor runtime (equivalent to backend="gvisor")
config = SandboxConfig(backend="docker", runtime="runsc")
```

If `runsc` is not registered with Docker, `create_session()` raises
`RuntimeError: ... gVisor runtime 'runsc' is not registered with Docker
(available runtimes: ...)`. The sandbox never falls back to the default runtime.

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
        "image": "node:22-slim",  # an image with the tools the code needs
        "memory_limit": "512M",
        "cpu_limit": 2,
        "pids_limit": 128,
        "timeout": 120,
        # "network": "full",      # only if the code must reach the network
    },
)
```

When the sandbox is enabled, five tools are added to the agent:
`sandbox_exec`, `sandbox_read_file`, `sandbox_write_file`, `sandbox_list_files`
and `sandbox_install_package`. With `trace_tools=True` their calls are printed
like any other tool call.

---

## Security Layers

What every sandbox container gets, and what it does not:

| Layer | What is applied |
|---|---|
| Network | No network interface besides loopback by default (`network="none"`, see [NetworkMode](#networkmode)) |
| Seccomp | Docker's default seccomp profile, which blocks ~44 syscalls such as `mount`, `reboot`, `kexec_load` and kernel module loading. Promptise does not ship a custom profile. |
| AppArmor | Docker's `docker-default` profile on hosts with AppArmor enabled. Promptise does not load a custom profile. |
| Capabilities | ~40 capabilities dropped, including `CAP_SYS_ADMIN`, `CAP_NET_ADMIN`, `CAP_SYS_PTRACE` and `CAP_SETUID`/`CAP_SETGID` (kept only with `allow_sudo=True`). `CAP_SYS_ADMIN` is never re-added. |
| User | Code runs as `nobody` (`user="65534:65534"`), not as the image's default user (root in `python:*-slim`). The workspace is owned by (or writable for) that user. Root only with `user="root"`. |
| Privileges | `no-new-privileges` always set; never a privileged container |
| Filesystem | Read-only root; writable size-capped tmpfs at `workdir`, `/tmp` and `/var/tmp` (`/tmp` and `/var/tmp` are `noexec`) |
| Resources | `cpu_limit`, `memory_limit` (swap disabled), `pids_limit`, `disk_limit` |
| Time | Per-command timeout; the command's processes are killed when it expires |
| Kernel | Optional gVisor (`backend="gvisor"`) for a user-space kernel |

The egress filter for `network="restricted"` and the kill of a timed-out
command run as short privileged `exec`s issued by the host-side backend; the
sandboxed code itself never gains those privileges.

!!! note "Upgrading from 1.5.0"
    Before 1.5.1 the sandbox did not set a user, so code ran as the image's
    default user, which is root in the default `python:3.11-slim` image.
    It now runs as `nobody`. The workspace (also `HOME`) and `/tmp` are
    writable, and `pip install` installs into the workspace as before. Code
    that writes outside them, or a custom image that expects a particular
    user, needs `user=` set: the image's user name, or `user="root"` for the
    old behaviour.

    ```python
    agent = await build_agent(..., sandbox={"user": "root"})  # 1.5.0 behaviour
    ```

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
    The sandbox requires Docker to be installed and running on the host machine and the `promptise[sandbox]` extra. The `gvisor` backend requires additional setup (install `runsc`).

---

## What's Next?

- [Observability](observability.md) -- track token usage
- [Memory](memory.md) -- persistent memory with vector search
- [Meta-Tools](../runtime/meta-tools.md) -- open mode and agent-created tools

!!! tip "Learn the concept"

    - [AI Agent Code Execution Sandbox: Run LLM Code Safely](https://www.promptise.com/guides/view/ai-agent-code-execution-sandbox-python) -- Run model-written code in a container, try escapes and set limits.
