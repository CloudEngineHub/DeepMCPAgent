---
title: "Securing Agent-Written Code with a Docker Sandbox"
description: "For teams shipping self-modifying or code-generating agents: a decision guide on the exact isolation layers that matter (seccomp, ~40 dropped capabilities…"
keywords: "sandboxed code execution for ai, secure agent-written code, docker sandbox llm, gvisor ai code execution, isolate untrusted llm code"
date: 2026-07-16
slug: sandboxed-code-execution-for-ai
categories:
  - Guardrails
---

# Securing Agent-Written Code with a Docker Sandbox

Sandboxed code execution for AI is the difference between an agent that drafts a Python script and an agent that runs it on your production host with your credentials in the environment. The moment you let a model write and execute code — for data analysis, glue scripts, or a self-modifying "open mode" agent — the generated script is untrusted input, no matter how good your prompt is. This post is a decision guide to the specific isolation layers that actually matter, and how to turn them on. By the end you will know exactly what a hardened container gives you, which of those layers most "sandbox" wrappers quietly skip, and how to run generated code with no blast radius.

## Why "sandbox" usually means less than you think

Plenty of frameworks advertise a sandbox and deliver a `subprocess.run()` with a timeout. That stops an infinite loop. It does nothing about a script that reads `~/.aws/credentials`, opens a socket to an attacker, or forks until the box falls over. A real sandbox for **secure agent-written code** has to assume the code is hostile and remove capabilities up front.

Promptise Foundry's sandbox runs each execution inside an isolated Docker container and layers on the controls that a bare subprocess can't provide:

- **Resource limits** — CPU, memory, disk, and wall-clock quotas so a runaway script can't starve the host.
- **Network isolation** — no network, restricted (DNS-filtered), or full — chosen per agent.
- **Filesystem isolation** — read-only root filesystem with a single writable `/workspace`.
- **Capability dropping** — most Linux capabilities removed, leaving only what user code needs.
- **Security profiles** — a seccomp syscall whitelist plus AppArmor filesystem rules.

Each of these is a separate wall. The value is in stacking them, and in the defaults being safe before you configure anything. The full field-by-field reference lives in the [sandbox documentation](../../core/sandbox.md).

## Turn on sandboxed code execution for AI in one line

You do not assemble any of this by hand. Pass `sandbox=True` to `build_agent()` and the framework auto-injects the execution tools (run a command, read a file, write a file, list files, install a package) and routes every call through a hardened container.

```python
import asyncio
from promptise import build_agent


async def main():
    agent = await build_agent(
        model="openai:gpt-5-mini",
        instructions=(
            "You are a data assistant. When a task needs computation, "
            "write a short Python script and run it in the sandbox."
        ),
        sandbox=True,  # hardened Docker: seccomp, dropped caps, read-only rootfs
    )

    result = await agent.ainvoke(
        {"messages": [{
            "role": "user",
            "content": "Compute the 20th Fibonacci number in Python and show the code.",
        }]}
    )
    print(result["messages"][-1].content)
    await agent.shutdown()


asyncio.run(main())
```

The agent writes the script, executes it inside the container, and reads back only the output. The host filesystem, host network, and host process table are never in scope. Docker must be installed and running for this to work — that is the one hard prerequisite.

Need something tighter than the defaults? Pass a dict instead of a bool. This is where the `network_mode` control from the call-to-action comes in:

```python
agent = await build_agent(
    model="openai:gpt-5-mini",
    sandbox={
        "network_mode": "restricted",  # none | restricted | full
        "memory_limit": "512M",
        "cpu_limit": 2,
        "timeout": 120,
        "tools": ["python", "node"],
    },
)
```

`network_mode="restricted"` gives DNS-filtered egress for scripts that legitimately need to `pip install` or hit an allowed API, while `"none"` cuts the network entirely for code that should never phone home.

## The hardening layers a docker sandbox llm setup must include

When you compare a "sandbox" claim across frameworks, these are the layers to check for. A Docker sandbox LLM integration that skips them is a container in name only.

- **Seccomp whitelist.** The default profile permits only an explicit set of syscalls. Kernel module loading, raw device access, and privilege-escalation paths are blocked, not merely discouraged.
- **~40 dropped capabilities.** Dangerous Linux capabilities — `CAP_NET_ADMIN`, `CAP_SYS_ADMIN`, `CAP_SYS_PTRACE`, and dozens more — are removed. Only the minimal set to run user code remains.
- **Read-only root filesystem.** `read_only_rootfs` is on by default. System paths are read-only; the agent can write to `/workspace` and `/tmp` and nowhere else, so it can't tamper with the runtime.
- **No ambient network.** The default network mode is restricted, and `NetworkMode.NONE` removes the interface. There is no implicit path off the box.
- **AppArmor filesystem rules.** `/home`, `/root`, `/dev/mem`, and `/proc/sys/kernel` are denied outright, independent of what the code tries.

If you need kernel-level isolation on top of the container boundary — the strongest option for genuinely untrusted, multi-source code — switch to the gVisor runtime. gVisor intercepts syscalls in a user-space kernel, shrinking the host attack surface further:

```python
from promptise.sandbox.config import SandboxConfig, NetworkMode

config = SandboxConfig(
    backend="gvisor",           # or backend="docker", runtime="runsc"
    network=NetworkMode.NONE,   # no network at all
    read_only_rootfs=True,      # writable /workspace only
    memory_limit="512M",
    cpu_limit=2,
    timeout=120,
)
```

gVisor is the right call for **gvisor AI code execution** when the workload is untrusted and the host is shared, with the tradeoff that its syscall interception adds runtime overhead. It requires `runsc` installed on the host. For trusted first-party scripts, standard Docker isolation is usually the better balance.

## Open Mode: forcing every agent-written script to isolate untrusted LLM code

The sharpest need for a sandbox is a self-modifying agent. In Open Mode, an agent can write its own tools at runtime — which means the code it generates is exactly the untrusted input you must isolate. Promptise Foundry closes that gap by making the sandbox mandatory for agent-authored code rather than optional.

```python
from promptise.runtime import ProcessConfig, ExecutionMode, OpenModeConfig

config = ProcessConfig(
    model="openai:gpt-5-mini",
    execution_mode=ExecutionMode.OPEN,
    open_mode=OpenModeConfig(
        allow_tool_creation=True,
        sandbox_custom_tools=True,  # every agent-written tool runs in the sandbox
    ),
)
```

With `sandbox_custom_tools=True`, any Python tool the agent creates is executed inside the sandbox with restricted builtins — no host filesystem, no host network, no system access. This is how you let an agent extend itself and still be able to isolate untrusted LLM code the moment it materializes. Open Mode pairs this with other guardrails (maximum instruction length, a cap on custom tools, and an MCP-URL allowlist) so a self-editing agent stays inside a known envelope.

## Sandbox, guardrails, and approval: defense in depth

The sandbox contains what code can *do*. It does not decide what code *should* run. Layer it with the other two controls for a complete story:

- **Input and output guardrails** stop the agent from acting on a poisoned instruction in the first place. Turn them on with `guardrails=True` on `build_agent()`; the local detection heads scan for prompt injection, PII, and leaked credentials before and after every turn. See the [guardrails guide](../../core/guardrails.md), and for a deeper walkthrough of the six risk classes read [LLM Guardrails in Python: The Complete Guide](llm-guardrails-python.md).
- **Human-in-the-loop approval** gates the irreversible calls. Wrap a tool with an [approval policy](../../core/approval.md) and the agent pauses for a reviewer before, say, installing a package from an untrusted source or writing to a shared volume.

Sandbox catches the blast, guardrails catch the intent, approval catches the judgment call. Together they mean a generated script that tries something malicious is contained, the malicious instruction that produced it is likely flagged, and the risky action never ran unreviewed.

## When a different approach fits better

Honesty first: a Docker sandbox is not always the right tool.

- **You only ever run trusted, first-party code.** If the agent executes a fixed set of vetted functions and never generates or runs arbitrary scripts, container startup is overhead you don't need. A plain in-process call is simpler.
- **You can't run a container runtime.** In serverless or locked-down environments without Docker, this sandbox won't start. A managed code-execution service or a language-level sandbox may fit better there.
- **You need microVM isolation for hostile multi-tenant workloads.** For running mutually distrusting tenants' code at scale, purpose-built microVM platforms like Firecracker or Kata Containers push isolation past what a shared-kernel container gives you. gVisor narrows that gap but doesn't fully close it.

For the common case — your own agent generating your own code that you nonetheless can't fully trust — the container sandbox is the pragmatic default: strong isolation, one line to enable, no separate infrastructure.

## Frequently asked questions

### Do I need Docker installed to use the sandbox?

Yes. The default backend talks to the Docker daemon to create isolated containers, so Docker must be installed and running on the host. The optional gVisor backend additionally needs `runsc` installed. Without a container runtime, enable trusted execution paths instead of the sandbox.

### What happens to network access inside the sandbox?

By default the network is restricted (DNS-filtered egress). Set `network_mode="none"` (or `NetworkMode.NONE`) to remove network access entirely for code that should never reach out, or `"full"` when a script genuinely needs open access. Inbound connections to the container are never exposed.

### Is gVisor required, or is plain Docker enough?

Plain Docker with the default seccomp profile, dropped capabilities, and read-only rootfs is enough for most agent-written code. Reach for gVisor when you're running genuinely untrusted code on a shared host and want user-space syscall interception, accepting some runtime overhead in exchange.

## Next steps

Set `sandbox=True` (or `sandbox={"network_mode": "restricted"}`) on `build_agent()` and start running generated code with no blast radius. From there, walk the [Quick Start](../../getting-started/quickstart.md) to stand up your first agent, then read the [sandbox reference](../../core/sandbox.md) to tune resource limits, network mode, and the gVisor runtime for your threat model.
