mod diagnostic_log;

use std::{
    fs,
    io::{BufRead, BufReader},
    path::PathBuf,
    process::{Child, Command, Stdio},
    sync::{mpsc, Arc, Mutex},
    thread,
    time::Duration,
};

use diagnostic_log::{
    DiagnosticLevel, DiagnosticLog, DiagnosticLogExportPreview, DiagnosticLogExportResult,
    DiagnosticLogQuery, DiagnosticLogQueryResult, DiagnosticLogStatus,
};

use serde::{Deserialize, Serialize};
use tauri::{
    menu::{MenuBuilder, MenuItem},
    tray::TrayIconBuilder,
    Emitter, Manager, State, WindowEvent,
};
use tauri_plugin_updater::UpdaterExt;

const PROTOCOL_VERSION: u8 = 1;
const SIDECAR_PATH: &str = "sidecar/codex-monitor-sidecar-x86_64-pc-windows-msvc.exe";
const STARTUP_TIMEOUT: Duration = Duration::from_secs(10);
const START_FAILED: &str = "Desktop service failed to start";
const START_TIMEOUT: &str = "Desktop service startup timed out";
const EXITED_EARLY: &str = "Desktop service exited before becoming ready";
const STARTUP_FAILURE_LIMIT: u8 = 3;
const STARTUP_STATE_FILE: &str = "startup-state.json";
const DESKTOP_APP_ID: &str = "com.codex-session-monitor.desktop";

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct DesktopConnection {
    port: u16,
    token: String,
    protocol_version: u8,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct AttentionActivation {
    event_key: Option<String>,
}

#[derive(Debug, Deserialize)]
struct ReadyEvent {
    event: String,
    port: u16,
    protocol_version: u8,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct AvailableUpdate {
    version: String,
    notes: Option<String>,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct UpdateDownloadProgress {
    downloaded: u64,
    total: Option<u64>,
}

#[derive(Clone, Debug, Default, Deserialize, Serialize)]
struct StartupState {
    failure_count: u8,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct RecoveryStatus {
    failure_count: u8,
    available: bool,
    recovery_url: Option<&'static str>,
}

#[derive(Clone)]
struct DesktopRuntime {
    connection: Arc<Mutex<Result<DesktopConnection, String>>>,
    child: Arc<Mutex<Option<Child>>>,
}

impl DesktopRuntime {
    fn starting() -> Self {
        Self {
            connection: Arc::new(Mutex::new(Err(START_FAILED.into()))),
            child: Arc::new(Mutex::new(None)),
        }
    }

    fn set_connection(&self, connection: Result<DesktopConnection, &str>) {
        if let Ok(mut state) = self.connection.lock() {
            *state = connection.map_err(str::to_owned);
        }
    }

    fn kill(&self) {
        if let Ok(mut child) = self.child.lock() {
            if let Some(child) = child.take() {
                let mut child = child;
                let _ = child.kill();
                let _ = child.wait();
            }
        }
    }
}

#[tauri::command]
fn desktop_connection(runtime: State<'_, DesktopRuntime>) -> Result<DesktopConnection, String> {
    runtime
        .connection
        .lock()
        .map_err(|_| START_FAILED.to_owned())?
        .clone()
}

fn record_diagnostic(
    app: &tauri::AppHandle,
    level: DiagnosticLevel,
    component: &'static str,
    event_code: &'static str,
    duration_ms: Option<u64>,
) {
    app.state::<DiagnosticLog>()
        .record(level, component, event_code, duration_ms);
}

fn record_window_failure(app: &tauri::AppHandle, event_code: &'static str) {
    record_diagnostic(app, DiagnosticLevel::Warn, "window", event_code, None);
}

#[tauri::command]
fn diagnostic_log_status(
    app: tauri::AppHandle,
    log: State<'_, DiagnosticLog>,
) -> Result<DiagnosticLogStatus, String> {
    log.status().map_err(|error| {
        record_diagnostic(
            &app,
            DiagnosticLevel::Error,
            "diagnostic",
            "status_read_failed",
            None,
        );
        error
    })
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
struct DiagnosticExportProgress {
    processed_files: usize,
    total_files: usize,
    processed_records: usize,
    processed_bytes: u64,
    total_bytes: u64,
}

#[tauri::command]
fn query_diagnostic_logs(
    app: tauri::AppHandle,
    log: State<'_, DiagnosticLog>,
    query: DiagnosticLogQuery,
) -> Result<DiagnosticLogQueryResult, String> {
    log.query(query).map_err(|error| {
        record_diagnostic(
            &app,
            DiagnosticLevel::Error,
            "diagnostic",
            "query_failed",
            None,
        );
        error
    })
}

#[tauri::command]
fn diagnostic_log_export_preview(
    app: tauri::AppHandle,
    log: State<'_, DiagnosticLog>,
) -> Result<DiagnosticLogExportPreview, String> {
    log.export_preview().map_err(|error| {
        record_diagnostic(
            &app,
            DiagnosticLevel::Error,
            "diagnostic",
            "export_preview_failed",
            None,
        );
        error
    })
}

#[tauri::command]
fn export_diagnostic_logs(
    app: tauri::AppHandle,
    log: State<'_, DiagnosticLog>,
    destination: String,
) -> Result<DiagnosticLogExportResult, String> {
    let progress_app = app.clone();
    let result = log.export(
        PathBuf::from(destination),
        move |processed_files, total_files, processed_records, processed_bytes, total_bytes| {
            let _ = progress_app.emit(
                "diagnostic-export-progress",
                DiagnosticExportProgress {
                    processed_files,
                    total_files,
                    processed_records,
                    processed_bytes,
                    total_bytes,
                },
            );
        },
    );
    result.map_err(|error| {
        record_diagnostic(
            &app,
            DiagnosticLevel::Error,
            "diagnostic",
            "export_failed",
            None,
        );
        error
    })
}

#[tauri::command]
fn cancel_diagnostic_log_export(log: State<'_, DiagnosticLog>) {
    log.cancel_export();
}

#[tauri::command]
fn update_diagnostic_log_settings(
    app: tauri::AppHandle,
    log: State<'_, DiagnosticLog>,
    capacity_mb: u16,
    enable_debug: bool,
) -> Result<DiagnosticLogStatus, String> {
    log.update_settings(capacity_mb, enable_debug)
        .map_err(|error| {
            record_diagnostic(
                &app,
                DiagnosticLevel::Error,
                "diagnostic",
                "settings_write_failed",
                None,
            );
            error
        })
}

#[tauri::command]
fn open_diagnostic_log_directory(
    app: tauri::AppHandle,
    log: State<'_, DiagnosticLog>,
) -> Result<(), String> {
    log.open_directory().map_err(|error| {
        record_diagnostic(
            &app,
            DiagnosticLevel::Error,
            "diagnostic",
            "directory_open_failed",
            None,
        );
        error
    })
}

#[tauri::command]
fn open_project_directory(app: tauri::AppHandle, path: String) -> Result<(), String> {
    let directory = PathBuf::from(path);
    if !directory.is_dir() {
        record_diagnostic(
            &app,
            DiagnosticLevel::Warn,
            "project",
            "directory_open_missing",
            None,
        );
        return Err("项目目录不存在或不可访问".to_owned());
    }
    #[cfg(target_os = "windows")]
    {
        Command::new("explorer")
            .arg(&directory)
            .spawn()
            .map_err(|_| {
                record_diagnostic(
                    &app,
                    DiagnosticLevel::Error,
                    "project",
                    "directory_open_failed",
                    None,
                );
                "无法打开项目目录".to_owned()
            })?;
        return Ok(());
    }
    #[cfg(not(target_os = "windows"))]
    {
        let _ = directory;
        record_diagnostic(
            &app,
            DiagnosticLevel::Warn,
            "project",
            "directory_open_unsupported",
            None,
        );
        Err("当前平台不支持打开项目目录".to_owned())
    }
}

#[tauri::command]
fn clear_diagnostic_logs(
    app: tauri::AppHandle,
    log: State<'_, DiagnosticLog>,
) -> Result<DiagnosticLogStatus, String> {
    log.clear().map_err(|error| {
        record_diagnostic(
            &app,
            DiagnosticLevel::Error,
            "diagnostic",
            "clear_failed",
            None,
        );
        error
    })
}

fn same_major(current: &semver::Version, candidate: &str) -> bool {
    semver::Version::parse(candidate).is_ok_and(|version| version.major == current.major)
}

fn same_major_version(app: &tauri::AppHandle, update: &tauri_plugin_updater::Update) -> bool {
    same_major(&app.package_info().version, &update.version)
}

#[tauri::command]
async fn check_for_update(app: tauri::AppHandle) -> Result<Option<AvailableUpdate>, String> {
    let update = app
        .updater()
        .map_err(|error| {
            record_diagnostic(
                &app,
                DiagnosticLevel::Warn,
                "updater",
                "updater_unavailable",
                None,
            );
            error.to_string()
        })?
        .check()
        .await
        .map_err(|error| {
            record_diagnostic(
                &app,
                DiagnosticLevel::Warn,
                "updater",
                "update_check_failed",
                None,
            );
            error.to_string()
        })?;

    Ok(update
        .filter(|update| same_major_version(&app, update))
        .map(|update| AvailableUpdate {
            version: update.version,
            notes: update.body,
        }))
}

#[tauri::command]
async fn install_verified_update(
    app: tauri::AppHandle,
    runtime: State<'_, DesktopRuntime>,
) -> Result<Option<AvailableUpdate>, String> {
    let runtime = runtime.inner().clone();
    let Some(update) = app
        .updater()
        .map_err(|error| {
            record_diagnostic(
                &app,
                DiagnosticLevel::Warn,
                "updater",
                "updater_unavailable",
                None,
            );
            error.to_string()
        })?
        .check()
        .await
        .map_err(|error| {
            record_diagnostic(
                &app,
                DiagnosticLevel::Warn,
                "updater",
                "update_check_failed",
                None,
            );
            error.to_string()
        })?
    else {
        return Ok(None);
    };
    if !same_major_version(&app, &update) {
        return Ok(None);
    }
    let available = AvailableUpdate {
        version: update.version.clone(),
        notes: update.body.clone(),
    };
    let progress_app = app.clone();
    let mut downloaded = 0_u64;
    let bytes = update
        .download(
            move |chunk, total| {
                downloaded = downloaded.saturating_add(chunk as u64);
                let _ = progress_app.emit(
                    "update-download-progress",
                    UpdateDownloadProgress { downloaded, total },
                );
            },
            || {},
        )
        .await
        .map_err(|error| {
            record_diagnostic(
                &app,
                DiagnosticLevel::Warn,
                "updater",
                "update_download_failed",
                None,
            );
            error.to_string()
        })?;

    runtime.kill();
    if let Err(error) = update.install(bytes) {
        start_sidecar(&app, &runtime);
        record_diagnostic(
            &app,
            DiagnosticLevel::Error,
            "updater",
            "update_install_failed",
            None,
        );
        return Err(error.to_string());
    }
    Ok(Some(available))
}

fn startup_state_path(app: &tauri::AppHandle) -> Result<PathBuf, String> {
    app.path()
        .app_local_data_dir()
        .map(|directory| directory.join(STARTUP_STATE_FILE))
        .map_err(|error| error.to_string())
}

fn read_startup_state(app: &tauri::AppHandle) -> StartupState {
    startup_state_path(app)
        .ok()
        .and_then(|path| fs::read(path).ok())
        .and_then(|bytes| serde_json::from_slice(&bytes).ok())
        .unwrap_or_default()
}

fn write_startup_state(app: &tauri::AppHandle, state: &StartupState) -> Result<(), String> {
    let path = startup_state_path(app)?;
    let parent = path.parent().ok_or_else(|| START_FAILED.to_owned())?;
    fs::create_dir_all(parent).map_err(|error| error.to_string())?;
    let bytes = serde_json::to_vec(state).map_err(|error| error.to_string())?;
    fs::write(path, bytes).map_err(|error| error.to_string())
}

fn record_startup_attempt(app: &tauri::AppHandle) {
    let mut state = read_startup_state(app);
    state.failure_count = state.failure_count.saturating_add(1);
    let _ = write_startup_state(app, &state);
}

#[tauri::command]
fn mark_desktop_healthy(app: tauri::AppHandle) -> Result<(), String> {
    write_startup_state(&app, &StartupState::default())
}

#[tauri::command]
fn recovery_status(app: tauri::AppHandle) -> RecoveryStatus {
    let failure_count = read_startup_state(&app).failure_count;
    let recovery_url = option_env!("CODEX_MONITOR_RECOVERY_URL").filter(|url| !url.is_empty());
    RecoveryStatus {
        failure_count,
        available: failure_count >= STARTUP_FAILURE_LIMIT,
        recovery_url,
    }
}

fn valid_event_key(value: &str) -> bool {
    value.len() == 64
        && value.bytes().all(|byte| {
            byte.is_ascii_digit() || (byte.is_ascii_lowercase() && byte.is_ascii_hexdigit())
        })
}

fn restore_attention_inbox(app: &tauri::AppHandle, event_key: Option<String>) {
    if let Some(window) = app.get_webview_window("main") {
        if window.unminimize().is_err() {
            record_window_failure(app, "window_unminimize_failed");
        }
        if window.show().is_err() {
            record_window_failure(app, "window_show_failed");
        }
        if window.set_focus().is_err() {
            record_window_failure(app, "window_focus_failed");
        }
    }
    let activation_result = app.emit("open-attention-inbox", AttentionActivation { event_key });
    if activation_result.is_err() {
        record_window_failure(app, "attention_inbox_emit_failed");
    }
}

#[cfg(target_os = "windows")]
fn is_default_notification_activation(response: &notify_rust::NotificationResponse) -> bool {
    matches!(response, notify_rust::NotificationResponse::Default)
}

#[tauri::command]
fn send_attention_notification(
    app: tauri::AppHandle,
    count: u16,
    event_key: Option<String>,
    kind: Option<String>,
) -> Result<(), String> {
    if count == 0 {
        return Ok(());
    }

    #[cfg(target_os = "windows")]
    {
        let event_key = event_key.filter(|value| valid_event_key(value));
        let body = match kind.as_deref() {
            Some("no_progress") => format!("有 {count} 个会话长时间没有新进展"),
            Some("long_running_tool") => format!("有 {count} 个会话的工具执行时间较长"),
            _ => format!("有 {count} 个会话需要关注"),
        };
        let mut notification = notify_rust::Notification::new();
        notification.summary("Codex Session Monitor").body(&body);
        if !cfg!(debug_assertions) {
            notification.app_id(DESKTOP_APP_ID);
        }
        let handle = notification.show().map_err(|_| {
            record_diagnostic(
                &app,
                DiagnosticLevel::Warn,
                "notification",
                "native_notification_show_failed",
                None,
            );
            "无法显示 Windows 通知".to_owned()
        })?;
        record_diagnostic(
            &app,
            DiagnosticLevel::Debug,
            "notification",
            "native_notification_shown",
            None,
        );
        thread::spawn(move || {
            let _ =
                handle.wait_for_response(move |response: &notify_rust::NotificationResponse| {
                    if is_default_notification_activation(response) {
                        let activation_app = app.clone();
                        record_diagnostic(
                            &activation_app,
                            DiagnosticLevel::Debug,
                            "notification",
                            "native_notification_activated",
                            None,
                        );
                        let _ = app.run_on_main_thread(move || {
                            restore_attention_inbox(&activation_app, event_key)
                        });
                    }
                });
        });
        return Ok(());
    }

    #[cfg(not(target_os = "windows"))]
    {
        let _ = app;
        Err("Native notification activation is only available on Windows".to_owned())
    }
}

fn startup_token() -> Result<String, &'static str> {
    let mut bytes = [0_u8; 32];
    getrandom::fill(&mut bytes).map_err(|_| START_FAILED)?;
    Ok(bytes.iter().map(|byte| format!("{byte:02x}")).collect())
}

fn parse_ready(bytes: &[u8], token: &str) -> Option<DesktopConnection> {
    let event: ReadyEvent = serde_json::from_slice(bytes).ok()?;
    (event.event == "ready" && event.port > 0 && event.protocol_version == PROTOCOL_VERSION).then(
        || DesktopConnection {
            port: event.port,
            token: token.to_owned(),
            protocol_version: event.protocol_version,
        },
    )
}

fn start_sidecar(app: &tauri::AppHandle, runtime: &DesktopRuntime) {
    let token = match startup_token() {
        Ok(token) => token,
        Err(error) => {
            runtime.set_connection(Err(error));
            record_diagnostic(
                app,
                DiagnosticLevel::Error,
                "sidecar",
                "startup_token_failed",
                None,
            );
            return;
        }
    };
    let executable = match std::env::current_exe()
        .ok()
        .and_then(|path| path.parent().map(|parent| parent.join(SIDECAR_PATH)))
    {
        Some(path) => path,
        None => {
            runtime.set_connection(Err(START_FAILED));
            record_diagnostic(
                app,
                DiagnosticLevel::Error,
                "sidecar",
                "executable_path_failed",
                None,
            );
            return;
        }
    };
    let mut command = Command::new(executable);
    command
        .env("CODEX_MONITOR_DESKTOP_PORT", "0")
        .env("CODEX_MONITOR_DESKTOP_TOKEN", &token)
        .env(
            "CODEX_MONITOR_DESKTOP_PROTOCOL",
            PROTOCOL_VERSION.to_string(),
        )
        .stdin(Stdio::null())
        .stdout(Stdio::piped())
        .stderr(Stdio::null());
    // Hide any console allocated for the sidecar (console or leftover windowed builds).
    #[cfg(windows)]
    {
        use std::os::windows::process::CommandExt;
        const CREATE_NO_WINDOW: u32 = 0x0800_0000;
        command.creation_flags(CREATE_NO_WINDOW);
    }
    let mut child = match command.spawn()
    {
        Ok(child) => child,
        Err(_) => {
            runtime.set_connection(Err(START_FAILED));
            record_diagnostic(
                app,
                DiagnosticLevel::Error,
                "sidecar",
                "process_start_failed",
                None,
            );
            return;
        }
    };
    let stdout = match child.stdout.take() {
        Some(stdout) => stdout,
        None => {
            let _ = child.kill();
            let _ = child.wait();
            runtime.set_connection(Err(START_FAILED));
            record_diagnostic(
                app,
                DiagnosticLevel::Error,
                "sidecar",
                "stdout_unavailable",
                None,
            );
            return;
        }
    };
    if let Ok(mut state) = runtime.child.lock() {
        *state = Some(child);
    } else {
        let _ = child.kill();
        let _ = child.wait();
        runtime.set_connection(Err(START_FAILED));
        record_diagnostic(
            app,
            DiagnosticLevel::Error,
            "sidecar",
            "runtime_state_failed",
            None,
        );
        return;
    }

    let (ready_tx, ready_rx) = mpsc::sync_channel(1);
    let event_runtime = runtime.clone();
    let diagnostics_app = app.clone();
    thread::spawn(move || {
        let mut startup_complete = false;
        for line in BufReader::new(stdout).split(b'\n') {
            let Ok(bytes) = line else {
                break;
            };
            if !startup_complete {
                if let Some(connection) = parse_ready(&bytes, &token) {
                    event_runtime.set_connection(Ok(connection));
                    record_diagnostic(
                        &diagnostics_app,
                        DiagnosticLevel::Debug,
                        "sidecar",
                        "startup_ready",
                        None,
                    );
                    startup_complete = true;
                    let _ = ready_tx.send(true);
                }
            } else if !bytes.is_empty() {
                diagnostics_app
                    .state::<DiagnosticLog>()
                    .ingest_sidecar_line(&bytes);
                let _ = diagnostics_app.emit("diagnostic-log-appended", ());
            }
        }
        if !startup_complete {
            event_runtime.set_connection(Err(EXITED_EARLY));
            record_diagnostic(
                &diagnostics_app,
                DiagnosticLevel::Error,
                "sidecar",
                "exited_before_ready",
                None,
            );
            let _ = ready_tx.send(false);
        } else {
            record_diagnostic(
                &diagnostics_app,
                DiagnosticLevel::Warn,
                "sidecar",
                "stdout_closed_after_ready",
                None,
            );
        }
    });

    match ready_rx.recv_timeout(STARTUP_TIMEOUT) {
        Ok(true) => {}
        Ok(false) => runtime.kill(),
        Err(_) => {
            runtime.set_connection(Err(START_TIMEOUT));
            runtime.kill();
            record_diagnostic(
                app,
                DiagnosticLevel::Error,
                "sidecar",
                "startup_timeout",
                None,
            );
        }
    }
}

pub fn run() {
    let runtime = DesktopRuntime::starting();
    let setup_runtime = runtime.clone();
    let app = tauri::Builder::default()
        .plugin(tauri_plugin_single_instance::init(|app, _, _| {
            if let Some(window) = app.get_webview_window("main") {
                if window.show().is_err() {
                    record_window_failure(app, "window_show_failed");
                }
                if window.set_focus().is_err() {
                    record_window_failure(app, "window_focus_failed");
                }
            }
        }))
        .plugin(tauri_plugin_dialog::init())
        .plugin(tauri_plugin_opener::init())
        .plugin(tauri_plugin_notification::init())
        .plugin(tauri_plugin_updater::Builder::new().build())
        .manage(runtime)
        .manage(DiagnosticLog::for_desktop())
        .invoke_handler(tauri::generate_handler![
            desktop_connection,
            diagnostic_log_status,
            query_diagnostic_logs,
            diagnostic_log_export_preview,
            export_diagnostic_logs,
            cancel_diagnostic_log_export,
            update_diagnostic_log_settings,
            open_diagnostic_log_directory,
            open_project_directory,
            clear_diagnostic_logs,
            check_for_update,
            install_verified_update,
            mark_desktop_healthy,
            recovery_status,
            send_attention_notification
        ])
        .setup(move |app| {
            record_startup_attempt(app.handle());
            start_sidecar(app.handle(), &setup_runtime);
            let show = MenuItem::with_id(app, "show", "Show", true, None::<&str>)?;
            let quit = MenuItem::with_id(app, "quit", "Quit", true, None::<&str>)?;
            let menu = MenuBuilder::new(app).items(&[&show, &quit]).build()?;
            let _tray = TrayIconBuilder::with_id("main-tray")
                .icon(
                    app.default_window_icon()
                        .expect("application icon is missing")
                        .clone(),
                )
                .menu(&menu)
                .on_menu_event(|app, event| match event.id.as_ref() {
                    "show" => {
                        if let Some(window) = app.get_webview_window("main") {
                            if window.show().is_err() {
                                record_window_failure(app, "window_show_failed");
                            }
                            if window.set_focus().is_err() {
                                record_window_failure(app, "window_focus_failed");
                            }
                        }
                    }
                    "quit" => app.exit(0),
                    _ => {}
                })
                .build(app)?;
            Ok(())
        })
        .on_window_event(|window, event| match event {
            WindowEvent::CloseRequested { api, .. } => {
                api.prevent_close();
                if window.hide().is_err() {
                    record_window_failure(&window.app_handle(), "window_hide_failed");
                }
            }
            WindowEvent::Destroyed => window.state::<DesktopRuntime>().kill(),
            _ => {}
        })
        .build(tauri::generate_context!())
        .expect("failed to build Codex Session Monitor desktop shell");

    app.run(|app, event| {
        if matches!(
            event,
            tauri::RunEvent::Exit | tauri::RunEvent::ExitRequested { .. }
        ) {
            app.state::<DesktopRuntime>().kill();
        }
    });
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn parses_valid_ready_event() {
        let connection = parse_ready(
            br#"{"event":"ready","port":49152,"protocol_version":1}"#,
            "secret",
        )
        .expect("valid readiness event");

        assert_eq!(connection.port, 49152);
        assert_eq!(connection.protocol_version, PROTOCOL_VERSION);
        assert_eq!(connection.token, "secret");
    }

    #[test]
    fn rejects_wrong_protocol_or_zero_port() {
        assert!(parse_ready(
            br#"{"event":"ready","port":0,"protocol_version":1}"#,
            "secret"
        )
        .is_none());
        assert!(parse_ready(
            br#"{"event":"ready","port":49152,"protocol_version":2}"#,
            "secret"
        )
        .is_none());
    }

    #[test]
    fn token_has_256_bits_encoded_as_hex() {
        let token = startup_token().expect("operating system randomness");
        assert_eq!(token.len(), 64);
        assert!(token.bytes().all(|byte| byte.is_ascii_hexdigit()));
    }

    #[test]
    fn accepts_only_lowercase_64_character_event_keys() {
        assert!(valid_event_key(&"a".repeat(64)));
        assert!(!valid_event_key(&"A".repeat(64)));
        assert!(!valid_event_key(&"g".repeat(64)));
        assert!(!valid_event_key(&"a".repeat(63)));
        assert!(!valid_event_key(&"a".repeat(65)));
    }

    #[test]
    fn stable_updates_stay_within_the_current_major_version() {
        let current = semver::Version::parse("6.3.0").expect("valid current version");

        assert!(same_major(&current, "6.4.1"));
        assert!(!same_major(&current, "7.0.0"));
        assert!(!same_major(&current, "not-a-version"));
    }

    #[cfg(target_os = "windows")]
    #[test]
    fn only_default_notification_activation_opens_the_attention_inbox() {
        assert!(is_default_notification_activation(
            &notify_rust::NotificationResponse::Default
        ));
        assert!(!is_default_notification_activation(
            &notify_rust::NotificationResponse::Action("other".to_owned())
        ));
        assert!(!is_default_notification_activation(
            &notify_rust::NotificationResponse::Closed(notify_rust::CloseReason::Dismissed)
        ));
    }
}
