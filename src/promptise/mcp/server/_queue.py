"""MCP-native job queue for asynchronous background work.

Allows server authors to define long-running job types that agents
submit and poll for results, instead of blocking on synchronous
tool calls.

Example::

    from promptise.mcp.server import MCPServer
    from promptise.mcp.server._queue import MCPQueue

    server = MCPServer(name="analytics")
    queue = MCPQueue(server, max_workers=4)

    @queue.job(name="generate_report", timeout=60)
    async def generate_report(department: str) -> dict:
        await asyncio.sleep(10)  # long-running work
        return {"department": department, "rows": 500}

    # MCPQueue auto-registers 5 tools on the server:
    #   queue_submit, queue_status, queue_result, queue_cancel, queue_list
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import secrets
import time
import typing
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable

from ._cancellation import CancellationToken, CancelledError
from ._errors import ToolError

logger = logging.getLogger("promptise.server")


# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class JobStatus(str, Enum):
    """Lifecycle states of a queue job."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    FAILED = "failed"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"


class JobPriority(str, Enum):
    """Job priority levels. Higher priority jobs are dequeued first."""

    LOW = "low"
    NORMAL = "normal"
    HIGH = "high"
    CRITICAL = "critical"


# ---------------------------------------------------------------------------
# Data models
# ---------------------------------------------------------------------------


@dataclass
class Job:
    """Internal mutable record tracking a single job through its lifecycle.

    Attributes:
        id: Unique job identifier (hex token).
        job_type: Registered job type name.
        args: Arguments passed at submission time.
        status: Current lifecycle state.
        priority: Scheduling priority.
        result: Return value on completion.
        error: Error message on failure.
        progress: Current progress value (0.0 to 1.0).
        progress_message: Human-readable progress status.
        created_at: Submission timestamp (monotonic).
        started_at: Execution start timestamp.
        completed_at: Completion timestamp.
        attempts: Number of execution attempts so far.
        max_retries: Maximum retry count for this job.
        timeout: Per-job timeout in seconds.
    """

    id: str
    job_type: str
    args: dict[str, Any]
    status: JobStatus = JobStatus.PENDING
    priority: JobPriority = JobPriority.NORMAL
    result: Any = None
    error: str | None = None
    progress: float = 0.0
    progress_message: str | None = None
    created_at: float = field(default_factory=time.monotonic)
    started_at: float | None = None
    completed_at: float | None = None
    attempts: int = 0
    max_retries: int = 0
    timeout: float | None = None


@dataclass(frozen=True)
class JobDef:
    """Definition of a registered job type (immutable).

    Created at decoration time by ``@queue.job()``.

    Attributes:
        name: Unique job type name.
        handler: The async callable that executes the job.
        description: Human-readable description.
        timeout: Default timeout in seconds.
        max_retries: Default max retry count.
        backoff_base: Exponential backoff base in seconds.
    """

    name: str
    handler: Any  # Callable
    description: str
    timeout: float = 300.0
    max_retries: int = 0
    backoff_base: float = 1.0


# ---------------------------------------------------------------------------
# Queue backend protocol + in-memory implementation
# ---------------------------------------------------------------------------

_PRIORITY_MAP: dict[JobPriority, int] = {
    JobPriority.CRITICAL: 0,
    JobPriority.HIGH: 1,
    JobPriority.NORMAL: 2,
    JobPriority.LOW: 3,
}


@runtime_checkable
class QueueBackend(Protocol):
    """Protocol for pluggable queue storage backends.

    The default implementation is ``InMemoryQueueBackend``.
    """

    async def enqueue(self, job: Job) -> None:
        """Add a job to the queue."""
        ...

    async def dequeue(self) -> Job | None:
        """Remove and return the highest-priority pending job, or ``None``."""
        ...

    async def get(self, job_id: str) -> Job | None:
        """Get a job by ID."""
        ...

    async def update(self, job: Job) -> None:
        """Update a job's state."""
        ...

    async def list_jobs(
        self,
        status: JobStatus | None = None,
        limit: int = 50,
    ) -> list[Job]:
        """List jobs, optionally filtered by status."""
        ...

    async def remove(self, job_id: str) -> bool:
        """Remove a job record. Returns ``True`` if found."""
        ...

    async def count(self, status: JobStatus | None = None) -> int:
        """Count jobs, optionally filtered by status."""
        ...


class InMemoryQueueBackend:
    """In-process queue backend using asyncio primitives.

    Uses ``asyncio.PriorityQueue`` for the pending queue and a dict
    for job storage. Suitable for single-process deployments and testing.

    Args:
        max_size: Maximum number of pending jobs (0 = unlimited).
    """

    def __init__(self, *, max_size: int = 0) -> None:
        self._jobs: dict[str, Job] = {}
        self._queue: asyncio.PriorityQueue[tuple[int, float, str]] = asyncio.PriorityQueue(
            maxsize=max_size
        )

    async def enqueue(self, job: Job) -> None:
        """Add a job to the queue."""
        self._jobs[job.id] = job
        pri = _PRIORITY_MAP.get(job.priority, 2)
        await self._queue.put((pri, job.created_at, job.id))

    async def dequeue(self) -> Job | None:
        """Remove and return the highest-priority pending job."""
        try:
            _, _, job_id = self._queue.get_nowait()
            job = self._jobs.get(job_id)
            if job is not None and job.status == JobStatus.PENDING:
                return job
            return None  # Job was cancelled while pending
        except asyncio.QueueEmpty:
            return None

    async def get(self, job_id: str) -> Job | None:
        """Get a job by ID."""
        return self._jobs.get(job_id)

    async def update(self, job: Job) -> None:
        """Update a job's state."""
        self._jobs[job.id] = job

    async def list_jobs(
        self,
        status: JobStatus | None = None,
        limit: int = 50,
    ) -> list[Job]:
        """List jobs, optionally filtered by status."""
        jobs = list(self._jobs.values())
        if status is not None:
            jobs = [j for j in jobs if j.status == status]
        jobs.sort(key=lambda j: j.created_at, reverse=True)
        return jobs[:limit]

    async def remove(self, job_id: str) -> bool:
        """Remove a job record."""
        return self._jobs.pop(job_id, None) is not None

    async def count(self, status: JobStatus | None = None) -> int:
        """Count jobs, optionally filtered by status."""
        if status is None:
            return len(self._jobs)
        return sum(1 for j in self._jobs.values() if j.status == status)


# ---------------------------------------------------------------------------
# Job progress reporter
# ---------------------------------------------------------------------------


class _JobProgressReporter:
    """Writes progress updates into a Job record.

    This allows agents polling ``queue_status`` to see real-time progress.
    """

    def __init__(self, job: Job, backend: QueueBackend) -> None:
        self._job = job
        self._backend = backend

    async def report(
        self,
        progress: float,
        *,
        total: float | None = None,
        message: str | None = None,
    ) -> None:
        """Update job progress and persist to backend."""
        if total and total > 0:
            self._job.progress = min(progress / total, 1.0)
        else:
            self._job.progress = min(progress, 1.0)
        self._job.progress_message = message
        await self._backend.update(self._job)


# ---------------------------------------------------------------------------
# MCPQueue
# ---------------------------------------------------------------------------


class MCPQueue:
    """MCP-native job queue for asynchronous background work.

    Allows server authors to define long-running job types that agents
    submit and poll for results, rather than blocking on synchronous
    tool calls.

    When attached to an ``MCPServer``, the queue auto-registers 5 MCP
    tools (``queue_submit``, ``queue_status``, ``queue_result``,
    ``queue_cancel``, ``queue_list``) and hooks into the server lifecycle
    for worker management.

    Args:
        server: The MCPServer to attach to. When provided, tools and
            lifecycle hooks are registered immediately.
        backend: Queue storage backend (default: InMemoryQueueBackend).
        max_workers: Maximum concurrent job workers.
        default_timeout: Default per-job timeout in seconds.
        result_ttl: How long to keep completed job results before
            auto-cleanup (seconds).
        cleanup_interval: Seconds between cleanup sweeps.
        tool_prefix: Prefix for auto-registered tool names.

    Example::

        server = MCPServer(name="analytics")
        queue = MCPQueue(server, max_workers=4)

        @queue.job(name="generate_report", timeout=60)
        async def generate_report(department: str) -> dict:
            await asyncio.sleep(10)
            return {"department": department, "rows": 500}

        server.run(transport="http", port=8080)
    """

    def __init__(
        self,
        server: Any | None = None,
        *,
        backend: QueueBackend | None = None,
        max_workers: int = 4,
        default_timeout: float = 300.0,
        result_ttl: float = 3600.0,
        cleanup_interval: float = 60.0,
        tool_prefix: str = "queue",
    ) -> None:
        self._backend = backend or InMemoryQueueBackend()
        self._max_workers = max_workers
        self._default_timeout = default_timeout
        self._result_ttl = result_ttl
        self._cleanup_interval = cleanup_interval
        self._tool_prefix = tool_prefix
        self._job_defs: dict[str, JobDef] = {}
        self._workers: list[asyncio.Task[None]] = []
        self._cleanup_task: asyncio.Task[None] | None = None
        self._shutdown_event: asyncio.Event | None = None
        self._cancellation_tokens: dict[str, CancellationToken] = {}

        if server is not None:
            self.register(server)

    # ------------------------------------------------------------------
    # Decorator: @queue.job()
    # ------------------------------------------------------------------

    def job(
        self,
        name: str | None = None,
        *,
        timeout: float | None = None,
        max_retries: int = 0,
        backoff_base: float = 1.0,
    ) -> Any:
        """Register an async function as a queue job type.

        The decorated function runs in background workers, not inline
        with the tool call. It receives its arguments as keyword args,
        and may optionally accept ``_JobProgressReporter`` or
        ``CancellationToken`` parameters (detected by type annotation).

        Args:
            name: Job type name (defaults to function name).
            timeout: Per-job timeout (overrides queue default).
            max_retries: Max retry attempts on failure.
            backoff_base: Exponential backoff base in seconds.

        Example::

            @queue.job(name="generate_report", timeout=60)
            async def generate_report(department: str) -> dict:
                return {"department": department, "rows": 500}
        """

        def decorator(func: Any) -> Any:
            job_name = name or func.__name__
            description = (func.__doc__ or "").strip().split("\n")[0] or job_name
            job_def = JobDef(
                name=job_name,
                handler=func,
                description=description,
                timeout=timeout or self._default_timeout,
                max_retries=max_retries,
                backoff_base=backoff_base,
            )
            if job_name in self._job_defs:
                raise ValueError(f"Job type '{job_name}' is already registered")
            self._job_defs[job_name] = job_def
            return func

        return decorator

    # ------------------------------------------------------------------
    # Registration on MCPServer
    # ------------------------------------------------------------------

    def register(self, server: Any) -> None:
        """Register queue tools and lifecycle hooks on an MCPServer.

        Called automatically when ``server`` is passed to the
        constructor. Call manually when constructing the queue
        separately.

        Args:
            server: The MCPServer instance.
        """
        self._register_tools(server)
        self._register_lifecycle(server)

    def _register_tools(self, server: Any) -> None:
        """Auto-register the 5 queue management MCP tools."""
        prefix = self._tool_prefix
        queue_ref = self

        @server.tool(
            name=f"{prefix}_submit",
            description=(
                "Submit a job to the background queue for async processing. "
                "Returns a job_id for tracking. Use queue_status to poll progress "
                "and queue_result to retrieve the output when complete."
            ),
            tags=["queue"],
        )
        async def queue_submit(
            job_type: str,
            args: dict[str, Any] | None = None,
            priority: str = "normal",
        ) -> dict[str, Any]:
            """Submit a job for background processing.

            Args:
                job_type: The registered job type name.
                args: Arguments to pass to the job handler.
                priority: Job priority (low, normal, high, critical).
            """
            return await queue_ref.submit(
                job_type,
                args or {},
                priority=JobPriority(priority),
            )

        @server.tool(
            name=f"{prefix}_status",
            description=(
                "Check the current status and progress of a queued job. "
                "Returns status, progress percentage, and progress message."
            ),
            tags=["queue"],
        )
        async def queue_status(job_id: str) -> dict[str, Any]:
            """Get the status of a job.

            Args:
                job_id: The job identifier returned by queue_submit.
            """
            return await queue_ref.status(job_id)

        @server.tool(
            name=f"{prefix}_result",
            description=(
                "Get the result of a completed job. If the job is still "
                "running, returns the current status instead."
            ),
            tags=["queue"],
        )
        async def queue_result(job_id: str) -> dict[str, Any]:
            """Get the result of a completed job.

            Args:
                job_id: The job identifier.
            """
            return await queue_ref.get_result(job_id)

        @server.tool(
            name=f"{prefix}_cancel",
            description="Cancel a pending or running job.",
            tags=["queue"],
        )
        async def queue_cancel(job_id: str) -> dict[str, Any]:
            """Cancel a job.

            Args:
                job_id: The job identifier to cancel.
            """
            return await queue_ref.cancel(job_id)

        @server.tool(
            name=f"{prefix}_list",
            description=(
                "List jobs in the queue, optionally filtered by status. "
                "Returns job summaries with status and progress."
            ),
            tags=["queue"],
        )
        async def queue_list(
            status: str | None = None,
            limit: int = 20,
            offset: int = 0,
        ) -> dict[str, Any]:
            """List jobs in the queue.

            Args:
                status: Filter by status (pending, running, completed, failed, cancelled, timeout).
                limit: Maximum number of jobs to return (default 20).
                offset: Number of jobs to skip for pagination (default 0).
            """
            result = await queue_ref.list_jobs(
                status=JobStatus(status) if status else None,
                limit=limit + offset,  # Fetch extra to handle offset
            )
            if offset > 0 and isinstance(result, dict) and "jobs" in result:
                result["jobs"] = result["jobs"][offset:]
            return result

    def _register_lifecycle(self, server: Any) -> None:
        """Hook into server startup/shutdown to manage workers."""
        queue_ref = self

        @server.on_startup
        async def _start_queue_workers() -> None:
            await queue_ref.start()

        @server.on_shutdown
        async def _stop_queue_workers() -> None:
            await queue_ref.stop()

    # ------------------------------------------------------------------
    # Core operations
    # ------------------------------------------------------------------

    async def submit(
        self,
        job_type: str,
        args: dict[str, Any],
        *,
        priority: JobPriority = JobPriority.NORMAL,
    ) -> dict[str, Any]:
        """Submit a job for background execution.

        Args:
            job_type: Registered job type name.
            args: Arguments for the job handler.
            priority: Scheduling priority.

        Returns:
            Dict with job_id, status, and job_type.

        Raises:
            ToolError: If the job type is not registered.
        """
        job_def = self._job_defs.get(job_type)
        if job_def is None:
            available = list(self._job_defs.keys())
            raise ToolError(
                f"Unknown job type: {job_type}",
                code="UNKNOWN_JOB_TYPE",
                retryable=False,
                suggestion=f"Available job types: {available}",
            )

        job = Job(
            id=secrets.token_hex(8),
            job_type=job_type,
            args=args,
            priority=priority,
            max_retries=job_def.max_retries,
            timeout=job_def.timeout,
        )
        await self._backend.enqueue(job)
        logger.info(
            "Job %s submitted (type=%s, priority=%s)",
            job.id,
            job_type,
            priority.value,
        )
        return {"job_id": job.id, "status": job.status.value, "job_type": job_type}

    async def status(self, job_id: str) -> dict[str, Any]:
        """Get job status.

        Args:
            job_id: Job identifier.

        Returns:
            Dict with job status information.

        Raises:
            ToolError: If the job is not found.
        """
        job = await self._backend.get(job_id)
        if job is None:
            raise ToolError(
                f"Job not found: {job_id}",
                code="JOB_NOT_FOUND",
                retryable=False,
            )
        return self._job_to_dict(job)

    async def get_result(self, job_id: str) -> dict[str, Any]:
        """Get job result.

        If the job is still in progress, returns current status
        instead of a result.

        Args:
            job_id: Job identifier.

        Returns:
            Dict with job result or status.

        Raises:
            ToolError: If the job is not found.
        """
        job = await self._backend.get(job_id)
        if job is None:
            raise ToolError(
                f"Job not found: {job_id}",
                code="JOB_NOT_FOUND",
                retryable=False,
            )
        if job.status in (JobStatus.RUNNING, JobStatus.PENDING):
            return {
                "job_id": job.id,
                "status": job.status.value,
                "message": "Job is still in progress. Poll again later.",
                "progress": job.progress,
            }
        return self._job_to_dict(job, include_result=True)

    async def cancel(self, job_id: str) -> dict[str, Any]:
        """Cancel a job.

        Args:
            job_id: Job identifier.

        Returns:
            Dict with cancellation result.

        Raises:
            ToolError: If the job is not found.
        """
        job = await self._backend.get(job_id)
        if job is None:
            raise ToolError(
                f"Job not found: {job_id}",
                code="JOB_NOT_FOUND",
                retryable=False,
            )
        if job.status in (
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.TIMEOUT,
        ):
            return {
                "job_id": job.id,
                "status": job.status.value,
                "message": "Job already finished.",
            }
        # Signal cancellation to running worker
        token = self._cancellation_tokens.get(job_id)
        if token is not None:
            token.cancel(reason="Cancelled by user")
        job.status = JobStatus.CANCELLED
        job.completed_at = time.monotonic()
        await self._backend.update(job)
        logger.info("Job %s cancelled", job_id)
        return {"job_id": job.id, "status": "cancelled"}

    async def list_jobs(
        self,
        status: JobStatus | None = None,
        limit: int = 20,
    ) -> dict[str, Any]:
        """List jobs.

        Args:
            status: Optional status filter.
            limit: Maximum number of jobs to return.

        Returns:
            Dict with jobs list and total count.
        """
        jobs = await self._backend.list_jobs(status=status, limit=limit)
        return {
            "jobs": [self._job_to_dict(j) for j in jobs],
            "total": await self._backend.count(status),
        }

    # ------------------------------------------------------------------
    # Worker management
    # ------------------------------------------------------------------

    async def start(self) -> None:
        """Start the worker loop and cleanup task."""
        self._shutdown_event = asyncio.Event()
        for i in range(self._max_workers):
            task = asyncio.create_task(
                self._worker_loop(worker_id=i),
                name=f"queue-worker-{i}",
            )
            self._workers.append(task)
        self._cleanup_task = asyncio.create_task(
            self._cleanup_loop(),
            name="queue-cleanup",
        )
        logger.info(
            "Queue started: %d workers, %d job types",
            self._max_workers,
            len(self._job_defs),
        )

    async def stop(self) -> None:
        """Gracefully stop all workers."""
        if self._shutdown_event is not None:
            self._shutdown_event.set()
        for task in self._workers:
            task.cancel()
        if self._workers:
            await asyncio.gather(*self._workers, return_exceptions=True)
        self._workers.clear()
        if self._cleanup_task is not None:
            self._cleanup_task.cancel()
            try:
                await self._cleanup_task
            except asyncio.CancelledError:
                pass
            self._cleanup_task = None
        logger.info("Queue stopped")

    async def _worker_loop(self, *, worker_id: int) -> None:
        """Single worker: dequeue jobs and execute them."""
        while True:
            try:
                if self._shutdown_event and self._shutdown_event.is_set():
                    break

                job = await self._backend.dequeue()
                if job is None:
                    await asyncio.sleep(0.1)
                    continue

                await self._execute_job(job, worker_id=worker_id)

            except asyncio.CancelledError:
                break
            except Exception:
                logger.debug(
                    "Worker %d encountered unexpected error",
                    worker_id,
                    exc_info=True,
                )
                await asyncio.sleep(1.0)

    async def _execute_job(self, job: Job, *, worker_id: int) -> None:
        """Execute a single job with timeout, cancellation, and retry."""
        job_def = self._job_defs.get(job.job_type)
        if job_def is None:
            job.status = JobStatus.FAILED
            job.error = f"Job type '{job.job_type}' no longer registered"
            job.completed_at = time.monotonic()
            await self._backend.update(job)
            return

        # Mark running
        job.status = JobStatus.RUNNING
        job.started_at = time.monotonic()
        job.attempts += 1
        await self._backend.update(job)

        # Create cancellation token
        cancel_token = CancellationToken()
        self._cancellation_tokens[job.id] = cancel_token

        # Create progress reporter
        progress = _JobProgressReporter(job, self._backend)

        try:
            # Build kwargs for the handler
            handler_kwargs = dict(job.args)

            # Inspect handler signature for injectable types.
            # Use typing.get_type_hints() to resolve string annotations
            # (from ``from __future__ import annotations``).
            try:
                resolved_hints = typing.get_type_hints(job_def.handler)
            except Exception:
                resolved_hints = {}
            sig = inspect.signature(job_def.handler)
            for pname, param in sig.parameters.items():
                if pname in handler_kwargs:
                    continue  # Already supplied by job args
                ann = resolved_hints.get(pname, param.annotation)
                # Match by type annotation
                if ann is _JobProgressReporter or (
                    isinstance(ann, type)
                    and ann.__name__ in ("ProgressReporter", "_JobProgressReporter")
                ):
                    handler_kwargs[pname] = progress
                elif ann is CancellationToken:
                    handler_kwargs[pname] = cancel_token
                # Also support Depends() markers (check default value)
                elif hasattr(param.default, "dependency"):
                    dep = param.default.dependency
                    if isinstance(dep, type) and dep.__name__ in (
                        "ProgressReporter",
                        "_JobProgressReporter",
                    ):
                        handler_kwargs[pname] = progress
                    elif dep is CancellationToken:
                        handler_kwargs[pname] = cancel_token

            # Execute with timeout
            timeout = job.timeout or job_def.timeout
            result = await asyncio.wait_for(
                self._invoke_handler(job_def.handler, handler_kwargs),
                timeout=timeout,
            )

            # Success
            job.status = JobStatus.COMPLETED
            job.result = result
            job.progress = 1.0
            job.completed_at = time.monotonic()

        except asyncio.TimeoutError:
            job.status = JobStatus.TIMEOUT
            job.error = f"Job timed out after {job.timeout or job_def.timeout}s"
            job.completed_at = time.monotonic()
            logger.warning("Job %s timed out (worker %d)", job.id, worker_id)

        except CancelledError:
            job.status = JobStatus.CANCELLED
            job.completed_at = time.monotonic()
            logger.info("Job %s cancelled during execution", job.id)

        except Exception as exc:
            # Check if we should retry
            if job.attempts < job_def.max_retries + 1:
                backoff = job_def.backoff_base * (2 ** (job.attempts - 1))
                job.status = JobStatus.PENDING
                job.error = f"Attempt {job.attempts} failed: {exc}. Retrying in {backoff}s."
                await self._backend.update(job)
                self._cancellation_tokens.pop(job.id, None)
                await asyncio.sleep(backoff)
                await self._backend.enqueue(job)
                logger.info(
                    "Job %s retry %d/%d after %.1fs",
                    job.id,
                    job.attempts,
                    job_def.max_retries,
                    backoff,
                )
                return
            else:
                job.status = JobStatus.FAILED
                job.error = str(exc)
                job.completed_at = time.monotonic()
                logger.error("Job %s failed (worker %d): %s", job.id, worker_id, exc)

        finally:
            self._cancellation_tokens.pop(job.id, None)

        await self._backend.update(job)

    async def _invoke_handler(
        self,
        handler: Any,
        kwargs: dict[str, Any],
    ) -> Any:
        """Call the job handler, supporting both sync and async."""
        result = handler(**kwargs)
        if asyncio.iscoroutine(result):
            result = await result
        return result

    # ------------------------------------------------------------------
    # Cleanup loop
    # ------------------------------------------------------------------

    async def _cleanup_loop(self) -> None:
        """Periodically remove completed/failed jobs past their TTL."""
        try:
            while True:
                await asyncio.sleep(self._cleanup_interval)
                await self._cleanup_expired()
        except asyncio.CancelledError:
            pass

    async def _cleanup_expired(self) -> None:
        """Remove terminal jobs older than result_ttl."""
        terminal = [
            JobStatus.COMPLETED,
            JobStatus.FAILED,
            JobStatus.CANCELLED,
            JobStatus.TIMEOUT,
        ]
        now = time.monotonic()
        for status in terminal:
            jobs = await self._backend.list_jobs(status=status, limit=1000)
            for job in jobs:
                if job.completed_at and (now - job.completed_at) > self._result_ttl:
                    await self._backend.remove(job.id)

    # ------------------------------------------------------------------
    # Health integration
    # ------------------------------------------------------------------

    def register_health(self, health: Any) -> None:
        """Add queue health checks to a HealthCheck instance.

        Args:
            health: The HealthCheck to register on.
        """
        backend = self._backend

        async def _queue_health() -> bool:
            pending = await backend.count(JobStatus.PENDING)
            return pending < 1000

        health.add_check("queue", _queue_health, required_for_ready=True)

    # ------------------------------------------------------------------
    # Properties
    # ------------------------------------------------------------------

    @property
    def job_types(self) -> list[str]:
        """List registered job type names."""
        return list(self._job_defs.keys())

    @property
    def backend(self) -> QueueBackend:
        """The queue storage backend."""
        return self._backend

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _job_to_dict(
        job: Job,
        *,
        include_result: bool = False,
    ) -> dict[str, Any]:
        """Serialize a Job to a dict for MCP tool responses."""
        d: dict[str, Any] = {
            "job_id": job.id,
            "job_type": job.job_type,
            "status": job.status.value,
            "priority": job.priority.value,
            "progress": job.progress,
            "attempts": job.attempts,
        }
        if job.progress_message:
            d["progress_message"] = job.progress_message
        if job.error:
            d["error"] = job.error
        if include_result and job.result is not None:
            d["result"] = job.result
        return d
