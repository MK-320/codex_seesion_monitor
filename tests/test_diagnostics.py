import json
from pathlib import Path
from typing import cast

import anyio
from fastapi.testclient import TestClient

from codex_monitor.api import create_app
from codex_monitor.config import AppConfig
from codex_monitor.diagnostics import build_diagnostics
from codex_monitor.parser import parse_file
from codex_monitor.store import SessionStore


def _sensitive_store(tmp_path: Path) -> SessionStore:
    project = tmp_path / "Users" / "Alice" / "private-project"
    project.mkdir(parents=True)
    session_file = tmp_path / "sensitive.jsonl"
    _ = session_file.write_text(
        "\n".join(
            (
                json.dumps(
                    {
                        "timestamp": "2026-07-12T00:00:00Z",
                        "type": "session_meta",
                        "payload": {
                            "session_id": "private-session-id",
                            "cwd": str(project),
                        },
                    }
                ),
                json.dumps(
                    {
                        "timestamp": "2026-07-12T00:00:01Z",
                        "type": "event_msg",
                        "payload": {
                            "type": "user_message",
                            "message": "sk-secret-token https://alice:password@example.test/private",
                        },
                    }
                ),
            )
        )
        + "\n",
        encoding="utf-8",
    )
    session = parse_file(session_file, project_key="private", project_root=project)
    assert session is not None
    store = SessionStore()
    _ = anyio.run(store.put, session)
    return store


def test_diagnostics_only_contains_aggregated_non_sensitive_data(tmp_path: Path) -> None:
    store = _sensitive_store(tmp_path)
    report = build_diagnostics(
        version="4.6.0",
        started_at=100.0,
        project_count=1,
        summaries=store.summaries(200.0, 120),
        health_counts=store.health_counts(),
        monitor_health={
            "known_file_count": 2,
            "last_reconciliation_at": 150.0,
            "reconciliation_count": 3,
            "coalesced_event_count": 99,
        },
        websocket_clients=0,
        stuck_seconds=120,
        now=200.0,
    )
    serialized = json.dumps(report)

    assert report["schema_version"] == 1
    health = cast("dict[str, object]", report["health"])
    assert health["session_count"] == 1
    assert health["known_file_count"] == 2
    assert health["reconciliation_count"] == 3
    assert health["coalesced_event_count"] == 99
    assert "private-session-id" not in serialized
    assert str(tmp_path) not in serialized
    assert "sk-secret-token" not in serialized
    assert "alice:password" not in serialized


def test_diagnostics_api_previews_without_writing_files(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    app = create_app(
        AppConfig(project_root=tmp_path, session_root=session_root),
        _sensitive_store(tmp_path),
    )

    with TestClient(app) as client:
        response = client.get("/api/diagnostics")
        sessions_before = client.get("/api/sessions")

    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-store"
    assert sessions_before.status_code == 200
    assert not list(tmp_path.glob("*diagnostic*"))
    serialized = response.text
    assert "private-session-id" not in serialized
    assert "sk-secret-token" not in serialized
    assert str(tmp_path) not in serialized
