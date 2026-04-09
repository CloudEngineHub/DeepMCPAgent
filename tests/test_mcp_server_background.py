"""Tests for promptise.mcp.server background tasks."""

from __future__ import annotations

from promptise.mcp.server import BackgroundTasks, Depends, MCPServer, TestClient

# =====================================================================
# BackgroundTasks unit tests
# =====================================================================


class TestBackgroundTasks:
    async def test_add_and_execute(self):
        results: list[str] = []

        async def task_a():
            results.append("a")

        def task_b():
            results.append("b")

        bg = BackgroundTasks()
        bg.add(task_a)
        bg.add(task_b)
        assert bg.pending == 2

        await bg.execute()
        assert results == ["a", "b"]
        assert bg.pending == 0

    async def test_execute_empty(self):
        bg = BackgroundTasks()
        await bg.execute()  # Should not raise

    async def test_args_and_kwargs(self):
        results: list[str] = []

        async def task(msg: str, prefix: str = ""):
            results.append(f"{prefix}{msg}")

        bg = BackgroundTasks()
        bg.add(task, "hello", prefix=">> ")
        await bg.execute()
        assert results == [">> hello"]

    async def test_error_swallowed(self):
        results: list[str] = []

        async def failing_task():
            raise RuntimeError("boom")

        async def success_task():
            results.append("ok")

        bg = BackgroundTasks()
        bg.add(failing_task)
        bg.add(success_task)

        # Should not raise — errors are logged and swallowed
        await bg.execute()
        assert results == ["ok"]

    async def test_sync_task(self):
        results: list[int] = []

        def sync_task(x: int):
            results.append(x * 2)

        bg = BackgroundTasks()
        bg.add(sync_task, 5)
        await bg.execute()
        assert results == [10]


# =====================================================================
# Integration with MCPServer via Depends
# =====================================================================


class TestBackgroundTasksIntegration:
    async def test_background_tasks_via_depends(self):
        server = MCPServer(name="test")
        executed: list[str] = []

        async def send_notification(data: str):
            executed.append(f"notified:{data}")

        @server.tool()
        async def process(
            data: str,
            bg: BackgroundTasks = Depends(BackgroundTasks),
        ) -> str:
            bg.add(send_notification, data)
            return "Done"

        client = TestClient(server)
        result = await client.call_tool("process", {"data": "hello"})
        assert result[0].text == "Done"
        assert executed == ["notified:hello"]

    async def test_multiple_background_tasks(self):
        server = MCPServer(name="test")
        executed: list[str] = []

        def log_event(event: str):
            executed.append(event)

        @server.tool()
        async def process(
            data: str,
            bg: BackgroundTasks = Depends(BackgroundTasks),
        ) -> str:
            bg.add(log_event, "start")
            bg.add(log_event, "end")
            return "Done"

        client = TestClient(server)
        await client.call_tool("process", {"data": "x"})
        assert executed == ["start", "end"]

    async def test_background_error_doesnt_affect_response(self):
        server = MCPServer(name="test")

        async def failing_task():
            raise RuntimeError("bg fail")

        @server.tool()
        async def process(
            x: int,
            bg: BackgroundTasks = Depends(BackgroundTasks),
        ) -> int:
            bg.add(failing_task)
            return x * 2

        client = TestClient(server)
        result = await client.call_tool("process", {"x": 5})
        assert result[0].text == "10"

    async def test_no_background_tasks_when_not_injected(self):
        """Tools without BackgroundTasks should work fine."""
        server = MCPServer(name="test")

        @server.tool()
        async def simple(x: int) -> int:
            return x + 1

        client = TestClient(server)
        result = await client.call_tool("simple", {"x": 5})
        assert result[0].text == "6"
