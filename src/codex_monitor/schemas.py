import hashlib
import json
import time
from base64 import urlsafe_b64decode, urlsafe_b64encode
from typing import ClassVar, Literal, cast

from pydantic import BaseModel, ConfigDict, Field

from codex_monitor.models import (
    ActivityState,
    AttentionReason,
    Session,
    SessionStatus,
    ToolCall,
    ToolStatus,
    TraceEvent,
    Turn,
)
from codex_monitor.parser import summarize

QUIET_AFTER_SECONDS = 120
EVENT_MESSAGE_KINDS = frozenset(
    {"task_started", "user_message", "agent_message", "task_complete", "turn_aborted"}
)
DISCOVERY_EVENT_KINDS = frozenset(
    {
        "tool_search",
        "tool_search_result",
        "web_search",
        "web_search_result",
        "available_tools",
        "tool_list",
    }
)


class ApiModel(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)


class _CursorError(ValueError):
    def __init__(self) -> None:
        super().__init__("cursor is invalid or expired")


class _CursorConflictError(ValueError):
    def __init__(self) -> None:
        super().__init__("cursor cannot be combined with before")


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


class TraceEventView(ApiModel):
    trace_id: str
    provider: str
    session_id: str
    turn_id: str | None
    sequence: int
    event_kind: str
    call_id: str | None
    related_trace_id: str | None = None
    tool_name: str | None
    namespace: str | None
    status: str
    started_at: float | None
    ended_at: float | None
    duration_ms: int | None
    input_value: object | None = None
    result_value: object | None = None
    raw_payload: object | None = None
    input_preview: str | None = None
    result_preview: str | None = None
    raw_preview: str | None = None
    content_available: bool = False
    source_line: int | None
    source_offset: int | None
    source_type: str | None
    parse_state: str
    content_metadata: dict[str, object]
    parallel_batch: int | None = None


class TracePage(ApiModel):
    events: tuple[TraceEventView, ...]
    total: int
    has_earlier: bool
    next_before: int | None
    metadata_only: bool
    query: str | None
    next_cursor: str | None = None
    index_state: Literal["ready", "building", "unavailable"] = "ready"
    data_freshness: Literal["live", "historical", "unavailable"] = "live"


class TraceSearchRequest(ApiModel):
    provider: str = "codex"
    session_key: str | None = None
    query: str | None = Field(default=None, max_length=512)
    metadata_only: bool = False
    turn_id: str | None = Field(default=None, max_length=256)
    event_kind: str | None = Field(default=None, max_length=64)
    status: str | None = Field(default=None, max_length=32)
    tool_name: str | None = Field(default=None, max_length=256)
    from_time: float | None = Field(default=None, ge=0)
    to_time: float | None = Field(default=None, ge=0)
    limit: int = Field(default=100, ge=1, le=500)
    cursor: str | None = Field(default=None, max_length=1024)


class TraceSearchResult(ApiModel):
    session_key: str
    event: TraceEventView


class TraceSearchPage(ApiModel):
    results: tuple[TraceSearchResult, ...]
    total: int
    next_cursor: str | None = None
    metadata_only: bool
    query: str | None
    index_state: Literal["ready", "building", "unavailable"] = "ready"


def trace_page(  # noqa: PLR0913
    session: Session,
    limit: int = 100,
    before: int | None = None,
    query: str | None = None,
    metadata_only: bool = False,
    event_kind: str | None = None,
    status: str | None = None,
    tool_name: str | None = None,
    from_time: float | None = None,
    to_time: float | None = None,
    turn_id: str | None = None,
    cursor: str | None = None,
) -> TracePage:
    return trace_page_events(
        session.trace_events,
        limit,
        before,
        query,
        metadata_only,
        event_kind,
        status,
        tool_name,
        from_time,
        to_time,
        turn_id,
        cursor,
    )


def trace_page_events(  # noqa: C901, PLR0912, PLR0913
    events: list[TraceEvent],
    limit: int = 100,
    before: int | None = None,
    query: str | None = None,
    metadata_only: bool = False,
    event_kind: str | None = None,
    status: str | None = None,
    tool_name: str | None = None,
    from_time: float | None = None,
    to_time: float | None = None,
    turn_id: str | None = None,
    cursor: str | None = None,
) -> TracePage:
    normalized_query = query.casefold().strip() if query else None
    matching: list[TraceEvent] = []
    for event in events:
        if normalized_query is not None and not _trace_matches(
            event, normalized_query, metadata_only
        ):
            continue
        if event_kind is not None:
            if event_kind == "event_msg":
                if event.event_kind not in EVENT_MESSAGE_KINDS:
                    continue
            elif event_kind == "discovery":
                if event.event_kind not in DISCOVERY_EVENT_KINDS:
                    continue
            elif event.event_kind != event_kind:
                continue
        if status is not None and event.status.value != status:
            continue
        if tool_name is not None and event.tool_name != tool_name:
            continue
        if turn_id is not None and (event.turn_id is None or str(event.turn_id) != turn_id):
            continue
        if from_time is not None and (event.ended_at or event.started_at or 0) < from_time:
            continue
        if to_time is not None and (event.started_at or event.ended_at or 0) > to_time:
            continue
        matching.append(event)
    if before is not None and cursor is not None:
        raise _CursorConflictError
    if cursor is not None:
        boundary = _decode_trace_cursor(
            cursor,
            events,
            query,
            metadata_only,
            event_kind,
            status,
            tool_name,
            from_time,
            to_time,
            turn_id,
        )
        end = sum(1 for event in matching if event.sequence < boundary)
    else:
        end = len(matching) if before is None else max(0, min(before, len(matching)))
    start = max(0, end - limit)
    selected = matching[start:end]
    parallel_batches = _parallel_batches(events)
    return TracePage(
        events=tuple(
            _trace_view(event, parallel_batches.get(event.trace_id)) for event in selected
        ),
        total=len(matching),
        has_earlier=start > 0,
        next_before=start if start > 0 else None,
        metadata_only=metadata_only,
        query=query,
        next_cursor=(
            None
            if start == 0
            else _encode_trace_cursor(
                matching[start].sequence,
                events,
                query,
                metadata_only,
                event_kind,
                status,
                tool_name,
                from_time,
                to_time,
                turn_id,
            )
        ),
    )


def _trace_cursor_context(  # noqa: PLR0913
    events: list[TraceEvent],
    query: str | None,
    metadata_only: bool,
    event_kind: str | None,
    status: str | None,
    tool_name: str | None,
    from_time: float | None,
    to_time: float | None,
    turn_id: str | None,
) -> dict[str, object]:
    fingerprint = hashlib.sha256(
        "|".join(f"{event.sequence}:{event.trace_id}" for event in events).encode("utf-8")
    ).hexdigest()
    return {
        "v": 1,
        "fingerprint": fingerprint,
        "query": query,
        "metadata_only": metadata_only,
        "event_kind": event_kind,
        "status": status,
        "tool_name": tool_name,
        "from_time": from_time,
        "to_time": to_time,
        "turn_id": turn_id,
    }


def _encode_trace_cursor(  # noqa: PLR0913
    sequence: int,
    events: list[TraceEvent],
    query: str | None,
    metadata_only: bool,
    event_kind: str | None,
    status: str | None,
    tool_name: str | None,
    from_time: float | None,
    to_time: float | None,
    turn_id: str | None,
) -> str:
    payload = _trace_cursor_context(
        events,
        query,
        metadata_only,
        event_kind,
        status,
        tool_name,
        from_time,
        to_time,
        turn_id,
    )
    payload["sequence"] = sequence
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    return urlsafe_b64encode(encoded).decode("ascii").rstrip("=")


def _decode_trace_cursor(  # noqa: PLR0913
    cursor: str,
    events: list[TraceEvent],
    query: str | None,
    metadata_only: bool,
    event_kind: str | None,
    status: str | None,
    tool_name: str | None,
    from_time: float | None,
    to_time: float | None,
    turn_id: str | None,
) -> int:
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        decoded_value = cast("object", json.loads(urlsafe_b64decode(padded).decode("utf-8")))
        if not isinstance(decoded_value, dict):
            raise _CursorError
        decoded = cast("dict[str, object]", decoded_value)
        sequence = decoded.get("sequence")
        if not isinstance(sequence, int):
            raise _CursorError
        expected = _trace_cursor_context(
            events,
            query,
            metadata_only,
            event_kind,
            status,
            tool_name,
            from_time,
            to_time,
            turn_id,
        )
        if any(decoded.get(key) != value for key, value in expected.items()):
            raise _CursorError
        return sequence  # noqa: TRY300
    except (ValueError, TypeError, json.JSONDecodeError):
        raise _CursorError from None


def _parallel_batches(events: list[TraceEvent]) -> dict[str, int]:
    now = time.time()
    timed = sorted(
        (
            event
            for event in events
            if event.started_at is not None
            and event.call_id is not None
            and event.event_kind in {"function_call", "custom_tool_call", "tool_call", "shell"}
        ),
        key=lambda event: (event.started_at or 0, event.sequence),
    )
    batches: dict[str, int] = {}
    batch_number = 0
    component: list[TraceEvent] = []
    component_end = 0.0
    for event in timed:
        start = event.started_at or 0.0
        end = event.ended_at if event.ended_at is not None else now
        if component and start >= component_end:
            if len(component) > 1:
                batch_number += 1
                for member in component:
                    batches[member.trace_id] = batch_number
            component = []
            component_end = 0.0
        component.append(event)
        component_end = max(component_end, end)
    if len(component) > 1:
        batch_number += 1
        for member in component:
            batches[member.trace_id] = batch_number
    return batches


def _trace_matches(event: TraceEvent, query: str, metadata_only: bool) -> bool:
    values: list[object] = [
        event.sequence,
        event.event_kind,
        event.call_id,
        event.tool_name,
        event.namespace,
        event.status.value,
        event.source_type,
        event.parse_state.value,
    ]
    if not metadata_only:
        values.extend((event.input_value, event.result_value, event.raw_payload))
    return query in " ".join(_trace_text(value) for value in values).casefold()


def _trace_text(value: object) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except (TypeError, ValueError):
        return str(value)


def _trace_view(event: TraceEvent, parallel_batch: int | None = None) -> TraceEventView:
    return TraceEventView(
        trace_id=event.trace_id,
        provider=event.provider,
        session_id=event.session_id,
        turn_id=None if event.turn_id is None else str(event.turn_id),
        sequence=event.sequence,
        event_kind=event.event_kind,
        call_id=event.call_id,
        related_trace_id=event.related_trace_id,
        tool_name=event.tool_name,
        namespace=event.namespace,
        status=event.status.value,
        started_at=event.started_at,
        ended_at=event.ended_at,
        duration_ms=event.duration_ms,
        input_preview=_trace_preview(event.input_value),
        result_preview=_trace_preview(event.result_value),
        raw_preview=_trace_preview(event.raw_payload),
        content_available=any(
            value is not None
            for value in (event.input_value, event.result_value, event.raw_payload)
        ),
        source_line=event.source_line,
        source_offset=event.source_offset,
        source_type=event.source_type,
        parse_state=event.parse_state.value,
        content_metadata=event.content_metadata,
        parallel_batch=parallel_batch,
    )


def _trace_preview(value: object | None, limit: int = 240) -> str | None:
    if value is None:
        return None
    text = value if isinstance(value, str) else _trace_text(value)
    return text if len(text) <= limit else f"{text[:limit]}…"


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


class TraceRevisionEvent(ApiModel):
    event: Literal["trace_revision"] = "trace_revision"
    version: int
    protocol_version: Literal[1] = 1
    generated_at: float = Field(default_factory=time.time)
    session_key: str
    data: SessionSummary
    provider: str = "codex"
    added_trace_ids: tuple[str, ...] = ()
    updated_trace_ids: tuple[str, ...] = ()
    last_sequence: int | None = None


type RealtimeEvent = SnapshotEvent | SessionChangedEvent | TraceRevisionEvent
