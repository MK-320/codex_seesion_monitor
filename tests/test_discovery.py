from pathlib import Path

import pytest

from codex_monitor.discovery import SessionDiscovery, is_within_project


def _write_session(path: Path, cwd: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _ = path.write_text(
        (
            '{"timestamp":"2026-07-04T03:00:00Z","type":"session_meta",'
            f'"payload":{{"session_id":"{path.stem}","cwd":"{cwd}"}}}}\n'
            '{"this":"second line is intentionally irrelevant"}\n'
        ),
        encoding="utf-8",
    )


def test_project_boundary_handles_root_children_and_prefix_traps() -> None:
    assert is_within_project("D:/Work/App", "D:/Work/App")
    assert is_within_project("D:/Work/App", "D:/Work/App/web")
    assert is_within_project("D:/Work/App", "d:/work/app/API")
    assert not is_within_project("D:/Work/App", "D:/Work/Application")
    assert not is_within_project("D:/Work/App", "E:/Work/App")


def test_discovery_returns_only_target_project_sessions(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    session_root = tmp_path / "sessions"
    target = session_root / "2026" / "target.jsonl"
    other = session_root / "2026" / "other.jsonl"
    _write_session(target, project_root.as_posix())
    _write_session(other, (tmp_path / "other").as_posix())

    discovery = SessionDiscovery(project_root, session_root)

    assert discovery.discover() == [target]


def test_discovery_caches_non_target_classification(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    session_root = tmp_path / "sessions"
    other = session_root / "other.jsonl"
    _write_session(other, (tmp_path / "other").as_posix())
    discovery = SessionDiscovery(project_root, session_root)
    assert discovery.discover() == []

    _write_session(other, project_root.as_posix())

    assert discovery.discover() == []


def test_discovery_retries_stale_probe_after_concurrent_project_import(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    initial = tmp_path / "initial"
    imported = tmp_path / "imported"
    initial.mkdir()
    imported.mkdir()
    session_root = tmp_path / "sessions"
    session = session_root / "imported.jsonl"
    _write_session(session, imported.as_posix())
    discovery = SessionDiscovery(initial, session_root)
    original_probe = SessionDiscovery._probe  # pyright: ignore[reportPrivateUsage]
    probe_calls = {"count": 0}

    def probe_with_concurrent_import(self: SessionDiscovery, path: Path) -> object:
        probe_calls["count"] += 1
        record = original_probe(self, path)
        if probe_calls["count"] == 1:
            project, added = self.add_project(imported)
            assert added
            assert project.root == imported
        return record

    monkeypatch.setattr(SessionDiscovery, "_probe", probe_with_concurrent_import)

    project = discovery.project_for(session)

    assert project is not None
    assert project.root == imported
    assert discovery.discover() == [session]


def test_discovery_assigns_nested_session_to_the_most_specific_project(tmp_path: Path) -> None:
    parent = tmp_path / "workspace"
    child = parent / "service"
    parent.mkdir()
    child.mkdir()
    session_root = tmp_path / "sessions"
    session = session_root / "nested.jsonl"
    _write_session(session, (child / "api").as_posix())

    discovery = SessionDiscovery((parent, child), session_root)

    project = discovery.project_for(session)
    assert project is not None
    assert project.root == child
