import argparse
import hashlib
import json
import os
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
SENSITIVE_SUFFIXES = (
    ".log",
    ".coverage",
    ".pfx",
    ".p12",
    ".pem",
    ".key",
    ".cer",
    ".crt",
    ".der",
    ".sqlite",
    ".sqlite3",
    ".db",
    ".env",
)
PUBLIC_FORBIDDEN_NAMES = {
    "Codex工作索引.md",
    ".debug-journal.md",
    "design-qa.md",
}
PUBLIC_FORBIDDEN_PREFIXES = ("docs/", ".codex/", ".claude/")
PUBLIC_ALLOWED_ROOT_FILES = {
    "README.md",
    "LICENSE",
    "CHANGELOG.md",
    "pyproject.toml",
    "uv.lock",
    "PUBLIC_RELEASE_CHECKLIST.md",
    "THIRD_PARTY_NOTICES.md",
    ".gitignore",
    ".gitattributes",
    "start-codex-monitor.ps1",
    "start-codex-monitor.cmd",
}
PUBLIC_ALLOWED_JSON_PATHS = {
    "frontend/package.json",
    "frontend/package-lock.json",
    "frontend/tsconfig.json",
    "frontend/tsconfig.app.json",
    "frontend/src-tauri/capabilities/default.json",
    "frontend/src-tauri/tauri.conf.json",
    "frontend/src-tauri/tauri.release.conf.json",
    "frontend/src-tauri/tauri.unsigned.conf.json",
}
PUBLIC_ALLOWED_PREFIXES = (
    "src/",
    "tests/",
    "frontend/",
    "scripts/",
    ".github/",
)
PUBLIC_SECRET_PATTERNS = (
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"-----BEGIN [A-Z ]*CERTIFICATE-----"),
    re.compile(r"\bghp_[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bsk-[A-Za-z0-9]{20,}\b"),
    re.compile(r"\bBearer\s+[A-Za-z0-9._-]{24,}\b"),
    re.compile(r"[A-Za-z]:\\Users\\[^\\\r\n]+"),
)


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


def find_public_path_violations(paths: list[str]) -> tuple[str, ...]:
    violations: list[str] = []
    for raw_path in paths:
        path = raw_path.replace("\\", "/")
        folded_path = path.casefold()
        name = Path(path).name.casefold()
        if name in {item.casefold() for item in PUBLIC_FORBIDDEN_NAMES} or any(
            folded_path.startswith(prefix.casefold()) for prefix in PUBLIC_FORBIDDEN_PREFIXES
        ):
            violations.append(raw_path)
            continue
        if folded_path.endswith(tuple(suffix.casefold() for suffix in SENSITIVE_SUFFIXES)):
            violations.append(raw_path)
            continue
        if folded_path.endswith(".jsonl") and not folded_path.startswith(
            SAFE_JSONL_PREFIX.casefold()
        ):
            violations.append(raw_path)
            continue
        if folded_path.endswith(".json") and folded_path not in {
            item.casefold() for item in PUBLIC_ALLOWED_JSON_PATHS
        }:
            violations.append(raw_path)
            continue
        if folded_path in {item.casefold() for item in PUBLIC_ALLOWED_ROOT_FILES} or any(
            folded_path.startswith(prefix.casefold()) for prefix in PUBLIC_ALLOWED_PREFIXES
        ):
            continue
        violations.append(raw_path)
    return tuple(sorted(set(violations)))


def scan_public_content(root: Path) -> tuple[str, ...]:
    findings: list[str] = []
    for directory, _, filenames in os.walk(root):
        if ".git" in Path(directory).parts:
            continue
        for filename in filenames:
            path = Path(directory) / filename
            relative = path.relative_to(root).as_posix()
            try:
                content = path.read_bytes()
            except OSError:
                findings.append(f"{relative}: unreadable")
                continue
            text = content.decode("utf-8", errors="replace")
            haystack = text + "\n" + content.decode("latin-1", errors="replace")
            if any(pattern.search(haystack) for pattern in PUBLIC_SECRET_PATTERNS):
                findings.append(relative)
    return tuple(sorted(set(findings)))


def scan_public_history(root: Path) -> tuple[str, ...]:
    git = shutil.which("git")
    if git is None:
        message = "git executable was not found"
        raise OSError(message)
    findings: list[str] = []
    history_paths = subprocess.run(  # noqa: S603
        [git, "log", "--all", "--name-only", "--format="],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    findings.extend(find_public_path_violations(history_paths))
    objects = subprocess.run(  # noqa: S603
        [git, "rev-list", "--objects", "--all"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    blob_paths: dict[str, str] = {}
    for item in objects:
        parts = item.split(" ", 1)
        if len(parts) == 2:  # noqa: PLR2004
            blob_paths[parts[0]] = parts[1]
    if blob_paths:
        request = "".join(f"{oid}\n" for oid in blob_paths)
        batch = subprocess.run(  # noqa: S603
            [git, "cat-file", "--batch"],
            cwd=root,
            check=True,
            input=request.encode("ascii"),
            capture_output=True,
        ).stdout
        cursor = 0
        while cursor < len(batch):
            header_end = batch.find(b"\n", cursor)
            if header_end < 0:
                break
            header = batch[cursor:header_end].split()
            cursor = header_end + 1
            if len(header) != 3 or header[1] != b"blob":  # noqa: PLR2004
                continue
            try:
                size = int(header[2])
            except ValueError:
                break
            content = batch[cursor : cursor + size]
            cursor += size + 1
            path = blob_paths.get(header[0].decode("ascii", errors="ignore"), "")
            text = content.decode("utf-8", errors="replace")
            haystack = text + "\n" + content.decode("latin-1", errors="replace")
            if any(pattern.search(haystack) for pattern in PUBLIC_SECRET_PATTERNS):
                findings.append(f"blob:{path}")
    return tuple(sorted(set(findings)))


def validate_public_export(root: Path, confirmation: Path | None = None) -> None:
    git = shutil.which("git")
    if git is None:
        message = "git executable was not found"
        raise OSError(message)
    tracked = subprocess.run(  # noqa: S603
        [git, "ls-files"],
        cwd=root,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.splitlines()
    path_violations = find_public_path_violations(tracked)
    content_violations = scan_public_content(root)
    history_violations = scan_public_history(root)
    violations = path_violations + content_violations + history_violations
    if violations:
        raise ValueError("public export gate failed: " + ", ".join(sorted(set(violations))))
    if confirmation is None:
        message = "public export gate requires a manual confirmation record"
        raise ValueError(message)
    validate_manual_confirmation(root, confirmation)


def validate_manual_confirmation(root: Path, confirmation: Path) -> None:
    try:
        payload = cast("dict[str, object]", json.loads(confirmation.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError) as error:
        message = "manual confirmation record is unreadable"
        raise ValueError(message) from error
    if payload.get("confirmed") is not True:
        message = "manual confirmation record is not confirmed"
        raise ValueError(message)
    if payload.get("rules_version") != "public-gate-v1":
        message = "manual confirmation record uses an unsupported rules version"
        raise ValueError(message)
    expected_hash = public_tree_hash(root)
    if payload.get("export_sha256") != expected_hash:
        message = "manual confirmation record does not match the export tree"
        raise ValueError(message)
    checks = payload.get("checks")
    typed_checks = cast("list[object]", checks) if isinstance(checks, list) else []
    if not typed_checks or not all(item is True for item in typed_checks):
        message = "manual confirmation checklist is incomplete"
        raise ValueError(message)


def public_tree_hash(root: Path) -> str:
    digest = hashlib.sha256()
    for path in sorted(
        (item for item in root.rglob("*") if item.is_file() and ".git" not in item.parts),
        key=lambda item: item.relative_to(root).as_posix(),
    ):
        relative = path.relative_to(root).as_posix().encode("utf-8")
        digest.update(relative)
        digest.update(b"\0")
        digest.update(hashlib.sha256(path.read_bytes()).digest())
        digest.update(b"\n")
    return digest.hexdigest()


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
    _ = parser.add_argument("--public-root", type=Path)
    _ = parser.add_argument("--confirmation", type=Path)
    args = parser.parse_args()
    root = cast("Path", args.root).resolve()
    public_root = cast("Path | None", args.public_root)
    confirmation = cast("Path | None", args.confirmation)
    try:
        validate_repository(root)
        if public_root is not None:
            validate_public_export(public_root.resolve(), confirmation)
    except (OSError, KeyError, TypeError, ValueError, subprocess.CalledProcessError) as error:
        typer.echo(f"release check failed: {error}", err=True)
        raise SystemExit(1) from error
    typer.echo("release metadata check passed")


if __name__ == "__main__":
    main()
