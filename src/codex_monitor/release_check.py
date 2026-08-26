import argparse
import json
import re
import shutil
import subprocess
import tomllib
from pathlib import Path
from typing import cast

import typer

from codex_monitor import __version__
from codex_monitor.api import create_app
from codex_monitor.config import AppConfig

SAFE_JSONL_PREFIX = "tests/fixtures/"
SENSITIVE_SUFFIXES = (".log", ".coverage")


def validate_versions(*declared_versions: str) -> None:
    versions = {_canonical_version(version) for version in declared_versions}
    if len(versions) != 1:
        message = f"version mismatch: {sorted(versions)}"
        raise ValueError(message)


def _canonical_version(version: str) -> str:
    return re.sub(r"\.dev(\d+)$", r"-dev.\1", version)


def validate_static_assets(static_root: Path) -> None:
    page = (static_root / "index.html").read_text(encoding="utf-8")
    references: list[str] = re.findall(r'(?:src|href)="/static/([^\"]+)"', page)
    if not references or any(not (static_root / reference).is_file() for reference in references):
        message = "static asset reference is missing"
        raise ValueError(message)


def find_sensitive_staged_paths(paths: list[str]) -> tuple[str, ...]:
    return tuple(
        path
        for path in paths
        if (path.endswith(".jsonl") and not path.startswith(SAFE_JSONL_PREFIX))
        or path.endswith(SENSITIVE_SUFFIXES)
    )


def validate_repository(root: Path) -> None:
    git = shutil.which("git")
    if git is None:
        message = "git executable was not found"
        raise OSError(message)
    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    frontend = _json_object(root / "frontend/package.json")
    lock = _json_object(root / "frontend/package-lock.json")
    packages = cast("dict[str, object]", lock["packages"])
    lock_project = cast("dict[str, object]", packages[""])
    validate_versions(
        __version__,
        cast("str", project["project"]["version"]),
        cast("str", frontend["version"]),
        cast("str", lock["version"]),
        cast("str", lock_project["version"]),
    )
    app = create_app(AppConfig(project_root=root, session_root=root))
    openapi = cast("dict[str, object]", app.openapi())
    info = cast("dict[str, object]", openapi["info"])
    validate_versions(__version__, app.version, cast("str", info["version"]))
    validate_static_assets(root / "src/codex_monitor/static")
    staged = subprocess.run(  # noqa: S603 - resolved git executable with fixed arguments
        [git, "diff", "--cached", "--name-only", "--diff-filter=ACMR"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    sensitive = find_sensitive_staged_paths(staged)
    if sensitive:
        message = f"sensitive staged paths: {', '.join(sensitive)}"
        raise ValueError(message)
    _ = subprocess.run(  # noqa: S603 - resolved git executable with fixed arguments
        [git, "diff", "--check"], cwd=root, check=True
    )


def _json_object(path: Path) -> dict[str, object]:
    value = cast("object", json.loads(path.read_text(encoding="utf-8")))
    if not isinstance(value, dict):
        message = f"expected JSON object: {path}"
        raise TypeError(message)
    return cast("dict[str, object]", value)


def main() -> None:
    parser = argparse.ArgumentParser()
    _ = parser.add_argument("--root", type=Path, default=Path.cwd())
    args = parser.parse_args()
    root = cast("Path", args.root).resolve()
    try:
        validate_repository(root)
    except (OSError, KeyError, TypeError, ValueError, subprocess.CalledProcessError) as error:
        typer.echo(f"release check failed: {error}", err=True)
        raise SystemExit(1) from error
    typer.echo("release metadata check passed")


if __name__ == "__main__":
    main()
