"""File-watch trigger: fires when files change on the filesystem.

Uses ``watchdog`` for native OS filesystem notifications when available,
with a polling fallback when ``watchdog`` is not installed.

Requires ``watchdog`` for optimal performance
(install via ``pip install promptise[runtime]``).

Example::

    from promptise.runtime.triggers.file_watch import FileWatchTrigger

    trigger = FileWatchTrigger(
        watch_path="/data/inbox",
        patterns=["*.csv", "*.json"],
    )
    await trigger.start()

    event = await trigger.wait_for_next()
    print(event.payload)  # {"path": "/data/inbox/new.csv", "event_type": "created"}

    await trigger.stop()
"""

from __future__ import annotations

import asyncio
import fnmatch
import logging
import os
import time
from pathlib import Path
from typing import Any

from .base import TriggerEvent

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
        events: Event types to react to.
        recursive: Watch subdirectories recursively.
        debounce_seconds: Debounce interval to avoid duplicate events.
        poll_interval: Polling interval in seconds (used when watchdog
            is not available).
    """

    def __init__(
        self,
        watch_path: str,
        patterns: list[str] | None = None,
        events: list[str] | None = None,
        recursive: bool = True,
        debounce_seconds: float = 0.5,
        poll_interval: float = 1.0,
    ) -> None:
        self._watch_path = Path(watch_path)
        self._patterns = patterns or ["*"]
        self._events = set(events or ["created", "modified"])
        self._recursive = recursive
        self._debounce_seconds = debounce_seconds
        self._poll_interval = poll_interval

        self.trigger_id: str = f"file_watch-{watch_path}"
        self._queue: asyncio.Queue[TriggerEvent] = asyncio.Queue(maxsize=1000)
        self._stopped = False
        self._stop_event = asyncio.Event()
        self._loop: asyncio.AbstractEventLoop | None = None

        # Watchdog components
        self._observer: Any | None = None

        # Polling fallback state
        self._poll_task: asyncio.Task[None] | None = None
        self._file_mtimes: dict[str, float] = {}
        self._known_files: set[str] = set()

        # Deduplication
        self._recent_events: dict[str, float] = {}

    async def start(self) -> None:
        """Start watching for file changes."""
        self._stopped = False
        self._stop_event.clear()
        self._loop = asyncio.get_event_loop()

        # Ensure watch path exists
        if not self._watch_path.exists():
            self._watch_path.mkdir(parents=True, exist_ok=True)
            logger.info("Created watch directory: %s", self._watch_path)

        if HAS_WATCHDOG:
            await self._start_watchdog()
        else:
            logger.info(
                "watchdog not installed, using polling fallback (interval=%.1fs)",
                self._poll_interval,
            )
            await self._start_polling()

        logger.info(
            "FileWatchTrigger started on %s (patterns=%s, events=%s)",
            self._watch_path,
            self._patterns,
            self._events,
        )

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

    async def wait_for_next(self) -> TriggerEvent:
        """Wait for the next file change event.

        Returns:
            A :class:`TriggerEvent` with file change details.

        Raises:
            asyncio.CancelledError: If the wait is cancelled.
        """
        while True:
            event = await self._queue.get()
            if event.metadata and event.metadata.get("_stop"):
                if self._stopped:
                    raise asyncio.CancelledError("File watch trigger stopped")
                continue
            return event

    def _matches_pattern(self, filename: str) -> bool:
        """Check if a filename matches any of the configured patterns."""
        return any(fnmatch.fnmatch(filename, p) for p in self._patterns)

    def _is_duplicate(self, file_path: str, event_type: str) -> bool:
        """Debounce: check if this event was recently emitted."""
        key = f"{event_type}:{file_path}"
        now = time.monotonic()

        last = self._recent_events.get(key)
        if last is not None and (now - last) < self._debounce_seconds:
            return True

        self._recent_events[key] = now

        # Garbage-collect old entries
        cutoff = now - self._debounce_seconds * 10
        self._recent_events = {k: v for k, v in self._recent_events.items() if v > cutoff}
        return False

    def _emit_event(self, file_path: str, event_type: str) -> None:
        """Create and enqueue a TriggerEvent."""
        if event_type not in self._events:
            return

        filename = os.path.basename(file_path)
        if not self._matches_pattern(filename):
            return

        if self._is_duplicate(file_path, event_type):
            return

        trigger_event = TriggerEvent(
            trigger_id=self.trigger_id,
            trigger_type="file_watch",
            payload={
                "path": str(file_path),
                "filename": filename,
                "event_type": event_type,
            },
            metadata={
                "watch_path": str(self._watch_path),
                "patterns": self._patterns,
            },
        )

        try:
            if self._loop and self._loop.is_running():
                self._loop.call_soon_threadsafe(self._queue.put_nowait, trigger_event)
            else:
                self._queue.put_nowait(trigger_event)
        except asyncio.QueueFull:
            logger.warning("FileWatchTrigger: queue full, dropping event")

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

    def _scan_files(self) -> dict[str, float]:
        """Scan the watched directory and return {path: mtime}."""
        result: dict[str, float] = {}
        try:
            if self._recursive:
                for root, _dirs, files in os.walk(self._watch_path):
                    for f in files:
                        fp = os.path.join(root, f)
                        try:
                            result[fp] = os.path.getmtime(fp)
                        except OSError:
                            pass
            else:
                for item in self._watch_path.iterdir():
                    if item.is_file():
                        try:
                            result[str(item)] = item.stat().st_mtime
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
                    if new_mtimes[fp] != self._file_mtimes.get(fp, 0):
                        self._emit_event(fp, "modified")

                self._file_mtimes = new_mtimes
                self._known_files = new_files

        except asyncio.CancelledError:
            return

    def __repr__(self) -> str:
        backend = "watchdog" if HAS_WATCHDOG else "polling"
        return (
            f"FileWatchTrigger(path={str(self._watch_path)!r}, "
            f"patterns={self._patterns}, "
            f"backend={backend!r})"
        )
