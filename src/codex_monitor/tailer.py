from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class TailBatch:
    lines: tuple[bytes, ...]
    offset: int
    skipped_oversized: int
    line_offsets: tuple[int, ...] = ()


def read_complete_lines(
    path: Path,
    offset: int,
    max_line_bytes: int | None = None,
) -> TailBatch:
    file_size = path.stat().st_size
    start = 0 if offset < 0 or offset > file_size else offset
    lines: list[bytes] = []
    line_offsets: list[int] = []
    committed_offset = start
    skipped_oversized = 0

    with path.open("rb") as session_file:
        _ = session_file.seek(start)
        while line := (
            session_file.readline()
            if max_line_bytes is None
            else session_file.readline(max_line_bytes + 1)
        ):
            record_start = committed_offset
            if line.endswith(b"\n"):
                content = line.rstrip(b"\r\n")
                committed_offset = session_file.tell()
                if max_line_bytes is not None and len(content) > max_line_bytes:
                    skipped_oversized += 1
                    continue
                lines.append(content)
                line_offsets.append(record_start)
                continue
            if max_line_bytes is not None and len(line) > max_line_bytes:
                while line and not line.endswith(b"\n"):
                    line = session_file.readline(max_line_bytes + 1)
                committed_offset = session_file.tell()
                skipped_oversized += 1
                continue
            _ = session_file.seek(record_start)
            break

    return TailBatch(tuple(lines), committed_offset, skipped_oversized, tuple(line_offsets))
