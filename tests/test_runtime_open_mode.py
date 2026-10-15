"""Tests for open/strict execution modes, hot-reload, and meta-tools.

Tests that:
- ExecutionMode enum and OpenModeConfig work correctly
- ProcessConfig validates execution mode fields
- AgentProcess._hot_reload() rebuilds the agent in open mode
- AgentProcess._hot_reload() raises in strict mode
- Meta-tools are created correctly and gated by permissions
- Rollback restores original configuration
- Dynamic triggers can be added/removed
- Manifest supports execution_mode and open_mode
"""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from promptise.runtime.config import (
    ContextConfig,
    ExecutionMode,
    OpenModeConfig,
    ProcessConfig,
)
from promptise.runtime.process import AgentProcess

# ===========================================================================
# ExecutionMode tests
# ===========================================================================


class TestExecutionMode:
    """Tests for ExecutionMode enum."""

    def test_strict_value(self):
        assert ExecutionMode.STRICT == "strict"
        assert ExecutionMode.STRICT.value == "strict"

    def test_open_value(self):
        assert ExecutionMode.OPEN == "open"
        assert ExecutionMode.OPEN.value == "open"

    def test_from_string(self):
        assert ExecutionMode("strict") == ExecutionMode.STRICT
        assert ExecutionMode("open") == ExecutionMode.OPEN

    def test_invalid_raises(self):
        with pytest.raises(ValueError):
            ExecutionMode("invalid")

    def test_string_comparison(self):
        assert ExecutionMode.STRICT == "strict"
        assert ExecutionMode.OPEN == "open"


# ===========================================================================
# OpenModeConfig tests
# ===========================================================================


class TestOpenModeConfig:
    """Tests for OpenModeConfig Pydantic model."""

    def test_defaults(self):
        cfg = OpenModeConfig()
        assert cfg.allow_identity_change is True
        assert cfg.allow_tool_creation is True
        assert cfg.allow_mcp_connect is True
        assert cfg.allow_trigger_management is True
        assert cfg.allow_memory_management is True
        assert cfg.max_custom_tools == 20
        assert cfg.max_dynamic_triggers == 10
        assert cfg.max_instruction_length == 10_000
        assert cfg.max_rebuilds is None
        assert cfg.allowed_mcp_urls == []
        assert cfg.sandbox_custom_tools is True

    def test_custom_values(self):
        cfg = OpenModeConfig(
            allow_identity_change=False,
            allow_tool_creation=False,
            max_custom_tools=5,
            max_rebuilds=10,
            allowed_mcp_urls=["http://localhost:8000"],
        )
        assert cfg.allow_identity_change is False
        assert cfg.allow_tool_creation is False
        assert cfg.max_custom_tools == 5
        assert cfg.max_rebuilds == 10
        assert cfg.allowed_mcp_urls == ["http://localhost:8000"]

    def test_serialization_roundtrip(self):
        cfg = OpenModeConfig(
            allow_identity_change=False,
            max_rebuilds=5,
        )
        d = cfg.model_dump()
        restored = OpenModeConfig.model_validate(d)
        assert restored.allow_identity_change is False
        assert restored.max_rebuilds == 5

    def test_max_instruction_length_min(self):
        with pytest.raises(Exception):
            OpenModeConfig(max_instruction_length=50)  # Below min of 100


# ===========================================================================
# ProcessConfig execution mode tests
# ===========================================================================


class TestProcessConfigExecutionMode:
    """Tests for execution mode fields on ProcessConfig."""

    def test_default_is_strict(self):
        config = ProcessConfig()
        assert config.execution_mode == ExecutionMode.STRICT

    def test_open_mode(self):
        config = ProcessConfig(execution_mode=ExecutionMode.OPEN)
        assert config.execution_mode == ExecutionMode.OPEN

    def test_open_mode_config_included(self):
        config = ProcessConfig(
            execution_mode=ExecutionMode.OPEN,
            open_mode=OpenModeConfig(max_custom_tools=5),
        )
        assert config.open_mode.max_custom_tools == 5

    def test_serialization(self):
        config = ProcessConfig(
            execution_mode=ExecutionMode.OPEN,
            open_mode=OpenModeConfig(allow_identity_change=False),
        )
        d = config.model_dump()
        assert d["execution_mode"] == "open"
        assert d["open_mode"]["allow_identity_change"] is False

        restored = ProcessConfig.model_validate(d)
        assert restored.execution_mode == ExecutionMode.OPEN
        assert restored.open_mode.allow_identity_change is False


# ===========================================================================
# AgentProcess hot-reload tests
# ===========================================================================


class TestHotReload:
    """Tests for AgentProcess._hot_reload()."""

    @pytest.mark.asyncio
    async def test_hot_reload_raises_in_strict_mode(self):
        process = AgentProcess(
            name="test",
            config=ProcessConfig(execution_mode=ExecutionMode.STRICT),
        )
        with pytest.raises(RuntimeError, match="open mode"):
            await process._hot_reload(reason="test")

    @pytest.mark.asyncio
    async def test_hot_reload_in_open_mode(self):
        process = AgentProcess(
            name="test",
            config=ProcessConfig(execution_mode=ExecutionMode.OPEN),
        )
        # Mock _build_agent
        process._build_agent = AsyncMock()
        process._agent = MagicMock()
        process._agent.shutdown = AsyncMock()

        result = await process._hot_reload(reason="test reload")
        assert result == "Rebuild successful"
        assert process._rebuild_count == 1
        process._build_agent.assert_called_once()
        process._agent.shutdown.assert_called_once()

    @pytest.mark.asyncio
    async def test_hot_reload_preserves_conversation(self):
        process = AgentProcess(
            name="test",
            config=ProcessConfig(execution_mode=ExecutionMode.OPEN),
        )
        # Pre-fill conversation
        process._conversation_buffer.append({"role": "user", "content": "keep me"})

        process._build_agent = AsyncMock()
        process._agent = MagicMock()
        process._agent.shutdown = AsyncMock()

        await process._hot_reload(reason="test")

        msgs = process._conversation_buffer.get_messages()
        assert len(msgs) == 1
        assert msgs[0]["content"] == "keep me"

    @pytest.mark.asyncio
    async def test_hot_reload_max_rebuilds(self):
        process = AgentProcess(
            name="test",
            config=ProcessConfig(
                execution_mode=ExecutionMode.OPEN,
                open_mode=OpenModeConfig(max_rebuilds=2),
            ),
        )
        process._build_agent = AsyncMock()
        process._agent = MagicMock()
        process._agent.shutdown = AsyncMock()

        # First two succeed
        await process._hot_reload(reason="r1")
        await process._hot_reload(reason="r2")
        assert process._rebuild_count == 2

        # Third should fail
        result = await process._hot_reload(reason="r3")
        assert "max rebuilds" in result
        assert process._rebuild_count == 2  # Not incremented

    @pytest.mark.asyncio
    async def test_hot_reload_without_existing_agent(self):
        """First build — no agent to shutdown."""
        process = AgentProcess(
            name="test",
            config=ProcessConfig(execution_mode=ExecutionMode.OPEN),
        )
        process._build_agent = AsyncMock()
        # _agent is None — no shutdown needed

        result = await process._hot_reload(reason="first build")
        assert result == "Rebuild successful"
        assert process._rebuild_count == 1


# ===========================================================================
# Rollback tests
# ===========================================================================


class TestRollback:
    """Tests for AgentProcess.rollback()."""

    @pytest.mark.asyncio
    async def test_rollback_raises_in_strict_mode(self):
        process = AgentProcess(
            name="test",
            config=ProcessConfig(execution_mode=ExecutionMode.STRICT),
        )
        with pytest.raises(RuntimeError, match="open mode"):
            await process.rollback()

    @pytest.mark.asyncio
    async def test_rollback_clears_dynamic_state(self):
        process = AgentProcess(
            name="test",
            config=ProcessConfig(execution_mode=ExecutionMode.OPEN),
        )
        # Set some dynamic state
        process._dynamic_instructions = "modified!"
        process._custom_tools = [MagicMock(name="tool1")]
        process._dynamic_servers = {"s1": {"url": "http://x"}}

        mock_trigger = MagicMock()
        mock_trigger.stop = AsyncMock()
        process._dynamic_triggers = [mock_trigger]

        process._build_agent = AsyncMock()
        process._agent = MagicMock()
        process._agent.shutdown = AsyncMock()

        await process.rollback()

        assert process._dynamic_instructions is None
        assert process._custom_tools == []
        assert process._dynamic_servers == {}
        assert process._dynamic_triggers == []
        mock_trigger.stop.assert_called_once()


# ===========================================================================
# Meta-tools creation tests
# ===========================================================================


class TestMetaToolCreation:
    """Tests for create_meta_tools factory."""

    def test_all_tools_created_with_defaults(self):
        from promptise.runtime.meta_tools import create_meta_tools

        process = AgentProcess(
            name="test",
            config=ProcessConfig(execution_mode=ExecutionMode.OPEN),
        )
        tools = create_meta_tools(process)

        tool_names = {t.name for t in tools}
        assert "modify_instructions" in tool_names
        assert "create_tool" in tool_names
        assert "connect_mcp_server" in tool_names
        assert "add_trigger" in tool_names
        assert "remove_trigger" in tool_names
        assert "store_memory" in tool_names
        assert "search_memory" in tool_names
        assert "forget_memory" in tool_names
        assert "list_capabilities" in tool_names

    def test_identity_change_disabled(self):
        from promptise.runtime.meta_tools import create_meta_tools

        process = AgentProcess(
            name="test",
            config=ProcessConfig(
                execution_mode=ExecutionMode.OPEN,
                open_mode=OpenModeConfig(allow_identity_change=False),
            ),
        )
        tools = create_meta_tools(process)
        tool_names = {t.name for t in tools}
        assert "modify_instructions" not in tool_names
        assert "list_capabilities" in tool_names  # Always present

    def test_tool_creation_disabled(self):
        from promptise.runtime.meta_tools import create_meta_tools

        process = AgentProcess(
            name="test",
            config=ProcessConfig(
                execution_mode=ExecutionMode.OPEN,
                open_mode=OpenModeConfig(allow_tool_creation=False),
            ),
        )
        tools = create_meta_tools(process)
        tool_names = {t.name for t in tools}
        assert "create_tool" not in tool_names

    def test_mcp_connect_disabled(self):
        from promptise.runtime.meta_tools import create_meta_tools

        process = AgentProcess(
            name="test",
            config=ProcessConfig(
                execution_mode=ExecutionMode.OPEN,
                open_mode=OpenModeConfig(allow_mcp_connect=False),
            ),
        )
        tools = create_meta_tools(process)
        tool_names = {t.name for t in tools}
        assert "connect_mcp_server" not in tool_names

    def test_trigger_management_disabled(self):
        from promptise.runtime.meta_tools import create_meta_tools

        process = AgentProcess(
            name="test",
            config=ProcessConfig(
                execution_mode=ExecutionMode.OPEN,
                open_mode=OpenModeConfig(allow_trigger_management=False),
            ),
        )
        tools = create_meta_tools(process)
        tool_names = {t.name for t in tools}
        assert "add_trigger" not in tool_names
        assert "remove_trigger" not in tool_names

    def test_memory_management_disabled(self):
        from promptise.runtime.meta_tools import create_meta_tools

        process = AgentProcess(
            name="test",
            config=ProcessConfig(
                execution_mode=ExecutionMode.OPEN,
                open_mode=OpenModeConfig(allow_memory_management=False),
            ),
        )
        tools = create_meta_tools(process)
        tool_names = {t.name for t in tools}
        assert "store_memory" not in tool_names
        assert "search_memory" not in tool_names
        assert "forget_memory" not in tool_names

    def test_all_disabled_still_has_list_capabilities(self):
        from promptise.runtime.meta_tools import create_meta_tools

        process = AgentProcess(
            name="test",
            config=ProcessConfig(
                execution_mode=ExecutionMode.OPEN,
                open_mode=OpenModeConfig(
                    allow_identity_change=False,
                    allow_tool_creation=False,
                    allow_mcp_connect=False,
                    allow_trigger_management=False,
                    allow_memory_management=False,
                ),
            ),
        )
        tools = create_meta_tools(process)
        assert len(tools) == 1
        assert tools[0].name == "list_capabilities"


# ===========================================================================
# Meta-tool implementation tests
# ===========================================================================


class TestModifyInstructionsTool:
    """Tests for modify_instructions meta-tool."""

    @pytest.mark.asyncio
    async def test_modifies_instructions(self):
        from promptise.runtime.meta_tools import _modify_instructions

        process = AgentProcess(
            name="test",
            config=ProcessConfig(execution_mode=ExecutionMode.OPEN),
        )
        process._build_agent = AsyncMock()
        process._agent = MagicMock()
        process._agent.shutdown = AsyncMock()

        result = await _modify_instructions(process, "You are now a poet.")
        assert process._dynamic_instructions == "You are now a poet."
        assert "successful" in result.lower() or "Rebuild" in result

    @pytest.mark.asyncio
    async def test_rejects_too_long(self):
        from promptise.runtime.meta_tools import _modify_instructions

        process = AgentProcess(
            name="test",
            config=ProcessConfig(
                execution_mode=ExecutionMode.OPEN,
                open_mode=OpenModeConfig(max_instruction_length=100),
            ),
        )
        result = await _modify_instructions(process, "x" * 200)
        assert "Error" in result
        assert "max length" in result
        assert process._dynamic_instructions is None  # Not modified


class TestCreateToolTool:
    """Tests for create_tool meta-tool."""

    @pytest.mark.asyncio
    async def test_creates_tool(self):
        from promptise.runtime.meta_tools import _create_tool

        process = AgentProcess(
            name="test",
            config=ProcessConfig(execution_mode=ExecutionMode.OPEN),
        )
        process._build_agent = AsyncMock()
        process._agent = MagicMock()
        process._agent.shutdown = AsyncMock()

        result = await _create_tool(
            process,
            tool_name="greet",
            tool_description="Say hello",
            parameters={},
            python_code="def run(**kwargs): return 'hello'",
        )
        assert "greet" in result
        assert len(process._custom_tools) == 1

    @pytest.mark.asyncio
    async def test_result_and_description_say_the_tool_is_for_the_next_run(self):
        """The run that creates a tool keeps its old tool list; the model is
        told so instead of looking for the tool and re-creating it."""
        from promptise.runtime.meta_tools import _create_tool, create_meta_tools

        process = AgentProcess(
            name="test",
            config=ProcessConfig(execution_mode=ExecutionMode.OPEN),
        )
        process._build_agent = AsyncMock()
        process._agent = MagicMock()
        process._agent.shutdown = AsyncMock()

        result = await _create_tool(
            process,
            tool_name="greet",
            tool_description="Say hello",
            parameters={},
            python_code="def run(input): return 'hello'",
        )
        assert result.startswith("Tool 'greet' created.")
        assert "next run, not in this one" in result

        tools = {t.name: t for t in create_meta_tools(process)}
        assert "next run" in tools["create_tool"].description
        assert "run(**kwargs)" not in tools["create_tool"].description
        assert "next run" in tools["connect_mcp_server"].description

    @pytest.mark.asyncio
    async def test_rejects_at_max_tools(self):
        from promptise.runtime.meta_tools import _create_tool

        process = AgentProcess(
            name="test",
            config=ProcessConfig(
                execution_mode=ExecutionMode.OPEN,
                open_mode=OpenModeConfig(max_custom_tools=1),
            ),
        )
        process._custom_tools = [MagicMock(name="existing")]

        result = await _create_tool(
            process,
            tool_name="new",
            tool_description="desc",
            parameters={},
            python_code="def run(): return 'x'",
        )
        assert "Error" in result
        assert "max" in result

    @pytest.mark.asyncio
    async def test_rejects_duplicate_name(self):
        from promptise.runtime.meta_tools import _create_tool

        process = AgentProcess(
            name="test",
            config=ProcessConfig(execution_mode=ExecutionMode.OPEN),
        )
        mock_tool = MagicMock()
        mock_tool.name = "greet"
        process._custom_tools = [mock_tool]

        process._build_agent = AsyncMock()
        process._agent = MagicMock()
        process._agent.shutdown = AsyncMock()

        result = await _create_tool(
            process,
            tool_name="greet",
            tool_description="desc",
            parameters={},
            python_code="def run(): return 'x'",
        )
        assert "Error" in result
        assert "already exists" in result


class TestConnectMCPServerTool:
    """Tests for connect_mcp_server meta-tool."""

    @pytest.mark.asyncio
    async def test_rejects_non_whitelisted_url(self):
        from promptise.runtime.meta_tools import _connect_mcp_server

        process = AgentProcess(
            name="test",
            config=ProcessConfig(
                execution_mode=ExecutionMode.OPEN,
                open_mode=OpenModeConfig(allowed_mcp_urls=["http://allowed.com"]),
            ),
        )
        result = await _connect_mcp_server(
            process,
            server_name="evil",
            url="http://evil.com",
        )
        assert "Error" in result
        assert "whitelist" in result

    @pytest.mark.asyncio
    async def test_rejects_duplicate_server(self):
        from promptise.runtime.meta_tools import _connect_mcp_server

        process = AgentProcess(
            name="test",
            config=ProcessConfig(execution_mode=ExecutionMode.OPEN),
        )
        process._dynamic_servers = {"existing": {"url": "http://x"}}

        result = await _connect_mcp_server(
            process,
            server_name="existing",
            url="http://new.com",
        )
        assert "Error" in result
        assert "already connected" in result

    @pytest.mark.asyncio
    async def test_result_says_the_tools_are_for_the_next_run(self):
        from promptise.runtime.meta_tools import _connect_mcp_server

        process = AgentProcess(
            name="test",
            config=ProcessConfig(execution_mode=ExecutionMode.OPEN),
        )
        process._hot_reload = AsyncMock(return_value="Rebuild successful")

        result = await _connect_mcp_server(
            process, server_name="extra", url="http://127.0.0.1:1/mcp"
        )
        assert result.startswith("Connected to MCP server 'extra'")
        assert "next run, not in this one" in result


class TestAddRemoveTriggerTool:
    """Tests for add_trigger and remove_trigger meta-tools."""

    @pytest.mark.asyncio
    async def test_add_trigger_max_limit(self):
        from promptise.runtime.meta_tools import _add_trigger

        process = AgentProcess(
            name="test",
            config=ProcessConfig(
                execution_mode=ExecutionMode.OPEN,
                open_mode=OpenModeConfig(max_dynamic_triggers=0),
            ),
        )
        result = await _add_trigger(
            process,
            trigger_type="cron",
            cron_expression="*/1 * * * *",
        )
        assert "Error" in result
        assert "max" in result

    @pytest.mark.asyncio
    async def test_remove_trigger_not_found(self):
        from promptise.runtime.meta_tools import _remove_trigger

        process = AgentProcess(
            name="test",
            config=ProcessConfig(execution_mode=ExecutionMode.OPEN),
        )
        result = await _remove_trigger(process, "nonexistent-id")
        assert "Error" in result
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_remove_trigger_success(self):
        from promptise.runtime.meta_tools import _remove_trigger

        process = AgentProcess(
            name="test",
            config=ProcessConfig(execution_mode=ExecutionMode.OPEN),
        )
        mock_trigger = MagicMock()
        mock_trigger.trigger_id = "dyn-1"
        mock_trigger.stop = AsyncMock()
        process._dynamic_triggers = [mock_trigger]

        result = await _remove_trigger(process, "dyn-1")
        assert "removed" in result
        assert len(process._dynamic_triggers) == 0
        mock_trigger.stop.assert_called_once()


class TestMemoryMetaTools:
    """Tests for store_memory, search_memory, forget_memory tools."""

    @pytest.mark.asyncio
    async def test_store_memory_no_provider(self):
        from promptise.runtime.meta_tools import _store_memory

        process = AgentProcess(
            name="test",
            config=ProcessConfig(),
        )
        result = await _store_memory(process, "hello")
        assert "Error" in result
        assert "no memory" in result.lower()

    @pytest.mark.asyncio
    async def test_store_memory_success(self):
        from promptise.runtime.meta_tools import _store_memory

        process = AgentProcess(
            name="test",
            config=ProcessConfig(
                context=ContextConfig(memory_provider="in_memory"),
            ),
        )
        result = await _store_memory(process, "remember this")
        assert "Stored" in result

    @pytest.mark.asyncio
    async def test_search_memory_no_provider(self):
        from promptise.runtime.meta_tools import _search_memory

        process = AgentProcess(
            name="test",
            config=ProcessConfig(),
        )
        result = await _search_memory(process, "query")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_search_memory_success(self):
        from promptise.runtime.meta_tools import _search_memory, _store_memory

        process = AgentProcess(
            name="test",
            config=ProcessConfig(
                context=ContextConfig(memory_provider="in_memory"),
            ),
        )
        await _store_memory(process, "the sky is blue")
        result = await _search_memory(process, "sky")
        assert "sky is blue" in result

    @pytest.mark.asyncio
    async def test_search_memory_no_results(self):
        from promptise.runtime.meta_tools import _search_memory

        process = AgentProcess(
            name="test",
            config=ProcessConfig(
                context=ContextConfig(memory_provider="in_memory"),
            ),
        )
        result = await _search_memory(process, "nonexistent")
        assert "No relevant memories" in result

    @pytest.mark.asyncio
    async def test_forget_memory_no_provider(self):
        from promptise.runtime.meta_tools import _forget_memory

        process = AgentProcess(
            name="test",
            config=ProcessConfig(),
        )
        result = await _forget_memory(process, "some-id")
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_forget_memory_not_found(self):
        from promptise.runtime.meta_tools import _forget_memory

        process = AgentProcess(
            name="test",
            config=ProcessConfig(
                context=ContextConfig(memory_provider="in_memory"),
            ),
        )
        result = await _forget_memory(process, "nonexistent")
        assert "not found" in result

    @pytest.mark.asyncio
    async def test_store_and_forget(self):
        from promptise.runtime.meta_tools import (
            _forget_memory,
            _store_memory,
        )

        process = AgentProcess(
            name="test",
            config=ProcessConfig(
                context=ContextConfig(memory_provider="in_memory"),
            ),
        )
        store_result = await _store_memory(process, "temp data")
        # Extract memory_id
        assert "id=" in store_result
        memory_id = store_result.split("id=")[1].rstrip(").")

        forget_result = await _forget_memory(process, memory_id)
        assert "deleted" in forget_result


class TestListCapabilitiesTool:
    """Tests for list_capabilities meta-tool."""

    @pytest.mark.asyncio
    async def test_list_capabilities(self):
        from promptise.runtime.meta_tools import _list_capabilities

        process = AgentProcess(
            name="test-agent",
            config=ProcessConfig(
                execution_mode=ExecutionMode.OPEN,
                model="openai:gpt-5-mini",
                instructions="You are a test agent.",
            ),
        )
        result = await _list_capabilities(process)
        assert "test-agent" in result
        assert "open" in result
        assert "gpt-5-mini" in result
        assert "test agent" in result


# ===========================================================================
# Dynamic tool builder tests
# ===========================================================================


class TestDynamicToolBuilder:
    """Tests for _build_dynamic_tool."""

    @pytest.mark.asyncio
    async def test_build_and_execute(self):
        from promptise.runtime.meta_tools import _build_dynamic_tool

        tool = _build_dynamic_tool(
            name="greet",
            description="Say hello",
            python_code="def run(**kwargs): return 'Hello, World!'",
        )
        assert tool.name == "greet"

        # Execute
        result = await tool.ainvoke({})
        assert "Hello, World!" in result

    @pytest.mark.asyncio
    async def test_missing_run_function(self):
        from promptise.runtime.meta_tools import _build_dynamic_tool

        tool = _build_dynamic_tool(
            name="bad",
            description="Bad tool",
            python_code="x = 42",  # No 'run' function
        )
        result = await tool.ainvoke({})
        assert "Error" in result
        assert "run" in result

    @pytest.mark.asyncio
    async def test_runtime_error_in_tool(self):
        from promptise.runtime.meta_tools import _build_dynamic_tool

        tool = _build_dynamic_tool(
            name="crasher",
            description="Crashes",
            python_code="def run(**kwargs): raise ValueError('boom')",
        )
        result = await tool.ainvoke({})
        assert "Error" in result
        assert "boom" in result


# ===========================================================================
# Sandbox execution tests
# ===========================================================================


class TestSandboxExecution:
    """Tests for sandbox execution in _build_dynamic_tool."""

    @pytest.mark.asyncio
    async def test_sandbox_blocks_imports(self):
        """Sandboxed tools cannot import modules."""
        from promptise.runtime.meta_tools import _build_dynamic_tool

        tool = _build_dynamic_tool(
            name="importer",
            description="Tries to import os",
            python_code="import os\ndef run(**kwargs): return os.getcwd()",
            sandbox=True,
        )
        result = await tool.ainvoke({})
        assert "Error" in result
        assert "import" in result.lower() or "Import" in result

    @pytest.mark.asyncio
    async def test_sandbox_blocks_open(self):
        """Sandboxed tools cannot use open()."""
        from promptise.runtime.meta_tools import _build_dynamic_tool

        tool = _build_dynamic_tool(
            name="file_reader",
            description="Tries to read a file",
            python_code="def run(**kwargs): return open('/etc/passwd').read()",
            sandbox=True,
        )
        result = await tool.ainvoke({})
        assert "Error" in result

    @pytest.mark.asyncio
    async def test_sandbox_allows_safe_builtins(self):
        """Sandboxed tools can use safe builtins like len, range, etc."""
        from promptise.runtime.meta_tools import _build_dynamic_tool

        tool = _build_dynamic_tool(
            name="safe_tool",
            description="Uses safe builtins",
            python_code=(
                "def run(**kwargs):\n    items = list(range(5))\n    return str(len(items))"
            ),
            sandbox=True,
        )
        result = await tool.ainvoke({})
        assert "5" in result

    @pytest.mark.asyncio
    async def test_no_sandbox_allows_imports(self):
        """Non-sandboxed tools can import modules."""
        from promptise.runtime.meta_tools import _build_dynamic_tool

        tool = _build_dynamic_tool(
            name="importer",
            description="Imports json",
            python_code=("import json\ndef run(**kwargs): return json.dumps({'ok': True})"),
            sandbox=False,
        )
        result = await tool.ainvoke({})
        assert "ok" in result
        assert "true" in result.lower()

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "code, error",
        [
            ("def run(**kwargs): return open('/etc/hosts').read()", "name 'open' is not defined"),
            ("import os\ndef run(**kwargs): return os.getcwd()", "Import of 'os' is not allowed"),
            (
                "from decimal import Decimal\ndef run(**kwargs): return str(Decimal(1))",
                "Import of 'decimal' is not allowed",
            ),
        ],
    )
    async def test_no_sandbox_is_not_full_builtins(self, code, error):
        """docs/runtime/meta-tools.md: sandbox_custom_tools=False keeps the
        restricted builtins and only allows an import allowlist (the docs
        used to say "full Python builtins")."""
        from promptise.runtime.meta_tools import _build_dynamic_tool

        tool = _build_dynamic_tool(
            name="probe", description="probe", python_code=code, sandbox=False
        )
        assert error in await tool.ainvoke({})

    def test_docs_describe_sandbox_false_as_it_behaves(self):
        from pathlib import Path

        page = Path(__file__).resolve().parent.parent / "docs/runtime/meta-tools.md"
        text = page.read_text(encoding="utf-8")
        assert "the code runs with full Python builtins" not in text
        assert "**same restricted builtins**" in text

    @pytest.mark.asyncio
    async def test_sandbox_blocks_exec(self):
        """Sandboxed tools cannot use exec/eval."""
        from promptise.runtime.meta_tools import _build_dynamic_tool

        tool = _build_dynamic_tool(
            name="exec_tool",
            description="Tries to use exec",
            python_code="def run(**kwargs): return str(eval('1+1'))",
            sandbox=True,
        )
        result = await tool.ainvoke({})
        assert "Error" in result


# ===========================================================================
# memory_auto_store wiring tests
# ===========================================================================


class TestMemoryAutoStoreWiring:
    """Tests that memory_auto_store is wired through to build_agent."""

    @pytest.mark.asyncio
    async def test_auto_store_passed_to_build(self):
        """Verify memory_auto_store from ContextConfig reaches build_agent."""
        process = AgentProcess(
            name="test",
            config=ProcessConfig(
                context=ContextConfig(
                    memory_provider="in_memory",
                    memory_auto_store=True,
                ),
            ),
        )

        with patch(
            "promptise.agent.build_agent",
            new_callable=AsyncMock,
        ) as mock_build:
            mock_build.return_value = MagicMock()
            await process._build_agent()

            call_kwargs = mock_build.call_args[1]
            assert call_kwargs["memory_auto_store"] is True

    @pytest.mark.asyncio
    async def test_auto_store_defaults_false(self):
        """Verify memory_auto_store defaults to False."""
        process = AgentProcess(
            name="test",
            config=ProcessConfig(),
        )

        with patch(
            "promptise.agent.build_agent",
            new_callable=AsyncMock,
        ) as mock_build:
            mock_build.return_value = MagicMock()
            await process._build_agent()

            call_kwargs = mock_build.call_args[1]
            assert call_kwargs["memory_auto_store"] is False


# ===========================================================================
# ConversationBuffer async safety tests
# ===========================================================================


class TestConversationBufferAsync:
    """Tests for async-safe ConversationBuffer methods."""

    @pytest.mark.asyncio
    async def test_async_snapshot(self):
        from promptise.runtime.conversation import ConversationBuffer

        buf = ConversationBuffer()
        buf.append({"role": "user", "content": "hello"})
        buf.append({"role": "assistant", "content": "hi"})

        snapshot = await buf.async_snapshot()
        assert len(snapshot) == 2
        assert snapshot[0]["content"] == "hello"

    @pytest.mark.asyncio
    async def test_async_replace(self):
        from promptise.runtime.conversation import ConversationBuffer

        buf = ConversationBuffer()
        buf.append({"role": "user", "content": "old"})

        await buf.async_replace(
            [
                {"role": "user", "content": "new1"},
                {"role": "assistant", "content": "new2"},
            ]
        )
        assert len(buf) == 2
        assert buf.get_messages()[0]["content"] == "new1"

    @pytest.mark.asyncio
    async def test_async_append(self):
        from promptise.runtime.conversation import ConversationBuffer

        buf = ConversationBuffer()
        await buf.async_append({"role": "user", "content": "async hello"})
        assert len(buf) == 1
        assert buf.get_messages()[0]["content"] == "async hello"

    @pytest.mark.asyncio
    async def test_async_concurrent_access(self):
        """Verify concurrent async operations don't corrupt the buffer."""
        from promptise.runtime.conversation import ConversationBuffer

        buf = ConversationBuffer(max_messages=1000)

        async def append_batch(start: int, count: int) -> None:
            for i in range(start, start + count):
                await buf.async_append({"role": "user", "content": f"msg-{i}"})

        # Run 5 concurrent batches of 20 messages each
        tasks = [append_batch(i * 20, 20) for i in range(5)]
        await asyncio.gather(*tasks)

        # All 100 messages should be present
        msgs = await buf.async_snapshot()
        assert len(msgs) == 100


# ===========================================================================
# Manifest tests for execution_mode
# ===========================================================================


class TestManifestExecutionMode:
    """Tests for execution_mode in manifest schema."""

    def test_manifest_accepts_execution_mode(self):
        from promptise.runtime.manifest import AgentManifestSchema

        manifest = AgentManifestSchema(
            name="test",
            execution_mode="open",
            open_mode={
                "allow_identity_change": True,
                "max_custom_tools": 5,
            },
        )
        assert manifest.execution_mode == "open"
        assert manifest.open_mode is not None
        assert manifest.open_mode["max_custom_tools"] == 5

    def test_manifest_to_process_config_with_open_mode(self):
        from promptise.runtime.manifest import (
            AgentManifestSchema,
            manifest_to_process_config,
        )

        manifest = AgentManifestSchema(
            name="test",
            execution_mode="open",
            open_mode={
                "allow_identity_change": False,
                "max_rebuilds": 10,
            },
        )
        config = manifest_to_process_config(manifest)
        assert config.execution_mode == ExecutionMode.OPEN
        assert config.open_mode.allow_identity_change is False
        assert config.open_mode.max_rebuilds == 10

    def test_manifest_default_strict(self):
        from promptise.runtime.manifest import (
            AgentManifestSchema,
            manifest_to_process_config,
        )

        manifest = AgentManifestSchema(name="test")
        config = manifest_to_process_config(manifest)
        assert config.execution_mode == ExecutionMode.STRICT

    def test_manifest_with_memory_config(self):
        from promptise.runtime.manifest import (
            AgentManifestSchema,
            manifest_to_process_config,
        )

        manifest = AgentManifestSchema(
            name="test",
            memory={
                "provider": "in_memory",
                "auto_store": True,
                "max": 10,
            },
        )
        config = manifest_to_process_config(manifest)
        assert config.context.memory_provider == "in_memory"
        assert config.context.memory_auto_store is True
        assert config.context.memory_max == 10


# ===========================================================================
# Integration-level tests (mocked agent)
# ===========================================================================


class TestOpenModeIntegration:
    """Integration tests with mocked agent."""

    @pytest.mark.asyncio
    async def test_dynamic_instructions_used_in_build(self):
        """Verify _build_agent uses dynamic instructions."""
        process = AgentProcess(
            name="test",
            config=ProcessConfig(
                execution_mode=ExecutionMode.OPEN,
                instructions="Original instructions.",
            ),
        )
        process._dynamic_instructions = "Modified instructions."

        with patch(
            "promptise.agent.build_agent",
            new_callable=AsyncMock,
        ) as mock_build:
            mock_build.return_value = MagicMock()
            await process._build_agent()

            # Verify the modified instructions were passed
            call_kwargs = mock_build.call_args[1]
            assert call_kwargs["instructions"] == "Modified instructions."

    @pytest.mark.asyncio
    async def test_dynamic_servers_merged_in_build(self):
        """Verify _build_agent merges static + dynamic servers.

        Since the runtime pre-discovers tools via MCPMultiClient
        and passes servers=None to build_agent, we verify that
        both static and dynamic servers are resolved to ServerSpec
        objects and that extra_tools includes the discovered MCP tools.
        """
        from promptise.config import HTTPServerSpec

        process = AgentProcess(
            name="test",
            config=ProcessConfig(
                execution_mode=ExecutionMode.OPEN,
                servers={"static": HTTPServerSpec(url="http://static")},
            ),
        )
        process._dynamic_servers = {
            "dynamic": {"url": "http://dynamic", "type": "http"},
        }

        mock_tool = MagicMock()

        with (
            patch(
                "promptise.agent.build_agent",
                new_callable=AsyncMock,
            ) as mock_build,
            patch(
                "promptise.mcp.client.MCPMultiClient",
            ) as mock_multi_cls,
            patch(
                "promptise.mcp.client.MCPToolAdapter",
            ) as mock_adapter_cls,
            patch(
                "promptise.mcp.client.MCPClient",
            ),
        ):
            mock_build.return_value = MagicMock()
            mock_multi_instance = MagicMock()
            mock_multi_instance.__aenter__ = AsyncMock(return_value=mock_multi_instance)
            mock_multi_instance.__aexit__ = AsyncMock(return_value=None)
            mock_multi_cls.return_value = mock_multi_instance

            mock_adapter_instance = MagicMock()
            mock_adapter_instance.as_langchain_tools = AsyncMock(return_value=[mock_tool])
            mock_adapter_cls.return_value = mock_adapter_instance

            await process._build_agent()

            # build_agent called with servers=None (tools pre-discovered)
            call_kwargs = mock_build.call_args[1]
            assert call_kwargs["servers"] is None

            # MCP tools were passed via extra_tools
            extra = call_kwargs.get("extra_tools", [])
            assert mock_tool in extra

    @pytest.mark.asyncio
    async def test_meta_tools_included_in_build(self):
        """Verify open mode includes meta-tools in build."""
        process = AgentProcess(
            name="test",
            config=ProcessConfig(execution_mode=ExecutionMode.OPEN),
        )

        with patch(
            "promptise.agent.build_agent",
            new_callable=AsyncMock,
        ) as mock_build:
            mock_build.return_value = MagicMock()
            await process._build_agent()

            call_kwargs = mock_build.call_args[1]
            extra_tools = call_kwargs.get("extra_tools")
            assert extra_tools is not None
            tool_names = {t.name for t in extra_tools}
            assert "modify_instructions" in tool_names
            assert "list_capabilities" in tool_names

    @pytest.mark.asyncio
    async def test_strict_mode_no_meta_tools(self):
        """Verify strict mode does NOT include meta-tools."""
        process = AgentProcess(
            name="test",
            config=ProcessConfig(execution_mode=ExecutionMode.STRICT),
        )

        with patch(
            "promptise.agent.build_agent",
            new_callable=AsyncMock,
        ) as mock_build:
            mock_build.return_value = MagicMock()
            await process._build_agent()

            call_kwargs = mock_build.call_args[1]
            extra_tools = call_kwargs.get("extra_tools")
            # Should be None (no tools to inject)
            assert extra_tools is None

    @pytest.mark.asyncio
    async def test_memory_wired_in_build(self):
        """Verify memory is passed to build_agent."""
        process = AgentProcess(
            name="test",
            config=ProcessConfig(
                context=ContextConfig(memory_provider="in_memory"),
            ),
        )

        with patch(
            "promptise.agent.build_agent",
            new_callable=AsyncMock,
        ) as mock_build:
            mock_build.return_value = MagicMock()
            await process._build_agent()

            call_kwargs = mock_build.call_args[1]
            assert call_kwargs["memory"] is not None


# ===========================================================================
# Extended add_trigger (webhook, file_watch, custom)
# ===========================================================================


class TestAddTriggerExtended:
    """Tests for add_trigger supporting all trigger types."""

    @pytest.mark.asyncio
    async def test_add_trigger_with_webhook_fields(self):
        """webhook_path and webhook_port are forwarded to TriggerConfig."""
        from promptise.runtime.meta_tools import _add_trigger

        process = AgentProcess(
            name="test",
            config=ProcessConfig(
                execution_mode=ExecutionMode.OPEN,
                open_mode=OpenModeConfig(max_dynamic_triggers=5),
            ),
        )
        # Mock the trigger listener to avoid real async loops
        process._trigger_listener = AsyncMock()

        # Patch create_trigger to capture the config
        with patch("promptise.runtime.triggers.create_trigger") as mock_create:
            mock_trigger = MagicMock()
            mock_trigger.trigger_id = "wh-1"
            mock_trigger.start = AsyncMock()
            mock_create.return_value = mock_trigger

            result = await _add_trigger(
                process,
                trigger_type="webhook",
                webhook_path="/my-hook",
                webhook_port=19090,
            )

            assert "added" in result.lower()
            # Verify the TriggerConfig was built with the right fields
            call_args = mock_create.call_args
            config_arg = call_args[0][0]
            assert config_arg.type == "webhook"
            assert config_arg.webhook_path == "/my-hook"
            assert config_arg.webhook_port == 19090

    @pytest.mark.asyncio
    async def test_add_trigger_with_file_watch_fields(self):
        """watch_path and watch_patterns are forwarded."""
        from promptise.runtime.meta_tools import _add_trigger

        process = AgentProcess(
            name="test",
            config=ProcessConfig(
                execution_mode=ExecutionMode.OPEN,
                open_mode=OpenModeConfig(max_dynamic_triggers=5),
            ),
        )
        process._trigger_listener = AsyncMock()

        with patch("promptise.runtime.triggers.create_trigger") as mock_create:
            mock_trigger = MagicMock()
            mock_trigger.trigger_id = "fw-1"
            mock_trigger.start = AsyncMock()
            mock_create.return_value = mock_trigger

            result = await _add_trigger(
                process,
                trigger_type="file_watch",
                watch_path="/data/inbox",
                watch_patterns=["*.csv", "*.json"],
            )

            assert "added" in result.lower()
            config_arg = mock_create.call_args[0][0]
            assert config_arg.type == "file_watch"
            assert config_arg.watch_path == "/data/inbox"
            assert config_arg.watch_patterns == ["*.csv", "*.json"]

    @pytest.mark.asyncio
    async def test_add_trigger_with_custom_config(self):
        """custom_config is forwarded for custom trigger types."""
        from promptise.runtime.meta_tools import _add_trigger

        process = AgentProcess(
            name="test",
            config=ProcessConfig(
                execution_mode=ExecutionMode.OPEN,
                open_mode=OpenModeConfig(max_dynamic_triggers=5),
            ),
        )
        process._trigger_listener = AsyncMock()

        with patch("promptise.runtime.triggers.create_trigger") as mock_create:
            mock_trigger = MagicMock()
            mock_trigger.trigger_id = "custom-1"
            mock_trigger.start = AsyncMock()
            mock_create.return_value = mock_trigger

            result = await _add_trigger(
                process,
                trigger_type="sqs",
                custom_config={"queue_url": "https://sqs.amazonaws.com/q"},
            )

            assert "added" in result.lower()
            config_arg = mock_create.call_args[0][0]
            assert config_arg.type == "sqs"
            assert config_arg.custom_config["queue_url"] == ("https://sqs.amazonaws.com/q")


# ===========================================================================
# Spawn process meta-tool
# ===========================================================================


class TestSpawnProcessMetaTool:
    """Tests for spawn_process and list_processes meta-tools."""

    @pytest.mark.asyncio
    async def test_spawn_requires_runtime(self):
        from promptise.runtime.meta_tools import _spawn_process

        process = AgentProcess(
            name="parent",
            config=ProcessConfig(
                execution_mode=ExecutionMode.OPEN,
                open_mode=OpenModeConfig(allow_process_spawn=True),
            ),
        )
        result = await _spawn_process(
            process,
            None,
            "child",
            "You are a child agent.",
        )
        assert "Error" in result
        assert "runtime" in result.lower()

    @pytest.mark.asyncio
    async def test_spawn_max_limit(self):
        from promptise.runtime.meta_tools import _spawn_process

        process = AgentProcess(
            name="parent",
            config=ProcessConfig(
                execution_mode=ExecutionMode.OPEN,
                open_mode=OpenModeConfig(
                    allow_process_spawn=True,
                    max_spawned_processes=0,
                ),
            ),
        )
        mock_runtime = MagicMock()
        mock_runtime.processes = {}

        result = await _spawn_process(
            process,
            mock_runtime,
            "child",
            "instructions",
        )
        assert "Error" in result
        assert "max" in result.lower()

    @pytest.mark.asyncio
    async def test_spawn_duplicate_name(self):
        from promptise.runtime.meta_tools import _spawn_process

        process = AgentProcess(
            name="parent",
            config=ProcessConfig(
                execution_mode=ExecutionMode.OPEN,
                open_mode=OpenModeConfig(
                    allow_process_spawn=True,
                    max_spawned_processes=3,
                ),
            ),
        )
        mock_runtime = MagicMock()
        mock_runtime.processes = {"child": MagicMock()}

        result = await _spawn_process(
            process,
            mock_runtime,
            "child",
            "instructions",
        )
        assert "Error" in result
        assert "already exists" in result

    @pytest.mark.asyncio
    async def test_spawn_success(self):
        from promptise.runtime.meta_tools import _spawn_process

        mock_child = MagicMock()
        mock_child.start = AsyncMock()

        mock_runtime = AsyncMock()
        mock_runtime.add_process = AsyncMock(return_value=mock_child)
        mock_runtime.processes = {}

        process = AgentProcess(
            name="parent",
            config=ProcessConfig(
                execution_mode=ExecutionMode.OPEN,
                open_mode=OpenModeConfig(
                    allow_process_spawn=True,
                    max_spawned_processes=3,
                ),
            ),
        )

        result = await _spawn_process(
            process,
            mock_runtime,
            "child-1",
            "You are a helper.",
        )
        assert "spawned" in result.lower()
        assert "child-1" in process._spawned_processes
        mock_runtime.add_process.assert_awaited_once()
        mock_child.start.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_spawn_with_triggers(self):
        from promptise.runtime.meta_tools import _spawn_process

        mock_child = MagicMock()
        mock_child.start = AsyncMock()

        mock_runtime = AsyncMock()
        mock_runtime.add_process = AsyncMock(return_value=mock_child)
        mock_runtime.processes = {}

        process = AgentProcess(
            name="parent",
            config=ProcessConfig(
                execution_mode=ExecutionMode.OPEN,
                open_mode=OpenModeConfig(
                    allow_process_spawn=True,
                    max_spawned_processes=3,
                ),
            ),
        )

        result = await _spawn_process(
            process,
            mock_runtime,
            "child-cron",
            "You run on a schedule.",
            triggers=[{"type": "cron", "cron_expression": "*/5 * * * *"}],
        )
        assert "spawned" in result.lower()
        assert "Triggers: 1" in result

    def test_spawn_permission_default_false(self):
        from promptise.runtime.meta_tools import create_meta_tools

        process = AgentProcess(
            name="test",
            config=ProcessConfig(execution_mode=ExecutionMode.OPEN),
        )
        tools = create_meta_tools(process)
        tool_names = {t.name for t in tools}
        assert "spawn_process" not in tool_names
        assert "list_processes" not in tool_names

    def test_spawn_permission_enabled(self):
        from promptise.runtime.meta_tools import create_meta_tools

        process = AgentProcess(
            name="test",
            config=ProcessConfig(
                execution_mode=ExecutionMode.OPEN,
                open_mode=OpenModeConfig(allow_process_spawn=True),
            ),
        )
        mock_runtime = MagicMock()
        tools = create_meta_tools(process, runtime=mock_runtime)
        tool_names = {t.name for t in tools}
        assert "spawn_process" in tool_names
        assert "list_processes" in tool_names

    @pytest.mark.asyncio
    async def test_list_processes(self):
        from promptise.runtime.meta_tools import _list_processes

        mock_proc1 = MagicMock()
        mock_proc1.state.value = "running"
        mock_proc1._invocation_count = 5

        mock_proc2 = MagicMock()
        mock_proc2.state.value = "stopped"
        mock_proc2._invocation_count = 0

        mock_runtime = MagicMock()
        mock_runtime.processes = {"parent": mock_proc1, "child": mock_proc2}

        process = AgentProcess(
            name="parent",
            config=ProcessConfig(
                execution_mode=ExecutionMode.OPEN,
                open_mode=OpenModeConfig(allow_process_spawn=True),
            ),
        )

        result = await _list_processes(process, mock_runtime)
        assert "parent" in result
        assert "child" in result
        assert "running" in result
        assert "(this process)" in result

    @pytest.mark.asyncio
    async def test_list_processes_requires_runtime(self):
        from promptise.runtime.meta_tools import _list_processes

        process = AgentProcess(
            name="test",
            config=ProcessConfig(execution_mode=ExecutionMode.OPEN),
        )
        result = await _list_processes(process, None)
        assert "Error" in result

    def test_open_mode_config_spawn_defaults(self):
        cfg = OpenModeConfig()
        assert cfg.allow_process_spawn is False
        assert cfg.max_spawned_processes == 3

    def test_status_includes_spawned_count(self):
        process = AgentProcess(
            name="test",
            config=ProcessConfig(execution_mode=ExecutionMode.OPEN),
        )
        process._spawned_processes = ["child-1", "child-2"]
        status = process.status()
        assert status["spawned_process_count"] == 2


# ===========================================================================
# 1.3.3: create_tool honours `parameters`; max_rebuilds never stages a change
# ===========================================================================


def _stub_process(**open_mode) -> AgentProcess:
    """An open-mode process whose agent build records what it was built with."""
    process = AgentProcess(
        "p",
        ProcessConfig(
            instructions="ORIGINAL",
            execution_mode=ExecutionMode.OPEN,
            open_mode=OpenModeConfig(**open_mode),
        ),
    )
    builds: list[dict] = []

    async def build() -> None:
        snapshot = {
            "instructions": process._dynamic_instructions or process.config.instructions,
            "tools": [t.name for t in process._custom_tools],
            "servers": sorted(process._dynamic_servers),
        }
        builds.append(snapshot)
        agent = MagicMock()
        agent.shutdown = AsyncMock()
        agent.built_with = snapshot
        process._agent = agent

    process._build_agent = build  # type: ignore[method-assign]
    process.builds = builds  # type: ignore[attr-defined]
    return process


class TestCreateToolParameters:
    """create_tool used to ignore `parameters`: every tool took one `input` string."""

    @pytest.mark.asyncio
    async def test_declared_parameters_become_the_tool_arguments(self):
        from promptise.runtime.meta_tools import create_meta_tools

        process = _stub_process()
        await process.start()
        try:
            create_tool = {t.name: t for t in create_meta_tools(process)}["create_tool"]
            reply = await create_tool.ainvoke(
                {
                    "tool_name": "add",
                    "tool_description": "add a and b",
                    "parameters": {
                        "type": "object",
                        "properties": {"a": {"type": "integer"}, "b": {"type": "integer"}},
                        "required": ["a", "b"],
                    },
                    "python_code": "def run(a, b):\n    return str(a + b)",
                }
            )
            assert "created" in reply
            tool = next(t for t in process._custom_tools if t.name == "add")
            assert list(tool.args) == ["a", "b"]
            assert await tool.ainvoke({"a": 2, "b": 3}) == "5"
            # The schema the model is offered names the parameters.
            schema = tool.tool_call_schema.model_json_schema()
            assert set(schema["properties"]) == {"a", "b"}
            assert schema["required"] == ["a", "b"]
        finally:
            await process.stop()

    @pytest.mark.asyncio
    async def test_arguments_are_validated_and_defaults_applied(self):
        from promptise.runtime.meta_tools import _build_dynamic_tool
        from promptise.tools import ToolArgumentError

        tool = _build_dynamic_tool(
            "scale",
            "scale items",
            "def run(items, factor, label='x'):\n"
            "    return label + ':' + str([i['n'] * factor for i in items])",
            parameters={
                "type": "object",
                "properties": {
                    "items": {
                        "type": "array",
                        "items": {"type": "object", "properties": {"n": {"type": "integer"}}},
                    },
                    "factor": {"type": "integer", "default": 2},
                    "label": {"type": "string"},
                },
                "required": ["items"],
            },
        )
        # Nested objects arrive as dicts; the schema default fills `factor`;
        # `label` (no schema default) is left to run()'s own default.
        assert await tool.ainvoke({"items": [{"n": 1}, {"n": 3}]}) == "x:[2, 6]"
        with pytest.raises(ToolArgumentError, match="factor"):
            await tool.ainvoke({"items": [], "factor": "lots"})

    @pytest.mark.asyncio
    async def test_no_parameters_keeps_the_free_form_input(self):
        from promptise.runtime.meta_tools import _build_dynamic_tool

        for parameters in (None, {}, {"type": "object"}, {"type": "object", "properties": {}}):
            tool = _build_dynamic_tool(
                "echo", "echo", "def run(input=''):\n    return input", parameters=parameters
            )
            assert list(tool.args) == ["input"]
            assert await tool.ainvoke({"input": "hi"}) == "hi"

    @pytest.mark.asyncio
    @pytest.mark.parametrize(
        "parameters",
        [
            {"a": {"type": "integer"}},  # properties without the object wrapper
            {"type": "string"},
            {"type": "object", "properties": {"class": {"type": "string"}}},
            {"type": "object", "properties": {"a": {}}, "required": ["b"]},
        ],
    )
    async def test_unusable_parameters_are_refused_and_nothing_is_staged(self, parameters):
        from promptise.runtime.meta_tools import _create_tool

        process = _stub_process()
        reply = await _create_tool(
            process,
            tool_name="t",
            tool_description="d",
            parameters=parameters,
            python_code="def run(**kw):\n    return 'x'",
        )
        assert reply.startswith("Error creating tool")
        assert process._custom_tools == []
        assert process.builds == []


class TestMaxRebuildsNeverStages:
    """A change refused by max_rebuilds used to be staged and applied on restart."""

    @pytest.mark.asyncio
    async def test_refused_instructions_do_not_apply_after_a_restart(self):
        from promptise.runtime.meta_tools import _modify_instructions

        process = _stub_process(max_rebuilds=1)
        await process.start()
        assert await _modify_instructions(process, "REWRITE_1") == "Rebuild successful"
        refused = await _modify_instructions(process, "REWRITE_2")
        assert refused.startswith("Error: max rebuilds (1) reached")
        assert "not applied" in refused
        assert process._dynamic_instructions == "REWRITE_1"

        await process.stop()
        await process.start()  # deploy, crash recovery, restart_policy
        try:
            assert process._agent.built_with["instructions"] == "REWRITE_1"
            # The limit is per lifetime: the restart did not reset it.
            assert process._rebuild_count == 1
            again = await _modify_instructions(process, "REWRITE_3")
            assert again.startswith("Error: max rebuilds")
            assert process._dynamic_instructions == "REWRITE_1"
        finally:
            await process.stop()

    @pytest.mark.asyncio
    async def test_refused_tool_and_server_are_not_staged(self):
        from promptise.runtime.meta_tools import _connect_mcp_server, _create_tool

        process = _stub_process(max_rebuilds=0)
        await process.start()
        try:
            tool_reply = await _create_tool(
                process,
                tool_name="greet",
                tool_description="Say hello",
                parameters={},
                python_code="def run(**kwargs): return 'hello'",
            )
            server_reply = await _connect_mcp_server(
                process, server_name="extra", url="http://127.0.0.1:1/mcp"
            )
        finally:
            await process.stop()

        assert tool_reply.startswith("Error: max rebuilds (0) reached")
        assert "created" not in tool_reply
        assert server_reply.startswith("Error: max rebuilds (0) reached")
        assert "Connected" not in server_reply
        assert process._custom_tools == []
        assert process._dynamic_servers == {}

        await process.start()
        try:
            assert process._agent.built_with == {
                "instructions": "ORIGINAL",
                "tools": [],
                "servers": [],
            }
        finally:
            await process.stop()

    @pytest.mark.asyncio
    async def test_a_failed_rebuild_undoes_the_change(self):
        from promptise.runtime.meta_tools import _create_tool, _modify_instructions

        process = _stub_process()
        await process.start()
        previous_agent = process._agent

        async def failing_build() -> None:
            raise RuntimeError("server unreachable")

        process._build_agent = failing_build  # type: ignore[method-assign]
        try:
            with pytest.raises(RuntimeError, match="unreachable"):
                await _modify_instructions(process, "NEW")
            reply = await _create_tool(
                process,
                tool_name="greet",
                tool_description="Say hello",
                parameters={},
                python_code="def run(**kwargs): return 'hello'",
            )
        finally:
            process._agent = previous_agent
            await process.stop()

        assert reply.startswith("Error creating tool: server unreachable")
        assert process._dynamic_instructions is None
        assert process._custom_tools == []
        assert process._rebuild_count == 0

    @pytest.mark.asyncio
    async def test_rollback_is_not_refused_by_the_limit(self):
        from promptise.runtime.meta_tools import _modify_instructions

        process = _stub_process(max_rebuilds=1)
        await process.start()
        try:
            await _modify_instructions(process, "REWRITE_1")
            assert await process.rollback() == "Rebuild successful"
            assert process._agent.built_with["instructions"] == "ORIGINAL"
            assert process._dynamic_instructions is None
            assert process._rebuild_count == 2
        finally:
            await process.stop()
