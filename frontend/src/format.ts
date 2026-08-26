import type { ActivityState, SessionSummary, SessionStatus, ToolStatus } from "./types";

export const statusText: Record<SessionStatus, string> = {
  running: "运行中",
  stuck: "长时间没有新进展",
  idle: "已完成",
  unknown: "状态未知",
};

export const toolStatusText: Record<ToolStatus, string> = {
  pending: "执行中",
  success: "成功",
  error: "错误",
};

export const activityStateText: Record<ActivityState, string> = {
  active: "正在处理",
  quiet: "暂时没有新事件",
  tool_running: "工具执行中",
  long_running_tool: "工具执行时间较长，建议查看",
  no_progress: "长时间没有新进展，建议查看",
  data_stale: "状态待确认，等待实时连接恢复",
};

export function needsAttention(session: SessionSummary): boolean {
  return session.attention_reasons.some((reason) => reason !== "stuck");
}

export function attentionStatus(session: SessionSummary): string {
  if (session.activity_state === "data_stale") return "状态待确认";
  if (session.attention_reasons.includes("tool_error")) return "错误";
  if (session.attention_reasons.includes("turn_aborted")) return "已中止";
  if (session.activity_state === "long_running_tool" || session.activity_state === "no_progress") return "建议查看";
  if (session.status === "stuck") return "状态待确认";
  return statusText[session.status];
}

export function relativeTime(timestamp: number, now = Date.now() / 1000): string {
  const seconds = Math.max(0, Math.floor(now - timestamp));
  if (seconds < 10) return "刚刚";
  if (seconds < 60) return `${seconds} 秒前`;
  const minutes = Math.floor(seconds / 60);
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  const days = Math.floor(hours / 24);
  if (days < 7) return `${days} 天前`;
  if (days < 30) return `${Math.floor(days / 7)} 周前`;
  return absoluteTime(timestamp);
}

export function absoluteTime(timestamp: number): string {
  return new Intl.DateTimeFormat("zh-CN", {
    year: "numeric",
    month: "numeric",
    day: "numeric",
  }).format(new Date(timestamp * 1000));
}

export function duration(seconds: number): string {
  if (seconds < 60) return `${Math.floor(seconds)} 秒`;
  const minutes = Math.floor(seconds / 60);
  return `${minutes} 分 ${Math.floor(seconds % 60)} 秒`;
}

export function contextSize(chars: number): string {
  return chars >= 1000 ? `${(chars / 1000).toFixed(1)}k 字符` : `${chars} 字符`;
}

export function pathTail(path: string): string {
  const parts = path.split(/[\\/]/).filter(Boolean);
  return parts.slice(-2).join("/") || path;
}
