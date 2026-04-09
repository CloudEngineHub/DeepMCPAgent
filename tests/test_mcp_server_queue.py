"""Tests for the MCP server queue feature.

Covers: JobStatus, InMemoryQueueBackend, MCPQueue registration,
core operations, worker execution, TestClient integration,
cleanup, and health checks.
"""

from __future__ import annotations

import asyncio
from unittest.mock import MagicMock

import pytest

from promptise.mcp.server._cancellation import CancellationToken
from promptise.mcp.server._errors import ToolError
from promptise.mcp.server._queue import (
    InMemoryQueueBackend,
    Job,
    JobPriority,
    JobStatus,
    MCPQueue,
    _JobProgressReporter,
)

# ---------------------------------------------------------------------------
# 1. JobStatus / JobPriority enums
# ---------------------------------------------------------------------------


class TestJobStatus:
    """Verify JobStatus enum values."""

    def test_all_statuses_defined(self):
        assert len(JobStatus) == 6
        values = {s.value for s in JobStatus}
        assert values == {"pending", "running", "completed", "failed", "cancelled", "timeout"}

    def test_status_from_string(self):
        assert JobStatus("pending") == JobStatus.PENDING
        assert JobStatus("completed") == JobStatus.COMPLETED


class TestJobPriority:
    """Verify JobPriority enum values."""

    def test_all_priorities_defined(self):
        assert len(JobPriority) == 4
        values = {p.value for p in JobPriority}
        assert values == {"low", "normal", "high", "critical"}


# ---------------------------------------------------------------------------
# 2. InMemoryQueueBackend
# ---------------------------------------------------------------------------


class TestInMemoryQueueBackend:
    """Test the in-memory backend implementation."""

    @pytest.mark.asyncio
    async def test_enqueue_and_dequeue(self):
        backend = InMemoryQueueBackend()
        job = Job(id="j1", job_type="test", args={})
        await backend.enqueue(job)
        result = await backend.dequeue()
        assert result is not None
        assert result.id == "j1"

    @pytest.mark.asyncio
    async def test_dequeue_empty_returns_none(self):
        backend = InMemoryQueueBackend()
        result = await backend.dequeue()
        assert result is None

    @pytest.mark.asyncio
    async def test_priority_ordering(self):
        """Critical jobs should be dequeued before normal and low."""
        backend = InMemoryQueueBackend()
        low = Job(id="low", job_type="t", args={}, priority=JobPriority.LOW)
        normal = Job(id="normal", job_type="t", args={}, priority=JobPriority.NORMAL)
        critical = Job(id="critical", job_type="t", args={}, priority=JobPriority.CRITICAL)

        await backend.enqueue(low)
        await backend.enqueue(normal)
        await backend.enqueue(critical)

        first = await backend.dequeue()
        assert first is not None
        assert first.id == "critical"

        second = await backend.dequeue()
        assert second is not None
        assert second.id == "normal"

        third = await backend.dequeue()
        assert third is not None
        assert third.id == "low"

    @pytest.mark.asyncio
    async def test_get_by_id(self):
        backend = InMemoryQueueBackend()
        job = Job(id="j1", job_type="test", args={"key": "val"})
        await backend.enqueue(job)
        found = await backend.get("j1")
        assert found is not None
        assert found.args == {"key": "val"}
        assert await backend.get("nonexistent") is None

    @pytest.mark.asyncio
    async def test_update_job(self):
        backend = InMemoryQueueBackend()
        job = Job(id="j1", job_type="test", args={})
        await backend.enqueue(job)
        job.status = JobStatus.RUNNING
        await backend.update(job)
        found = await backend.get("j1")
        assert found is not None
        assert found.status == JobStatus.RUNNING

    @pytest.mark.asyncio
    async def test_list_jobs_all(self):
        backend = InMemoryQueueBackend()
        await backend.enqueue(Job(id="j1", job_type="t", args={}))
        await backend.enqueue(Job(id="j2", job_type="t", args={}))
        jobs = await backend.list_jobs()
        assert len(jobs) == 2

    @pytest.mark.asyncio
    async def test_list_jobs_filtered(self):
        backend = InMemoryQueueBackend()
        j1 = Job(id="j1", job_type="t", args={}, status=JobStatus.PENDING)
        j2 = Job(id="j2", job_type="t", args={}, status=JobStatus.COMPLETED)
        await backend.enqueue(j1)
        backend._jobs["j2"] = j2  # bypass queue for completed jobs

        pending = await backend.list_jobs(status=JobStatus.PENDING)
        assert len(pending) == 1
        assert pending[0].id == "j1"

    @pytest.mark.asyncio
    async def test_remove_job(self):
        backend = InMemoryQueueBackend()
        job = Job(id="j1", job_type="t", args={})
        await backend.enqueue(job)
        assert await backend.remove("j1") is True
        assert await backend.remove("j1") is False
        assert await backend.get("j1") is None

    @pytest.mark.asyncio
    async def test_count(self):
        backend = InMemoryQueueBackend()
        await backend.enqueue(Job(id="j1", job_type="t", args={}))
        j2 = Job(id="j2", job_type="t", args={}, status=JobStatus.COMPLETED)
        backend._jobs["j2"] = j2

        assert await backend.count() == 2
        assert await backend.count(JobStatus.PENDING) == 1
        assert await backend.count(JobStatus.COMPLETED) == 1

    @pytest.mark.asyncio
    async def test_dequeue_skips_cancelled(self):
        """If a job is cancelled while pending, dequeue should skip it."""
        backend = InMemoryQueueBackend()
        job = Job(id="j1", job_type="t", args={})
        await backend.enqueue(job)
        job.status = JobStatus.CANCELLED
        await backend.update(job)
        result = await backend.dequeue()
        assert result is None


# ---------------------------------------------------------------------------
# 3. MCPQueue registration
# ---------------------------------------------------------------------------


class TestMCPQueueRegistration:
    """Test job type registration and tool auto-registration."""

    def test_register_job_type(self):
        queue = MCPQueue()

        @queue.job(name="test_job")
        async def test_job(data: str) -> str:
            return data

        assert "test_job" in queue.job_types

    def test_duplicate_job_type_raises(self):
        queue = MCPQueue()

        @queue.job(name="dupe")
        async def first(data: str) -> str:
            return data

        with pytest.raises(ValueError, match="already registered"):

            @queue.job(name="dupe")
            async def second(data: str) -> str:
                return data

    def test_job_name_defaults_to_function_name(self):
        queue = MCPQueue()

        @queue.job()
        async def my_custom_job() -> str:
            return "ok"

        assert "my_custom_job" in queue.job_types

    def test_tools_registered_on_server(self):
        server = MagicMock()
        server.tool = MagicMock(side_effect=lambda **kw: lambda f: f)
        server.on_startup = MagicMock(side_effect=lambda f: f)
        server.on_shutdown = MagicMock(side_effect=lambda f: f)

        MCPQueue(server)

        # 5 tools should be registered
        assert server.tool.call_count == 5
        tool_names = [call.kwargs["name"] for call in server.tool.call_args_list]
        assert "queue_submit" in tool_names
        assert "queue_status" in tool_names
        assert "queue_result" in tool_names
        assert "queue_cancel" in tool_names
        assert "queue_list" in tool_names

    def test_custom_prefix(self):
        server = MagicMock()
        server.tool = MagicMock(side_effect=lambda **kw: lambda f: f)
        server.on_startup = MagicMock(side_effect=lambda f: f)
        server.on_shutdown = MagicMock(side_effect=lambda f: f)

        MCPQueue(server, tool_prefix="jobs")

        tool_names = [call.kwargs["name"] for call in server.tool.call_args_list]
        assert "jobs_submit" in tool_names
        assert "jobs_status" in tool_names


# ---------------------------------------------------------------------------
# 4. MCPQueue core operations
# ---------------------------------------------------------------------------


class TestMCPQueueOperations:
    """Test submit, status, result, cancel, list (unit tests)."""

    @pytest.mark.asyncio
    async def test_submit_returns_job_id(self):
        queue = MCPQueue()

        @queue.job(name="test")
        async def test_job(x: int) -> int:
            return x * 2

        result = await queue.submit("test", {"x": 5})
        assert "job_id" in result
        assert result["status"] == "pending"
        assert result["job_type"] == "test"

    @pytest.mark.asyncio
    async def test_submit_unknown_type_raises(self):
        queue = MCPQueue()

        with pytest.raises(ToolError, match="Unknown job type"):
            await queue.submit("nonexistent", {})

    @pytest.mark.asyncio
    async def test_status_returns_pending(self):
        queue = MCPQueue()

        @queue.job(name="test")
        async def test_job() -> str:
            return "ok"

        submitted = await queue.submit("test", {})
        status = await queue.status(submitted["job_id"])
        assert status["status"] == "pending"

    @pytest.mark.asyncio
    async def test_status_not_found(self):
        queue = MCPQueue()
        with pytest.raises(ToolError, match="Job not found"):
            await queue.status("nonexistent")

    @pytest.mark.asyncio
    async def test_cancel_pending_job(self):
        queue = MCPQueue()

        @queue.job(name="test")
        async def test_job() -> str:
            return "ok"

        submitted = await queue.submit("test", {})
        result = await queue.cancel(submitted["job_id"])
        assert result["status"] == "cancelled"

    @pytest.mark.asyncio
    async def test_cancel_completed_is_noop(self):
        queue = MCPQueue()
        backend = queue.backend

        @queue.job(name="test")
        async def test_job() -> str:
            return "ok"

        submitted = await queue.submit("test", {})
        job = await backend.get(submitted["job_id"])
        assert job is not None
        job.status = JobStatus.COMPLETED
        await backend.update(job)

        result = await queue.cancel(submitted["job_id"])
        assert result["message"] == "Job already finished."

    @pytest.mark.asyncio
    async def test_list_jobs_empty(self):
        queue = MCPQueue()
        result = await queue.list_jobs()
        assert result["jobs"] == []
        assert result["total"] == 0

    @pytest.mark.asyncio
    async def test_list_jobs_with_filter(self):
        queue = MCPQueue()

        @queue.job(name="test")
        async def test_job() -> str:
            return "ok"

        await queue.submit("test", {})
        result = await queue.list_jobs(status=JobStatus.PENDING)
        assert result["total"] == 1
        assert len(result["jobs"]) == 1

    @pytest.mark.asyncio
    async def test_get_result_while_running(self):
        queue = MCPQueue()

        @queue.job(name="test")
        async def test_job() -> str:
            return "ok"

        submitted = await queue.submit("test", {})
        result = await queue.get_result(submitted["job_id"])
        assert result["status"] == "pending"
        assert "Poll again later" in result["message"]

    @pytest.mark.asyncio
    async def test_get_result_completed(self):
        queue = MCPQueue()
        backend = queue.backend

        @queue.job(name="test")
        async def test_job() -> str:
            return "ok"

        submitted = await queue.submit("test", {})
        job = await backend.get(submitted["job_id"])
        assert job is not None
        job.status = JobStatus.COMPLETED
        job.result = {"value": 42}
        await backend.update(job)

        result = await queue.get_result(submitted["job_id"])
        assert result["status"] == "completed"
        assert result["result"] == {"value": 42}

    @pytest.mark.asyncio
    async def test_submit_with_priority(self):
        queue = MCPQueue()

        @queue.job(name="test")
        async def test_job() -> str:
            return "ok"

        result = await queue.submit("test", {}, priority=JobPriority.CRITICAL)
        status = await queue.status(result["job_id"])
        assert status["priority"] == "critical"


# ---------------------------------------------------------------------------
# 5. Worker execution
# ---------------------------------------------------------------------------


class TestQueueWorker:
    """Test worker execution."""

    @pytest.mark.asyncio
    async def test_job_executes_and_completes(self):
        queue = MCPQueue(max_workers=1)

        @queue.job(name="double")
        async def double(x: int) -> int:
            return x * 2

        submitted = await queue.submit("double", {"x": 21})
        await queue.start()
        try:
            # Wait for job to complete
            for _ in range(50):
                status = await queue.status(submitted["job_id"])
                if status["status"] == "completed":
                    break
                await asyncio.sleep(0.05)

            result = await queue.get_result(submitted["job_id"])
            assert result["status"] == "completed"
            assert result["result"] == 42
        finally:
            await queue.stop()

    @pytest.mark.asyncio
    async def test_job_with_progress(self):
        queue = MCPQueue(max_workers=1)

        @queue.job(name="stepped")
        async def stepped(progress: _JobProgressReporter) -> str:
            for i in range(5):
                await progress.report(i + 1, total=5, message=f"Step {i + 1}")
                await asyncio.sleep(0.01)
            return "done"

        submitted = await queue.submit("stepped", {})
        await queue.start()
        try:
            for _ in range(50):
                status = await queue.status(submitted["job_id"])
                if status["status"] == "completed":
                    break
                await asyncio.sleep(0.05)

            result = await queue.get_result(submitted["job_id"])
            assert result["status"] == "completed"
            assert result["result"] == "done"
        finally:
            await queue.stop()

    @pytest.mark.asyncio
    async def test_job_timeout(self):
        queue = MCPQueue(max_workers=1)

        @queue.job(name="slow", timeout=0.1)
        async def slow_job() -> str:
            await asyncio.sleep(10)
            return "never"

        submitted = await queue.submit("slow", {})
        await queue.start()
        try:
            for _ in range(50):
                status = await queue.status(submitted["job_id"])
                if status["status"] in ("timeout", "failed"):
                    break
                await asyncio.sleep(0.05)

            status = await queue.status(submitted["job_id"])
            assert status["status"] == "timeout"
        finally:
            await queue.stop()

    @pytest.mark.asyncio
    async def test_job_failure(self):
        queue = MCPQueue(max_workers=1)

        @queue.job(name="failing")
        async def failing_job() -> str:
            raise RuntimeError("Something went wrong")

        submitted = await queue.submit("failing", {})
        await queue.start()
        try:
            for _ in range(50):
                status = await queue.status(submitted["job_id"])
                if status["status"] == "failed":
                    break
                await asyncio.sleep(0.05)

            status = await queue.status(submitted["job_id"])
            assert status["status"] == "failed"
            assert "Something went wrong" in status["error"]
        finally:
            await queue.stop()

    @pytest.mark.asyncio
    async def test_job_retry_on_failure(self):
        attempt_count = 0

        queue = MCPQueue(max_workers=1)

        @queue.job(name="flaky", max_retries=2, backoff_base=0.01)
        async def flaky_job() -> str:
            nonlocal attempt_count
            attempt_count += 1
            if attempt_count < 3:
                raise RuntimeError(f"Fail attempt {attempt_count}")
            return "success"

        submitted = await queue.submit("flaky", {})
        await queue.start()
        try:
            for _ in range(100):
                status = await queue.status(submitted["job_id"])
                if status["status"] == "completed":
                    break
                await asyncio.sleep(0.05)

            result = await queue.get_result(submitted["job_id"])
            assert result["status"] == "completed"
            assert result["result"] == "success"
            assert attempt_count == 3
        finally:
            await queue.stop()

    @pytest.mark.asyncio
    async def test_job_cancellation(self):
        queue = MCPQueue(max_workers=1)

        @queue.job(name="cancellable")
        async def cancellable_job(cancel: CancellationToken) -> str:
            for _ in range(100):
                cancel.check()
                await asyncio.sleep(0.01)
            return "done"

        submitted = await queue.submit("cancellable", {})
        await queue.start()
        try:
            # Wait until running
            for _ in range(50):
                status = await queue.status(submitted["job_id"])
                if status["status"] == "running":
                    break
                await asyncio.sleep(0.05)

            # Cancel it
            await queue.cancel(submitted["job_id"])

            # Wait for cancellation to take effect
            for _ in range(50):
                status = await queue.status(submitted["job_id"])
                if status["status"] == "cancelled":
                    break
                await asyncio.sleep(0.05)

            assert status["status"] == "cancelled"
        finally:
            await queue.stop()


# ---------------------------------------------------------------------------
# 6. TestClient integration
# ---------------------------------------------------------------------------


class TestQueueWithTestClient:
    """Integration tests using TestClient."""

    @pytest.mark.asyncio
    async def test_submit_via_tool(self):
        from promptise.mcp.server import MCPServer, TestClient

        server = MCPServer(name="test")
        queue = MCPQueue(server, max_workers=1)

        @queue.job(name="echo")
        async def echo(msg: str) -> str:
            return msg

        client = TestClient(server)
        result = await client.call_tool(
            "queue_submit",
            {
                "job_type": "echo",
                "args": {"msg": "hello"},
            },
        )
        text = result[0].text
        assert "job_id" in text
        assert "pending" in text

    @pytest.mark.asyncio
    async def test_list_via_tool(self):
        from promptise.mcp.server import MCPServer, TestClient

        server = MCPServer(name="test")
        queue = MCPQueue(server, max_workers=1)

        @queue.job(name="echo")
        async def echo(msg: str) -> str:
            return msg

        client = TestClient(server)
        result = await client.call_tool("queue_list", {})
        text = result[0].text
        assert "jobs" in text

    @pytest.mark.asyncio
    async def test_tools_visible_in_list(self):
        from promptise.mcp.server import MCPServer, TestClient

        server = MCPServer(name="test")
        MCPQueue(server)

        client = TestClient(server)
        tools = await client.list_tools()
        tool_names = {t.name for t in tools}
        assert "queue_submit" in tool_names
        assert "queue_status" in tool_names
        assert "queue_result" in tool_names
        assert "queue_cancel" in tool_names
        assert "queue_list" in tool_names

    @pytest.mark.asyncio
    async def test_full_lifecycle_via_testclient(self):
        """Submit → start workers → poll → get result."""
        from promptise.mcp.server import MCPServer, TestClient

        server = MCPServer(name="test")
        queue = MCPQueue(server, max_workers=1)

        @queue.job(name="add")
        async def add(a: int, b: int) -> int:
            return a + b

        client = TestClient(server)

        # Submit
        submit_result = await client.call_tool(
            "queue_submit",
            {
                "job_type": "add",
                "args": {"a": 3, "b": 7},
            },
        )
        import json

        submit_data = json.loads(submit_result[0].text)
        job_id = submit_data["job_id"]

        # Start workers manually (TestClient doesn't trigger lifecycle)
        await queue.start()
        try:
            # Poll until complete
            for _ in range(50):
                status_result = await client.call_tool("queue_status", {"job_id": job_id})
                status_data = json.loads(status_result[0].text)
                if status_data["status"] == "completed":
                    break
                await asyncio.sleep(0.05)

            # Get result
            result_result = await client.call_tool("queue_result", {"job_id": job_id})
            result_data = json.loads(result_result[0].text)
            assert result_data["status"] == "completed"
            assert result_data["result"] == 10
        finally:
            await queue.stop()


# ---------------------------------------------------------------------------
# 7. Cleanup
# ---------------------------------------------------------------------------


class TestQueueCleanup:
    """Test result TTL cleanup."""

    @pytest.mark.asyncio
    async def test_expired_results_removed(self):
        import time

        queue = MCPQueue(result_ttl=0.01)

        @queue.job(name="test")
        async def test_job() -> str:
            return "ok"

        submitted = await queue.submit("test", {})
        job = await queue.backend.get(submitted["job_id"])
        assert job is not None
        job.status = JobStatus.COMPLETED
        job.completed_at = time.monotonic() - 1.0  # 1 second ago
        await queue.backend.update(job)

        await queue._cleanup_expired()
        assert await queue.backend.get(submitted["job_id"]) is None

    @pytest.mark.asyncio
    async def test_non_expired_results_kept(self):
        import time

        queue = MCPQueue(result_ttl=3600)

        @queue.job(name="test")
        async def test_job() -> str:
            return "ok"

        submitted = await queue.submit("test", {})
        job = await queue.backend.get(submitted["job_id"])
        assert job is not None
        job.status = JobStatus.COMPLETED
        job.completed_at = time.monotonic()
        await queue.backend.update(job)

        await queue._cleanup_expired()
        assert await queue.backend.get(submitted["job_id"]) is not None


# ---------------------------------------------------------------------------
# 8. Health check
# ---------------------------------------------------------------------------


class TestQueueHealth:
    """Test health check integration."""

    @pytest.mark.asyncio
    async def test_healthy_when_few_pending(self):
        from promptise.mcp.server._health import HealthCheck

        queue = MCPQueue()
        health = HealthCheck()
        queue.register_health(health)

        result = await health.readiness()
        assert "ready" in result

    @pytest.mark.asyncio
    async def test_register_health_check(self):
        health = MagicMock()
        health.add_check = MagicMock()

        queue = MCPQueue()
        queue.register_health(health)

        health.add_check.assert_called_once()
        args = health.add_check.call_args
        assert args[0][0] == "queue"


# ---------------------------------------------------------------------------
# 9. _JobProgressReporter
# ---------------------------------------------------------------------------


class TestJobProgressReporter:
    """Test the progress reporter writes to Job record."""

    @pytest.mark.asyncio
    async def test_report_with_total(self):
        backend = InMemoryQueueBackend()
        job = Job(id="j1", job_type="t", args={})
        await backend.enqueue(job)

        reporter = _JobProgressReporter(job, backend)
        await reporter.report(3, total=10, message="Processing")

        updated = await backend.get("j1")
        assert updated is not None
        assert updated.progress == pytest.approx(0.3)
        assert updated.progress_message == "Processing"

    @pytest.mark.asyncio
    async def test_report_without_total(self):
        backend = InMemoryQueueBackend()
        job = Job(id="j1", job_type="t", args={})
        await backend.enqueue(job)

        reporter = _JobProgressReporter(job, backend)
        await reporter.report(0.75)

        updated = await backend.get("j1")
        assert updated is not None
        assert updated.progress == pytest.approx(0.75)

    @pytest.mark.asyncio
    async def test_progress_capped_at_one(self):
        backend = InMemoryQueueBackend()
        job = Job(id="j1", job_type="t", args={})
        await backend.enqueue(job)

        reporter = _JobProgressReporter(job, backend)
        await reporter.report(15, total=10)

        updated = await backend.get("j1")
        assert updated is not None
        assert updated.progress == 1.0
