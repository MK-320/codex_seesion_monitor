import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Protocol, cast

import pytest
from fastapi import WebSocketDisconnect
from fastapi.testclient import TestClient
from pydantic import SecretStr
from typer.testing import CliRunner

import codex_monitor.main as main_module
import codex_monitor.sidecar as sidecar_module
from codex_monitor.api import create_app
from codex_monitor.config import AppConfig
from codex_monitor.diagnostic_events import resolve_stdout_stream
from codex_monitor.schemas import SnapshotEvent

STARTUP_TOKEN = "a" * 64
AUTHORIZATION = {"Authorization": f"Bearer {STARTUP_TOKEN}"}
TAURI_ORIGIN = "http://tauri.localhost"
TAURI_DEV_ORIGIN = "http://127.0.0.1:1420"

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping


class SidecarSettings(Protocol):
    port: int
    startup_token: str
    protocol_version: int


def _desktop_config(tmp_path: Path) -> AppConfig:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    return AppConfig.model_validate(
        {
            "project_root": tmp_path,
            "session_root": session_root,
            "port": 0,
            "startup_token": SecretStr(STARTUP_TOKEN),
            "desktop_protocol_version": 1,
        }
    )


def test_startup_token_protects_all_sidecar_http_without_leaking(tmp_path: Path) -> None:
    with TestClient(create_app(_desktop_config(tmp_path))) as client:
        missing = client.get("/api/health")
        wrong = client.get("/api/health", headers={"Authorization": "Bearer wrong"})
        dashboard = client.get("/")
        health = client.get("/api/health", headers=AUTHORIZATION)
        diagnostics = client.get("/api/diagnostics", headers=AUTHORIZATION)

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert dashboard.status_code == 401
    assert health.status_code == 200
    assert health.json()["protocol_version"] == 1
    assert STARTUP_TOKEN not in health.text
    assert STARTUP_TOKEN not in diagnostics.text


def test_tokenless_browser_mode_remains_compatible(tmp_path: Path) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    config = AppConfig(project_root=tmp_path, session_root=session_root)

    with TestClient(create_app(config)) as client:
        assert client.get("/api/health").status_code == 200


@pytest.mark.parametrize("origin", [TAURI_ORIGIN, TAURI_DEV_ORIGIN])
def test_tauri_origin_preflight_and_authenticated_mutation_are_allowed(
    tmp_path: Path,
    origin: str,
) -> None:
    imported = tmp_path / "imported"
    imported.mkdir()

    with TestClient(create_app(_desktop_config(tmp_path))) as client:
        preflight = client.options(
            "/api/projects",
            headers={
                "Origin": origin,
                "Access-Control-Request-Method": "POST",
                "Access-Control-Request-Headers": "authorization,content-type",
            },
        )
        imported_response = client.post(
            "/api/projects",
            headers={**AUTHORIZATION, "Origin": origin},
            json={"project_root": str(imported)},
        )

    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == origin
    assert imported_response.status_code == 201
    assert imported_response.headers["access-control-allow-origin"] == origin


def test_desktop_sidecar_rejects_backend_gui_picker(tmp_path: Path) -> None:
    with TestClient(create_app(_desktop_config(tmp_path))) as client:
        response = client.post("/api/projects/pick", headers=AUTHORIZATION)

    assert response.status_code == 501


def test_websocket_requires_token_subprotocol_in_desktop_mode(tmp_path: Path) -> None:
    protocol = f"codex-monitor-v1.{STARTUP_TOKEN}"
    app = create_app(_desktop_config(tmp_path))

    with TestClient(app) as client:
        with (
            pytest.raises(WebSocketDisconnect) as missing,
            client.websocket_connect("/ws") as websocket,
        ):
            _ = websocket.receive_text()
        with (
            pytest.raises(WebSocketDisconnect) as wrong,
            client.websocket_connect(
                "/ws",
                subprotocols=["codex-monitor-v1.wrong"],
                headers={"Origin": TAURI_ORIGIN},
            ) as websocket,
        ):
            _ = websocket.receive_text()
        with client.websocket_connect(
            "/ws",
            subprotocols=[protocol],
            headers={"Origin": TAURI_ORIGIN},
        ) as websocket:
            event = SnapshotEvent.model_validate_json(websocket.receive_text())

    assert missing.value.code == 1008
    assert wrong.value.code == 1008
    assert event.protocol_version == 1


def test_sidecar_settings_require_valid_environment() -> None:
    loader = cast(
        "Callable[[Mapping[str, str]], SidecarSettings]",
        sidecar_module.load_sidecar_settings,
    )
    settings = loader(
        {
            "CODEX_MONITOR_DESKTOP_PORT": "0",
            "CODEX_MONITOR_DESKTOP_TOKEN": STARTUP_TOKEN,
            "CODEX_MONITOR_DESKTOP_PROTOCOL": "1",
        }
    )

    assert settings.port == 0
    assert settings.startup_token == STARTUP_TOKEN
    assert settings.protocol_version == 1
    for invalid in (
        {},
        {"CODEX_MONITOR_DESKTOP_PORT": "not-a-port"},
        {"CODEX_MONITOR_DESKTOP_PORT": "0", "CODEX_MONITOR_DESKTOP_TOKEN": "short"},
        {
            "CODEX_MONITOR_DESKTOP_PORT": "0",
            "CODEX_MONITOR_DESKTOP_TOKEN": STARTUP_TOKEN,
            "CODEX_MONITOR_DESKTOP_PROTOCOL": "2",
        },
    ):
        with pytest.raises(ValueError, match="Desktop sidecar"):
            _ = loader(invalid)


def test_sidecar_does_not_implicitly_monitor_user_home(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    session_root = tmp_path / "sessions"
    session_root.mkdir()
    captured: list[AppConfig] = []
    monkeypatch.setattr(main_module, "run_sidecar", captured.append)

    result = CliRunner().invoke(
        main_module.app,
        ["--sidecar", "--session-root", str(session_root)],
        env={
            "CODEX_MONITOR_DESKTOP_PORT": "0",
            "CODEX_MONITOR_DESKTOP_TOKEN": STARTUP_TOKEN,
            "CODEX_MONITOR_DESKTOP_PROTOCOL": "1",
        },
    )

    assert result.exit_code == 0
    assert captured[0].project_roots == ()


def test_pyinstaller_build_script_targets_tauri_onedir_resource() -> None:
    script = Path("scripts/build-sidecar.ps1").read_text(encoding="utf-8")
    gitignore = Path(".gitignore").read_text(encoding="utf-8")

    assert "pyinstaller" in script
    assert "--onedir" in script
    assert "--noconsole" in script
    assert "codex-monitor-sidecar-x86_64-pc-windows-msvc" in script
    assert "frontend\\src-tauri\\binaries" in script
    assert "--startup-token" not in script
    assert "CODEX_MONITOR_DESKTOP_TOKEN" not in script
    assert "frontend/src-tauri/binaries/" in gitignore


def test_ready_event_is_non_sensitive() -> None:
    event = {
        "event": "ready",
        "port": 49152,
        "protocol_version": 1,
    }

    encoded = json.dumps(event)
    assert STARTUP_TOKEN not in encoded
    assert set(event) == {"event", "port", "protocol_version"}


def test_resolve_stdout_stream_restores_fd_when_windowed_stdout_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original = sys.stdout
    monkeypatch.setattr(sys, "stdout", None)
    try:
        restored = resolve_stdout_stream()
        assert restored is not None
        assert not restored.closed
        _ = restored.write("")
        _ = restored.flush()
        assert sys.stdout is restored
    finally:
        sys.stdout = original
