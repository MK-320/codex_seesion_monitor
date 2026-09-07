import hashlib
import json
import re
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Final, assert_never, cast

from codex_monitor.events import (
    AgentMessagePayload,
    EventMessageEvent,
    FunctionCallOutputPayload,
    FunctionCallPayload,
    KnownEvent,
    MessageContent,
    ResponseItemEvent,
    ResponseMessagePayload,
    SessionMetaEvent,
    TaskCompletePayload,
    TaskStartedPayload,
    ToolDiscoveryPayload,
    TurnAbortedPayload,
    UserMessagePayload,
    parse_event_with_issue,
)
from codex_monitor.models import (
    Session,
    SessionId,
    SessionMetadata,
    SessionStatus,
    ToolCall,
    ToolName,
    ToolStatus,
    TraceEvent,
    TraceParseState,
    TraceStatus,
    Turn,
    TurnId,
)
from codex_monitor.tailer import read_complete_lines

SUMMARY_LIMIT: Final = 300
TOOL_NAMESPACE_DEPTH: Final = 2
TOOL_CALL_PATTERN: Final = re.compile(
    r"\[external_agent_tool_call:\s*([^\]]+)\]\s*"
    r"(?:input:\s*)?(.*?)\s*\[/external_agent_tool_call\]",
    re.DOTALL,
)
TOOL_RESULT_PATTERN: Final = re.compile(
    r"\[external_agent_tool_result(?::\s*(error))?\]\s*"
    r"(.*?)\s*\[/external_agent_tool_result\]",
    re.DOTALL,
)
CALL_EVENT_KINDS: Final[frozenset[str]] = frozenset({"function_call", "custom_tool_call"})
PAIR_EVENT_KINDS: Final[dict[str, str]] = {
    "function_call": "function_call_output",
    "custom_tool_call": "custom_tool_call_output",
    "function_call_output": "function_call",
    "custom_tool_call_output": "custom_tool_call",
}


@dataclass(frozen=True, slots=True)
class _TraceBuildContext:
    session: Session
    event: KnownEvent
    raw_payload: object
    raw_line: bytes
    sequence: int
    source_line: int | None
    source_offset: int | None


def summarize(value: str) -> str:
    normalized = " ".join(value.split())
    return normalized[:SUMMARY_LIMIT]


def record_trace_event(  # noqa: PLR0913
    session: Session,
    event: KnownEvent | None,
    issue: str | None,
    raw_line: bytes,
    source_line: int | None = None,
    source_offset: int | None = None,
) -> TraceEvent:
    sequence = len(session.trace_events)
    raw_payload: object
    try:
        raw_payload = cast("object", json.loads(raw_line))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raw_payload = raw_line.decode("utf-8", errors="replace")
    trace_id = hashlib.sha256(
        session.session_key.encode("utf-8")
        + b"\0"
        + str(sequence).encode("ascii")
        + b"\0"
        + raw_line
    ).hexdigest()
    if event is None:
        trace = TraceEvent(
            trace_id=trace_id,
            provider="codex",
            session_id=session.session_id,
            turn_id=None,
            sequence=sequence,
            event_kind="unknown",
            call_id=None,
            tool_name=None,
            namespace=None,
            status=TraceStatus.UNKNOWN,
            started_at=None,
            ended_at=None,
            input_value=None,
            result_value=None,
            raw_payload=raw_payload,
            source_line=source_line,
            source_offset=source_offset,
            source_type=None,
            parse_state=(
                TraceParseState.UNKNOWN if issue == "unknown" else TraceParseState.INVALID
            ),
        )
    else:
        trace = _trace_from_known_event(
            _TraceBuildContext(
                session=session,
                event=event,
                raw_payload=raw_payload,
                raw_line=raw_line,
                sequence=sequence,
                source_line=source_line,
                source_offset=source_offset,
            )
        )
    session.trace_events.append(trace)
    _reconcile_trace_relationships(session, trace)
    return trace


def _reconcile_trace_relationships(session: Session, trace: TraceEvent) -> None:
    if trace.event_kind == "turn_aborted":
        _mark_incomplete_trace_calls(session, trace, TraceStatus.INTERRUPTED)
        return
    if trace.event_kind == "task_complete":
        _mark_incomplete_trace_calls(session, trace, TraceStatus.UNKNOWN)
        return
    if trace.call_id is None or trace.event_kind not in PAIR_EVENT_KINDS:
        return

    current_index = len(session.trace_events) - 1
    if trace.event_kind in CALL_EVENT_KINDS:
        result_kind = PAIR_EVENT_KINDS[trace.event_kind]
        related_index = next(
            (
                index
                for index in range(current_index - 1, -1, -1)
                if session.trace_events[index].event_kind == result_kind
                and session.trace_events[index].call_id == trace.call_id
                and session.trace_events[index].related_trace_id is None
            ),
            None,
        )
        if related_index is None:
            session.trace_events[current_index] = replace(
                trace,
                parse_state=TraceParseState.PARTIALLY_RECOGNIZED,
            )
            return
        related = session.trace_events[related_index]
        session.trace_events[current_index] = replace(
            trace,
            status=related.status,
            ended_at=related.ended_at,
            result_value=related.result_value,
            related_trace_id=related.trace_id,
        )
        session.trace_events[related_index] = replace(related, related_trace_id=trace.trace_id)
        return

    call_kind = PAIR_EVENT_KINDS[trace.event_kind]
    related_index = next(
        (
            index
            for index in range(current_index - 1, -1, -1)
            if session.trace_events[index].event_kind == call_kind
            and session.trace_events[index].call_id == trace.call_id
            and session.trace_events[index].related_trace_id is None
        ),
        None,
    )
    if related_index is None:
        session.trace_events[current_index] = replace(
            trace,
            parse_state=TraceParseState.PARTIALLY_RECOGNIZED,
        )
        return
    related = session.trace_events[related_index]
    session.trace_events[current_index] = replace(trace, related_trace_id=related.trace_id)
    session.trace_events[related_index] = replace(
        related,
        status=trace.status,
        ended_at=trace.started_at,
        result_value=trace.result_value,
        related_trace_id=trace.trace_id,
    )


def _mark_incomplete_trace_calls(session: Session, marker: TraceEvent, status: TraceStatus) -> None:
    for index, event in enumerate(session.trace_events[:-1]):
        if (
            event.turn_id == marker.turn_id
            and event.event_kind in CALL_EVENT_KINDS
            and event.status is TraceStatus.RUNNING
        ):
            session.trace_events[index] = replace(
                event,
                status=status,
                ended_at=None,
            )


def _trace_from_known_event(context: _TraceBuildContext) -> TraceEvent:  # noqa: C901, PLR0915
    session = context.session
    event = context.event
    raw_payload = context.raw_payload
    sequence = context.sequence
    source_line = context.source_line
    source_offset = context.source_offset
    trace_id = hashlib.sha256(
        session.session_key.encode("utf-8")
        + b"\0"
        + str(sequence).encode("ascii")
        + b"\0"
        + context.raw_line
    ).hexdigest()
    timestamp = event.timestamp.timestamp()
    turn = session.current_turn
    turn_id = None if turn is None else turn.turn_id
    event_kind = "event"
    call_id = None
    tool_name = None
    namespace = None
    status = TraceStatus.UNKNOWN
    input_value = None
    result_value = None
    if isinstance(event, SessionMetaEvent):
        event_kind = "session_meta"
    elif isinstance(event, EventMessageEvent):
        payload = event.payload
        event_kind = payload.type
        match payload:
            case AgentMessagePayload():
                result_value = payload.message
            case UserMessagePayload():
                input_value = payload.message
            case TurnAbortedPayload():
                status = TraceStatus.INTERRUPTED
            case ToolDiscoveryPayload():
                tool_name = payload.name
                input_value = payload.input if payload.input is not None else payload.query
                result_value = (
                    payload.output
                    if payload.output is not None
                    else payload.results
                    if payload.results is not None
                    else payload.tools
                )
            case TaskStartedPayload() | TaskCompletePayload():
                pass
    else:
        payload = event.payload
        event_kind = payload.type
        match payload:
            case ResponseMessagePayload():
                result_value = "\n".join(item.text for item in payload.content)
            case FunctionCallPayload():
                call_id = payload.call_id
                tool_name = payload.name
                namespace = _tool_namespace(payload.name)
                input_value = _json_or_text(payload.arguments or payload.input)
                status = TraceStatus.RUNNING
            case FunctionCallOutputPayload():
                call_id = payload.call_id
                result_value = payload.output
                status = _trace_status_from_output(payload.output)
            case ToolDiscoveryPayload():
                call_id = payload.call_id
                tool_name = payload.name
                input_value = payload.input if payload.input is not None else payload.query
                result_value = (
                    payload.output
                    if payload.output is not None
                    else payload.results
                    if payload.results is not None
                    else payload.tools
                )
                status = TraceStatus.SUCCEEDED if result_value is not None else TraceStatus.RUNNING
    return TraceEvent(
        trace_id=trace_id,
        provider="codex",
        session_id=session.session_id,
        turn_id=turn_id,
        sequence=sequence,
        event_kind=event_kind,
        call_id=call_id,
        tool_name=tool_name,
        namespace=namespace,
        status=status,
        started_at=timestamp,
        ended_at=None if status is TraceStatus.RUNNING else timestamp,
        input_value=input_value,
        result_value=result_value,
        raw_payload=raw_payload,
        source_line=source_line,
        source_offset=source_offset,
        source_type=event.type,
        parse_state=TraceParseState.RECOGNIZED,
    )


def _json_or_text(value: str) -> object:
    if not value:
        return ""
    try:
        return cast("object", json.loads(value))
    except json.JSONDecodeError:
        return value


def _trace_status_from_output(output: object) -> TraceStatus:
    if isinstance(output, dict):
        data = cast("dict[str, object]", output)
        if data.get("is_error") is True or data.get("isError") is True:
            return TraceStatus.FAILED
    return TraceStatus.SUCCEEDED


def _tool_namespace(name: str) -> str | None:
    parts = name.split("__")
    return "__".join(parts[:TOOL_NAMESPACE_DEPTH]) if len(parts) > TOOL_NAMESPACE_DEPTH else None


def create_session(
    event: SessionMetaEvent,
    path: Path,
    project_key: str = "default",
    project_root: Path | None = None,
) -> Session | None:
    raw_session_id = event.payload.session_id or event.payload.id
    if raw_session_id is None:
        return None
    metadata = SessionMetadata(
        session_id=SessionId(raw_session_id),
        cwd=event.payload.cwd,
        originator=event.payload.originator,
        source=event.payload.source,
        cli_version=event.payload.cli_version,
        model_provider=event.payload.model_provider,
    )
    return Session(metadata, path, event.timestamp.timestamp(), project_key, project_root)


def apply_event(session: Session, event: KnownEvent) -> bool:
    match event:
        case SessionMetaEvent():
            return False
        case EventMessageEvent() as message_event:
            return _apply_event_message(session, message_event)
        case ResponseItemEvent() as response_event:
            return _apply_response_item(session, response_event)
        case unreachable:
            assert_never(unreachable)


def _apply_event_message(session: Session, event: EventMessageEvent) -> bool:
    event_at = event.timestamp.timestamp()
    turn = session.current_turn
    match event.payload:
        case TaskStartedPayload(turn_id=turn_id, started_at=started_at):
            session.turns.append(Turn(TurnId(turn_id), started_at or event_at))
            session.status = SessionStatus.RUNNING
        case UserMessagePayload(message=message) if turn is not None:
            if turn.user_message == message:
                return False
            turn.user_message = message
            session.approx_context_chars += len(message)
            session.status = SessionStatus.RUNNING
        case AgentMessagePayload(message=message) if turn is not None:
            if not _apply_agent_message(turn, message, event_at):
                return False
            session.approx_context_chars += len(message)
            session.status = SessionStatus.RUNNING
        case TaskCompletePayload() if turn is not None:
            turn.ended_at = event_at
            session.status = SessionStatus.IDLE
        case TurnAbortedPayload(reason=reason) if turn is not None:
            turn.ended_at = event_at
            turn.aborted = True
            turn.end_reason = reason
            session.status = SessionStatus.IDLE
        case ToolDiscoveryPayload():
            return False
        case (
            UserMessagePayload()
            | AgentMessagePayload()
            | TaskCompletePayload()
            | TurnAbortedPayload()
        ):
            return False
        case unreachable:
            assert_never(unreachable)
    session.last_event_at = event_at
    _mark_progress(session, event_at)
    return True


def _apply_response_item(session: Session, event: ResponseItemEvent) -> bool:
    turn = session.current_turn
    if turn is None:
        return False
    event_at = event.timestamp.timestamp()
    match event.payload:
        case ResponseMessagePayload(role="user", content=content):
            changed = _apply_response_user_message(session, turn, content)
        case ResponseMessagePayload(role="assistant", content=content):
            changed = _apply_response_agent_message(session, turn, content)
        case FunctionCallPayload(
            name=name,
            call_id=call_id,
            arguments=arguments,
            input=input_value,
        ):
            changed = _apply_function_call(
                turn,
                name,
                arguments or input_value,
                call_id,
                event_at,
            )
        case FunctionCallOutputPayload(call_id=call_id, output=output):
            changed = _apply_function_output(turn, call_id, output, event_at)
        case ResponseMessagePayload():
            changed = False
        case ToolDiscoveryPayload():
            changed = False
    if changed:
        session.last_event_at = event_at
        session.status = SessionStatus.RUNNING
        _mark_progress(session, event_at)
    return changed


def _mark_progress(session: Session, event_at: float) -> None:
    turn = session.current_turn
    pending = None if turn is None else _latest_pending_call(turn)
    session.last_progress_at = event_at
    if pending is None:
        session.pending_tool_name = None
        session.pending_tool_started_at = None
        session.activity_since = event_at
        return
    pending_name = ToolName(pending.name)
    if (
        session.pending_tool_name != pending_name
        or session.pending_tool_started_at != pending.started_at
    ):
        session.activity_since = pending.started_at
    session.pending_tool_name = pending_name
    session.pending_tool_started_at = pending.started_at


def _apply_response_user_message(
    session: Session,
    turn: Turn,
    content: tuple[MessageContent, ...],
) -> bool:
    message = "\n".join(item.text for item in content)
    if not message or turn.user_message == message:
        return False
    turn.user_message = message
    session.approx_context_chars += len(message)
    return True


def _apply_response_agent_message(
    session: Session,
    turn: Turn,
    content: tuple[MessageContent, ...],
) -> bool:
    snippets = [item.text for item in content]
    additions = [text for text in snippets if text and text not in turn.agent_text_snippets]
    turn.agent_text_snippets.extend(additions)
    session.approx_context_chars += sum(map(len, additions))
    return bool(additions)


def _apply_agent_message(turn: Turn, message: str, event_at: float) -> bool:
    changed = False
    calls = tuple(TOOL_CALL_PATTERN.finditer(message))
    results = tuple(TOOL_RESULT_PATTERN.finditer(message))
    for match in calls:
        changed = (
            _apply_function_call(
                turn,
                match.group(1).strip(),
                match.group(2),
                None,
                event_at,
            )
            or changed
        )
    for match in results:
        tool_call = _latest_pending_call(turn)
        if tool_call is None:
            continue
        tool_call.status = ToolStatus.ERROR if match.group(1) else ToolStatus.SUCCESS
        tool_call.result_summary = summarize(match.group(2))
        tool_call.ended_at = event_at
        changed = True
    if not calls and not results and message not in turn.agent_text_snippets:
        turn.agent_text_snippets.append(message)
        changed = True
    return changed


def _apply_function_call(
    turn: Turn,
    name: str,
    input_value: str,
    call_id: str | None,
    event_at: float,
) -> bool:
    input_summary = summarize(input_value)
    existing = next(
        (
            call
            for call in reversed(turn.tool_calls)
            if (call_id is not None and call.call_id == call_id)
            or (
                call.name == name
                and call.input_summary == input_summary
                and call.started_at == event_at
            )
        ),
        None,
    )
    if existing is not None:
        if existing.call_id is None and call_id is not None:
            existing.call_id = call_id
            return True
        return False
    turn.tool_calls.append(
        ToolCall(
            name=ToolName(name),
            input_summary=input_summary,
            started_at=event_at,
            call_id=call_id,
        )
    )
    return True


def _apply_function_output(turn: Turn, call_id: str, output: object, event_at: float) -> bool:
    tool_call = next(
        (call for call in reversed(turn.tool_calls) if call.call_id == call_id),
        None,
    )
    if tool_call is None:
        return False
    result, status = _structured_output(output)
    if tool_call.status is status and tool_call.result_summary == result:
        return False
    tool_call.status = status
    tool_call.result_summary = result
    tool_call.ended_at = event_at
    return True


def _structured_output(output: object) -> tuple[str, ToolStatus]:
    status = ToolStatus.SUCCESS
    value = output
    if isinstance(output, dict):
        mapping = cast("dict[str, object]", output)
        status = (
            ToolStatus.ERROR
            if mapping.get("is_error") is True or mapping.get("isError") is True
            else ToolStatus.SUCCESS
        )
        value = mapping.get("content", mapping)
    serialized = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    return summarize(serialized), status


def _latest_pending_call(turn: Turn) -> ToolCall | None:
    return next(
        (call for call in reversed(turn.tool_calls) if call.status is ToolStatus.PENDING),
        None,
    )


def parse_file(
    path: Path,
    project_key: str = "default",
    project_root: Path | None = None,
) -> Session | None:
    batch = read_complete_lines(path, 0)
    lines = batch.lines
    if not lines:
        return None
    first_event, first_issue = parse_event_with_issue(lines[0])
    match first_event:
        case SessionMetaEvent() as metadata_event:
            session = create_session(metadata_event, path, project_key, project_root)
        case EventMessageEvent() | ResponseItemEvent() | None:
            return None
        case unreachable:
            assert_never(unreachable)
    if session is None:
        return None
    source_offset = batch.line_offsets[0] if batch.line_offsets else 0
    _ = record_trace_event(
        session, first_event, first_issue, lines[0], source_line=1, source_offset=source_offset
    )
    for source_line, raw_line in enumerate(lines[1:], start=2):
        line_index = source_line - 1
        current_offset = (
            batch.line_offsets[line_index]
            if line_index < len(batch.line_offsets)
            else source_offset + len(lines[line_index - 1]) + 1
        )
        event, issue = parse_event_with_issue(raw_line)
        if event is not None:
            _ = apply_event(session, event)
            _ = record_trace_event(
                session,
                event,
                issue,
                raw_line,
                source_line=source_line,
                source_offset=current_offset,
            )
        elif issue == "unknown":
            session.unknown_event_count += 1
            _ = record_trace_event(
                session,
                None,
                issue,
                raw_line,
                source_line=source_line,
                source_offset=current_offset,
            )
        elif issue == "malformed":
            session.malformed_line_count += 1
            _ = record_trace_event(
                session,
                None,
                issue,
                raw_line,
                source_line=source_line,
                source_offset=current_offset,
            )
        source_offset = current_offset + len(raw_line) + 1
    session.oversized_line_count = batch.skipped_oversized
    session.byte_offset = batch.offset
    return session
