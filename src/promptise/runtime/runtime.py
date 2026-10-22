"""AgentRuntime — multi-process manager for agent processes.

The :class:`AgentRuntime` is the operating system kernel for your AI
agents: it manages a collection of :class:`AgentProcess` instances,
handles shared resources (EventBus, MessageBroker), and provides the
surface API consumed by the CLI and programmatic callers.

Example::

    from promptise.runtime import AgentRuntime, ProcessConfig, TriggerConfig

    async with AgentRuntime() as runtime:
        await runtime.add_process(
            "data-watcher",
            ProcessConfig(
                model="openai:gpt-5-mini",
                instructions="You monitor data pipelines.",
                triggers=[
                    TriggerConfig(type="cron", cron_expression="*/5 * * * *"),
                ],
            ),
        )
        await runtime.start_all()

        # Runtime manages processes until stopped …
        await runtime.stop_all()

For a long-running service, :meth:`AgentRuntime.run_forever` starts every
process and stops them cleanly on ``SIGTERM`` / ``SIGINT`` (``docker
stop``, Ctrl+C), letting runs in flight finish first for up to each
process's ``drain_timeout`` seconds::

    runtime = AgentRuntime(RuntimeConfig(processes={"watcher": config}))
    await runtime.run_forever()
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import signal
from pathlib import Path
from typing import Any

from .._logsafe import log_safe
from .config import ProcessConfig, RuntimeConfig
from .exceptions import ManifestError, RuntimeBaseError
from .lifecycle import ProcessState
from .manifest import (
    load_manifest,
    manifest_to_process_config,
)
from .process import AgentProcess

logger = logging.getLogger(__name__)


class AgentRuntime:
    """Multi-process agent runtime manager.

    Manages a dictionary of :class:`AgentProcess` instances with shared
    resources and coordinated lifecycle.

    The processes declared in ``config.processes`` are registered at
    construction (not started): ``AgentRuntime(config)`` and
    :meth:`from_config` are equivalent.

    ``async with AgentRuntime(...)`` stops and closes every process on
    exit.  Signals are not handled there: use :meth:`run_forever` (or
    call :meth:`request_shutdown` from your own handler) so ``SIGTERM``
    shuts the runtime down cleanly instead of killing it mid-run.

    Distributed mode: with ``config.distributed.enabled``,
    :meth:`start_all` (and :meth:`run_forever`) first start this node's
    management transport and, per the config, the cluster coordinator or
    the registration with one (see
    :class:`~promptise.runtime.config.DistributedConfig`;
    :attr:`cluster`).  :meth:`shutdown` stops them.

    Args:
        config: Optional :class:`RuntimeConfig` for global settings and
            the processes to register.
        event_bus: Optional shared EventBus for inter-process events.
        broker: Optional shared MessageBroker for message triggers.
        event_notifier: Optional :class:`~promptise.events.EventNotifier`
            shared by all processes (drained by :meth:`stop_all`).
        hooks: Optional :class:`~promptise.runtime.hooks.HookManager`
            shared by all processes: its hooks fire for every process's
            lifecycle events (after each process's own ``hooks``;
            ``ctx.process_id`` names the process).
    """

    def __init__(
        self,
        config: RuntimeConfig | None = None,
        *,
        event_bus: Any | None = None,
        broker: Any | None = None,
        event_notifier: Any | None = None,
        hooks: Any | None = None,
    ) -> None:
        self._config = config or RuntimeConfig()
        self._event_bus = event_bus
        self._broker = broker
        self._event_notifier = event_notifier
        self._hooks = hooks
        # Distributed mode (started by start_all, see start_distributed)
        self._cluster: Any | None = None

        # Process registry
        self._processes: dict[str, AgentProcess] = {}
        self._lock = asyncio.Lock()
        # Set by request_shutdown() (and the signal handlers of run_forever()).
        self._shutdown_event: asyncio.Event | None = None

        for name, process_config in self._config.processes.items():
            self._register(name, process_config)

    # ------------------------------------------------------------------
    # Process management
    # ------------------------------------------------------------------

    async def add_process(
        self,
        name: str,
        config: ProcessConfig,
        *,
        process_id: str | None = None,
        deploy_policy: Any | None = None,
    ) -> AgentProcess:
        """Register a new agent process.

        Args:
            name: Unique process name.
            config: Process configuration.
            process_id: Optional explicit process ID.
            deploy_policy: Where the config came from (see
                :class:`~promptise.runtime.process.AgentProcess`); the
                control API passes ``source="api"`` for its deploys.

        Returns:
            The created :class:`AgentProcess`.

        Raises:
            RuntimeBaseError: If a process with this name already exists.
        """
        async with self._lock:
            return self._register(name, config, process_id=process_id, deploy_policy=deploy_policy)

    def _register(
        self,
        name: str,
        config: ProcessConfig,
        *,
        process_id: str | None = None,
        deploy_policy: Any | None = None,
    ) -> AgentProcess:
        if name in self._processes:
            raise RuntimeBaseError(
                f"Process {name!r} already exists. Use remove_process() "
                "first or choose a different name."
            )

        process = AgentProcess(
            name=name,
            config=config,
            process_id=process_id,
            event_bus=self._event_bus,
            broker=self._broker,
            runtime=self,
            event_notifier=self._event_notifier,
            hooks=self._hooks,
            deploy_policy=deploy_policy,
        )
        self._processes[name] = process
        logger.info("AgentRuntime: registered process %s", log_safe(repr(name)))
        return process

    async def remove_process(self, name: str) -> None:
        """Remove a process, stopping and closing it first.

        Works in every state: a ``FAILED`` process is stopped too, so its
        triggers, listeners and heartbeat end before it is removed.  The
        process is then closed (:meth:`AgentProcess.close`: long-term
        memory closed, secrets revoked).

        Args:
            name: Process name to remove.

        Raises:
            KeyError: If the process does not exist.
        """
        async with self._lock:
            process = self._processes.get(name)
            if process is None:
                raise KeyError(f"No process named {name!r}")

            try:
                await process.close()
            finally:
                del self._processes[name]
            logger.info("AgentRuntime: removed process %s", log_safe(repr(name)))

    def get_process(self, name: str) -> AgentProcess:
        """Get a process by name.

        Args:
            name: Process name.

        Returns:
            The :class:`AgentProcess`.

        Raises:
            KeyError: If the process does not exist.
        """
        process = self._processes.get(name)
        if process is None:
            raise KeyError(f"No process named {name!r}")
        return process

    @property
    def processes(self) -> dict[str, AgentProcess]:
        """Read-only view of registered processes."""
        return dict(self._processes)

    # ------------------------------------------------------------------
    # Manifest loading
    # ------------------------------------------------------------------

    async def load_manifest(
        self,
        path: str | Path,
        *,
        name_override: str | None = None,
    ) -> AgentProcess:
        """Load and register a process from a ``.agent`` manifest.

        Args:
            path: Path to the ``.agent`` file.
            name_override: Override the process name from the manifest.

        Returns:
            The created :class:`AgentProcess`.

        Raises:
            ManifestError: If the manifest cannot be loaded.
        """
        manifest = load_manifest(path)
        config = manifest_to_process_config(manifest)
        name = name_override or manifest.name
        return await self.add_process(name, config)

    async def load_directory(self, directory: str | Path) -> list[str]:
        """Load all ``.agent`` files from a directory.

        Args:
            directory: Directory containing ``.agent`` files.

        Returns:
            List of process names that were loaded.

        Raises:
            ManifestError: If any manifest fails to load.
        """
        directory = Path(directory)
        if not directory.is_dir():
            raise ManifestError(f"Not a directory: {directory}")

        loaded: list[str] = []
        for agent_file in sorted(directory.glob("*.agent")):
            try:
                process = await self.load_manifest(agent_file)
                loaded.append(process.name)
                logger.info(
                    "AgentRuntime: loaded %s from %s",
                    process.name,
                    agent_file,
                )
            except Exception as exc:
                logger.error(
                    "AgentRuntime: failed to load %s: %s",
                    agent_file,
                    exc,
                )
                raise

        return loaded

    # ------------------------------------------------------------------
    # Lifecycle control
    # ------------------------------------------------------------------

    async def start_process(self, name: str) -> None:
        """Start a single process.

        Args:
            name: Process name.

        Raises:
            KeyError: If the process does not exist.
        """
        process = self.get_process(name)
        await process.start()

    async def stop_process(self, name: str) -> None:
        """Stop a single process.

        Args:
            name: Process name.

        Raises:
            KeyError: If the process does not exist.
        """
        process = self.get_process(name)
        await process.stop()

    async def restart_process(self, name: str, *, stop_timeout: float = 30.0) -> None:
        """Restart a single process (stop then start).

        A running or suspended process is stopped first; a ``FAILED`` one
        is torn down by :meth:`AgentProcess.start` itself (its old
        triggers, listeners and heartbeat end before new ones start), so
        nothing fires twice.  Context state, long-term memory and the
        conversation history carry over.

        A process that is already stopping (another stop or restart in
        progress) is waited for, up to *stop_timeout* seconds.

        Args:
            name: Process name.
            stop_timeout: Seconds to wait for a stop in progress.

        Raises:
            KeyError: If the process does not exist.
            RuntimeError: The process was still stopping after
                *stop_timeout* seconds (nothing was started).
        """
        process = self.get_process(name)
        if process.state not in (
            ProcessState.STOPPED,
            ProcessState.CREATED,
            ProcessState.FAILED,
        ):
            await process.stop()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + stop_timeout
        while process.state == ProcessState.STOPPING:
            if loop.time() >= deadline:
                raise RuntimeError(
                    f"Process {name!r} is still stopping after {stop_timeout:g}s; "
                    "restart it once it has stopped"
                )
            await asyncio.sleep(0.05)
        await process.start()

    async def start_all(self) -> None:
        """Start all registered processes that are not running.

        ``CREATED``, ``STOPPED`` and ``FAILED`` processes are started (a
        failed one is torn down first, see :meth:`AgentProcess.start`).  A
        process that fails to start is logged and skipped.  With
        ``distributed.enabled`` the cluster node starts first
        (:meth:`start_distributed`); its failure raises.
        """
        await self.start_distributed()
        for name, process in list(self._processes.items()):
            if process.state in (
                ProcessState.CREATED,
                ProcessState.STOPPED,
                ProcessState.FAILED,
            ):
                try:
                    await process.start()
                except Exception as exc:
                    logger.error("AgentRuntime: failed to start %s: %s", name, exc)

    async def stop_all(self, *, drain_timeout: float | None = 0.0) -> None:
        """Stop all processes that are not stopped (``FAILED`` ones too).

        Afterwards the shared event notifier (if any) is drained, so every
        ``process.stopped`` event is delivered before this returns.

        Args:
            drain_timeout: Passed to :meth:`AgentProcess.stop`: ``0``
                (default) cancels runs in flight at once, a positive value
                lets them finish for up to that many seconds, ``None``
                uses each process's ``config.drain_timeout``.  A graceful
                stop stops the processes concurrently, so the wait is not
                multiplied by the number of processes.
        """
        targets = [
            (name, process)
            for name, process in list(self._processes.items())
            if process.state not in (ProcessState.STOPPED, ProcessState.CREATED)
        ]

        async def _stop(name: str, process: AgentProcess) -> None:
            try:
                await process.stop(drain_timeout=drain_timeout)
            except Exception as exc:
                logger.error("AgentRuntime: failed to stop %s: %s", name, exc)

        if drain_timeout == 0:
            for name, process in targets:
                await _stop(name, process)
        else:
            await asyncio.gather(*(_stop(name, process) for name, process in targets))
        # The shared notifier outlives single processes; deliver what the
        # processes emitted (process.stopped included) and stop it last.
        if self._event_notifier is not None and hasattr(self._event_notifier, "stop"):
            try:
                await self._event_notifier.stop()
            except Exception as exc:
                logger.error("AgentRuntime: failed to drain event notifier: %s", exc)

    # ------------------------------------------------------------------
    # Status and monitoring
    # ------------------------------------------------------------------

    def status(self) -> dict[str, Any]:
        """Global runtime status.

        Returns:
            Dict with process count and per-process status (plus
            ``cluster`` in distributed mode).
        """
        process_statuses = {}
        for name, process in self._processes.items():
            process_statuses[name] = process.status()

        status: dict[str, Any] = {
            "process_count": len(self._processes),
            "processes": process_statuses,
        }
        if self._cluster is not None:
            status["cluster"] = self._cluster.status()
        return status

    def process_status(self, name: str) -> dict[str, Any]:
        """Status for a single process.

        Args:
            name: Process name.

        Returns:
            Process status dict.

        Raises:
            KeyError: If the process does not exist.
        """
        return self.get_process(name).status()

    def list_processes(self) -> list[dict[str, Any]]:
        """List all processes with summary info.

        Returns:
            One dict per process: ``name``, ``state`` (the
            :class:`ProcessState` value) and ``process_id``.  Use
            :meth:`process_status` for uptime and counters.
        """
        result: list[dict[str, Any]] = []
        for name, process in self._processes.items():
            result.append(
                {
                    "name": name,
                    "state": process.state.value,
                    "process_id": process.process_id,
                }
            )
        return result

    # ------------------------------------------------------------------
    # Context manager
    # ------------------------------------------------------------------

    async def __aenter__(self) -> AgentRuntime:
        return self

    async def __aexit__(self, *exc: object) -> None:
        await self.shutdown()

    async def shutdown(self, *, drain_timeout: float | None = 0.0) -> None:
        """Stop every process (:meth:`stop_all`), then close them all.

        Closing releases what a process keeps across restarts (long-term
        memory, secrets, conversation history).  The processes stay
        registered.  The cluster node (distributed mode) stops last.

        Args:
            drain_timeout: How long runs in flight may finish first (see
                :meth:`stop_all`; default ``0``: cancel them at once).
        """
        await self.stop_all(drain_timeout=drain_timeout)
        for name, process in list(self._processes.items()):
            try:
                await process.close()
            except Exception as exc:
                logger.error("AgentRuntime: failed to close %s: %s", name, exc)
        await self.stop_distributed()

    # ------------------------------------------------------------------
    # Distributed mode
    # ------------------------------------------------------------------

    @property
    def cluster(self) -> Any | None:
        """The running :class:`~promptise.runtime.distributed.node.ClusterNode`
        (its ``transport``, ``coordinator``, ``node_id``, ``url``), or
        ``None`` when distributed mode is off or not started."""
        return self._cluster

    async def start_distributed(self) -> None:
        """Start this node's cluster membership (``distributed.enabled``).

        Starts the management transport on
        ``distributed.transport_host:transport_port``, then the
        coordinator (``coordinator=True``) or the registration with
        ``coordinator_url``.  Does nothing when ``distributed.enabled`` is
        off or it already runs.  :meth:`start_all` calls it.

        Raises:
            ValueError: The cluster token's ``${ENV_VAR}`` is not set.
            OSError: The transport port can't be bound.
        """
        cfg = self._config.distributed
        if not cfg.enabled or self._cluster is not None:
            return
        from .distributed.node import ClusterNode

        node = ClusterNode(self, cfg)
        await node.start()
        self._cluster = node

    async def stop_distributed(self) -> None:
        """Stop the cluster membership (unregister, stop the transport)."""
        node, self._cluster = self._cluster, None
        if node is not None:
            try:
                await node.stop()
            except Exception as exc:
                logger.error("AgentRuntime: failed to stop the cluster node: %s", exc)

    def request_shutdown(self, *, force: bool = False) -> None:
        """Ask :meth:`run_forever` to stop (safe from a signal handler).

        Args:
            force: Also end the graceful wait of a shutdown already in
                progress: runs still in flight are cancelled now (see
                :meth:`AgentProcess.abort_drain`).
        """
        if self._shutdown_event is None:
            self._shutdown_event = asyncio.Event()
        self._shutdown_event.set()
        if force:
            for process in list(self._processes.values()):
                process.abort_drain()

    async def run_forever(
        self,
        *,
        install_signal_handlers: bool = True,
        drain_timeout: float | None = None,
    ) -> None:
        """Start all processes and run until shutdown is requested.

        Returns after :meth:`request_shutdown`, ``SIGTERM`` or ``SIGINT``
        (with *install_signal_handlers*), or when the task running it is
        cancelled; every process is then stopped and closed
        (:meth:`shutdown`), so ``docker stop`` or Ctrl+C never cuts a
        process off without ``process.stopped``, a final checkpoint and
        its dead letters.

        The stop is graceful: no new trigger event starts, and runs in
        flight get up to *drain_timeout* seconds to finish before they
        are cancelled (a cancelled run is journaled, see
        :meth:`AgentProcess.stop`).  A second signal during that wait
        cancels them at once.

        Args:
            install_signal_handlers: Install ``SIGTERM`` / ``SIGINT``
                handlers on the running loop for the duration of the call
                (removed afterwards: SIGTERM goes back to the default,
                SIGINT to ``KeyboardInterrupt``).  Skipped
                where the loop doesn't support them (Windows, or a thread
                other than the main one).
            drain_timeout: Seconds runs in flight may take to finish at
                shutdown.  ``None`` (default) uses each process's
                ``config.drain_timeout`` (5 seconds unless set); ``0``
                cancels them at once.
        """
        if self._shutdown_event is None:
            self._shutdown_event = asyncio.Event()
        loop = asyncio.get_running_loop()
        installed: list[signal.Signals] = []
        if install_signal_handlers:
            for sig in (signal.SIGTERM, signal.SIGINT):
                try:
                    loop.add_signal_handler(sig, self._on_signal, sig)
                    installed.append(sig)
                except (NotImplementedError, RuntimeError, ValueError):
                    logger.debug("AgentRuntime: cannot handle %s on this loop", sig.name)
        try:
            await self.start_all()
            await self._shutdown_event.wait()
        finally:
            # Also when cancelled: a signal from here on is the second one.
            self._shutdown_event.set()
            try:
                # The handlers stay during the shutdown: a second signal
                # cancels the runs still in flight instead of killing the
                # program before they are journaled.
                await self.shutdown(drain_timeout=drain_timeout)
            finally:
                for sig in installed:
                    with contextlib.suppress(Exception):
                        loop.remove_signal_handler(sig)
                self._shutdown_event = None

    def _on_signal(self, sig: signal.Signals) -> None:
        if self._shutdown_event is not None and self._shutdown_event.is_set():
            logger.warning(
                "AgentRuntime: received %s again; cancelling the runs still in flight",
                sig.name,
            )
            self.request_shutdown(force=True)
            # A further signal gets the default action (exit at once).
            loop = asyncio.get_running_loop()
            for other in (signal.SIGTERM, signal.SIGINT):
                with contextlib.suppress(Exception):
                    loop.remove_signal_handler(other)
            return
        logger.info("AgentRuntime: received %s, shutting down", sig.name)
        self.request_shutdown()

    # ------------------------------------------------------------------
    # Config-based initialization
    # ------------------------------------------------------------------

    @classmethod
    async def from_config(
        cls,
        config: RuntimeConfig,
        *,
        event_bus: Any | None = None,
        broker: Any | None = None,
        event_notifier: Any | None = None,
        hooks: Any | None = None,
    ) -> AgentRuntime:
        """Create a runtime from a :class:`RuntimeConfig` with all
        processes pre-registered.

        Equivalent to ``AgentRuntime(config, ...)``, which registers the
        configured processes too (kept for backward compatibility).

        Args:
            config: Runtime configuration.
            event_bus: Optional shared EventBus.
            broker: Optional shared MessageBroker.
            event_notifier: Optional :class:`EventNotifier` for all processes.
            hooks: Optional :class:`HookManager` shared by all processes.

        Returns:
            A new :class:`AgentRuntime` with processes registered.
        """
        return cls(
            config=config,
            event_bus=event_bus,
            broker=broker,
            event_notifier=event_notifier,
            hooks=hooks,
        )

    def __repr__(self) -> str:
        states = {}
        for name, process in self._processes.items():
            state = process.state.value
            states[state] = states.get(state, 0) + 1
        state_str = ", ".join(f"{v} {k}" for k, v in states.items())
        return (
            f"AgentRuntime(processes={len(self._processes)}{', ' + state_str if state_str else ''})"
        )
