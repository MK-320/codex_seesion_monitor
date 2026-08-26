import ntpath
import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import assert_never, final

from codex_monitor.config import Project
from codex_monitor.events import EventMessageEvent, ResponseItemEvent, SessionMetaEvent, parse_event


@dataclass(frozen=True, slots=True)
class ProbeRecord:
    cwd: str
    project: Project | None


_WINDOWS_ABSOLUTE_PATH = re.compile(r"^[a-zA-Z]:[\\/]")


def _is_windows_absolute_path(value: str) -> bool:
    return _WINDOWS_ABSOLUTE_PATH.match(value) is not None


def is_within_project(project_root: str | Path, candidate: str | Path) -> bool:
    root_text = str(project_root)
    candidate_text = str(candidate)
    if _is_windows_absolute_path(root_text) or _is_windows_absolute_path(candidate_text):
        if os.name == "nt":
            normalized_root = os.path.normcase(os.path.normpath(str(Path(root_text).resolve())))
            normalized_candidate = os.path.normcase(
                os.path.normpath(str(Path(candidate_text).resolve()))
            )
        else:
            normalized_root = ntpath.normcase(ntpath.normpath(root_text))
            normalized_candidate = ntpath.normcase(ntpath.normpath(candidate_text))
        commonpath = ntpath.commonpath
    else:
        normalized_root = os.path.normcase(os.path.normpath(str(Path(project_root).resolve())))
        normalized_candidate = os.path.normcase(os.path.normpath(str(Path(candidate).resolve())))
        commonpath = os.path.commonpath
    try:
        common = commonpath((normalized_root, normalized_candidate))
    except ValueError:
        return False
    return common == normalized_root


@final
class SessionDiscovery:
    __slots__ = ("_cache", "_generation", "_projects", "_session_root")

    _projects: tuple[Project, ...]
    _session_root: Path
    _cache: dict[Path, ProbeRecord]
    _generation: int

    def __init__(
        self,
        project_roots: Path | tuple[Path, ...] | tuple[Project, ...],
        session_root: Path,
    ) -> None:
        roots = (project_roots,) if isinstance(project_roots, Path) else project_roots
        self._projects = tuple(
            root if isinstance(root, Project) else Project(root.name or str(root), root)
            for root in roots
        )
        self._session_root = session_root
        self._cache = {}
        self._generation = 0

    def discover(self) -> list[Path]:
        return [
            path
            for path in sorted(self._session_root.rglob("*.jsonl"))
            if self.project_for(path) is not None
        ]

    @property
    def projects(self) -> tuple[Project, ...]:
        return self._projects

    def add_project(self, root: Path) -> tuple[Project, bool]:
        normalized_root = root.resolve()
        for project in self._projects:
            if project.root.resolve() == normalized_root:
                return project, False
        project = Project(_project_key(normalized_root, self._projects), normalized_root)
        self._projects = (*self._projects, project)
        self._invalidate_cache()
        return project, True

    def remove_project(self, project_key: str) -> Project | None:
        project = next((item for item in self._projects if item.key == project_key), None)
        if project is None:
            return None
        self._projects = tuple(item for item in self._projects if item.key != project_key)
        self._invalidate_cache()
        return project

    def belongs_to_project(self, path: Path) -> bool:
        return self.project_for(path) is not None

    def project_for(self, path: Path) -> Project | None:
        cached = self._cache.get(path)
        if cached is not None:
            return cached.project
        generation = self._generation
        record = self._probe(path)
        if record is None:
            return None
        # A concurrent add/remove can clear the cache while this probe still runs.
        # Retry under the new generation instead of re-poisoning with a stale miss.
        if generation != self._generation:
            return self.project_for(path)
        self._cache[path] = record
        return record.project

    def _invalidate_cache(self) -> None:
        self._generation += 1
        self._cache.clear()

    def _probe(self, path: Path) -> ProbeRecord | None:
        try:
            with path.open("rb") as session_file:
                first_line = session_file.readline()
        except OSError:
            return None
        event = parse_event(first_line)
        match event:
            case SessionMetaEvent(payload=payload):
                matches = [
                    project
                    for project in self._projects
                    if is_within_project(project.root, payload.cwd)
                ]
                project = max(matches, key=lambda item: len(str(item.root))) if matches else None
                return ProbeRecord(cwd=payload.cwd, project=project)
            case EventMessageEvent() | ResponseItemEvent() | None:
                return None
            case unreachable:
                assert_never(unreachable)


def _project_key(root: Path, projects: tuple[Project, ...]) -> str:
    base_key = root.name or str(root)
    keys = {project.key for project in projects}
    if base_key not in keys:
        return base_key
    suffix = 2
    while f"{base_key}-{suffix}" in keys:
        suffix += 1
    return f"{base_key}-{suffix}"
