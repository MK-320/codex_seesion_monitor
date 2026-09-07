import json
import time
from dataclasses import replace
from pathlib import Path
from typing import cast

import anyio

from codex_monitor.models import TraceParseState, TraceStatus
from codex_monitor.parser import parse_file
from codex_monitor.store import SessionStore
from codex_monitor.trace_store import TraceStore

FIXTURES = Path(__file__).parent / "fixtures"


def test_parse_file_keeps_lossless_structured_trace_events() -> None:
    session = parse_file(FIXTURES / "modern_session.jsonl")

    assert session is not None
    assert len(session.trace_events) == 13
    calls = [event for event in session.trace_events if event.event_kind == "function_call"]
    results = [
        event for event in session.trace_events if event.event_kind == "function_call_output"
    ]
    assert len(calls) == 1
    assert calls[0].call_id == "call-modern"
    assert calls[0].tool_name == "mcp__demo__read"
    assert calls[0].namespace == "mcp__demo"
    assert calls[0].input_value == {"path": "README.md"}
    assert calls[0].status is TraceStatus.SUCCEEDED
    assert calls[0].ended_at == results[0].started_at
    assert calls[0].related_trace_id == results[0].trace_id
    assert len(results) == 1
    assert results[0].result_value == '{"ok":true}'
    assert results[0].status is TraceStatus.SUCCEEDED
    assert results[0].related_trace_id == calls[0].trace_id
    assert [event.sequence for event in session.trace_events] == list(range(13))


def test_parse_file_retains_unknown_and_malformed_trace_records() -> None:
    session = parse_file(FIXTURES / "modern_session.jsonl")

    assert session is not None
    unknown = [
        event for event in session.trace_events if event.parse_state is TraceParseState.UNKNOWN
    ]
    invalid = [
        event for event in session.trace_events if event.parse_state is TraceParseState.INVALID
    ]
    assert len(unknown) == 1
    assert unknown[0].event_kind == "unknown"
    assert unknown[0].raw_payload == {
        "timestamp": "2026-07-04T04:00:07.000Z",
        "type": "future_event",
        "payload": {},
    }
    assert len(invalid) == 1
    assert invalid[0].event_kind == "unknown"
    assert isinstance(invalid[0].raw_payload, str)


def test_trace_store_persists_reports_usage_and_exports(tmp_path: Path) -> None:
    session = parse_file(FIXTURES / "modern_session.jsonl")
    assert session is not None
    trace_store = TraceStore(tmp_path / "traces", retention_days=1)
    trace_store.save_session(session)
    assert trace_store.file_count() == 1
    assert trace_store.usage_bytes() > 0
    exported = trace_store.export_session(session)
    assert b'"schema_version": 1' in exported
    assert b'"provider": "codex"' in exported
    assert trace_store.clear() == 1
    assert trace_store.file_count() == 0


def test_trace_store_streams_export_without_loading_all_events(tmp_path: Path) -> None:
    session = parse_file(FIXTURES / "modern_session.jsonl")
    assert session is not None
    trace_store = TraceStore(tmp_path / "traces")
    trace_store.save_session(session)

    stream = trace_store.export_session_stream(session.session_key, str(session.session_id))
    assert stream is not None
    exported_value = cast("object", json.loads(b"".join(stream)))
    assert isinstance(exported_value, dict)
    exported = cast("dict[str, object]", exported_value)
    assert exported["schema_version"] == 1
    assert exported["session_key"] == session.session_key
    events_value = exported["events"]
    assert isinstance(events_value, list)
    events = cast("list[object]", events_value)
    assert len(events) == len(session.trace_events)


def test_trace_store_rewrites_when_an_existing_event_changes(tmp_path: Path) -> None:
    session = parse_file(FIXTURES / "modern_session.jsonl")
    assert session is not None
    trace_store = TraceStore(tmp_path / "traces")
    trace_store.save_session(session)
    session.trace_events[0] = replace(session.trace_events[0], raw_payload={"changed": True})

    trace_store.save_session(session)

    restored = trace_store.read_trace_events(session.session_key)
    assert restored[0].raw_payload == {"changed": True}


def test_clear_prevents_restart_backfill_until_manual_rebuild(tmp_path: Path) -> None:
    session = parse_file(FIXTURES / "modern_session.jsonl")
    assert session is not None
    trace_store = TraceStore(tmp_path / "traces")
    store = SessionStore()
    store.attach_trace_store(trace_store)
    _ = anyio.run(store.put, session)

    _ = store.clear_traces()
    restarted = SessionStore()
    restarted.attach_trace_store(trace_store)
    reloaded = parse_file(FIXTURES / "modern_session.jsonl")
    assert reloaded is not None
    _ = anyio.run(restarted.put, reloaded)
    assert reloaded.trace_events == []

    _ = restarted.rebuild_trace_history()
    rebuilt = parse_file(FIXTURES / "modern_session.jsonl")
    assert rebuilt is not None
    _ = anyio.run(restarted.put, rebuilt)
    assert len(rebuilt.trace_events) == 13


def test_backfill_pause_state_survives_store_restart(tmp_path: Path) -> None:
    root = tmp_path / "traces"
    first = TraceStore(root)
    first.begin_backfill(4)
    first.advance_backfill()
    first.pause_backfill()

    restarted = TraceStore(root)
    restarted.begin_backfill(4)

    assert restarted.backfill_status() == {
        "state": "paused",
        "processed": 1,
        "total": 4,
        "error": None,
    }
    restarted.resume_backfill()
    assert restarted.backfill_status()["state"] == "building"


def test_trace_store_reads_chunks_and_searches_filters(tmp_path: Path) -> None:
    session = parse_file(FIXTURES / "modern_session.jsonl")
    assert session is not None
    trace_store = TraceStore(tmp_path / "traces")
    trace_store.save_session(session)
    event = next(item for item in session.trace_events if item.event_kind == "function_call")

    rows = trace_store.read_events(session.session_key, limit=2)
    assert len(rows) == 2
    assert trace_store.read_all_events(session.session_key)
    assert trace_store.read_event(session.session_key, event.trace_id) is not None
    assert trace_store.read_event(session.session_key, "missing") is None
    input_chunk = trace_store.read_content_chunk(session.session_key, event.trace_id, "input", 0, 8)
    raw_chunk = trace_store.read_content_chunk(session.session_key, event.trace_id, "raw", 0, 8)
    assert input_chunk is not None
    assert input_chunk[0]
    assert raw_chunk is not None
    assert raw_chunk[0]
    assert trace_store.read_content_chunk(session.session_key, "missing", "raw", 0, 8) is None
    matches = trace_store.search_all_events(
        query="README.md",
        provider="codex",
        turn_id="turn-modern",
        event_kind="function_call",
        status="succeeded",
        tool_name="mcp__demo__read",
    )
    assert [item[1].trace_id for item in matches] == [event.trace_id]
    event_messages = trace_store.search_events(session.session_key, event_kind="event_msg")
    assert event_messages
    assert all(
        item.event_kind
        in {"task_started", "user_message", "agent_message", "task_complete", "turn_aborted"}
        for item in event_messages
    )


def test_trace_store_prune_removes_expired_session_and_database_rows(tmp_path: Path) -> None:
    session = parse_file(FIXTURES / "modern_session.jsonl")
    assert session is not None
    root = tmp_path / "traces"
    trace_store = TraceStore(root, retention_days=1)
    trace_store.save_session(session)
    assert trace_store.prune(now=time.time() + 2 * 86_400) == 1
    assert trace_store.file_count() == 0
    assert trace_store.search_events(session.session_key) == []


def test_trace_store_prune_uses_each_event_timestamp(tmp_path: Path) -> None:
    session = parse_file(FIXTURES / "modern_session.jsonl")
    assert session is not None
    now = time.time()
    session.trace_events[:] = [
        replace(
            event,
            started_at=now - 3 * 86_400 if index == 0 else now,
            ended_at=now - 3 * 86_400 if index == 0 else now,
        )
        for index, event in enumerate(session.trace_events)
    ]
    trace_store = TraceStore(tmp_path / "traces", retention_days=1)
    trace_store.save_session(session)

    assert trace_store.prune(now=now) == 1
    restored = trace_store.read_trace_events(session.session_key)
    assert len(restored) == len(session.trace_events) - 1
    assert all(event.sequence != 0 for event in restored)
    assert trace_store.search_events(session.session_key)
