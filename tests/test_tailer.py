from pathlib import Path

from codex_monitor.tailer import read_complete_lines


def test_tailer_retries_incomplete_last_line(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    _ = path.write_bytes(b'{"type":"event_msg"}\n{"type":"partial"')

    first = read_complete_lines(path, 0)

    assert first.lines == (b'{"type":"event_msg"}',)
    assert first.offset == len(b'{"type":"event_msg"}\n')

    with path.open("ab") as session_file:
        _ = session_file.write(b"}\n")

    second = read_complete_lines(path, first.offset)

    assert second.lines == (b'{"type":"partial"}',)
    assert second.offset == path.stat().st_size


def test_tailer_restarts_when_file_is_shorter_than_offset(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    _ = path.write_bytes(b'{"old":true}\n')
    old_offset = path.stat().st_size
    _ = path.write_bytes(b'{"new":true}\n')

    batch = read_complete_lines(path, old_offset + 20)

    assert batch.lines == (b'{"new":true}',)
    assert batch.offset == path.stat().st_size


def test_tailer_skips_oversized_record(tmp_path: Path) -> None:
    path = tmp_path / "session.jsonl"
    _ = path.write_bytes(b"x" * 17 + b"\n" + b'{"ok":true}\n')

    batch = read_complete_lines(path, 0, max_line_bytes=16)

    assert batch.lines == (b'{"ok":true}',)
    assert batch.skipped_oversized == 1
