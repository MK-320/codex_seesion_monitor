from pathlib import Path

from codex_monitor.events import EventMessageEvent, parse_event
from codex_monitor.models import SessionStatus, ToolStatus
from codex_monitor.parser import apply_event, parse_file

FIXTURES = Path(__file__).parent / "fixtures"


def test_parser_rebuilds_turn_and_tool_call() -> None:
    session = parse_file(FIXTURES / "desktop_session.jsonl")

    assert session is not None
    assert session.session_id == "session-demo"
    assert session.status is SessionStatus.IDLE
    assert len(session.turns) == 1
    assert session.turns[0].user_message == "Inspect the parser."
    assert session.turns[0].tool_calls[0].name == "mcp__demo__read"
    assert session.turns[0].tool_calls[0].status is ToolStatus.SUCCESS
    assert session.turns[0].tool_calls[0].result_summary == '{"ok":true}'
    assert session.approx_context_chars > 0


def test_unknown_and_incomplete_records_are_skipped() -> None:
    assert parse_event(b'{"timestamp":"2026-07-04T03:00:00Z","type":"future"}') is None
    assert parse_event(b'{"timestamp":"2026-07-04T03:00:00Z","type":') is None
    assert (
        parse_event(
            b'{"timestamp":"2026-07-04T03:00:00Z","type":"event_msg",'
            b'"payload":{"type":"user_message"}}'
        )
        is None
    )


def test_tool_error_updates_latest_pending_call() -> None:
    session = parse_file(FIXTURES / "desktop_session.jsonl")
    assert session is not None

    event = parse_event(
        b'{"timestamp":"2026-07-04T03:00:07Z","type":"event_msg",'
        b'"payload":{"type":"agent_message","message":"'
        b"[external_agent_tool_call: demo__write]\\ninput: {}\\n"
        b'[/external_agent_tool_call]"}}'
    )
    assert type(event) is EventMessageEvent
    assert apply_event(session, event)

    error = parse_event(
        b'{"timestamp":"2026-07-04T03:00:08Z","type":"event_msg",'
        b'"payload":{"type":"agent_message","message":"'
        b"[external_agent_tool_result: error]\\nDenied\\n"
        b'[/external_agent_tool_result]"}}'
    )
    assert type(error) is EventMessageEvent
    assert apply_event(session, error)
    assert session.turns[-1].tool_calls[-1].status is ToolStatus.ERROR
    assert session.turns[-1].tool_calls[-1].result_summary == "Denied"


def test_parser_handles_modern_items_without_duplicate_semantics() -> None:
    session = parse_file(FIXTURES / "modern_session.jsonl")

    assert session is not None
    assert session.status is SessionStatus.IDLE
    assert len(session.turns) == 1
    turn = session.turns[0]
    assert turn.user_message == "Inspect modern events."
    assert turn.aborted
    assert turn.end_reason == "interrupted"
    assert turn.agent_text_snippets == ["Modern inspection stopped."]
    assert len(turn.tool_calls) == 1
    assert turn.tool_calls[0].call_id == "call-modern"
    assert turn.tool_calls[0].status is ToolStatus.SUCCESS
    assert turn.tool_calls[0].result_summary == '{"ok":true}'
    assert session.unknown_event_count == 1
    assert session.malformed_line_count == 1


def test_structured_tool_error_sets_error_status() -> None:
    session = parse_file(FIXTURES / "desktop_session.jsonl")
    assert session is not None
    call = parse_event(
        b'{"timestamp":"2026-07-04T03:00:07Z","type":"response_item",'
        b'"payload":{"type":"function_call","name":"demo__write",'
        b'"arguments":"{}","call_id":"call-error"}}'
    )
    result = parse_event(
        b'{"timestamp":"2026-07-04T03:00:08Z","type":"response_item",'
        b'"payload":{"type":"function_call_output","call_id":"call-error",'
        b'"output":{"is_error":true,"content":"Denied"}}}'
    )
    assert call is not None
    assert result is not None

    assert apply_event(session, call)
    assert apply_event(session, result)
    assert session.turns[-1].tool_calls[-1].status is ToolStatus.ERROR
    assert "Denied" in session.turns[-1].tool_calls[-1].result_summary
