"""Sandbox configuration schemas and defaults."""

from __future__ import annotations

from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, field_validator


class NetworkMode(str, Enum):
    """Network isolation modes for sandbox."""

    NONE = "none"  # No network access
    RESTRICTED = "restricted"  # Limited network with DNS filtering
    FULL = "full"  # Full network access


class SandboxConfig(BaseModel):
    """Configuration for sandbox environment.

    Attributes:
        backend: Container backend to use (docker, gvisor)
        image: Base container image (default: python:3.11-slim)
        cpu_limit: Maximum CPU cores (default: 2)
        memory_limit: Maximum memory (default: "4G")
        disk_limit: Maximum disk space (default: "10G")
        network: Network isolation mode (default: "restricted")
        persistent: Keep workspace between runs (default: False)
        timeout: Max execution time in seconds (default: 300)
        tools: Pre-installed tools to include (default: ["python"])
        workdir: Working directory inside container (default: "/workspace")
        env: Additional environment variables
        allow_sudo: Allow sudo access in container (default: False)

    Examples:
        >>> # Minimal config
        >>> config = SandboxConfig()
        >>>
        >>> # Custom config
        >>> config = SandboxConfig(
        ...     backend="gvisor",
        ...     cpu_limit=4,
        ...     memory_limit="8G",
        ...     network=NetworkMode.FULL,
        ...     tools=["python", "node", "rust"]
        ... )
    """

    backend: Literal["docker", "gvisor"] = Field("docker", description="Container backend")
    image: str = Field("python:3.11-slim", description="Base container image")
    cpu_limit: int = Field(2, gt=0, le=32, description="Maximum CPU cores")
    memory_limit: str = Field("4G", description="Maximum memory (e.g., '4G', '512M')")
    disk_limit: str = Field("10G", description="Maximum disk space (e.g., '10G', '1T')")
    network: NetworkMode = Field(NetworkMode.RESTRICTED, description="Network isolation mode")
    persistent: bool = Field(False, description="Keep workspace between runs")
    timeout: int = Field(300, gt=0, le=3600, description="Max execution time in seconds")
    tools: list[str] = Field(default_factory=lambda: ["python"], description="Pre-installed tools")
    workdir: str = Field("/workspace", description="Working directory inside container")
    env: dict[str, str] = Field(default_factory=dict, description="Environment variables")
    allow_sudo: bool = Field(False, description="Allow sudo access in container")

    # Runtime options (managed internally)
    runtime: str | None = Field(None, description="Container runtime (e.g., 'runsc' for gVisor)")
    security_opt: list[str] = Field(default_factory=list, description="Security options")
    cap_drop: list[str] = Field(default_factory=list, description="Capabilities to drop")
    read_only_rootfs: bool = Field(True, description="Read-only root filesystem")

    @field_validator("memory_limit", "disk_limit")
    @classmethod
    def validate_size(cls, v: str) -> str:
        """Validate memory/disk size format."""
        if not v:
            raise ValueError("Size cannot be empty")

        # Extract number and unit
        units = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
        if v[-1] in units:
            try:
                size = int(v[:-1])
                if size <= 0:
                    raise ValueError("Size must be positive")
                return v
            except ValueError:
                raise ValueError(f"Invalid size format: {v}")
        else:
            raise ValueError(f"Size must end with K, M, G, or T: {v}")

    @classmethod
    def from_simple(cls, enabled: bool = True) -> SandboxConfig:
        """Create config from simple boolean flag.

        Args:
            enabled: If True, use default config

        Returns:
            SandboxConfig with defaults
        """
        if not enabled:
            raise ValueError("Cannot create config when sandbox is disabled")
        return cls()

    @classmethod
    def from_dict(cls, data: dict) -> SandboxConfig:
        """Create config from dictionary.

        Args:
            data: Configuration dictionary

        Returns:
            SandboxConfig instance
        """
        return cls(**data)


DEFAULT_APPARMOR_PROFILE = """#include <tunables/global>

profile promptise-sandbox flags=(attach_disconnected,mediate_deleted) {
  #include <abstractions/base>

  # Allow network access
  network inet tcp,
  network inet udp,
  network inet6 tcp,
  network inet6 udp,

  # File operations in workspace
  /workspace/** rw,
  /tmp/** rw,

  # Read-only system files
  /usr/** r,
  /lib/** r,
  /etc/** r,
  /proc/** r,
  /sys/** r,

  # Python interpreter
  /usr/bin/python* ix,
  /usr/local/bin/python* ix,

  # Node.js
  /usr/bin/node ix,
  /usr/local/bin/node ix,

  # Common tools
  /usr/bin/bash ix,
  /bin/bash ix,
  /usr/bin/sh ix,
  /bin/sh ix,

  # Deny dangerous operations
  deny /sys/kernel/security/** w,
  deny /proc/sys/kernel/** w,
  deny /boot/** rw,
  deny /dev/mem rw,
  deny /dev/kmem rw,

  # Deny access to host files
  deny /home/** rw,
  deny /root/** rw,
}
"""

# Capabilities to drop (all except what's needed)
DEFAULT_CAP_DROP = [
    "CAP_AUDIT_CONTROL",
    "CAP_AUDIT_READ",
    "CAP_AUDIT_WRITE",
    "CAP_BLOCK_SUSPEND",
    "CAP_BPF",
    "CAP_CHECKPOINT_RESTORE",
    "CAP_DAC_OVERRIDE",
    "CAP_DAC_READ_SEARCH",
    "CAP_FOWNER",
    "CAP_FSETID",
    "CAP_IPC_LOCK",
    "CAP_IPC_OWNER",
    "CAP_KILL",
    "CAP_LEASE",
    "CAP_LINUX_IMMUTABLE",
    "CAP_MAC_ADMIN",
    "CAP_MAC_OVERRIDE",
    "CAP_MKNOD",
    "CAP_NET_ADMIN",
    "CAP_NET_BIND_SERVICE",
    "CAP_NET_BROADCAST",
    "CAP_NET_RAW",
    "CAP_PERFMON",
    "CAP_SETFCAP",
    "CAP_SETGID",
    "CAP_SETPCAP",
    "CAP_SETUID",
    "CAP_SYS_ADMIN",
    "CAP_SYS_BOOT",
    "CAP_SYS_CHROOT",
    "CAP_SYS_MODULE",
    "CAP_SYS_NICE",
    "CAP_SYS_PACCT",
    "CAP_SYS_PTRACE",
    "CAP_SYS_RAWIO",
    "CAP_SYS_RESOURCE",
    "CAP_SYS_TIME",
    "CAP_SYS_TTY_CONFIG",
    "CAP_SYSLOG",
    "CAP_WAKE_ALARM",
]
