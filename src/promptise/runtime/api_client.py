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

        # Ask a question and wait for the agent's answer (None on timeout)
        answer = await client.ask("monitor", "What anomalies have you found?")
        print(answer)

Errors: a ``4xx``/``5xx`` answer raises :class:`OrchestrationAPIError`, a
subclass of ``httpx.HTTPStatusError`` that carries the server's error
``code`` (``PROCESS_NOT_FOUND``, ``SECRET_NOT_FOUND``, ...) and message.
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

import httpx

__all__ = ["OrchestrationAPIError", "OrchestrationClient"]


class OrchestrationAPIError(httpx.HTTPStatusError):
    """An error answer from the Orchestration API.

    Subclasses ``httpx.HTTPStatusError``, so code that catches that keeps
    working.

    Attributes:
        status_code: HTTP status (``404``, ``409``, ...).
        code: The API's error code (``"PROCESS_NOT_FOUND"``, ...), or
            ``None`` when the body was not an API error object.
        error_message: The API's error message (or the raw body).
        error_id: The id under which the server logged the failure's
            details (``500`` and other failures whose cause stays
            server-side), or ``None``.
    """

    def __init__(self, response: httpx.Response) -> None:
        code: str | None = None
        error_id: str | None = None
        message = response.text
        try:
            error = response.json().get("error")
            if isinstance(error, dict):
                code = error.get("code")
                message = error.get("message", message)
                error_id = error.get("error_id")
        except (ValueError, AttributeError):
            pass  # not a JSON error object: keep the raw body as the message
        self.status_code = response.status_code
        self.code = code
        self.error_message = message
        self.error_id = error_id
        label = f"{response.status_code} {code}" if code else str(response.status_code)
        if error_id:
            message = f"{message} [error_id={error_id}]"
        super().__init__(
            f"{label}: {message} ({response.request.method} {response.request.url.path})",
            request=response.request,
            response=response,
        )


def _segment(value: str) -> str:
    """Quote one URL path segment (names may hold any character)."""
    return quote(str(value), safe="")


class OrchestrationClient:
    """Async client for the Promptise Orchestration API.

    Provides typed methods for every API endpoint.  Uses httpx
    internally — the client is an async context manager that manages
    the HTTP connection pool.

    Args:
        base_url: Base URL of the Orchestration API
            (e.g. ``"http://localhost:9100"``).
        token: Bearer token for authentication (the server's
            ``auth_token``).
        auth_token: Alias of *token*, matching the server's argument name.
        timeout: Default request timeout in seconds.

    Raises:
        ValueError: *token* and *auth_token* are both given and differ.

    Use it as an async context manager, or call :meth:`aclose` when done
    (the connection pool is opened on first use).

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
        auth_token: str | None = None,
        timeout: float = 30.0,
    ) -> None:
        if token is not None and auth_token is not None and token != auth_token:
            raise ValueError(
                "OrchestrationClient: pass the bearer token as token= or auth_token=, not both"
            )
        self._base = base_url.rstrip("/")
        self._token = token if token is not None else auth_token
        self._timeout = timeout
        self._client: httpx.AsyncClient | None = None

    async def __aenter__(self) -> OrchestrationClient:
        self._http()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        """Close the HTTP connection pool."""
        if self._client is not None:
            await self._client.aclose()
            self._client = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _http(self) -> httpx.AsyncClient:
        if self._client is None:
            headers: dict[str, str] = {}
            if self._token:
                headers["Authorization"] = f"Bearer {self._token}"
            self._client = httpx.AsyncClient(
                base_url=self._base,
                headers=headers,
                timeout=self._timeout,
            )
        return self._client

    async def _request(
        self,
        method: str,
        path: str,
        *,
        body: dict[str, Any] | None = None,
        params: dict[str, Any] | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send a request; return the JSON body (``{}`` for ``204 No Content``).

        Raises:
            OrchestrationAPIError: The server answered ``4xx``/``5xx``.
        """
        kwargs: dict[str, Any] = {}
        if body is not None:
            kwargs["json"] = body
        if params:
            kwargs["params"] = params
        if timeout is not None:
            kwargs["timeout"] = httpx.Timeout(timeout)
        resp = await self._http().request(method, path, **kwargs)
        if resp.is_error:
            raise OrchestrationAPIError(resp)
        if resp.status_code == 204 or not resp.content.strip():
            return {}
        data = resp.json()
        return data if isinstance(data, dict) else {"data": data}

    async def _get(self, path: str, **params: Any) -> dict[str, Any]:
        return await self._request("GET", path, params=params or None)

    async def _post(self, path: str, body: dict[str, Any] | None = None) -> dict[str, Any]:
        return await self._request("POST", path, body=body or {})

    async def _patch(self, path: str, body: dict[str, Any]) -> dict[str, Any]:
        return await self._request("PATCH", path, body=body)

    async def _delete(self, path: str) -> dict[str, Any]:
        return await self._request("DELETE", path)

    @staticmethod
    def _proc(name: str, *rest: str) -> str:
        """``/api/v1/processes/{name}/...`` with every segment quoted."""
        return "/".join(["/api/v1/processes", _segment(name), *(_segment(r) for r in rest)])

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

    async def shutdown(self) -> dict[str, Any]:
        """Ask the server to shut its runtime down (``POST /api/v1/runtime/shutdown``).

        Answered ``202 {"status": "shutting_down"}`` when the API was
        created with ``on_shutdown`` (as ``promptise runtime start`` does);
        otherwise the route doesn't exist and this raises
        :class:`OrchestrationAPIError` with status 404.
        """
        return await self._post("/api/v1/runtime/shutdown")

    # ------------------------------------------------------------------
    # Process lifecycle
    # ------------------------------------------------------------------

    async def list_processes(self) -> list[dict[str, Any]]:
        """List all registered processes."""
        data = await self._get("/api/v1/processes")
        return data.get("processes", [])

    async def get_process(self, name: str) -> dict[str, Any]:
        """Get detailed status for a single process."""
        return await self._get(self._proc(name))

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
        """Remove a process (stops it first if running).

        Returns:
            ``{}`` (the server answers ``204 No Content``).
        """
        return await self._delete(self._proc(name))

    async def start_process(self, name: str) -> dict[str, Any]:
        """Start a process."""
        return await self._post(self._proc(name, "start"))

    async def stop_process(self, name: str) -> dict[str, Any]:
        """Stop a process."""
        return await self._post(self._proc(name, "stop"))

    async def restart_process(self, name: str) -> dict[str, Any]:
        """Restart a process (stop then start)."""
        return await self._post(self._proc(name, "restart"))

    async def suspend_process(self, name: str) -> dict[str, Any]:
        """Suspend a process (pause trigger processing)."""
        return await self._post(self._proc(name, "suspend"))

    async def resume_process(self, name: str) -> dict[str, Any]:
        """Resume a suspended process."""
        return await self._post(self._proc(name, "resume"))

    # ------------------------------------------------------------------
    # Config updates
    # ------------------------------------------------------------------

    async def update_instructions(self, name: str, instructions: str) -> dict[str, Any]:
        """Update the system prompt (effective next invocation)."""
        return await self._patch(
            self._proc(name, "instructions"),
            {"instructions": instructions},
        )

    async def update_budget(self, name: str, **fields: Any) -> dict[str, Any]:
        """Update budget limits.

        New limits are read at the next budget check (the next tool call,
        LLM turn or run start); usage counted so far is kept.  The
        response's ``effective_from`` says when the change applies and
        ``live`` whether the running process uses it (``False`` when it
        only applies after a redeploy, e.g. ``enabled`` on a process that
        was built without a budget).

        Example::

            await client.update_budget("monitor", max_cost_per_day=500.0)
        """
        return await self._patch(self._proc(name, "budget"), fields)

    async def update_health(self, name: str, **fields: Any) -> dict[str, Any]:
        """Update health monitoring thresholds.

        See the response's ``effective_from`` and ``live`` fields, as for
        :meth:`update_budget`.
        """
        return await self._patch(self._proc(name, "health"), fields)

    async def update_mission(self, name: str, **fields: Any) -> dict[str, Any]:
        """Update mission configuration.

        See the response's ``effective_from`` and ``live`` fields, as for
        :meth:`update_budget`.
        """
        return await self._patch(self._proc(name, "mission"), fields)

    # ------------------------------------------------------------------
    # Triggers
    # ------------------------------------------------------------------

    async def list_triggers(self, name: str) -> list[dict[str, Any]]:
        """List all triggers (from config and agent-added) for a process.

        Each has a stable ``id``: removing one trigger does not change the
        others' IDs.
        """
        data = await self._get(self._proc(name, "triggers"))
        return data.get("triggers", [])

    async def add_trigger(self, name: str, trigger_config: dict[str, Any]) -> dict[str, Any]:
        """Add a trigger to a process.

        Returns:
            ``trigger_id`` (use it with :meth:`remove_trigger`), ``live``
            (whether it is already listening) and ``effective_from``
            (``"immediate"``, ``"next_start"`` or ``"restart"``).

        Example::

            await client.add_trigger("monitor", {
                "type": "cron",
                "cron_expression": "0 9 * * 1-5",
            })
        """
        return await self._post(self._proc(name, "triggers"), trigger_config)

    async def remove_trigger(self, name: str, trigger_id: str) -> dict[str, Any]:
        """Remove a trigger by the ``id`` from :meth:`list_triggers`.

        The response's ``live`` says whether the running trigger stopped
        now; when it is ``False`` the trigger keeps firing until the
        process restarts (``effective_from: "restart"``).
        """
        return await self._delete(self._proc(name, "triggers", trigger_id))

    # ------------------------------------------------------------------
    # Secrets
    # ------------------------------------------------------------------

    async def list_secrets(self, name: str) -> list[dict[str, Any]]:
        """List secret metadata: name, source and expiry (never values)."""
        data = await self._get(self._proc(name, "secrets"))
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
            ttl: New lifetime in seconds, counted from now.  Omitted: the
                secret keeps its current expiry.

        Raises:
            OrchestrationAPIError: ``404 SECRET_NOT_FOUND`` when the process
                has no secret of that name.
        """
        body: dict[str, Any] = {"value": value}
        if ttl is not None:
            body["ttl"] = ttl
        return await self._patch(self._proc(name, "secrets", secret_name), body)

    async def revoke_secrets(self, name: str) -> dict[str, Any]:
        """Revoke all secrets (zero-fill, immediate)."""
        return await self._delete(self._proc(name, "secrets"))

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
        data = await self._get(self._proc(name, "journal"), **params)
        return data.get("entries", [])

    # ------------------------------------------------------------------
    # Rewind
    # ------------------------------------------------------------------

    async def rewind_plan(
        self, name: str, target_entry_id: str, *, mode: str = "both"
    ) -> dict[str, Any]:
        """Preview a rewind (changes nothing).

        Args:
            name: Process name.
            target_entry_id: A journal ``entry_id`` (see :meth:`get_journal`).
            mode: ``both``, ``conversation_only``, ``code_only``,
                ``summarize`` or ``cancel``.

        Returns:
            The plan: ``entries_affected``, ``conversation_entries_affected``,
            ``code_entries_affected``, ``target_timestamp``, ``summary``, ...
        """
        data = await self._get(
            self._proc(name, "rewind", "plan"), target_entry_id=target_entry_id, mode=mode
        )
        plan: dict[str, Any] = data.get("plan", {})
        return plan

    async def rewind(
        self,
        name: str,
        target_entry_id: str,
        *,
        mode: str = "both",
        actor: str | None = None,
    ) -> dict[str, Any]:
        """Rewind a process to just before a journal entry.

        See :meth:`AgentProcess.rewind <promptise.runtime.AgentProcess.rewind>`.
        Raises :class:`OrchestrationAPIError` with code ``ENTRY_NOT_FOUND``,
        ``JOURNAL_DISABLED`` or ``REWIND_BUSY``.
        """
        body: dict[str, Any] = {"target_entry_id": target_entry_id, "mode": mode}
        if actor is not None:
            body["actor"] = actor
        return await self._post(self._proc(name, "rewind"), body)

    # ------------------------------------------------------------------
    # Mission control
    # ------------------------------------------------------------------

    async def get_mission(self, name: str) -> dict[str, Any]:
        """Get mission state and evaluation history."""
        return await self._get(self._proc(name, "mission"))

    async def fail_mission(
        self, name: str, reason: str = "Manually failed via client"
    ) -> dict[str, Any]:
        """Manually fail an active or paused mission (``409`` otherwise)."""
        return await self._post(self._proc(name, "mission", "fail"), {"reason": reason})

    async def pause_mission(self, name: str) -> dict[str, Any]:
        """Pause an active mission's evaluation (``409`` otherwise)."""
        return await self._post(self._proc(name, "mission", "pause"))

    async def resume_mission(self, name: str) -> dict[str, Any]:
        """Resume a paused mission (``409`` otherwise)."""
        return await self._post(self._proc(name, "mission", "resume"))

    # ------------------------------------------------------------------
    # Health
    # ------------------------------------------------------------------

    async def get_anomalies(self, name: str) -> list[dict[str, Any]]:
        """Get anomaly history for a process."""
        data = await self._get(self._proc(name, "health", "anomalies"))
        return data.get("anomalies", [])

    async def clear_anomalies(self, name: str) -> dict[str, Any]:
        """Clear anomaly history (and cooldowns) after resolving the issue."""
        return await self._delete(self._proc(name, "health", "anomalies"))

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
        ttl: float | None = None,
        metadata: dict[str, Any] | None = None,
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
            metadata: Custom data stored with the message.

        Raises:
            OrchestrationAPIError: ``409 PROCESS_NOT_RUNNING`` when the
                process is stopped or failed, ``429 RATE_LIMITED`` when the
                sender's rate limit is exceeded.
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
        if metadata:
            body["metadata"] = metadata
        return await self._post(self._proc(name, "messages"), body)

    async def ask(
        self,
        name: str,
        question: str,
        *,
        timeout: float = 120,
        sender_id: str | None = None,
        priority: str = "normal",
    ) -> str | None:
        """Ask a question and wait for the agent's answer.

        Long-polls until the agent answers or *timeout* expires.  The agent
        sees the question on its next run, so an agent that doesn't run
        within *timeout* (no trigger fires) can't answer it.

        Args:
            name: Process name.
            question: Question text.
            timeout: Max seconds the server waits for the answer.
            sender_id: Identifier for the sender.
            priority: One of ``"low"``, ``"normal"``, ``"high"``, ``"critical"``.

        Returns:
            The agent's answer text, or ``None`` when it did not answer
            within *timeout* (the server answers ``status: "timeout"``).

        Raises:
            OrchestrationAPIError: For an error answer, e.g. ``409
                PROCESS_NOT_RUNNING`` or ``400 INBOX_DISABLED``.
        """
        body: dict[str, Any] = {"content": question, "timeout": timeout, "priority": priority}
        if sender_id:
            body["sender_id"] = sender_id
        # The HTTP timeout outlasts the server's long-poll.
        data = await self._request("POST", self._proc(name, "ask"), body=body, timeout=timeout + 10)
        if data.get("status") != "answered":
            return None
        answer = data.get("response")
        return answer if isinstance(answer, str) else None

    async def get_inbox(self, name: str) -> dict[str, Any]:
        """Get inbox status for a process."""
        return await self._get(self._proc(name, "inbox"))

    async def clear_inbox(self, name: str) -> dict[str, Any]:
        """Clear all messages in the inbox.

        Returns:
            ``{}`` (the server answers ``204 No Content``).
        """
        return await self._delete(self._proc(name, "inbox"))

    # ------------------------------------------------------------------
    # Context
    # ------------------------------------------------------------------

    async def get_context(self, name: str) -> dict[str, Any]:
        """Get the process's AgentContext state."""
        return await self._get(self._proc(name, "context"))

    async def update_context(self, name: str, state: dict[str, Any]) -> dict[str, Any]:
        """Update AgentContext key-value state, all keys or none.

        Only writable keys are accepted: if any key is not writable the
        server answers ``403 WRITE_DENIED`` naming them, and writes nothing.

        Args:
            name: Process name.
            state: Dict of key-value pairs to update.
        """
        return await self._patch(self._proc(name, "context"), {"state": state})

    async def get_metrics(self, name: str) -> dict[str, Any]:
        """Get process metrics (invocation count, budget usage, etc.)."""
        return await self._get(self._proc(name, "metrics"))
