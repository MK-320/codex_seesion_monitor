use std::{
    cmp::Reverse,
    fs::{self, File, OpenOptions},
    io::Write,
    path::PathBuf,
    sync::{
        atomic::{AtomicBool, Ordering},
        Mutex,
    },
    time::{Duration, SystemTime, UNIX_EPOCH},
};

use serde::{Deserialize, Serialize};

const DEFAULT_CAPACITY_MB: u16 = 50;
const MIN_CAPACITY_MB: u16 = 5;
const MAX_CAPACITY_MB: u16 = 500;
const FILE_LIMIT_BYTES: u64 = 5 * 1024 * 1024;
const RECORD_LIMIT_BYTES: usize = 4 * 1024;
const DEBUG_DURATION: Duration = Duration::from_secs(30 * 60);
const SETTINGS_FILE: &str = "diagnostics-settings.json";
const LOG_SCHEMA_VERSION: u8 = 2;
const ACTIVE_LOG_FILE: &str = "diagnostics-v2-current.jsonl";
const ROLLED_LOG_PREFIX: &str = "diagnostics-v2-";
const MAX_QUERY_ENTRIES: usize = 1000;

#[derive(Clone, Copy, Debug, Deserialize, Eq, PartialEq, Serialize)]
pub(crate) enum DiagnosticLevel {
    #[serde(rename = "DEBUG", alias = "debug")]
    Debug,
    #[serde(rename = "WARN", alias = "warn")]
    Warn,
    #[serde(rename = "ERROR", alias = "error")]
    Error,
}

impl DiagnosticLevel {
    fn is_enabled(self, debug_until_ms: u64) -> bool {
        !matches!(self, Self::Debug) || now_ms() < debug_until_ms
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "camelCase")]
struct DiagnosticLogSettings {
    capacity_mb: u16,
    debug_until_ms: u64,
    #[serde(default)]
    schema_version: u8,
}

impl Default for DiagnosticLogSettings {
    fn default() -> Self {
        Self {
            capacity_mb: DEFAULT_CAPACITY_MB,
            debug_until_ms: 0,
            schema_version: LOG_SCHEMA_VERSION,
        }
    }
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
#[serde(deny_unknown_fields)]
pub(crate) struct DiagnosticLogEntry {
    schema_version: u8,
    timestamp: String,
    level: DiagnosticLevel,
    source: String,
    component: String,
    event_code: String,
    #[serde(skip_serializing_if = "Option::is_none")]
    operation_id: Option<String>,
    duration_ms: Option<u64>,
    #[serde(skip_serializing_if = "Option::is_none")]
    path_category: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    path_alias: Option<String>,
    #[serde(skip_serializing_if = "Option::is_none")]
    network: Option<DiagnosticNetworkFields>,
    error_summary: Option<String>,
}

#[derive(Clone, Debug, Deserialize, Serialize)]
#[serde(rename_all = "snake_case")]
#[serde(deny_unknown_fields)]
pub(crate) struct DiagnosticNetworkFields {
    protocol: String,
    scope: String,
    http_status: Option<u16>,
    close_code: Option<u16>,
    system_error_code: Option<u32>,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct DiagnosticLogStatus {
    debug_enabled: bool,
    debug_remaining_seconds: u64,
    capacity_mb: u16,
    used_bytes: u64,
    recent_entries: Vec<DiagnosticLogEntry>,
    last_error: Option<&'static str>,
}

#[derive(Clone, Debug, Deserialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct DiagnosticLogQuery {
    pub(crate) level: Option<DiagnosticLevel>,
    pub(crate) component: Option<String>,
    pub(crate) event_code: Option<String>,
    pub(crate) keyword: Option<String>,
    pub(crate) since_hours: Option<u8>,
    pub(crate) cursor: Option<String>,
    pub(crate) page_size: Option<u16>,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct DiagnosticLogQueryResult {
    pub(crate) entries: Vec<DiagnosticLogEntry>,
    pub(crate) next_cursor: Option<String>,
    pub(crate) total: usize,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct DiagnosticLogExportPreview {
    pub(crate) file_count: usize,
    pub(crate) estimated_bytes: u64,
    pub(crate) record_count: usize,
}

#[derive(Clone, Debug, Serialize)]
#[serde(rename_all = "camelCase")]
pub(crate) struct DiagnosticLogExportResult {
    pub(crate) status: String,
    pub(crate) file_count: usize,
    pub(crate) invalid_record_count: usize,
    pub(crate) failed_file_count: usize,
    pub(crate) replaced_record_count: usize,
    pub(crate) skipped_file_count: usize,
}

struct TemporaryExport {
    path: PathBuf,
    committed: bool,
}

impl Drop for TemporaryExport {
    fn drop(&mut self) {
        if !self.committed {
            let _ = fs::remove_file(&self.path);
        }
    }
}

pub(crate) struct DiagnosticLog {
    root: PathBuf,
    operation_lock: Mutex<()>,
    last_error: Mutex<Option<&'static str>>,
    cancel_export: AtomicBool,
}

impl DiagnosticLog {
    pub(crate) fn for_desktop() -> Self {
        Self::with_root(diagnostic_root())
    }

    fn with_root(root: PathBuf) -> Self {
        Self {
            root,
            operation_lock: Mutex::new(()),
            last_error: Mutex::new(None),
            cancel_export: AtomicBool::new(false),
        }
    }

    pub(crate) fn status(&self) -> Result<DiagnosticLogStatus, String> {
        let _guard = self.lock()?;
        let settings = self.ensure_initialized()?;
        let remaining_ms = settings.debug_until_ms.saturating_sub(now_ms());
        Ok(DiagnosticLogStatus {
            debug_enabled: remaining_ms > 0,
            debug_remaining_seconds: remaining_ms.div_ceil(1000),
            capacity_mb: settings.capacity_mb,
            used_bytes: self.directory_size()?,
            recent_entries: self.recent_entries()?,
            last_error: self.last_error(),
        })
    }

    pub(crate) fn query(
        &self,
        query: DiagnosticLogQuery,
    ) -> Result<DiagnosticLogQueryResult, String> {
        let _guard = self.lock()?;
        self.ensure_initialized()?;
        let mut entries = self.read_entries()?;
        if let Some(level) = query.level {
            entries.retain(|entry| entry.level == level);
        }
        if let Some(component) = query
            .component
            .as_deref()
            .map(str::trim)
            .filter(|v| !v.is_empty())
        {
            entries.retain(|entry| entry.component.eq_ignore_ascii_case(component));
        }
        if let Some(event_code) = query
            .event_code
            .as_deref()
            .map(str::trim)
            .filter(|v| !v.is_empty())
        {
            entries.retain(|entry| entry.event_code.eq_ignore_ascii_case(event_code));
        }
        if let Some(keyword) = query
            .keyword
            .as_deref()
            .map(str::trim)
            .filter(|v| !v.is_empty())
        {
            let keyword = keyword.to_ascii_lowercase();
            entries.retain(|entry| {
                entry.component.to_ascii_lowercase().contains(&keyword)
                    || entry.event_code.to_ascii_lowercase().contains(&keyword)
                    || entry
                        .error_summary
                        .as_deref()
                        .unwrap_or_default()
                        .to_ascii_lowercase()
                        .contains(&keyword)
            });
        }
        if let Some(hours) = query.since_hours {
            if !matches!(hours, 1 | 6 | 24) {
                return Err("diagnostic_log_time_range_invalid".to_owned());
            }
            let cutoff = format_rfc3339(now_ms().saturating_sub(u64::from(hours) * 3_600_000));
            entries.retain(|entry| entry.timestamp >= cutoff);
        }
        entries.sort_by_key(|entry| Reverse(entry.timestamp.clone()));
        let total = entries.len().min(MAX_QUERY_ENTRIES);
        let offset = query
            .cursor
            .as_deref()
            .unwrap_or("0")
            .parse::<usize>()
            .map_err(|_| "diagnostic_log_cursor_invalid".to_owned())?;
        if offset > total {
            return Err("diagnostic_log_cursor_invalid".to_owned());
        }
        let page_size = usize::from(query.page_size.unwrap_or(100).clamp(1, 100));
        let end = offset.saturating_add(page_size).min(total);
        let page = entries[offset..end].to_vec();
        Ok(DiagnosticLogQueryResult {
            entries: page,
            next_cursor: (end < total).then(|| end.to_string()),
            total,
        })
    }

    pub(crate) fn export_preview(&self) -> Result<DiagnosticLogExportPreview, String> {
        let _guard = self.lock()?;
        self.ensure_initialized()?;
        let files = self.log_files()?;
        let mut estimated_bytes = 0_u64;
        let mut record_count = 0_usize;
        for path in &files {
            estimated_bytes = estimated_bytes.saturating_add(
                fs::metadata(path)
                    .map(|metadata| metadata.len())
                    .unwrap_or_default(),
            );
            if let Ok(contents) = fs::read_to_string(path) {
                record_count = record_count.saturating_add(contents.lines().count());
            }
        }
        Ok(DiagnosticLogExportPreview {
            file_count: files.len(),
            estimated_bytes,
            record_count,
        })
    }

    pub(crate) fn cancel_export(&self) {
        self.cancel_export.store(true, Ordering::Release);
    }

    pub(crate) fn export<F>(
        &self,
        destination: PathBuf,
        progress: F,
    ) -> Result<DiagnosticLogExportResult, String>
    where
        F: Fn(usize, usize, usize, u64, u64) + Send + 'static,
    {
        self.cancel_export.store(false, Ordering::Release);
        let _guard = self.lock()?;
        self.ensure_initialized()?;
        let files = self.log_files()?;
        let temporary = destination.with_extension("zip.tmp");
        let mut temporary_guard = TemporaryExport {
            path: temporary.clone(),
            committed: false,
        };
        let file = File::create(&temporary)
            .map_err(|_| self.fail("diagnostic_export_destination_unwritable"))?;
        let mut archive = zip::ZipWriter::new(file);
        let options = zip::write::SimpleFileOptions::default();
        let mut invalid_record_count = 0_usize;
        let mut failed_file_count = 0_usize;
        let mut processed_files = 0_usize;
        let mut processed_records = 0_usize;
        let total_bytes = files
            .iter()
            .map(|path| {
                fs::metadata(path)
                    .map(|metadata| metadata.len())
                    .unwrap_or_default()
            })
            .sum::<u64>();
        let mut processed_bytes = 0_u64;
        for path in &files {
            if self.cancel_export.load(Ordering::Acquire) {
                drop(archive);
                return Err("diagnostic_export_cancelled".to_owned());
            }
            let Some(name) = path.file_name().and_then(|value| value.to_str()) else {
                failed_file_count = failed_file_count.saturating_add(1);
                continue;
            };
            if archive.start_file(format!("logs/{name}"), options).is_err() {
                failed_file_count = failed_file_count.saturating_add(1);
                continue;
            }
            match fs::read_to_string(path) {
                Ok(contents) => {
                    processed_bytes = processed_bytes.saturating_add(contents.len() as u64);
                    for line in contents.lines() {
                        if self.cancel_export.load(Ordering::Acquire) {
                            drop(archive);
                            return Err("diagnostic_export_cancelled".to_owned());
                        }
                        let entry = serde_json::from_str::<DiagnosticLogEntry>(line)
                            .ok()
                            .and_then(sanitize_entry);
                        let entry = match entry {
                            Some(entry) => entry,
                            None => {
                                invalid_record_count = invalid_record_count.saturating_add(1);
                                invalid_record_entry()
                            }
                        };
                        let mut encoded = serde_json::to_vec(&entry)
                            .map_err(|_| "diagnostic_export_encode_failed".to_owned())?;
                        encoded.push(b'\n');
                        archive
                            .write_all(&encoded)
                            .map_err(|_| "diagnostic_export_write_failed".to_owned())?;
                        processed_records = processed_records.saturating_add(1);
                    }
                }
                Err(_) => failed_file_count = failed_file_count.saturating_add(1),
            }
            processed_files = processed_files.saturating_add(1);
            progress(
                processed_files,
                files.len(),
                processed_records,
                processed_bytes,
                total_bytes,
            );
        }
        let status = if failed_file_count == 0 {
            "success"
        } else {
            "partial"
        };
        let manifest = serde_json::json!({
            "schema_version": LOG_SCHEMA_VERSION,
            "exported_at": now_rfc3339(),
            "status": status,
            "file_count": processed_files,
            "invalid_record_count": invalid_record_count,
            "failed_file_count": failed_file_count,
            "replaced_record_count": invalid_record_count,
            "skipped_file_count": failed_file_count,
            "processed_bytes": processed_bytes,
            "total_bytes": total_bytes,
        });
        archive
            .start_file("manifest.json", options)
            .map_err(|_| "diagnostic_export_write_failed".to_owned())?;
        archive
            .write_all(
                serde_json::to_string_pretty(&manifest)
                    .map_err(|_| "diagnostic_export_encode_failed".to_owned())?
                    .as_bytes(),
            )
            .map_err(|_| "diagnostic_export_write_failed".to_owned())?;
        archive
            .start_file("health.json", options)
            .map_err(|_| "diagnostic_export_write_failed".to_owned())?;
        let health = serde_json::json!({
            "capacity_mb": self.read_settings().capacity_mb,
            "used_bytes": self.directory_size()?,
            "log_schema_version": LOG_SCHEMA_VERSION,
        });
        archive
            .write_all(
                serde_json::to_string_pretty(&health)
                    .map_err(|_| "diagnostic_export_encode_failed".to_owned())?
                    .as_bytes(),
            )
            .map_err(|_| "diagnostic_export_write_failed".to_owned())?;
        archive
            .finish()
            .map_err(|_| "diagnostic_export_write_failed".to_owned())?;
        #[cfg(target_os = "windows")]
        if destination.exists() {
            fs::remove_file(&destination)
                .map_err(|_| self.fail("diagnostic_export_destination_unwritable"))?;
        }
        fs::rename(&temporary, &destination)
            .map_err(|_| self.fail("diagnostic_export_destination_unwritable"))?;
        temporary_guard.committed = true;
        Ok(DiagnosticLogExportResult {
            status: status.to_owned(),
            file_count: processed_files,
            invalid_record_count,
            failed_file_count,
            replaced_record_count: invalid_record_count,
            skipped_file_count: failed_file_count,
        })
    }

    pub(crate) fn update_settings(
        &self,
        capacity_mb: u16,
        enable_debug: bool,
    ) -> Result<DiagnosticLogStatus, String> {
        if !(MIN_CAPACITY_MB..=MAX_CAPACITY_MB).contains(&capacity_mb) {
            return Err("诊断日志容量必须在 5 MB 到 500 MB 之间".to_owned());
        }
        let _guard = self.lock()?;
        let settings = DiagnosticLogSettings {
            capacity_mb,
            debug_until_ms: enable_debug
                .then(|| now_ms().saturating_add(DEBUG_DURATION.as_millis() as u64))
                .unwrap_or_default(),
            schema_version: LOG_SCHEMA_VERSION,
        };
        self.write_settings(&settings)?;
        self.cleanup(&settings)?;
        drop(_guard);
        self.status()
    }

    pub(crate) fn open_directory(&self) -> Result<(), String> {
        let _guard = self.lock()?;
        self.ensure_initialized()?;
        #[cfg(target_os = "windows")]
        {
            std::process::Command::new("explorer")
                .arg(&self.root)
                .spawn()
                .map_err(|_| self.fail("无法打开诊断日志目录"))?;
        }
        #[cfg(not(target_os = "windows"))]
        {
            return Err("当前平台不支持打开诊断日志目录".to_owned());
        }
        self.clear_error();
        Ok(())
    }

    pub(crate) fn clear(&self) -> Result<DiagnosticLogStatus, String> {
        let _guard = self.lock()?;
        let settings = self.ensure_initialized()?;
        for path in self.log_files()? {
            fs::remove_file(path).map_err(|_| self.fail("无法清空诊断日志"))?;
        }
        File::create(self.active_log_path()).map_err(|_| self.fail("无法创建新的诊断日志"))?;
        self.write_settings(&settings)?;
        self.clear_error();
        drop(_guard);
        self.status()
    }

    pub(crate) fn record(
        &self,
        level: DiagnosticLevel,
        component: &'static str,
        event_code: &'static str,
        duration_ms: Option<u64>,
    ) {
        let _ = self.record_inner(level, component, event_code, duration_ms);
    }

    pub(crate) fn ingest_sidecar_line(&self, line: &[u8]) {
        let _ = self.ingest_sidecar_line_inner(line);
    }

    fn ingest_sidecar_line_inner(&self, line: &[u8]) -> Result<(), String> {
        let _guard = self.lock()?;
        let settings = self.ensure_initialized()?;
        let entry = serde_json::from_slice::<DiagnosticLogEntry>(line)
            .ok()
            .and_then(sanitize_entry)
            .unwrap_or_else(invalid_record_entry);
        self.write_entry_locked(&settings, entry)
    }

    fn record_inner(
        &self,
        level: DiagnosticLevel,
        component: &'static str,
        event_code: &'static str,
        duration_ms: Option<u64>,
    ) -> Result<(), String> {
        let _guard = self.lock()?;
        let settings = self.ensure_initialized()?;
        let entry = DiagnosticLogEntry {
            schema_version: LOG_SCHEMA_VERSION,
            timestamp: now_rfc3339(),
            level,
            source: "tauri".to_owned(),
            component: component.to_owned(),
            event_code: event_code.to_owned(),
            operation_id: None,
            duration_ms,
            path_category: None,
            path_alias: None,
            network: None,
            error_summary: None,
        };
        self.write_entry_locked(&settings, entry)
    }

    fn write_entry_locked(
        &self,
        settings: &DiagnosticLogSettings,
        entry: DiagnosticLogEntry,
    ) -> Result<(), String> {
        if !entry.level.is_enabled(settings.debug_until_ms) {
            return Ok(());
        }
        let bytes = record_bytes(entry)?;
        self.rotate_if_needed(bytes.len() as u64)?;
        let mut file = OpenOptions::new()
            .create(true)
            .append(true)
            .open(self.active_log_path())
            .map_err(|_| self.fail("无法写入诊断日志"))?;
        file.write_all(&bytes)
            .map_err(|_| self.fail("无法写入诊断日志"))?;
        self.cleanup(&settings)?;
        self.clear_error();
        Ok(())
    }

    fn lock(&self) -> Result<std::sync::MutexGuard<'_, ()>, String> {
        self.operation_lock
            .lock()
            .map_err(|_| "诊断日志状态不可用".to_owned())
    }

    fn settings_path(&self) -> PathBuf {
        self.root.join(SETTINGS_FILE)
    }

    fn active_log_path(&self) -> PathBuf {
        self.root.join(ACTIVE_LOG_FILE)
    }

    fn ensure_initialized(&self) -> Result<DiagnosticLogSettings, String> {
        fs::create_dir_all(&self.root).map_err(|_| self.fail("无法创建诊断日志目录"))?;
        let settings = self.read_settings();
        let schema_is_current = fs::read(self.settings_path())
            .ok()
            .and_then(|bytes| serde_json::from_slice::<DiagnosticLogSettings>(&bytes).ok())
            .is_some_and(|value| {
                value.schema_version == LOG_SCHEMA_VERSION
                    && (MIN_CAPACITY_MB..=MAX_CAPACITY_MB).contains(&value.capacity_mb)
            });
        if !schema_is_current {
            let mut migrated = settings;
            migrated.schema_version = LOG_SCHEMA_VERSION;
            migrated.debug_until_ms = 0;
            let mut cleanup_failed = false;
            for path in self.all_jsonl_files()? {
                if fs::remove_file(path).is_err() {
                    cleanup_failed = true;
                }
            }
            self.write_settings(&migrated)?;
            File::create(self.active_log_path()).map_err(|_| self.fail("无法创建新的诊断日志"))?;
            if cleanup_failed {
                let _ = self.fail("legacy_log_cleanup_failed");
            } else {
                self.clear_error();
            }
            return Ok(migrated);
        }
        if !self.active_log_path().exists() {
            File::create(self.active_log_path()).map_err(|_| self.fail("无法创建新的诊断日志"))?;
        }
        Ok(settings)
    }

    fn read_settings(&self) -> DiagnosticLogSettings {
        fs::read(self.settings_path())
            .ok()
            .and_then(|bytes| serde_json::from_slice(&bytes).ok())
            .filter(|settings: &DiagnosticLogSettings| {
                (MIN_CAPACITY_MB..=MAX_CAPACITY_MB).contains(&settings.capacity_mb)
            })
            .unwrap_or_default()
    }

    fn write_settings(&self, settings: &DiagnosticLogSettings) -> Result<(), String> {
        fs::create_dir_all(&self.root).map_err(|_| self.fail("无法创建诊断日志目录"))?;
        let temporary = self.settings_path().with_extension("json.tmp");
        let bytes = serde_json::to_vec(settings).map_err(|_| "诊断日志设置不可用".to_owned())?;
        fs::write(&temporary, bytes).map_err(|_| self.fail("无法保存诊断日志设置"))?;
        #[cfg(target_os = "windows")]
        if self.settings_path().exists() {
            fs::remove_file(self.settings_path()).map_err(|_| self.fail("无法保存诊断日志设置"))?;
        }
        fs::rename(temporary, self.settings_path())
            .map_err(|_| self.fail("无法保存诊断日志设置"))?;
        self.clear_error();
        Ok(())
    }

    fn rotate_if_needed(&self, incoming_bytes: u64) -> Result<(), String> {
        let active = self.active_log_path();
        let size = fs::metadata(&active)
            .map(|metadata| metadata.len())
            .unwrap_or_default();
        if size == 0 || size.saturating_add(incoming_bytes) <= FILE_LIMIT_BYTES {
            return Ok(());
        }
        let timestamp = now_ms();
        for sequence in 0..1000 {
            let candidate = self
                .root
                .join(format!("{ROLLED_LOG_PREFIX}{timestamp}-{sequence}.jsonl"));
            if !candidate.exists() {
                fs::rename(&active, candidate).map_err(|_| self.fail("无法滚动诊断日志"))?;
                return Ok(());
            }
        }
        Err(self.fail("无法滚动诊断日志"))
    }

    fn cleanup(&self, settings: &DiagnosticLogSettings) -> Result<(), String> {
        let limit = u64::from(settings.capacity_mb) * 1024 * 1024;
        let mut files = self.log_files()?;
        files.sort_by_key(|path| {
            Reverse(
                fs::metadata(path)
                    .and_then(|metadata| metadata.modified())
                    .unwrap_or(UNIX_EPOCH),
            )
        });
        let mut total = files
            .iter()
            .map(|path| {
                fs::metadata(path)
                    .map(|metadata| metadata.len())
                    .unwrap_or_default()
            })
            .sum::<u64>();
        for path in files.into_iter().rev() {
            if total <= limit {
                break;
            }
            let size = fs::metadata(&path)
                .map(|metadata| metadata.len())
                .unwrap_or_default();
            fs::remove_file(path).map_err(|_| self.fail("无法清理诊断日志"))?;
            total = total.saturating_sub(size);
        }
        Ok(())
    }

    fn recent_entries(&self) -> Result<Vec<DiagnosticLogEntry>, String> {
        let mut entries = self.read_entries()?;
        entries.sort_by_key(|entry| Reverse(entry.timestamp.clone()));
        entries.truncate(8);
        Ok(entries)
    }

    fn read_entries(&self) -> Result<Vec<DiagnosticLogEntry>, String> {
        let mut paths = self.log_files()?;
        paths.sort_by_key(|path| {
            fs::metadata(path)
                .and_then(|metadata| metadata.modified())
                .ok()
        });
        let mut entries = Vec::new();
        for path in paths {
            let contents = fs::read_to_string(path).map_err(|_| self.fail("无法读取诊断日志"))?;
            for line in contents.lines() {
                match serde_json::from_str::<DiagnosticLogEntry>(line) {
                    Ok(entry) if entry.schema_version == LOG_SCHEMA_VERSION => entries.push(entry),
                    _ => entries.push(invalid_record_entry()),
                }
            }
        }
        Ok(entries)
    }

    fn log_files(&self) -> Result<Vec<PathBuf>, String> {
        if !self.root.exists() {
            return Ok(Vec::new());
        }
        Ok(fs::read_dir(&self.root)
            .map_err(|_| self.fail("无法读取诊断日志"))?
            .filter_map(Result::ok)
            .map(|entry| entry.path())
            .filter(|path| {
                path.file_name().is_some_and(|name| {
                    name == ACTIVE_LOG_FILE
                        || (name.to_string_lossy().starts_with(ROLLED_LOG_PREFIX)
                            && path
                                .extension()
                                .is_some_and(|extension| extension == "jsonl"))
                })
            })
            .collect())
    }

    fn all_jsonl_files(&self) -> Result<Vec<PathBuf>, String> {
        if !self.root.exists() {
            return Ok(Vec::new());
        }
        Ok(fs::read_dir(&self.root)
            .map_err(|_| self.fail("无法读取诊断日志"))?
            .filter_map(Result::ok)
            .map(|entry| entry.path())
            .filter(|path| {
                path.extension()
                    .is_some_and(|extension| extension == "jsonl")
            })
            .collect())
    }

    fn directory_size(&self) -> Result<u64, String> {
        Ok(self
            .log_files()?
            .iter()
            .map(|path| {
                fs::metadata(path)
                    .map(|metadata| metadata.len())
                    .unwrap_or_default()
            })
            .sum())
    }

    fn fail(&self, message: &'static str) -> String {
        if let Ok(mut last_error) = self.last_error.lock() {
            *last_error = Some(message);
        }
        message.to_owned()
    }

    fn clear_error(&self) {
        if let Ok(mut last_error) = self.last_error.lock() {
            *last_error = None;
        }
    }

    fn last_error(&self) -> Option<&'static str> {
        self.last_error.lock().ok().and_then(|error| *error)
    }
}

fn diagnostic_root() -> PathBuf {
    #[cfg(target_os = "windows")]
    if let Some(local_app_data) = std::env::var_os("LOCALAPPDATA") {
        return PathBuf::from(local_app_data)
            .join("Codex Session Monitor")
            .join("logs");
    }
    PathBuf::from("Codex Session Monitor").join("logs")
}

fn now_ms() -> u64 {
    SystemTime::now()
        .duration_since(UNIX_EPOCH)
        .unwrap_or_default()
        .as_millis() as u64
}

fn now_rfc3339() -> String {
    format_rfc3339(now_ms())
}

fn format_rfc3339(milliseconds: u64) -> String {
    let seconds = milliseconds / 1000;
    let millis = milliseconds % 1000;
    let days = seconds / 86_400;
    let day_seconds = seconds % 86_400;
    let (year, month, day) = civil_from_days(days as i64);
    let hour = day_seconds / 3_600;
    let minute = (day_seconds % 3_600) / 60;
    let second = day_seconds % 60;
    format!("{year:04}-{month:02}-{day:02}T{hour:02}:{minute:02}:{second:02}.{millis:03}Z")
}

fn civil_from_days(days: i64) -> (i64, i64, i64) {
    let shifted = days + 719_468;
    let era = if shifted >= 0 {
        shifted / 146_097
    } else {
        (shifted - 146_096) / 146_097
    };
    let day_of_era = shifted - era * 146_097;
    let year_of_era =
        (day_of_era - day_of_era / 1_460 + day_of_era / 36_524 - day_of_era / 146_096) / 365;
    let year = year_of_era + era * 400;
    let day_of_year = day_of_era - (365 * year_of_era + year_of_era / 4 - year_of_era / 100);
    let month_part = (5 * day_of_year + 2) / 153;
    let day = day_of_year - (153 * month_part + 2) / 5 + 1;
    let month = month_part + if month_part < 10 { 3 } else { -9 };
    (year + i64::from(month <= 2), month, day)
}

fn safe_token(value: &str, max_len: usize) -> bool {
    let bytes = value.as_bytes();
    !bytes.is_empty()
        && bytes.len() <= max_len
        && bytes[0].is_ascii_lowercase()
        && bytes[1..]
            .iter()
            .all(|byte| byte.is_ascii_lowercase() || byte.is_ascii_digit() || b"_.-".contains(byte))
}

fn safe_operation_id(value: &str) -> bool {
    value.len() >= 4
        && value.starts_with("op-")
        && value[3..].bytes().all(|byte| byte.is_ascii_digit())
}

fn safe_path_alias(value: &str) -> bool {
    let Some((category, suffix)) = value.split_once(':') else {
        return false;
    };
    matches!(category, "project" | "session" | "path")
        && suffix.rsplit_once('-').is_some_and(|(_, number)| {
            !number.is_empty() && number.bytes().all(|byte| byte.is_ascii_digit())
        })
        && safe_token(suffix.split('-').next().unwrap_or_default(), 32)
}

fn valid_timestamp(value: &str) -> bool {
    let bytes = value.as_bytes();
    bytes.len() == 24
        && bytes[4] == b'-'
        && bytes[7] == b'-'
        && bytes[10] == b'T'
        && bytes[13] == b':'
        && bytes[16] == b':'
        && bytes[19] == b'.'
        && bytes[23] == b'Z'
        && bytes.iter().enumerate().all(|(index, byte)| {
            matches!(index, 4 | 7 | 10 | 13 | 16 | 19 | 23) || byte.is_ascii_digit()
        })
}

fn sanitize_entry(mut entry: DiagnosticLogEntry) -> Option<DiagnosticLogEntry> {
    if entry.schema_version != LOG_SCHEMA_VERSION
        || !valid_timestamp(&entry.timestamp)
        || !matches!(entry.source.as_str(), "tauri" | "sidecar")
        || !safe_token(&entry.component, 64)
        || !safe_token(&entry.event_code, 64)
    {
        return None;
    }
    if let Some(operation_id) = &entry.operation_id {
        if !safe_operation_id(operation_id) {
            entry.operation_id = None;
            entry.error_summary = Some("redaction_failed".to_owned());
        }
    }
    if let Some(path_alias) = &entry.path_alias {
        if !safe_path_alias(path_alias) {
            entry.path_alias = None;
            entry.error_summary = Some("redaction_failed".to_owned());
        }
    }
    if let Some(summary) = &entry.error_summary {
        if !safe_token(summary, 128) {
            entry.error_summary = Some("redaction_failed".to_owned());
        }
    }
    if let Some(network) = &entry.network {
        let network_valid = safe_token(&network.protocol, 32)
            && safe_token(&network.scope, 32)
            && network
                .http_status
                .is_none_or(|status| (100..=599).contains(&status));
        if !network_valid {
            entry.network = None;
            entry.error_summary = Some("redaction_failed".to_owned());
        }
    }
    Some(entry)
}

fn invalid_record_entry() -> DiagnosticLogEntry {
    DiagnosticLogEntry {
        schema_version: LOG_SCHEMA_VERSION,
        timestamp: now_rfc3339(),
        level: DiagnosticLevel::Warn,
        source: "tauri".to_owned(),
        component: "storage".to_owned(),
        event_code: "invalid_log_record".to_owned(),
        operation_id: None,
        duration_ms: None,
        path_category: None,
        path_alias: None,
        network: None,
        error_summary: Some("invalid_log_record".to_owned()),
    }
}

fn record_bytes(mut entry: DiagnosticLogEntry) -> Result<Vec<u8>, String> {
    if entry.schema_version != LOG_SCHEMA_VERSION {
        return Err("diagnostic_log_schema_unsupported".to_owned());
    }
    let mut bytes = serde_json::to_vec(&entry).map_err(|_| "诊断日志记录不可用".to_owned())?;
    if bytes.len().saturating_add(1) > RECORD_LIMIT_BYTES {
        entry.component.truncate(64);
        entry.event_code.truncate(128);
        entry.error_summary = Some("diagnostic_record_truncated".to_owned());
        bytes = serde_json::to_vec(&entry).map_err(|_| "诊断日志记录不可用".to_owned())?;
    }
    if bytes.len().saturating_add(1) > RECORD_LIMIT_BYTES {
        return Err("诊断日志记录超过允许长度".to_owned());
    }
    serde_json::from_slice::<DiagnosticLogEntry>(&bytes)
        .map_err(|_| "diagnostic_log_schema_invalid".to_owned())?;
    bytes.push(b'\n');
    Ok(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::atomic::{AtomicU64, Ordering};

    static TEST_ROOT_COUNTER: AtomicU64 = AtomicU64::new(0);

    fn temporary_root() -> PathBuf {
        let sequence = TEST_ROOT_COUNTER.fetch_add(1, Ordering::Relaxed);
        std::env::temp_dir().join(format!(
            "codex-monitor-diagnostic-log-{}-{sequence}",
            now_ms()
        ))
    }

    #[test]
    fn defaults_to_warn_and_error_with_no_debug_window() {
        let log = DiagnosticLog::with_root(temporary_root());

        let status = log.status().expect("status is available");

        assert!(!status.debug_enabled);
        assert_eq!(status.capacity_mb, DEFAULT_CAPACITY_MB);
        assert!(status.recent_entries.is_empty());
    }

    #[test]
    fn debug_window_and_capacity_are_persisted() {
        let root = temporary_root();
        let log = DiagnosticLog::with_root(root.clone());

        let status = log.update_settings(5, true).expect("settings are valid");

        assert!(status.debug_enabled);
        assert_eq!(status.capacity_mb, 5);
        assert!(status.debug_remaining_seconds <= 30 * 60);
        assert!(status.debug_remaining_seconds > 0);
        assert_eq!(
            DiagnosticLog::with_root(root)
                .status()
                .expect("reloaded status")
                .capacity_mb,
            5
        );
    }

    #[test]
    fn logs_only_safe_structured_fields() {
        let root = temporary_root();
        let log = DiagnosticLog::with_root(root.clone());

        log.record(
            DiagnosticLevel::Warn,
            "sidecar",
            "connection_failed",
            Some(12),
        );

        let serialized = fs::read_to_string(root.join(ACTIVE_LOG_FILE)).expect("log file exists");
        assert!(serialized.contains("connection_failed"));
        assert!(!serialized.contains("session_key"));
        assert!(!serialized.contains("project_root"));
    }

    #[test]
    fn first_startup_removes_legacy_jsonl_and_resets_debug() {
        let root = temporary_root();
        fs::create_dir_all(&root).expect("temporary directory exists");
        fs::write(
            root.join(SETTINGS_FILE),
            serde_json::json!({
                "capacityMb": 5,
                "debugUntilMs": now_ms().saturating_add(60_000)
            })
            .to_string(),
        )
        .expect("legacy settings exist");
        fs::write(root.join("diagnostics-current.jsonl"), "legacy\n").expect("legacy log exists");

        let status = DiagnosticLog::with_root(root.clone())
            .status()
            .expect("status initializes the v2 store");

        assert_eq!(status.capacity_mb, 5);
        assert!(!status.debug_enabled);
        assert!(!root.join("diagnostics-current.jsonl").exists());
        assert!(root.join(ACTIVE_LOG_FILE).exists());
    }

    #[test]
    fn invalid_jsonl_is_exposed_as_safe_placeholder() {
        let root = temporary_root();
        let log = DiagnosticLog::with_root(root.clone());
        log.status().expect("status initializes the v2 store");
        fs::write(log.active_log_path(), "not-json\n").expect("invalid line exists");

        let entries = log
            .status()
            .expect("status reads invalid lines")
            .recent_entries;

        assert_eq!(entries.len(), 1);
        assert_eq!(entries[0].event_code, "invalid_log_record");
        assert!(!serde_json::to_string(&entries[0])
            .expect("placeholder serializes")
            .contains("not-json"));
    }

    #[test]
    fn sidecar_ingest_redacts_invalid_aliases_before_storage() {
        let root = temporary_root();
        let log = DiagnosticLog::with_root(root.clone());
        log.status().expect("status initializes the v2 store");
        let line = serde_json::json!({
            "schema_version": 2,
            "timestamp": "2026-07-21T00:00:00.000Z",
            "level": "warn",
            "source": "sidecar",
            "component": "api",
            "event_code": "request_failed",
            "operation_id": "op-7",
            "path_alias": "C:\\fixtures\\secret"
        });
        log.ingest_sidecar_line(line.to_string().as_bytes());

        let serialized = fs::read_to_string(root.join(ACTIVE_LOG_FILE)).expect("log exists");

        assert!(serialized.contains("op-7"));
        assert!(serialized.contains("redaction_failed"));
        assert!(!serialized.contains("C:\\\\Users"));
        assert!(!serialized.contains("secret"));
    }

    #[test]
    fn query_filters_and_paginates_newest_entries() {
        let root = temporary_root();
        let log = DiagnosticLog::with_root(root);
        log.update_settings(5, true).expect("debug is enabled");
        log.record(DiagnosticLevel::Warn, "api", "request_failed", Some(4));
        log.record(DiagnosticLevel::Error, "sidecar", "server_failed", Some(8));

        let result = log
            .query(DiagnosticLogQuery {
                level: Some(DiagnosticLevel::Error),
                component: None,
                event_code: None,
                keyword: None,
                since_hours: Some(24),
                cursor: None,
                page_size: Some(1),
            })
            .expect("query succeeds");

        assert_eq!(result.total, 1);
        assert_eq!(result.entries.len(), 1);
        assert_eq!(result.entries[0].event_code, "server_failed");
        assert!(result.next_cursor.is_none());
    }

    #[test]
    fn query_caps_cumulative_results_at_one_thousand_entries() {
        let root = temporary_root();
        let log = DiagnosticLog::with_root(root);
        log.update_settings(5, true).expect("debug is enabled");
        for _ in 0..=MAX_QUERY_ENTRIES {
            log.record(DiagnosticLevel::Warn, "api", "request_failed", None);
        }

        let result = log
            .query(DiagnosticLogQuery {
                level: None,
                component: None,
                event_code: None,
                keyword: None,
                since_hours: None,
                cursor: None,
                page_size: Some(100),
            })
            .expect("query succeeds");

        assert_eq!(result.total, MAX_QUERY_ENTRIES);
        assert_eq!(result.entries.len(), 100);
        assert_eq!(result.next_cursor.as_deref(), Some("100"));
        assert!(log
            .query(DiagnosticLogQuery {
                level: None,
                component: None,
                event_code: None,
                keyword: None,
                since_hours: None,
                cursor: Some(MAX_QUERY_ENTRIES.to_string()),
                page_size: Some(100),
            })
            .expect("last page query succeeds")
            .entries
            .is_empty());
    }

    #[test]
    fn export_writes_redacted_logs_manifest_and_health_snapshot() {
        let root = temporary_root();
        let destination = root.with_extension("zip");
        let log = DiagnosticLog::with_root(root.clone());
        log.status().expect("status initializes the v2 store");
        log.record(DiagnosticLevel::Warn, "sidecar", "server_failed", Some(9));

        let result = log
            .export(destination.clone(), |_, _, _, _, _| {})
            .expect("export succeeds");

        assert_eq!(result.status, "success");
        assert!(destination.exists());
        let archive_file = File::open(destination).expect("zip exists");
        let mut archive = zip::ZipArchive::new(archive_file).expect("zip is readable");
        assert!(archive.by_name("manifest.json").is_ok());
        assert!(archive.by_name("health.json").is_ok());
        assert!(archive.by_name(ACTIVE_LOG_FILE).is_err());
        assert!(archive.by_name(&format!("logs/{ACTIVE_LOG_FILE}")).is_ok());
    }

    #[test]
    fn oversized_records_keep_structure_and_add_a_truncation_marker() {
        let bytes = record_bytes(DiagnosticLogEntry {
            schema_version: LOG_SCHEMA_VERSION,
            timestamp: now_rfc3339(),
            level: DiagnosticLevel::Error,
            source: "tauri".to_owned(),
            component: "component".repeat(1000),
            event_code: "event".repeat(1000),
            operation_id: None,
            duration_ms: None,
            path_category: None,
            path_alias: None,
            network: None,
            error_summary: None,
        })
        .expect("oversized records are safely truncated");

        assert!(bytes.len() <= RECORD_LIMIT_BYTES);
        assert!(String::from_utf8(bytes)
            .expect("utf-8 jsonl")
            .contains("diagnostic_record_truncated"));
    }
}
