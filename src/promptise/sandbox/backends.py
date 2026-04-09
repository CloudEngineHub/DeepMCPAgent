"""Container backends for sandbox execution.

This module provides abstract backend interface and concrete implementations
for different container runtimes (Docker, gVisor).
"""

from __future__ import annotations

import logging
import shlex
import time
from abc import ABC, abstractmethod
from typing import Any

from .config import DEFAULT_CAP_DROP, NetworkMode, SandboxConfig
from .session import CommandResult

logger = logging.getLogger(__name__)


class SandboxBackend(ABC):
    """Abstract base class for sandbox backends.

    Defines the interface that all backend implementations must provide.
    """

    def __init__(self, config: SandboxConfig):
        """Initialize backend with configuration.

        Args:
            config: Sandbox configuration
        """
        self.config = config

    @abstractmethod
    async def create_container(self) -> str:
        """Create and start a new container.

        Returns:
            Container ID
        """
        pass

    @abstractmethod
    async def execute_command(
        self, container_id: str, command: str, timeout: int, workdir: str
    ) -> CommandResult:
        """Execute a command in the container.

        Args:
            container_id: Container ID
            command: Shell command to execute
            timeout: Timeout in seconds
            workdir: Working directory

        Returns:
            CommandResult with execution details
        """
        pass

    @abstractmethod
    async def read_file(self, container_id: str, file_path: str) -> str:
        """Read a file from the container.

        Args:
            container_id: Container ID
            file_path: Path to file inside container

        Returns:
            File contents
        """
        pass

    @abstractmethod
    async def write_file(self, container_id: str, file_path: str, content: str) -> None:
        """Write a file to the container.

        Args:
            container_id: Container ID
            file_path: Path to file inside container
            content: File contents
        """
        pass

    @abstractmethod
    async def stop_container(self, container_id: str) -> None:
        """Stop a running container.

        Args:
            container_id: Container ID
        """
        pass

    @abstractmethod
    async def remove_container(self, container_id: str) -> None:
        """Remove a container.

        Args:
            container_id: Container ID
        """
        pass

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if backend is available and working.

        Returns:
            True if backend is healthy
        """
        pass


class DockerBackend(SandboxBackend):
    """Docker-based sandbox backend with gVisor support.

    This backend uses Docker as the container runtime, with optional gVisor
    for enhanced security through user-space kernel implementation.
    """

    def __init__(self, config: SandboxConfig):
        """Initialize Docker backend.

        Args:
            config: Sandbox configuration

        Raises:
            ImportError: If the ``docker`` package is not installed.
        """
        try:
            import docker as _docker  # noqa: F401
        except ImportError:
            raise ImportError(
                "The 'docker' package is required for DockerBackend. "
                "Install with: pip install docker"
            ) from None
        super().__init__(config)
        self._docker_client: Any = None

    async def _get_client(self) -> Any:
        """Get or create Docker client.

        Returns:
            Docker client instance
        """
        if self._docker_client is None:
            try:
                import docker

                self._docker_client = docker.from_env()
            except ImportError:
                raise RuntimeError(
                    "Docker Python client not installed. Install with: pip install docker"
                )
            except Exception as e:
                raise RuntimeError(f"Failed to connect to Docker: {e}")

        return self._docker_client

    def _build_container_config(self) -> dict[str, Any]:
        """Build container configuration with security hardening.

        Returns:
            Docker container configuration dict
        """
        from datetime import UTC, datetime

        # Base configuration
        config: dict[str, Any] = {
            "image": self.config.image,
            "detach": True,
            "tty": True,
            "stdin_open": True,
            "working_dir": self.config.workdir,
            "environment": self.config.env,
            "command": "/bin/bash",
            "labels": {
                "promptise.sandbox": "true",
                "promptise.sandbox.created": datetime.now(UTC).isoformat(),
                "promptise.sandbox.backend": self.config.backend,
            },
        }

        # Network configuration
        if self.config.network == NetworkMode.NONE:
            config["network_mode"] = "none"
        elif self.config.network == NetworkMode.RESTRICTED:
            # Use bridge with DNS filtering (implemented via iptables in container)
            config["network_mode"] = "bridge"
        else:  # FULL
            config["network_mode"] = "bridge"

        # Security options
        security_opt = []

        # Seccomp profile — disabled: Docker applies its default seccomp
        # profile automatically, which blocks ~44 dangerous syscalls
        # (e.g., reboot, mount, kexec_load).  A custom profile would
        # need thorough testing across Python/Node/Rust/Go workloads
        # to avoid breaking legitimate operations.  The default profile
        # is sufficient for sandbox security when combined with gVisor,
        # AppArmor, capability dropping, and read-only root filesystem.

        # AppArmor (only if profile is loaded on the system)
        if self._check_apparmor_available():
            security_opt.append("apparmor=promptise-sandbox")
        else:
            import logging

            logger = logging.getLogger(__name__)
            logger.warning(
                "[sandbox] AppArmor profile 'promptise-sandbox' not found. "
                "AppArmor protection will not be active. "
                "Other security layers (seccomp, capabilities) are still enforced."
            )

        # No new privileges — always enforced, even with allow_sudo
        security_opt.append("no-new-privileges")

        if security_opt:
            config["security_opt"] = security_opt

        # Host config (resource limits and capabilities)
        host_config = {
            "privileged": False,
            # Read-only rootfs is always enforced when configured,
            # regardless of allow_sudo.  Sudo only adds CAP_SYS_ADMIN
            # for package installation, not a full jailbreak.
            "read_only": self.config.read_only_rootfs,
        }

        # CPU limits (in nano CPUs: 1 CPU = 1e9)
        host_config["nano_cpus"] = int(self.config.cpu_limit * 1e9)

        # Memory limit
        memory_bytes = self._parse_size(self.config.memory_limit)
        host_config["mem_limit"] = memory_bytes
        host_config["memswap_limit"] = memory_bytes  # Disable swap

        # Always drop dangerous capabilities.  If allow_sudo is True,
        # add back only CAP_SETUID/CAP_SETGID for sudo functionality.
        # CAP_SYS_ADMIN is NEVER re-added — it enables mount, ptrace,
        # BPF, and namespace manipulation which are container escape vectors.
        host_config["cap_drop"] = list(DEFAULT_CAP_DROP)
        if self.config.allow_sudo:
            for cap in ("SETUID", "SETGID"):
                if cap in host_config["cap_drop"]:
                    host_config["cap_drop"].remove(cap)

        # gVisor runtime
        if self.config.backend == "gvisor" or self.config.runtime == "runsc":
            host_config["runtime"] = "runsc"

        # Tmpfs mounts for writable directories (when using read-only rootfs)
        if host_config.get("read_only"):
            host_config["tmpfs"] = {
                "/tmp": "rw,noexec,nosuid,size=1g",
                "/var/tmp": "rw,noexec,nosuid,size=512m",
                self.config.workdir: "rw,size=5g",
            }

        config["host_config"] = host_config

        return config

    def _check_apparmor_available(self) -> bool:
        """Check if AppArmor profile is loaded on the system.

        Returns:
            True if promptise-sandbox profile is loaded
        """
        import os
        import subprocess

        # Check if AppArmor is available
        if not os.path.exists("/sys/kernel/security/apparmor"):
            return False

        # Check if our profile is loaded
        try:
            result = subprocess.run(
                ["aa-status", "--profiled"],
                capture_output=True,
                text=True,
                timeout=2,
                check=False,
            )
            return "promptise-sandbox" in result.stdout
        except (FileNotFoundError, subprocess.TimeoutExpired):
            return False

    def _parse_size(self, size_str: str) -> int:
        """Parse size string to bytes.

        Args:
            size_str: Size string (e.g., "4G", "512M")

        Returns:
            Size in bytes
        """
        units = {"K": 1024, "M": 1024**2, "G": 1024**3, "T": 1024**4}
        if size_str[-1] in units:
            return int(size_str[:-1]) * units[size_str[-1]]
        return int(size_str)

    async def create_container(self) -> str:
        """Create and start a new Docker container.

        Returns:
            Container ID

        Raises:
            RuntimeError: If container creation fails
        """
        client = await self._get_client()
        config = self._build_container_config()

        try:
            # Pull image if not present
            try:
                client.images.get(self.config.image)
            except Exception:
                logger.info("Pulling image %s...", self.config.image)
                client.images.pull(self.config.image)

            # Modern Docker SDK expects parameters flattened (not nested in host_config)
            host_config_dict = config.pop("host_config", {})
            # Merge host config parameters at top level
            config.update(host_config_dict)

            # Create and start container
            container = client.containers.create(**config)
            container.start()

            # Setup network restrictions if needed
            if self.config.network == NetworkMode.RESTRICTED:
                await self._setup_network_restrictions(container.id)

            return container.id

        except Exception as e:
            raise RuntimeError(f"Failed to create container: {e}")

    async def _setup_network_restrictions(self, container_id: str) -> None:
        """Setup network restrictions using iptables.

        Note: Network restrictions require iptables in the container image.
        If installation fails, the container will have unrestricted network access.

        Args:
            container_id: Container ID
        """
        import logging

        logger = logging.getLogger(__name__)

        # Check if iptables is available
        check_result = await self.execute_command(
            container_id, "command -v iptables", timeout=5, workdir="/tmp"
        )

        if check_result.exit_code != 0:
            logger.warning(
                f"[sandbox] iptables not found in container {container_id}. "
                f"Network restrictions will NOT be enforced. "
                f"Consider using an image with iptables pre-installed."
            )
            print("[sandbox] WARNING: Network restrictions NOT active - iptables not available")
            return

        # Apply iptables rules
        commands = [
            "iptables -A OUTPUT -p udp --dport 53 -j ACCEPT",  # DNS
            "iptables -A OUTPUT -p tcp --dport 443 -j ACCEPT",  # HTTPS
            "iptables -A OUTPUT -p tcp --dport 80 -j ACCEPT",  # HTTP
            "iptables -A OUTPUT -j DROP",  # Drop everything else
        ]

        for cmd in commands:
            result = await self.execute_command(container_id, cmd, timeout=10, workdir="/tmp")
            if result.exit_code != 0:
                logger.error(
                    f"[sandbox] Failed to apply network restriction: {cmd}\nError: {result.stderr}"
                )
                print(
                    "[sandbox] WARNING: Network restrictions may not be fully active - "
                    "iptables command failed"
                )
                return

        logger.info(f"[sandbox] Network restrictions applied successfully to {container_id}")
        print("[sandbox] Network restrictions active (DNS + HTTP/HTTPS only)")

    async def execute_command(
        self, container_id: str, command: str, timeout: int, workdir: str
    ) -> CommandResult:
        """Execute a command in Docker container with proper timeout enforcement.

        Args:
            container_id: Container ID
            command: Shell command to execute
            timeout: Timeout in seconds (actually enforced)
            workdir: Working directory

        Returns:
            CommandResult with execution details
        """
        import asyncio
        from concurrent.futures import ThreadPoolExecutor

        client = await self._get_client()

        try:
            container = client.containers.get(container_id)
            start_time = time.time()

            # Docker SDK is synchronous, so run in thread pool
            def _exec():
                return container.exec_run(
                    cmd=["bash", "-c", command],
                    workdir=workdir,
                    demux=True,  # Separate stdout/stderr
                    environment=self.config.env,
                )

            executor = ThreadPoolExecutor(max_workers=1)

            try:
                # Actually enforce timeout with asyncio
                exec_result = await asyncio.wait_for(
                    asyncio.get_event_loop().run_in_executor(executor, _exec), timeout=timeout
                )
                timed_out = False

                # Decode output
                exit_code = exec_result.exit_code
                stdout_bytes, stderr_bytes = exec_result.output

                stdout = stdout_bytes.decode("utf-8", errors="replace") if stdout_bytes else ""
                stderr = stderr_bytes.decode("utf-8", errors="replace") if stderr_bytes else ""

            except asyncio.TimeoutError:
                # Command timed out - return timeout result
                timed_out = True
                exit_code = -1
                stdout = ""
                stderr = f"Command timed out after {timeout} seconds"

            finally:
                executor.shutdown(wait=False)

            duration = time.time() - start_time

            return CommandResult(
                exit_code=exit_code,
                stdout=stdout,
                stderr=stderr,
                timeout=timed_out,
                duration=duration,
            )

        except Exception as e:
            return CommandResult(
                exit_code=-1,
                stdout="",
                stderr=f"Execution failed: {str(e)}",
                timeout=False,
                duration=0.0,
            )

    async def read_file(self, container_id: str, file_path: str) -> str:
        """Read a file from Docker container.

        Args:
            container_id: Container ID
            file_path: Path to file

        Returns:
            File contents

        Raises:
            FileNotFoundError: If file doesn't exist
        """
        result = await self.execute_command(
            container_id, f"cat {shlex.quote(file_path)}", timeout=10, workdir="/tmp"
        )

        if not result.success:
            raise FileNotFoundError(f"File not found or not readable: {file_path}")

        return result.stdout

    async def write_file(self, container_id: str, file_path: str, content: str) -> None:
        """Write a file to Docker container using Docker API.

        Uses Docker's put_archive API to safely write files without shell injection risks.

        Args:
            container_id: Container ID
            file_path: Path to file
            content: File contents
        """
        import io
        import os
        import tarfile

        client = await self._get_client()

        try:
            container = client.containers.get(container_id)

            # Create tar archive in memory
            tar_stream = io.BytesIO()
            with tarfile.open(fileobj=tar_stream, mode="w") as tar:
                # Create tarinfo for the file
                tarinfo = tarfile.TarInfo(name=os.path.basename(file_path))
                content_bytes = content.encode("utf-8")
                tarinfo.size = len(content_bytes)

                # Add file to archive
                tar.addfile(tarinfo, io.BytesIO(content_bytes))

            # Upload tar archive to container
            tar_stream.seek(0)
            target_dir = os.path.dirname(file_path) or "/"

            success = container.put_archive(path=target_dir, data=tar_stream.getvalue())

            if not success:
                raise RuntimeError(f"Failed to write file: {file_path}")

        except Exception as e:
            raise RuntimeError(f"Failed to write file {file_path}: {e}")

    async def stop_container(self, container_id: str) -> None:
        """Stop Docker container.

        Args:
            container_id: Container ID
        """
        client = await self._get_client()

        try:
            container = client.containers.get(container_id)
            container.stop(timeout=5)
        except Exception as e:
            print(f"[sandbox] Warning: Failed to stop container {container_id}: {e}")

    async def remove_container(self, container_id: str) -> None:
        """Remove Docker container.

        Args:
            container_id: Container ID
        """
        client = await self._get_client()

        try:
            container = client.containers.get(container_id)
            container.remove(force=True)
        except Exception as e:
            print(f"[sandbox] Warning: Failed to remove container {container_id}: {e}")

    async def health_check(self) -> bool:
        """Check if Docker is available and gVisor runtime is configured if requested.

        Returns:
            True if Docker is healthy (and gVisor is available if requested)
        """
        try:
            client = await self._get_client()
            client.ping()

            # If gVisor/runsc is requested, verify it's available
            if self.config.backend == "gvisor" or self.config.runtime == "runsc":
                return await self._verify_gvisor_available(client)

            return True
        except Exception:
            return False

    async def _verify_gvisor_available(self, client: Any) -> bool:
        """Verify gVisor runtime is available in Docker.

        Raises an error if gVisor was explicitly requested but is not
        available — the framework does not silently fall back to runc.

        Args:
            client: Docker client.

        Returns:
            True if gVisor/runsc is available.

        Raises:
            RuntimeError: If gVisor is not installed.
        """
        import logging

        logger = logging.getLogger(__name__)

        try:
            info = client.info()
            runtimes = info.get("Runtimes", {})

            if "runsc" not in runtimes:
                raise RuntimeError(
                    "gVisor runtime 'runsc' is not available in Docker. "
                    "The sandbox was configured to use gVisor but it is not "
                    "installed.  Install gVisor from https://gvisor.dev/docs/"
                    "user_guide/install/ and restart Docker, or set "
                    "backend='docker' to use the default runtime."
                )

            return True

        except RuntimeError:
            raise
        except Exception as exc:
            logger.error("Failed to verify gVisor availability: %s", exc)
            raise RuntimeError(f"Cannot verify gVisor availability: {exc}") from exc
