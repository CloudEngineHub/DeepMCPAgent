"""File-watch trigger: fires when files change on the filesystem.

Uses ``watchdog`` for native OS filesystem notifications when available,
with a polling fallback when ``watchdog`` is not installed.  Pass
``backend="polling"`` to force polling (network shares, bind mounts) or
``backend="watchdog"`` to require native notifications.

``watchdog`` is included in the base ``pip install promptise``.

Example::

    from promptise.runtime.triggers.file_watch import FileWatchTrigger

    trigger = FileWatchTrigger(
        watch_path="/data/inbox",
        patterns=["*.csv", "*.json"],
    )
    await trigger.start()

    event = await trigger.wait_for_next()
    print(event.payload)
    # {"path": "/data/inbox/new.csv", "filename": "new.csv",
    #  "event_type": "created", "event_types": ["created", "modified"]}

    await trigger.stop()

Debouncing: the operating system often reports one write as several
events (a new file is usually ``created`` then ``modified``).  All events
for the same path inside the ``debounce_seconds`` window are merged into
**one** trigger event whose ``event_type`` describes the net change:

* the file is gone → ``deleted`` (nothing at all if it was also created
  inside the window)
* it was created inside the window → ``created``
* it was moved/renamed into place → ``moved``
* otherwise → ``modified``

The merged ``event_type`` is then checked against ``events`` (the
trigger's ``watch_events``), so ``events=["deleted"]`` never fires for a
new or changed file.  ``event_types`` lists the raw events that were merged.

Back-pressure: events are not dropped when the consumer is busy.  Once
the trigger's queue is full, further changes wait in a per-file backlog
(a second change to a file that is already waiting is merged into its
event) and move to the queue as the consumer takes events.  Only when
``max_pending_events`` different files are waiting is a new change
dropped; drops are logged as errors and counted in
:attr:`FileWatchTrigger.dropped_events`.
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
from pathlib import Path
from typing import Any

from .base import TriggerEvent, drain_queue

#: Event types a :class:`FileWatchTrigger` can report.
FILE_EVENT_TYPES = frozenset({"created", "modified", "deleted", "moved"})

#: Change-detection backends a :class:`FileWatchTrigger` accepts.
FILE_WATCH_BACKENDS = ("auto", "watchdog", "polling")

#: Size of the trigger's event queue (the backlog holds the overflow).
_QUEUE_SIZE = 1000

logger = logging.getLogger(__name__)

# Try to import watchdog; fall back to polling if unavailable
try:
    from watchdog.events import FileSystemEvent, FileSystemEventHandler
    from watchdog.observers import Observer

    HAS_WATCHDOG = True
except ImportError:
    HAS_WATCHDOG = False


class FileWatchTrigger:
    """Filesystem watch trigger.

    Monitors a directory for file changes and produces
    :class:`TriggerEvent` objects.

    Args:
        watch_path: Directory to monitor.
        patterns: Glob patterns to match (e.g. ``["*.csv", "*.json"]``).
        events: Event types to react to (``created``, ``modified``,
            ``deleted``, ``moved``).  Defaults to created + modified.
        recursive: Watch subdirectories recursively.
        debounce_seconds: Window in which all events for one path are
            merged into a single trigger event.
        poll_interval: Seconds between directory scans with the polling
            backend.
        backend: ``"auto"`` (watchdog when installed, else polling),
            ``"watchdog"`` or ``"polling"``.
        max_pending_events: Most files that can wait for the consumer
            (queued plus backlog) before new changes are dropped.

    Raises:
        ValueError: An unknown event type or backend, ``backend="watchdog"``
            without watchdog installed, or an out-of-range number.
    """

    def __init__(
        self,
        watch_path: str,
        patterns: list[str] | None = None,
        events: list[str] | None = None,
        recursive: bool = True,
        debounce_seconds: float = 0.5,
        poll_interval: float = 1.0,
        *,
        backend: str = "auto",
        max_pending_events: int = 10_000,
    ) -> None:
        self._watch_path = Path(watch_path)
        self._patterns = patterns or ["*"]
        self._events = set(events or ["created", "modified"])
        unknown = self._events - FILE_EVENT_TYPES
        if unknown:
            raise ValueError(
                f"Unknown file watch events {sorted(unknown)}; choose from {sorted(FILE_EVENT_TYPES)}"
            )
        if backend not in FILE_WATCH_BACKENDS:
            raise ValueError(
                f"Unknown file watch backend {backend!r}; choose from {list(FILE_WATCH_BACKENDS)}"
            )
        if backend == "watchdog" and not HAS_WATCHDOG:
            raise ValueError(
                "backend='watchdog' needs the 'watchdog' package (pip install watchdog); "
                "use 'auto' or 'polling' instead"
            )
        if poll_interval <= 0:
            raise ValueError(f"poll_interval must be positive, got {poll_interval}")
        if debounce_seconds < 0:
            raise ValueError(f"debounce_seconds must be 0 or more, got {debounce_seconds}")
        if max_pending_events < 1:
            raise ValueError(f"max_pending_events must be at least 1, got {max_pending_events}")
        self._recursive = recursive
        self._debounce_seconds = debounce_seconds
        self._poll_interval = poll_interval
        self._backend = backend
        self._max_pending_events = max_pending_events

        self.trigger_id: str = f"file_watch-{watch_path}"
        self._queue: asyncio.Queue[TriggerEvent] = asyncio.Queue(maxsize=_QUEUE_SIZE)
        # Changes whose debounce window closed while the queue was full:
        # path -> raw event types, in arrival order (see _drain_backlog).
        self._backlog: dict[str, list[str]] = {}
        # Waiting changes turned into events by stop() (see drain_pending).
        self._leftover: list[TriggerEvent] = []
        self._dropped_events = 0
        self._stopped = False
        self._stop_event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

        # Watchdog components
        self._observer: Any | None = None

        # Polling fallback state
        self._poll_task: asyncio.Task[None] | None = None
        self._file_mtimes: dict[str, tuple[int, int]] = {}
        self._known_files: set[str] = set()

        # Debounce: raw event types per path, flushed after the window
        self._pending: dict[str, list[str]] = {}
        self._pending_handles: dict[str, asyncio.TimerHandle] = {}

    async def start(self) -> None:
        """Start watching for file changes."""
        self._stopped = False
        self._stop_event.clear()
        self._loop = asyncio.get_running_loop()

        # Ensure watch path exists
        if not self._watch_path.exists():
            self._watch_path.mkdir(parents=True, exist_ok=True)
            logger.info("Created watch directory: %s", self._watch_path)

        if self.backend == "watchdog":
            await self._start_watchdog()
        else:
            if self._backend == "auto":
                logger.info(
                    "watchdog not installed, using polling fallback (interval=%.1fs)",
                    self._poll_interval,
                )
            await self._start_polling()

        logger.info(
            "FileWatchTrigger started on %s (patterns=%s, events=%s, backend=%s, recursive=%s)",
            self._watch_path,
            self._patterns,
            self._events,
            self.backend,
            self._recursive,
        )

    @property
    def backend(self) -> str:
        """The backend in use: ``"watchdog"`` or ``"polling"``."""
        if self._backend == "auto":
            return "watchdog" if HAS_WATCHDOG else "polling"
        return self._backend

    @property
    def dropped_events(self) -> int:
        """Changes dropped because ``max_pending_events`` files were waiting."""
        return self._dropped_events

    @property
    def pending_events(self) -> int:
        """Events waiting for the consumer (queued plus backlog)."""
        return self._queue.qsize() + len(self._backlog)

    async def stop(self) -> None:
        """Stop watching for file changes."""
        self._stopped = True
        self._stop_event.set()

        if self._observer is not None:
            self._observer.stop()
            self._observer.join(timeout=5)
            self._observer = None

        if self._poll_task is not None:
            self._poll_task.cancel()
            try:
                await self._poll_task
            except asyncio.CancelledError:
                pass
            self._poll_task = None

        for handle in self._pending_handles.values():
            handle.cancel()
        self._pending_handles.clear()
        # Changes still waiting (backlog first, then those in their
        # debounce window) become events for drain_pending().
        waiting = {path: list(types) for path, types in self._backlog.items()}
        for path, types in self._pending.items():
            merged = waiting.setdefault(path, [])
            merged.extend(t for t in types if t not in merged)
        for path, types in waiting.items():
            event = self._build_event(path, types)
            if event is not None:
                self._leftover.append(event)
        self._pending.clear()
        self._backlog.clear()

        # Unblock waiters
        sentinel = TriggerEvent(
            trigger_id=self.trigger_id,
            trigger_type="file_watch",
            payload=None,
            metadata={"_stop": True},
        )
        try:
            self._queue.put_nowait(sentinel)
        except asyncio.QueueFull:
            pass

        logger.info("FileWatchTrigger stopped")

    def drain_pending(self) -> list[TriggerEvent]:
        """Take the changes nobody consumed (after :meth:`stop`): the queued
        events, then the backlog and the changes still in their debounce
        window."""
        events = drain_queue(self._queue) + self._leftover
        self._leftover = []
        return events

    async def wait_for_next(self) -> TriggerEvent:
        """Wait for the next file change event.

        Returns:
            A :class:`TriggerEvent` with file change details.

        Raises:
            asyncio.CancelledError: If the wait is cancelled.
        """
        while True:
            event = await self._queue.get()
            # Space was freed: move waiting changes into the queue.
            self._drain_backlog()
            if event.metadata and event.metadata.get("_stop"):
                if self._stopped:
                    raise asyncio.CancelledError("File watch trigger stopped")
                continue
            return event

    def _matches_pattern(self, filename: str) -> bool:
        """Check if a filename matches any of the configured patterns."""
        return any(fnmatch.fnmatch(filename, p) for p in self._patterns)

    def _emit_event(self, file_path: str, event_type: str) -> None:
        """Record a raw filesystem event (safe to call from any thread).

        The event is merged with others for the same path and turned into
        a trigger event once the debounce window closes.
        """
        file_path = str(file_path)
        if not self._matches_pattern(os.path.basename(file_path)):
            return
        loop = self._loop
        if loop is not None and loop.is_running():
            loop.call_soon_threadsafe(self._record, file_path, event_type)
        else:
            self._record(file_path, event_type)

    def _record(self, file_path: str, event_type: str) -> None:
        """Add a raw event to the path's debounce window (event-loop thread)."""
        if self._stopped:
            return
        types = self._pending.get(file_path)
        if types is not None:
            if event_type not in types:
                types.append(event_type)
            return
        self._pending[file_path] = [event_type]
        if self._loop is None:
            self._flush(file_path)
            return
        self._pending_handles[file_path] = self._loop.call_later(
            self._debounce_seconds, self._flush, file_path
        )

    @staticmethod
    def _net_event_type(types: list[str], exists: bool) -> str | None:
        """Collapse the raw events seen in one window into the net change."""
        if not exists:
            # Created and removed inside one window: nothing to report.
            return None if "created" in types else "deleted"
        if "created" in types or "deleted" in types:
            return "created"
        if "moved" in types:
            return "moved"
        return "modified"

    def _flush(self, file_path: str) -> None:
        """Close the debounce window for *file_path* and enqueue one event.

        When the queue is full the change waits in the backlog instead
        (merged with a change to the same file that is already waiting).
        """
        self._pending_handles.pop(file_path, None)
        types = self._pending.pop(file_path, None)
        if not types or self._stopped:
            return
        waiting = self._backlog.get(file_path)
        if waiting is not None:
            waiting.extend(t for t in types if t not in waiting)
            return
        if self._backlog or self._queue.full():
            if self.pending_events >= self._max_pending_events:
                self._drop(file_path)
                return
            self._backlog[file_path] = list(types)
            return
        if self._queue.qsize() >= self._max_pending_events:
            self._drop(file_path)
            return
        event = self._build_event(file_path, types)
        if event is not None:
            self._queue.put_nowait(event)

    def _drain_backlog(self) -> None:
        """Move waiting changes into the queue while it has room (oldest first)."""
        while self._backlog and not self._queue.full():
            file_path = next(iter(self._backlog))
            types = self._backlog.pop(file_path)
            event = self._build_event(file_path, types)
            if event is not None:
                self._queue.put_nowait(event)

    def _drop(self, file_path: str) -> None:
        self._dropped_events += 1
        if self._dropped_events == 1 or self._dropped_events % 100 == 0:
            logger.error(
                "FileWatchTrigger %s: %d files are waiting for the process "
                "(max_pending_events=%d); dropped the change to %s "
                "(%d dropped so far). Raise watch_max_pending_events or "
                "process events faster.",
                self.trigger_id,
                self.pending_events,
                self._max_pending_events,
                file_path,
                self._dropped_events,
            )

    def _build_event(self, file_path: str, types: list[str]) -> TriggerEvent | None:
        """The trigger event for the merged *types*, or ``None`` if filtered out."""
        event_type = self._net_event_type(types, os.path.exists(file_path))
        if event_type is None or event_type not in self._events:
            return None

        filename = os.path.basename(file_path)
        return TriggerEvent(
            trigger_id=self.trigger_id,
            trigger_type="file_watch",
            payload={
                "path": file_path,
                "filename": filename,
                "event_type": event_type,
                "event_types": types,
            },
            metadata={
                "watch_path": str(self._watch_path),
                "patterns": self._patterns,
            },
        )

    # ------------------------------------------------------------------
    # Watchdog backend
    # ------------------------------------------------------------------

    async def _start_watchdog(self) -> None:
        """Start watchdog observer."""

        class _Handler(FileSystemEventHandler):  # type: ignore[misc]
            def __init__(self, trigger: FileWatchTrigger) -> None:
                self._trigger = trigger

            def on_created(self, event: FileSystemEvent) -> None:
                if not event.is_directory:
                    self._trigger._emit_event(event.src_path, "created")

            def on_modified(self, event: FileSystemEvent) -> None:
                if not event.is_directory:
                    self._trigger._emit_event(event.src_path, "modified")

            def on_deleted(self, event: FileSystemEvent) -> None:
                if not event.is_directory:
                    self._trigger._emit_event(event.src_path, "deleted")

            def on_moved(self, event: FileSystemEvent) -> None:
                if not event.is_directory:
                    self._trigger._emit_event(getattr(event, "dest_path", event.src_path), "moved")

        handler = _Handler(self)
        self._observer = Observer()
        self._observer.schedule(
            handler,
            str(self._watch_path),
            recursive=self._recursive,
        )
        self._observer.start()

    # ------------------------------------------------------------------
    # Polling fallback
    # ------------------------------------------------------------------

    async def _start_polling(self) -> None:
        """Start polling-based file watching."""
        # Snapshot current state
        self._file_mtimes = self._scan_files()
        self._known_files = set(self._file_mtimes.keys())

        self._poll_task = asyncio.create_task(
            self._poll_loop(),
            name=f"file-watch-poll-{self._watch_path}",
        )

    def _scan_files(self) -> dict[str, tuple[int, int]]:
        """Scan the watched directory and return ``{path: (mtime_ns, size)}``.

        The size is compared too, so a rewrite within the file system's
        timestamp resolution is still seen as a change.
        """
        result: dict[str, tuple[int, int]] = {}
        try:
            if self._recursive:
                for root, _dirs, files in os.walk(self._watch_path):
                    for f in files:
                        fp = os.path.join(root, f)
                        try:
                            st = os.stat(fp)
                            result[fp] = (st.st_mtime_ns, st.st_size)
                        except OSError:
                            pass
            else:
                for item in self._watch_path.iterdir():
                    try:
                        if item.is_file():
                            st = item.stat()
                            result[str(item)] = (st.st_mtime_ns, st.st_size)
                    except OSError:
                        pass
        except OSError:
            pass
        return result

    async def _poll_loop(self) -> None:
        """Background polling loop."""
        try:
            while not self._stopped:
                try:
                    await asyncio.wait_for(
                        self._stop_event.wait(),
                        timeout=self._poll_interval,
                    )
                    # If stop_event was set, exit
                    break
                except asyncio.TimeoutError:
                    pass

                new_mtimes = self._scan_files()
                new_files = set(new_mtimes.keys())

                # Detect created files
                for fp in new_files - self._known_files:
                    self._emit_event(fp, "created")

                # Detect deleted files
                for fp in self._known_files - new_files:
                    self._emit_event(fp, "deleted")

                # Detect modified files
                for fp in new_files & self._known_files:
                    if new_mtimes[fp] != self._file_mtimes.get(fp):
                        self._emit_event(fp, "modified")

                self._file_mtimes = new_mtimes
                self._known_files = new_files

        except asyncio.CancelledError:
            return

    def __repr__(self) -> str:
        return (
            f"FileWatchTrigger(path={str(self._watch_path)!r}, "
            f"patterns={self._patterns}, "
            f"events={sorted(self._events)}, "
            f"backend={self.backend!r}, "
            f"recursive={self._recursive})"
        )
