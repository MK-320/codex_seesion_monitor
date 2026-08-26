export type SessionStatus = "running" | "stuck" | "idle" | "unknown";
export type ActivityState = "active" | "quiet" | "tool_running" | "long_running_tool" | "no_progress" | "data_stale";
export type ToolStatus = "pending" | "success" | "error";
export type AttentionReason = "stuck" | "long_running_tool" | "no_progress" | "tool_error" | "turn_aborted";

export interface AttentionContext {
  reason: AttentionReason;
  event_key: string;
  occurred_at: number;
  turn_id: string | null;
  tool_call_index: number | null;
  tool_name: string | null;
}

export function isAttentionContext(value: unknown): value is AttentionContext {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const context = value as Record<string, unknown>;
  const reason = context.reason;
  const turnId = context.turn_id;
  const toolCallIndex = context.tool_call_index;
  const toolName = context.tool_name;
  if (
    (reason !== "stuck" && reason !== "long_running_tool" && reason !== "no_progress" && reason !== "tool_error" && reason !== "turn_aborted") ||
    typeof context.event_key !== "string" ||
    !/^[0-9a-f]{64}$/.test(context.event_key) ||
    typeof context.occurred_at !== "number" ||
    !Number.isFinite(context.occurred_at) ||
    context.occurred_at < 0 ||
    (turnId !== null && typeof turnId !== "string") ||
    (toolCallIndex !== null &&
      (typeof toolCallIndex !== "number" || !Number.isInteger(toolCallIndex) || toolCallIndex < 0)) ||
    (toolName !== null && typeof toolName !== "string")
  ) {
    return false;
  }
  if (reason === "tool_error") {
    return typeof turnId === "string" && typeof toolCallIndex === "number" && typeof toolName === "string";
  }
  if (reason === "long_running_tool") {
    return typeof turnId === "string" && typeof toolCallIndex === "number" && typeof toolName === "string";
  }
  return toolCallIndex === null && toolName === null && (reason === "stuck" || reason === "no_progress" || typeof turnId === "string");
}

export interface ToolCall {
  name: string;
  input_summary: string;
  status: ToolStatus;
  result_summary: string;
  started_at: number;
  ended_at: number | null;
  duration_seconds: number | null;
}

export interface Turn {
  turn_id: string;
  started_at: number;
  ended_at: number | null;
  duration_seconds: number;
  user_message: string;
  tool_calls: ToolCall[];
  agent_text_snippets: string[];
  aborted: boolean;
  end_reason: string;
}

export interface SessionSummary {
  session_key: string;
  session_id: string;
  project_key: string;
  project_root: string;
  cwd: string;
  originator: string;
  source: string;
  cli_version: string;
  model_provider: string;
  status: SessionStatus;
  activity_state: ActivityState;
  last_event_at: number;
  last_progress_at: number;
  activity_since: number;
  pending_tool_name: string | null;
  pending_tool_started_at: number | null;
  current_turn_id: string | null;
  current_turn_started_at: number | null;
  current_user_message_summary: string;
  current_action: string;
  turn_count: number;
  approx_context_chars: number;
  attention_reasons: AttentionReason[];
  attention_context?: AttentionContext | null;
  parse_diagnostics: {
    unknown_event_count: number;
    malformed_line_count: number;
    oversized_line_count: number;
  };
}

export interface SessionDetail extends SessionSummary {
  turns: Turn[];
  has_earlier: boolean;
  next_before: number | null;
}

export interface MonitorConfig {
  activity_alert_seconds: number | null;
  layout: LayoutPreferences | null;
  projects: Project[];
}

export interface LayoutPreferences {
  project_sidebar_ratio: number;
  session_sidebar_ratio: number;
}

export interface DiagnosticsReport {
  schema_version: number;
  generated_at: number;
  application: { name: string; version: string; protocol_version: number };
  runtime: { python: string; system: string; release: string; machine: string };
  dependencies: Record<string, string>;
  health: {
    uptime_seconds: number;
    listen_host: string;
    project_count: number;
    session_count: number;
    unknown_event_count: number;
    malformed_line_count: number;
    oversized_line_count: number;
    known_file_count: number;
    last_reconciliation_at: number | null;
    reconciliation_count: number;
    coalesced_event_count: number;
    status_counts: Record<string, number>;
    websocket_clients: number;
  };
  configuration: { schema_version: number; stuck_seconds: number };
  privacy: { excluded: string[] };
}

export interface Project {
  project_key: string;
  project_root: string;
  project_name: string;
  status: "active" | "missing" | "error";
  persisted: boolean;
}

export type RealtimeEvent =
  | { event: "snapshot"; version: number; protocol_version: 1; generated_at: number; data: SessionSummary[] }
  | {
      event: "session_created" | "session_updated";
      version: number;
      protocol_version: 1;
      generated_at: number;
      session_key: string;
      data: SessionSummary;
    };
