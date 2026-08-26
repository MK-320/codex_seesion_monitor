from pathlib import Path

import pytest

from codex_monitor.config import AppConfig
from codex_monitor.monitor import Monitor
from codex_monitor.store import SessionStore


@pytest.mark.anyio
async def test_monitor_ignores_file_removed_before_processing(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    monitor = Monitor(
        AppConfig(project_root=project_root, session_root=session_root),
        SessionStore(),
    )

    assert await monitor.process_path(session_root / "gone.jsonl") is None
