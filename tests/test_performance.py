import json
import time
from pathlib import Path

from codex_monitor.discovery import SessionDiscovery
from codex_monitor.parser import parse_file


def _write_session(path: Path, session_id: str, cwd: Path) -> None:
    event = {
        "timestamp": "2026-07-04T03:00:00.000Z",
        "type": "session_meta",
        "payload": {
            "session_id": session_id,
            "id": session_id,
            "cwd": str(cwd),
            "originator": "Codex Desktop",
            "cli_version": "0.200.0",
            "source": "fixture",
            "model_provider": "openai",
        },
    }
    _ = path.write_text(json.dumps(event) + "\n", encoding="utf-8")


def test_discovery_handles_2000_files_and_200_targets(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    session_root = tmp_path / "sessions"
    project_root.mkdir()
    session_root.mkdir()
    for index in range(2000):
        cwd = project_root if index < 200 else tmp_path / "other"
        _write_session(session_root / f"session-{index}.jsonl", f"session-{index}", cwd)

    discovery = SessionDiscovery(project_root, session_root)
    started = time.perf_counter()
    paths = discovery.discover()
    sessions = [parse_file(path) for path in paths]
    elapsed = time.perf_counter() - started

    assert len(paths) == 200
    assert all(session is not None for session in sessions)
    assert elapsed < 5
