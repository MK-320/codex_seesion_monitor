import json
import os
from dataclasses import dataclass
from pathlib import Path
from threading import Lock
from typing import Annotated, ClassVar, Final, Literal, cast

from pydantic import BaseModel, ConfigDict, DirectoryPath, Field, SecretStr

DEFAULT_SESSION_ROOT: Final = Path.home() / ".codex" / "sessions"
DEFAULT_TRACE_ROOT: Final = (
    Path(os.environ.get("LOCALAPPDATA", Path.home() / ".config")) / "CodexSessionMonitor" / "traces"
)
DEFAULT_CONFIG_FILE: Final = (
    Path(os.environ.get("LOCALAPPDATA", Path.home() / ".config"))
    / "CodexSessionMonitor"
    / "config.json"
)
_CONFIG_WRITE_LOCK = Lock()


@dataclass(frozen=True, slots=True)
class Project:
    key: str
    root: Path

    @property
    def name(self) -> str:
        return self.root.name or str(self.root)


class LayoutPreferences(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True)

    project_sidebar_ratio: Annotated[float, Field(gt=0, lt=1)]
    session_sidebar_ratio: Annotated[float, Field(gt=0, lt=1)]


class AppConfig(BaseModel):
    model_config: ClassVar[ConfigDict] = ConfigDict(frozen=True, validate_default=True)

    project_root: DirectoryPath | None = None
    project_roots: tuple[DirectoryPath, ...] = ()
    session_root: DirectoryPath = DEFAULT_SESSION_ROOT
    trace_root: Path = DEFAULT_TRACE_ROOT
    trace_enabled: bool = True
    trace_retention_days: Annotated[int | None, Field(gt=0)] = None
    stuck_seconds: Annotated[int | None, Field(gt=0)] = 300
    host: Literal["127.0.0.1"] = "127.0.0.1"
    port: Annotated[int, Field(ge=0, le=65535)] = 8000
    startup_token: Annotated[SecretStr | None, Field(min_length=64, max_length=64)] = None
    desktop_protocol_version: Literal[1] = 1
    config_file: Path | None = None
    load_saved_projects: bool = True

    @property
    def projects(self) -> tuple[Project, ...]:
        counts: dict[str, int] = {}
        projects: list[Project] = []
        roots = list(
            self.project_roots or (() if self.project_root is None else (self.project_root,))
        )
        if self.config_file is not None and self.load_saved_projects:
            roots.extend(load_saved_project_roots(self.config_file))
        roots = list(dict.fromkeys(Path(root).resolve() for root in roots))
        for root in roots:
            base_key = root.name or str(root)
            count = counts.get(base_key, 0) + 1
            counts[base_key] = count
            projects.append(Project(base_key if count == 1 else f"{base_key}-{count}", root))
        return tuple(projects)


def _load_saved_config(path: Path) -> dict[str, object]:
    try:
        raw = cast("object", json.loads(path.read_text(encoding="utf-8")))
        if not isinstance(raw, dict):
            return {}
        data = cast("dict[str, object]", raw)
        if data.get("version") != 1:
            return {}
    except (FileNotFoundError, json.JSONDecodeError, OSError, TypeError, AttributeError):
        return {}
    return data


def _write_saved_config(path: Path, data: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(f"{path.suffix}.tmp")
    _ = temporary.write_text(
        json.dumps(data, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    _ = temporary.replace(path)


def load_saved_project_roots(path: Path) -> tuple[Path, ...]:
    projects = _load_saved_config(path).get("projects")
    roots = cast("list[object]", projects) if isinstance(projects, list) else []
    return tuple(Path(root).resolve() for root in roots if isinstance(root, str))


def save_saved_project_roots(path: Path, roots: tuple[Path, ...]) -> None:
    with _CONFIG_WRITE_LOCK:
        data = _load_saved_config(path)
        data.update({"version": 1, "projects": [str(root.resolve()) for root in roots]})
        _write_saved_config(path, data)


def load_saved_layout(path: Path) -> LayoutPreferences | None:
    layout = _load_saved_config(path).get("layout")
    if not isinstance(layout, dict):
        return None
    try:
        return LayoutPreferences.model_validate(layout)
    except (ValueError, TypeError, AttributeError):
        return None


def save_saved_layout(path: Path, layout: LayoutPreferences) -> None:
    with _CONFIG_WRITE_LOCK:
        data = _load_saved_config(path)
        data.update({"version": 1, "layout": layout.model_dump()})
        _ = data.setdefault("projects", [])
        _write_saved_config(path, data)


def load_saved_activity_alert_seconds(path: Path, default: int | None) -> int | None:
    value = _load_saved_config(path).get("activity_alert_seconds", default)
    if value is None:
        return None
    return value if isinstance(value, int) and value in {180, 300, 600, 900} else default


def save_saved_activity_alert_seconds(path: Path, seconds: int | None) -> None:
    with _CONFIG_WRITE_LOCK:
        data = _load_saved_config(path)
        data.update({"version": 1, "activity_alert_seconds": seconds})
        _ = data.setdefault("projects", [])
        _write_saved_config(path, data)


def load_saved_trace_settings(
    path: Path,
    default_enabled: bool,
    default_retention_days: int | None,
) -> tuple[bool, int | None]:
    data = _load_saved_config(path)
    enabled = data.get("trace_enabled", default_enabled)
    retention = data.get("trace_retention_days", default_retention_days)
    if not isinstance(enabled, bool):
        enabled = default_enabled
    if retention is not None and (not isinstance(retention, int) or retention <= 0):
        retention = default_retention_days
    return enabled, retention if retention is None else int(retention)


def save_saved_trace_settings(path: Path, enabled: bool, retention_days: int | None) -> None:
    with _CONFIG_WRITE_LOCK:
        data = _load_saved_config(path)
        data.update(
            {
                "version": 1,
                "trace_enabled": enabled,
                "trace_retention_days": retention_days,
            }
        )
        _ = data.setdefault("projects", [])
        _write_saved_config(path, data)
