import re
from pathlib import Path
from queue import SimpleQueue

import anyio
import pytest

from codex_monitor.config import AppConfig
from codex_monitor.discovery import SessionDiscovery
from codex_monitor.models import (
    ActivityState,
    AttentionReason,
    SessionStatus,
    ToolCall,
    ToolName,
    ToolStatus,
)
from codex_monitor.monitor import Monitor
from codex_monitor.store import ChangeKind, SessionStore


def _session_meta(project_root: Path) -> str:
    cwd = str(project_root).replace("\\", "\\\\")
    return (
        '{"timestamp":"2026-07-04T03:00:00Z","type":"session_meta",'
        f'"payload":{{"session_id":"session-live","cwd":"{cwd}"}}}}\n'
    )


TASK_STARTED = (
    '{"timestamp":"2026-07-04T03:00:01Z","type":"event_msg",'
    '"payload":{"type":"task_started","turn_id":"turn-live","started_at":1783134001}}\n'
)
TASK_COMPLETE = (
    '{"timestamp":"2026-07-04T03:00:06Z","type":"event_msg",'
    '"payload":{"type":"task_complete","turn_id":"turn-live"}}\n'
)
USER_MESSAGE = (
    '{"timestamp":"2026-07-04T03:00:07Z","type":"event_msg",'
    '"payload":{"type":"user_message","message":"Continue monitoring."}}\n'
)


@pytest.mark.anyio
async def test_monitor_creates_updates_and_deduplicates_session(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    session_path = session_root / "live.jsonl"
    _ = session_path.write_text(
        _session_meta(project_root) + TASK_STARTED,
        encoding="utf-8",
    )
    config = AppConfig(project_root=project_root, session_root=session_root)
    store = SessionStore()
    monitor = Monitor(config, store)

    first_change = await monitor.process_path(session_path)
    duplicate_change = await monitor.process_path(session_path)

    assert first_change is ChangeKind.CREATED
    assert duplicate_change is None
    assert store.version == 1
    assert store.summaries(now=1783134002, stuck_seconds=120)[0].status is SessionStatus.RUNNING

    with session_path.open("a", encoding="utf-8") as session_file:
        _ = session_file.write(TASK_COMPLETE)
    second_change = await monitor.process_path(session_path)

    assert second_change is ChangeKind.UPDATED
    assert store.version == 2
    detail = store.detail("project:session-live", now=1783134007, stuck_seconds=120)
    assert detail is not None
    assert detail.status is SessionStatus.IDLE
    assert len(detail.turns) == 1


@pytest.mark.anyio
async def test_store_coalesces_changes_to_latest_version(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    session_path = session_root / "live.jsonl"
    _ = session_path.write_text(
        _session_meta(project_root) + TASK_STARTED,
        encoding="utf-8",
    )
    config = AppConfig(project_root=project_root, session_root=session_root)
    store = SessionStore()
    monitor = Monitor(config, store)
    assert await monitor.process_path(session_path) is ChangeKind.CREATED

    with session_path.open("a", encoding="utf-8") as session_file:
        _ = session_file.write(TASK_COMPLETE)
    assert await monitor.process_path(session_path) is ChangeKind.UPDATED

    assert await store.wait_for_change(0) == 2


@pytest.mark.anyio
async def test_store_marks_no_progress_without_changing_execution_status(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    session_path = session_root / "live.jsonl"
    _ = session_path.write_text(
        _session_meta(project_root) + TASK_STARTED,
        encoding="utf-8",
    )
    config = AppConfig(project_root=project_root, session_root=session_root)
    store = SessionStore()
    monitor = Monitor(config, store)
    assert await monitor.process_path(session_path) is ChangeKind.CREATED

    assert not await store.refresh_stuck(now=1783134002, stuck_seconds=120)
    assert await store.refresh_stuck(now=1783134122, stuck_seconds=120)
    summary = store.summaries(now=1783134122, stuck_seconds=120)[0]
    session = store.get("session-live")

    assert summary.status is SessionStatus.RUNNING
    assert summary.activity_state.value == "no_progress"
    assert session is not None
    assert session.status is SessionStatus.RUNNING
    assert store.version == 2

    with session_path.open("a", encoding="utf-8") as session_file:
        _ = session_file.write(USER_MESSAGE)
    assert await monitor.process_path(session_path) is ChangeKind.UPDATED
    assert store.summaries(now=1783134008, stuck_seconds=120)[0].status is SessionStatus.RUNNING


@pytest.mark.anyio
async def test_stale_data_suppresses_activity_attention_until_fresh_again(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    session_path = session_root / "live.jsonl"
    _ = session_path.write_text(_session_meta(project_root) + TASK_STARTED, encoding="utf-8")
    store = SessionStore()
    monitor = Monitor(AppConfig(project_root=project_root, session_root=session_root), store)
    assert await monitor.process_path(session_path) is ChangeKind.CREATED

    assert not await store.refresh_stuck(1783134122, 120, data_fresh=False)
    stale = store.summaries(1783134122, 120, data_fresh=False)[0]
    assert stale.activity_state is ActivityState.DATA_STALE
    assert stale.attention_context is None
    assert await store.refresh_stuck(1783134122, 120, data_fresh=True)
    fresh = store.summaries(1783134122, 120, data_fresh=True)[0]
    assert fresh.activity_state is ActivityState.NO_PROGRESS
    assert fresh.attention_context is not None


@pytest.mark.anyio
async def test_oldest_pending_tool_is_the_activity_baseline(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    session_path = session_root / "live.jsonl"
    _ = session_path.write_text(_session_meta(project_root) + TASK_STARTED, encoding="utf-8")
    store = SessionStore()
    monitor = Monitor(AppConfig(project_root=project_root, session_root=session_root), store)
    assert await monitor.process_path(session_path) is ChangeKind.CREATED
    session = store.get("session-live")
    assert session is not None
    assert session.current_turn is not None
    session.current_turn.tool_calls.extend(
        [
            ToolCall(ToolName("first_tool"), "", started_at=1783134002),
            ToolCall(ToolName("second_tool"), "", started_at=1783134010),
        ]
    )
    session.pending_tool_name = ToolName("first_tool")
    session.pending_tool_started_at = 1783134002
    session.activity_since = 1783134002

    summary = store.summaries(1783134118, 110)[0]
    assert summary.activity_state is ActivityState.LONG_RUNNING_TOOL
    assert summary.pending_tool_name == "first_tool"
    assert summary.pending_tool_started_at == 1783134002


@pytest.mark.anyio
async def test_attention_context_selects_latest_event_and_ignores_call_id_backfill(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    session_path = session_root / "live.jsonl"
    _ = session_path.write_text(_session_meta(project_root) + TASK_STARTED, encoding="utf-8")
    store = SessionStore()
    monitor = Monitor(AppConfig(project_root=project_root, session_root=session_root), store)
    assert await monitor.process_path(session_path) is ChangeKind.CREATED
    session = store.get("session-live")
    assert session is not None
    turn = session.current_turn
    assert turn is not None

    assert store.summaries(now=1783134002, stuck_seconds=120)[0].attention_context is None

    failed_call = ToolCall(ToolName("shell_command"), "{}", started_at=1783134003)
    failed_call.status = ToolStatus.ERROR
    failed_call.ended_at = 1783134004
    turn.tool_calls.append(failed_call)
    failed = store.summaries(now=1783134005, stuck_seconds=120)[0].attention_context
    assert failed is not None
    assert failed.reason is AttentionReason.TOOL_ERROR
    assert failed.occurred_at == 1783134004
    assert failed.turn_id == "turn-live"
    assert failed.tool_call_index == 0
    assert failed.tool_name == "shell_command"
    assert re.fullmatch(r"[0-9a-f]{64}", failed.event_key)

    failed_call.call_id = "backfilled-call-id"
    repeated = store.summaries(now=1783134005, stuck_seconds=120)[0].attention_context
    assert repeated is not None
    assert repeated.event_key == failed.event_key

    turn.aborted = True
    turn.ended_at = 1783134006
    aborted = store.summaries(now=1783134007, stuck_seconds=120)[0].attention_context
    assert aborted is not None
    assert aborted.reason is AttentionReason.TURN_ABORTED

    session.last_event_at = 1783134001
    failed_call.ended_at = 1783134121
    turn.ended_at = 1783134121
    tied = store.summaries(now=1783134121.001, stuck_seconds=120)[0].attention_context
    assert tied is not None
    assert tied.reason is AttentionReason.TOOL_ERROR
    turn.tool_calls.clear()
    tied_without_tool = store.summaries(now=1783134121.001, stuck_seconds=120)[0].attention_context
    assert tied_without_tool is not None
    assert tied_without_tool.reason is AttentionReason.TURN_ABORTED


@pytest.mark.anyio
async def test_attention_context_keeps_stuck_episode_key_until_recovery(
    tmp_path: Path,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    session_path = session_root / "live.jsonl"
    _ = session_path.write_text(_session_meta(project_root) + TASK_STARTED, encoding="utf-8")
    store = SessionStore()
    monitor = Monitor(AppConfig(project_root=project_root, session_root=session_root), store)
    assert await monitor.process_path(session_path) is ChangeKind.CREATED
    session = store.get("session-live")
    assert session is not None

    first_stuck = store.summaries(now=1783134122, stuck_seconds=120)[0].attention_context
    repeated_stuck = store.summaries(now=1783134200, stuck_seconds=120)[0].attention_context
    assert first_stuck is not None
    assert repeated_stuck is not None
    assert first_stuck.reason is AttentionReason.NO_PROGRESS
    assert repeated_stuck.event_key == first_stuck.event_key

    session.last_event_at = 1783134300
    session.last_progress_at = 1783134300
    session.activity_since = 1783134300
    assert store.summaries(now=1783134301, stuck_seconds=120)[0].attention_context is None
    next_stuck = store.summaries(now=1783134421, stuck_seconds=120)[0].attention_context
    assert next_stuck is not None
    assert next_stuck.reason is AttentionReason.NO_PROGRESS
    assert next_stuck.event_key != first_stuck.event_key


@pytest.mark.anyio
async def test_monitor_keeps_same_session_id_for_multiple_projects(tmp_path: Path) -> None:
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    alpha.mkdir()
    beta.mkdir()
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    alpha_path = session_root / "alpha.jsonl"
    beta_path = session_root / "beta.jsonl"
    _ = alpha_path.write_text(_session_meta(alpha) + TASK_STARTED, encoding="utf-8")
    _ = beta_path.write_text(_session_meta(beta) + TASK_STARTED, encoding="utf-8")
    store = SessionStore()
    monitor = Monitor(AppConfig(project_roots=(alpha, beta), session_root=session_root), store)

    assert await monitor.process_path(alpha_path) is ChangeKind.CREATED
    assert await monitor.process_path(beta_path) is ChangeKind.CREATED

    assert {summary.session_key for summary in store.summaries(1783134002, 120)} == {
        "alpha:session-live",
        "beta:session-live",
    }


@pytest.mark.anyio
async def test_monitor_imports_project_and_reassigns_nested_sessions(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    nested = workspace / "service"
    workspace.mkdir()
    nested.mkdir()
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    session_path = session_root / "nested.jsonl"
    _ = session_path.write_text(_session_meta(nested) + TASK_STARTED, encoding="utf-8")
    store = SessionStore()
    monitor = Monitor(AppConfig(project_root=workspace, session_root=session_root), store)

    assert await monitor.process_path(session_path) is ChangeKind.CREATED
    project, added = await monitor.add_project(nested)

    assert added
    assert project.root == nested
    assert [item.key for item in await monitor.projects()] == ["workspace", "service"]
    assert {summary.session_key for summary in store.summaries(1783134002, 120)} == {
        "service:session-live"
    }


@pytest.mark.anyio
async def test_monitor_import_during_initial_load_keeps_historical_sessions(
    tmp_path: Path,
) -> None:
    initial = tmp_path / "initial"
    imported = tmp_path / "imported"
    initial.mkdir()
    imported.mkdir()
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    session_path = session_root / "imported.jsonl"
    _ = session_path.write_text(_session_meta(imported) + TASK_STARTED, encoding="utf-8")
    store = SessionStore()
    monitor = Monitor(AppConfig(project_root=initial, session_root=session_root), store)
    project = None
    added = False

    async with anyio.create_task_group() as task_group:
        _ = task_group.start_soon(monitor._initial_load)  # pyright: ignore[reportPrivateUsage]
        project, added = await monitor.add_project(imported)
        task_group.cancel_scope.cancel()

    assert added
    assert project is not None
    assert project.root == imported
    assert {summary.session_key for summary in store.summaries(1783134002, 120)} == {
        "imported:session-live"
    }


@pytest.mark.anyio
async def test_monitor_import_is_idempotent_and_rejects_missing_directory(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    monitor = Monitor(AppConfig(project_root=tmp_path, session_root=session_root), SessionStore())

    project, added = await monitor.add_project(tmp_path)
    duplicate, added_again = await monitor.add_project(tmp_path)

    assert added is False
    assert duplicate == project
    assert added_again is False
    with pytest.raises(ValueError, match="existing directory"):
        _ = await monitor.add_project(tmp_path / "missing")


@pytest.mark.anyio
async def test_monitor_remove_nested_project_reassigns_sessions(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    nested = workspace / "service"
    workspace.mkdir()
    nested.mkdir()
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    session_path = session_root / "nested.jsonl"
    _ = session_path.write_text(_session_meta(nested) + TASK_STARTED, encoding="utf-8")
    store = SessionStore()
    monitor = Monitor(
        AppConfig(project_roots=(workspace, nested), session_root=session_root), store
    )
    assert await monitor.process_path(session_path) is ChangeKind.CREATED

    assert await monitor.remove_project("service")

    assert {summary.session_key for summary in store.summaries(1783134002, 120)} == {
        "workspace:session-live"
    }


@pytest.mark.anyio
async def test_monitor_coalesces_duplicate_paths_per_drain(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    session_path = session_root / "live.jsonl"
    _ = session_path.write_text(
        _session_meta(project_root) + TASK_STARTED,
        encoding="utf-8",
    )
    store = SessionStore()
    monitor = Monitor(AppConfig(project_root=project_root, session_root=session_root), store)
    paths: SimpleQueue[Path] = SimpleQueue()
    for _ in range(100):
        paths.put(session_path)

    await monitor._drain(paths)  # pyright: ignore[reportPrivateUsage]

    assert store.version == 1
    assert monitor.runtime_health().coalesced_event_count == 99
    assert monitor.runtime_health().known_file_count == 1


@pytest.mark.anyio
async def test_known_file_poll_does_not_run_full_discovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    session_path = session_root / "live.jsonl"
    _ = session_path.write_text(_session_meta(project_root), encoding="utf-8")
    store = SessionStore()
    monitor = Monitor(AppConfig(project_root=project_root, session_root=session_root), store)
    discover_calls = 0
    original_discover = SessionDiscovery.discover

    def recording_discover(discovery: SessionDiscovery) -> list[Path]:
        nonlocal discover_calls
        discover_calls += 1
        return original_discover(discovery)

    monkeypatch.setattr(SessionDiscovery, "discover", recording_discover)

    await monitor._reconcile()  # pyright: ignore[reportPrivateUsage]
    await monitor._poll_known_files()  # pyright: ignore[reportPrivateUsage]

    assert discover_calls == 1
    assert monitor.runtime_health().reconciliation_count == 1


@pytest.mark.anyio
async def test_reconciliation_recovers_a_missed_file_event(tmp_path: Path) -> None:
    project_root = tmp_path / "project"
    project_root.mkdir()
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    first_path = session_root / "first.jsonl"
    _ = first_path.write_text(_session_meta(project_root), encoding="utf-8")
    store = SessionStore()
    monitor = Monitor(AppConfig(project_root=project_root, session_root=session_root), store)

    await monitor._reconcile()  # pyright: ignore[reportPrivateUsage]
    missed_path = session_root / "missed.jsonl"
    _ = missed_path.write_text(
        _session_meta(project_root).replace("session-live", "session-missed"),
        encoding="utf-8",
    )
    await monitor._reconcile()  # pyright: ignore[reportPrivateUsage]

    assert {summary.session_id for summary in store.summaries(1783134002, 120)} == {
        "session-live",
        "session-missed",
    }
    health = monitor.runtime_health()
    assert health.known_file_count == 2
    assert health.reconciliation_count == 2
    assert health.last_reconciliation_at is not None
