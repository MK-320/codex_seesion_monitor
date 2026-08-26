import json
import os
import tomllib
from pathlib import Path
from typing import TypedDict, cast

import pytest

from codex_monitor import __version__
from codex_monitor.release_check import (
    find_sensitive_staged_paths,
    validate_static_assets,
    validate_versions,
)


class VersionSection(TypedDict):
    version: str


class ProjectMetadata(TypedDict):
    project: VersionSection


class PackageLock(VersionSection):
    packages: dict[str, VersionSection]


class LockPackage(VersionSection):
    name: str


class LockMetadata(TypedDict):
    package: list[LockPackage]


class CargoMetadata(TypedDict):
    package: VersionSection


def test_release_metadata_matches_expected_version() -> None:
    root = Path(__file__).parents[1]
    project = cast(
        "ProjectMetadata",
        cast("object", tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))),
    )
    uv_lock = cast(
        "LockMetadata",
        cast("object", tomllib.loads((root / "uv.lock").read_text(encoding="utf-8"))),
    )
    frontend = cast(
        "VersionSection",
        json.loads((root / "frontend/package.json").read_text(encoding="utf-8")),
    )
    package_lock = cast(
        "PackageLock",
        json.loads((root / "frontend/package-lock.json").read_text(encoding="utf-8")),
    )
    cargo = cast(
        "CargoMetadata",
        cast(
            "object",
            tomllib.loads((root / "frontend/src-tauri/Cargo.toml").read_text(encoding="utf-8")),
        ),
    )
    cargo_lock = cast(
        "LockMetadata",
        cast(
            "object",
            tomllib.loads((root / "frontend/src-tauri/Cargo.lock").read_text(encoding="utf-8")),
        ),
    )
    tauri = cast(
        "VersionSection",
        json.loads((root / "frontend/src-tauri/tauri.conf.json").read_text(encoding="utf-8")),
    )

    release_version = os.environ.get("CODEX_RELEASE_VERSION")
    expected_version = release_version or __version__
    expected_frontend_version = expected_version.replace(".dev", "-dev.")
    if release_version is None:
        assert expected_version.endswith(".dev0")
    uv_project = next(
        package for package in uv_lock["package"] if package["name"] == "codex-session-monitor"
    )
    cargo_project = next(
        package for package in cargo_lock["package"] if package["name"] == "codex-session-monitor"
    )

    assert (__version__, project["project"]["version"], uv_project["version"]) == (
        expected_version,
        expected_version,
        expected_version,
    )
    assert (
        frontend["version"],
        package_lock["version"],
        package_lock["packages"][""]["version"],
        cargo["package"]["version"],
        cargo_project["version"],
        tauri["version"],
    ) == (expected_frontend_version,) * 6


def test_release_check_validates_versions_and_static_assets(tmp_path: Path) -> None:
    static = tmp_path / "static"
    assets = static / "assets"
    assets.mkdir(parents=True)
    _ = (assets / "app.js").write_text("", encoding="utf-8")
    _ = (static / "index.html").write_text(
        '<script src="/static/assets/app.js"></script>', encoding="utf-8"
    )

    validate_versions("4.5.0", "4.5.0", "4.5.0", "4.5.0", "4.5.0")
    validate_versions("6.3.9.dev0", "6.3.9-dev.0")
    validate_static_assets(static)

    with pytest.raises(ValueError, match="version mismatch"):
        validate_versions("4.5.0", "4.4.0", "4.5.0", "4.5.0", "4.5.0")


def test_release_check_rejects_sensitive_staged_paths() -> None:
    assert find_sensitive_staged_paths(
        ["tests/fixtures/safe.jsonl", ".coverage", "private/session.jsonl", "debug.log"]
    ) == (".coverage", "private/session.jsonl", "debug.log")
