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
    derive_activity_state,
    session_detail,
    session_summary,
)


class ChangeKind(StrEnum):
    CREATED = "session_created"
    UPDATED = "session_updated"


@dataclass(frozen=True, slots=True)
class ChangeRecord:
    kind: ChangeKind
    session_id: str | None


@final
class SessionStore:
    __slots__ = (
        "_activity_states",
        "_condition",
        "_last_change",
        "_paths",
        "_sessions",
        "_version",
    )

    _condition: anyio.Condition
    _activity_states: dict[str, ActivityState]
    _last_change: ChangeRecord | None
    _paths: dict[Path, str]
    _sessions: dict[str, Session]
    _version: int

    def __init__(self) -> None:
        self._condition = anyio.Condition()
        self._activity_states = {}
        self._last_change = None
        self._paths = {}
        self._sessions = {}
        self._version = 0

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

    async def put(self, session: Session) -> ChangeKind:
        key = session.session_key
        change = ChangeKind.CREATED if key not in self._sessions else ChangeKind.UPDATED
        self._sessions[key] = session
        self._paths[session.file_path] = key
        self._last_change = ChangeRecord(change, key)
        async with self._condition:
            self._version += 1
            self._condition.notify_all()
        return change

    async def replace(self, sessions: list[Session]) -> None:
        self._sessions = {session.session_key: session for session in sessions}
        self._paths = {session.file_path: session.session_key for session in sessions}
        self._activity_states = {}
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
            session = self._sessions[record.session_id]
            return SessionChangedEvent(
                event=_event_name(record.kind),
                version=self._version,
                session_key=record.session_id,
                data=session_summary(session, now, stuck_seconds, data_fresh),
            )
        return self.snapshot_event(now, stuck_seconds, data_fresh)


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
