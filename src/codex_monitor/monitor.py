import os
import time
from dataclasses import dataclass
from pathlib import Path
from queue import Empty, SimpleQueue
from typing import final, override

import anyio
from anyio.to_thread import run_sync
from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from codex_monitor.config import AppConfig, Project
from codex_monitor.diagnostic_events import (
    DiagnosticEmitter,
    DiagnosticEvent,
    safe_exception_summary,
)
from codex_monitor.discovery import SessionDiscovery
from codex_monitor.events import parse_event_with_issue
from codex_monitor.models import Session
from codex_monitor.parser import apply_event, parse_file
from codex_monitor.store import ChangeKind, SessionStore
from codex_monitor.tailer import read_complete_lines

INVALID_PROJECT_ROOT_MESSAGE = "project_root must be an existing directory"
DATA_STALE_AFTER_SECONDS = 90


@dataclass(frozen=True, slots=True)
class MonitorRuntimeHealth:
    known_file_count: int
    last_reconciliation_at: float | None
    reconciliation_count: int
    coalesced_event_count: int


@final
class SessionFileHandler(FileSystemEventHandler):
    __slots__ = ("_paths",)

    _paths: SimpleQueue[Path]

    def __init__(self, paths: SimpleQueue[Path]) -> None:
        super().__init__()
        self._paths = paths

    @override
    def on_created(self, event: FileSystemEvent) -> None:
        self._enqueue(event)

    @override
    def on_modified(self, event: FileSystemEvent) -> None:
        self._enqueue(event)

    def _enqueue(self, event: FileSystemEvent) -> None:
        path = Path(os.fsdecode(event.src_path))
        if not event.is_directory and path.suffix == ".jsonl":
            self._paths.put(path)


@final
class Monitor:
    __slots__ = (
        "_activity_alert_seconds",
        "_coalesced_event_count",
        "_config",
        "_diagnostic_emitter",
        "_discovery",
        "_initial_sync_complete",
        "_known_paths",
        "_last_reconciliation_at",
        "_project_lock",
        "_reconciliation_count",
        "_store",
    )

    _config: AppConfig
    _diagnostic_emitter: DiagnosticEmitter | None
    _coalesced_event_count: int
    _discovery: SessionDiscovery
    _known_paths: set[Path]
    _last_reconciliation_at: float | None
    _project_lock: anyio.Lock
    _reconciliation_count: int
    _store: SessionStore

    def __init__(
        self,
        config: AppConfig,
        store: SessionStore,
        diagnostic_emitter: DiagnosticEmitter | None = None,
        activity_alert_seconds: int | None = None,
    ) -> None:
        self._config = config
        self._diagnostic_emitter = diagnostic_emitter
        self._activity_alert_seconds = (
            config.stuck_seconds if activity_alert_seconds is None else activity_alert_seconds
        )
        self._discovery = SessionDiscovery(
            tuple(project for project in config.projects if project.root.is_dir()),
            config.session_root,
        )
        self._project_lock = anyio.Lock()
        self._store = store
        self._known_paths = set()
        self._initial_sync_complete = False
        self._last_reconciliation_at = None
        self._reconciliation_count = 0
        self._coalesced_event_count = 0

    def runtime_health(self) -> MonitorRuntimeHealth:
        return MonitorRuntimeHealth(
            known_file_count=len(self._known_paths),
            last_reconciliation_at=self._last_reconciliation_at,
            reconciliation_count=self._reconciliation_count,
            coalesced_event_count=self._coalesced_event_count,
        )

    @property
    def activity_alert_seconds(self) -> int | None:
        return self._activity_alert_seconds

    def set_activity_alert_seconds(self, seconds: int | None) -> None:
        self._activity_alert_seconds = seconds

    def data_is_fresh(self, now: float) -> bool:
        if not self._initial_sync_complete or self._last_reconciliation_at is None:
            return True
        return now - self._last_reconciliation_at <= DATA_STALE_AFTER_SECONDS

    async def projects(self) -> tuple[Project, ...]:
        async with self._project_lock:
            return self._discovery.projects

    async def add_project(self, root: Path) -> tuple[Project, bool]:
        if not await run_sync(_is_directory, root):
            raise ValueError(INVALID_PROJECT_ROOT_MESSAGE)
        async with self._project_lock:
            project, added = await run_sync(self._discovery.add_project, root)
            if added:
                await self._reconcile_locked(force_rebuild=True)
            return project, added

    async def add_projects(
        self, roots: tuple[Path, ...]
    ) -> tuple[tuple[tuple[Project, bool], ...], tuple[tuple[Path, str], ...]]:
        """Add a selected batch and rebuild the session index only once."""
        valid_roots: list[Path] = []
        errors: list[tuple[Path, str]] = []
        for root in roots:
            if await run_sync(_is_directory, root):
                valid_roots.append(root)
            else:
                errors.append((root, INVALID_PROJECT_ROOT_MESSAGE))

        results: list[tuple[Project, bool]] = []
        async with self._project_lock:
            for root in valid_roots:
                results.append(await run_sync(self._discovery.add_project, root))  # noqa: PERF401
            if any(added for _, added in results):
                await self._reconcile_locked(force_rebuild=True)
        return tuple(results), tuple(errors)

    async def remove_project(self, project_key: str) -> Project | None:
        async with self._project_lock:
            project = await run_sync(self._discovery.remove_project, project_key)
            if project is not None:
                await self._reconcile_locked(force_rebuild=True)
            return project

    async def process_path(self, path: Path) -> ChangeKind | None:  # noqa: C901
        started = time.perf_counter()
        path = _normalize_path(path)
        file_size = await run_sync(_file_size, path)
        if file_size is None:
            return None
        project = await run_sync(self._discovery.project_for, path)
        if project is None:
            return None
        self._known_paths.add(path)
        existing = self._store.get_by_path(path)
        if existing is None or file_size < existing.byte_offset:
            session = await run_sync(parse_file, path, project.key, project.root)
            if session is None:
                self._emit("warn", "parser", "jsonl_parse_failed", error_summary="parse_failed")
                return None
            self._emit_parse_anomalies(session)
            change = await self._store.put(session)
            self._emit("debug", "store", "store_update", self._elapsed_ms(started))
            return change

        batch = await run_sync(read_complete_lines, path, existing.byte_offset)
        changed = False
        for raw_line in batch.lines:
            event, issue = parse_event_with_issue(raw_line)
            if event is not None:
                changed = apply_event(existing, event) or changed
            elif issue == "unknown":
                existing.unknown_event_count += 1
                changed = True
                self._emit("warn", "parser", "jsonl_unknown_event", error_summary="unknown_event")
            elif issue == "malformed":
                existing.malformed_line_count += 1
                changed = True
                self._emit("warn", "parser", "jsonl_line_malformed", error_summary="malformed_line")
        if batch.skipped_oversized:
            existing.oversized_line_count += batch.skipped_oversized
            changed = True
            self._emit("warn", "parser", "jsonl_line_oversized", error_summary="oversized_line")
        existing.byte_offset = batch.offset
        if not changed:
            return None
        result = await self._store.put(existing)
        self._emit("debug", "store", "store_update", self._elapsed_ms(started))
        return result

    async def _initial_load(self) -> None:
        async with self._project_lock:
            paths = {_normalize_path(path) for path in await run_sync(self._discovery.discover)}
            self._known_paths = paths
            for path in sorted(paths, key=str):
                _ = await self.process_path(path)
            self._record_reconciliation()
            self._initial_sync_complete = True

    async def _reconcile(self, *, force_rebuild: bool = False) -> None:
        async with self._project_lock:
            await self._reconcile_locked(force_rebuild=force_rebuild)

    async def _reconcile_locked(self, *, force_rebuild: bool = False) -> None:
        paths = {_normalize_path(path) for path in await run_sync(self._discovery.discover)}
        if not force_rebuild and paths == self._known_paths:
            await self._poll_known_files()
            self._record_reconciliation()
            return

        sessions: list[Session] = []
        for path in sorted(paths, key=str):
            project = await run_sync(self._discovery.project_for, path)
            if project is not None:
                session = await run_sync(parse_file, path, project.key, project.root)
                if session is not None:
                    self._emit_parse_anomalies(session)
                    sessions.append(session)
        self._known_paths = paths
        await self._store.replace(sessions)
        self._emit("debug", "store", "store_full_sync")

        self._record_reconciliation()

    def _record_reconciliation(self) -> None:
        self._last_reconciliation_at = time.time()
        self._reconciliation_count += 1

    async def run(self) -> None:
        await self._initial_load()

        paths: SimpleQueue[Path] = SimpleQueue()
        handler = SessionFileHandler(paths)
        observer = Observer()
        try:
            _ = observer.schedule(handler, str(self._config.session_root), recursive=True)
            observer.start()
        except Exception as error:
            self._emit(
                "error",
                "watcher",
                "watcher_start_failed",
                error_summary=safe_exception_summary(error),
            )
            raise
        next_poll = anyio.current_time() + 5
        next_reconciliation = anyio.current_time() + 60
        try:
            while True:
                await self._drain(paths)
                current_time = anyio.current_time()
                if current_time >= next_reconciliation:
                    await self._reconcile()
                    next_reconciliation = current_time + 60
                    next_poll = current_time + 5
                elif current_time >= next_poll:
                    await self._poll_known_files()
                    next_poll = current_time + 5
                _ = await self._store.refresh_stuck(
                    now=time.time(),
                    stuck_seconds=self._activity_alert_seconds,
                    data_fresh=self.data_is_fresh(time.time()),
                )
                await anyio.sleep(0.2)
        finally:
            try:
                observer.stop()
                with anyio.CancelScope(shield=True):
                    await run_sync(observer.join, 5)
                    if observer.is_alive():
                        await run_sync(observer.join)
            except Exception as error:  # noqa: BLE001 - watcher shutdown must not mask cleanup
                self._emit(
                    "warn",
                    "watcher",
                    "watcher_stop_failed",
                    error_summary=safe_exception_summary(error),
                )

    async def _drain(self, paths: SimpleQueue[Path]) -> None:
        received = 0
        unique_paths: set[Path] = set()
        while True:
            try:
                path = paths.get_nowait()
            except Empty:
                break
            received += 1
            unique_paths.add(_normalize_path(path))
        self._coalesced_event_count += received - len(unique_paths)
        for path in sorted(unique_paths, key=str):
            try:
                _ = await self.process_path(path)
            except Exception as error:  # noqa: BLE001 - isolate one bad file from the watcher
                self._emit(
                    "error",
                    "watcher",
                    "jsonl_process_failed",
                    error_summary=safe_exception_summary(error),
                )

    async def _poll_known_files(self) -> None:
        for path in tuple(self._known_paths):
            session = self._store.get_by_path(path)
            file_size = await run_sync(_file_size, path)
            if file_size is not None and (session is None or file_size != session.byte_offset):
                try:
                    _ = await self.process_path(path)
                except Exception as error:  # noqa: BLE001 - isolate one bad file from polling
                    self._emit(
                        "error",
                        "watcher",
                        "jsonl_poll_failed",
                        error_summary=safe_exception_summary(error),
                    )

    def _emit(
        self,
        level: str,
        component: str,
        event_code: str,
        duration_ms: int | None = None,
        error_summary: str | None = None,
    ) -> None:
        if self._diagnostic_emitter is not None:
            self._diagnostic_emitter.emit(
                DiagnosticEvent(
                    level=level,
                    component=component,
                    event_code=event_code,
                    duration_ms=duration_ms,
                    error_summary=error_summary,
                )
            )

    def _emit_parse_anomalies(self, session: Session) -> None:
        if session.unknown_event_count:
            self._emit("warn", "parser", "jsonl_unknown_event", error_summary="unknown_event")
        if session.malformed_line_count:
            self._emit("warn", "parser", "jsonl_line_malformed", error_summary="malformed_line")
        if session.oversized_line_count:
            self._emit("warn", "parser", "jsonl_line_oversized", error_summary="oversized_line")

    @staticmethod
    def _elapsed_ms(started: float) -> int:
        return min(86_400_000, max(0, round((time.perf_counter() - started) * 1000)))


def _file_size(path: Path) -> int | None:
    try:
        return path.stat().st_size if path.is_file() else None
    except OSError:
        return None


def _normalize_path(path: Path) -> Path:
    return Path(os.path.normcase(str(path.resolve())))


def _is_directory(path: Path) -> bool:
    return path.is_dir()
