import platform
from collections.abc import Mapping
from importlib.metadata import PackageNotFoundError
from importlib.metadata import version as distribution_version

from codex_monitor.models import SessionStatus
from codex_monitor.schemas import SessionSummary

DEPENDENCIES = ("anyio", "fastapi", "pydantic", "uvicorn", "watchdog", "websockets")


def build_diagnostics(  # noqa: PLR0913 - explicit aggregate inputs keep sensitive data out
    *,
    version: str,
    started_at: float,
    project_count: int,
    summaries: tuple[SessionSummary, ...],
    health_counts: dict[str, int],
    monitor_health: Mapping[str, int | float | None],
    websocket_clients: int,
    stuck_seconds: int | None,
    now: float,
) -> dict[str, object]:
    status_counts = {status.value: 0 for status in SessionStatus}
    for summary in summaries:
        status_counts[summary.status.value] += 1
    return {
        "schema_version": 1,
        "generated_at": now,
        "application": {
            "name": "codex-session-monitor",
            "version": version,
            "protocol_version": 1,
        },
        "runtime": {
            "python": platform.python_version(),
            "system": platform.system(),
            "release": platform.release(),
            "machine": platform.machine(),
        },
        "dependencies": {name: _dependency_version(name) for name in DEPENDENCIES},
        "health": {
            "started_at": started_at,
            "uptime_seconds": max(0.0, now - started_at),
            "listen_host": "127.0.0.1",
            "project_count": project_count,
            **health_counts,
            **monitor_health,
            "status_counts": status_counts,
            "websocket_clients": websocket_clients,
        },
        "configuration": {
            "schema_version": 1,
            "stuck_seconds": stuck_seconds,
        },
        "privacy": {
            "excluded": (
                "session_ids",
                "project_paths",
                "user_messages",
                "agent_text",
                "tool_inputs",
                "tool_results",
                "environment_values",
            ),
        },
    }


def _dependency_version(name: str) -> str:
    try:
        return distribution_version(name)
    except PackageNotFoundError:
        return "unavailable"
