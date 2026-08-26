from pathlib import Path

import pytest
from pydantic import ValidationError

from codex_monitor.config import (
    AppConfig,
    LayoutPreferences,
    load_saved_layout,
    load_saved_project_roots,
    save_saved_layout,
    save_saved_project_roots,
)


def test_config_accepts_existing_local_directories(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()

    config = AppConfig(project_root=tmp_path, session_root=session_root)

    assert config.project_root == tmp_path
    assert config.session_root == session_root
    assert config.host == "127.0.0.1"
    assert config.stuck_seconds == 300


def test_config_accepts_an_empty_project_list(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()

    config = AppConfig(session_root=session_root)

    assert config.projects == ()


def test_config_rejects_missing_project(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()

    with pytest.raises(ValidationError):
        _ = AppConfig(project_root=tmp_path / "missing", session_root=session_root)


def test_config_rejects_public_bind_address(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()

    with pytest.raises(ValidationError):
        _ = AppConfig.model_validate(
            {
                "project_root": tmp_path,
                "session_root": session_root,
                "host": "0.0.0.0",
            }
        )


def test_saved_projects_round_trip_and_corrupt_config_degrades(tmp_path: Path) -> None:
    config_file = tmp_path / "monitor" / "config.json"
    first = tmp_path / "first"
    missing = tmp_path / "missing"
    first.mkdir()

    save_saved_project_roots(config_file, (first, missing))

    assert load_saved_project_roots(config_file) == (first.resolve(), missing.resolve())
    _ = config_file.write_text("not json", encoding="utf-8")
    assert load_saved_project_roots(config_file) == ()


def test_saved_layout_round_trip_preserves_projects(tmp_path: Path) -> None:
    config_file = tmp_path / "monitor" / "config.json"
    project = tmp_path / "project"
    project.mkdir()
    save_saved_project_roots(config_file, (project,))

    layout = LayoutPreferences(project_sidebar_ratio=0.2, session_sidebar_ratio=0.24)
    save_saved_layout(config_file, layout)

    assert load_saved_project_roots(config_file) == (project.resolve(),)
    assert load_saved_layout(config_file) == layout


def test_saved_projects_update_preserves_layout(tmp_path: Path) -> None:
    config_file = tmp_path / "monitor" / "config.json"
    project = tmp_path / "project"
    project.mkdir()
    layout = LayoutPreferences(project_sidebar_ratio=0.18, session_sidebar_ratio=0.22)
    save_saved_layout(config_file, layout)

    save_saved_project_roots(config_file, (project,))

    assert load_saved_layout(config_file) == layout
