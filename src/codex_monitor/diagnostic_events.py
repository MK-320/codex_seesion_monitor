from __future__ import annotations

import json
import re
import sys
import threading
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Final, TextIO, final

LOG_SCHEMA_VERSION: Final = 2
RECORD_LIMIT_BYTES: Final = 4 * 1024
MAX_DURATION_MS: Final = 86_400_000
MIN_HTTP_STATUS: Final = 100
MAX_HTTP_STATUS: Final = 599
MAX_SYSTEM_ERROR_CODE: Final = 4_294_967_295
_SAFE_TOKEN = re.compile(r"^[a-z][a-z0-9_.-]{0,63}$")
_SAFE_ALIAS = re.compile(r"^(?:project|session|path):[a-z][a-z0-9_-]{0,31}-\d+$")
_SAFE_OPERATION = re.compile(r"^op-\d+$")


def _safe_token(value: str, fallback: str) -> str:
    candidate = value.strip().lower()
    return candidate if _SAFE_TOKEN.fullmatch(candidate) else fallback


def _safe_level(value: str) -> str:
    candidate = value.strip().upper()
    return candidate if candidate in {"DEBUG", "WARN", "ERROR"} else "WARN"


def _safe_alias(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip().lower()
    return candidate if _SAFE_ALIAS.fullmatch(candidate) else None


def _safe_operation(value: str | None) -> str | None:
    if value is None:
        return None
    candidate = value.strip().lower()
    return candidate if _SAFE_OPERATION.fullmatch(candidate) else None


def _safe_summary(error_summary: str | None) -> str | None:
    if error_summary is None:
        return None
    return _safe_token(error_summary, "redaction_failed")


@dataclass(frozen=True, slots=True)
class DiagnosticEvent:
    level: str
    component: str
    event_code: str
    source: str = "sidecar"
    duration_ms: int | None = None
    operation_id: str | None = None
    path_category: str | None = None
    path_alias: str | None = None
    error_summary: str | None = None
    network: dict[str, object] | None = None


def resolve_stdout_stream() -> TextIO:
    """Return a writable stdout, restoring fd 1 when windowed builds leave sys.stdout None."""
    stream = sys.stdout
    if stream is not None and not stream.closed:
        return stream
    # Keep the restored handle for the process lifetime; a context manager would close it early.
    restored = open(  # noqa: SIM115
        1, mode="w", encoding="utf-8", buffering=1, closefd=False, errors="replace"
    )
    sys.stdout = restored
    return restored


@final
class DiagnosticEmitter:
    def __init__(self, stream: TextIO | None = None) -> None:
        self._stream: TextIO = resolve_stdout_stream() if stream is None else stream
        self._lock: threading.Lock = threading.Lock()

    def emit(self, event: DiagnosticEvent) -> None:
        record: dict[str, object] = {
            "schema_version": LOG_SCHEMA_VERSION,
            "timestamp": datetime.now(UTC)
            .isoformat(timespec="milliseconds")
            .replace("+00:00", "Z"),
            "level": _safe_level(event.level),
            "component": _safe_token(event.component, "sidecar"),
            "event_code": _safe_token(event.event_code, "redaction_failed"),
            "source": "sidecar",
        }
        safe_operation = _safe_operation(event.operation_id)
        safe_category = (
            _safe_token(event.path_category, "redaction_failed") if event.path_category else None
        )
        safe_alias = _safe_alias(event.path_alias)
        safe_summary = _safe_summary(event.error_summary)
        if safe_operation is not None:
            record["operation_id"] = safe_operation
        if event.duration_ms is not None and 0 <= event.duration_ms <= MAX_DURATION_MS:
            record["duration_ms"] = event.duration_ms
        if safe_category is not None:
            record["path_category"] = safe_category
        if safe_alias is not None:
            record["path_alias"] = safe_alias
        if safe_summary is not None:
            record["error_summary"] = safe_summary
        if event.network is not None:
            record["network"] = self._safe_network(event.network)
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) + 1 > RECORD_LIMIT_BYTES:
            record["error_summary"] = "diagnostic_record_truncated"
            _ = record.pop("network", None)
            encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        if len(encoded.encode("utf-8")) + 1 > RECORD_LIMIT_BYTES:
            record = {
                "schema_version": LOG_SCHEMA_VERSION,
                "timestamp": record["timestamp"],
                "level": "WARN",
                "component": "sidecar",
                "event_code": "diagnostic_record_truncated",
            }
            encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":"))
        with self._lock:
            try:
                _ = self._stream.write(f"{encoded}\n")
                _ = self._stream.flush()
            except (OSError, ValueError):
                return

    @staticmethod
    def _safe_network(network: dict[str, object]) -> dict[str, object]:
        result: dict[str, object] = {
            "protocol": _safe_token(str(network.get("protocol", "unknown")), "unknown"),
            "scope": _safe_token(str(network.get("scope", "unknown")), "unknown"),
        }
        status = network.get("http_status", network.get("status"))
        if isinstance(status, int) and MIN_HTTP_STATUS <= status <= MAX_HTTP_STATUS:
            result["http_status"] = status
        for key in ("close_code", "system_error_code"):
            value = network.get(key)
            if isinstance(value, int) and 0 <= value <= MAX_SYSTEM_ERROR_CODE:
                result[key] = value
        return result


def safe_exception_summary(error: BaseException) -> str:
    return _safe_token(type(error).__name__, "redaction_failed")
