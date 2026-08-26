import inspect
import json
import tomllib
from pathlib import Path
from typing import cast

from typer.testing import CliRunner

import codex_monitor
from codex_monitor.main import app, serve


def test_package_imports() -> None:
    assert codex_monitor.__version__


def test_package_version_matches_project_metadata() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    frontend = cast(
        "dict[str, object]",
        json.loads(Path("frontend/package.json").read_text(encoding="utf-8")),
    )
    frontend_lock = cast(
        "dict[str, object]",
        json.loads(Path("frontend/package-lock.json").read_text(encoding="utf-8")),
    )
    lock_packages = cast("dict[str, object]", frontend_lock["packages"])
    lock_project = cast("dict[str, object]", lock_packages[""])

    assert codex_monitor.__version__ == project["project"]["version"]
    frontend_version = cast("str", frontend["version"])
    assert frontend_version == codex_monitor.__version__.replace(".dev", "-dev.")
    assert frontend_lock["version"] == frontend_version
    assert lock_project["version"] == frontend_version
    assert codex_monitor.__version__.replace(".dev", "-dev.") == frontend_version
    assert "websockets>=15.0" in project["project"]["dependencies"]


def test_cli_exposes_direct_serve_options() -> None:
    result = CliRunner().invoke(app, ["--help"])

    assert result.exit_code == 0
    assert {"project", "session_root", "stuck_seconds", "port"} <= set(
        inspect.signature(serve).parameters
    )


def test_cli_uses_options_without_a_subcommand(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()

    result = CliRunner().invoke(
        app,
        [
            "--project",
            str(tmp_path),
            "--session-root",
            str(session_root),
            "--stuck-seconds",
            "0",
            "--port",
            "8000",
        ],
    )

    assert result.exit_code != 0
    assert isinstance(result.exception, SystemExit)
    assert result.exception.code == 2


def test_start_script_uses_local_uv_and_loopback_defaults() -> None:
    script = Path("start-codex-monitor.ps1").read_text(encoding="utf-8")

    assert ".uv-bootstrap\\bin\\uv.exe" in script
    assert "127.0.0.1" in script
    assert "8000" in script
    assert '"run", "--no-sync", "python", "-m", "codex_monitor"' in script
    assert "[string[]]$Project" in script
    assert "& $uv @uvArgs" in script


def test_double_click_launcher_starts_script_and_opens_loopback_url() -> None:
    launcher = Path("start-codex-monitor.cmd").read_text(encoding="utf-8")

    assert "Get-NetTCPConnection" in launcher
    assert 'set "MONITOR_ROOT=%~dp0"' in launcher
    assert "Start-Process powershell.exe" in launcher
    assert "-WorkingDirectory $root" in launcher
    assert "-PassThru" in launcher
    assert "Join-Path $root 'start-codex-monitor.ps1'" in launcher
    assert "http://127.0.0.1:" in launcher
    assert "$port=8000" in launcher
    assert "@($config.projects).project_root -contains $root" in launcher
    assert "Start-Process $url" in launcher
    assert "taskkill.exe /PID $server.Id /T /F" in launcher
    assert "exit /b %status%" in launcher


def test_readme_documents_runtime_project_import() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "项目清单只在启动时确定" not in readme
    assert "导入项目" in readme
    assert "重启后恢复" in readme
    assert "--no-saved-projects" in readme
    assert "不删除项目目录或 Codex 会话文件" in readme


def test_release_entry_and_browser_smoke_exist() -> None:
    release = Path("scripts/check-release.ps1").read_text(encoding="utf-8")
    browser = Path("frontend/e2e/browser-smoke.mjs").read_text(encoding="utf-8")

    assert "codex_monitor.release_check" in release
    assert "npm run e2e" in release
    assert "diff --exit-code -- src/codex_monitor/static" in release
    assert release.index("diff --exit-code -- src/codex_monitor/static") < release.index(
        "npm run build"
    )
    assert "CODEX_RELEASE_VERSION" in release
    assert "$LASTEXITCODE" in release
    assert "finally" in browser
    assert "setViewportSize({ width: 390, height: 844 })" in browser
    assert "waitForSessionSummariesWithAttention" in browser
    assert "SESSION_SUMMARY_READY_TIMEOUT_MS = 20_000" in browser
    assert "SESSION_SUMMARY_POLL_INTERVAL_MS = 200" in browser
    assert "http://127.0.0.1:8014/api/health" in browser
    assert "lastHealth" in browser
    assert '["initial-attention", "batch-one", "batch-two", "unsafe;id"]' in browser
