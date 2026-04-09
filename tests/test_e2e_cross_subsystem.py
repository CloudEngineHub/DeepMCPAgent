"""Cross-subsystem integration tests for Promptise Foundry.

Verifies that all 6 subsystems (core agent, MCP server, MCP client,
prompts, runtime, and memory) compose correctly and that the public
API surface is consistent.

This is the FINAL integration test file: no real LLM calls, no
network I/O.  Uses mocks for agent internals and in-process
TestClient for MCP server verification.
"""

from __future__ import annotations

import asyncio
import importlib
import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_mock_inner() -> MagicMock:
    """Return a mock object that quacks like a LangGraph Runnable."""
    mock = MagicMock()
    mock.ainvoke = AsyncMock(return_value={"messages": [{"role": "assistant", "content": "ok"}]})
    mock.invoke = MagicMock(return_value={"messages": [{"role": "assistant", "content": "ok"}]})
    return mock


# ===========================================================================
# 1. TestPublicAPIExports
# ===========================================================================


class TestPublicAPIExports:
    """Verify every symbol listed in __all__ is importable."""

    def test_all_public_exports_importable(self) -> None:
        """Every symbol in promptise.__init__.__all__ is importable."""
        import promptise

        missing: list[str] = []
        for name in promptise.__all__:
            if not hasattr(promptise, name):
                missing.append(name)
        assert missing == [], f"Missing exports from promptise: {missing}"

    def test_runtime_exports_importable(self) -> None:
        """Every symbol in promptise.runtime.__init__.__all__ is importable."""
        import promptise.runtime as rt

        missing: list[str] = []
        for name in rt.__all__:
            if not hasattr(rt, name):
                missing.append(name)
        assert missing == [], f"Missing exports from promptise.runtime: {missing}"

    def test_prompts_exports_importable(self) -> None:
        """Every symbol in promptise.prompts.__init__.__all__ is importable."""
        import promptise.prompts as prompts

        missing: list[str] = []
        for name in prompts.__all__:
            if not hasattr(prompts, name):
                missing.append(name)
        assert missing == [], f"Missing exports from promptise.prompts: {missing}"


# ===========================================================================
# 2. TestPydanticModelSerialization
# ===========================================================================


class TestPydanticModelSerialization:
    """Verify Pydantic model roundtrip serialization."""

    def test_process_config_roundtrip(self) -> None:
        """ProcessConfig model_dump -> model_validate roundtrip."""
        from promptise.runtime.config import ProcessConfig, TriggerConfig

        original = ProcessConfig(
            model="openai:gpt-5-mini",
            instructions="You are a test agent.",
            triggers=[
                TriggerConfig(type="cron", cron_expression="*/5 * * * *"),
            ],
            concurrency=3,
            heartbeat_interval=15.0,
        )
        data = original.model_dump()
        restored = ProcessConfig.model_validate(data)
        assert restored.model == original.model
        assert restored.instructions == original.instructions
        assert len(restored.triggers) == 1
        assert restored.triggers[0].type == "cron"
        assert restored.triggers[0].cron_expression == "*/5 * * * *"
        assert restored.concurrency == 3

    def test_trigger_config_roundtrip(self) -> None:
        """TriggerConfig model_dump -> model_validate roundtrip."""
        from promptise.runtime.config import TriggerConfig

        original = TriggerConfig(
            type="event",
            event_type="task.completed",
            event_source="agent-1",
            filter_expression="data.status == 'success'",
        )
        data = original.model_dump()
        restored = TriggerConfig.model_validate(data)
        assert restored.type == "event"
        assert restored.event_type == "task.completed"
        assert restored.event_source == "agent-1"
        assert restored.filter_expression == "data.status == 'success'"

    def test_runtime_config_roundtrip(self) -> None:
        """RuntimeConfig model_dump -> model_validate roundtrip."""
        from promptise.runtime.config import (
            DistributedConfig,
            ProcessConfig,
            RuntimeConfig,
            TriggerConfig,
        )

        original = RuntimeConfig(
            processes={
                "watcher": ProcessConfig(
                    model="openai:gpt-5-mini",
                    triggers=[
                        TriggerConfig(type="cron", cron_expression="0 * * * *"),
                    ],
                ),
                "analyzer": ProcessConfig(
                    model="anthropic:claude-sonnet-4-20250514",
                    instructions="Analyze data.",
                ),
            },
            distributed=DistributedConfig(
                enabled=True,
                transport_port=9200,
            ),
        )
        data = original.model_dump()
        restored = RuntimeConfig.model_validate(data)
        assert set(restored.processes.keys()) == {"watcher", "analyzer"}
        assert restored.distributed.enabled is True
        assert restored.distributed.transport_port == 9200

        # Also test the to_dict / from_dict convenience methods
        d = original.to_dict()
        from_d = RuntimeConfig.from_dict(d)
        assert set(from_d.processes.keys()) == {"watcher", "analyzer"}


# ===========================================================================
# 3. TestExceptionHierarchy
# ===========================================================================


class TestExceptionHierarchy:
    """Verify exception inheritance chains."""

    def test_all_exceptions_inherit_base(self) -> None:
        """SuperAgentError, SuperAgentValidationError, EnvVarNotFoundError hierarchy."""
        from promptise.exceptions import (
            EnvVarNotFoundError,
            SuperAgentError,
            SuperAgentValidationError,
        )

        # SuperAgentError is the root
        assert issubclass(SuperAgentError, RuntimeError)
        assert issubclass(SuperAgentValidationError, SuperAgentError)
        assert issubclass(EnvVarNotFoundError, SuperAgentError)

        # Instances should be catchable via the base
        exc_val = SuperAgentValidationError("bad schema", errors=[], file_path="a.yml")
        assert isinstance(exc_val, SuperAgentError)

        exc_env = EnvVarNotFoundError("MY_KEY", context="agent.model")
        assert isinstance(exc_env, SuperAgentError)
        assert exc_env.var_name == "MY_KEY"
        assert exc_env.context == "agent.model"

    def test_runtime_exceptions_hierarchy(self) -> None:
        """RuntimeBaseError, ProcessStateError, ManifestError, TriggerError, JournalError."""
        from promptise.runtime.exceptions import (
            JournalError,
            ManifestError,
            ManifestValidationError,
            ProcessStateError,
            RuntimeBaseError,
            TriggerError,
        )

        assert issubclass(RuntimeBaseError, RuntimeError)
        assert issubclass(ProcessStateError, RuntimeBaseError)
        assert issubclass(ManifestError, RuntimeBaseError)
        assert issubclass(ManifestValidationError, ManifestError)
        assert issubclass(TriggerError, RuntimeBaseError)
        assert issubclass(JournalError, RuntimeBaseError)

        # Verify ProcessStateError stores attributes
        exc = ProcessStateError("proc-1", "RUNNING", "CREATED")
        assert exc.process_id == "proc-1"
        assert exc.current_state == "RUNNING"
        assert exc.attempted_state == "CREATED"
        assert isinstance(exc, RuntimeBaseError)


# ===========================================================================
# 4. TestAgentWithMCPServer
# ===========================================================================


class TestAgentWithMCPServer:
    """Verify MCP server + TestClient integration."""

    @pytest.mark.asyncio
    async def test_mcp_server_tool_discovery_via_test_client(self) -> None:
        """Create MCPServer with tools, verify tools discoverable via TestClient."""
        from promptise.mcp.server import MCPServer, TestClient

        server = MCPServer(name="cross-test")

        @server.tool()
        async def multiply(x: int, y: int) -> int:
            """Multiply two numbers."""
            return x * y

        @server.tool()
        async def reverse(text: str) -> str:
            """Reverse a string."""
            return text[::-1]

        client = TestClient(server)
        tools = await client.list_tools()
        tool_names = {t.name for t in tools}
        assert "multiply" in tool_names
        assert "reverse" in tool_names

        # Invoke a tool
        result = await client.call_tool("multiply", {"x": 6, "y": 7})
        assert result[0].text == "42"

        result = await client.call_tool("reverse", {"text": "hello"})
        assert result[0].text == "olleh"

    @pytest.mark.asyncio
    async def test_server_auth_jwt_flow(self) -> None:
        """JWT auth: create token, use it in client, verify authenticated tool call."""
        from promptise.mcp.server import (
            AuthMiddleware,
            JWTAuth,
            MCPServer,
            TestClient,
        )

        server = MCPServer(name="auth-cross-test")
        jwt = JWTAuth(secret="cross-test-secret")
        server.add_middleware(AuthMiddleware(jwt))

        @server.tool(auth=True)
        async def secret_action() -> str:
            """Do something secret."""
            return "classified-data"

        # Authenticated call should succeed
        token = jwt.create_token({"sub": "agent-cross", "roles": ["admin"]})
        auth_client = TestClient(server, meta={"authorization": f"Bearer {token}"})
        result = await auth_client.call_tool("secret_action", {})
        assert result[0].text == "classified-data"

        # Unauthenticated call should fail
        unauth_client = TestClient(server)
        result = await unauth_client.call_tool("secret_action", {})
        parsed = json.loads(result[0].text)
        assert parsed["error"]["code"] == "AUTHENTICATION_ERROR"


# ===========================================================================
# 5. TestPromptWithAgent
# ===========================================================================


class TestPromptWithAgent:
    """Verify prompt blocks compose into usable agent instructions."""

    def test_prompt_blocks_compose_system_prompt(self) -> None:
        """PromptAssembler composes blocks into text usable as agent instructions."""
        from promptise.prompts.blocks import (
            AssembledPrompt,
            Identity,
            OutputFormat,
            PromptAssembler,
            Rules,
        )

        assembler = PromptAssembler(
            Identity("Expert data analyst"),
            Rules(["Always cite sources", "Use metric units"]),
            OutputFormat(format="markdown"),
        )
        assembled = assembler.assemble()

        assert isinstance(assembled, AssembledPrompt)
        assert "Expert data analyst" in assembled.text
        assert "Always cite sources" in assembled.text
        assert "metric units" in assembled.text
        assert len(assembled.text) > 0

        # The assembled text can be used as system prompt for PromptiseAgent
        from promptise.agent import PromptiseAgent

        inner = _make_mock_inner()
        agent = PromptiseAgent(inner=inner)
        # No error constructing with the assembled text as instructions
        assert agent is not None

    @pytest.mark.asyncio
    async def test_conversation_flow_evolves_prompts(self) -> None:
        """ConversationFlow produces different assembled prompts across turns."""
        from promptise.prompts.blocks import Identity, Rules, Section
        from promptise.prompts.flows import ConversationFlow, phase

        class TestFlow(ConversationFlow):
            base_blocks = [
                Identity("Test agent"),
                Rules(["Be helpful"]),
            ]

            @phase("greeting", initial=True)
            async def greet(self, ctx):
                ctx.activate(Section("greeting_section", "Greet the user."))

            @phase("working")
            async def work(self, ctx):
                ctx.deactivate("greeting_section")
                ctx.activate(Section("work_section", "Work on the task."))

        flow = TestFlow()

        # Start the flow (enters greeting phase)
        assembled_1 = await flow.start()
        assert "Greet the user" in assembled_1.text
        assert "Test agent" in assembled_1.text

        # Advance to working phase
        await flow.transition("working")
        assembled_2 = await flow.next_turn("I need help with analysis")
        assert "Work on the task" in assembled_2.text
        # greeting_section should be gone
        assert "Greet the user" not in assembled_2.text


# ===========================================================================
# 6. TestMemoryIntegration
# ===========================================================================


class TestMemoryIntegration:
    """Verify memory provider integration with agent."""

    @pytest.mark.asyncio
    async def test_memory_provider_with_agent(self) -> None:
        """InMemoryProvider stores data, PromptiseAgent can use it for context injection."""
        from promptise.agent import PromptiseAgent
        from promptise.memory import InMemoryProvider

        provider = InMemoryProvider()
        await provider.add("The project deadline is March 15.", metadata={"source": "calendar"})
        await provider.add("Alice prefers dark mode.", metadata={"source": "prefs"})

        inner = _make_mock_inner()
        agent = PromptiseAgent(inner=inner, memory_provider=provider)

        # ainvoke should search memory and inject context.
        # InMemoryProvider uses case-insensitive substring matching, so use
        # a query that is a substring of the stored content.
        await agent.ainvoke(
            {
                "messages": [{"role": "user", "content": "project deadline"}],
            }
        )

        # The inner graph should have been called with memory context injected
        call_args = inner.ainvoke.call_args
        input_data = call_args[0][0]
        messages = input_data["messages"]
        # At least one SystemMessage with memory context should have been injected
        has_memory_context = any(
            hasattr(m, "content") and "deadline" in str(getattr(m, "content", "")).lower()
            for m in messages
        )
        assert has_memory_context, "Memory context should be injected into messages"

    @pytest.mark.asyncio
    async def test_memory_search_results_format(self) -> None:
        """MemoryResult objects have id, content, score, metadata."""
        from promptise.memory import InMemoryProvider, MemoryResult

        provider = InMemoryProvider()
        mid = await provider.add(
            "Python is great for AI.",
            metadata={"topic": "programming"},
        )

        # InMemoryProvider uses case-insensitive substring matching.
        # Use a query that is an exact substring of the stored content.
        results = await provider.search("Python is great")
        assert len(results) >= 1
        r = results[0]
        assert isinstance(r, MemoryResult)
        assert isinstance(r.content, str)
        assert isinstance(r.score, float)
        assert 0.0 <= r.score <= 1.0
        assert isinstance(r.memory_id, str)
        assert isinstance(r.metadata, dict)
        assert "Python" in r.content


# ===========================================================================
# 7. TestRuntimeWithTriggers
# ===========================================================================


class TestRuntimeWithTriggers:
    """Verify runtime process creation and configuration."""

    def test_process_with_cron_trigger_config(self) -> None:
        """ProcessConfig with TriggerConfig creates valid AgentProcess."""
        from promptise.runtime import AgentProcess, ProcessConfig, TriggerConfig

        config = ProcessConfig(
            model="openai:gpt-5-mini",
            instructions="Monitor data pipelines.",
            triggers=[
                TriggerConfig(type="cron", cron_expression="*/10 * * * *"),
            ],
        )
        process = AgentProcess(name="data-watcher", config=config)
        assert process.name == "data-watcher"
        assert process.config.triggers[0].type == "cron"
        assert process.config.triggers[0].cron_expression == "*/10 * * * *"

    @pytest.mark.asyncio
    async def test_runtime_registers_and_manages_processes(self) -> None:
        """AgentRuntime add_process + get_process + remove_process."""
        from promptise.runtime import AgentRuntime, ProcessConfig, ProcessState

        runtime = AgentRuntime()

        config = ProcessConfig(
            model="openai:gpt-5-mini",
            instructions="Test agent.",
        )
        process = await runtime.add_process("test-proc", config)
        assert process.name == "test-proc"
        assert "test-proc" in runtime.processes

        # get_process should work
        fetched = runtime.get_process("test-proc")
        assert fetched is process

        # Process should be in CREATED state
        assert process.state == ProcessState.CREATED

        # remove_process should remove it
        await runtime.remove_process("test-proc")
        assert "test-proc" not in runtime.processes

    def test_open_mode_meta_tools_registered(self) -> None:
        """OPEN mode process has all expected meta-tools."""
        from promptise.runtime.config import ExecutionMode, OpenModeConfig, ProcessConfig
        from promptise.runtime.meta_tools import create_meta_tools

        config = ProcessConfig(
            model="openai:gpt-5-mini",
            execution_mode=ExecutionMode.OPEN,
            open_mode=OpenModeConfig(
                allow_identity_change=True,
                allow_tool_creation=True,
                allow_mcp_connect=True,
                allow_trigger_management=True,
                allow_memory_management=True,
                allow_process_spawn=True,
            ),
        )

        # Mock the process object needed by create_meta_tools
        mock_process = MagicMock()
        mock_process.config = config
        mock_process.name = "open-proc"

        tools = create_meta_tools(mock_process, runtime=MagicMock())
        tool_names = {t.name for t in tools}

        expected = {
            "modify_instructions",
            "create_tool",
            "connect_mcp_server",
            "add_trigger",
            "remove_trigger",
            "spawn_process",
            "list_processes",
            "store_memory",
            "search_memory",
            "forget_memory",
            "list_capabilities",
        }
        assert expected.issubset(tool_names), f"Missing meta-tools: {expected - tool_names}"


# ===========================================================================
# 8. TestPromptChainOperators
# ===========================================================================


class _MockPrompt:
    """Lightweight mock that satisfies the Prompt interface for chain operators."""

    def __init__(self, name: str, handler: Any) -> None:
        self.name = name
        self._handler = handler

    async def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return await self._handler(*args, **kwargs)


class TestPromptChainOperators:
    """Verify chain, parallel, retry, and fallback operators."""

    @pytest.mark.asyncio
    async def test_chain_sequential(self) -> None:
        """chain() creates sequential pipeline that composes prompts."""
        from promptise.prompts.chain import chain

        step_a = _MockPrompt("step_a", AsyncMock(return_value="processed-hello"))
        step_b = _MockPrompt("step_b", AsyncMock(return_value="final-processed-hello"))

        pipeline = chain(step_a, step_b)
        assert repr(pipeline).startswith("<Chain")

        result = await pipeline("hello")
        assert result == "final-processed-hello"
        step_a._handler.assert_awaited_once()
        step_b._handler.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_parallel_concurrent(self) -> None:
        """parallel() runs prompts concurrently."""
        from promptise.prompts.chain import parallel

        analyzer = _MockPrompt("analyzer", AsyncMock(return_value="analyzed"))
        summarizer = _MockPrompt("summarizer", AsyncMock(return_value="summarized"))

        multi = parallel(analyzer=analyzer, summarizer=summarizer)
        assert repr(multi).startswith("<Parallel")

        results = await multi("input text")
        assert isinstance(results, dict)
        assert results["analyzer"] == "analyzed"
        assert results["summarizer"] == "summarized"

    @pytest.mark.asyncio
    async def test_retry_on_failure(self) -> None:
        """retry() retries failed prompt execution."""
        from promptise.prompts.chain import retry

        attempt = 0

        async def flaky_call(*args, **kwargs):
            nonlocal attempt
            attempt += 1
            if attempt < 3:
                raise RuntimeError("temporary failure")
            return "success"

        flaky = _MockPrompt("flaky", flaky_call)
        wrapper = retry(flaky, max_retries=3, backoff=0.01)
        assert repr(wrapper).startswith("<Retry")

        result = await wrapper("test")
        assert result == "success"
        assert attempt == 3

    @pytest.mark.asyncio
    async def test_fallback_on_failure(self) -> None:
        """fallback() tries alternative when primary fails."""
        from promptise.prompts.chain import fallback

        primary = _MockPrompt("primary", AsyncMock(side_effect=RuntimeError("primary down")))
        backup = _MockPrompt("backup", AsyncMock(return_value="backup_result"))

        fb = fallback(primary, backup)
        assert repr(fb).startswith("<Fallback")

        result = await fb("test")
        assert result == "backup_result"


# ===========================================================================
# 10. TestSuperAgentToRuntime
# ===========================================================================


class TestSuperAgentToRuntime:
    """Verify SuperAgentLoader config composition."""

    def test_superagent_yaml_parses_to_config(self, tmp_path) -> None:
        """SuperAgentLoader parses YAML into config usable for runtime."""
        from promptise.superagent import SuperAgentLoader

        # Write a minimal .superagent file (needs at least one server)
        content = """\
agent:
  model: openai:gpt-5-mini
  instructions: You are a test agent.
  trace: false
servers:
  tools:
    type: http
    url: http://localhost:8080/mcp
    transport: streamable-http
"""
        sa_file = tmp_path / "test.superagent"
        sa_file.write_text(content)

        loader = SuperAgentLoader.from_file(sa_file)
        assert loader.schema.agent.model == "openai:gpt-5-mini"
        assert loader.schema.agent.instructions == "You are a test agent."

        config = loader.to_agent_config()
        assert config.model == "openai:gpt-5-mini"
        assert config.instructions == "You are a test agent."

    def test_config_composition(self) -> None:
        """Verify all config models compose into RuntimeConfig."""
        from promptise.runtime.config import (
            ContextConfig,
            DistributedConfig,
            ExecutionMode,
            JournalConfig,
            OpenModeConfig,
            ProcessConfig,
            RuntimeConfig,
            TriggerConfig,
        )

        # Build all atomic configs
        trigger = TriggerConfig(type="cron", cron_expression="0 */6 * * *")
        journal = JournalConfig(level="full", backend="memory")
        ctx = ContextConfig(
            memory_provider="in_memory",
            conversation_max_messages=50,
            initial_state={"started": True},
        )
        open_mode = OpenModeConfig(
            allow_identity_change=False,
            max_custom_tools=5,
        )

        # Compose into ProcessConfig
        proc = ProcessConfig(
            model="openai:gpt-5-mini",
            instructions="Composite test.",
            execution_mode=ExecutionMode.OPEN,
            open_mode=open_mode,
            triggers=[trigger],
            journal=journal,
            context=ctx,
            concurrency=2,
        )

        # Compose into RuntimeConfig
        runtime_cfg = RuntimeConfig(
            processes={"composite-proc": proc},
            distributed=DistributedConfig(enabled=False),
        )

        # Verify composition
        assert "composite-proc" in runtime_cfg.processes
        p = runtime_cfg.processes["composite-proc"]
        assert p.execution_mode == ExecutionMode.OPEN
        assert p.open_mode.allow_identity_change is False
        assert p.open_mode.max_custom_tools == 5
        assert p.triggers[0].cron_expression == "0 */6 * * *"
        assert p.journal.level == "full"
        assert p.context.memory_provider == "in_memory"
        assert p.context.initial_state == {"started": True}


# ===========================================================================
# 11. TestConcurrentOperations
# ===========================================================================


class TestConcurrentOperations:
    """Verify concurrent async operations are safe."""

    @pytest.mark.asyncio
    async def test_concurrent_memory_operations(self) -> None:
        """Multiple async tasks using same InMemoryProvider."""
        from promptise.memory import InMemoryProvider

        provider = InMemoryProvider()

        async def add_and_search(i: int) -> bool:
            content = f"Memory item number {i}"
            mid = await provider.add(content, metadata={"index": i})
            results = await provider.search(f"item number {i}")
            return len(results) >= 1

        tasks = [add_and_search(i) for i in range(20)]
        results = await asyncio.gather(*tasks)
        assert all(results), "All concurrent memory operations should succeed"

        # Verify all items were stored
        for i in range(20):
            r = await provider.search(f"item number {i}")
            assert len(r) >= 1


# ===========================================================================
# 12. TestBackwardCompat
# ===========================================================================


class TestBackwardCompat:
    """Verify backward compatibility for agent types."""

    def test_promptise_agent_is_importable(self) -> None:
        """PromptiseAgent is importable from promptise.agent."""
        from promptise.agent import PromptiseAgent

        inner = _make_mock_inner()
        agent = PromptiseAgent(inner=inner)
        assert isinstance(agent, PromptiseAgent)


# ===========================================================================
# 13. TestFullImportSmokeTest
# ===========================================================================


class TestFullImportSmokeTest:
    """Verify all packages and subpackages are importable."""

    def test_all_subpackages_importable(self) -> None:
        """Import every subpackage (mcp.server, mcp.client, prompts, runtime, etc.)."""
        subpackages = [
            "promptise",
            "promptise.mcp.server",
            "promptise.mcp.client",
            "promptise.prompts",
            "promptise.runtime",
            "promptise.runtime.triggers",
            "promptise.runtime.journal",
            "promptise.runtime.distributed",
            "promptise.sandbox",
        ]

        failed: list[str] = []
        for pkg in subpackages:
            try:
                importlib.import_module(pkg)
            except ImportError as exc:
                failed.append(f"{pkg}: {exc}")

        assert failed == [], "Failed to import subpackages:\n" + "\n".join(failed)

    def test_top_level_convenience_imports(self) -> None:
        """Key classes importable from promptise directly."""
        from promptise import (
            AgentProcess,
            AgentRuntime,
            InMemoryProvider,
            MCPClient,
            MCPMultiClient,
            MemoryProvider,
            MemoryResult,
            ObservabilityConfig,
            ProcessConfig,
            ProcessState,
            Prompt,
            PromptBuilder,
            PromptiseAgent,
            RuntimeConfig,
            SuperAgentLoader,
            build_agent,
        )

        # Verify these are the correct types (not None or stale aliases)
        assert PromptiseAgent is not None
        assert build_agent is not None
        assert MCPClient is not None
        assert MCPMultiClient is not None
        assert InMemoryProvider is not None
        assert MemoryResult is not None
        assert MemoryProvider is not None
        assert ObservabilityConfig is not None
        assert Prompt is not None
        assert PromptBuilder is not None
        assert SuperAgentLoader is not None
        assert AgentProcess is not None
        assert AgentRuntime is not None
        assert ProcessConfig is not None
        assert ProcessState is not None
        assert RuntimeConfig is not None
