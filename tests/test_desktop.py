import json
import tomllib
from pathlib import Path
from typing import cast


def test_tauri_shell_uses_existing_frontend_and_v2() -> None:
    cargo = cast(
        "dict[str, object]",
        tomllib.loads(Path("frontend/src-tauri/Cargo.toml").read_text(encoding="utf-8")),
    )
    dependencies = cast("dict[str, object]", cargo["dependencies"])
    tauri = dependencies["tauri"]
    tauri_version = tauri if isinstance(tauri, str) else cast("dict[str, str]", tauri)["version"]
    config = cast(
        "dict[str, object]",
        json.loads(Path("frontend/src-tauri/tauri.conf.json").read_text(encoding="utf-8")),
    )
    build = cast("dict[str, object]", config["build"])
    app = cast("dict[str, object]", config["app"])
    windows = cast("list[dict[str, object]]", app["windows"])

    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    project_metadata = cast("dict[str, str]", project["project"])
    assert cast("dict[str, str]", cargo["package"])["version"] == project_metadata[
        "version"
    ].replace(".dev", "-dev.")
    assert tauri_version.startswith("2")
    assert cast("str", config["productName"]) == "Codex Session Monitor"
    assert cast("str", build["devUrl"]) == "http://127.0.0.1:1420"
    assert cast("str", build["frontendDist"]) == "../dist"
    assert cast("str", windows[0]["title"]) == "Codex Session Monitor"
    assert cast("int", windows[0]["minWidth"]) >= 960
    assert cast("int", windows[0]["minHeight"]) >= 640
    assert Path("frontend/src-tauri/icons/icon.ico").is_file()


def test_v633_local_tauri_config_initializes_inert_updater() -> None:
    config = cast(
        "dict[str, object]",
        json.loads(Path("frontend/src-tauri/tauri.conf.json").read_text(encoding="utf-8")),
    )
    plugins = cast("dict[str, object]", config["plugins"])

    assert plugins["updater"] == {"endpoints": [], "pubkey": ""}


def test_desktop_scripts_reuse_react_and_delegate_sidecar_to_tauri() -> None:
    package = cast(
        "dict[str, object]",
        json.loads(Path("frontend/package.json").read_text(encoding="utf-8")),
    )
    scripts = cast("dict[str, str]", package["scripts"])
    dev_script = Path("scripts/start-desktop-dev.ps1").read_text(encoding="utf-8")
    tauri_script = Path("scripts/start-tauri-dev.ps1").read_text(encoding="utf-8")
    desktop_build_script = Path("scripts/build-desktop.ps1").read_text(encoding="utf-8")
    desktop_vite = Path("frontend/vite.desktop.config.ts").read_text(encoding="utf-8")
    build_script = Path("frontend/src-tauri/build.rs").read_text(encoding="utf-8")

    assert "start-tauri-dev.ps1" in scripts["desktop:dev"]
    assert scripts["build:desktop"] == "vite build --config vite.desktop.config.ts"
    assert scripts["build:static"] == "vite build --config vite.config.ts"
    assert scripts["dev:desktop"] == "vite --config vite.desktop.config.ts"
    assert "build-sidecar.ps1" in dev_script
    assert "python -m codex_monitor" not in dev_script
    assert "8766" not in dev_script
    assert "npm run desktop:dev" in dev_script
    assert '"E:\\Rust"' in dev_script
    assert "$env:CARGO_TARGET_DIR = $targetRoot" in dev_script
    assert '$targetRoot = Join-Path $repoRoot ".cargo-target"' in dev_script
    assert '$env:CARGO_TARGET_DIR = Join-Path $repoRoot ".cargo-target"' in tauri_script
    assert "npm.cmd exec tauri dev" in tauri_script
    assert "build:static" in desktop_build_script
    assert "build-sidecar.ps1" in desktop_build_script
    assert "sidecarStaticRoot" in desktop_build_script
    assert "codex_monitor\\static" in desktop_build_script
    assert '"run", "dev:desktop"' in tauri_script
    assert "127.0.0.1:1420" in tauri_script
    assert 'GetEnvironmentVariables("Process")' in tauri_script
    assert "cargo:rerun-if-changed=tauri.conf.json" in build_script
    assert "cargo:rerun-if-changed=capabilities/default.json" in build_script
    assert "vcvars64.bat" in dev_script
    assert 'outDir: "dist"' in desktop_vite
    assert "proxy" in desktop_vite
    assert "changeOrigin: true" in desktop_vite
    assert "rewriteWsOrigin: true" in desktop_vite
    assert 'setHeader("origin", backendOrigin)' in desktop_vite


def test_browser_dev_script_starts_and_stops_backend() -> None:
    package = cast(
        "dict[str, object]",
        json.loads(Path("frontend/package.json").read_text(encoding="utf-8")),
    )
    scripts = cast("dict[str, str]", package["scripts"])
    dev_script = Path("scripts/start-browser-dev.ps1").read_text(encoding="utf-8")

    assert "start-browser-dev.ps1" in scripts["dev"]
    assert scripts["dev:vite"] == "vite"
    assert "codex_monitor" in dev_script
    assert "--no-saved-projects" in dev_script
    assert "/api/health" in dev_script
    assert "taskkill.exe" in dev_script
    assert 'GetEnvironmentVariables("Process")' in dev_script
    assert "CODEX_MONITOR_VITE_PORT" in dev_script
    assert "Get-MonitorHealth" in dev_script
    assert "Test-ViteDevServer" in dev_script
    assert "strictPort: true" in Path("frontend/vite.config.ts").read_text(encoding="utf-8")


def test_desktop_dev_refreshes_a_stale_sidecar_without_recursive_deletion() -> None:
    dev_script = Path("scripts/start-desktop-dev.ps1").read_text(encoding="utf-8")
    sidecar_build = Path("scripts/build-sidecar.ps1").read_text(encoding="utf-8")

    assert "function Test-SidecarRefreshRequired" in dev_script
    assert 'Join-Path $RepositoryRoot "src"' in dev_script
    assert "$sidecarBuild -Refresh" in dev_script
    assert "[switch]$Refresh" in sidecar_build
    assert "refresh-dist" in sidecar_build
    assert "Copy-Item -LiteralPath" in sidecar_build
    assert "Remove-Item" not in sidecar_build


def test_v610_versions_are_synchronized() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    package = cast(
        "dict[str, object]",
        json.loads(Path("frontend/package.json").read_text(encoding="utf-8")),
    )

    project_metadata = cast("dict[str, str]", project["project"])
    assert package["version"] == project_metadata["version"].replace(".dev", "-dev.")


def test_tauri_owns_authenticated_sidecar_runtime() -> None:
    cargo = Path("frontend/src-tauri/Cargo.toml").read_text(encoding="utf-8")
    config = Path("frontend/src-tauri/tauri.conf.json").read_text(encoding="utf-8")
    rust = Path("frontend/src-tauri/src/lib.rs").read_text(encoding="utf-8")
    main_rs = Path("frontend/src-tauri/src/main.rs").read_text(encoding="utf-8")
    desktop = Path("frontend/src/desktop.ts").read_text(encoding="utf-8")

    assert 'windows_subsystem = "windows"' in main_rs
    assert "tauri-plugin-shell" not in cargo
    assert "getrandom" in cargo
    assert '"externalBin"' not in config
    assert '"resources"' in config
    assert "codex-monitor-sidecar" in config
    assert '"sidecar/"' in config
    assert "http://127.0.0.1:*" in config
    assert "ws://127.0.0.1:*" in config
    assert "process::{Child, Command, Stdio}" in rust
    assert "BufReader" in rust
    sidecar_path = (
        'const SIDECAR_PATH: &str = "sidecar/codex-monitor-sidecar-x86_64-pc-windows-msvc.exe";'
    )
    assert sidecar_path in rust
    assert "CODEX_MONITOR_DESKTOP_TOKEN" in rust
    assert ".kill()" in rust
    assert "desktop_connection" in rust
    assert "Authorization" in desktop
    assert "codex-monitor-v1" in desktop
    assert "localStorage" not in desktop
    assert "URLSearchParams" not in desktop


def test_v620_uses_native_lifecycle_and_directory_picker() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    package = cast(
        "dict[str, object]",
        json.loads(Path("frontend/package.json").read_text(encoding="utf-8")),
    )
    cargo = Path("frontend/src-tauri/Cargo.toml").read_text(encoding="utf-8")
    capabilities = Path("frontend/src-tauri/capabilities/default.json").read_text(encoding="utf-8")
    rust = Path("frontend/src-tauri/src/lib.rs").read_text(encoding="utf-8")
    desktop = Path("frontend/src/desktop.ts").read_text(encoding="utf-8")
    sidecar_build = Path("scripts/build-sidecar.ps1").read_text(encoding="utf-8")

    project_metadata = cast("dict[str, str]", project["project"])
    assert package["version"] == project_metadata["version"].replace(".dev", "-dev.")
    assert "@tauri-apps/plugin-dialog" in cast("dict[str, str]", package["dependencies"])
    assert "tauri-plugin-dialog" in cargo
    assert "tauri-plugin-single-instance" in cargo
    assert "dialog:allow-open" in capabilities
    assert "dialog:allow-save" in capabilities
    assert "tauri_plugin_single_instance" in rust
    assert rust.index(".plugin(tauri_plugin_single_instance::init") < rust.index(
        ".plugin(tauri_plugin_dialog::init())"
    )
    assert "TrayIconBuilder" in rust
    assert ".icon(" in rust
    assert "app.default_window_icon()" in rust
    assert "CloseRequested" in rust
    assert "prevent_close" in rust
    assert "@tauri-apps/plugin-dialog" in desktop
    assert 'import { downloadDir, join } from "@tauri-apps/api/path";' in desktop
    assert 'join(await downloadDir(), "codex-session-monitor-diagnostics.zip")' in desktop
    assert "pickProjectDirectory" in desktop
    assert "--exclude-module tkinter" in sidecar_build
    assert "--exclude-module _tkinter" in sidecar_build
    assert "--noconsole" in sidecar_build
    assert "CREATE_NO_WINDOW" in rust
    assert "creation_flags(CREATE_NO_WINDOW)" in rust
    assert "resolve_stdout_stream" in Path("src/codex_monitor/sidecar.py").read_text(
        encoding="utf-8"
    )


def test_desktop_directory_picker_persists_imported_projects() -> None:
    sessions = Path("frontend/src/useSessions.ts").read_text(encoding="utf-8")

    assert "body: JSON.stringify({ project_root: selected, persist: true })" in sessions


def test_denied_notification_button_does_not_use_busy_cursor() -> None:
    app = Path("frontend/src/App.tsx").read_text(encoding="utf-8")
    styles = Path("frontend/src/styles.css").read_text(encoding="utf-8")

    assert (
        'className="icon-button notification-toggle"\n'
        '            type="button"\n'
        '            aria-label={notifications.enabled ? "关闭桌面提醒" : "开启桌面提醒"}'
    ) in app
    assert ".notification-toggle:disabled { cursor: not-allowed; }" in styles


def test_v630_release_overlay_builds_a_safe_windows_nsis_package() -> None:
    release_config = cast(
        "dict[str, object]",
        json.loads(Path("frontend/src-tauri/tauri.release.conf.json").read_text(encoding="utf-8")),
    )
    bundle = cast("dict[str, object]", release_config["bundle"])
    windows = cast("dict[str, object]", bundle["windows"])

    assert set(release_config) == {"bundle"}
    assert bundle["active"] is True
    assert bundle["targets"] == ["nsis"]
    assert bundle["createUpdaterArtifacts"] is True
    assert windows["webviewInstallMode"] == {"type": "embedBootstrapper"}
    assert windows["nsis"] == {
        "installMode": "currentUser",
        "installerHooks": "./windows/hooks.nsh",
    }
    uninstall_hooks = Path("frontend/src-tauri/windows/hooks.nsh").read_text(encoding="utf-8")
    assert "NSIS_HOOK_PREINSTALL" in uninstall_hooks
    assert "NSIS_HOOK_PREUNINSTALL" in uninstall_hooks
    assert 'taskkill.exe" /F /T /IM "Codex Session Monitor.exe"' in uninstall_hooks
    assert (
        'taskkill.exe" /F /T /IM "codex-monitor-sidecar-x86_64-pc-windows-msvc.exe"'
        in uninstall_hooks
    )
    assert "${IfNot} ${Silent}" in uninstall_hooks
    assert '"/UPDATE"' in uninstall_hooks
    assert "$LOCALAPPDATA\\CodexSessionMonitor\\config.json" in uninstall_hooks
    assert "MB_DEFBUTTON2" in uninstall_hooks
    assert ".codex" not in uninstall_hooks
    assert 'Delete "$LOCALAPPDATA\\CodexSessionMonitor\\config.json"' in uninstall_hooks
    assert "RMDir /r" not in uninstall_hooks

    unsigned_config = cast(
        "dict[str, object]",
        json.loads(Path("frontend/src-tauri/tauri.unsigned.conf.json").read_text(encoding="utf-8")),
    )
    assert unsigned_config == {"bundle": {"createUpdaterArtifacts": False}}


def test_v630_updater_verifies_before_stopping_the_sidecar() -> None:
    cargo = Path("frontend/src-tauri/Cargo.toml").read_text(encoding="utf-8")
    rust = Path("frontend/src-tauri/src/lib.rs").read_text(encoding="utf-8")

    assert 'tauri-plugin-updater = "2.10.1"' in cargo
    assert "tauri_plugin_updater::UpdaterExt" in rust
    assert ".plugin(tauri_plugin_updater::Builder::new().build())" in rust
    assert "check_for_update" in rust
    assert "install_verified_update" in rust
    assert '"update-download-progress"' in rust
    assert "UpdateDownloadProgress" in rust
    assert "same_major_version" in rust
    assert 'semver = "1"' in cargo
    assert "semver::Version::parse" in rust
    assert "app.package_info().version" in rust
    update_command = rust.split("async fn install_verified_update", maxsplit=1)[1]
    assert update_command.index(".download(") < update_command.index("runtime.kill()")
    assert update_command.index("runtime.kill()") < update_command.index("update.install(bytes)")


def test_v630_exposes_update_and_recovery_controls() -> None:
    rust = Path("frontend/src-tauri/src/lib.rs").read_text(encoding="utf-8")
    desktop = Path("frontend/src/desktop.ts").read_text(encoding="utf-8")
    app = Path("frontend/src/App.tsx").read_text(encoding="utf-8")

    assert "STARTUP_FAILURE_LIMIT" in rust
    assert "record_startup_attempt" in rust
    assert "mark_desktop_healthy" in rust
    assert "recovery_status" in rust
    assert 'option_env!("CODEX_MONITOR_RECOVERY_URL")' in rust
    for command in (
        "check_for_update",
        "install_verified_update",
        "mark_desktop_healthy",
        "recovery_status",
    ):
        assert f'invoke("{command}")' in desktop
    assert "checkForUpdate" in app
    assert "installVerifiedUpdate" in app
    assert "listenUpdateProgress" in desktop
    assert "downloadProgress" in app
    assert "重新检查" in app
    assert all(marker in app for marker in ("安装程序未能启动", "当前版本仍保留"))
    assert "if (status.available) setUpdateOpen(true)" in app
    assert "available: failure_count >= STARTUP_FAILURE_LIMIT" in rust
    assert "当前没有可用的上一稳定桌面版" in app
    assert "recoveryStatus" in app
    assert "markDesktopHealthy" in app
    assert "AUTO_UPDATE_CHECK_DELAY_MS" in app
    assert "LAST_DISMISSED_UPDATE_KEY" in app
    assert "const hasSynced = lastSyncedAt !== null" in app
    assert "autoUpdateChecked" in app
    assert "setAvailableUpdate(update)" in app
    assert "localStorage.getItem(LAST_DISMISSED_UPDATE_KEY) !== update.version" in app
    assert "setUpdateOpen(true)" in app
    assert "localStorage.getItem(LAST_DISMISSED_UPDATE_KEY)" in app
    assert "update-dialog glass" in app


def test_v621_uses_native_tauri_notifications_and_records_patch() -> None:
    project = tomllib.loads(Path("pyproject.toml").read_text(encoding="utf-8"))
    package = cast(
        "dict[str, object]",
        json.loads(Path("frontend/package.json").read_text(encoding="utf-8")),
    )
    tauri_config = cast(
        "dict[str, object]",
        json.loads(Path("frontend/src-tauri/tauri.conf.json").read_text(encoding="utf-8")),
    )
    cargo = Path("frontend/src-tauri/Cargo.toml").read_text(encoding="utf-8")
    capabilities = Path("frontend/src-tauri/capabilities/default.json").read_text(encoding="utf-8")
    rust = Path("frontend/src-tauri/src/lib.rs").read_text(encoding="utf-8")
    notifications = Path("frontend/src/useAttentionNotifications.ts").read_text(encoding="utf-8")

    project_metadata = cast("dict[str, str]", project["project"])
    expected_frontend_version = project_metadata["version"].replace(".dev", "-dev.")
    assert package["version"] == expected_frontend_version
    assert tauri_config["version"] == expected_frontend_version
    assert (
        cast("dict[str, str]", package["dependencies"])["@tauri-apps/plugin-notification"]
        == "2.3.3"
    )
    assert 'tauri-plugin-notification = "2.3.3"' in cargo
    assert ".plugin(tauri_plugin_notification::init())" in rust
    for permission in (
        "notification:allow-is-permission-granted",
        "notification:allow-request-permission",
        "notification:allow-notify",
    ):
        assert permission in capabilities
    assert "@tauri-apps/plugin-notification" in notifications
    assert "isDesktopRuntime" in notifications
    assert "sendNotification" in notifications
    assert "Notification.requestPermission()" in notifications


def test_v637_notification_activation_preserves_a_valid_internal_target() -> None:
    cargo = Path("frontend/src-tauri/Cargo.toml").read_text(encoding="utf-8")
    rust = Path("frontend/src-tauri/src/lib.rs").read_text(encoding="utf-8")
    desktop = Path("frontend/src/desktop.ts").read_text(encoding="utf-8")
    app = Path("frontend/src/App.tsx").read_text(encoding="utf-8")
    notifications = Path("frontend/src/useAttentionNotifications.ts").read_text(encoding="utf-8")

    assert 'notify-rust = "4.18.0"' in cargo
    assert "send_attention_notification" in rust
    assert "NotificationResponse::Default" in rust
    assert (
        rust.index("window.unminimize()")
        < rust.index("window.show()")
        < rust.index("window.set_focus()")
    )
    assert "struct AttentionActivation" in rust
    assert "valid_event_key" in rust
    assert 'app.emit("open-attention-inbox", AttentionActivation { event_key })' in rust
    assert 'invoke("send_attention_notification", { count, eventKey, kind })' in desktop
    assert 'listen<AttentionActivation>("open-attention-inbox"' in desktop
    assert "notificationEventKey(newKeys)" in notifications
    assert "sendAttentionNotification(newKeys.length, eventKey, kind)" in notifications
    assert "notification.onclick" in notifications
    assert "openAttentionInbox" in app
    assert "targets.length === 1 ? targets[0] : null" in app
    assert 'setFilter("attention")' in app
    assert "ordered.some((session) => session.session_key === selectedId)" in app
