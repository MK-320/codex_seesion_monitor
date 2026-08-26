// Prevents a console window on Windows release builds. Sidecar is already GUI via --noconsole.
#![cfg_attr(not(debug_assertions), windows_subsystem = "windows")]

fn main() {
    codex_session_monitor_lib::run();
}
