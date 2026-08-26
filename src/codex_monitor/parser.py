import json
import re
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
    Turn,
    TurnId,
)
from codex_monitor.tailer import read_complete_lines

SUMMARY_LIMIT: Final = 300
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


def summarize(value: str) -> str:
    normalized = " ".join(value.split())
    return normalized[:SUMMARY_LIMIT]


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
    first_event, _ = parse_event_with_issue(lines[0])
    match first_event:
        case SessionMetaEvent() as metadata_event:
            session = create_session(metadata_event, path, project_key, project_root)
        case EventMessageEvent() | ResponseItemEvent() | None:
            return None
        case unreachable:
            assert_never(unreachable)
    if session is None:
        return None
    for raw_line in lines[1:]:
        event, issue = parse_event_with_issue(raw_line)
        if event is not None:
            _ = apply_event(session, event)
        elif issue == "unknown":
            session.unknown_event_count += 1
        elif issue == "malformed":
            session.malformed_line_count += 1
    session.oversized_line_count = batch.skipped_oversized
    session.byte_offset = batch.offset
    return session
