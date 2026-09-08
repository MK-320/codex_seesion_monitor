from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from collections import deque
from pathlib import Path
from threading import RLock
from typing import TYPE_CHECKING, cast, final

from codex_monitor.models import (
    Session,
    SessionId,
    TraceEvent,
    TraceParseState,
    TraceStatus,
    TurnId,
)

EVENT_MESSAGE_TRACE_KINDS = frozenset(
    {"task_started", "user_message", "agent_message", "task_complete", "turn_aborted"}
)
DISCOVERY_TRACE_KINDS = frozenset(
    {
        "tool_search",
        "tool_search_result",
        "web_search",
        "web_search_result",
        "available_tools",
        "tool_list",
    }
)

if TYPE_CHECKING:
    from collections.abc import Iterator


@final
class TraceStore:
    def __init__(
        self,
        root: Path,
        *,
        enabled: bool = True,
        retention_days: int | None = None,
    ) -> None:
        self.root: Path = root.expanduser().resolve()
        self.enabled: bool = enabled
        self.retention_days: int | None = retention_days
        self._index_path: Path = self.root / "index.json"
        self._db_path: Path = self.root / "traces.sqlite3"
        self._fts_available = True
        self._index: dict[str, object] = self._read_index()
        self._lock = RLock()
        self._backfill_state = "not_started"
        self._backfill_total = 0
        self._backfill_processed = 0
        self._backfill_error: str | None = None

    def _path_for(self, session_key: str) -> Path:
        digest = hashlib.sha256(session_key.encode("utf-8")).hexdigest()
        return self.root / f"{digest}.jsonl"

    def save_session(self, session: Session) -> None:
        with self._lock:
            self._save_session_unlocked(session)

    def _save_session_unlocked(self, session: Session) -> None:  # noqa: C901
        if not self.enabled:
            return
        _ = self.root.mkdir(parents=True, exist_ok=True)
        destination = self._path_for(session.session_key)
        entry = self._index.get(session.session_key)
        typed_entry = cast("dict[str, object]", entry) if isinstance(entry, dict) else {}
        content_hash = _session_content_hash(session)
        existing = _as_int(typed_entry.get("event_count", 0)) if destination.is_file() else 0
        if destination.is_file():
            indexed_bytes = _as_int(typed_entry.get("file_bytes", -1))
            actual_bytes = destination.stat().st_size
            if indexed_bytes >= 0 and indexed_bytes != actual_bytes:
                actual_count, actual_hash = _file_content_stats(destination)
                if actual_count == len(session.trace_events) and actual_hash == content_hash:
                    self._write_index(session)
                    return
                existing = actual_count
        if destination.is_file() and existing == 0:
            try:
                with destination.open("r", encoding="utf-8") as handle:
                    existing = sum(1 for _ in handle)
            except OSError:
                existing = 0
        if (
            existing == len(session.trace_events)
            and existing > 0
            and typed_entry.get("content_hash") == content_hash
        ):
            self._write_index(session)
            return
        if 0 < existing < len(session.trace_events):
            with destination.open("a", encoding="utf-8", newline="\n") as handle:
                for event in session.trace_events[existing:]:
                    _ = handle.write(
                        json.dumps(_event_payload(event), ensure_ascii=False, default=str)
                    )
                    _ = handle.write("\n")
            self._write_index(session)
            return
        temporary = destination.with_suffix(".jsonl.tmp")
        with temporary.open("w", encoding="utf-8", newline="\n") as handle:
            for event in session.trace_events:
                _ = handle.write(json.dumps(_event_payload(event), ensure_ascii=False, default=str))
                _ = handle.write("\n")
        _ = temporary.replace(destination)
        self._write_index(session)

    def _write_index(self, session: Session) -> None:
        with self._lock:
            self._write_index_unlocked(session)

    def _write_index_unlocked(self, session: Session) -> None:
        _ = self.root.mkdir(parents=True, exist_ok=True)
        previous = self._index.get(session.session_key)
        previous_entry = cast("dict[str, object]", previous) if isinstance(previous, dict) else {}
        first_seen_at = _float_or_none(previous_entry.get("first_seen_at"))
        if first_seen_at is None:
            first_seen_at = time.time()
        self._index[session.session_key] = {
            "session_id": str(session.session_id),
            "first_seen_at": first_seen_at,
            "updated_at": time.time(),
            "event_count": len(session.trace_events),
            "content_hash": _session_content_hash(session),
            "file": self._path_for(session.session_key).name,
            "file_bytes": self._path_for(session.session_key).stat().st_size,
        }
        temporary = self._index_path.with_suffix(".json.tmp")
        _ = temporary.write_text(
            json.dumps(self._index, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        _ = temporary.replace(self._index_path)
        self._sync_sqlite_unlocked(session)

    def _connect_sqlite(self) -> sqlite3.Connection:
        _ = self.root.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self._db_path, timeout=30)
        connection.row_factory = sqlite3.Row
        return connection

    @staticmethod
    def _row_value(row: sqlite3.Row, index: int) -> object:
        return cast("object", row[index])

    @classmethod
    def _row_text(cls, row: sqlite3.Row, index: int) -> str:
        value = cls._row_value(row, index)
        return value if isinstance(value, str) else ""

    @classmethod
    def _row_int(cls, row: sqlite3.Row, index: int) -> int:
        value = cls._row_value(row, index)
        return int(value) if isinstance(value, (int, float)) else 0

    @classmethod
    def _row_float(cls, row: sqlite3.Row, index: int) -> float:
        value = cls._row_value(row, index)
        return float(value) if isinstance(value, (int, float)) else 0.0

    @classmethod
    def _row_optional_text(cls, row: sqlite3.Row, index: int) -> str | None:
        value = cls._row_value(row, index)
        return value if isinstance(value, str) else None

    def _ensure_sqlite_unlocked(self, connection: sqlite3.Connection) -> None:
        _ = connection.execute("PRAGMA journal_mode=WAL")
        _ = connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS trace_events (
                trace_id TEXT PRIMARY KEY,
                session_key TEXT NOT NULL,
                sequence INTEGER NOT NULL,
                event_kind TEXT NOT NULL,
                call_id TEXT,
                tool_name TEXT,
                status TEXT NOT NULL,
                started_at REAL,
                ended_at REAL,
                metadata_text TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                UNIQUE(session_key, sequence)
            );
            CREATE INDEX IF NOT EXISTS trace_events_session_sequence
                ON trace_events(session_key, sequence);
            CREATE TABLE IF NOT EXISTS trace_checkpoints (
                session_key TEXT PRIMARY KEY,
                source_file TEXT NOT NULL,
                source_bytes INTEGER NOT NULL,
                source_event_count INTEGER NOT NULL,
                source_offset INTEGER NOT NULL,
                generation INTEGER NOT NULL DEFAULT 0,
                updated_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trace_clear_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                cleared_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trace_clear_checkpoints (
                session_key TEXT PRIMARY KEY,
                source_file TEXT NOT NULL,
                source_bytes INTEGER NOT NULL,
                source_offset INTEGER NOT NULL,
                cleared_at REAL NOT NULL
            );
            CREATE TABLE IF NOT EXISTS trace_backfill_state (
                singleton INTEGER PRIMARY KEY CHECK(singleton = 1),
                state TEXT NOT NULL,
                processed INTEGER NOT NULL,
                total INTEGER NOT NULL,
                error TEXT,
                updated_at REAL NOT NULL
            );
            """
        )
        try:
            _ = connection.execute(
                "CREATE VIRTUAL TABLE IF NOT EXISTS trace_fts USING fts5("
                "trace_id UNINDEXED, session_key UNINDEXED, content)"
            )
        except sqlite3.OperationalError:
            self._fts_available = False

    def _sync_sqlite_unlocked(self, session: Session) -> None:
        connection = self._connect_sqlite()
        try:
            self._ensure_sqlite_unlocked(connection)
            _ = connection.execute("BEGIN")
            _ = connection.execute(
                "DELETE FROM trace_events WHERE session_key = ?", (session.session_key,)
            )
            if self._fts_available:
                _ = connection.execute(
                    "DELETE FROM trace_fts WHERE session_key = ?", (session.session_key,)
                )
            for event in session.trace_events:
                payload = _event_payload(event)
                payload_json = json.dumps(payload, ensure_ascii=False, default=str)
                metadata_text = " ".join(
                    _trace_scalar(value)
                    for value in (
                        event.sequence,
                        event.event_kind,
                        event.call_id,
                        event.tool_name,
                        event.namespace,
                        event.status.value,
                        event.source_type,
                        event.parse_state.value,
                    )
                )
                _ = connection.execute(
                    "INSERT OR REPLACE INTO trace_events("
                    "trace_id,session_key,sequence,event_kind,call_id,tool_name,status,"
                    "started_at,ended_at,metadata_text,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        event.trace_id,
                        session.session_key,
                        event.sequence,
                        event.event_kind,
                        event.call_id,
                        event.tool_name,
                        event.status.value,
                        event.started_at,
                        event.ended_at,
                        metadata_text,
                        payload_json,
                    ),
                )
                if self._fts_available:
                    _ = connection.execute(
                        "DELETE FROM trace_fts WHERE trace_id = ?", (event.trace_id,)
                    )
                    _ = connection.execute(
                        "INSERT INTO trace_fts(trace_id,session_key,content) VALUES(?,?,?)",
                        (
                            event.trace_id,
                            session.session_key,
                            " ".join(
                                (
                                    metadata_text,
                                    _trace_scalar(event.input_value),
                                    _trace_scalar(event.result_value),
                                    _trace_scalar(event.raw_payload),
                                )
                            ),
                        ),
                    )
            _ = connection.execute(
                "INSERT OR REPLACE INTO trace_checkpoints("
                "session_key,source_file,source_bytes,source_event_count,source_offset,updated_at) "
                "VALUES(?,?,?,?,?,?)",
                (
                    session.session_key,
                    str(session.file_path),
                    session.file_path.stat().st_size
                    if session.file_path.is_file()
                    else session.byte_offset,
                    len(session.trace_events),
                    session.byte_offset,
                    time.time(),
                ),
            )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _sync_sqlite_payloads_unlocked(
        self, session_key: str, payloads: list[dict[str, object]]
    ) -> None:
        connection = self._connect_sqlite()
        try:
            self._ensure_sqlite_unlocked(connection)
            _ = connection.execute("BEGIN")
            _ = connection.execute("DELETE FROM trace_events WHERE session_key = ?", (session_key,))
            if self._fts_available:
                _ = connection.execute(
                    "DELETE FROM trace_fts WHERE session_key = ?", (session_key,)
                )
            for payload in payloads:
                trace_id = str(payload.get("trace_id", ""))
                sequence = _as_int(payload.get("sequence", 0))
                metadata_text = _metadata_text_from_payload(payload)
                payload_json = json.dumps(payload, ensure_ascii=False, default=str)
                _ = connection.execute(
                    "INSERT OR REPLACE INTO trace_events("
                    "trace_id,session_key,sequence,event_kind,call_id,tool_name,status,"
                    "started_at,ended_at,metadata_text,payload_json) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        trace_id,
                        session_key,
                        sequence,
                        str(payload.get("event_kind", "unknown")),
                        None if payload.get("call_id") is None else str(payload["call_id"]),
                        None if payload.get("tool_name") is None else str(payload["tool_name"]),
                        str(payload.get("status", TraceStatus.UNKNOWN.value)),
                        _float_or_none(payload.get("started_at")),
                        _float_or_none(payload.get("ended_at")),
                        metadata_text,
                        payload_json,
                    ),
                )
                if self._fts_available:
                    _ = connection.execute(
                        "INSERT INTO trace_fts(trace_id,session_key,content) VALUES(?,?,?)",
                        (
                            trace_id,
                            session_key,
                            " ".join(
                                (
                                    metadata_text,
                                    _trace_scalar(payload.get("input_value")),
                                    _trace_scalar(payload.get("result_value")),
                                    _trace_scalar(payload.get("raw_payload")),
                                )
                            ),
                        ),
                    )
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def search_events(  # noqa: PLR0913
        self,
        session_key: str,
        *,
        query: str | None = None,
        metadata_only: bool = False,
        event_kind: str | None = None,
        status: str | None = None,
        tool_name: str | None = None,
        from_time: float | None = None,
        to_time: float | None = None,
    ) -> list[TraceEvent]:
        if not self._db_path.is_file():
            return _filter_event_kind(self.read_trace_events(session_key), event_kind)
        event_kind_filter = None if event_kind in {"event_msg", "discovery"} else event_kind
        connection = self._connect_sqlite()
        try:
            self._ensure_sqlite_unlocked(connection)
            normalized_query = query.casefold() if query else None
            if normalized_query is not None and self._fts_available and not metadata_only:
                sql = (
                    "SELECT e.payload_json FROM trace_events e "
                    "WHERE e.session_key = ? "
                    "AND e.trace_id IN (SELECT trace_id FROM trace_fts WHERE trace_fts MATCH ?) "
                    "AND (? IS NULL OR e.event_kind = ?) "
                    "AND (? IS NULL OR e.status = ?) "
                    "AND (? IS NULL OR e.tool_name = ?) "
                    "AND (? IS NULL OR COALESCE(e.ended_at, e.started_at, 0) >= ?) "
                    "AND (? IS NULL OR COALESCE(e.started_at, e.ended_at, 0) <= ?) "
                    "ORDER BY e.sequence"
                )
                values: list[object] = [
                    session_key,
                    _fts_phrase(query or ""),
                    event_kind_filter,
                    event_kind_filter,
                    status,
                    status,
                    tool_name,
                    tool_name,
                    from_time,
                    from_time,
                    to_time,
                    to_time,
                ]
            elif normalized_query is not None:
                sql = (
                    "SELECT e.payload_json FROM trace_events e "
                    "WHERE e.session_key = ? AND e.metadata_text LIKE ? "
                    "AND (? IS NULL OR e.event_kind = ?) "
                    "AND (? IS NULL OR e.status = ?) "
                    "AND (? IS NULL OR e.tool_name = ?) "
                    "AND (? IS NULL OR COALESCE(e.ended_at, e.started_at, 0) >= ?) "
                    "AND (? IS NULL OR COALESCE(e.started_at, e.ended_at, 0) <= ?) "
                    "ORDER BY e.sequence"
                )
                values = [
                    session_key,
                    f"%{normalized_query}%",
                    event_kind_filter,
                    event_kind_filter,
                    status,
                    status,
                    tool_name,
                    tool_name,
                    from_time,
                    from_time,
                    to_time,
                    to_time,
                ]
            else:
                sql = (
                    "SELECT e.payload_json FROM trace_events e "
                    "WHERE e.session_key = ? "
                    "AND (? IS NULL OR e.event_kind = ?) "
                    "AND (? IS NULL OR e.status = ?) "
                    "AND (? IS NULL OR e.tool_name = ?) "
                    "AND (? IS NULL OR COALESCE(e.ended_at, e.started_at, 0) >= ?) "
                    "AND (? IS NULL OR COALESCE(e.started_at, e.ended_at, 0) <= ?) "
                    "ORDER BY e.sequence"
                )
                values = [
                    session_key,
                    event_kind_filter,
                    event_kind_filter,
                    status,
                    status,
                    tool_name,
                    tool_name,
                    from_time,
                    from_time,
                    to_time,
                    to_time,
                ]
            rows = cast("list[sqlite3.Row]", connection.execute(sql, values).fetchall())
            events: list[TraceEvent] = []
            for row in rows:
                decoded = cast("object", json.loads(self._row_text(row, 0)))
                if isinstance(decoded, dict):
                    events.append(_event_from_payload(cast("dict[str, object]", decoded)))
            return _filter_event_kind(events, event_kind)
        finally:
            connection.close()

    def search_all_events(  # noqa: PLR0913
        self,
        *,
        query: str | None = None,
        metadata_only: bool = False,
        provider: str | None = None,
        turn_id: str | None = None,
        event_kind: str | None = None,
        status: str | None = None,
        tool_name: str | None = None,
        from_time: float | None = None,
        to_time: float | None = None,
    ) -> list[tuple[str, TraceEvent]]:
        session_keys = sorted(self._index)
        if not session_keys and self._db_path.is_file():
            connection = self._connect_sqlite()
            try:
                self._ensure_sqlite_unlocked(connection)
                rows = cast(
                    "list[sqlite3.Row]",
                    connection.execute(
                        "SELECT DISTINCT session_key FROM trace_events ORDER BY session_key"
                    ).fetchall(),
                )
                session_keys = [self._row_text(row, 0) for row in rows]
            finally:
                connection.close()
        matches: list[tuple[str, TraceEvent]] = []
        for session_key in session_keys:
            for event in self.search_events(
                session_key,
                query=query,
                metadata_only=metadata_only,
                event_kind=event_kind,
                status=status,
                tool_name=tool_name,
                from_time=from_time,
                to_time=to_time,
            ):
                if provider is not None and event.provider != provider:
                    continue
                if turn_id is not None and str(event.turn_id) != turn_id:
                    continue
                matches.append((session_key, event))
        return matches

    def _read_index(self) -> dict[str, object]:
        if not self._index_path.is_file():
            return {}
        try:
            value = cast("object", json.loads(self._index_path.read_text(encoding="utf-8")))
        except (OSError, json.JSONDecodeError):
            return {}
        if not isinstance(value, dict):
            return {}
        entries = cast("dict[object, object]", value)
        return {key: raw_value for key, raw_value in entries.items() if isinstance(key, str)}

    def clear(self) -> int:
        with self._lock:
            if not self.root.is_dir():
                return 0
            removed = 0
            for path in self.root.glob("*.jsonl"):
                path.unlink(missing_ok=True)
                removed += 1
            for path in self.root.glob("*.jsonl.tmp"):
                path.unlink(missing_ok=True)
            self._index_path.unlink(missing_ok=True)
            self._index = {}
            if self._db_path.is_file():
                connection = self._connect_sqlite()
                try:
                    self._ensure_sqlite_unlocked(connection)
                    _ = connection.execute("DELETE FROM trace_events")
                    _ = connection.execute("DELETE FROM trace_checkpoints")
                    if self._fts_available:
                        _ = connection.execute("DELETE FROM trace_fts")
                    connection.commit()
                finally:
                    connection.close()
            return removed

    def mark_cleared(self, session: Session) -> None:
        connection = self._connect_sqlite()
        try:
            self._ensure_sqlite_unlocked(connection)
            cleared_at = time.time()
            source_path = session.file_path
            source_bytes = (
                source_path.stat().st_size if source_path.is_file() else session.byte_offset
            )
            _ = connection.execute(
                "INSERT OR REPLACE INTO trace_clear_state(singleton,cleared_at) VALUES(1,?)",
                (cleared_at,),
            )
            _ = connection.execute(
                "INSERT OR REPLACE INTO trace_clear_checkpoints("
                "session_key,source_file,source_bytes,source_offset,cleared_at) "
                "VALUES(?,?,?,?,?)",
                (
                    session.session_key,
                    str(source_path),
                    source_bytes,
                    session.byte_offset,
                    cleared_at,
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def apply_clear_policy(self, session: Session) -> None:
        if not self._db_path.is_file():
            return
        connection = self._connect_sqlite()
        try:
            self._ensure_sqlite_unlocked(connection)
            row = cast(
                "sqlite3.Row | None",
                connection.execute(
                    "SELECT source_file,source_bytes,source_offset FROM trace_clear_checkpoints "
                    "WHERE session_key = ?",
                    (session.session_key,),
                ).fetchone(),
            )
            if row is not None:
                source_path = Path(self._row_text(row, 0))
                if not source_path.is_file():
                    session.trace_events.clear()
                    return
                threshold = self._row_int(row, 2)
                session.trace_events[:] = [
                    event
                    for event in session.trace_events
                    if event.source_offset is not None and event.source_offset >= threshold
                ]
                return
            state = cast(
                "sqlite3.Row | None",
                connection.execute(
                    "SELECT cleared_at FROM trace_clear_state WHERE singleton = 1"
                ).fetchone(),
            )
            if state is not None and session.last_event_at < self._row_float(state, 0):
                session.trace_events.clear()
        finally:
            connection.close()

    def clear_checkpoints(self) -> None:
        if not self._db_path.is_file():
            return
        connection = self._connect_sqlite()
        try:
            self._ensure_sqlite_unlocked(connection)
            _ = connection.execute("DELETE FROM trace_clear_checkpoints")
            _ = connection.execute("DELETE FROM trace_clear_state")
            connection.commit()
        finally:
            connection.close()

    def prune(self, now: float | None = None) -> int:  # noqa: C901, PLR0912, PLR0915
        with self._lock:
            if self.retention_days is None or self.retention_days <= 0 or not self.root.is_dir():
                return 0
            cutoff = (time.time() if now is None else now) - self.retention_days * 86400
            changed_sessions: set[str] = set()
            for path in self.root.glob("*.jsonl"):
                session_key = next(
                    (
                        key
                        for key, value in self._index.items()
                        if (
                            isinstance(value, dict)
                            and cast("dict[str, object]", value).get("file") == path.name
                        )
                    ),
                    None,
                )
                if session_key is None:
                    continue
                entry = self._index.get(session_key)
                typed_entry = cast("dict[str, object]", entry) if isinstance(entry, dict) else {}
                fallback_time = _float_or_none(typed_entry.get("first_seen_at"))
                if fallback_time is None:
                    fallback_time = path.stat().st_mtime
                kept: list[bytes] = []
                payloads: list[dict[str, object]] = []
                expired = False
                try:
                    with path.open("rb") as handle:
                        for raw_line in handle:
                            try:
                                decoded = cast("object", json.loads(raw_line))
                            except (json.JSONDecodeError, UnicodeDecodeError):
                                if fallback_time >= cutoff:
                                    kept.append(raw_line)
                                else:
                                    expired = True
                                continue
                            if not isinstance(decoded, dict):
                                if fallback_time >= cutoff:
                                    kept.append(raw_line)
                                else:
                                    expired = True
                                continue
                            payload = cast("dict[str, object]", decoded)
                            event_time = _retention_time(payload, fallback_time)
                            status = str(payload.get("status", TraceStatus.UNKNOWN.value))
                            if event_time < cutoff and status != TraceStatus.RUNNING.value:
                                expired = True
                                continue
                            kept.append(raw_line)
                            payloads.append(payload)
                except OSError:
                    continue
                if not expired:
                    continue
                changed_sessions.add(session_key)
                if kept:
                    temporary = path.with_suffix(".jsonl.tmp")
                    with temporary.open("wb") as handle:
                        for raw_line in kept:
                            _ = handle.write(raw_line)
                    _ = temporary.replace(path)
                    typed_entry["event_count"] = len(payloads)
                    typed_entry["file_bytes"] = path.stat().st_size
                    typed_entry["updated_at"] = time.time()
                    typed_entry["content_hash"] = _payload_content_hash(payloads)
                    self._index[session_key] = typed_entry
                    self._sync_sqlite_payloads_unlocked(session_key, payloads)
                else:
                    path.unlink(missing_ok=True)
                    del self._index[session_key]
                    if self._db_path.is_file():
                        connection = self._connect_sqlite()
                        try:
                            self._ensure_sqlite_unlocked(connection)
                            _ = connection.execute(
                                "DELETE FROM trace_events WHERE session_key = ?",
                                (session_key,),
                            )
                            _ = connection.execute(
                                "DELETE FROM trace_checkpoints WHERE session_key = ?",
                                (session_key,),
                            )
                            if self._fts_available:
                                _ = connection.execute(
                                    "DELETE FROM trace_fts WHERE session_key = ?", (session_key,)
                                )
                            connection.commit()
                        finally:
                            connection.close()
            if changed_sessions:
                temporary = self._index_path.with_suffix(".json.tmp")
                _ = temporary.write_text(
                    json.dumps(self._index, ensure_ascii=False, indent=2), encoding="utf-8"
                )
                _ = temporary.replace(self._index_path)
            return len(changed_sessions)

    def usage_bytes(self) -> int:
        if not self.root.is_dir():
            return 0
        return sum(path.stat().st_size for path in self.root.rglob("*") if path.is_file())

    def file_count(self) -> int:
        if not self.root.is_dir():
            return 0
        return sum(1 for path in self.root.glob("*.jsonl") if path.is_file())

    def begin_backfill(self, total: int) -> None:
        with self._lock:
            persisted = self._read_backfill_state_unlocked()
            if persisted is not None and persisted[0] == "paused":
                self._backfill_state = "paused"
                self._backfill_total = max(0, total)
                self._backfill_processed = min(persisted[1], self._backfill_total)
                self._backfill_error = persisted[3]
                self._persist_backfill_state_unlocked()
                return
            self._backfill_state = "building"
            self._backfill_total = max(0, total)
            self._backfill_processed = 0
            self._backfill_error = None
            self._persist_backfill_state_unlocked()

    def advance_backfill(self) -> None:
        with self._lock:
            self._backfill_processed = min(self._backfill_total, self._backfill_processed + 1)
            self._persist_backfill_state_unlocked()

    def complete_backfill(self) -> None:
        with self._lock:
            self._backfill_processed = self._backfill_total
            self._backfill_state = "complete"
            self._persist_backfill_state_unlocked()

    def fail_backfill(self, error: str) -> None:
        with self._lock:
            self._backfill_state = "error"
            self._backfill_error = error
            self._persist_backfill_state_unlocked()

    def pause_backfill(self) -> None:
        with self._lock:
            if self._backfill_state == "building":
                self._backfill_state = "paused"
                self._persist_backfill_state_unlocked()

    def resume_backfill(self) -> None:
        with self._lock:
            if self._backfill_state == "paused":
                self._backfill_state = "building"
                self._persist_backfill_state_unlocked()

    def backfill_is_paused(self) -> bool:
        with self._lock:
            return self._backfill_state == "paused"

    def backfill_status(self) -> dict[str, object]:
        with self._lock:
            persisted = self._read_backfill_state_unlocked()
            if persisted is not None:
                (
                    self._backfill_state,
                    self._backfill_processed,
                    self._backfill_total,
                    self._backfill_error,
                ) = persisted
            return {
                "state": self._backfill_state,
                "processed": self._backfill_processed,
                "total": self._backfill_total,
                "error": self._backfill_error,
            }

    def _read_backfill_state_unlocked(self) -> tuple[str, int, int, str | None] | None:
        if not self._db_path.is_file():
            return None
        connection = self._connect_sqlite()
        try:
            self._ensure_sqlite_unlocked(connection)
            row = cast(
                "sqlite3.Row | None",
                connection.execute(
                    "SELECT state,processed,total,error "
                    "FROM trace_backfill_state WHERE singleton = 1"
                ).fetchone(),
            )
            if row is None:
                return None
            state = self._row_text(row, 0)
            processed = self._row_int(row, 1)
            total = self._row_int(row, 2)
            error = self._row_optional_text(row, 3)
            return state, processed, total, error
        finally:
            connection.close()

    def _persist_backfill_state_unlocked(self) -> None:
        connection = self._connect_sqlite()
        try:
            self._ensure_sqlite_unlocked(connection)
            _ = connection.execute(
                "INSERT OR REPLACE INTO trace_backfill_state("
                "singleton,state,processed,total,error,updated_at) VALUES(1,?,?,?,?,?)",
                (
                    self._backfill_state,
                    self._backfill_processed,
                    self._backfill_total,
                    self._backfill_error,
                    time.time(),
                ),
            )
            connection.commit()
        finally:
            connection.close()

    def read_events(
        self, session_key: str, *, before: int | None = None, limit: int = 100
    ) -> list[dict[str, object]]:
        path = self._path_for(session_key)
        if not path.is_file():
            return []
        events: deque[dict[str, object]] = deque(maxlen=max(1, limit))
        seen = 0
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    if before is not None and seen >= before:
                        break
                    try:
                        value = cast("object", json.loads(line))
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        entries = cast("dict[object, object]", value)
                        events.append(
                            {
                                key: raw_value
                                for key, raw_value in entries.items()
                                if isinstance(key, str)
                            }
                        )
                        seen += 1
        except OSError:
            return []
        return list(events)

    def iter_events(self, session_key: str) -> Iterator[dict[str, object]]:
        path = self._path_for(session_key)
        if not path.is_file():
            return
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        value = cast("object", json.loads(line))
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        entries = cast("dict[object, object]", value)
                        yield {
                            key: raw_value
                            for key, raw_value in entries.items()
                            if isinstance(key, str)
                        }
        except OSError:
            return

    def read_all_events(self, session_key: str) -> list[dict[str, object]]:
        return list(self.iter_events(session_key))

    def read_trace_events(self, session_key: str) -> list[TraceEvent]:
        payloads = self.read_all_events(session_key)
        return [_event_from_payload(payload) for payload in payloads]

    def read_event(self, session_key: str, trace_id: str) -> dict[str, object] | None:
        path = self._path_for(session_key)
        if not path.is_file():
            return None
        try:
            with path.open("r", encoding="utf-8") as handle:
                for line in handle:
                    try:
                        value = cast("object", json.loads(line))
                    except json.JSONDecodeError:
                        continue
                    if isinstance(value, dict):
                        typed_value = cast("dict[str, object]", value)
                    else:
                        continue
                    if typed_value.get("trace_id") == trace_id:
                        entries = typed_value
                        return dict(entries)
        except OSError:
            return None
        return None

    def read_content_chunk(
        self,
        session_key: str,
        trace_id: str,
        content: str,
        offset: int,
        length: int,
    ) -> tuple[str, int] | None:
        event = self.read_event(session_key, trace_id)
        if event is None:
            return None
        value = event.get("raw_payload") if content == "raw" else event.get(f"{content}_value")
        if isinstance(value, str):
            return value[offset : offset + length], len(value)
        chunks = json.JSONEncoder(ensure_ascii=False, default=str).iterencode(value)
        wanted: list[str] = []
        position = 0
        total = 0
        for part in chunks:
            next_position = position + len(part)
            if next_position > offset and position < offset + length:
                start = max(0, offset - position)
                end = min(len(part), offset + length - position)
                wanted.append(part[start:end])
            position = next_position
            total = next_position
        return "".join(wanted), total

    def content_sha256(self, session_key: str, trace_id: str, content: str) -> str | None:
        event = self.read_event(session_key, trace_id)
        if event is None:
            return None
        value = event.get("raw_payload") if content == "raw" else event.get(f"{content}_value")
        return hashlib.sha256(
            json.dumps(value, ensure_ascii=False, default=str).encode("utf-8")
            if not isinstance(value, str)
            else value.encode("utf-8")
        ).hexdigest()

    def export_session(self, session: Session) -> bytes:
        payload = {
            "schema_version": 1,
            "provider": "codex",
            "session_key": session.session_key,
            "session_id": str(session.session_id),
            "events": [_event_payload(event) for event in session.trace_events],
        }
        return json.dumps(payload, ensure_ascii=False, indent=2, default=str).encode("utf-8")

    def export_session_stream(self, session_key: str, session_id: str) -> Iterator[bytes] | None:
        path = self._path_for(session_key)
        if not path.is_file():
            return None

        def chunks() -> Iterator[bytes]:
            header = json.dumps(
                {
                    "schema_version": 1,
                    "provider": "codex",
                    "session_key": session_key,
                    "session_id": session_id,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            )
            yield (header[:-1] + ',"events":[').encode("utf-8")
            first = True
            for payload in self.iter_events(session_key):
                if not first:
                    yield b","
                first = False
                yield json.dumps(
                    payload, ensure_ascii=False, separators=(",", ":"), default=str
                ).encode("utf-8")
            yield b"]}"

        return chunks()


def _event_payload(event: TraceEvent) -> dict[str, object]:
    return {
        "trace_id": event.trace_id,
        "provider": event.provider,
        "session_id": str(event.session_id),
        "turn_id": None if event.turn_id is None else str(event.turn_id),
        "sequence": event.sequence,
        "event_kind": event.event_kind,
        "call_id": event.call_id,
        "related_trace_id": event.related_trace_id,
        "tool_name": event.tool_name,
        "namespace": event.namespace,
        "status": event.status.value,
        "started_at": event.started_at,
        "ended_at": event.ended_at,
        "duration_ms": event.duration_ms,
        "content_metadata": event.content_metadata,
        "input_value": event.input_value,
        "result_value": event.result_value,
        "raw_payload": event.raw_payload,
        "source_line": event.source_line,
        "source_offset": event.source_offset,
        "source_type": event.source_type,
        "parse_state": event.parse_state.value,
    }


def _trace_scalar(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)


def _metadata_text_from_payload(payload: dict[str, object]) -> str:
    return " ".join(
        _trace_scalar(value)
        for value in (
            payload.get("sequence"),
            payload.get("event_kind"),
            payload.get("call_id"),
            payload.get("tool_name"),
            payload.get("namespace"),
            payload.get("status", TraceStatus.UNKNOWN.value),
            payload.get("source_type"),
            payload.get("parse_state", TraceParseState.UNKNOWN.value),
        )
    )


def _fts_phrase(value: str) -> str:
    return '"' + value.replace('"', '""') + '"'


def _session_content_hash(session: Session) -> str:
    digest = hashlib.sha256()
    for event in session.trace_events:
        payload = json.dumps(
            _event_payload(event), ensure_ascii=False, default=str, separators=(",", ":")
        ).encode("utf-8")
        digest.update(payload)
        digest.update(b"\n")
    return digest.hexdigest()


def _payload_content_hash(payloads: list[dict[str, object]]) -> str:
    digest = hashlib.sha256()
    for payload in payloads:
        encoded = json.dumps(
            payload, ensure_ascii=False, default=str, separators=(",", ":")
        ).encode("utf-8")
        digest.update(encoded)
        digest.update(b"\n")
    return digest.hexdigest()


def _retention_time(payload: dict[str, object], fallback: float) -> float:
    ended = _float_or_none(payload.get("ended_at"))
    if ended is not None:
        return ended
    started = _float_or_none(payload.get("started_at"))
    return fallback if started is None else started


def _filter_event_kind(events: list[TraceEvent], event_kind: str | None) -> list[TraceEvent]:
    if event_kind == "event_msg":
        return [event for event in events if event.event_kind in EVENT_MESSAGE_TRACE_KINDS]
    if event_kind == "discovery":
        return [event for event in events if event.event_kind in DISCOVERY_TRACE_KINDS]
    return events


def _file_content_stats(path: Path) -> tuple[int, str]:
    digest = hashlib.sha256()
    count = 0
    with path.open("rb") as handle:
        for line in handle:
            digest.update(line)
            count += 1
    return count, digest.hexdigest()


def _event_from_payload(payload: dict[str, object]) -> TraceEvent:
    status_value = str(payload.get("status", TraceStatus.UNKNOWN.value))
    parse_value = str(payload.get("parse_state", TraceParseState.UNKNOWN.value))
    try:
        status = TraceStatus(status_value)
    except ValueError:
        status = TraceStatus.UNKNOWN
    parse_state = (
        TraceParseState(parse_value)
        if parse_value in {item.value for item in TraceParseState}
        else TraceParseState.UNKNOWN
    )
    turn_id_value = payload.get("turn_id")
    return TraceEvent(
        trace_id=str(payload.get("trace_id", "unknown")),
        provider=str(payload.get("provider", "codex")),
        session_id=SessionId(str(payload.get("session_id", "unknown"))),
        turn_id=None if turn_id_value is None else TurnId(str(turn_id_value)),
        sequence=_as_int(payload.get("sequence", 0)),
        event_kind=str(payload.get("event_kind", "unknown")),
        call_id=None if payload.get("call_id") is None else str(payload.get("call_id")),
        related_trace_id=(
            None
            if payload.get("related_trace_id") is None
            else str(payload.get("related_trace_id"))
        ),
        tool_name=None if payload.get("tool_name") is None else str(payload.get("tool_name")),
        namespace=None if payload.get("namespace") is None else str(payload.get("namespace")),
        status=status,
        started_at=_float_or_none(payload.get("started_at")),
        ended_at=_float_or_none(payload.get("ended_at")),
        input_value=payload.get("input_value"),
        result_value=payload.get("result_value"),
        raw_payload=payload.get("raw_payload"),
        source_line=_int_or_none(payload.get("source_line")),
        source_offset=_int_or_none(payload.get("source_offset")),
        source_type=None if payload.get("source_type") is None else str(payload.get("source_type")),
        parse_state=parse_state,
    )


def _float_or_none(value: object) -> float | None:
    if value is None or not isinstance(value, (int, float, str)):
        return None
    try:
        return float(value)
    except (TypeError, ValueError, OverflowError):
        return None


def _int_or_none(value: object) -> int | None:
    return None if value is None else _as_int(value)


def _as_int(value: object) -> int:
    if not isinstance(value, (int, float, str)):
        return 0
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return 0
