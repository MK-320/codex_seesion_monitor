from dataclasses import replace
from pathlib import Path
from typing import cast

import anyio
import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient

import codex_monitor
import codex_monitor.api as api_module
from codex_monitor.api import create_app
from codex_monitor.config import AppConfig, load_saved_project_roots, save_saved_project_roots
from codex_monitor.models import SessionId, SessionMetadata, SessionStatus, Turn, TurnId
from codex_monitor.parser import parse_file
from codex_monitor.schemas import SessionChangedEvent, SnapshotEvent, TraceRevisionEvent
from codex_monitor.store import SessionStore
from codex_monitor.trace_store import TraceStore

FIXTURE = Path(__file__).parent / "fixtures" / "desktop_session.jsonl"
MODERN_FIXTURE = Path(__file__).parent / "fixtures" / "modern_session.jsonl"


def _seeded_store() -> SessionStore:
    session = parse_file(FIXTURE)
    assert session is not None
    store = SessionStore()
    _ = anyio.run(store.put, session)
    return store


def _session_detail_client(
    tmp_path: Path,
    turn_ids: list[str] | None = None,
) -> TestClient:
    session_root = tmp_path / "sessions"
    session_root.mkdir(exist_ok=True)
    session = parse_file(FIXTURE)
    assert session is not None
    ids = turn_ids if turn_ids is not None else [f"turn-{index}" for index in range(100)]
    session.turns = [Turn(TurnId(turn_id), float(index)) for index, turn_id in enumerate(ids)]
    session.status = SessionStatus.IDLE
    store = SessionStore()
    _ = anyio.run(store.put, session)
    return TestClient(
        create_app(AppConfig(project_root=tmp_path, session_root=session_root), store)
    )


def test_session_rest_contract(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    app = create_app(
        AppConfig(project_root=tmp_path, session_root=session_root),
        _seeded_store(),
    )

    with TestClient(app) as client:
        app_config = client.get("/api/config")
        sessions = client.get("/api/sessions")
        detail = client.get("/api/sessions/default:session-demo")
        missing = client.get("/api/sessions/missing")

    assert app_config.json()["projects"][0]["project_root"] == str(tmp_path)
    assert sessions.status_code == 200
    assert sessions.json()[0]["session_id"] == "session-demo"
    assert detail.status_code == 200
    assert detail.json()["turns"][0]["tool_calls"][0]["status"] == "success"
    assert missing.status_code == 404


def test_session_detail_pages_recent_turns_and_loads_earlier(tmp_path: Path) -> None:
    with _session_detail_client(tmp_path) as client:
        recent = client.get("/api/sessions/default:session-demo")
        earlier = client.get("/api/sessions/default:session-demo?limit=20&before=80")

    recent_data = cast("dict[str, object]", recent.json())
    earlier_data = cast("dict[str, object]", earlier.json())
    recent_turns = cast("list[dict[str, object]]", recent_data["turns"])
    earlier_turns = cast("list[dict[str, object]]", earlier_data["turns"])
    assert [turn["turn_id"] for turn in recent_turns] == [
        f"turn-{index}" for index in range(80, 100)
    ]
    assert recent_data["has_earlier"] is True
    assert recent_data["next_before"] == 80
    assert [turn["turn_id"] for turn in earlier_turns] == [
        f"turn-{index}" for index in range(60, 80)
    ]
    assert earlier_data["has_earlier"] is True


def test_session_detail_anchor_centers_requested_turn(tmp_path: Path) -> None:
    with _session_detail_client(tmp_path) as client:
        response = client.get(
            "/api/sessions/default:session-demo",
            params={"anchor_turn_id": "turn-50"},
        )

    assert response.status_code == 200
    data = cast("dict[str, object]", response.json())
    turns = cast("list[dict[str, object]]", data["turns"])
    assert [turn["turn_id"] for turn in turns] == [f"turn-{index}" for index in range(40, 60)]
    assert data["has_earlier"] is True
    assert data["next_before"] == 40


@pytest.mark.parametrize(
    ("turn_ids", "anchor_turn_id", "expected"),
    [
        (
            [f"turn-{index}" for index in range(100)],
            "turn-2",
            ([f"turn-{index}" for index in range(20)], False, None),
        ),
        (
            [f"turn-{index}" for index in range(100)],
            "turn-98",
            ([f"turn-{index}" for index in range(80, 100)], True, 80),
        ),
        (
            [f"short-{index}" for index in range(8)],
            "short-3",
            ([f"short-{index}" for index in range(8)], False, None),
        ),
    ],
)
def test_session_detail_anchor_handles_boundaries_and_short_sessions(
    tmp_path: Path,
    turn_ids: list[str],
    anchor_turn_id: str,
    expected: tuple[list[str], bool, int | None],
) -> None:
    expected_turn_ids, has_earlier, next_before = expected
    with _session_detail_client(tmp_path, turn_ids) as client:
        response = client.get(
            "/api/sessions/default:session-demo",
            params={"anchor_turn_id": anchor_turn_id},
        )

    assert response.status_code == 200
    data = cast("dict[str, object]", response.json())
    turns = cast("list[dict[str, object]]", data["turns"])
    assert [turn["turn_id"] for turn in turns] == expected_turn_ids
    assert data["has_earlier"] is has_earlier
    assert data["next_before"] == next_before


def test_session_detail_anchor_uses_last_duplicate_turn_id(tmp_path: Path) -> None:
    turn_ids = [f"turn-{index}" for index in range(100)]
    turn_ids[20] = "duplicate-turn"
    turn_ids[80] = "duplicate-turn"

    with _session_detail_client(tmp_path, turn_ids) as client:
        response = client.get(
            "/api/sessions/default:session-demo",
            params={"anchor_turn_id": "duplicate-turn"},
        )

    assert response.status_code == 200
    turns = cast("list[dict[str, object]]", response.json()["turns"])
    assert [turn["turn_id"] for turn in turns] == turn_ids[70:90]


def test_session_detail_anchor_preserves_not_found_errors(tmp_path: Path) -> None:
    with _session_detail_client(tmp_path) as client:
        missing_anchor = client.get(
            "/api/sessions/default:session-demo",
            params={"anchor_turn_id": "missing-turn"},
        )
        missing_session = client.get(
            "/api/sessions/missing",
            params={"anchor_turn_id": "turn-50"},
        )

    assert missing_anchor.status_code == 404
    assert missing_anchor.json() == {"detail": "Attention anchor not found"}
    assert missing_session.status_code == 404
    assert missing_session.json() == {"detail": "Session not found"}


@pytest.mark.parametrize(
    "params",
    [
        {"before": "80", "anchor_turn_id": "turn-50"},
        {"anchor_turn_id": ""},
        {"anchor_turn_id": "x" * 257},
    ],
)
def test_session_detail_anchor_rejects_conflicts_and_invalid_lengths(
    tmp_path: Path,
    params: dict[str, str],
) -> None:
    with _session_detail_client(tmp_path) as client:
        response = client.get("/api/sessions/default:session-demo", params=params)

    assert response.status_code == 422


def test_api_scopes_same_session_id_to_distinct_projects(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    alpha.mkdir()
    beta.mkdir()
    first = parse_file(FIXTURE, project_key="alpha", project_root=alpha)
    second = parse_file(FIXTURE, project_key="beta", project_root=beta)
    assert first is not None
    assert second is not None
    store = SessionStore()
    _ = anyio.run(store.put, first)
    _ = anyio.run(store.put, second)
    app = create_app(AppConfig(project_roots=(alpha, beta), session_root=session_root), store)

    with TestClient(app) as client:
        config = cast("dict[str, list[dict[str, str]]]", client.get("/api/config").json())
        sessions = cast("list[dict[str, str]]", client.get("/api/sessions").json())

    assert [project["project_key"] for project in config["projects"]] == ["alpha", "beta"]
    assert {session["session_key"] for session in sessions} == {
        "alpha:session-demo",
        "beta:session-demo",
    }
    assert {session["project_root"] for session in sessions} == {str(alpha), str(beta)}


def test_app_exposes_current_version(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()

    app = create_app(AppConfig(project_root=tmp_path, session_root=session_root))

    assert app.version == codex_monitor.__version__


def test_websocket_sends_typed_initial_snapshot(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    store = _seeded_store()
    app = create_app(
        AppConfig(project_root=tmp_path, session_root=session_root),
        store,
    )

    with TestClient(app) as client, client.websocket_connect("/ws") as websocket:
        event = SnapshotEvent.model_validate_json(websocket.receive_text())

    assert event.event == "snapshot"
    assert event.version == store.version
    assert event.protocol_version == 1
    assert event.generated_at > 0
    assert event.data[0].session_id == "session-demo"


def test_api_exposes_structured_attention_and_parse_diagnostics(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    session = parse_file(MODERN_FIXTURE)
    assert session is not None
    store = SessionStore()
    _ = anyio.run(store.put, session)
    app = create_app(AppConfig(project_root=tmp_path, session_root=session_root), store)

    with TestClient(app) as client:
        summary = cast("list[dict[str, object]]", client.get("/api/sessions").json())[0]

    assert summary["attention_reasons"] == ["turn_aborted"]
    assert summary["parse_diagnostics"] == {
        "unknown_event_count": 1,
        "malformed_line_count": 1,
        "oversized_line_count": 0,
    }


def test_api_exposes_paginated_lossless_traces_and_search_modes(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    session = parse_file(MODERN_FIXTURE)
    assert session is not None
    store = SessionStore()
    _ = anyio.run(store.put, session)
    app = create_app(AppConfig(project_root=tmp_path, session_root=session_root), store)

    with TestClient(app) as client:
        page = client.get(
            "/api/sessions/default:session-modern/traces",
            params={"limit": 2},
        )
        full_match = client.get(
            "/api/sessions/default:session-modern/traces",
            params={"query": "README.md"},
        )
        metadata_match = client.get(
            "/api/sessions/default:session-modern/traces",
            params={"query": "README.md", "metadata_only": "true"},
        )

    assert page.status_code == 200
    page_data = cast("dict[str, object]", page.json())
    assert page_data["total"] == 13
    assert len(cast("list[object]", page_data["events"])) == 2
    assert page_data["has_earlier"] is True
    assert full_match.json()["total"] == 2
    assert metadata_match.json()["total"] == 0


def test_api_manages_trace_storage_and_exports_raw_event(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    session = parse_file(MODERN_FIXTURE)
    assert session is not None
    store = SessionStore()
    _ = anyio.run(store.put, session)
    app = create_app(
        AppConfig(project_root=tmp_path, session_root=session_root, trace_root=tmp_path / "traces"),
        store,
    )
    with TestClient(app) as client:
        status_response = client.get("/api/traces/status")
        event = next(item for item in session.trace_events if item.event_kind == "function_call")
        raw = client.get(f"/api/sessions/{session.session_key}/traces/{event.trace_id}")
        content = client.get(
            f"/api/sessions/{session.session_key}/traces/{event.trace_id}/content",
            params={"content": "raw", "offset": 0, "length": 32},
        )
        exported = client.get(f"/api/sessions/{session.session_key}/traces/export")
        updated = client.put(
            "/api/traces/settings",
            json={"enabled": True, "retention_days": 30},
        )
        cleared = client.post("/api/traces/clear")
    assert status_response.status_code == 200
    assert status_response.json()["file_count"] == 1
    assert raw.status_code == 200
    assert raw.json()["trace_id"] == event.trace_id
    assert raw.json()["input_value"] is None
    assert content.status_code == 200
    assert content.json()["has_more"] is True
    assert exported.status_code == 200
    assert exported.headers["content-type"].startswith("application/json")
    assert updated.status_code == 200
    assert updated.json()["retention_days"] == 30
    assert cleared.status_code == 200
    assert cleared.json()["file_count"] == 0


def test_api_reads_persisted_trace_events_when_session_is_not_loaded(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    session = parse_file(MODERN_FIXTURE)
    assert session is not None
    trace_root = tmp_path / "traces"
    TraceStore(trace_root).save_session(session)
    store = SessionStore()
    app = create_app(
        AppConfig(project_root=tmp_path, session_root=session_root, trace_root=trace_root),
        store,
    )
    event = next(item for item in session.trace_events if item.event_kind == "function_call")

    with TestClient(app) as client:
        page = client.get(
            f"/api/sessions/{session.session_key}/traces",
            params={"limit": 1},
        )
        raw = client.get(f"/api/sessions/{session.session_key}/traces/{event.trace_id}")
        content = client.get(
            f"/api/sessions/{session.session_key}/traces/{event.trace_id}/content",
            params={"content": "raw", "offset": 0, "length": 64},
        )
        exported = client.get(f"/api/sessions/{session.session_key}/traces/export")

    assert page.status_code == 200
    assert page.json()["total"] == len(session.trace_events)
    assert raw.status_code == 200
    assert raw.json()["trace_id"] == event.trace_id
    assert content.status_code == 200
    assert content.json()["trace_id"] == event.trace_id
    assert exported.status_code == 200
    assert exported.json()["events"]


def test_trace_store_persists_fts_index_and_searches_full_content(tmp_path: Path) -> None:
    session = parse_file(MODERN_FIXTURE)
    assert session is not None
    trace_store = TraceStore(tmp_path / "traces")
    trace_store.save_session(session)

    matches = trace_store.search_events(
        session.session_key,
        query="README.md",
    )
    metadata_matches = trace_store.search_events(
        session.session_key,
        query="README.md",
        metadata_only=True,
    )

    assert matches
    assert any("README.md" in str(event.input_value) for event in matches)
    assert metadata_matches == []
    assert (tmp_path / "traces" / "traces.sqlite3").is_file()


def test_trace_store_clear_removes_index_without_touching_source(tmp_path: Path) -> None:
    session = parse_file(MODERN_FIXTURE)
    assert session is not None
    source = tmp_path / "source.jsonl"
    _ = source.write_text("source", encoding="utf-8")
    trace_store = TraceStore(tmp_path / "traces")
    trace_store.save_session(session)

    _ = trace_store.clear()

    assert source.read_text(encoding="utf-8") == "source"
    assert trace_store.search_events(session.session_key) == []


def test_trace_api_keyset_cursor_rejects_changed_filters(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    session = parse_file(MODERN_FIXTURE)
    assert session is not None
    store = SessionStore()
    trace_store = TraceStore(tmp_path / "traces")
    store.attach_trace_store(trace_store)
    _ = anyio.run(store.put, session)
    app = create_app(
        AppConfig(project_root=tmp_path, session_root=session_root, trace_root=tmp_path / "traces"),
        store,
    )

    with TestClient(app) as client:
        first = client.get(
            f"/api/sessions/{session.session_key}/traces",
            params={"limit": 2},
        )
        first_data = cast("dict[str, object]", first.json())
        cursor_value = first_data.get("next_cursor")
        assert isinstance(cursor_value, str)
        cursor = cursor_value
        second = client.get(
            f"/api/sessions/{session.session_key}/traces",
            params={"limit": 2, "cursor": cursor},
        )
        changed = client.get(
            f"/api/sessions/{session.session_key}/traces",
            params={"limit": 2, "cursor": cursor, "event_kind": "function_call"},
        )
        conflicting = client.get(
            f"/api/sessions/{session.session_key}/traces",
            params={"limit": 2, "before": 1, "cursor": cursor},
        )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["events"]
    assert changed.status_code == 409
    assert conflicting.status_code == 409


def test_cross_session_trace_search_returns_metadata_only_results(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    session = parse_file(MODERN_FIXTURE)
    assert session is not None
    store = SessionStore()
    trace_store = TraceStore(tmp_path / "traces")
    store.attach_trace_store(trace_store)
    _ = anyio.run(store.put, session)
    app = create_app(
        AppConfig(project_root=tmp_path, session_root=session_root, trace_root=tmp_path / "traces"),
        store,
    )

    with TestClient(app) as client:
        response = client.post(
            "/api/traces/search",
            json={"query": "mcp__demo__read", "metadata_only": True, "limit": 10},
        )
        invalid_cursor = client.post(
            "/api/traces/search",
            json={"query": "mcp__demo__read", "metadata_only": True, "cursor": "invalid"},
        )

    assert response.status_code == 200
    data = cast("dict[str, object]", response.json())
    results = cast("list[dict[str, object]]", data["results"])
    first_result = results[0]
    first_event = cast("dict[str, object]", first_result["event"])
    assert data["total"] == 1
    assert first_result["session_key"] == session.session_key
    assert first_event["input_value"] is None
    assert invalid_cursor.status_code == 409


def test_api_controls_trace_backfill_without_touching_source_logs(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    store = SessionStore()
    store.attach_trace_store(TraceStore(tmp_path / "traces"))
    app = create_app(AppConfig(project_root=tmp_path, session_root=session_root), store)

    store.begin_trace_backfill(3)
    with TestClient(app) as client:
        paused = client.post("/api/traces/backfill/pause")
        resumed = client.post("/api/traces/backfill/resume")
        status = client.get("/api/traces/status")
        rebuilt = client.post("/api/traces/rebuild")

    assert paused.status_code == 200
    assert paused.json()["backfill"] == {
        "state": "paused",
        "processed": 0,
        "total": 3,
        "error": None,
    }
    assert resumed.status_code == 200
    assert resumed.json()["backfill_state"] in {"building", "complete"}
    assert status.status_code == 200
    assert status.json()["file_count"] == 0
    assert rebuilt.status_code == 200


def test_attention_context_matches_across_rest_and_websocket_v1(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    session = parse_file(MODERN_FIXTURE)
    assert session is not None
    store = SessionStore()
    _ = anyio.run(store.put, session)
    app = create_app(AppConfig(project_root=tmp_path, session_root=session_root), store)

    with TestClient(app) as client, client.websocket_connect("/ws") as websocket:
        snapshot = cast("dict[str, object]", websocket.receive_json())
        session_list = cast("list[dict[str, object]]", client.get("/api/sessions").json())
        list_summary = session_list[0]
        detail = cast(
            "dict[str, object]", client.get("/api/sessions/default:session-modern").json()
        )
        assert client.portal is not None
        _ = client.portal.call(store.put, session)
        update = cast("dict[str, object]", websocket.receive_json())

    snapshot_summary = cast("list[dict[str, object]]", snapshot["data"])[0]
    update_summary = cast("dict[str, object]", update["data"])
    payloads = (list_summary, detail, snapshot_summary, update_summary)

    assert snapshot["protocol_version"] == update["protocol_version"] == 1
    assert all("attention_context" in payload for payload in payloads)
    contexts = [payload["attention_context"] for payload in payloads]
    assert contexts[0] is not None
    assert all(context == contexts[0] for context in contexts[1:])
    context = cast("dict[str, object]", contexts[0])
    assert set(context) == {
        "event_key",
        "reason",
        "occurred_at",
        "turn_id",
        "tool_call_index",
        "tool_name",
    }
    assert "call_id" not in context
    assert "error_summary" not in context


def test_health_contract_is_non_sensitive(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    store = _seeded_store()
    app = create_app(AppConfig(project_root=tmp_path, session_root=session_root), store)

    with TestClient(app) as client:
        health = cast("dict[str, object]", client.get("/api/health").json())

    assert health["version"] == codex_monitor.__version__
    assert health["listen_host"] == "127.0.0.1"
    assert health["project_count"] == 1
    assert health["session_count"] == 1
    assert health["websocket_clients"] == 0
    assert isinstance(health["known_file_count"], int)
    assert isinstance(health["reconciliation_count"], int)
    assert isinstance(health["coalesced_event_count"], int)
    assert health["last_reconciliation_at"] is None or isinstance(
        health["last_reconciliation_at"], float
    )
    assert "project_root" not in health
    assert "user_message" not in health


def test_browser_security_and_cache_headers(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    app = create_app(AppConfig(project_root=tmp_path, session_root=session_root))

    with TestClient(app) as client:
        dashboard = client.get("/")
        health = client.get("/api/health")
        index = Path("src/codex_monitor/static/index.html").read_text(encoding="utf-8")
        asset_path = index.split("/static/", 1)[1].split('"', 1)[0]
        asset = client.get(f"/static/{asset_path}")

    for response in (dashboard, health, asset):
        assert response.headers["x-content-type-options"] == "nosniff"
        assert response.headers["referrer-policy"] == "no-referrer"
        assert response.headers["x-frame-options"] == "DENY"
        assert "frame-ancestors 'none'" in response.headers["content-security-policy"]
    assert dashboard.headers["cache-control"] == "no-store"
    assert health.headers["cache-control"] == "no-store"
    assert asset.headers["cache-control"] == "public, max-age=31536000, immutable"


def test_websocket_rejects_cross_origin_but_allows_absent_origin(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    app = create_app(AppConfig(project_root=tmp_path, session_root=session_root), _seeded_store())

    with TestClient(app) as client:
        with client.websocket_connect("/ws") as websocket:
            assert SnapshotEvent.model_validate_json(websocket.receive_text()).event == "snapshot"
        with (
            pytest.raises(WebSocketDisconnect) as rejected,
            client.websocket_connect(
                "/ws",
                headers={"Origin": "https://example.invalid"},
            ) as websocket,
        ):
            _ = websocket.receive_text()

    assert rejected.value.code == 1008


def test_diagnostics_preview_and_export_are_privacy_safe(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    project = tmp_path / "Users" / "alice" / "private-project"
    project.mkdir(parents=True)
    session = parse_file(FIXTURE, project_key="private", project_root=project)
    assert session is not None
    session.metadata = SessionMetadata(
        SessionId("session-secret-123"),
        str(project),
        session.metadata.originator,
        session.metadata.source,
        session.metadata.cli_version,
        session.metadata.model_provider,
    )
    session.turns[0].user_message = "PRIVATE USER BODY"
    session.turns[0].agent_text_snippets = ["Bearer secret-token-123"]
    session.turns[0].tool_calls[0].input_summary = "https://alice:password@example.test"
    session.turns[0].tool_calls[0].result_summary = "PRIVATE TOOL RESULT"
    store = SessionStore()
    _ = anyio.run(store.put, session)
    app = create_app(AppConfig(project_root=project, session_root=session_root), store)

    with TestClient(app) as client:
        preview = client.get("/api/diagnostics")
        rejected = client.post("/api/diagnostics/export", json={"confirmed": False})
        exported = client.post("/api/diagnostics/export", json={"confirmed": True})
        health = client.get("/api/health")

    assert preview.status_code == 200
    assert preview.json()["schema_version"] == 1
    assert preview.json()["health"]["session_count"] == 1
    assert rejected.status_code == 422
    assert exported.status_code == 200
    assert exported.headers["content-disposition"] == (
        'attachment; filename="codex-monitor-diagnostics.json"'
    )
    assert health.status_code == 200
    combined = preview.text + exported.text
    for secret in (
        str(project),
        "session-secret-123",
        "PRIVATE USER BODY",
        "Bearer secret-token-123",
        "alice:password",
        "PRIVATE TOOL RESULT",
    ):
        assert secret not in combined


def test_api_allows_removing_the_only_project(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    app = create_app(AppConfig(session_root=session_root))

    with TestClient(app) as client:
        initial = cast("dict[str, object]", client.get("/api/config").json())
        assert initial["projects"] == []
        imported = client.post(
            "/api/projects",
            json={"project_root": str(tmp_path), "persist": False},
        )
        project_key = cast("str", cast("dict[str, object]", imported.json())["project_key"])
        response = client.delete(f"/api/projects/{project_key}")
        config = cast("dict[str, object]", client.get("/api/config").json())

    assert imported.status_code == 201
    assert response.status_code == 204
    assert tmp_path.is_dir()
    remaining = cast("list[dict[str, object]]", config["projects"])
    assert remaining == []


def test_api_removes_the_only_missing_saved_project_across_restart(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    missing = tmp_path / "missing"
    config_file = tmp_path / "config.json"
    save_saved_project_roots(config_file, (missing,))
    config = AppConfig(session_root=session_root, config_file=config_file)

    with TestClient(create_app(config)) as client:
        projects = cast("dict[str, list[dict[str, object]]]", client.get("/api/config").json())[
            "projects"
        ]
        removed = client.delete(f"/api/projects/{projects[0]['project_key']}")

    with TestClient(create_app(config)) as restarted_client:
        restarted = cast(
            "dict[str, list[dict[str, object]]]", restarted_client.get("/api/config").json()
        )

    assert removed.status_code == 204
    assert load_saved_project_roots(config_file) == ()
    assert restarted["projects"] == []


def test_api_persists_layout_preferences(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    config_file = tmp_path / "config.json"
    config = AppConfig(session_root=session_root, config_file=config_file)

    with TestClient(create_app(config)) as client:
        initial = cast("dict[str, object]", client.get("/api/config").json())
        response = client.put(
            "/api/config/layout",
            json={"project_sidebar_ratio": 0.2, "session_sidebar_ratio": 0.24},
        )
        saved = cast("dict[str, object]", client.get("/api/config").json())

    assert initial["layout"] is None
    assert response.status_code == 204
    assert saved["layout"] == {
        "project_sidebar_ratio": 0.2,
        "session_sidebar_ratio": 0.24,
    }


def test_api_persists_activity_alert_settings_and_rejects_invalid_updates(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    config_file = tmp_path / "config.json"
    config = AppConfig(session_root=session_root, config_file=config_file)

    with TestClient(create_app(config)) as client:
        initial = cast("dict[str, object]", client.get("/api/config").json())
        saved = client.put("/api/config/activity", json={"activity_alert_seconds": 600})
        updated = cast("dict[str, object]", client.get("/api/config").json())
        invalid = client.put("/api/config/activity", json={"activity_alert_seconds": 1})

    assert initial["activity_alert_seconds"] == 300
    assert saved.status_code == 204
    assert updated["activity_alert_seconds"] == 600
    assert invalid.status_code == 422

    persisted = config_file.read_text(encoding="utf-8")
    assert '"activity_alert_seconds": 600' in persisted

    with TestClient(create_app(AppConfig(session_root=session_root))) as client:
        assert (
            client.put("/api/config/activity", json={"activity_alert_seconds": 600}).status_code
            == 422
        )


def test_project_mutations_reject_cross_origin_requests(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    app = create_app(AppConfig(project_root=tmp_path, session_root=session_root))

    with TestClient(app) as client:
        initial = cast("dict[str, object]", client.get("/api/config").json())
        projects = cast("list[dict[str, object]]", initial["projects"])
        project_key = cast("str", projects[0]["project_key"])
        imported = client.post(
            "/api/projects",
            headers={"Origin": "https://example.invalid"},
            json={"project_root": str(tmp_path), "persist": False},
        )
        removed = client.delete(
            f"/api/projects/{project_key}",
            headers={"Origin": "https://example.invalid"},
        )

    assert imported.status_code == 403
    assert removed.status_code == 403


def test_websocket_sends_session_update_after_snapshot(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    store = _seeded_store()
    app = create_app(
        AppConfig(project_root=tmp_path, session_root=session_root),
        store,
    )

    with TestClient(app) as client, client.websocket_connect("/ws") as websocket:
        initial = SnapshotEvent.model_validate_json(websocket.receive_text())
        session = store.get("session-demo")
        assert session is not None
        assert client.portal is not None
        _ = client.portal.call(store.put, session)
        update = SessionChangedEvent.model_validate_json(websocket.receive_text())

    assert initial.event == "snapshot"
    assert update.event == "session_updated"
    assert update.version == initial.version + 1
    assert update.session_key == "default:session-demo"


def test_websocket_sends_trace_revision_without_trace_payload(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    session = parse_file(MODERN_FIXTURE)
    assert session is not None
    store = SessionStore()
    _ = anyio.run(store.put, session)
    app = create_app(AppConfig(project_root=tmp_path, session_root=session_root), store)

    with TestClient(app) as client, client.websocket_connect("/ws") as websocket:
        _ = SnapshotEvent.model_validate_json(websocket.receive_text())
        session.trace_events.append(
            replace(session.trace_events[-1], trace_id="new-trace", sequence=99)
        )
        assert client.portal is not None
        _ = client.portal.call(store.put, session)
        update = TraceRevisionEvent.model_validate_json(websocket.receive_text())

    assert update.event == "trace_revision"
    assert update.session_key == session.session_key
    assert update.data.session_key == session.session_key
    assert update.added_trace_ids
    assert update.updated_trace_ids == ()


def test_websocket_update_uses_project_scoped_key(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    project = tmp_path / "alpha"
    project.mkdir()
    session = parse_file(FIXTURE, project_key="alpha", project_root=project)
    assert session is not None
    store = SessionStore()
    _ = anyio.run(store.put, session)
    app = create_app(AppConfig(project_roots=(project,), session_root=session_root), store)

    with TestClient(app) as client, client.websocket_connect("/ws") as websocket:
        _ = SnapshotEvent.model_validate_json(websocket.receive_text())
        assert client.portal is not None
        _ = client.portal.call(store.put, session)
        update = SessionChangedEvent.model_validate_json(websocket.receive_text())

    assert update.session_key == "alpha:session-demo"


def test_api_imports_a_project_and_returns_its_historical_sessions(tmp_path: Path) -> None:
    initial = tmp_path / "initial"
    imported = tmp_path / "imported"
    initial.mkdir()
    imported.mkdir()
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    session_path = session_root / "imported.jsonl"
    _ = session_path.write_text(
        (
            '{"timestamp":"2026-07-04T03:00:00Z","type":"session_meta",'
            f'"payload":{{"session_id":"imported-session","cwd":"{imported.as_posix()}"}}}}\n'
            '{"timestamp":"2026-07-04T03:00:01Z","type":"event_msg",'
            '"payload":{"type":"task_started","turn_id":"turn","started_at":1783134001}}\n'
        ),
        encoding="utf-8",
    )
    app = create_app(AppConfig(project_root=initial, session_root=session_root))

    with TestClient(app) as client:
        imported_response = client.post("/api/projects", json={"project_root": str(imported)})
        config = cast("dict[str, list[dict[str, str]]]", client.get("/api/config").json())
        sessions = cast("list[dict[str, str]]", client.get("/api/sessions").json())

    assert imported_response.status_code == 201
    assert config["projects"][-1]["project_root"] == str(imported)
    assert sessions, "imported project should expose its historical sessions"
    assert sessions[0]["session_key"] == "imported:imported-session"


def test_api_rejects_invalid_project_import(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    app = create_app(AppConfig(project_root=tmp_path, session_root=session_root))

    with TestClient(app) as client:
        response = client.post("/api/projects", json={"project_root": str(tmp_path / "missing")})

    assert response.status_code == 422


def test_api_persists_and_removes_project_without_deleting_directory(tmp_path: Path) -> None:
    initial = tmp_path / "initial"
    imported = tmp_path / "imported"
    initial.mkdir()
    imported.mkdir()
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    config_file = tmp_path / "config.json"
    app = create_app(
        AppConfig(project_root=initial, session_root=session_root, config_file=config_file)
    )

    with TestClient(app) as client:
        added = client.post(
            "/api/projects",
            json={"project_root": str(imported), "persist": True},
        )
        removed = client.delete(f"/api/projects/{added.json()['project_key']}")
        config = cast("dict[str, list[dict[str, object]]]", client.get("/api/config").json())

    assert added.status_code == 201
    assert removed.status_code == 204
    assert imported.is_dir()
    assert all(project["project_root"] != str(imported) for project in config["projects"])


def test_websocket_sends_snapshot_after_project_import(tmp_path: Path) -> None:
    initial = tmp_path / "initial"
    imported = tmp_path / "imported"
    initial.mkdir()
    imported.mkdir()
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    _ = (session_root / "imported.jsonl").write_text(
        (
            '{"timestamp":"2026-07-04T03:00:00Z","type":"session_meta",'
            f'"payload":{{"session_id":"imported-session","cwd":"{imported.as_posix()}"}}}}\n'
        ),
        encoding="utf-8",
    )
    app = create_app(AppConfig(project_root=initial, session_root=session_root))

    with TestClient(app) as client, client.websocket_connect("/ws") as websocket:
        _ = SnapshotEvent.model_validate_json(websocket.receive_text())
        imported_response = client.post("/api/projects", json={"project_root": str(imported)})
        snapshot = SnapshotEvent.model_validate_json(websocket.receive_text())

    assert imported_response.status_code == 201
    assert snapshot.data, "websocket snapshot should include imported historical sessions"
    assert snapshot.data[0].session_key == "imported:imported-session"


def test_api_picks_and_imports_a_project(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    initial = tmp_path / "initial"
    imported = tmp_path / "imported"
    initial.mkdir()
    imported.mkdir()
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    monkeypatch.setattr(api_module, "pick_project_directory", lambda: imported)
    app = create_app(AppConfig(project_root=initial, session_root=session_root))

    with TestClient(app) as client:
        response = client.post("/api/projects/pick")
        config = cast("dict[str, list[dict[str, str]]]", client.get("/api/config").json())

    assert response.status_code == 201
    assert response.json()["project_root"] == str(imported)
    assert config["projects"][-1]["project_root"] == str(imported)


def test_api_project_picker_cancel_does_not_change_projects(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    monkeypatch.setattr(api_module, "pick_project_directory", lambda: None)
    app = create_app(AppConfig(project_root=tmp_path, session_root=session_root))

    with TestClient(app) as client:
        before = cast("dict[str, list[dict[str, str]]]", client.get("/api/config").json())
        response = client.post("/api/projects/pick")
        after = cast("dict[str, list[dict[str, str]]]", client.get("/api/config").json())

    assert response.status_code == 204
    assert after == before


def test_api_project_picker_rejects_cross_origin_requests(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    monkeypatch.setattr(api_module, "pick_project_directory", lambda: tmp_path)
    app = create_app(AppConfig(project_root=tmp_path, session_root=session_root))

    with TestClient(app) as client:
        response = client.post(
            "/api/projects/pick",
            headers={"Origin": "https://example.invalid"},
        )

    assert response.status_code == 403
