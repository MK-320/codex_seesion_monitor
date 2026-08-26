import hashlib
import json
import time
from typing import ClassVar, Literal

from pydantic import BaseModel, ConfigDict, Field

from codex_monitor.models import (
    ActivityState,
    AttentionReason,
    Session,
    SessionStatus,
    ToolCall,
    ToolStatus,
    Turn,
)
from codex_monitor.parser import summarize

QUIET_AFTER_SECONDS = 120


class ApiModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)


class ToolCallView(ApiModel):
    name: str
    input_summary: str
    status: str
    result_summary: str
    started_at: float
    ended_at: float | None
    duration_seconds: float | None


class TurnView(ApiModel):
    turn_id: str
    started_at: float
    ended_at: float | None
    duration_seconds: float
    user_message: str
    tool_calls: tuple[ToolCallView, ...]
    agent_text_snippets: tuple[str, ...]
    aborted: bool
    end_reason: str


class ParseDiagnostics(ApiModel):
    unknown_event_count: int
    malformed_line_count: int
    oversized_line_count: int


class AttentionContext(ApiModel):
    reason: AttentionReason
    event_key: str = Field(pattern=r"^[0-9a-f]{64}$")
    occurred_at: float = Field(ge=0)
    turn_id: str | None
    tool_call_index: int | None = Field(default=None, ge=0)
    tool_name: str | None


class SessionSummary(ApiModel):
    session_key: str
    session_id: str
    project_key: str
    project_root: str
    cwd: str
    originator: str
    source: str
    cli_version: str
    model_provider: str
    status: SessionStatus
    activity_state: ActivityState
    last_event_at: float
    last_progress_at: float
    activity_since: float
    pending_tool_name: str | None
    pending_tool_started_at: float | None
    current_turn_id: str | None
    current_turn_started_at: float | None
    current_user_message_summary: str
    current_action: str
    turn_count: int
    approx_context_chars: int
    attention_reasons: tuple[AttentionReason, ...]
    attention_context: AttentionContext | None
    parse_diagnostics: ParseDiagnostics


class SessionDetail(SessionSummary):
    turns: tuple[TurnView, ...]
    has_earlier: bool
    next_before: int | None


def session_summary(
    session: Session,
    now: float,
    stuck_seconds: int | None,
    data_fresh: bool = True,
) -> SessionSummary:
    current = session.current_turn
    attention_context = _attention_context(session, now, stuck_seconds, data_fresh)
    return SessionSummary(
        session_key=session.session_key,
        session_id=session.session_id,
        project_key=session.project_key,
        project_root=str(session.project_root),
        cwd=session.metadata.cwd,
        originator=session.metadata.originator,
        source=session.metadata.source,
        cli_version=session.metadata.cli_version,
        model_provider=session.metadata.model_provider,
        status=_effective_status(session, now, stuck_seconds),
        activity_state=derive_activity_state(session, now, stuck_seconds, data_fresh),
        last_event_at=session.last_event_at,
        last_progress_at=session.last_progress_at,
        activity_since=session.activity_since,
        pending_tool_name=session.pending_tool_name,
        pending_tool_started_at=session.pending_tool_started_at,
        current_turn_id=None if current is None else current.turn_id,
        current_turn_started_at=None if current is None else current.started_at,
        current_user_message_summary="" if current is None else summarize(current.user_message),
        current_action="" if current is None else _current_action(current),
        turn_count=len(session.turns),
        approx_context_chars=session.approx_context_chars,
        attention_reasons=_attention_reasons(session, now, stuck_seconds, data_fresh),
        attention_context=attention_context,
        parse_diagnostics=ParseDiagnostics(
            unknown_event_count=session.unknown_event_count,
            malformed_line_count=session.malformed_line_count,
            oversized_line_count=session.oversized_line_count,
        ),
    )


def session_detail(  # noqa: PLR0913
    session: Session,
    now: float,
    stuck_seconds: int | None,
    limit: int = 20,
    before: int | None = None,
    anchor_turn_id: str | None = None,
    data_fresh: bool = True,
) -> SessionDetail | None:
    summary = session_summary(session, now, stuck_seconds, data_fresh)
    total = len(session.turns)
    if anchor_turn_id is None:
        end = total if before is None else max(0, min(before, total))
        start = max(0, end - limit)
    else:
        anchor_index = next(
            (
                index
                for index in range(total - 1, -1, -1)
                if str(session.turns[index].turn_id) == anchor_turn_id
            ),
            None,
        )
        if anchor_index is None:
            return None
        start = max(0, min(anchor_index - limit // 2, max(0, total - limit)))
        end = min(total, start + limit)
    return SessionDetail(
        session_key=summary.session_key,
        session_id=summary.session_id,
        project_key=summary.project_key,
        project_root=summary.project_root,
        cwd=summary.cwd,
        originator=summary.originator,
        source=summary.source,
        cli_version=summary.cli_version,
        model_provider=summary.model_provider,
        status=summary.status,
        activity_state=summary.activity_state,
        last_event_at=summary.last_event_at,
        last_progress_at=summary.last_progress_at,
        activity_since=summary.activity_since,
        pending_tool_name=summary.pending_tool_name,
        pending_tool_started_at=summary.pending_tool_started_at,
        current_turn_id=summary.current_turn_id,
        current_turn_started_at=summary.current_turn_started_at,
        current_user_message_summary=summary.current_user_message_summary,
        current_action=summary.current_action,
        turn_count=summary.turn_count,
        approx_context_chars=summary.approx_context_chars,
        attention_reasons=summary.attention_reasons,
        attention_context=summary.attention_context,
        parse_diagnostics=summary.parse_diagnostics,
        turns=tuple(_turn_view(turn, now) for turn in session.turns[start:end]),
        has_earlier=start > 0,
        next_before=start if start > 0 else None,
    )


def _effective_status(session: Session, now: float, stuck_seconds: int | None) -> SessionStatus:
    del now, stuck_seconds
    return session.status


def derive_activity_state(
    session: Session,
    now: float,
    alert_seconds: int | None,
    data_fresh: bool = True,
) -> ActivityState:
    if session.status is not SessionStatus.RUNNING:
        return ActivityState.ACTIVE
    if not data_fresh:
        return ActivityState.DATA_STALE
    pending = _pending_tool(session)
    if pending is not None:
        return (
            ActivityState.LONG_RUNNING_TOOL
            if alert_seconds is not None and now - pending.started_at >= alert_seconds
            else ActivityState.TOOL_RUNNING
        )
    age = max(0.0, now - session.last_progress_at)
    if alert_seconds is not None and age >= alert_seconds:
        return ActivityState.NO_PROGRESS
    return ActivityState.QUIET if age >= QUIET_AFTER_SECONDS else ActivityState.ACTIVE


def _pending_tool(session: Session) -> ToolCall | None:
    current = session.current_turn
    if current is None:
        return None
    for call in current.tool_calls:
        if call.status is ToolStatus.PENDING:
            return call
    return None


def _current_action(turn: Turn) -> str:
    if turn.tool_calls:
        latest = turn.tool_calls[-1]
        return f"{latest.name} · {latest.status.value}"
    if turn.agent_text_snippets:
        return summarize(turn.agent_text_snippets[-1])
    return ""


def _attention_reasons(
    session: Session,
    now: float,
    stuck_seconds: int | None,
    data_fresh: bool = True,
) -> tuple[AttentionReason, ...]:
    reasons: list[AttentionReason] = []
    current = session.current_turn
    if (
        _effective_status(session, now, stuck_seconds) is SessionStatus.STUCK
        and stuck_seconds is not None
    ):
        reasons.append(AttentionReason.STUCK)
    activity = derive_activity_state(session, now, stuck_seconds, data_fresh)
    if activity is ActivityState.LONG_RUNNING_TOOL:
        reasons.append(AttentionReason.LONG_RUNNING_TOOL)
    elif activity is ActivityState.NO_PROGRESS:
        reasons.append(AttentionReason.NO_PROGRESS)
    if current is not None and any(call.status is ToolStatus.ERROR for call in current.tool_calls):
        reasons.append(AttentionReason.TOOL_ERROR)
    if current is not None and current.aborted:
        reasons.append(AttentionReason.TURN_ABORTED)
    return tuple(reasons)


def _event_key(parts: list[str]) -> str:
    canonical = json.dumps(parts, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _attention_context(
    session: Session,
    now: float,
    stuck_seconds: int | None,
    data_fresh: bool = True,
) -> AttentionContext | None:
    candidates: list[tuple[float, int, int, AttentionContext]] = []
    current = session.current_turn

    if current is not None:
        turn_id = str(current.turn_id)
        for index, call in enumerate(current.tool_calls):
            if call.status is not ToolStatus.ERROR:
                continue
            occurred_at = call.ended_at if call.ended_at is not None else call.started_at
            candidates.append(
                (
                    occurred_at,
                    2,
                    index,
                    AttentionContext(
                        reason=AttentionReason.TOOL_ERROR,
                        event_key=_event_key(
                            [
                                "codex-monitor-attention-v1",
                                AttentionReason.TOOL_ERROR.value,
                                session.session_key,
                                turn_id,
                                f"index:{index}",
                            ]
                        ),
                        occurred_at=occurred_at,
                        turn_id=turn_id,
                        tool_call_index=index,
                        tool_name=str(call.name),
                    ),
                )
            )

        if current.aborted:
            occurred_at = current.ended_at if current.ended_at is not None else current.started_at
            candidates.append(
                (
                    occurred_at,
                    1,
                    -1,
                    AttentionContext(
                        reason=AttentionReason.TURN_ABORTED,
                        event_key=_event_key(
                            [
                                "codex-monitor-attention-v1",
                                AttentionReason.TURN_ABORTED.value,
                                session.session_key,
                                turn_id,
                            ]
                        ),
                        occurred_at=occurred_at,
                        turn_id=turn_id,
                        tool_call_index=None,
                        tool_name=None,
                    ),
                )
            )

    if (
        _effective_status(session, now, stuck_seconds) is SessionStatus.STUCK
        and stuck_seconds is not None
    ):
        turn_id = "" if current is None else str(current.turn_id)
        occurred_at = session.last_event_at + stuck_seconds
        candidates.append(
            (
                occurred_at,
                0,
                -1,
                AttentionContext(
                    reason=AttentionReason.STUCK,
                    event_key=_event_key(
                        [
                            "codex-monitor-attention-v1",
                            AttentionReason.STUCK.value,
                            session.session_key,
                            turn_id,
                            float.hex(float(session.last_event_at)),
                        ]
                    ),
                    occurred_at=occurred_at,
                    turn_id=turn_id or None,
                    tool_call_index=None,
                    tool_name=None,
                ),
            )
        )

    activity = derive_activity_state(session, now, stuck_seconds, data_fresh)
    if (
        activity in {ActivityState.LONG_RUNNING_TOOL, ActivityState.NO_PROGRESS}
        and stuck_seconds is not None
    ):
        current_tool = _pending_tool(session)
        occurred_at = session.activity_since + stuck_seconds
        turn_id = None if current is None else str(current.turn_id)
        tool_name = None if current_tool is None else str(current_tool.name)
        reason = (
            AttentionReason.LONG_RUNNING_TOOL
            if activity is ActivityState.LONG_RUNNING_TOOL
            else AttentionReason.NO_PROGRESS
        )
        candidates.append(
            (
                occurred_at,
                1 if reason is AttentionReason.LONG_RUNNING_TOOL else 0,
                -1,
                AttentionContext(
                    reason=reason,
                    event_key=_event_key(
                        [
                            "codex-monitor-attention-v2",
                            reason.value,
                            session.session_key,
                            turn_id or "",
                            tool_name or "",
                            float.hex(float(session.activity_since)),
                        ]
                    ),
                    occurred_at=occurred_at,
                    turn_id=turn_id,
                    tool_call_index=(
                        None
                        if current is None or current_tool is None
                        else next(
                            index
                            for index, call in enumerate(current.tool_calls)
                            if call is current_tool
                        )
                    ),
                    tool_name=tool_name,
                ),
            )
        )

    return max(candidates, key=lambda candidate: candidate[:3])[3] if candidates else None


def _turn_view(turn: Turn, now: float) -> TurnView:
    end = turn.ended_at or now
    return TurnView(
        turn_id=turn.turn_id,
        started_at=turn.started_at,
        ended_at=turn.ended_at,
        duration_seconds=max(0.0, end - turn.started_at),
        user_message=turn.user_message,
        tool_calls=tuple(_tool_view(call) for call in turn.tool_calls),
        agent_text_snippets=tuple(turn.agent_text_snippets),
        aborted=turn.aborted,
        end_reason=turn.end_reason,
    )


def _tool_view(call: ToolCall) -> ToolCallView:
    duration = None if call.ended_at is None else max(0.0, call.ended_at - call.started_at)
    return ToolCallView(
        name=call.name,
        input_summary=call.input_summary,
        status=call.status.value,
        result_summary=call.result_summary,
        started_at=call.started_at,
        ended_at=call.ended_at,
        duration_seconds=duration,
    )


class SnapshotEvent(ApiModel):
    event: Literal["snapshot"] = "snapshot"
    version: int
    protocol_version: Literal[1] = 1
    generated_at: float = Field(default_factory=time.time)
    data: tuple[SessionSummary, ...]


class SessionChangedEvent(ApiModel):
    event: Literal["session_created", "session_updated"]
    version: int
    protocol_version: Literal[1] = 1
    generated_at: float = Field(default_factory=time.time)
    session_key: str
    data: SessionSummary


type RealtimeEvent = SnapshotEvent | SessionChangedEvent
