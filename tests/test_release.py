import json
import os
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import TypedDict, cast

import pytest

from codex_monitor import __version__
from codex_monitor.release_check import (
    find_public_path_violations,
    find_sensitive_staged_paths,
    public_tree_hash,
    scan_public_content,
    scan_public_history,
    validate_manual_confirmation,
    validate_public_export,
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


def test_public_export_path_gate_is_case_insensitive_and_rejects_private_history_paths() -> None:
    assert find_public_path_violations(
        [
            "DOCS/internal.md",
            "SIGNING.PFX",
            "Codex工作索引.md",
            "src/public.py",
            "frontend/src-tauri/tauri.conf.json",
            "frontend/package-lock.json",
            "tmp/config.json",
        ]
    ) == ("Codex工作索引.md", "DOCS/internal.md", "SIGNING.PFX", "tmp/config.json")


def test_manual_confirmation_is_bound_to_export_tree(tmp_path: Path) -> None:
    export = tmp_path / "export"
    export.mkdir()
    _ = (export / "README.md").write_text("public", encoding="utf-8")
    confirmation = tmp_path / "approval.json"
    _ = confirmation.write_text(
        json.dumps(
            {
                "confirmed": True,
                "rules_version": "public-gate-v1",
                "export_sha256": public_tree_hash(export),
                "checks": [True, True, True],
            }
        ),
        encoding="utf-8",
    )

    validate_manual_confirmation(export, confirmation)
    _ = (export / "README.md").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="does not match"):
        validate_manual_confirmation(export, confirmation)


def test_public_gate_scans_content_history_and_requires_current_confirmation(
    tmp_path: Path,
) -> None:
    fake_github_token = "gh" + "p_" + ("1234567890" * 4)
    export = tmp_path / "export"
    export.mkdir()
    _ = (export / "README.md").write_text("public", encoding="utf-8")
    _ = (export / "private.txt").write_text(fake_github_token, encoding="utf-8")
    assert scan_public_content(export) == ("private.txt",)

    _ = (export / "private.txt").write_text("internal", encoding="utf-8")
    git = shutil.which("git")
    if git is None:
        pytest.skip("git executable is required for history gate test")
    _ = subprocess.run([git, "init"], cwd=export, check=True, capture_output=True)  # noqa: S603
    _ = subprocess.run(  # noqa: S603
        [git, "config", "user.email", "test@example.invalid"],
        cwd=export,
        check=True,
        capture_output=True,
    )
    _ = subprocess.run(  # noqa: S603
        [git, "config", "user.name", "Public Gate Test"],
        cwd=export,
        check=True,
        capture_output=True,
    )
    _ = subprocess.run([git, "add", "README.md"], cwd=export, check=True, capture_output=True)  # noqa: S603
    _ = subprocess.run(  # noqa: S603
        [git, "commit", "-m", "initial public export"],
        cwd=export,
        check=True,
        capture_output=True,
    )
    assert scan_public_history(export) == ()

    confirmation = tmp_path / "approval.json"
    _ = confirmation.write_text(
        json.dumps(
            {
                "confirmed": True,
                "rules_version": "public-gate-v1",
                "export_sha256": public_tree_hash(export),
                "checks": [True],
            }
        ),
        encoding="utf-8",
    )
    validate_public_export(export, confirmation)

    _ = (export / "secret.txt").write_text(
        fake_github_token,
        encoding="utf-8",
    )
    _ = subprocess.run(  # noqa: S603
        [git, "add", "secret.txt"],
        cwd=export,
        check=True,
        capture_output=True,
    )
    _ = subprocess.run(  # noqa: S603
        [git, "commit", "-m", "secret must be rejected"],
        cwd=export,
        check=True,
        capture_output=True,
    )
    assert scan_public_history(export) == ("blob:secret.txt", "secret.txt")
    assert scan_public_content(export) == ("secret.txt",)

    _ = (confirmation).write_text(
        json.dumps(
            {
                "confirmed": False,
                "rules_version": "public-gate-v1",
                "export_sha256": public_tree_hash(export),
                "checks": [True],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="not confirmed"):
        validate_manual_confirmation(export, confirmation)


def test_public_gate_rejects_missing_or_invalid_confirmation_records(tmp_path: Path) -> None:
    export = tmp_path / "export"
    export.mkdir()
    _ = (export / "README.md").write_text("public", encoding="utf-8")
    git = shutil.which("git")
    if git is None:
        pytest.skip("git executable is required for history gate test")
    _ = subprocess.run([git, "init"], cwd=export, check=True, capture_output=True)  # noqa: S603
    _ = subprocess.run([git, "add", "README.md"], cwd=export, check=True, capture_output=True)  # noqa: S603
    _ = subprocess.run(  # noqa: S603
        [
            git,
            "-c",
            "user.email=test@example.invalid",
            "-c",
            "user.name=Public Gate Test",
            "commit",
            "-m",
            "initial",
        ],
        cwd=export,
        check=True,
        capture_output=True,
    )

    with pytest.raises(ValueError, match="requires a manual confirmation"):
        validate_public_export(export)

    confirmation = tmp_path / "approval.json"
    _ = confirmation.write_text("not json", encoding="utf-8")
    with pytest.raises(ValueError, match="unreadable"):
        validate_manual_confirmation(export, confirmation)

    _ = confirmation.write_text(
        json.dumps(
            {
                "confirmed": True,
                "rules_version": "old",
                "export_sha256": public_tree_hash(export),
                "checks": [True],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="unsupported rules"):
        validate_manual_confirmation(export, confirmation)

    _ = confirmation.write_text(
        json.dumps(
            {
                "confirmed": True,
                "rules_version": "public-gate-v1",
                "export_sha256": public_tree_hash(export),
                "checks": [],
            }
        ),
        encoding="utf-8",
    )
    with pytest.raises(ValueError, match="checklist is incomplete"):
        validate_manual_confirmation(export, confirmation)
