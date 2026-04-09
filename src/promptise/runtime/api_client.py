"""Orchestration API client for managing remote Agent Runtimes.

A typed Python client that mirrors every endpoint of the
:class:`OrchestrationAPI`.  Works with any HTTP client (uses httpx).

Example::

    from promptise.runtime import OrchestrationClient

    async with OrchestrationClient("http://localhost:9100", token="my-token") as client:
        # List all processes
        processes = await client.list_processes()

        # Deploy a new agent
        await client.deploy("monitor", {
            "model": "openai:gpt-5-mini",
            "instructions": "Monitor data pipelines.",
            "triggers": [{"type": "cron", "cron_expression": "*/5 * * * *"}],
        }, start=True)

        # Send a message to a running agent
        await client.send_message("monitor", "Ignore staging alerts for 1 hour")

        # Ask a question and wait for the agent's answer
        answer = await client.ask("monitor", "What anomalies have you found?")
        print(answer)
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("promptise.runtime.api_client")

__all__ = ["OrchestrationClient"]


class OrchestrationClient:
    """Async client for the Promptise Orchestration API.

    Provides typed methods for every API endpoint.  Uses httpx
    internally — the client is an async context manager that manages
    the HTTP connection pool.

    Args:
        base_url: Base URL of the Orchestration API
            (e.g. ``"http://localhost:9100"``).
        token: Bearer token for authentication.
        timeout: Default request timeout in seconds.

    Example::

        async with OrchestrationClient("http://localhost:9100", token="tok") as c:
            await c.start_process("monitor")
            status = await c.get_process("monitor")
    """

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = token
        self._timeout = timeout
        self._client: Any = None

    async def __aenter__(self) -> OrchestrationClient:
        import httpx

        headers: dict[str, str] = {}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        self._client = httpx.AsyncClient(
            base_url=self._base,
            headers=headers,
            timeout=self._timeout,
        )
        return self

    async def __aexit__(self, *exc: Any) -> None:
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        resp = await self._client.get(path, params=params or None)
        resp.raise_for_status()
        return resp.json()

    async def _post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        resp = await self._client.post(path, json=body or {})
        resp.raise_for_status()
        return resp.json()

    async def _patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        resp = await self._client.patch(path, json=body)
        resp.raise_for_status()
        return resp.json()

    async def _delete(self, path: str) -> dict[str, Any]:
        resp = await self._client.delete(path)
        resp.raise_for_status()
        return resp.json()

    # ------------------------------------------------------------------
    # Health & runtime
    # ------------------------------------------------------------------

    async def health(self) -> dict[str, Any]:
        """Health check (no auth required)."""
        return await self._get("/api/v1/health")

    async def runtime_status(self) -> dict[str, Any]:
        """Get global runtime status."""
        return await self._get("/api/v1/runtime/status")

    async def start_all(self) -> dict[str, Any]:
        """Start all processes."""
        return await self._post("/api/v1/runtime/start-all")

    async def stop_all(self) -> dict[str, Any]:
        """Stop all processes."""
        return await self._post("/api/v1/runtime/stop-all")

    # ------------------------------------------------------------------
    # Process lifecycle
    # ------------------------------------------------------------------

    async def list_processes(self) -> list[dict[str, Any]]:
        """List all registered processes."""
        data = await self._get("/api/v1/processes")
        return data.get("processes", [])

    async def get_process(self, name: str) -> dict[str, Any]:
        """Get detailed status for a single process."""
        return await self._get(f"/api/v1/processes/{name}")

    async def deploy(
        self,
        name: str,
        config: dict[str, Any],
        *,
        start: bool = False,
    ) -> dict[str, Any]:
        """Deploy a new agent process.

        Args:
            name: Process name (unique).
            config: ProcessConfig as a dict.
            start: Whether to start immediately after deployment.
        """
        return await self._post(
            "/api/v1/processes",
            {
                "name": name,
                "config": config,
                "start": start,
            },
        )

    async def remove_process(self, name: str) -> dict[str, Any]:
        """Remove a process (stops it first if running)."""
        return await self._delete(f"/api/v1/processes/{name}")

    async def start_process(self, name: str) -> dict[str, Any]:
        """Start a process."""
        return await self._post(f"/api/v1/processes/{name}/start")

    async def stop_process(self, name: str) -> dict[str, Any]:
        """Stop a process."""
        return await self._post(f"/api/v1/processes/{name}/stop")

    async def restart_process(self, name: str) -> dict[str, Any]:
        """Restart a process (stop then start)."""
        return await self._post(f"/api/v1/processes/{name}/restart")

    async def suspend_process(self, name: str) -> dict[str, Any]:
        """Suspend a process (pause trigger processing)."""
        return await self._post(f"/api/v1/processes/{name}/suspend")

    async def resume_process(self, name: str) -> dict[str, Any]:
        """Resume a suspended process."""
        return await self._post(f"/api/v1/processes/{name}/resume")

    # ------------------------------------------------------------------
    # Config updates
    # ------------------------------------------------------------------

    async def update_instructions(self, name: str, instructions: str) -> dict[str, Any]:
        """Update the system prompt (effective next invocation)."""
        return await self._patch(
            f"/api/v1/processes/{name}/instructions",
            {"instructions": instructions},
        )

    async def update_budget(self, name: str, **fields: Any) -> dict[str, Any]:
        """Update budget limits (effective immediately).

        Example::

            await client.update_budget("monitor", max_cost_per_day=500.0)
        """
        return await self._patch(f"/api/v1/processes/{name}/budget", fields)

    async def update_health(self, name: str, **fields: Any) -> dict[str, Any]:
        """Update health monitoring thresholds."""
        return await self._patch(f"/api/v1/processes/{name}/health", fields)

    async def update_mission(self, name: str, **fields: Any) -> dict[str, Any]:
        """Update mission configuration."""
        return await self._patch(f"/api/v1/processes/{name}/mission", fields)

    # ------------------------------------------------------------------
    # Triggers
    # ------------------------------------------------------------------

    async def list_triggers(self, name: str) -> list[dict[str, Any]]:
        """List all triggers (static + dynamic) for a process."""
        data = await self._get(f"/api/v1/processes/{name}/triggers")
        return data.get("triggers", [])

    async def add_trigger(self, name: str, trigger_config: dict[str, Any]) -> dict[str, Any]:
        """Add a trigger to a process.

        Example::

            await client.add_trigger("monitor", {
                "type": "cron",
                "cron_expression": "0 9 * * 1-5",
            })
        """
        return await self._post(f"/api/v1/processes/{name}/triggers", trigger_config)

    async def remove_trigger(self, name: str, trigger_id: str) -> dict[str, Any]:
        """Remove a trigger by ID."""
        return await self._delete(f"/api/v1/processes/{name}/triggers/{trigger_id}")

    # ------------------------------------------------------------------
    # Secrets
    # ------------------------------------------------------------------

    async def list_secrets(self, name: str) -> list[dict[str, Any]]:
        """List secret names and active status (never values)."""
        data = await self._get(f"/api/v1/processes/{name}/secrets")
        return data.get("secrets", [])

    async def rotate_secret(
        self,
        name: str,
        secret_name: str,
        value: str,
        *,
        ttl: int | None = None,
    ) -> dict[str, Any]:
        """Rotate a secret value (immediate effect).

        Args:
            name: Process name.
            secret_name: Name of the secret to rotate.
            value: New secret value.
            ttl: Optional TTL in seconds.
        """
        body: dict[str, Any] = {"value": value}
        if ttl is not None:
            body["ttl"] = ttl
        return await self._patch(f"/api/v1/processes/{name}/secrets/{secret_name}", body)

    async def revoke_secrets(self, name: str) -> dict[str, Any]:
        """Revoke all secrets (zero-fill, immediate)."""
        return await self._delete(f"/api/v1/processes/{name}/secrets")

    # ------------------------------------------------------------------
    # Journal
    # ------------------------------------------------------------------

    async def get_journal(
        self,
        name: str,
        *,
        limit: int = 50,
        entry_type: str | None = None,
    ) -> list[dict[str, Any]]:
        """Read journal entries for a process.

        Args:
            name: Process name.
            limit: Max entries to return (1-500).
            entry_type: Filter by entry type (e.g. ``"state_transition"``).
        """
        params: dict[str, Any] = {"limit": limit}
        if entry_type:
            params["type"] = entry_type
        data = await self._get(f"/api/v1/processes/{name}/journal", **params)
        return data.get("entries", [])

    # ------------------------------------------------------------------
    # Mission control
    # ------------------------------------------------------------------

    async def get_mission(self, name: str) -> dict[str, Any]:
        """Get mission state and evaluation history."""
        return await self._get(f"/api/v1/processes/{name}/mission")

    async def fail_mission(
        self, name: str, reason: str = "Manually failed via client"
    ) -> dict[str, Any]:
        """Manually fail a mission."""
        return await self._post(f"/api/v1/processes/{name}/mission/fail", {"reason": reason})

    async def pause_mission(self, name: str) -> dict[str, Any]:
        """Pause mission evaluation."""
        return await self._post(f"/api/v1/processes/{name}/mission/pause")

    async def resume_mission(self, name: str) -> dict[str, Any]:
        """Resume mission evaluation."""
        return await self._post(f"/api/v1/processes/{name}/mission/resume")

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def get_anomalies(self, name: str) -> list[dict[str, Any]]:
        """Get anomaly history for a process."""
        data = await self._get(f"/api/v1/processes/{name}/health/anomalies")
        return data.get("anomalies", [])

    async def clear_anomalies(self, name: str) -> dict[str, Any]:
        """Clear anomaly history after resolving the issue."""
        return await self._delete(f"/api/v1/processes/{name}/health/anomalies")

    # ------------------------------------------------------------------
    # Human communication
    # ------------------------------------------------------------------

    async def send_message(
        self,
        name: str,
        content: str,
        *,
        message_type: str = "context",
        priority: str = "normal",
        sender_id: str | None = None,
        ttl: int | None = None,
    ) -> dict[str, Any]:
        """Send a message to a running agent.

        Args:
            name: Process name.
            content: Message content.
            message_type: One of ``"directive"``, ``"context"``,
                ``"question"``, ``"correction"``.
            priority: One of ``"low"``, ``"normal"``, ``"high"``, ``"critical"``.
            sender_id: Identifier for the sender (for audit trail).
            ttl: Time-to-live in seconds (message expires after TTL).
        """
        body: dict[str, Any] = {
            "content": content,
            "message_type": message_type,
            "priority": priority,
        }
        if sender_id:
            body["sender_id"] = sender_id
        if ttl is not None:
            body["ttl"] = ttl
        return await self._post(f"/api/v1/processes/{name}/messages", body)

    async def ask(
        self,
        name: str,
        question: str,
        *,
        timeout: float = 120,
        sender_id: str | None = None,
    ) -> str:
        """Ask a question and wait for the agent's answer.

        Long-polls until the agent responds or timeout expires.

        Args:
            name: Process name.
            question: Question text.
            timeout: Max seconds to wait for a response.
            sender_id: Identifier for the sender.

        Returns:
            The agent's response text.

        Raises:
            httpx.TimeoutException: If the agent doesn't respond in time.
        """
        body: dict[str, Any] = {"content": question, "timeout": timeout}
        if sender_id:
            body["sender_id"] = sender_id
        # Use a longer HTTP timeout to allow for long-polling
        import httpx

        resp = await self._client.post(
            f"/api/v1/processes/{name}/ask",
            json=body,
            timeout=httpx.Timeout(timeout + 10),
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("response", data.get("answer", ""))

    async def get_inbox(self, name: str) -> dict[str, Any]:
        """Get inbox status for a process."""
        return await self._get(f"/api/v1/processes/{name}/inbox")

    async def clear_inbox(self, name: str) -> dict[str, Any]:
        """Clear all messages in the inbox."""
        return await self._delete(f"/api/v1/processes/{name}/inbox")

    # ------------------------------------------------------------------
    # Context
    # ------------------------------------------------------------------

    async def get_context(self, name: str) -> dict[str, Any]:
        """Get the process's AgentContext state."""
        return await self._get(f"/api/v1/processes/{name}/context")

    async def update_context(self, name: str, state: dict[str, Any]) -> dict[str, Any]:
        """Update AgentContext key-value state.

        Only writable keys are accepted. Others return 403.

        Args:
            name: Process name.
            state: Dict of key-value pairs to update.
        """
        return await self._patch(f"/api/v1/processes/{name}/context", {"state": state})

    async def get_metrics(self, name: str) -> dict[str, Any]:
        """Get process metrics (invocation count, budget usage, etc.)."""
        return await self._get(f"/api/v1/processes/{name}/metrics")
