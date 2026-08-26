import { invoke } from "@tauri-apps/api/core";
import { listen } from "@tauri-apps/api/event";
import { downloadDir, join } from "@tauri-apps/api/path";
import type { UnlistenFn } from "@tauri-apps/api/event";
import { getCurrentWindow } from "@tauri-apps/api/window";
import { open, save } from "@tauri-apps/plugin-dialog";

interface DesktopConnection {
  port: number;
  token: string;
  protocolVersion: number;
}

export interface AvailableUpdate {
  version: string;
  notes: string | null;
}

export interface RecoveryStatus {
  failureCount: number;
  available: boolean;
  recoveryUrl: string | null;
}

export interface UpdateDownloadProgress {
  downloaded: number;
  total: number | null;
}

export interface DiagnosticLogEntry {
  schema_version: number;
  timestamp: string;
  level: "DEBUG" | "WARN" | "ERROR";
  source: "tauri" | "sidecar";
  component: string;
  event_code: string;
  operation_id?: string;
  duration_ms: number | null;
  path_category?: string;
  path_alias?: string;
  network?: {
    protocol: string;
    scope: string;
    http_status?: number;
    close_code?: number;
    system_error_code?: number;
  };
  error_summary: string | null;
}

export interface DiagnosticLogStatus {
  debugEnabled: boolean;
  debugRemainingSeconds: number;
  capacityMb: number;
  usedBytes: number;
  recentEntries: DiagnosticLogEntry[];
  lastError: string | null;
}

export interface DiagnosticLogQuery {
  level?: "debug" | "warn" | "error";
  component?: string;
  eventCode?: string;
  keyword?: string;
  sinceHours?: 1 | 6 | 24;
  cursor?: string;
  pageSize?: number;
}

export interface DiagnosticLogQueryResult {
  entries: DiagnosticLogEntry[];
  nextCursor: string | null;
  total: number;
}

export interface DiagnosticLogExportPreview {
  fileCount: number;
  estimatedBytes: number;
  recordCount: number;
}

export interface DiagnosticLogExportResult {
  status: "success" | "partial";
  fileCount: number;
  invalidRecordCount: number;
  failedFileCount: number;
  replacedRecordCount: number;
  skippedFileCount: number;
}

export interface DiagnosticExportProgress {
  processedFiles: number;
  totalFiles: number;
  processedRecords: number;
  processedBytes: number;
  totalBytes: number;
}

export interface AttentionActivation {
  eventKey: string | null;
}

export class DesktopStartupError extends Error {
  constructor(message: string, options?: ErrorOptions) {
    super(message, options);
    this.name = "DesktopStartupError";
  }
}

export const isDesktopRuntime = "__TAURI_INTERNALS__" in window;
let connection: Promise<DesktopConnection> | null = null;

function desktopConnection(): Promise<DesktopConnection | null> {
  if (!isDesktopRuntime) return Promise.resolve(null);
  connection ??= invoke<DesktopConnection>("desktop_connection")
    .then((value) => {
      if (
        value.protocolVersion !== 1
        || !Number.isInteger(value.port)
        || value.port < 1
        || value.port > 65535
        || !value.token
      ) {
        throw new Error("Desktop service returned invalid connection details");
      }
      return value;
    })
    .catch((reason: unknown) => {
      throw new DesktopStartupError("Desktop service failed to start", { cause: reason });
    });
  return connection;
}

export async function pickProjectDirectories(): Promise<string[]> {
  if (!isDesktopRuntime) return [];
  const selected = await open({ directory: true, multiple: true, title: "Select project folders" });
  if (typeof selected === "string") return [selected];
  return selected ?? [];
}

export async function pickProjectDirectory(): Promise<string | null> {
  const selected = await pickProjectDirectories();
  return selected[0] ?? null;
}

export function checkForUpdate(): Promise<AvailableUpdate | null> {
  if (!isDesktopRuntime) return Promise.resolve(null);
  return invoke("check_for_update") as Promise<AvailableUpdate | null>;
}

export function installVerifiedUpdate(): Promise<AvailableUpdate | null> {
  if (!isDesktopRuntime) return Promise.resolve(null);
  return invoke("install_verified_update") as Promise<AvailableUpdate | null>;
}

export function listenUpdateProgress(
  handler: (progress: UpdateDownloadProgress) => void,
): Promise<UnlistenFn> {
  if (!isDesktopRuntime) return Promise.resolve(() => undefined);
  return listen<UpdateDownloadProgress>("update-download-progress", (event) => handler(event.payload));
}

export function markDesktopHealthy(): Promise<void> {
  if (!isDesktopRuntime) return Promise.resolve();
  return invoke("mark_desktop_healthy") as Promise<void>;
}

export function recoveryStatus(): Promise<RecoveryStatus> {
  if (!isDesktopRuntime) {
    return Promise.resolve({ failureCount: 0, available: false, recoveryUrl: null });
  }
  return invoke("recovery_status") as Promise<RecoveryStatus>;
}

export function diagnosticLogStatus(): Promise<DiagnosticLogStatus> {
  if (!isDesktopRuntime) return Promise.reject(new Error("Desktop runtime is unavailable"));
  return invoke("diagnostic_log_status") as Promise<DiagnosticLogStatus>;
}

export function queryDiagnosticLogs(query: DiagnosticLogQuery = {}): Promise<DiagnosticLogQueryResult> {
  if (!isDesktopRuntime) return Promise.reject(new Error("Desktop runtime is unavailable"));
  return invoke("query_diagnostic_logs", { query }) as Promise<DiagnosticLogQueryResult>;
}

export function listenDiagnosticLogAppended(handler: () => void): Promise<UnlistenFn> {
  if (!isDesktopRuntime) return Promise.resolve(() => undefined);
  return listen("diagnostic-log-appended", () => handler());
}

export function diagnosticLogExportPreview(): Promise<DiagnosticLogExportPreview> {
  if (!isDesktopRuntime) return Promise.reject(new Error("Desktop runtime is unavailable"));
  return invoke("diagnostic_log_export_preview") as Promise<DiagnosticLogExportPreview>;
}

export async function chooseDiagnosticLogExportPath(): Promise<string | null> {
  if (!isDesktopRuntime) return null;
  const defaultPath = await join(await downloadDir(), "codex-session-monitor-diagnostics.zip");
  const selected = await save({
    defaultPath,
    filters: [{ name: "ZIP archive", extensions: ["zip"] }],
  });
  return typeof selected === "string" ? selected : null;
}

export function exportDiagnosticLogs(destination: string): Promise<DiagnosticLogExportResult> {
  if (!isDesktopRuntime) return Promise.reject(new Error("Desktop runtime is unavailable"));
  return invoke("export_diagnostic_logs", { destination }) as Promise<DiagnosticLogExportResult>;
}

export function cancelDiagnosticLogExport(): Promise<void> {
  if (!isDesktopRuntime) return Promise.resolve();
  return invoke("cancel_diagnostic_log_export") as Promise<void>;
}

export function listenDiagnosticExportProgress(
  handler: (progress: DiagnosticExportProgress) => void,
): Promise<UnlistenFn> {
  if (!isDesktopRuntime) return Promise.resolve(() => undefined);
  return listen<DiagnosticExportProgress>("diagnostic-export-progress", (event) => handler(event.payload));
}

export function updateDiagnosticLogSettings(
  capacityMb: number,
  enableDebug: boolean,
): Promise<DiagnosticLogStatus> {
  if (!isDesktopRuntime) return Promise.reject(new Error("Desktop runtime is unavailable"));
  return invoke("update_diagnostic_log_settings", { capacityMb, enableDebug }) as Promise<DiagnosticLogStatus>;
}

export function openDiagnosticLogDirectory(): Promise<void> {
  if (!isDesktopRuntime) return Promise.reject(new Error("Desktop runtime is unavailable"));
  return invoke("open_diagnostic_log_directory") as Promise<void>;
}

export function openProjectDirectory(path: string): Promise<void> {
  if (!isDesktopRuntime) return Promise.reject(new Error("Desktop runtime is unavailable"));
  return invoke("open_project_directory", { path }) as Promise<void>;
}

export function clearDiagnosticLogs(): Promise<DiagnosticLogStatus> {
  if (!isDesktopRuntime) return Promise.reject(new Error("Desktop runtime is unavailable"));
  return invoke("clear_diagnostic_logs") as Promise<DiagnosticLogStatus>;
}

export function sendAttentionNotification(count: number, eventKey: string | null, kind: "no_progress" | "long_running_tool" | "mixed"): Promise<void> {
  if (!isDesktopRuntime) return Promise.reject(new Error("Desktop runtime is unavailable"));
  return invoke("send_attention_notification", { count, eventKey, kind }) as Promise<void>;
}

export function isDesktopWindowFocused(): Promise<boolean> {
  if (!isDesktopRuntime) return Promise.resolve(document.hasFocus());
  return getCurrentWindow().isFocused();
}

export function listenAttentionInbox(handler: (activation: AttentionActivation) => void): Promise<UnlistenFn> {
  if (!isDesktopRuntime) return Promise.resolve(() => undefined);
  return listen<AttentionActivation>("open-attention-inbox", (event) => handler(event.payload));
}

export async function apiFetch(path: string, init?: RequestInit): Promise<Response> {
  const desktop = await desktopConnection();
  if (desktop === null) return fetch(path, init);

  const headers = new Headers(init?.headers);
  headers.set("Authorization", `Bearer ${desktop.token}`);
  return fetch(`http://127.0.0.1:${desktop.port}${path}`, { ...init, headers });
}

export async function monitorWebSocket(): Promise<WebSocket> {
  const desktop = await desktopConnection();
  if (desktop !== null) {
    return new WebSocket(
      `ws://127.0.0.1:${desktop.port}/ws`,
      `codex-monitor-v1.${desktop.token}`,
    );
  }

  const protocol = location.protocol === "https:" ? "wss" : "ws";
  return new WebSocket(`${protocol}://${location.host}/ws`);
}
