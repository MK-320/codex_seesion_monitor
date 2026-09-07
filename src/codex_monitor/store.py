from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Literal, final

import anyio

from codex_monitor.models import ActivityState, AttentionReason, Session, SessionStatus
from codex_monitor.schemas import (
    RealtimeEvent,
    SessionChangedEvent,
    SessionDetail,
    SessionSummary,
    SnapshotEvent,
    TraceRevisionEvent,
    derive_activity_state,
    session_detail,
    session_summary,
)
from codex_monitor.trace_store import TraceStore


class ChangeKind(StrEnum):
    CREATED = "session_created"
    UPDATED = "session_updated"


@dataclass(frozen=True, slots=True)
class ChangeRecord:
    kind: ChangeKind
    session_id: str | None
    trace_added_ids: tuple[str, ...] = ()
    trace_updated_ids: tuple[str, ...] = ()
    trace_last_sequence: int | None = None


@final
class SessionStore:
    __slots__ = (
        "_activity_states",
        "_condition",
        "_last_change",
        "_paths",
        "_sessions",
        "_trace_signatures",
        "_trace_store",
        "_version",
    )

    _condition: anyio.Condition
    _activity_states: dict[str, ActivityState]
    _last_change: ChangeRecord | None
    _paths: dict[Path, str]
    _sessions: dict[str, Session]
    _trace_signatures: dict[str, dict[str, tuple[object, ...]]]
    _version: int

    def __init__(self) -> None:
        self._condition = anyio.Condition()
        self._activity_states = {}
        self._last_change = None
        self._paths = {}
        self._sessions = {}
        self._version = 0
        self._trace_store: TraceStore | None = None
        self._trace_signatures = {}

    def attach_trace_store(self, trace_store: TraceStore) -> None:
        self._trace_store = trace_store
        for session in self._sessions.values():
            trace_store.save_session(session)

    @property
    def version(self) -> int:
        return self._version

    def health_counts(self) -> dict[str, int]:
        return {
            "session_count": len(self._sessions),
            "unknown_event_count": sum(
                session.unknown_event_count for session in self._sessions.values()
            ),
            "malformed_line_count": sum(
                session.malformed_line_count for session in self._sessions.values()
            ),
            "oversized_line_count": sum(
                session.oversized_line_count for session in self._sessions.values()
            ),
        }

    def get(self, session_id: str) -> Session | None:
        exact = self._sessions.get(session_id)
        if exact is not None:
            return exact
        matches = [
            session for session in self._sessions.values() if session.session_id == session_id
        ]
        return matches[0] if len(matches) == 1 else None

    def get_by_path(self, path: Path) -> Session | None:
        session_id = self._paths.get(path)
        return None if session_id is None else self._sessions.get(session_id)

    def all_sessions(self) -> tuple[Session, ...]:
        return tuple(self._sessions.values())

    async def put(self, session: Session) -> ChangeKind:
        key = session.session_key
        if self._trace_store is not None:
            self._trace_store.apply_clear_policy(session)
        change = ChangeKind.CREATED if key not in self._sessions else ChangeKind.UPDATED
        previous_signature = self._trace_signatures.get(key, {})
        current_signature = _trace_signature(session)
        added_trace_ids = tuple(
            trace_id for trace_id in current_signature if trace_id not in previous_signature
        )
        updated_trace_ids = tuple(
            trace_id
            for trace_id, signature in current_signature.items()
            if trace_id in previous_signature and previous_signature[trace_id] != signature
        )
        self._sessions[key] = session
        self._trace_signatures[key] = current_signature
        if self._trace_store is not None:
            self._trace_store.save_session(session)
        self._paths[session.file_path] = key
        last_sequence = session.trace_events[-1].sequence if session.trace_events else None
        self._last_change = ChangeRecord(
            change,
            key,
            added_trace_ids,
            updated_trace_ids,
            last_sequence,
        )
        async with self._condition:
            self._version += 1
            self._condition.notify_all()
        return change

    async def replace(self, sessions: list[Session]) -> None:
        self._sessions = {session.session_key: session for session in sessions}
        self._paths = {session.file_path: session.session_key for session in sessions}
        self._activity_states = {}
        if self._trace_store is not None:
            for session in sessions:
                self._trace_store.apply_clear_policy(session)
                self._trace_store.save_session(session)
        self._trace_signatures = {
            session.session_key: _trace_signature(session) for session in sessions
        }
        self._last_change = None
        async with self._condition:
            self._version += 1
            self._condition.notify_all()

    async def wait_for_change(self, after_version: int) -> int:
        async with self._condition:
            while self._version <= after_version:
                await self._condition.wait()
            return self._version

    async def refresh_stuck(
        self,
        now: float,
        stuck_seconds: int | None,
        data_fresh: bool = True,
    ) -> bool:
        states = {
            session.session_key: derive_activity_state(session, now, stuck_seconds, data_fresh)
            for session in self._sessions.values()
        }
        if not self._activity_states:
            self._activity_states = states
            return False
        changed_keys = [
            key for key, state in states.items() if self._activity_states.get(key) is not state
        ]
        self._activity_states = states
        if changed_keys:
            session_key = changed_keys[0] if len(changed_keys) == 1 else None
            self._last_change = ChangeRecord(ChangeKind.UPDATED, session_key)
            async with self._condition:
                self._version += 1
                self._condition.notify_all()
        return bool(changed_keys)

    def trace_status(self) -> dict[str, object]:
        if self._trace_store is None:
            return {
                "enabled": False,
                "retention_days": None,
                "used_bytes": 0,
                "file_count": 0,
                "index_state": "unavailable",
                "backfill_state": "unavailable",
                "backfill": {
                    "state": "unavailable",
                    "processed": 0,
                    "total": 0,
                    "error": None,
                },
            }
        return {
            "enabled": self._trace_store.enabled,
            "retention_days": self._trace_store.retention_days,
            "used_bytes": self._trace_store.usage_bytes(),
            "file_count": self._trace_store.file_count(),
            "root": str(self._trace_store.root),
            "index_state": "ready",
            "backfill": self._trace_store.backfill_status(),
            "backfill_state": self._trace_store.backfill_status()["state"],
        }

    def begin_trace_backfill(self, total: int) -> None:
        if self._trace_store is not None:
            self._trace_store.begin_backfill(total)

    def advance_trace_backfill(self) -> None:
        if self._trace_store is not None:
            self._trace_store.advance_backfill()

    def complete_trace_backfill(self) -> None:
        if self._trace_store is not None:
            self._trace_store.complete_backfill()

    def fail_trace_backfill(self, error: str) -> None:
        if self._trace_store is not None:
            self._trace_store.fail_backfill(error)

    def pause_trace_backfill(self) -> None:
        if self._trace_store is not None:
            self._trace_store.pause_backfill()

    def resume_trace_backfill(self) -> None:
        if self._trace_store is not None:
            self._trace_store.resume_backfill()

    def trace_backfill_is_paused(self) -> bool:
        return self._trace_store is not None and self._trace_store.backfill_is_paused()

    def configure_traces(self, *, enabled: bool, retention_days: int | None) -> dict[str, object]:
        if self._trace_store is None:
            return self.trace_status()
        self._trace_store.enabled = enabled
        self._trace_store.retention_days = retention_days
        _ = self._trace_store.prune()
        return self.trace_status()

    def clear_traces(self) -> dict[str, object]:
        if self._trace_store is not None:
            for session in self._sessions.values():
                self._trace_store.mark_cleared(session)
            _ = self._trace_store.clear()
            for session in self._sessions.values():
                session.trace_events.clear()
            self._trace_signatures.clear()
        return self.trace_status()

    def rebuild_trace_history(self) -> dict[str, object]:
        if self._trace_store is not None:
            self._trace_store.clear_checkpoints()
        return self.trace_status()

    def export_traces(self, session_id: str) -> bytes | None:
        session = self.get(session_id)
        if session is None or self._trace_store is None:
            return None
        return self._trace_store.export_session(session)

    def summaries(
        self,
        now: float,
        stuck_seconds: int | None,
        data_fresh: bool = True,
    ) -> tuple[SessionSummary, ...]:
        snapshots = (
            session_summary(session, now, stuck_seconds, data_fresh)
            for session in self._sessions.values()
        )
        return tuple(
            sorted(
                snapshots,
                key=lambda item: (_status_rank(item), -item.last_event_at),
            )
        )

    def detail(  # noqa: PLR0913
        self,
        session_id: str,
        now: float,
        stuck_seconds: int | None,
        limit: int = 20,
        before: int | None = None,
        anchor_turn_id: str | None = None,
        data_fresh: bool = True,
    ) -> SessionDetail | None:
        session = self.get(session_id)
        return (
            None
            if session is None
            else session_detail(
                session,
                now,
                stuck_seconds,
                limit,
                before,
                anchor_turn_id,
                data_fresh,
            )
        )

    def snapshot_event(
        self,
        now: float,
        stuck_seconds: int | None,
        data_fresh: bool = True,
    ) -> SnapshotEvent:
        return SnapshotEvent(
            version=self._version,
            data=self.summaries(now, stuck_seconds, data_fresh),
        )

    def event_after(
        self,
        after_version: int,
        now: float,
        stuck_seconds: int | None,
        data_fresh: bool = True,
    ) -> RealtimeEvent:
        record = self._last_change
        if (
            after_version == self._version - 1
            and record is not None
            and record.session_id is not None
        ):
            if record.trace_added_ids or record.trace_updated_ids:
                return TraceRevisionEvent(
                    version=self._version,
                    session_key=record.session_id,
                    data=session_summary(
                        self._sessions[record.session_id],
                        now,
                        stuck_seconds,
                        data_fresh,
                    ),
                    added_trace_ids=record.trace_added_ids,
                    updated_trace_ids=record.trace_updated_ids,
                    last_sequence=record.trace_last_sequence,
                )
            session = self._sessions[record.session_id]
            return SessionChangedEvent(
                event=_event_name(record.kind),
                version=self._version,
                session_key=record.session_id,
                data=session_summary(session, now, stuck_seconds, data_fresh),
            )
        return self.snapshot_event(now, stuck_seconds, data_fresh)


def _trace_signature(session: Session) -> dict[str, tuple[object, ...]]:
    return {
        event.trace_id: (
            event.status.value,
            event.started_at,
            event.ended_at,
            event.duration_ms,
            event.related_trace_id,
        )
        for event in session.trace_events
    }


def _status_rank(summary: SessionSummary) -> int:
    reasons = summary.attention_reasons
    reason_ranks = {
        AttentionReason.TOOL_ERROR: 0,
        AttentionReason.TURN_ABORTED: 1,
        AttentionReason.LONG_RUNNING_TOOL: 2,
        AttentionReason.NO_PROGRESS: 3,
    }
    if reasons:
        ranked = [reason_ranks[reason] for reason in reasons if reason in reason_ranks]
        if ranked:
            return min(ranked)
    status = summary.status
    match status:
        case SessionStatus.RUNNING:
            return 4
        case SessionStatus.IDLE:
            return 5
        case SessionStatus.STUCK:
            return 5
        case SessionStatus.UNKNOWN:
            return 6


def _event_name(
    kind: ChangeKind,
) -> Literal["session_created", "session_updated"]:
    match kind:
        case ChangeKind.CREATED:
            return "session_created"
        case ChangeKind.UPDATED:
            return "session_updated"
