import json
from pathlib import Path

from codex_monitor.events import EventMessageEvent, parse_event
from codex_monitor.models import SessionStatus, ToolStatus, TraceStatus
from codex_monitor.parser import apply_event, parse_file, record_trace_event
from codex_monitor.schemas import trace_page

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
    trace_call = next(
        event for event in session.trace_events if event.event_kind == "function_call"
    )
    trace_result = next(
        event for event in session.trace_events if event.event_kind == "function_call_output"
    )
    assert trace_call.status.value == "succeeded"
    assert trace_call.ended_at == trace_result.started_at
    assert trace_call.related_trace_id == trace_result.trace_id
    assert trace_result.related_trace_id == trace_call.trace_id
    page = trace_page(session)
    assert all(
        event.parallel_batch is None for event in page.events if event.event_kind != "function_call"
    )


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


def test_custom_tool_call_pairs_with_custom_tool_result() -> None:
    session = parse_file(FIXTURES / "desktop_session.jsonl")
    assert session is not None
    call = parse_event(
        b'{"timestamp":"2026-07-04T03:00:07Z","type":"response_item",'
        b'"payload":{"type":"custom_tool_call","name":"demo__custom",'
        b'"input":"{\\"value\\":1}","call_id":"custom-1"}}'
    )
    result = parse_event(
        b'{"timestamp":"2026-07-04T03:00:08Z","type":"response_item",'
        b'"payload":{"type":"custom_tool_call_output","call_id":"custom-1",'
        b'"output":{"ok":true}}}'
    )
    assert call is not None
    assert result is not None

    call_line = (
        b'{"timestamp":"2026-07-04T03:00:07Z","type":"response_item",'
        b'"payload":{"type":"custom_tool_call","name":"demo__custom",'
        b'"input":"{\\"value\\":1}","call_id":"custom-1"}}'
    )
    result_line = (
        b'{"timestamp":"2026-07-04T03:00:08Z","type":"response_item",'
        b'"payload":{"type":"custom_tool_call_output","call_id":"custom-1",'
        b'"output":{"ok":true}}}'
    )
    _ = record_trace_event(session, call, None, call_line)
    _ = record_trace_event(session, result, None, result_line)
    trace_call = session.trace_events[-2]
    trace_result = session.trace_events[-1]
    assert trace_call.event_kind == "custom_tool_call"
    assert trace_result.event_kind == "custom_tool_call_output"
    assert trace_call.related_trace_id == trace_result.trace_id
    assert trace_result.related_trace_id == trace_call.trace_id
    assert trace_call.status is TraceStatus.SUCCEEDED


def test_trace_page_can_filter_tool_discovery_events() -> None:
    session = parse_file(FIXTURES / "modern_session.jsonl")
    assert session is not None
    discovery = parse_event(
        b'{"timestamp":"2026-07-04T04:00:09Z","type":"event_msg",'
        b'"payload":{"type":"tool_search","query":"filesystem"}}'
    )
    assert discovery is not None
    discovery_line = (
        b'{"timestamp":"2026-07-04T04:00:09Z","type":"event_msg",'
        b'"payload":{"type":"tool_search","query":"filesystem"}}'
    )
    _ = record_trace_event(session, discovery, None, discovery_line)

    page = trace_page(session, event_kind="discovery")
    assert page.total == 1
    assert page.events[0].event_kind == "tool_search"


def test_parser_preserves_duplicate_and_out_of_order_call_records(tmp_path: Path) -> None:
    source = tmp_path / "session.jsonl"
    records = [
        {
            "timestamp": "2026-07-04T04:00:00Z",
            "type": "session_meta",
            "payload": {"session_id": "edge", "cwd": "D:\\Projects\\edge"},
        },
        {
            "timestamp": "2026-07-04T04:00:01Z",
            "type": "event_msg",
            "payload": {"type": "task_started", "turn_id": "turn", "started_at": 1783137601},
        },
        {
            "timestamp": "2026-07-04T04:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "demo__a",
                "arguments": "{}",
                "call_id": "a",
            },
        },
        {
            "timestamp": "2026-07-04T04:00:02Z",
            "type": "response_item",
            "payload": {
                "type": "function_call",
                "name": "demo__b",
                "arguments": "{}",
                "call_id": "b",
            },
        },
        {
            "timestamp": "2026-07-04T04:00:03Z",
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "b", "output": {"ok": True}},
        },
        {
            "timestamp": "2026-07-04T04:00:04Z",
            "type": "response_item",
            "payload": {"type": "function_call_output", "call_id": "b", "output": {"ok": True}},
        },
        {
            "timestamp": "2026-07-04T04:00:05Z",
            "type": "event_msg",
            "payload": {"type": "turn_aborted", "turn_id": "turn", "reason": "cancelled"},
        },
    ]
    _ = source.write_text(
        "\n".join(json.dumps(record) for record in records) + "\n", encoding="utf-8"
    )

    session = parse_file(source)

    assert session is not None
    calls = [event for event in session.trace_events if event.event_kind == "function_call"]
    outputs = [
        event for event in session.trace_events if event.event_kind == "function_call_output"
    ]
    assert calls[0].status is TraceStatus.INTERRUPTED
    assert calls[0].related_trace_id is None
    assert calls[1].status is TraceStatus.SUCCEEDED
    assert calls[1].related_trace_id == outputs[0].trace_id
    assert outputs[0].related_trace_id == calls[1].trace_id
    assert outputs[1].related_trace_id is None
    assert outputs[1].parse_state.value == "partially_recognized"
