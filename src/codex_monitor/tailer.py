from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class TailBatch:
    lines: tuple[bytes, ...]
    offset: int
    skipped_oversized: int


def read_complete_lines(
    path: Path,
    offset: int,
    max_line_bytes: int = 1_048_576,
) -> TailBatch:
    file_size = path.stat().st_size
    start = 0 if offset < 0 or offset > file_size else offset
    lines: list[bytes] = []
    committed_offset = start
    skipped_oversized = 0

    with path.open("rb") as session_file:
        _ = session_file.seek(start)
        while line := session_file.readline(max_line_bytes + 1):
            record_start = committed_offset
            if line.endswith(b"\n"):
                content = line.rstrip(b"\r\n")
                committed_offset = session_file.tell()
                if len(content) > max_line_bytes:
                    skipped_oversized += 1
                    continue
                lines.append(content)
                continue
            if len(line) > max_line_bytes:
                while line and not line.endswith(b"\n"):
                    line = session_file.readline(max_line_bytes + 1)
                committed_offset = session_file.tell()
                skipped_oversized += 1
                continue
            _ = session_file.seek(record_start)
            break

    return TailBatch(tuple(lines), committed_offset, skipped_oversized)
