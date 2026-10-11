"""Tests for sandbox functionality."""

import pytest

from promptise.sandbox import SandboxConfig, SandboxManager
from promptise.sandbox.config import NetworkMode


def test_sandbox_config_defaults():
    """Test default sandbox configuration."""
    config = SandboxConfig()

    assert config.backend == "docker"
    assert config.image == "python:3.11-slim"
    assert config.cpu_limit == 2
    assert config.memory_limit == "4G"
    assert config.disk_limit == "1G"
    assert config.pids_limit == 256
    assert config.network == NetworkMode.NONE
    assert config.persistent is False
    assert config.timeout == 300
    assert config.workdir == "/workspace"
    assert config.allow_sudo is False


def test_sandbox_config_from_simple():
    """Test creating config from simple boolean."""
    config = SandboxConfig.from_simple(True)
    assert config.backend == "docker"

    with pytest.raises(ValueError, match="Cannot create config when sandbox is disabled"):
        SandboxConfig.from_simple(False)


def test_sandbox_config_from_dict():
    """Test creating config from dictionary."""
    config = SandboxConfig.from_dict(
        {"backend": "gvisor", "cpu_limit": 4, "memory_limit": "8G", "network": "full"}
    )

    assert config.backend == "gvisor"
    assert config.cpu_limit == 4
    assert config.memory_limit == "8G"
    assert config.network == NetworkMode.FULL


def test_sandbox_config_validation():
    """Test configuration validation."""
    # Valid memory format
    config = SandboxConfig(memory_limit="2G")
    assert config.memory_limit == "2G"

    # Invalid memory format
    with pytest.raises(ValueError):
        SandboxConfig(memory_limit="invalid")

    with pytest.raises(ValueError):
        SandboxConfig(memory_limit="2")  # Missing unit


def test_sandbox_manager_creation():
    """Test sandbox manager initialization."""
    # From boolean
    manager = SandboxManager(True)
    assert isinstance(manager.config, SandboxConfig)
    assert manager.config.backend == "docker"

    # From dict
    manager = SandboxManager({"backend": "gvisor", "cpu_limit": 4})
    assert manager.config.backend == "gvisor"
    assert manager.config.cpu_limit == 4

    # From config object
    config = SandboxConfig(backend="gvisor")
    manager = SandboxManager(config)
    assert manager.config.backend == "gvisor"

    # Invalid
    with pytest.raises(ValueError):
        SandboxManager(False)


def test_sandbox_manager_backend_selection():
    """Test backend selection logic."""
    # Docker backend
    manager = SandboxManager({"backend": "docker"})
    assert manager.backend.__class__.__name__ == "DockerBackend"

    # gVisor backend (Docker with runsc runtime)
    manager = SandboxManager({"backend": "gvisor"})
    assert manager.backend.__class__.__name__ == "DockerBackend"
    assert manager.config.runtime == "runsc"

    # Unsupported backend rejected by Pydantic Literal validation
    with pytest.raises(ValueError):
        SandboxManager({"backend": "lxc"})


@pytest.mark.asyncio
async def test_sandbox_session_context_manager():
    """Test sandbox session lifecycle with context manager."""
    from unittest.mock import AsyncMock, MagicMock

    from promptise.sandbox.session import SandboxSession

    # Mock backend
    backend = MagicMock()
    backend.kill_container = AsyncMock()
    backend.remove_container = AsyncMock()

    # Mock config
    config = MagicMock()
    config.persistent = False

    # Create session and use as context manager
    async with SandboxSession("test-container", backend, config) as session:
        assert session.container_id == "test-container"
        assert session._running is True

    # After exit, cleanup should be called: the container is removed at once,
    # so it is killed rather than stopped gracefully (that waited 5 s).
    backend.kill_container.assert_called_once_with("test-container")
    backend.remove_container.assert_called_once_with("test-container")


@pytest.mark.asyncio
async def test_sandbox_tools_creation():
    """Test sandbox tools creation."""
    from unittest.mock import MagicMock

    from promptise.sandbox.session import SandboxSession
    from promptise.sandbox.tools import create_sandbox_tools

    # Mock session
    session = MagicMock(spec=SandboxSession)

    # Create tools
    tools = create_sandbox_tools(session)

    assert len(tools) == 5
    tool_names = [tool.name for tool in tools]
    assert "sandbox_exec" in tool_names
    assert "sandbox_read_file" in tool_names
    assert "sandbox_write_file" in tool_names
    assert "sandbox_list_files" in tool_names
    assert "sandbox_install_package" in tool_names


def test_network_mode_enum():
    """Test NetworkMode enum values."""
    assert NetworkMode.NONE.value == "none"
    assert NetworkMode.RESTRICTED.value == "restricted"
    assert NetworkMode.FULL.value == "full"


@pytest.mark.asyncio
async def test_docker_backend_kills_instead_of_waiting_for_a_graceful_stop():
    """A sandbox's PID 1 is bash, which ignores SIGTERM, so ``docker stop``
    always waited its 5 s timeout. Cleanup now kills the container."""
    from unittest.mock import AsyncMock, MagicMock

    from promptise.sandbox import DockerBackend, SandboxConfig
    from promptise.sandbox.session import SandboxSession

    container = MagicMock()
    client = MagicMock()
    client.containers.get.return_value = container
    backend = DockerBackend(SandboxConfig())
    backend._get_client = AsyncMock(return_value=client)  # type: ignore[method-assign]

    session = SandboxSession("cid", backend, SandboxConfig())
    await session.cleanup()

    container.kill.assert_called_once_with()
    container.stop.assert_not_called()
    container.remove.assert_called_once_with(force=True)


@pytest.mark.asyncio
async def test_backend_kill_container_defaults_to_stop():
    """A custom backend without kill_container still cleans up (via stop)."""
    from unittest.mock import AsyncMock

    from promptise.sandbox.backends import SandboxBackend

    class Minimal(SandboxBackend):
        create_container = AsyncMock()
        execute_command = AsyncMock()
        read_file = AsyncMock()
        write_file = AsyncMock()
        stop_container = AsyncMock()
        remove_container = AsyncMock()
        health_check = AsyncMock()

    backend = Minimal(SandboxConfig())
    await backend.kill_container("cid")
    backend.stop_container.assert_awaited_once_with("cid")
