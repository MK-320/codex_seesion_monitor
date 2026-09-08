import {
  ArrowLeft,
  ArrowDown,
  ArrowUp,
  CaretLeft,
  CaretRight,
  ChatCircle,
  CheckCircle,
  Clock,
  Code,
  Copy,
  FolderPlus,
  FolderSimple,
  Gear,
  GithubLogo,
  EnvelopeSimple,
  Moon,
  Bell,
  BellSlash,
  ChartLineUp,
  Info,
  ListBullets,
  Pulse,
  SidebarSimple,
  SpinnerGap,
  Sun,
  TerminalWindow,
  Trash,
  User,
  Warning,
  X,
} from "@phosphor-icons/react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { openUrl } from "@tauri-apps/plugin-opener";
import frontendPackage from "../package.json";

import {
  activityStateText,
  attentionStatus,
  absoluteTime,
  contextSize,
  duration,
  needsAttention,
  relativeTime,
  toolStatusText,
} from "./format";
import {
  apiFetch,
  cancelDiagnosticLogExport,
  checkForUpdate,
  chooseDiagnosticLogExportPath,
  clearDiagnosticLogs,
  diagnosticLogStatus,
  diagnosticLogExportPreview,
  exportDiagnosticLogs,
  installVerifiedUpdate,
  isDesktopRuntime,
  listenDiagnosticLogAppended,
  listenDiagnosticExportProgress,
  listenAttentionInbox,
  listenUpdateProgress,
  markDesktopHealthy,
  openDiagnosticLogDirectory,
  openProjectDirectory,
  queryDiagnosticLogs,
  recoveryStatus,
  updateDiagnosticLogSettings,
} from "./desktop";
import type {
  AvailableUpdate,
  DiagnosticExportProgress,
  DiagnosticLogEntry,
  DiagnosticLogQuery,
  DiagnosticLogStatus,
  RecoveryStatus,
  UpdateDownloadProgress,
} from "./desktop";
import type { AttentionContext, DiagnosticsReport, MonitorConfig, Project, SessionDetail, SessionSummary, ToolCall, TraceEvent, TracePage, TraceStorageStatus, Turn } from "./types";
import { isAttentionContext, MAX_ATTENTION_REGISTRY_ENTRIES, useAttentionRegistry } from "./useAttentionRegistry";
import { ACTIVITY_BASELINE_RESET_EVENT, useAttentionNotifications } from "./useAttentionNotifications";
import { useMonitorConfig, useSessionDetail, useSessions } from "./useSessions";

const AUTO_UPDATE_CHECK_DELAY_MS = 1500;
const PROJECT_SIDEBAR_MIN_WIDTH_REM = 11.25;
const PROJECT_SIDEBAR_MAX_WIDTH_REM = 22.5;
const PROJECT_SIDEBAR_DEFAULT_WIDTH_REM = 16.25;
const SESSION_SIDEBAR_MIN_WIDTH_REM = 13.75;
const SESSION_SIDEBAR_MAX_WIDTH_REM = 26.25;
const SESSION_SIDEBAR_DEFAULT_WIDTH_REM = 16.25;
const SESSION_ABSOLUTE_TIME_MIN_WIDTH_REM = 21.25;
const COMPACT_LAYOUT_MAX_WIDTH_REM = 45;
const LAST_DISMISSED_UPDATE_KEY = "last-dismissed-update-version";
const DIALOG_EXIT_DURATION_MS = 180;
const AUTHOR_EMAIL = "drgeek320@163.com";
const REPOSITORY_URL = "https://github.com/MK-320/codex_multiple_thread_monitor";

function rootFontSize(): number {
  return Number.parseFloat(getComputedStyle(document.documentElement).fontSize) || 16;
}

function useReducedMotion(): boolean {
  const [reduced, setReduced] = useState(false);

  useEffect(() => {
    const query = window.matchMedia("(prefers-reduced-motion: reduce)");
    const update = () => setReduced(query.matches);
    update();
    query.addEventListener("change", update);
    return () => query.removeEventListener("change", update);
  }, []);

  return reduced;
}
import type { AnchorState, DetailState } from "./useSessions";

type Filter = "all" | "attention" | "handled" | "running" | "idle";
type Sort = "attention" | "recent" | "oldest";
type TimeRange = "all" | "hour" | "day" | "week";
type Theme = "light" | "dark";
const EVENT_KEY_PATTERN = /^[0-9a-f]{64}$/;

interface DialogOrigin {
  x: number;
  y: number;
  width: number;
  height: number;
}

const initialParams = new URLSearchParams(window.location.search);

function initialChoice<T extends string>(key: string, values: readonly T[], fallback: T): T {
  const value = initialParams.get(key);
  return value !== null && values.includes(value as T) ? value as T : fallback;
}

const filters: Array<{ value: Filter; label: string }> = [
  { value: "all", label: "全部" },
  { value: "attention", label: "未处理" },
  { value: "handled", label: "已处理" },
  { value: "running", label: "运行中" },
  { value: "idle", label: "已完成" },
];

function currentAttention(session: SessionSummary): AttentionContext | null {
  return isAttentionContext(session.attention_context) ? session.attention_context : null;
}

function displayVersion(version: string): string {
  return version.replace(/\.dev(\d+)$/, "-dev.$1");
}

function isHandledAttention(session: SessionSummary, isHandled: (eventKey: string) => boolean): boolean {
  const context = currentAttention(session);
  return context !== null && isHandled(context.event_key);
}

function isUnhandledAttention(session: SessionSummary, isHandled: (eventKey: string) => boolean): boolean {
  return needsAttention(session) && !isHandledAttention(session, isHandled);
}

function attentionState(session: SessionSummary, isHandled: (eventKey: string) => boolean): "unhandled" | "handled" | "unavailable" | null {
  if (!needsAttention(session)) return null;
  if (currentAttention(session) === null) return "unavailable";
  return isHandledAttention(session, isHandled) ? "handled" : "unhandled";
}

function matchesFilter(session: SessionSummary, filter: Filter, isHandled: (eventKey: string) => boolean): boolean {
  if (filter === "all") return true;
  if (filter === "attention") return isUnhandledAttention(session, isHandled);
  if (filter === "handled") return isHandledAttention(session, isHandled);
  return session.status === filter;
}

function statusClass(session: SessionSummary): string {
  if (session.attention_reasons.includes("tool_error")) return "error";
  if (session.attention_reasons.includes("turn_aborted")) return "stuck";
  if (session.activity_state === "data_stale") return "stale";
  if (session.activity_state === "long_running_tool" || session.activity_state === "no_progress") return "stuck";
  if (session.activity_state === "tool_running") return "tool";
  if (session.activity_state === "quiet") return "quiet";
  return session.status === "stuck" ? "stale" : session.status;
}

function activityDetailText(session: SessionSummary): string {
  const now = Date.now() / 1000;
  if (session.activity_state === "data_stale") return "监控数据可能过期";
  if (session.activity_state === "quiet") return `暂时没有新事件 · ${duration(Math.max(0, now - session.activity_since))}`;
  if (session.activity_state === "tool_running") {
    const startedAt = session.pending_tool_started_at ?? session.activity_since;
    return `${session.pending_tool_name ?? "工具"} · 已运行 ${duration(Math.max(0, now - startedAt))}`;
  }
  if (session.activity_state === "long_running_tool") {
    const startedAt = session.pending_tool_started_at ?? session.activity_since;
    return `工具执行时间较长 · ${duration(Math.max(0, now - startedAt))}`;
  }
  if (session.activity_state === "no_progress") return `长时间没有新进展 · ${duration(Math.max(0, now - session.activity_since))}`;
  return session.current_action || session.current_user_message_summary || "暂无当前动作";
}

function decisionText(session: SessionSummary): string {
  if (session.attention_reasons.includes("tool_error")) return "工具执行失败，需要检查结果";
  if (session.attention_reasons.includes("turn_aborted")) return "本轮任务已中止，建议检查原因";
  if (session.activity_state !== "active") return activityStateText[session.activity_state];
  if (session.status === "running") {
    const action = session.current_action.split(" · ")[0] || "当前任务";
    return `正在执行 ${action}，暂不需要介入`;
  }
  return "本轮任务已完成，可回看执行过程";
}

function ThemeToggle({ theme, onChange }: { theme: Theme; onChange: (theme: Theme) => void }) {
  return (
    <div className="theme-toggle" aria-label="主题切换">
      <button
        className={theme === "light" ? "active" : ""}
        type="button"
        aria-label="浅色主题"
        title="切换到浅色主题"
        onClick={() => onChange("light")}
      >
        <Sun size={18} weight="bold" />
      </button>
      <button
        className={theme === "dark" ? "active" : ""}
        type="button"
        aria-label="深色主题"
        title="切换到深色主题"
        onClick={() => onChange("dark")}
      >
        <Moon size={18} weight="bold" />
      </button>
    </div>
  );
}

function useAnimatedDialogClose(onClose: () => void) {
  const [closing, setClosing] = useState(false);
  const closingRef = useRef(false);
  const timerRef = useRef<number | null>(null);

  useEffect(() => () => {
    if (timerRef.current !== null) window.clearTimeout(timerRef.current);
  }, []);

  const requestClose = useCallback(() => {
    if (closingRef.current) return;
    closingRef.current = true;
    setClosing(true);
    timerRef.current = window.setTimeout(onClose, DIALOG_EXIT_DURATION_MS);
  }, [onClose]);

  return { closing, requestClose };
}

function dialogOriginFromEvent(event: React.MouseEvent<HTMLElement>): DialogOrigin {
  const rect = event.currentTarget.getBoundingClientRect();
  return {
    x: rect.left + rect.width / 2,
    y: rect.top + rect.height / 2,
    width: rect.width,
    height: rect.height,
  };
}

function useDialogMotion(dialog: { current: HTMLDialogElement | null }, origin: DialogOrigin | null) {
  useLayoutEffect(() => {
    const element = dialog.current;
    if (element === null) return;
    element.showModal();
    if (origin === null) return;
    const rect = element.getBoundingClientRect();
    const scale = Math.min(origin.width / rect.width, origin.height / rect.height);
    element.style.setProperty("--dialog-origin-x", `${origin.x - (rect.left + rect.width / 2)}px`);
    element.style.setProperty("--dialog-origin-y", `${origin.y - (rect.top + rect.height / 2)}px`);
    element.style.setProperty("--dialog-origin-scale", String(Math.max(.08, Math.min(.55, scale))));
  }, [dialog, origin]);
}

function AttentionBar({ sessions, filter, isHandled, onFilter }: {
  sessions: SessionSummary[];
  filter: Filter;
  isHandled: (eventKey: string) => boolean;
  onFilter: (filter: Filter) => void;
}) {
  const attention = sessions.filter((session) => isUnhandledAttention(session, isHandled)).length;
  const handled = sessions.filter((session) => isHandledAttention(session, isHandled)).length;
  const running = sessions.filter((session) => session.status === "running").length;
  const completed = sessions.filter((session) => session.status === "idle").length;
  return (
    <section className="attention-bar glass" aria-label="注意力摘要">
      <div className="attention-title">注意力摘要</div>
      <div className="metrics">
        <span><i className="dot all" />全部 <strong>{sessions.length}</strong></span>
        <span><i className="dot attention" />未处理 <strong>{attention}</strong></span>
        <span><i className="dot handled" />已处理 <strong>{handled}</strong></span>
        <span><i className="dot running" />运行中 <strong>{running}</strong></span>
        <span><i className="dot idle" />已完成 <strong>{completed}</strong></span>
      </div>
      <Segmented value={filter} onChange={onFilter} />
    </section>
  );
}

function Segmented({ value, onChange }: { value: Filter; onChange: (value: Filter) => void }) {
  return (
    <div className="segmented" role="group" aria-label="筛选会话">
      {filters.map((filter) => (
        <button
          type="button"
          key={filter.value}
          className={value === filter.value ? "active" : ""}
          onClick={() => onChange(filter.value)}
        >
          {filter.label}
        </button>
      ))}
    </div>
  );
}

function ProjectNavigation({ projects, sessions, selected, importing, removing, isHandled, onSelect, onImport, onRemove, onContextMenu }: {
  projects: Project[];
  sessions: SessionSummary[];
  selected: string;
  importing: boolean;
  removing: string | null;
  isHandled: (eventKey: string) => boolean;
  onSelect: (projectKey: string) => void;
  onImport: () => void;
  onRemove: (project: Project) => void;
  onContextMenu: (event: React.MouseEvent, project: Project) => void;
}) {
  const count = (projectKey: string) => sessions.filter((session) => projectKey === "all" || session.project_key === projectKey).length;
  const attention = (projectKey: string) => sessions.filter((session) => (projectKey === "all" || session.project_key === projectKey) && isUnhandledAttention(session, isHandled)).length;
  return (
    <nav className="project-nav glass" aria-label="项目导航">
      <header className="panel-heading">
        <div><h2>项目</h2><p>一个连接，实时汇总</p></div>
        <div className="panel-actions">
          <><span>{projects.length} 个</span><button
            className="icon-button"
            type="button"
            aria-label={importing ? "正在导入项目" : "选择多个文件夹并导入项目"}
            title={importing ? "正在导入项目，请稍候" : "选择多个文件夹并导入项目"}
            aria-busy={importing}
            disabled={importing}
            onClick={onImport}
          >
            {importing ? <SpinnerGap className="project-import-spinner" size={18} weight="bold" /> : <FolderPlus size={18} weight="bold" />}
          </button></>
        </div>
      </header>
      <div className="project-list">
        <button type="button" className={`project-select ${selected === "all" ? "selected" : ""}`} onClick={() => onSelect("all")}>
          <FolderSimple size={18} /><span><strong>全部项目</strong><small>跨项目会话视图</small></span><b className={attention("all") > 0 ? "attention-count" : ""}>{attention("all") || count("all")}</b>
        </button>
        {projects.map((project) => (
          <div className="project-item" key={project.project_key}>
            <button type="button" className={`project-select ${selected === project.project_key ? "selected" : ""}`} onClick={() => onSelect(project.project_key)} onContextMenu={(event) => onContextMenu(event, project)}>
              <FolderSimple size={18} /><span><strong>{project.project_name}</strong><small title={project.project_root}>{project.project_root}</small><small className={`project-state state-${project.status}`}>{project.status === "missing" ? "目录失效" : project.persisted ? "已保存" : "仅本次"}</small></span><b className={attention(project.project_key) > 0 ? "attention-count" : ""}>{attention(project.project_key) || count(project.project_key)}</b>
            </button>
            <button className="project-remove" type="button" aria-label={`移除项目 ${project.project_name}`} title="从监控中移除" disabled={removing !== null} onClick={() => onRemove(project)}>
              <Trash size={17} />
            </button>
          </div>
        ))}
      </div>
    </nav>
  );
}

function SessionQueue({ sessions, selectedId, showProject, isHandled, onSelect, onToggle, onContextMenu }: {
  sessions: SessionSummary[];
  selectedId: string | null;
  showProject: boolean;
  isHandled: (eventKey: string) => boolean;
  onSelect: (session: SessionSummary) => void;
  onToggle: () => void;
  onContextMenu: (event: React.MouseEvent, session: SessionSummary) => void;
}) {
  const queueListRef = useRef<HTMLDivElement>(null);
  const [queueWidthRem, setQueueWidthRem] = useState(0);

  useEffect(() => {
    const element = queueListRef.current;
    if (element === null) return;
    const observer = new ResizeObserver(([entry]) => {
      if (entry !== undefined) setQueueWidthRem(entry.contentRect.width / rootFontSize());
    });
    observer.observe(element);
    return () => observer.disconnect();
  }, []);

  const showAbsoluteTime = queueWidthRem >= SESSION_ABSOLUTE_TIME_MIN_WIDTH_REM;

  return (
    <aside className="queue glass" aria-label="会话队列">
      <header className="panel-heading">
        <div>
          <h2>会话队列</h2>
          <p>按需要关注程度排序</p>
        </div>
        <div className="panel-actions">
          <span>{sessions.length} 个会话</span>
          <button
            className="icon-button queue-toggle"
            type="button"
            aria-label="折叠会话列"
            title="折叠会话列"
            aria-expanded="true"
            onClick={onToggle}
          >
            <CaretLeft size={16} weight="bold" />
          </button>
        </div>
      </header>
      <div className="queue-list" ref={queueListRef}>
        {sessions.map((session) => (
          <button
            type="button"
            key={session.session_key}
            className={`queue-row status-${statusClass(session)} ${selectedId === session.session_key ? "selected" : ""}`}
            onClick={() => onSelect(session)}
            onContextMenu={(event) => onContextMenu(event, session)}
          >
            <span className="row-status"><i className="status-dot" />{attentionStatus(session)}</span>
            <strong>{session.session_id}</strong>
            {attentionState(session, isHandled) === "unavailable"
              ? <span className="attention-state unavailable">需要新版监控服务才能处理</span>
              : attentionState(session, isHandled) !== null && <span className={`attention-state ${attentionState(session, isHandled)}`}>{attentionState(session, isHandled) === "handled" ? "已处理" : "未处理"}</span>}
            {showProject && <span className="row-project">{session.project_key}</span>}
            <span className="row-action">{activityDetailText(session)}</span>
            <time title={absoluteTime(session.last_event_at)}>{showAbsoluteTime ? absoluteTime(session.last_event_at) : relativeTime(session.last_event_at)}</time>
          </button>
        ))}
        {sessions.length === 0 && <div className="empty-queue">当前筛选下没有会话</div>}
      </div>
    </aside>
  );
}

function LocatorControls({ query, timeRange, sort, batchCount, batchTooLarge, lastBatchCount, batchUndoMessage, memoryOnly, onQuery, onTimeRange, onSort, onClear, onBatch, onBatchUndo }: {
  query: string;
  timeRange: TimeRange;
  sort: Sort;
  batchCount: number;
  batchTooLarge: boolean;
  lastBatchCount: number;
  batchUndoMessage: string | null;
  memoryOnly: boolean;
  onQuery: (value: string) => void;
  onTimeRange: (value: TimeRange) => void;
  onSort: (value: Sort) => void;
  onClear: () => void;
  onBatch: () => void;
  onBatchUndo: () => void;
}) {
  return (
    <section className="locator-controls glass" aria-label="会话定位">
      <input
        type="search"
        aria-label="搜索会话"
        placeholder="搜索会话、项目、路径或当前动作"
        value={query}
        onChange={(event) => onQuery(event.target.value)}
      />
      <select aria-label="时间范围" value={timeRange} onChange={(event) => onTimeRange(event.target.value as TimeRange)}>
        <option value="all">全部时间</option><option value="hour">最近 1 小时</option><option value="day">最近 24 小时</option><option value="week">最近 7 天</option>
      </select>
      <select aria-label="会话排序" value={sort} onChange={(event) => onSort(event.target.value as Sort)}>
        <option value="attention">需关注优先</option><option value="recent">最近更新</option><option value="oldest">最早更新</option>
      </select>
      <button type="button" onClick={onClear}>清除筛选</button>
      <button className="batch-action" type="button" disabled={batchCount === 0 || batchTooLarge} title={batchTooLarge ? "当前结果超过 1000 条，请缩小筛选范围" : undefined} onClick={onBatch}>标记当前筛选为已处理 ({batchCount})</button>
      {lastBatchCount > 0 && <button className="batch-action" type="button" onClick={onBatchUndo}>撤销最近一次批量处理 ({lastBatchCount})</button>}
      {batchUndoMessage !== null && <span className="batch-note" role="status">{batchUndoMessage}</span>}
      {batchTooLarge && <span className="batch-note" role="status">当前结果超过 1000 条，请缩小筛选范围</span>}
      {memoryOnly && <span className="batch-note warning" role="status">处理状态仅在本次打开期间保留</span>}
    </section>
  );
}

function DecisionSummary({ session, showBack, showQueueButton, onBack, onOpenSessionQueue }: { session: SessionSummary; showBack: boolean; showQueueButton: boolean; onBack: () => void; onOpenSessionQueue: () => void }) {
  const [metadataExpanded, setMetadataExpanded] = useState(false);
  const elapsed = session.current_turn_started_at
    ? Math.max(0, Date.now() / 1000 - session.current_turn_started_at)
    : 0;
  return (
    <header className={`decision status-${statusClass(session)}`}>
      <div className="decision-topline">
        <div className="decision-heading">
          {showQueueButton && <button className="queue-reopen" type="button" aria-label="展开会话列" title="展开会话列" onClick={onOpenSessionQueue}><CaretRight size={16} weight="bold" /></button>}
          <div className="session-title">
            <h2>{session.session_id}</h2>
            <span className="status-pill"><i className="status-dot" />{attentionStatus(session)}</span>
          </div>
        </div>
        {showBack && <button className="back-button" type="button" onClick={onBack}>
          <ArrowLeft size={16} /> 返回总览
        </button>}
      </div>
      <p className="decision-message"><i className="status-dot" />{decisionText(session)}</p>
      <section className="decision-context" aria-label="会话信息">
        <button
          className="decision-context-toggle"
          type="button"
          aria-controls="session-context-panel"
          aria-expanded={metadataExpanded}
          onClick={() => setMetadataExpanded((expanded) => !expanded)}
        >
          <span><CaretRight size={15} weight="bold" />会话信息</span>
          <small>{metadataExpanded ? "收起" : "展开"}</small>
        </button>
        {metadataExpanded && <div id="session-context-panel" className="decision-context-panel">
          <div className="session-metadata" aria-label="会话元信息">
            <span><small>工作路径</small><strong>{session.cwd}</strong></span>
            <span><small>会话来源</small><strong>{session.originator} / {session.source}</strong></span>
            <span><small>CLI 版本</small><strong>{session.cli_version}</strong></span>
            <span><small>模型提供方</small><strong>{session.model_provider}</strong></span>
          </div>
          <div className="decision-facts">
            <span><TerminalWindow size={20} /><small>当前动作</small><strong>{session.current_action.split(" · ")[0] || "无"}</strong></span>
            <span><Clock size={20} /><small>{session.status === "running" ? "已运行" : "本轮状态"}</small><strong>{session.status === "running" ? duration(elapsed) : "已结束"}</strong></span>
            <span><Code size={20} /><small>上下文约</small><strong>{contextSize(session.approx_context_chars)}</strong></span>
            <span><Clock size={20} /><small>最近更新</small><strong>{relativeTime(session.last_event_at)}</strong></span>
          </div>
        </div>}
      </section>
    </header>
  );
}

function ToolBlock({ tool, targeted = false, onTarget }: { tool: ToolCall; targeted?: boolean; onTarget?: (element: HTMLElement | null) => void }) {
  const toolDuration = tool.duration_seconds === null ? "执行中" : duration(tool.duration_seconds);
  return (
    <div ref={targeted ? onTarget : undefined} tabIndex={targeted ? -1 : undefined} className={`tool-block tool-${tool.status}`}>
      <div className="event-title"><TerminalWindow size={18} /><strong>工具调用</strong><code>{tool.name}</code><span>{toolStatusText[tool.status]}</span></div>
      <div className="tool-duration">工具耗时：{toolDuration}</div>
      {tool.input_summary && <DataBlock label="输入" text={tool.input_summary} />}
      {tool.result_summary && <DataBlock label="结果" text={tool.result_summary} tone={tool.status} />}
    </div>
  );
}

function DataBlock({ label, text, tone = "pending" }: { label: string; text: string; tone?: string }) {
  const [copyState, setCopyState] = useState<"idle" | "copied" | "failed">("idle");
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(text);
      setCopyState("copied");
    } catch {
      setCopyState("failed");
    }
    window.setTimeout(() => setCopyState("idle"), 1200);
  };
  return (
    <details className={`data-block tone-${tone}`} open={text.length <= 300}>
      <summary><span>{label}</span><button type="button" aria-label={`复制${label}`} title={copyState === "copied" ? "已复制" : copyState === "failed" ? "复制失败" : `复制${label}`} onClick={(event) => { event.preventDefault(); void copy(); }}><Copy size={15} /><span className="sr-only" role="status">{copyState === "copied" ? "已复制" : copyState === "failed" ? "复制失败" : ""}</span></button></summary>
      <pre>{text}</pre>
    </details>
  );
}

function TurnTimeline({ turn, target, onTarget }: { turn: Turn; target: AttentionContext | null; onTarget: (element: HTMLElement | null) => void }) {
  const targetsTurn = target !== null && target.reason !== "tool_error" && target.reason !== "long_running_tool" && target.turn_id === turn.turn_id;
  return (
    <details className="turn" open>
      <summary ref={targetsTurn ? onTarget : undefined} tabIndex={targetsTurn ? -1 : undefined}><strong>{turn.turn_id}</strong><span>{duration(turn.duration_seconds)} · {turn.ended_at ? "已完成" : "进行中"}</span></summary>
      <div className="event user-event">
        <span className="event-icon"><User size={18} weight="bold" /></span>
        <div><div className="event-title"><strong>用户消息</strong></div><p>{turn.user_message || "无"}</p></div>
      </div>
      {turn.tool_calls.map((tool, index) => <div className="event" key={`${tool.name}-${index}`}><span className="event-icon"><TerminalWindow size={18} weight="bold" /></span><ToolBlock tool={tool} targeted={(target?.reason === "tool_error" || target?.reason === "long_running_tool") && target.turn_id === turn.turn_id && target.tool_call_index === index} onTarget={onTarget} /></div>)}
      {turn.agent_text_snippets.map((snippet, index) => (
        <div className="event result-event" key={`${snippet}-${index}`}>
          <span className="event-icon"><CheckCircle size={18} weight="bold" /></span>
          <DataBlock label="Agent 回复" text={snippet} />
        </div>
      ))}
    </details>
  );
}

function traceValue(value: unknown): string {
  if (typeof value === "string") return value;
  try {
    return JSON.stringify(value, null, 2) ?? "";
  } catch {
    return String(value);
  }
}

function traceGroups(events: TraceEvent[]): TraceEvent[][] {
  const groups = new Map<string, TraceEvent[]>();
  for (const event of events) {
    const key = event.parallel_batch === null || event.parallel_batch === undefined
      ? `event-${event.sequence}`
      : `batch-${event.parallel_batch}`;
    const group = groups.get(key) ?? [];
    group.push(event);
    groups.set(key, group);
  }
  return [...groups.values()];
}

type TraceViewMode = "parallel" | "strict" | "raw" | "discovery";

function LazyTraceContent({
  sessionKey,
  traceId,
  content,
  label,
}: {
  sessionKey: string;
  traceId: string;
  content: "input" | "result" | "raw";
  label: string;
}) {
  const [text, setText] = useState("");
  const [offset, setOffset] = useState(0);
  const [hasMore, setHasMore] = useState(true);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const readChunk = async () => {
    if (loading || !hasMore) return;
    setLoading(true);
    setError(null);
    try {
      const response = await apiFetch(
        `/api/sessions/${encodeURIComponent(sessionKey)}/traces/${encodeURIComponent(traceId)}/content?content=${content}&offset=${offset}&length=262144`,
      );
      if (!response.ok) throw new Error("trace content request failed");
      const chunk = await response.json() as {
        text: string;
        length: number;
        has_more: boolean;
      };
      setText(chunk.text);
      setOffset((current) => current + chunk.length);
      setHasMore(chunk.has_more);
    } catch (requestError: unknown) {
      setError(
        requestError instanceof Error ? requestError.message : "Trace content read failed",
      );
    } finally {
      setLoading(false);
    }
  };

  return (
    <details className="data-block trace-lazy-content">
      <summary>
        <span>{label}</span>
        <button
          type="button"
          onClick={(click) => {
            click.preventDefault();
            void readChunk();
          }}
          disabled={loading}
        >
          {loading ? "Loading…" : text ? "Next chunk" : "Read content"}
        </button>
      </summary>
      {error !== null && <small className="trace-error" role="alert">{error}</small>}
      {text && <pre>{text}</pre>}
      {text && hasMore && (
        <button
          type="button"
          className="trace-content-more"
          onClick={() => {
            void readChunk();
          }}
          disabled={loading}
        >
          Next chunk
        </button>
      )}
    </details>
  );
}

function TraceCard({ event, sessionKey, showRaw = false }: { event: TraceEvent; sessionKey: string; showRaw?: boolean }) {
  const [detail, setDetail] = useState<TraceEvent | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputValue = detail?.input_value ?? event.input_value ?? event.input_preview;
  const resultValue = detail?.result_value ?? event.result_value ?? event.result_preview;
  const rawValue = detail?.raw_payload ?? event.raw_payload ?? event.raw_preview;
  const input = inputValue === null || inputValue === undefined ? "" : traceValue(inputValue);
  const result = resultValue === null || resultValue === undefined ? "" : traceValue(resultValue);
  const raw = rawValue === null || rawValue === undefined ? "" : traceValue(rawValue);
  const loadDetail = async () => {
    if (detail !== null || loading) return;
    setLoading(true);
    setError(null);
    try {
      const response = await apiFetch(`/api/sessions/${encodeURIComponent(sessionKey)}/traces/${encodeURIComponent(event.trace_id)}`);
      if (!response.ok) throw new Error("trace detail request failed");
      const metadata = await response.json() as TraceEvent;
      setDetail(metadata);
    } catch (requestError: unknown) {
      setError(requestError instanceof Error ? requestError.message : "Trace 读取失败");
    } finally {
      setLoading(false);
    }
  };
  return <article className={`trace-card trace-${event.status}`}>
    <header><span className="trace-sequence">#{event.sequence}</span><strong>{event.tool_name ?? event.event_kind}</strong><small>{event.status}</small></header>
    <div className="trace-meta"><span>{event.provider}</span>{event.namespace && <span>{event.namespace}</span>}{event.duration_ms !== null && <span>{event.duration_ms} ms</span>}{event.parallel_batch !== null && event.parallel_batch !== undefined && <span>并行批次 {event.parallel_batch}</span>}{event.source_line !== null && <span>第 {event.source_line} 行</span>}</div>
    <div className="trace-content-summary"><span>内容 {String(event.content_metadata?.bytes ?? 0)} bytes</span><span>{event.source_type ?? "来源不可用"}</span><button type="button" onClick={() => { void loadDetail(); }} disabled={loading}>{loading ? "正在读取…" : detail === null ? "查看完整内容" : "已加载完整内容"}</button></div>
    {error !== null && <small className="trace-error" role="alert">{error}</small>}
    {input && <DataBlock label="参数预览" text={input} />}
    {result && <DataBlock label="结果预览" text={result} tone={event.status} />}
    {(showRaw || event.parse_state !== "recognized") && raw && <DataBlock label="原始事件预览" text={raw} />}
    {event.parse_state !== "recognized" && <small className="trace-parse-state">解析状态：{event.parse_state}</small>}
    {detail !== null && Boolean(detail.content_metadata?.has_input) && <LazyTraceContent sessionKey={sessionKey} traceId={event.trace_id} content="input" label="Input" />}
    {detail !== null && Boolean(detail.content_metadata?.has_result) && <LazyTraceContent sessionKey={sessionKey} traceId={event.trace_id} content="result" label="Result" />}
    {detail !== null && Boolean(detail.content_metadata?.has_raw) && <LazyTraceContent sessionKey={sessionKey} traceId={event.trace_id} content="raw" label="Raw event" />}
  </article>;
}

function TraceExplorer({ sessionKey }: { sessionKey: string }) {
  const [page, setPage] = useState<TracePage | null>(null);
  const [query, setQuery] = useState("");
  const [metadataOnly, setMetadataOnly] = useState(false);
  const [eventKind, setEventKind] = useState("");
  const [traceStatusFilter, setTraceStatusFilter] = useState("");
  const [viewMode, setViewMode] = useState<TraceViewMode>("parallel");
  const [loading, setLoading] = useState(false);
  const [loadingMore, setLoadingMore] = useState(false);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const nextCursorRef = useRef<string | null>(null);
  const requestSerialRef = useRef(0);
  const activeControllerRef = useRef<AbortController | null>(null);
  const loadPage = useCallback((append: boolean) => {
    const requestSerial = requestSerialRef.current + 1;
    requestSerialRef.current = requestSerial;
    if (!append) activeControllerRef.current?.abort();
    const controller = new AbortController();
    activeControllerRef.current = controller;
    if (append) setLoadingMore(true); else setLoading(true);
    const params = new URLSearchParams({ limit: "100" });
    if (append && nextCursorRef.current !== null) params.set("cursor", nextCursorRef.current);
    if (query.trim()) params.set("query", query.trim());
    if (metadataOnly) params.set("metadata_only", "true");
    const requestedEventKind = viewMode === "discovery" ? "discovery" : eventKind;
    if (requestedEventKind) params.set("event_kind", requestedEventKind);
    if (traceStatusFilter) params.set("trace_status", traceStatusFilter);
    void apiFetch(`/api/sessions/${encodeURIComponent(sessionKey)}/traces?${params}`, { signal: controller.signal })
      .then((response) => response.ok ? response.json() as Promise<TracePage> : Promise.reject(new Error("trace request failed")))
      .then((incoming) => {
        if (requestSerial !== requestSerialRef.current) return;
        setPage((current) => append && current !== null
          ? { ...incoming, events: [...incoming.events, ...current.events] }
          : incoming);
        nextCursorRef.current = incoming.next_cursor ?? null;
        setNextCursor(incoming.next_cursor ?? null);
      })
      .catch((error: unknown) => {
        if (requestSerial !== requestSerialRef.current) return;
        if (!(error instanceof DOMException && error.name === "AbortError")) setPage(null);
      })
      .finally(() => {
        if (requestSerial !== requestSerialRef.current) return;
        setLoading(false);
        setLoadingMore(false);
        if (activeControllerRef.current === controller) activeControllerRef.current = null;
      });
    return () => controller.abort();
  }, [eventKind, metadataOnly, query, sessionKey, traceStatusFilter, viewMode]);
  useEffect(() => {
    setNextCursor(null);
    nextCursorRef.current = null;
    return loadPage(false);
  }, [loadPage]);
  useEffect(() => {
    const onRevision = (event: Event) => {
      const detail = (event as CustomEvent<{ session_key?: string }>).detail;
      if (detail?.session_key === sessionKey) void loadPage(false);
    };
    window.addEventListener("codex-trace-revision", onRevision);
    return () => window.removeEventListener("codex-trace-revision", onRevision);
  }, [loadPage, sessionKey]);
  const events = page?.events ?? [];
  const strictOrder = viewMode !== "parallel";
  const groups = strictOrder ? events.map((event) => [event]) : traceGroups(events);
  const [exporting, setExporting] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);
  const exportTrace = async () => {
    setExporting(true);
    setExportError(null);
    try {
      const response = await apiFetch(`/api/sessions/${encodeURIComponent(sessionKey)}/traces/export`);
      if (!response.ok) throw new Error("trace export failed");
      const url = URL.createObjectURL(await response.blob());
      const anchor = document.createElement("a");
      anchor.href = url;
      anchor.download = `trace-${sessionKey.replace(/[^A-Za-z0-9._-]+/g, "_")}.json`;
      anchor.click();
      URL.revokeObjectURL(url);
    } catch (error: unknown) {
      setExportError(error instanceof Error ? error.message : "Trace 导出失败");
    } finally {
      setExporting(false);
    }
  };
  return <section className="trace-explorer" aria-label="Tool Trace">
    <header className="trace-toolbar"><div><h3>Tool Trace</h3><small>{page?.total ?? 0} 条事件 · {viewMode === "parallel" ? "并行泳道" : viewMode === "strict" ? "严格时序" : viewMode === "raw" ? "原始事件" : "工具发现"} · 本机完整记录 · {page?.index_state ?? "ready"}</small></div><label><input value={query} onChange={(event) => setQuery(event.target.value)} placeholder="搜索参数、结果、工具…" aria-label="搜索 Trace" /></label><label><select aria-label="Trace 视图" value={viewMode} onChange={(event) => setViewMode(event.target.value as TraceViewMode)}><option value="parallel">并行泳道</option><option value="strict">严格时序</option><option value="raw">原始事件</option><option value="discovery">工具发现</option></select></label><label><select aria-label="事件类型" value={eventKind} onChange={(event) => setEventKind(event.target.value)} disabled={viewMode === "discovery"}><option value="">全部事件</option><option value="function_call">工具调用</option><option value="custom_tool_call">自定义工具调用</option><option value="function_call_output">工具结果</option><option value="custom_tool_call_output">自定义工具结果</option><option value="event_msg">消息事件</option><option value="tool_search">工具搜索</option><option value="web_search">Web 搜索</option><option value="available_tools">可用工具</option><option value="unknown">未知事件</option></select></label><label><select aria-label="Trace 状态" value={traceStatusFilter} onChange={(event) => setTraceStatusFilter(event.target.value)}><option value="">全部状态</option><option value="running">运行中</option><option value="succeeded">成功</option><option value="failed">失败</option><option value="cancelled">已取消</option><option value="interrupted">已中断</option><option value="unknown">未知</option></select></label><label><input type="checkbox" checked={metadataOnly} onChange={(event) => setMetadataOnly(event.target.checked)} />仅元数据</label><button type="button" className="trace-export" onClick={() => { void exportTrace(); }} disabled={exporting}>{exporting ? "正在导出…" : "导出 Trace"}</button></header>
    {exportError !== null && <p className="trace-error" role="alert">{exportError}</p>}
    {loading && <p className="loading">正在加载 Trace…</p>}
    {!loading && events.length === 0 && <p className="loading">没有匹配的 Trace 事件</p>}
    <div className={`trace-list ${strictOrder ? "strict" : ""} trace-view-${viewMode}`}>{groups.map((group, index) => <div className="trace-batch" key={`${group[0]?.trace_id ?? "empty"}-${index}`}><small className="trace-batch-label">{strictOrder ? (viewMode === "raw" ? "原始事件" : viewMode === "discovery" ? "工具发现" : "时序") : group[0]?.parallel_batch ? `并行批次 ${group[0].parallel_batch}` : "时序事件"}</small>{group.map((event) => <TraceCard key={event.trace_id} event={event} sessionKey={sessionKey} showRaw={viewMode === "raw"} />)}</div>)}</div>
    {page?.has_earlier && <button type="button" className="trace-load-more" disabled={loadingMore} onClick={() => { void loadPage(true); }}>{loadingMore ? "正在加载…" : "加载更早事件"}</button>}
  </section>;
}

function CopyButton({ label, text, disabled = false, disabledTitle }: { label: string; text: string; disabled?: boolean; disabledTitle?: string }) {
  const [state, setState] = useState<"idle" | "copied" | "failed">("idle");
  const title = disabled ? disabledTitle : state === "copied" ? "已复制" : state === "failed" ? "复制失败" : label;
  return <button className="icon-button" type="button" aria-label={label} title={title} disabled={disabled} onClick={() => {
    void navigator.clipboard.writeText(text).then(() => setState("copied"), () => setState("failed"));
    window.setTimeout(() => setState("idle"), 1200);
  }}><Copy size={16} /><span className="sr-only" role="status">{state === "copied" ? "已复制" : state === "failed" ? "复制失败" : ""}</span></button>;
}

function AttentionDetails({ session, context, currentContext, handled, memoryOnly, detailState, anchorState, targetFound, targetTool, onHandle, onUndo, onRetry }: {
  session: SessionSummary;
  context: AttentionContext;
  currentContext: AttentionContext | null;
  handled: boolean;
  memoryOnly: boolean;
  detailState: DetailState | "idle";
  anchorState: AnchorState;
  targetFound: boolean;
  targetTool: ToolCall | null;
  onHandle: () => void;
  onUndo: () => void;
  onRetry: () => void;
}) {
  const safeSessionId = /^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(session.session_id);
  const reason = context.reason === "tool_error"
    ? "工具失败"
    : context.reason === "turn_aborted"
      ? "任务中止"
      : context.reason === "long_running_tool"
        ? "工具执行时间较长"
        : "长时间没有新进展";
  const activityAttention = context.reason === "stuck" || context.reason === "long_running_tool" || context.reason === "no_progress";
  const actionLabel = activityAttention ? (handled ? "恢复提醒" : "暂不提醒") : (handled ? "撤销处理" : "标记已处理");
  const actionTitle = activityAttention
    ? (handled ? "恢复当前会话提醒" : "暂不提醒当前会话")
    : (handled ? "撤销处理当前关注事件" : "标记当前关注事件已处理");
  const changed = currentContext !== null && currentContext.event_key !== context.event_key;
  return <section className="attention-details" aria-label="注意力上下文">
    <header>
      <div><h3>注意力上下文</h3>{changed && <p className="attention-updated" role="status">关注事件已更新，本次仍定位到打开详情时的事件</p>}</div>
      <button className="attention-action" type="button" aria-label={actionLabel} title={actionTitle} onClick={handled ? onUndo : onHandle}>{actionLabel}</button>
    </header>
    <dl>
      <div><dt>原因</dt><dd>{reason}</dd></div>
      <div><dt>发生时间</dt><dd>{new Date(context.occurred_at * 1000).toLocaleString()}</dd></div>
      <div><dt>处理状态</dt><dd>{handled ? "已处理" : "未处理"}</dd></div>
      <div><dt>定位 turn</dt><dd>{context.turn_id ?? "最新位置"}</dd></div>
            <div><dt>最近失败步骤</dt><dd>{context.reason === "tool_error" ? context.tool_name : context.reason === "long_running_tool" ? context.tool_name || "当前工具" : "无失败步骤"}</dd></div>
      <div><dt>错误摘要</dt><dd>{context.reason !== "tool_error" ? "不适用" : targetTool?.result_summary || "没有可用摘要"}</dd></div>
    </dl>
    {anchorState === "locating" && <p className="attention-location" role="status">正在定位关注事件…</p>}
    {anchorState === "fallback" && <p className="attention-location" role="status">未找到历史位置，已显示最近详情</p>}
    {anchorState === "located" && detailState === "ready" && !targetFound && <p className="attention-location" role="status">历史详情中未找到对应步骤</p>}
    {(detailState === "error" || detailState === "not_found" || (anchorState === "located" && detailState === "ready" && !targetFound)) && <button className="retry-detail" type="button" onClick={onRetry}>重试详情</button>}
    <div className="attention-copy-actions">
      <CopyButton label="复制 session ID" text={session.session_id} />
      <CopyButton label="复制 codex resume" text={`codex resume ${session.session_id}`} disabled={!safeSessionId} disabledTitle="session ID 含不安全字符，只能复制原始 ID" />
    </div>
    {memoryOnly && <p className="attention-memory-warning" role="status">处理状态仅在本次打开期间保留</p>}
  </section>;
}

interface AttentionSelection {
  context: AttentionContext | null;
  nonce: number;
}

type FollowMode = "idle" | "following" | "paused" | "historical" | "ended";

function Workspace({ session, detail, detailState, anchorState, attentionSelection, currentContext, attentionHandled, memoryOnly, historicalWindow, mobileOpen, sessionQueueCollapsed, onBack, onOpenSessionQueue, onHandle, onUndo, onLoadEarlier, onResetLatest, onRetry }: {
  session: SessionSummary | null;
  detail: SessionDetail | null;
  detailState: DetailState | "idle";
  anchorState: AnchorState;
  attentionSelection: AttentionSelection;
  currentContext: AttentionContext | null;
  attentionHandled: boolean;
  memoryOnly: boolean;
  historicalWindow: boolean;
  mobileOpen: boolean;
  sessionQueueCollapsed: boolean;
  onBack: () => void;
  onOpenSessionQueue: () => void;
  onHandle: () => void;
  onUndo: () => void;
  onLoadEarlier: () => Promise<void>;
  onResetLatest: () => void;
  onRetry: () => void;
}) {
  const [errorsOnly, setErrorsOnly] = useState(false);
  const [followMode, setFollowMode] = useState<FollowMode>("idle");
  const [newContentCount, setNewContentCount] = useState(0);
  const latest = useRef<HTMLDivElement | null>(null);
  const workspaceRoot = useRef<HTMLElement | null>(null);
  const timeline = useRef<HTMLElement | null>(null);
  const followLatest = useRef(false);
  const programmaticScroll = useRef(false);
  const settleFrame = useRef<number | null>(null);
  const lastDetailRevision = useRef<string | null>(null);
  const attentionTarget = useRef<HTMLElement | null>(null);
  const lastLocatedSelection = useRef(-1);
  const context = attentionSelection.context;
  const targetTurn = context?.turn_id === null ? null : detail?.turns.find((turn) => turn.turn_id === context?.turn_id) ?? null;
  const targetTool = (context?.reason === "tool_error" || context?.reason === "long_running_tool") && targetTurn !== null && context.tool_call_index !== null
    ? targetTurn.tool_calls[context.tool_call_index] ?? null
    : null;
  const targetFound = context === null || (context.reason === "tool_error" || context.reason === "long_running_tool" ? targetTool !== null : context.turn_id === null || targetTurn !== null);
  const locateAttentionTarget = (element: HTMLElement | null) => {
    attentionTarget.current = element;
    if (
      element === null
      || context === null
      || detailState !== "ready"
      || !targetFound
      || lastLocatedSelection.current === attentionSelection.nonce
      || timeline.current === null
    ) {
      return;
    }
    // Mark only after the target exists so a missed rAF/ref race can still retry.
    lastLocatedSelection.current = attentionSelection.nonce;
    const scroller = timeline.current;
    if (scroller.closest(".compact-layout") !== null) {
      element.scrollIntoView({ block: "center" });
    } else {
      // Only the timeline owns scrolling; scrollIntoView also scrolls clipped ancestors.
      const viewport = scroller.getBoundingClientRect();
      const target = element.getBoundingClientRect();
      const toolbarHeight = (scroller.querySelector(".timeline-actions")?.getBoundingClientRect().height ?? 0)
        + Number.parseFloat(window.getComputedStyle(scroller).paddingTop);
      const freeSpace = Math.max(0, scroller.clientHeight - toolbarHeight - target.height);
      scroller.scrollTo({
        top: scroller.scrollTop + target.top - viewport.top - scroller.clientTop - toolbarHeight - freeSpace / 2,
      });
    }
    element.focus({ preventScroll: true });
    element.classList.add("attention-target-highlight");
    window.setTimeout(() => element.classList.remove("attention-target-highlight"), 1800);
  };
  const alignWorkspaceEnd = () => {
    const root = document.scrollingElement;
    const container = workspaceRoot.current;
    if (root === null || container === null) return;
    const maxTop = Math.max(0, root.scrollHeight - root.clientHeight);
    const targetTop = Math.min(maxTop, Math.max(0, root.scrollTop + container.getBoundingClientRect().bottom - root.clientHeight));
    root.scrollTop = targetTop;
  };

  const pinLatest = () => {
    const element = timeline.current;
    if (element === null) return;
    alignWorkspaceEnd();
    element.scrollTop = element.scrollHeight;
  };

  const settleLatest = () => {
    const element = timeline.current;
    if (element === null) return;
    if (settleFrame.current !== null) window.cancelAnimationFrame(settleFrame.current);
    setNewContentCount(0);
    followLatest.current = true;
    programmaticScroll.current = true;
    let previousHeight = -1;
    let stableFrames = 0;
    let attempts = 0;
    const settle = () => {
      settleFrame.current = null;
      if (!followLatest.current || timeline.current !== element || detailState !== "ready") {
        programmaticScroll.current = false;
        return;
      }
      const currentHeight = element.scrollHeight;
      const distanceFromBottom = Math.max(0, currentHeight - element.clientHeight - element.scrollTop);
      stableFrames = currentHeight === previousHeight && distanceFromBottom <= 1 ? stableFrames + 1 : 0;
      previousHeight = currentHeight;
      pinLatest();
      alignWorkspaceEnd();
      attempts += 1;
      if (stableFrames >= 4 || attempts >= 24) {
        programmaticScroll.current = false;
        if (followLatest.current && session?.status === "running" && anchorState !== "located") {
          setFollowMode("following");
        } else {
          followLatest.current = false;
          setFollowMode(session?.status === "running" ? "idle" : "ended");
        }
        return;
      }
      settleFrame.current = window.requestAnimationFrame(settle);
    };
    settle();
  };

  const scrollToLatest = () => settleLatest();

  const handleTimelineScroll = () => {
    if (!programmaticScroll.current && followLatest.current) {
      followLatest.current = false;
      setFollowMode("paused");
    }
  };
  const stopFollowingLatest = () => {
    if (followLatest.current || followMode === "following") {
      followLatest.current = false;
      setFollowMode("paused");
    }
  };

  useLayoutEffect(() => {
    if (settleFrame.current !== null) window.cancelAnimationFrame(settleFrame.current);
    followLatest.current = false;
    programmaticScroll.current = false;
    lastDetailRevision.current = null;
    lastLocatedSelection.current = -1;
    attentionTarget.current = null;
    setNewContentCount(0);
    setFollowMode(session?.status === "running" ? "idle" : "ended");
  }, [session?.session_key]);

  useEffect(() => {
    followLatest.current = false;
    setNewContentCount(0);
    setFollowMode(session?.status === "running" ? "idle" : "ended");
  }, [attentionSelection.nonce, session?.session_key]);

  useEffect(() => {
    if (session?.status === "running") {
      if (followMode === "ended") setFollowMode("idle");
      return;
    }
    if (settleFrame.current !== null) window.cancelAnimationFrame(settleFrame.current);
    followLatest.current = false;
    programmaticScroll.current = false;
    setFollowMode("ended");
  }, [followMode, session?.status]);

  useEffect(() => {
    if (anchorState === "located") {
      if (settleFrame.current !== null) window.cancelAnimationFrame(settleFrame.current);
      followLatest.current = false;
      programmaticScroll.current = false;
      setFollowMode("historical");
    } else if (anchorState === "idle" && followMode === "historical") {
      setFollowMode(session?.status === "running" ? "idle" : "ended");
    }
  }, [anchorState, followMode, session?.status]);

  useLayoutEffect(() => {
    if (!followLatest.current || detailState !== "ready") return;
    settleLatest();
  }, [detail, detailState, errorsOnly]);

  useEffect(() => {
    if (session === null || session.status !== "running" || detailState !== "ready" || detail === null) return;
    const latestTurn = detail.turns.at(-1);
    const revision = [
      session.session_key,
      detail.last_event_at,
      latestTurn?.turn_id ?? "",
      latestTurn?.agent_text_snippets.length ?? 0,
      detail.turns.length,
    ].join(":");
    if (lastDetailRevision.current === null) {
      lastDetailRevision.current = revision;
      return;
    }
    if (revision !== lastDetailRevision.current && !followLatest.current) {
      setNewContentCount((count) => count + 1);
    }
    lastDetailRevision.current = revision;
  }, [detail, detailState, session]);

  useEffect(() => () => {
    if (settleFrame.current !== null) window.cancelAnimationFrame(settleFrame.current);
  }, []);

  useEffect(() => {
    const stopFollowingOuterScroll = () => {
      if (!programmaticScroll.current && followLatest.current) {
        followLatest.current = false;
        setFollowMode("paused");
      }
    };
    window.addEventListener("scroll", stopFollowingOuterScroll, { passive: true });
    return () => window.removeEventListener("scroll", stopFollowingOuterScroll);
  }, []);

  useEffect(() => {
    if (context !== null && context.reason !== "tool_error") setErrorsOnly(false);
  }, [attentionSelection.nonce, context]);

  useLayoutEffect(() => {
    if (context === null || detailState !== "ready" || !targetFound) return;
    locateAttentionTarget(attentionTarget.current);
  }, [attentionSelection.nonce, context, detailState, targetFound]);

  if (session === null) {
    return <main className="workspace glass empty-workspace">{sessionQueueCollapsed && <button className="queue-reopen empty-queue-reopen" type="button" aria-label="展开会话列" title="展开会话列" onClick={onOpenSessionQueue}><CaretRight size={16} weight="bold" /></button>}<Warning size={28} /><h2>选择一个会话</h2><p>从左侧会话队列查看当前状态和完整时间线。</p></main>;
  }
  const followLabel = followMode === "following"
    ? "正在跟随最新回复"
    : followMode === "paused"
      ? "已暂停自动跟随，点击跳到最新后恢复"
      : "";
  const latestActionTitle = followMode === "paused"
    ? "已暂停自动跟随，点击跳到最新后恢复"
    : session.status === "running"
      ? "跳到最新并开始自动跟随"
      : "跳到会话最新内容";
  return (
    <main ref={workspaceRoot} className={`workspace glass ${mobileOpen ? "mobile-open" : ""}`}>
      <DecisionSummary session={session} showBack={mobileOpen} showQueueButton={!mobileOpen && sessionQueueCollapsed} onBack={onBack} onOpenSessionQueue={onOpenSessionQueue} />
      {context !== null && <AttentionDetails session={session} context={context} currentContext={currentContext} handled={attentionHandled} memoryOnly={memoryOnly} detailState={detailState} anchorState={anchorState} targetFound={targetFound} targetTool={targetTool} onHandle={onHandle} onUndo={onUndo} onRetry={onRetry} />}
      {context === null && needsAttention(session) && <section className="attention-compat-warning" role="status">需要新版监控服务才能处理</section>}
      <section ref={timeline} className="timeline" onScroll={handleTimelineScroll} onPointerDown={stopFollowingLatest} onWheel={stopFollowingLatest} onTouchMove={stopFollowingLatest}>
        <div className="timeline-actions">
          <label><input type="checkbox" checked={errorsOnly} onChange={(event) => setErrorsOnly(event.target.checked)} />仅看错误</label>
          {anchorState === "located" && <span className="history-indicator" role="status">历史定位视图</span>}
          {(historicalWindow || anchorState === "located") && <button type="button" onClick={onResetLatest}>回到最近</button>}
          {followLabel && <span className={`follow-indicator follow-${followMode}`} role="status" title={followLabel} aria-label={followLabel}>
            <ArrowDown size={16} weight="bold" aria-hidden="true" />
            <i aria-hidden="true" />
            <span className="sr-only">{followLabel}</span>
          </span>}
          {anchorState !== "located" && <button className={`latest-action ${followMode === "following" ? "is-following" : ""} ${newContentCount > 0 ? "has-new-content" : ""}`} type="button" title={latestActionTitle} onClick={scrollToLatest}>
            <ArrowDown size={16} weight="bold" aria-hidden="true" />
            跳到最新
            {newContentCount > 0 && <span className="follow-new-count" title={`新增 ${newContentCount} 条内容`} aria-label={`新增 ${newContentCount} 条内容`}>{newContentCount > 99 ? "99+" : newContentCount}</span>}
          </button>}
        </div>
        {detailState === "loading" && <div className="loading">正在加载时间线…</div>}
        {detailState === "error" && <div className="loading" role="alert">时间线加载失败 <button type="button" onClick={onRetry}>重试</button></div>}
        {detailState === "not_found" && <div className="loading" role="alert">会话已从快照中移除</div>}
        {detailState === "ready" && detail?.turns.length === 0 && <div className="loading">此会话还没有 turn</div>}
        {detail?.has_earlier && <button className="load-earlier" type="button" onClick={() => { void onLoadEarlier(); }}>加载更早</button>}
        {detail?.turns.filter((turn) => !errorsOnly || turn.tool_calls.some((tool) => tool.status === "error") || context?.turn_id === turn.turn_id).map((turn) => <TurnTimeline key={turn.turn_id} turn={turn} target={context} onTarget={locateAttentionTarget} />)}
        <div ref={(element) => { latest.current = element; if (context?.reason === "stuck" && context.turn_id === null) locateAttentionTarget(element); }} tabIndex={context?.reason === "stuck" && context.turn_id === null ? -1 : undefined} />
        <TraceExplorer sessionKey={session.session_key} />
      </section>
    </main>
  );
}

function DiagnosticsDialog({
  onClose,
  onCheckForUpdate,
  onNotice,
  notices,
  onCloseNotice,
  notifications,
  origin,
}: {
  onClose: () => void;
  onCheckForUpdate: (origin: DialogOrigin) => void;
  onNotice: (notice: AppNotice) => void;
  notices: StoredNotice[];
  onCloseNotice: (id: number) => void;
  origin: DialogOrigin | null;
  notifications: {
    enabled: boolean;
    permission: NotificationPermission | "unsupported";
    supported: boolean;
  };
}) {
  const [report, setReport] = useState<DiagnosticsReport | null>(null);
  const [logStatus, setLogStatus] = useState<DiagnosticLogStatus | null>(null);
  const [entries, setEntries] = useState<DiagnosticLogEntry[]>([]);
  const [cursor, setCursor] = useState<string | null>(null);
  const [total, setTotal] = useState(0);
  const [area, setArea] = useState<"overview" | "logs" | "activity" | "settings" | "about">("overview");
  const [level, setLevel] = useState<DiagnosticLogQuery["level"]>();
  const [component, setComponent] = useState("");
  const [eventCode, setEventCode] = useState("");
  const [keyword, setKeyword] = useState("");
  const [hours, setHours] = useState<1 | 6 | 24>(24);
  const [expanded, setExpanded] = useState<string | null>(null);
  const [newCount, setNewCount] = useState(0);
  const [capacityMb, setCapacityMb] = useState("50");
  const [activityAlertSeconds, setActivityAlertSeconds] = useState<number | null>(300);
  const [traceStatus, setTraceStatus] = useState<TraceStorageStatus | null>(null);
  const [traceEnabled, setTraceEnabled] = useState(true);
  const [traceRetention, setTraceRetention] = useState("permanent");
  const [error, setError] = useState<string | null>(null);
  const [saving, setSaving] = useState(false);
  const [clearing, setClearing] = useState(false);
  const [copying, setCopying] = useState(false);
  const [exporting, setExporting] = useState(false);
  const [exportProgress, setExportProgress] = useState<DiagnosticExportProgress | null>(null);
  const dialog = useRef<HTMLDialogElement | null>(null);
  const cursorRef = useRef<string | null>(null);
  const { closing, requestClose } = useAnimatedDialogClose(onClose);
  useDialogMotion(dialog, origin);

  const loadStatus = useCallback(async () => {
    if (!isDesktopRuntime) return;
    const status = await diagnosticLogStatus();
    setLogStatus(status);
    setCapacityMb(String(status.capacityMb));
  }, []);

  const loadEntries = useCallback(async (append: boolean) => {
    if (!isDesktopRuntime) return;
    const result = await queryDiagnosticLogs({
      level,
      component: component.trim() || undefined,
      eventCode: eventCode.trim() || undefined,
      keyword: keyword.trim() || undefined,
      sinceHours: hours,
      pageSize: 100,
      cursor: append ? cursorRef.current ?? undefined : undefined,
    });
    cursorRef.current = result.nextCursor;
    setCursor(result.nextCursor);
    setTotal(result.total);
    setEntries((current) => append ? [...current, ...result.entries] : result.entries);
    setNewCount(0);
  }, [component, eventCode, hours, keyword, level]);

  useEffect(() => {
    const controller = new AbortController();
    apiFetch("/api/diagnostics", { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error("diagnostics");
        return response.json() as Promise<DiagnosticsReport>;
      })
      .then(setReport)
      .catch((reason: unknown) => {
        if (!(reason instanceof DOMException && reason.name === "AbortError")) setError("诊断概览加载失败");
      });
    apiFetch("/api/config", { signal: controller.signal })
      .then((response) => response.json() as Promise<{ activity_alert_seconds?: number | null; trace?: TraceStorageStatus }>)
      .then((config) => {
        setActivityAlertSeconds(config.activity_alert_seconds ?? 300);
        if (config.trace) {
          setTraceStatus(config.trace);
          setTraceEnabled(config.trace.enabled);
          setTraceRetention(config.trace.retention_days === null ? "permanent" : String(config.trace.retention_days));
        }
      })
      .catch((reason: unknown) => {
        if (!(reason instanceof DOMException && reason.name === "AbortError")) setError("会话提醒设置加载失败");
      });
    void loadStatus().catch(() => setError("本地日志状态加载失败"));
    apiFetch("/api/traces/status", { signal: controller.signal })
      .then((response) => response.json() as Promise<TraceStorageStatus>)
      .then((status) => {
        setTraceStatus(status);
        setTraceEnabled(status.enabled);
        setTraceRetention(status.retention_days === null ? "permanent" : String(status.retention_days));
      })
      .catch((reason: unknown) => {
        if (!(reason instanceof DOMException && reason.name === "AbortError")) setError("Tool Trace 状态加载失败");
      });
    return () => controller.abort();
  }, [loadStatus]);

  useEffect(() => {
    cursorRef.current = null;
    void loadEntries(false).catch(() => setError("本地诊断日志查询失败"));
  }, [loadEntries]);

  useEffect(() => {
    let disposed = false;
    let stop: () => void = () => undefined;
    void listenDiagnosticLogAppended(() => {
      if (disposed) return;
      const atTop = (document.querySelector<HTMLUListElement>(".diagnostics-log-entries")?.scrollTop ?? 0) <= 8;
      if (area === "logs" && (!atTop || expanded !== null)) {
        setNewCount((count) => count + 1);
        return;
      }
      if (area === "logs") void loadEntries(false).catch(() => setError("新日志刷新失败"));
      else setNewCount((count) => count + 1);
    }).then((unlisten) => {
      if (disposed) unlisten();
      else stop = unlisten;
    });
    return () => {
      disposed = true;
      stop();
    };
  }, [area, expanded, loadEntries]);

  useEffect(() => {
    let disposed = false;
    let stop: () => void = () => undefined;
    void listenDiagnosticExportProgress((progress) => {
      if (!disposed) setExportProgress(progress);
    }).then((unlisten) => {
      if (disposed) unlisten();
      else stop = unlisten;
    });
    return () => {
      disposed = true;
      stop();
    };
  }, []);

  const saveSettings = async (enableDebug: boolean) => {
    const value = Number(capacityMb);
    if (!Number.isInteger(value) || value < 5 || value > 500) {
      onNotice({ kind: "warning", title: "日志容量无效", message: "请输入 5 到 500 MB 之间的整数" });
      return;
    }
    setSaving(true);
    setError(null);
    try {
      const status = await updateDiagnosticLogSettings(value, enableDebug);
      setLogStatus(status);
      setCapacityMb(String(status.capacityMb));
      onNotice({ kind: "success", title: "日志设置已保存", message: enableDebug ? "DEBUG 已临时开启，30 分钟后自动恢复" : "已恢复为 WARN / ERROR 记录" });
    } catch {
      onNotice({ kind: "error", title: "保存日志设置失败", message: "本次设置未生效，请稍后重试" });
    } finally {
      setSaving(false);
    }
  };

  const saveActivitySettings = async (value: number | null) => {
    try {
      const response = await apiFetch("/api/config/activity", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ activity_alert_seconds: value }),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      setActivityAlertSeconds(value);
      window.dispatchEvent(new Event(ACTIVITY_BASELINE_RESET_EVENT));
      onNotice({ kind: "success", title: "会话提醒已更新", message: value === null ? "已关闭长时间无进展提醒" : `已设置为 ${Math.round(value / 60)} 分钟` });
    } catch {
      onNotice({ kind: "error", title: "保存会话提醒失败", message: "本次设置未生效，请稍后重试" });
    }
  };

  const saveTraceSettings = async () => {
    const retention = traceRetention === "permanent" ? null : Number(traceRetention);
    if (retention !== null && (!Number.isInteger(retention) || retention <= 0)) {
      onNotice({ kind: "warning", title: "保留时长无效", message: "请输入正整数天数或选择永久保留" });
      return;
    }
    try {
      const response = await apiFetch("/api/traces/settings", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ enabled: traceEnabled, retention_days: retention }),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const status = await response.json() as TraceStorageStatus;
      setTraceStatus(status);
      onNotice({ kind: "success", title: "Tool Trace 设置已保存", message: traceEnabled ? "已开启本机完整记录" : "已暂停新 Trace 记录" });
    } catch {
      onNotice({ kind: "error", title: "保存 Tool Trace 设置失败", message: "本次设置未生效" });
    }
  };

  const clearTraceStorage = async () => {
    try {
      const response = await apiFetch("/api/traces/clear", { method: "POST" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const status = await response.json() as TraceStorageStatus;
      setTraceStatus(status);
      onNotice({ kind: "success", title: "追踪记录已清除", message: "只删除应用 Trace 与索引，不影响 Codex 原始会话日志" });
    } catch {
      onNotice({ kind: "error", title: "清除追踪记录失败", message: "原有记录仍保留" });
    }
  };

  const setBackfillPaused = async (pause: boolean) => {
    try {
      const response = await apiFetch(`/api/traces/backfill/${pause ? "pause" : "resume"}`, { method: "POST" });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const status = await response.json() as TraceStorageStatus;
      setTraceStatus(status);
      onNotice({ kind: "success", title: pause ? "历史回填已暂停" : "历史回填已恢复", message: pause ? "后台索引构建已暂停，可随时恢复" : "后台索引构建已恢复" });
    } catch {
      onNotice({ kind: "error", title: "历史回填操作失败", message: "本次操作未生效，请稍后重试" });
    }
  };

  const copySummary = async () => {
    if (logStatus === null) return;
    setCopying(true);
    try {
      await navigator.clipboard.writeText(JSON.stringify({
        debugEnabled: logStatus.debugEnabled,
        debugRemainingSeconds: logStatus.debugRemainingSeconds,
        capacityMb: logStatus.capacityMb,
        usedBytes: logStatus.usedBytes,
        recentEntries: entries.slice(0, 20),
        lastError: logStatus.lastError,
      }, null, 2));
      onNotice({ kind: "success", title: "脱敏摘要已复制", message: "摘要已复制到剪贴板" });
    } catch {
      onNotice({ kind: "error", title: "复制脱敏摘要失败", message: "请检查剪贴板权限后重试" });
    } finally {
      setCopying(false);
    }
  };

  const clearLogs = async () => {
    if (!window.confirm("确认清空本机全部诊断日志吗？")) return;
    setClearing(true);
    try {
      const status = await clearDiagnosticLogs();
      setLogStatus(status);
      cursorRef.current = null;
      await loadEntries(false);
      onNotice({ kind: "success", title: "日志已清空", message: "本机诊断日志和近期摘要已清除" });
    } catch {
      onNotice({ kind: "error", title: "清空诊断日志失败", message: "原有日志仍保留，稍后可以重试" });
    } finally {
      setClearing(false);
    }
  };

  const copyEntry = async (entry: DiagnosticLogEntry) => {
    try {
      await navigator.clipboard.writeText(JSON.stringify(entry));
      onNotice({ kind: "success", title: "日志记录已复制", message: "完整脱敏 JSONL 已复制到剪贴板" });
    } catch {
      onNotice({ kind: "error", title: "复制完整 JSONL 失败", message: "请检查剪贴板权限后重试" });
    }
  };

  const exportLogs = async () => {
    if (!isDesktopRuntime) return;
    try {
      const destination = await chooseDiagnosticLogExportPath();
      if (destination === null) return;
      const preview = await diagnosticLogExportPreview();
      const sizeMb = (preview.estimatedBytes / 1024 / 1024).toFixed(2);
      if (!window.confirm(`将导出 ${preview.fileCount} 个日志文件、约 ${sizeMb} MB（${preview.recordCount} 条记录）。仅本机、已脱敏、不会自动上传，是否继续？`)) return;
      setExporting(true);
      setExportProgress({
        processedFiles: 0,
        totalFiles: preview.fileCount,
        processedRecords: 0,
        processedBytes: 0,
        totalBytes: preview.estimatedBytes,
      });
      const result = await exportDiagnosticLogs(destination);
      if (result.status === "partial") {
        onNotice({ kind: "warning", title: "日志导出部分完成", message: `有 ${result.failedFileCount} 个文件未能读取，其余日志已导出` });
      } else {
        onNotice({ kind: "success", title: "日志已导出", message: `已导出 ${preview.recordCount} 条脱敏记录` });
      }
    } catch (reason: unknown) {
      if (!(reason instanceof Error && reason.message.includes("cancelled"))) onNotice({ kind: "error", title: "诊断日志导出失败", message: "日志未导出，请稍后重试" });
    } finally {
      setExporting(false);
    }
  };

  const closeDialog = () => {
    if (exporting) void cancelDiagnosticLogExport();
    requestClose();
  };

  const description = (code: string) => ({
    startup_ready: "sidecar 已就绪",
    server_ready: "服务已就绪",
    monitor_starting: "监控器启动",
    monitor_runtime_failed: "监控器运行失败",
    listener_bind_failed: "监听端口绑定失败",
    server_start_failed: "服务启动失败",
    request_completed: "请求完成",
    request_failed: "请求处理失败",
    websocket_auth_rejected: "实时连接认证被拒绝",
    websocket_origin_rejected: "实时连接来源被拒绝",
    watcher_start_failed: "文件监听启动失败",
    watcher_stop_failed: "文件监听停止失败",
    jsonl_parse_failed: "会话文件解析失败",
    jsonl_unknown_event: "发现未知事件",
    jsonl_line_malformed: "发现格式错误行",
    jsonl_line_oversized: "发现超长日志行",
    jsonl_process_failed: "文件变更处理失败",
    jsonl_poll_failed: "文件轮询失败",
    store_update: "状态增量更新",
    store_full_sync: "状态完整同步",
    stdout_closed_after_ready: "sidecar 就绪后退出",
    startup_token_failed: "启动令牌生成失败",
    executable_path_failed: "sidecar 路径解析失败",
    process_start_failed: "sidecar 进程启动失败",
    stdout_unavailable: "sidecar 输出不可用",
    runtime_state_failed: "桌面运行状态不可用",
    exited_before_ready: "sidecar 就绪前退出",
    startup_timeout: "sidecar 启动超时",
    invalid_log_record: "日志格式无效",
    redaction_failed: "脱敏校验失败",
    native_notification_shown: "桌面通知已提交",
    native_notification_show_failed: "桌面通知显示失败",
    native_notification_activated: "桌面通知已激活",
    updater_unavailable: "更新器不可用",
    update_check_failed: "更新检查失败",
    update_download_failed: "更新下载失败",
    update_install_failed: "更新安装失败",
    status_read_failed: "日志状态读取失败",
    query_failed: "日志查询失败",
    export_preview_failed: "导出预览失败",
    export_failed: "日志导出失败",
    settings_write_failed: "日志设置写入失败",
    directory_open_failed: "日志目录打开失败",
    clear_failed: "日志清空失败",
    window_unminimize_failed: "窗口取消最小化失败",
    window_show_failed: "窗口显示失败",
    window_focus_failed: "窗口聚焦失败",
    window_hide_failed: "窗口隐藏失败",
    attention_inbox_emit_failed: "收件箱打开事件发送失败",
  }[code] ?? "诊断事件");
  const notificationSummary = !notifications.supported
    ? "状态不可用 · 当前运行环境不支持"
    : notifications.permission === "denied"
      ? "权限已拒绝 · 请在系统设置中手动允许"
      : notifications.enabled
        ? "已开启 · 权限已允许"
        : "已关闭 · 可通过顶部铃铛开启";

  return <dialog ref={dialog} className={`diagnostics-dialog glass${closing ? " dialog-closing" : ""}`} role="dialog" aria-labelledby="settings-title" onCancel={(event) => { event.preventDefault(); closeDialog(); }} onClick={(event) => { if (event.target === event.currentTarget) closeDialog(); }}>
    <header><div><h2 id="settings-title">设置</h2><p>应用设置 · 日志与隐私</p></div><button autoFocus className="icon-button" type="button" aria-label="关闭设置" title="关闭" onClick={closeDialog}><X size={18} weight="bold" /></button></header>
    <div className="diagnostics-shell">
      <nav className="diagnostics-nav" aria-label="设置区域">
        {([["overview", "概览", ChartLineUp], ["logs", "日志", ListBullets], ["activity", "会话", Bell], ["settings", "日志设置", Gear], ["about", "关于", Info]] as const).map(([value, label, Icon]) => <button key={value} type="button" className={area === value ? "active" : ""} onClick={() => setArea(value)}><Icon size={16} weight="duotone" aria-hidden="true" /><span>{label}</span></button>)}
      </nav>
      <div className="diagnostics-panel">
        {area === "overview" && <section className="diagnostics-area"><div className="diagnostics-area-heading diagnostics-area-heading-with-icon"><div className="diagnostics-area-title"><div className="diagnostics-area-icon" aria-hidden="true"><ChartLineUp size={20} weight="duotone" /></div><div><h3>运行概览</h3><p>查看监控服务、连接状态和本机数据概况。</p></div></div></div>{report === null && <div className="loading">正在加载设置…</div>}{report && <><div className="diagnostics-health-grid"><article><strong>sidecar 进程 / 协议</strong><span>{displayVersion(report.application.version)} · v{report.application.protocol_version}</span></article><article><strong>实时连接 / 最近同步</strong><span>{report.health.websocket_clients} 个客户端 · {report.health.last_reconciliation_at === null ? "尚未同步" : relativeTime(report.health.last_reconciliation_at)}</span></article><article><strong>Windows 通知</strong><span>{notificationSummary}</span></article><article><strong>日志模式 / 容量</strong><span>{logStatus?.debugEnabled ? "临时 DEBUG" : "WARN / ERROR"} · {logStatus?.capacityMb ?? 50} MB</span></article></div><div className="diagnostics-overview-grid" aria-label="运行指标">
  <article className="diagnostics-metric-card">
    <div className="diagnostics-metric-icon" aria-hidden="true"><FolderSimple size={20} weight="duotone" /></div>
    <div><span className="diagnostics-metric-label">项目</span><strong>{report.health.project_count}</strong><span className="diagnostics-metric-caption">已接入项目</span></div>
  </article>
  <article className="diagnostics-metric-card">
    <div className="diagnostics-metric-icon" aria-hidden="true"><ChatCircle size={20} weight="duotone" /></div>
    <div><span className="diagnostics-metric-label">会话</span><strong>{report.health.session_count}</strong><span className="diagnostics-metric-caption">已发现会话</span></div>
  </article>
  <article className="diagnostics-metric-card">
    <div className="diagnostics-metric-icon diagnostics-metric-icon-success" aria-hidden="true"><Pulse size={20} weight="duotone" /></div>
    <div><span className="diagnostics-metric-label">实时连接</span><strong>{report.health.websocket_clients}</strong><span className="diagnostics-metric-caption">当前客户端</span></div>
  </article>
  <article className="diagnostics-metric-card">
    <div className="diagnostics-metric-icon diagnostics-metric-icon-warning" aria-hidden="true"><Warning size={20} weight="duotone" /></div>
    <div><span className="diagnostics-metric-label">解析异常</span><strong>{report.health.unknown_event_count + report.health.malformed_line_count + report.health.oversized_line_count}</strong><span className="diagnostics-metric-caption">未知 / 损坏 / 超大</span></div>
  </article>
</div>
<section className="diagnostics-status-card" aria-label="会话状态">
  <div className="diagnostics-status-card-heading"><div><h4>会话状态</h4><p>当前会话的实时分布。</p></div><Pulse size={20} weight="duotone" aria-hidden="true" /></div>
  <div className="diagnostics-status-metrics">
    <div className="diagnostics-status-metric diagnostics-status-running"><Pulse size={16} weight="fill" aria-hidden="true" /><div><span>运行中</span><strong>{report.health.status_counts.running ?? 0}</strong></div></div>
    <div className="diagnostics-status-metric diagnostics-status-attention"><Warning size={16} weight="fill" aria-hidden="true" /><div><span>需关注</span><strong>{report.health.status_counts.stuck ?? 0}</strong></div></div>
    <div className="diagnostics-status-metric diagnostics-status-completed"><CheckCircle size={16} weight="fill" aria-hidden="true" /><div><span>已完成</span><strong>{report.health.status_counts.idle ?? 0}</strong></div></div>
  </div>
  <div className="diagnostics-status-bar" role="img" aria-label="会话状态分布">
    <span className="diagnostics-status-bar-running" style={{ width: `${((report.health.status_counts.running ?? 0) / Math.max((report.health.status_counts.running ?? 0) + (report.health.status_counts.stuck ?? 0) + (report.health.status_counts.idle ?? 0), 1)) * 100}%` }} />
    <span className="diagnostics-status-bar-attention" style={{ width: `${((report.health.status_counts.stuck ?? 0) / Math.max((report.health.status_counts.running ?? 0) + (report.health.status_counts.stuck ?? 0) + (report.health.status_counts.idle ?? 0), 1)) * 100}%` }} />
    <span className="diagnostics-status-bar-completed" style={{ width: `${((report.health.status_counts.idle ?? 0) / Math.max((report.health.status_counts.running ?? 0) + (report.health.status_counts.stuck ?? 0) + (report.health.status_counts.idle ?? 0), 1)) * 100}%` }} />
  </div>
</section></>}</section>}
        {area === "logs" && <section className="diagnostics-area"><div className="diagnostics-area-heading diagnostics-area-heading-with-icon"><div className="diagnostics-area-title"><div className="diagnostics-area-icon" aria-hidden="true"><ListBullets size={20} weight="duotone" /></div><div><h3>本机诊断日志</h3><p>最近 {hours} 小时 · {total} 条匹配记录{total >= 1000 ? "（已达到最多 1000 条）" : ""}</p></div></div><div className="diagnostics-area-actions">{newCount > 0 && <button type="button" onClick={() => { void loadEntries(false); }}>有 {newCount} 条新日志，刷新</button>}<button className="primary" type="button" disabled={exporting} onClick={() => { void exportLogs(); }}>{exporting ? "正在导出…" : "导出全部日志"}</button></div></div><div className="diagnostics-log-filters"><select aria-label="级别" value={level ?? ""} onChange={(event) => setLevel(event.target.value ? event.target.value as DiagnosticLogQuery["level"] : undefined)}><option value="">全部级别</option><option value="error">ERROR</option><option value="warn">WARN</option><option value="debug">DEBUG</option></select><input aria-label="组件" placeholder="组件" value={component} onChange={(event) => setComponent(event.target.value)} /><input aria-label="事件码" placeholder="事件码" value={eventCode} onChange={(event) => setEventCode(event.target.value)} /><input aria-label="关键词" placeholder="脱敏摘要关键词" value={keyword} onChange={(event) => setKeyword(event.target.value)} /><select aria-label="时间范围" value={hours} onChange={(event) => setHours(Number(event.target.value) as 1 | 6 | 24)}><option value={1}>最近 1 小时</option><option value={6}>最近 6 小时</option><option value={24}>最近 24 小时</option></select></div><ul className="diagnostics-log-entries" aria-label="本机诊断事件">{entries.length === 0 && <li>暂无匹配日志</li>}{entries.map((entry, index) => { const key = entry.timestamp + "-" + index; const isExpanded = expanded === key; return <li className={"diagnostics-log-row " + (isExpanded ? "expanded" : "")} key={key}><button type="button" className="diagnostics-log-row-main" onClick={() => setExpanded(isExpanded ? null : key)} aria-expanded={isExpanded}><time dateTime={entry.timestamp}>{new Date(entry.timestamp).toLocaleString()}</time><strong className={"diagnostics-level-" + entry.level.toLowerCase()}>{entry.level.toUpperCase()}</strong><span><b>{entry.component}</b> · {entry.event_code} · {description(entry.event_code)}</span></button>{isExpanded && <div className="diagnostics-log-detail"><pre>{JSON.stringify(entry, null, 2)}</pre><button type="button" onClick={() => { void copyEntry(entry); }}>复制完整 JSONL</button></div>}</li>; })}</ul>{cursor !== null && <button type="button" className="diagnostics-load-more" onClick={() => { void loadEntries(true); }}>加载更多（100）</button>}</section>}
        {area === "activity" && <section className="diagnostics-area diagnostics-activity"><div className="diagnostics-area-heading diagnostics-area-heading-with-icon"><div className="diagnostics-area-icon" aria-hidden="true"><Bell size={20} weight="duotone" /></div><div><h3>会话提醒</h3><p>帮助你及时发现长时间没有进展的任务。错误和中止提醒始终保留。</p></div></div><div className="diagnostics-activity-card"><div><strong>长时间无进展提醒</strong><span>任务持续一段时间没有新进展时提醒你关注。</span></div><label><span className="sr-only">长时间无进展提醒阈值</span><select aria-label="长时间无进展提醒" value={activityAlertSeconds === null ? "off" : String(activityAlertSeconds)} onChange={(event) => { void saveActivitySettings(event.target.value === "off" ? null : Number(event.target.value)); }}><option value="180">3 分钟</option><option value="300">5 分钟（推荐）</option><option value="600">10 分钟</option><option value="900">15 分钟</option><option value="off">关闭</option></select></label></div></section>}
        {area === "settings" && <section className="diagnostics-area diagnostics-settings">
          <div className="diagnostics-area-heading diagnostics-area-heading-with-icon"><div className="diagnostics-area-title"><div className="diagnostics-area-icon" aria-hidden="true"><Gear size={20} weight="duotone" /></div><div><h3>日志设置</h3><p>管理本机日志的记录级别、存储容量和隐私边界。</p></div></div></div>
          {logStatus && <>
            <section className="diagnostics-settings-card diagnostics-settings-policy" aria-labelledby="diagnostics-settings-policy-title">
              <div className="diagnostics-settings-card-heading"><div><h4 id="diagnostics-settings-policy-title">日志策略</h4><p>日志只保存在本机应用数据目录，不会自动上传。</p></div><Code size={20} weight="duotone" aria-hidden="true" /></div>
              <div className="diagnostics-settings-policy-details">
                <div><span>保存范围</span><strong>仅本机</strong><small>不会自动上传</small></div>
                <div><span>记录级别</span><strong>{logStatus.debugEnabled ? "临时 DEBUG" : "WARN / ERROR"}</strong><small>{logStatus.debugEnabled ? "排障模式已开启" : "默认低噪声记录"}</small></div>
                <div><span>单文件滚动</span><strong>5 MB</strong><small>自动保留最新日志</small></div>
              </div>
              <label className="diagnostics-debug-toggle diagnostics-settings-debug-row"><input type="checkbox" checked={logStatus.debugEnabled} disabled={saving} onChange={(event) => { void saveSettings(event.target.checked); }} /><span><strong>临时开启 DEBUG</strong><small>开启后 30 分钟自动恢复为 WARN / ERROR</small></span></label>
            </section>
            <section className="diagnostics-settings-card diagnostics-settings-storage" aria-labelledby="diagnostics-settings-storage-title">
              <div className="diagnostics-settings-card-heading"><div><h4 id="diagnostics-settings-storage-title">存储用量</h4><p>目录容量可设置为 5–500 MB，达到上限时优先删除最旧日志。</p></div><FolderSimple size={20} weight="duotone" aria-hidden="true" /></div>
              <div className="diagnostics-settings-usage"><div className="diagnostics-settings-usage-label"><span>当前占用</span><strong>{(logStatus.usedBytes / 1024 / 1024).toFixed(2)} MB <small>/ {logStatus.capacityMb} MB</small></strong></div><div className="diagnostics-settings-usage-bar" role="progressbar" aria-label="日志目录使用量" aria-valuemin={0} aria-valuemax={100} aria-valuenow={Math.min((logStatus.usedBytes / (logStatus.capacityMb * 1024 * 1024)) * 100, 100)}><span style={{ width: `${Math.min((logStatus.usedBytes / (logStatus.capacityMb * 1024 * 1024)) * 100, 100)}%` }} /></div><div className="diagnostics-settings-usage-meta"><span>最近查询</span><strong>{entries.length} / {total} 条</strong></div></div>
              <div className="diagnostics-settings-capacity-control"><label><span>目录上限（MB）</span><input type="number" min="5" max="500" step="1" value={capacityMb} onChange={(event) => setCapacityMb(event.target.value)} /></label><button type="button" disabled={saving} onClick={() => { void saveSettings(logStatus.debugEnabled); }}>保存容量</button><small>范围：5–500 MB</small></div>
            </section>
            <section className="diagnostics-settings-card diagnostics-settings-actions" aria-labelledby="diagnostics-settings-actions-title">
              <div className="diagnostics-settings-card-heading"><div><h4 id="diagnostics-settings-actions-title">本机日志操作</h4><p>打开、复制或清空当前设备上的诊断日志。</p></div><FolderSimple size={20} weight="duotone" aria-hidden="true" /></div>
              <div className="diagnostics-log-actions"><button type="button" onClick={() => { void openDiagnosticLogDirectory().then(() => onNotice({ kind: "success", title: "日志目录已打开", message: "已在文件管理器中打开本机日志目录" })).catch(() => onNotice({ kind: "error", title: "打开日志目录失败", message: "请确认应用具有访问本机文件的权限" })); }}>打开日志目录</button><button type="button" disabled={copying} onClick={() => { void copySummary(); }}>{copying ? "正在复制…" : "复制脱敏摘要"}</button><button type="button" disabled={clearing} onClick={() => { void clearLogs(); }}>{clearing ? "正在清空…" : "清空日志"}</button></div>
            </section>
            <section className="diagnostics-settings-card diagnostics-settings-trace" aria-labelledby="trace-settings-title">
              <div className="diagnostics-settings-card-heading"><div><h4 id="trace-settings-title">Tool Trace</h4><p>完整保存 Codex 工具调用，仅保存在本机，不会自动上传或同步。</p></div><Code size={20} weight="duotone" aria-hidden="true" /></div>
              <div className="diagnostics-settings-policy-details"><div><span>当前占用</span><strong>{((traceStatus?.used_bytes ?? 0) / 1024 / 1024).toFixed(2)} MB</strong><small>{traceStatus?.file_count ?? 0} 个会话文件</small></div><div><span>默认行为</span><strong>{traceEnabled ? "已开启" : "已暂停"}</strong><small>不丢事件，后台追加保存</small></div><div><span>索引与历史</span><strong>{traceStatus?.index_state === "ready" ? "就绪" : "不可用"}</strong><small>历史回填：{traceStatus?.backfill ? `${traceStatus.backfill.processed}/${traceStatus.backfill.total} · ${traceStatus.backfill.state}` : traceStatus?.backfill_state === "complete" ? "已完成" : "未开始"}</small></div></div>
              <label className="diagnostics-debug-toggle diagnostics-settings-debug-row"><input type="checkbox" checked={traceEnabled} onChange={(event) => setTraceEnabled(event.target.checked)} /><span><strong>开启 Tool Trace</strong><small>保留完整参数、结果和未知事件</small></span></label>
              <div className="diagnostics-settings-capacity-control"><label><span>保留时长</span><select value={traceRetention} onChange={(event) => setTraceRetention(event.target.value)}><option value="permanent">永久</option><option value="7">7 天</option><option value="30">30 天</option><option value="90">90 天</option><option value="365">365 天</option></select></label><button type="button" onClick={() => { void saveTraceSettings(); }}>保存 Trace 设置</button><button type="button" onClick={() => { void clearTraceStorage(); }}>清除全部追踪记录</button></div>
              {(traceStatus?.backfill_state === "building" || traceStatus?.backfill_state === "paused") && <div className="diagnostics-settings-capacity-control"><small>历史回填不会丢失原始事件，可在后台继续构建索引。</small>{traceStatus.backfill_state === "building" ? <button type="button" onClick={() => { void setBackfillPaused(true); }}>暂停历史回填</button> : <button type="button" onClick={() => { void setBackfillPaused(false); }}>恢复历史回填</button>}</div>}
            </section>
          </>}
          <details className="diagnostics-settings-card diagnostics-settings-privacy">
            <summary><span><Info size={18} weight="duotone" aria-hidden="true" />隐私说明</span><CaretRight size={16} weight="bold" aria-hidden="true" /></summary>
            <div className="diagnostics-settings-privacy-content"><p>日志只记录时间、级别、组件、稳定事件码、耗时和脱敏错误类别。</p><p>不会记录会话正文、命令、完整项目路径、凭据、URL、端口、事件标识或会话标识。路径只保留类别和本次运行别名，网络只保留协议、范围、状态和系统错误类别。</p><p>展开的完整 JSONL 是磁盘中已经保存的脱敏记录，无法恢复被删除的原始内容。应用不会自动上传日志。</p></div>
          </details>
        </section>}
        {area === "about" && <section className="diagnostics-area diagnostics-about"><div className="diagnostics-area-heading diagnostics-area-heading-with-icon"><div className="diagnostics-area-title"><div className="diagnostics-area-icon" aria-hidden="true"><Info size={20} weight="duotone" /></div><div><h3>关于</h3><p>Codex Session Monitor 桌面端</p></div></div></div><div className="diagnostics-health-grid"><article><strong>应用版本</strong><span>{displayVersion(frontendPackage.version)}</span></article><article><strong>监控协议</strong><span>{report ? `v${report.application.protocol_version}` : "读取中…"}</span></article></div><div className="diagnostics-log-actions"><button type="button" disabled={!isDesktopRuntime} onClick={(event) => { const nextOrigin = dialogOriginFromEvent(event); closeDialog(); window.setTimeout(() => onCheckForUpdate(nextOrigin), DIALOG_EXIT_DURATION_MS); }}>检查更新</button></div>{!isDesktopRuntime && <p>浏览器开发模式不提供桌面安装包更新。</p>}<div className="diagnostics-contact"><h3>联系作者</h3><p>如果你发现问题或有改进建议，欢迎通过以下方式联系。</p><div className="diagnostics-contact-links"><a className="diagnostics-contact-link" href={`mailto:${AUTHOR_EMAIL}`} aria-label={`发送邮件至 ${AUTHOR_EMAIL}`} onClick={(event) => { if (isDesktopRuntime) { event.preventDefault(); void openUrl(`mailto:${AUTHOR_EMAIL}`).catch(() => setError("打开邮件客户端失败")); } }}><EnvelopeSimple size={18} weight="bold" aria-hidden="true" /><span>{AUTHOR_EMAIL}</span></a><a className="diagnostics-contact-link" href={REPOSITORY_URL} target="_blank" rel="noreferrer" aria-label="打开 GitHub 仓库" onClick={(event) => { if (isDesktopRuntime) { event.preventDefault(); void openUrl(REPOSITORY_URL).catch(() => setError("打开 GitHub 仓库失败")); } }}><GithubLogo size={18} weight="bold" aria-hidden="true" /><span>GitHub 仓库</span></a></div></div></section>}
      </div>
    </div>
    {error && <p className="diagnostics-error" role="alert">{error}</p>}
    {exporting && exportProgress && <div className="diagnostics-export-progress" role="status"><span>正在导出：{exportProgress.processedFiles} / {exportProgress.totalFiles} 个文件，{(exportProgress.processedBytes / 1024 / 1024).toFixed(2)} / {(exportProgress.totalBytes / 1024 / 1024).toFixed(2)} MB，{exportProgress.processedRecords} 条记录</span><button type="button" onClick={() => { void cancelDiagnosticLogExport(); }}>取消导出</button></div>}
    <NoticeToast notices={notices} onClose={onCloseNotice} />
  </dialog>;
}

function DiagnosticsDialogLegacy({ onClose }: { onClose: () => void }) {
  const [report, setReport] = useState<DiagnosticsReport | null>(null);
  const [logStatus, setLogStatus] = useState<DiagnosticLogStatus | null>(null);
  const [capacityMb, setCapacityMb] = useState("50");
  const [error, setError] = useState<string | null>(null);
  const [exporting, setExporting] = useState(false);
  const [savingLogSettings, setSavingLogSettings] = useState(false);
  const [clearingLogs, setClearingLogs] = useState(false);
  const [copyingLogSummary, setCopyingLogSummary] = useState(false);
  const dialog = useRef<HTMLDialogElement | null>(null);
  const { closing, requestClose } = useAnimatedDialogClose(onClose);
  useDialogMotion(dialog, null);

  const loadLogStatus = useCallback(async () => {
    if (!isDesktopRuntime) return;
    const status = await diagnosticLogStatus();
    setLogStatus(status);
    setCapacityMb(String(status.capacityMb));
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    apiFetch("/api/diagnostics", { signal: controller.signal })
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json() as Promise<DiagnosticsReport>;
      })
      .then(setReport)
      .catch((reason: unknown) => {
        if (!(reason instanceof DOMException && reason.name === "AbortError")) setError("设置加载失败");
      });
    void loadLogStatus().catch(() => setError("本地诊断日志状态加载失败，监控服务未受影响"));
    return () => controller.abort();
  }, [loadLogStatus]);

  const exportReport = async () => {
    if (!window.confirm("确认导出当前脱敏诊断？")) return;
    setExporting(true);
    setError(null);
    let url: string | null = null;
    try {
      const response = await apiFetch("/api/diagnostics/export", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ confirmed: true }),
      });
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      link.download = "codex-monitor-diagnostics.json";
      link.click();
    } catch {
      setError("诊断导出失败，监控服务未受影响");
    } finally {
      if (url !== null) URL.revokeObjectURL(url);
      setExporting(false);
    }
  };

  const saveLogSettings = async (enableDebug: boolean) => {
    const nextCapacity = Number(capacityMb);
    if (!Number.isInteger(nextCapacity) || nextCapacity < 5 || nextCapacity > 500) {
      setError("诊断日志容量必须是 5 到 500 之间的整数 MB");
      return;
    }
    setSavingLogSettings(true);
    setError(null);
    try {
      const status = await updateDiagnosticLogSettings(nextCapacity, enableDebug);
      setLogStatus(status);
      setCapacityMb(String(status.capacityMb));
    } catch {
      setError("保存本地诊断日志设置失败，监控服务未受影响");
    } finally {
      setSavingLogSettings(false);
    }
  };

  const copyLogSummary = async () => {
    if (logStatus === null) return;
    setCopyingLogSummary(true);
    setError(null);
    try {
      await navigator.clipboard.writeText(JSON.stringify({
        debugEnabled: logStatus.debugEnabled,
        debugRemainingSeconds: logStatus.debugRemainingSeconds,
        capacityMb: logStatus.capacityMb,
        usedBytes: logStatus.usedBytes,
        recentEntries: logStatus.recentEntries,
        lastError: logStatus.lastError,
      }, null, 2));
    } catch {
      setError("复制脱敏诊断摘要失败");
    } finally {
      setCopyingLogSummary(false);
    }
  };

  const clearLogs = async () => {
    if (!window.confirm("确认清空本机的全部诊断日志文件和应用内近期摘要？此操作不会影响项目、会话或监控服务。")) return;
    if (!window.confirm("再次确认：将删除全部本地滚动诊断日志，是否继续？")) return;
    setClearingLogs(true);
    setError(null);
    try {
      const status = await clearDiagnosticLogs();
      setLogStatus(status);
    } catch {
      setError("清空本地诊断日志失败，监控服务未受影响");
    } finally {
      setClearingLogs(false);
    }
  };

  return (
      <dialog ref={dialog} className={`diagnostics-dialog glass${closing ? " dialog-closing" : ""}`} role="dialog" aria-labelledby="settings-title" onCancel={(event) => { event.preventDefault(); requestClose(); }} onClick={(event) => { if (event.target === event.currentTarget) requestClose(); }}>
        <header>
          <div><h2 id="settings-title">设置</h2><p>应用设置 · 日志与隐私</p></div>
          <button autoFocus className="icon-button" type="button" aria-label="关闭设置" title="关闭" onClick={requestClose}><X size={18} weight="bold" /></button>
        </header>
        {report === null && error === null && <div className="loading">正在加载设置…</div>}
        {report && <div className="diagnostics-content">
          <dl>
            <div><dt>应用版本</dt><dd>{displayVersion(report.application.version)}</dd></div>
            <div><dt>运行环境</dt><dd>{report.runtime.system} {report.runtime.release} · Python {report.runtime.python}</dd></div>
            <div><dt>项目 / 会话</dt><dd>{report.health.project_count} / {report.health.session_count}</dd></div>
            <div><dt>运行 / 卡住 / 完成</dt><dd>{report.health.status_counts.running ?? 0} / {report.health.status_counts.stuck ?? 0} / {report.health.status_counts.idle ?? 0}</dd></div>
            <div><dt>未知 / 损坏 / 超大事件</dt><dd>{report.health.unknown_event_count} / {report.health.malformed_line_count} / {report.health.oversized_line_count}</dd></div>
            <div><dt>已知会话文件</dt><dd>{report.health.known_file_count}</dd></div>
            <div><dt>最近完整校准</dt><dd>{report.health.last_reconciliation_at === null ? "尚未完成" : relativeTime(report.health.last_reconciliation_at)}</dd></div>
            <div><dt>完整校准次数</dt><dd>{report.health.reconciliation_count}</dd></div>
            <div><dt>合并重复事件</dt><dd>{report.health.coalesced_event_count}</dd></div>
            <div><dt>实时连接</dt><dd>{report.health.websocket_clients} 个客户端</dd></div>
          </dl>
          <div className="diagnostics-privacy">
            <h3>明确排除</h3>
            <p>会话 ID、项目完整路径、用户与 Agent 正文、工具输入与结果、环境变量和凭据。</p>
          </div>
        </div>}
        {isDesktopRuntime && <section className="diagnostics-log-settings" aria-label="本地诊断日志设置">
          <h3>本地诊断日志</h3>
          <p>仅本机、已脱敏、不会自动上传。不会记录会话正文、命令、项目路径、凭据或内部事件键。</p>
          {logStatus === null && error === null && <div className="loading">正在读取日志设置…</div>}
          {logStatus !== null && <>
            <div className="diagnostics-log-controls">
              <label>目录总上限（MB）<input type="number" min="5" max="500" step="1" value={capacityMb} onChange={(event) => setCapacityMb(event.target.value)} /></label>
              <button type="button" disabled={savingLogSettings} onClick={() => { void saveLogSettings(logStatus.debugEnabled); }}>保存容量</button>
              <label className="diagnostics-debug-toggle"><input type="checkbox" checked={logStatus.debugEnabled} disabled={savingLogSettings} onChange={(event) => { void saveLogSettings(event.target.checked); }} />临时开启 DEBUG（{logStatus.debugEnabled ? `${Math.ceil(logStatus.debugRemainingSeconds / 60)} 分钟后自动关闭` : "默认仅 WARN / ERROR"}）</label>
            </div>
            <dl className="diagnostics-log-summary">
              <div><dt>当前用量</dt><dd>{(logStatus.usedBytes / 1024 / 1024).toFixed(2)} MB / {logStatus.capacityMb} MB</dd></div>
              <div><dt>单文件滚动</dt><dd>5 MB</dd></div>
              <div><dt>最近事件</dt><dd>{logStatus.recentEntries.length} 条</dd></div>
            </dl>
            {logStatus.lastError !== null && <p className="diagnostics-error" role="alert">{logStatus.lastError}，监控服务未受影响。</p>}
            <ul className="diagnostics-log-entries" aria-label="最近本地诊断事件">
              {logStatus.recentEntries.length === 0 && <li>暂无本地诊断记录</li>}
              {logStatus.recentEntries.map((entry) => <li key={`${entry.timestamp}-${entry.component}-${entry.event_code}`}><time>{new Date(entry.timestamp).toLocaleString()}</time><strong>{entry.level.toUpperCase()}</strong><span>{entry.component} · {entry.event_code}</span></li>)}
            </ul>
            <div className="diagnostics-log-actions">
              <button type="button" onClick={() => { void openDiagnosticLogDirectory().catch(() => setError("打开日志目录失败")); }}>打开日志目录</button>
              <button type="button" disabled={copyingLogSummary} onClick={() => { void copyLogSummary(); }}>{copyingLogSummary ? "正在复制…" : "复制脱敏摘要"}</button>
              <button type="button" disabled={clearingLogs} onClick={() => { void clearLogs(); }}>{clearingLogs ? "正在清空…" : "清空本地诊断日志"}</button>
            </div>
          </>}
        </section>}
        {error && <p className="diagnostics-error" role="alert">{error}</p>}
        <footer>
          <button type="button" onClick={requestClose}>取消</button>
          <button type="button" disabled={report === null || exporting} onClick={() => { void exportReport(); }}>
            {exporting ? "正在导出…" : "确认并下载"}<span className="sr-only">导出诊断</span>
          </button>
        </footer>
      </dialog>
  );
}

function UpdateDialog({ recovery, initialUpdate, onUpdate, onClose, origin }: {
  recovery: RecoveryStatus;
  initialUpdate: AvailableUpdate | null;
  onUpdate: (update: AvailableUpdate | null) => void;
  onClose: () => void;
  origin: DialogOrigin | null;
}) {
  const [update, setUpdate] = useState<AvailableUpdate | null>(initialUpdate);
  const [checking, setChecking] = useState(initialUpdate === null);
  const [installing, setInstalling] = useState(false);
  const [downloadProgress, setDownloadProgress] = useState<UpdateDownloadProgress | null>(null);
  const [restartRequired, setRestartRequired] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const dialog = useRef<HTMLDialogElement | null>(null);
  const { closing, requestClose } = useAnimatedDialogClose(() => {
    onClose();
    if (restartRequired) window.location.reload();
  });
  useDialogMotion(dialog, origin);

  useEffect(() => {
    if (initialUpdate !== null) return;
    checkForUpdate()
      .then((available) => {
        setUpdate(available);
        onUpdate(available);
      })
      .catch(() => setError("检查更新失败，当前版本可继续使用"))
      .finally(() => setChecking(false));
  }, [initialUpdate, onUpdate]);

  useEffect(() => {
    let disposed = false;
    let unlisten: () => void = () => undefined;
    void listenUpdateProgress(setDownloadProgress).then((stop) => {
      if (disposed) stop();
      else unlisten = stop;
    });
    return () => {
      disposed = true;
      unlisten();
    };
  }, []);

  const checkAgain = async () => {
    setChecking(true);
    setError(null);
    try {
      const available = await checkForUpdate();
      setUpdate(available);
      onUpdate(available);
    } catch {
      setError("检查更新失败，当前版本可继续使用");
    } finally {
      setChecking(false);
    }
  };

  const install = async () => {
    setInstalling(true);
    setDownloadProgress(null);
    setError(null);
    try {
      await installVerifiedUpdate();
    } catch {
      setError("安装程序未能启动，当前版本仍保留；关闭窗口后应用将重新启动");
      setInstalling(false);
      setRestartRequired(true);
    }
  };

  const close = requestClose;

  return (
    <dialog ref={dialog} className={`update-dialog glass${closing ? " dialog-closing" : ""}`} aria-labelledby="update-title" onCancel={(event) => { event.preventDefault(); close(); }} onClick={(event) => { if (event.target === event.currentTarget) close(); }}>
      <header>
        <div><h2 id="update-title">应用更新</h2><p>稳定通道与恢复</p></div>
        <button autoFocus className="icon-button" type="button" aria-label="关闭更新" title="关闭" onClick={close}><X size={18} weight="bold" /></button>
      </header>
      <div className="update-content">
        {checking && <p>正在检查稳定版本…</p>}
        {!checking && update === null && error === null && <p>当前已是最新稳定版本。</p>}
        {update && <><h3>可更新至 {update.version}</h3><p>{update.notes || "此版本没有附加说明。"}</p></>}
        {installing && <div className="update-progress">
          <progress value={downloadProgress?.downloaded} max={downloadProgress?.total ?? undefined} />
          <span>{downloadProgress ? `已下载 ${(downloadProgress.downloaded / 1024 / 1024).toFixed(1)} MB` : "正在准备下载…"}</span>
        </div>}
        {recovery.available && <p className="recovery-note">连续 {recovery.failureCount} 次启动未完成健康检查。{recovery.recoveryUrl
          ? <a href={recovery.recoveryUrl} target="_blank" rel="noreferrer">打开上一稳定版恢复页面</a>
          : "当前没有可用的上一稳定桌面版，请保留配置并重新安装当前版本。"}</p>}
        {error && <p className="diagnostics-error" role="alert">{error}</p>}
      </div>
      <footer>
        <button type="button" onClick={close}>稍后</button>
        {error && <button type="button" onClick={() => { void checkAgain(); }}>重新检查</button>}
        {update && <button type="button" disabled={installing} onClick={() => { void install(); }}>{installing ? "正在安装…" : `安装 ${update.version}`}</button>}
      </footer>
    </dialog>
  );
}

type NoticeKind = "success" | "error" | "warning" | "progress";

interface AppNotice {
  kind: NoticeKind;
  title: string;
  message: string;
}

interface StoredNotice extends AppNotice {
  id: number;
}

interface ContextMenuAction {
  label: string;
  icon: React.ReactNode;
  onSelect: () => void;
  disabled?: boolean;
}

interface ContextMenuState {
  x: number;
  y: number;
  title: string;
  actions: ContextMenuAction[];
}

type EditableElement = HTMLInputElement | HTMLTextAreaElement;

function editableTarget(target: EventTarget | null): EditableElement | null {
  if (target instanceof HTMLTextAreaElement) return target;
  if (target instanceof HTMLInputElement && target.type !== "checkbox" && target.type !== "radio") return target;
  return null;
}

function editableSelection(target: EditableElement): { start: number; end: number } {
  return {
    start: target.selectionStart ?? 0,
    end: target.selectionEnd ?? target.value.length,
  };
}

function restoreEditableSelection(target: EditableElement, start: number, end: number): void {
  target.focus();
  try {
    target.setSelectionRange(start, end);
  } catch {
    return;
  }
}

function dispatchEditableInput(target: EditableElement): void {
  target.dispatchEvent(new Event("input", { bubbles: true }));
}

function setEditableValue(target: EditableElement, value: string): void {
  const prototype = target instanceof HTMLTextAreaElement ? HTMLTextAreaElement.prototype : HTMLInputElement.prototype;
  const setter = Object.getOwnPropertyDescriptor(prototype, "value")?.set;
  if (setter) setter.call(target, value);
  else target.value = value;
  dispatchEditableInput(target);
}

async function copyEditableSelection(target: EditableElement, start: number, end: number): Promise<boolean> {
  const selected = target.value.slice(start, end);
  if (selected.length === 0) return false;
  restoreEditableSelection(target, start, end);
  if (document.execCommand("copy")) return true;
  if (!navigator.clipboard?.writeText) return false;
  await navigator.clipboard.writeText(selected);
  return true;
}

async function pasteIntoEditable(target: EditableElement, start: number, end: number): Promise<boolean> {
  if (!navigator.clipboard?.readText) return false;
  const pasted = await navigator.clipboard.readText();
  restoreEditableSelection(target, start, end);
  setEditableValue(target, target.value.slice(0, start) + pasted + target.value.slice(end));
  const caret = start + pasted.length;
  restoreEditableSelection(target, caret, caret);
  return true;
}

function ContextMenu({ menu, onClose }: { menu: ContextMenuState | null; onClose: () => void }) {
  const menuRef = useRef<HTMLDivElement | null>(null);
  const [position, setPosition] = useState({ left: 0, top: 0 });

  useLayoutEffect(() => {
    if (menu === null) return;
    setPosition({ left: menu.x, top: menu.y });
    const element = menuRef.current;
    if (element === null) return;
    const bounds = element.getBoundingClientRect();
    setPosition({
      left: Math.min(Math.max(8, menu.x), Math.max(8, window.innerWidth - bounds.width - 8)),
      top: Math.min(Math.max(8, menu.y), Math.max(8, window.innerHeight - bounds.height - 8)),
    });
  }, [menu]);

  useEffect(() => {
    if (menu === null) return;
    const handlePointerDown = (event: PointerEvent) => {
      if (!menuRef.current?.contains(event.target as Node)) onClose();
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape") onClose();
    };
    window.addEventListener("pointerdown", handlePointerDown);
    window.addEventListener("keydown", handleKeyDown);
    window.addEventListener("scroll", onClose, true);
    return () => {
      window.removeEventListener("pointerdown", handlePointerDown);
      window.removeEventListener("keydown", handleKeyDown);
      window.removeEventListener("scroll", onClose, true);
    };
  }, [menu, onClose]);

  if (menu === null) return null;
  return (
    <div
      ref={menuRef}
      className="context-menu glass"
      role="menu"
      aria-label={menu.title}
      style={{ left: position.left, top: position.top }}
      onContextMenu={(event) => {
        event.preventDefault();
        event.stopPropagation();
      }}
    >
      <div className="context-menu-title">{menu.title}</div>
      {menu.actions.map((action, index) => (
        <button
          key={action.label}
          type="button"
          role="menuitem"
          autoFocus={index === 0}
          disabled={action.disabled}
          onClick={() => {
            onClose();
            action.onSelect();
          }}
        >
          {action.icon}
          <span>{action.label}</span>
        </button>
      ))}
    </div>
  );
}

function NoticeItem({ notice, onClose, stackIndex }: { notice: StoredNotice; onClose: (id: number) => void; stackIndex: number }) {
  const [closing, setClosing] = useState(false);
  const closingRef = useRef(false);
  const timerRef = useRef<number | null>(null);
  const visualIndex = Math.min(stackIndex, 3);

  const dismiss = useCallback(() => {
    if (closingRef.current) return;
    closingRef.current = true;
    setClosing(true);
    timerRef.current = window.setTimeout(() => onClose(notice.id), 180);
  }, [notice.id, onClose]);

  useEffect(() => {
    if (notice.kind === "progress") return;
    const timeout = window.setTimeout(dismiss, notice.kind === "error" ? 7000 : 4200);
    return () => {
      window.clearTimeout(timeout);
      if (timerRef.current !== null) window.clearTimeout(timerRef.current);
    };
  }, [dismiss, notice.kind]);

  return (
    <div
      className={`notice-toast notice-${notice.kind}${stackIndex === 0 ? " notice-front" : " notice-back"}${closing ? " notice-closing" : ""}`}
      role={notice.kind === "error" ? "alert" : "status"}
      aria-live="polite"
      aria-hidden={stackIndex > 0}
      style={{ "--notice-index": visualIndex } as React.CSSProperties}
    >
      <span className="notice-icon" aria-hidden="true">
        {notice.kind === "success" && <CheckCircle size={21} weight="fill" />}
        {notice.kind === "progress" && <SpinnerGap className="project-import-spinner" size={21} weight="bold" />}
        {notice.kind === "warning" && <Warning size={21} weight="fill" />}
        {notice.kind === "error" && <Warning size={21} weight="fill" />}
      </span>
      <span className="notice-content"><strong>{notice.title}</strong><span>{notice.message}</span></span>
      {notice.kind !== "progress" && <button type="button" className="notice-close" aria-label="关闭提示" title="关闭" onClick={dismiss}><X size={16} weight="bold" /></button>}
    </div>
  );
}

function NoticeToast({ notices, onClose }: { notices: StoredNotice[]; onClose: (id: number) => void }) {
  if (notices.length === 0) return null;
  return (
    <div className="notice-stack" aria-live="polite" style={{ "--notice-count": Math.min(notices.length, 4) } as React.CSSProperties}>
      {notices.map((notice, index) => <NoticeItem key={notice.id} notice={notice} stackIndex={index} onClose={onClose} />)}
    </div>
  );
}

export function App() {
  const { sessions, connectionState, lastSyncedAt, retryCount } = useSessions();
  const { isHandled, handle, undo, handleMany, undoMany, memoryOnly } = useAttentionRegistry();
  const { config, pickProject, removeProject, saveLayout } = useMonitorConfig();
  const [, setNow] = useState(Date.now());
  const [filter, setFilter] = useState<Filter>(() => initialChoice("filter", filters.map((item) => item.value), "all"));
  const [selectedProject, setSelectedProject] = useState(() => initialParams.get("project") || "all");
  const [query, setQuery] = useState(() => initialParams.get("q") || "");
  const [sort, setSort] = useState<Sort>(() => initialChoice("sort", ["attention", "recent", "oldest"], "attention"));
  const [timeRange, setTimeRange] = useState<TimeRange>(() => initialChoice("time", ["all", "hour", "day", "week"], "all"));
  const [selectedId, setSelectedId] = useState<string | null>(null);
  const [attentionSelection, setAttentionSelection] = useState<AttentionSelection>({ context: null, nonce: 0 });
  const [mobileDetailOpen, setMobileDetailOpen] = useState(false);
  const [projectCollapsed, setProjectCollapsed] = useState(false);
  const [sessionCollapsed, setSessionCollapsed] = useState(false);
  const [projectWidth, setProjectWidth] = useState(PROJECT_SIDEBAR_DEFAULT_WIDTH_REM);
  const [sessionWidth, setSessionWidth] = useState(SESSION_SIDEBAR_DEFAULT_WIDTH_REM);
  const [showBackToTop, setShowBackToTop] = useState(false);
  const [diagnosticsOpen, setDiagnosticsOpen] = useState(false);
  const [diagnosticsOrigin, setDiagnosticsOrigin] = useState<DialogOrigin | null>(null);
  const [updateOpen, setUpdateOpen] = useState(false);
  const [updateOrigin, setUpdateOrigin] = useState<DialogOrigin | null>(null);
  const [availableUpdate, setAvailableUpdate] = useState<AvailableUpdate | null>(null);
  const [recovery, setRecovery] = useState<RecoveryStatus>({ failureCount: 0, available: false, recoveryUrl: null });
  const autoUpdateChecked = useRef(false);
  const [importing, setImporting] = useState(false);
  const [removingProject, setRemovingProject] = useState<string | null>(null);
  const [notices, setNotices] = useState<StoredNotice[]>([]);
  const noticeIdRef = useRef(0);
  const setNotice = useCallback((nextNotice: AppNotice | null) => {
    if (nextNotice === null) {
      setNotices((current) => current.filter((notice) => notice.kind !== "progress"));
      return;
    }
    const storedNotice: StoredNotice = { ...nextNotice, id: noticeIdRef.current++ };
    setNotices((current) => [storedNotice, ...current.filter((notice) => notice.kind !== "progress")].slice(0, 5));
  }, []);
  const closeNotice = useCallback((id: number) => setNotices((current) => current.filter((notice) => notice.id !== id)), []);
  const openUpdateFromSettings = useCallback((origin: DialogOrigin) => {
    setDiagnosticsOpen(false);
    setDiagnosticsOrigin(null);
    setAvailableUpdate(null);
    setUpdateOrigin(origin);
    setUpdateOpen(true);
  }, []);
  const [contextMenu, setContextMenu] = useState<ContextMenuState | null>(null);
  const closeContextMenu = useCallback(() => setContextMenu(null), []);
  const [lastBatchEventKeys, setLastBatchEventKeys] = useState<readonly string[] | null>(null);
  const [batchUndoMessage, setBatchUndoMessage] = useState<string | null>(null);
  const [theme, setTheme] = useState<Theme>(() => {
    try {
      return (localStorage.getItem("theme") as Theme) || "light";
    } catch {
      return "light";
    }
  });
  const reducedMotion = useReducedMotion();
  const [isCompact, setIsCompact] = useState(() => window.innerWidth / rootFontSize() < COMPACT_LAYOUT_MAX_WIDTH_REM);
  const [compactPane, setCompactPane] = useState<"projects" | "sessions">("sessions");
  const [inboxQueueOnly, setInboxQueueOnly] = useState(false);
  const workbenchRef = useRef<HTMLDivElement | null>(null);
  const projectResizeRef = useRef<{ startX: number; startWidth: number } | null>(null);
  const sessionResizeRef = useRef<{ startX: number; startWidth: number } | null>(null);
  const projectRatioRef = useRef<number | null>(null);
  const sessionRatioRef = useRef<number | null>(null);
  const sessionsRef = useRef(sessions);
  const hasSyncedRef = useRef(lastSyncedAt !== null);
  const connectionStateRef = useRef(connectionState);
  const isHandledRef = useRef(isHandled);
  sessionsRef.current = sessions;
  hasSyncedRef.current = lastSyncedAt !== null;
  connectionStateRef.current = connectionState;
  isHandledRef.current = isHandled;

  const scoped = useMemo(() => sessions.filter((session) => selectedProject === "all" || session.project_key === selectedProject), [sessions, selectedProject]);
  const normalizedQuery = query.trim().toLocaleLowerCase();
  const seconds = timeRange === "hour" ? 3600 : timeRange === "day" ? 86400 : timeRange === "week" ? 604800 : null;
  const ordered = scoped.filter((session) => {
    if (!matchesFilter(session, filter, isHandled)) return false;
    if (seconds !== null && Date.now() / 1000 - session.last_event_at > seconds) return false;
    if (!normalizedQuery) return true;
    return [session.session_id, session.project_key, session.cwd, session.current_action, session.current_user_message_summary]
      .some((value) => value.toLocaleLowerCase().includes(normalizedQuery));
  }).sort((left, right) => {
    if (sort === "recent") return right.last_event_at - left.last_event_at;
    if (sort === "oldest") return left.last_event_at - right.last_event_at;
    const rank = (session: SessionSummary) => isUnhandledAttention(session, isHandled) ? 0 : isHandledAttention(session, isHandled) ? 1 : session.status === "running" ? 2 : 3;
    return rank(left) - rank(right) || right.last_event_at - left.last_event_at;
  });
  const batchEventKeys = [...new Set(ordered.flatMap((session) => {
    const context = currentAttention(session);
    return context !== null && !isHandled(context.event_key) ? [context.event_key] : [];
  }))];
  const batchTooLarge = batchEventKeys.length > MAX_ATTENTION_REGISTRY_ENTRIES;
  const hasSynced = lastSyncedAt !== null;

  useEffect(() => {
    const updateLayout = () => setIsCompact(window.innerWidth / rootFontSize() < COMPACT_LAYOUT_MAX_WIDTH_REM);
    window.addEventListener("resize", updateLayout);
    return () => window.removeEventListener("resize", updateLayout);
  }, []);

  const openAttentionInbox = useCallback((eventKey: string | null) => {
    const targets = eventKey !== null
      && EVENT_KEY_PATTERN.test(eventKey)
      && hasSyncedRef.current
      && connectionStateRef.current === "online"
      ? sessionsRef.current.filter((session) => {
        const context = currentAttention(session);
        return context?.event_key === eventKey && needsAttention(session) && !isHandledRef.current(eventKey);
      })
      : [];
    const target = targets.length === 1 ? targets[0] : null;
    setSelectedProject("all");
    setFilter("attention");
    setQuery("");
    setTimeRange("all");
    setSort("attention");
    setProjectCollapsed(false);
    setSessionCollapsed(false);
    setMobileDetailOpen(isCompact && target !== null);
    setCompactPane("sessions");
    setInboxQueueOnly(isCompact && target === null);
    setSelectedId(target?.session_key ?? null);
    setAttentionSelection((selection) => ({
      context: target === null ? null : currentAttention(target),
      nonce: selection.nonce + 1,
    }));
  }, [isCompact]);

  const notifications = useAttentionNotifications(sessions, lastSyncedAt !== null && connectionState === "online", isHandled, openAttentionInbox);

  const handleNotificationToggle = async () => {
    const result = await notifications.toggle();
    if (result === "enabled") {
      setNotice({ kind: "success", title: "桌面提醒已开启", message: "新的关注事件会在窗口未聚焦时提醒你" });
    } else if (result === "disabled") {
      setNotice({ kind: "success", title: "桌面提醒已关闭", message: "你仍可随时从顶部铃铛重新开启" });
    } else if (result === "denied") {
      setNotice({ kind: "warning", title: "桌面提醒权限未允许", message: "请在系统通知设置中允许后再试" });
    } else if (result === "unsupported") {
      setNotice({ kind: "warning", title: "当前环境不支持桌面提醒", message: "请使用桌面端或支持通知权限的浏览器" });
    } else {
      setNotice({ kind: "error", title: "桌面提醒设置失败", message: "请稍后重试，监控功能不受影响" });
    }
  };

  const handleThemeChange = (nextTheme: Theme) => {
    setTheme(nextTheme);
    setNotice({
      kind: "success",
      title: "主题已切换",
      message: nextTheme === "dark" ? "已切换为深色主题" : "已切换为浅色主题",
    });
  };

  useEffect(() => {
    let active = true;
    let unlisten: () => void = () => undefined;
    void listenAttentionInbox((activation) => openAttentionInbox(activation.eventKey)).then((handler) => {
      if (active) unlisten = handler;
      else handler();
    });
    return () => {
      active = false;
      unlisten();
    };
  }, [openAttentionInbox]);

  useEffect(() => {
    if (selectedId === null && inboxQueueOnly && isCompact) return;
    if (selectedId !== null && ordered.some((session) => session.session_key === selectedId)) return;
    const next = ordered[0] ?? null;
    if (selectedId === null && next === null) return;
    setSelectedId(next?.session_key ?? null);
    setAttentionSelection((selection) => ({ context: next === null ? null : currentAttention(next), nonce: selection.nonce + 1 }));
    setMobileDetailOpen(false);
  }, [inboxQueueOnly, isCompact, ordered, selectedId]);

  useEffect(() => {
    const params = new URLSearchParams();
    if (selectedProject !== "all") params.set("project", selectedProject);
    if (filter !== "all") params.set("filter", filter);
    if (sort !== "attention") params.set("sort", sort);
    if (timeRange !== "all") params.set("time", timeRange);
    if (query.trim()) params.set("q", query.trim());
    const search = params.toString();
    window.history.replaceState(null, "", `${window.location.pathname}${search ? `?${search}` : ""}`);
  }, [filter, query, selectedProject, sort, timeRange]);

  useEffect(() => {
    if (config && selectedProject !== "all" && !config.projects.some((project) => project.project_key === selectedProject)) {
      setSelectedProject("all");
    }
  }, [config, selectedProject]);

  useEffect(() => {
    document.documentElement.dataset.theme = theme;
    try {
      localStorage.setItem("theme", theme);
    } catch {
      // The attention registry owns the visible storage fallback warning.
    }
  }, [theme]);

  useLayoutEffect(() => {
    const workbench = workbenchRef.current;
    if (workbench === null || isCompact) return;

    projectRatioRef.current = config?.layout?.project_sidebar_ratio ?? null;
    sessionRatioRef.current = config?.layout?.session_sidebar_ratio ?? null;

    let previousWidth = 0;
    const restoreWidths = () => {
      const availableWidth = workbench.clientWidth / rootFontSize();
      if (availableWidth <= 0 || availableWidth === previousWidth) return;
      previousWidth = availableWidth;

      if (projectRatioRef.current === null) {
        projectRatioRef.current = projectWidth / availableWidth;
      } else {
        setProjectWidth(Math.min(
          PROJECT_SIDEBAR_MAX_WIDTH_REM,
          Math.max(PROJECT_SIDEBAR_MIN_WIDTH_REM, availableWidth * projectRatioRef.current),
        ));
      }

      if (sessionRatioRef.current === null) {
        sessionRatioRef.current = sessionWidth / availableWidth;
      } else {
        setSessionWidth(Math.min(
          SESSION_SIDEBAR_MAX_WIDTH_REM,
          Math.max(SESSION_SIDEBAR_MIN_WIDTH_REM, availableWidth * sessionRatioRef.current),
        ));
      }

    };

    restoreWidths();
    const observer = new ResizeObserver(restoreWidths);
    observer.observe(workbench);
    return () => observer.disconnect();
  }, [config?.layout, isCompact]);

  const rememberSidebarRatio = (ratioRef: React.MutableRefObject<number | null>, width: number) => {
    const availableWidth = (workbenchRef.current?.clientWidth ?? 0) / rootFontSize();
    if (availableWidth <= 0) return;
    ratioRef.current = width / availableWidth;
  };

  const persistSidebarRatios = () => {
    const projectRatio = projectRatioRef.current;
    const sessionRatio = sessionRatioRef.current;
    if (projectRatio === null || sessionRatio === null) return;
    void saveLayout({
      project_sidebar_ratio: projectRatio,
      session_sidebar_ratio: sessionRatio,
    }).catch(() => undefined);
  };

  useEffect(() => {
    const move = (event: PointerEvent) => {
      const projectResize = projectResizeRef.current;
      if (projectResize !== null) {
        const nextWidth = projectResize.startWidth + (event.clientX - projectResize.startX) / rootFontSize();
        if (nextWidth < PROJECT_SIDEBAR_MIN_WIDTH_REM) {
          setProjectCollapsed(true);
          projectResizeRef.current = null;
          persistSidebarRatios();
        } else {
          const width = Math.min(PROJECT_SIDEBAR_MAX_WIDTH_REM, nextWidth);
          setProjectWidth(width);
          rememberSidebarRatio(projectRatioRef, width);
        }
      }

      const sessionResize = sessionResizeRef.current;
      if (sessionResize !== null) {
        const nextWidth = sessionResize.startWidth + (event.clientX - sessionResize.startX) / rootFontSize();
        if (nextWidth < SESSION_SIDEBAR_MIN_WIDTH_REM) {
          setSessionCollapsed(true);
          sessionResizeRef.current = null;
          persistSidebarRatios();
        } else {
          const width = Math.min(SESSION_SIDEBAR_MAX_WIDTH_REM, nextWidth);
          setSessionWidth(width);
          rememberSidebarRatio(sessionRatioRef, width);
        }
      }
    };
    const end = () => {
      const resized = projectResizeRef.current !== null || sessionResizeRef.current !== null;
      projectResizeRef.current = null;
      sessionResizeRef.current = null;
      if (resized) persistSidebarRatios();
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", end);
    window.addEventListener("pointercancel", end);
    window.addEventListener("blur", end);
    return () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", end);
      window.removeEventListener("pointercancel", end);
      window.removeEventListener("blur", end);
    };
  }, []);

  const startProjectResize = (event: React.PointerEvent<HTMLDivElement>) => {
    if (isCompact || projectCollapsed) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    projectResizeRef.current = { startX: event.clientX, startWidth: projectWidth };
  };

  const nudgeProjectWidth = (delta: number) => {
    if (projectCollapsed) return;
    const nextWidth = Math.min(
      PROJECT_SIDEBAR_MAX_WIDTH_REM,
      Math.max(PROJECT_SIDEBAR_MIN_WIDTH_REM, projectWidth + delta),
    );
    setProjectWidth(nextWidth);
    rememberSidebarRatio(projectRatioRef, nextWidth);
    window.queueMicrotask(persistSidebarRatios);
  };

  const startSessionResize = (event: React.PointerEvent<HTMLDivElement>) => {
    if (isCompact || sessionCollapsed) return;
    event.preventDefault();
    event.currentTarget.setPointerCapture(event.pointerId);
    sessionResizeRef.current = { startX: event.clientX, startWidth: sessionWidth };
  };

  const nudgeSessionWidth = (delta: number) => {
    if (sessionCollapsed) return;
    const nextWidth = Math.min(
      SESSION_SIDEBAR_MAX_WIDTH_REM,
      Math.max(SESSION_SIDEBAR_MIN_WIDTH_REM, sessionWidth + delta),
    );
    setSessionWidth(nextWidth);
    rememberSidebarRatio(sessionRatioRef, nextWidth);
    window.queueMicrotask(persistSidebarRatios);
  };

  useEffect(() => {
    const timer = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(timer);
  }, []);

  useEffect(() => {
    const blockBrowserShortcuts = (event: KeyboardEvent) => {
      const opensNativeMenu = event.key === "ContextMenu" || (event.shiftKey && event.key === "F10");
      if (!opensNativeMenu) return;
      event.preventDefault();
      event.stopPropagation();
      closeContextMenu();
    };
    window.addEventListener("keydown", blockBrowserShortcuts, true);
    return () => window.removeEventListener("keydown", blockBrowserShortcuts, true);
  }, [closeContextMenu]);

  useEffect(() => {
    if (!isDesktopRuntime) return;
    void recoveryStatus().then((status) => {
      setRecovery(status);
      if (status.available) setUpdateOpen(true);
    });
  }, []);

  useEffect(() => {
    if (!isDesktopRuntime || connectionState !== "online" || lastSyncedAt === null) return;
    void markDesktopHealthy().then(() => setRecovery({ failureCount: 0, available: false, recoveryUrl: null }));
  }, [connectionState, lastSyncedAt]);

  useEffect(() => {
    if (!isDesktopRuntime || connectionState !== "online" || !hasSynced || autoUpdateChecked.current) return;
    const timer = window.setTimeout(() => {
      autoUpdateChecked.current = true;
      void checkForUpdate()
        .then((update) => {
          setAvailableUpdate(update);
          if (update !== null && localStorage.getItem(LAST_DISMISSED_UPDATE_KEY) !== update.version) {
            setUpdateOpen(true);
          }
        })
        .catch(() => undefined);
    }, AUTO_UPDATE_CHECK_DELAY_MS);
    return () => window.clearTimeout(timer);
  }, [connectionState, hasSynced]);

  useEffect(() => {
    const timeline = document.querySelector<HTMLElement>(".timeline");
    const update = () => setShowBackToTop(window.scrollY > window.innerHeight / 2 || (timeline?.scrollTop ?? 0) > 200);
    update();
    window.addEventListener("scroll", update, { passive: true });
    timeline?.addEventListener("scroll", update, { passive: true });
    return () => {
      window.removeEventListener("scroll", update);
      timeline?.removeEventListener("scroll", update);
    };
  }, [selectedId]);

  const selected = sessions.find((session) => session.session_key === selectedId) ?? null;
  const selectedCurrentContext = selected === null ? null : currentAttention(selected);
  const selectedAttentionHandled = attentionSelection.context !== null && isHandled(attentionSelection.context.event_key);
  const {
    detail,
    state: detailState,
    anchorState,
    historicalWindow,
    loadEarlier,
    resetLatest,
    retry: retryDetail,
  } = useSessionDetail(
    selectedId,
    selected?.last_event_at ?? null,
    attentionSelection.context?.turn_id ?? null,
    attentionSelection.nonce,
  );
  const activeProject = config?.projects.find((project) => project.project_key === selectedProject);
  const projectPath = selectedProject === "all" ? "全部项目" : activeProject?.project_root ?? "正在读取项目配置";
  const handleImport = async () => {
    setImporting(true);
    setNotice({ kind: "progress", title: "正在导入项目", message: "正在处理所选文件夹，请稍候…" });
    try {
      const result = await pickProject();
      if (result.projects.length === 0) {
        setNotice(null);
        return;
      }
      const project = result.projects.at(-1);
      if (project === undefined) {
        setNotice(null);
        return;
      }
      setSelectedProject(project.project_key);
      setSelectedId(null);
      setMobileDetailOpen(false);
      if (result.errors.length > 0) {
        setNotice({ kind: "warning", title: "项目导入部分完成", message: `已导入 ${result.projects.length} 个项目，${result.errors.length} 个项目失败。` });
      } else {
        setNotice({ kind: "success", title: "项目导入成功", message: `已导入 ${result.projects.length} 个项目。` });
      }
    } catch (reason) {
      setNotice({ kind: "error", title: "项目导入失败", message: reason instanceof Error ? reason.message : "导入失败" });
    } finally {
      setImporting(false);
    }
  };
  const handleRemove = async (project: Project) => {
    if (removingProject !== null) return;
    if (!window.confirm(`确认从监控中移除 ${project.project_name}？不会删除磁盘文件。`)) return;
    setRemovingProject(project.project_key);
    setNotice({ kind: "progress", title: "正在移除项目", message: `正在处理 ${project.project_name}，请稍候…` });
    try {
      await removeProject(project.project_key);
      if (selectedProject === project.project_key) setSelectedProject("all");
      setSelectedId(null);
      setMobileDetailOpen(false);
      setNotice({ kind: "success", title: "项目已移除", message: `${project.project_name} 已从监控列表移除，磁盘文件未被删除。` });
    } catch (reason) {
      setNotice({ kind: "error", title: "移除项目失败", message: reason instanceof Error ? reason.message : "移除项目失败" });
    } finally {
      setRemovingProject(null);
    }
  };
  const copyContextValue = async (label: string, value: string) => {
    try {
      await navigator.clipboard.writeText(value);
      setNotice({ kind: "success", title: "已复制", message: label });
    } catch {
      setNotice({ kind: "error", title: "复制失败", message: `无法复制${label}，请检查剪贴板权限。` });
    }
  };
  const showProjectContextMenu = (event: React.MouseEvent, project: Project) => {
    event.preventDefault();
    event.stopPropagation();
    const actions: ContextMenuAction[] = [
      { label: "复制项目路径", icon: <Copy size={16} />, onSelect: () => { void copyContextValue("项目路径", project.project_root); } },
      { label: "复制项目名称", icon: <Copy size={16} />, onSelect: () => { void copyContextValue("项目名称", project.project_name); } },
    ];
    if (isDesktopRuntime) {
      actions.push({
        label: "打开项目目录",
        icon: <FolderSimple size={16} />,
        disabled: project.status === "missing",
        onSelect: () => {
          void openProjectDirectory(project.project_root)
            .then(() => setNotice({ kind: "success", title: "已打开项目目录", message: project.project_name }))
            .catch((reason: unknown) => setNotice({ kind: "error", title: "打开项目目录失败", message: reason instanceof Error ? reason.message : "无法打开项目目录" }));
        },
      });
    }
    setContextMenu({ x: event.clientX, y: event.clientY, title: "项目操作", actions });
  };
  const showSessionContextMenu = (event: React.MouseEvent, session: SessionSummary) => {
    event.preventDefault();
    event.stopPropagation();
    const actions: ContextMenuAction[] = [
      { label: "复制会话 ID", icon: <Copy size={16} />, onSelect: () => { void copyContextValue("会话 ID", session.session_id); } },
    ];
    if (/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(session.session_id)) {
      actions.push({ label: "复制 codex resume", icon: <Copy size={16} />, onSelect: () => { void copyContextValue("codex resume 命令", `codex resume ${session.session_id}`); } });
    }
    setContextMenu({ x: event.clientX, y: event.clientY, title: "会话操作", actions });
  };
  const showEditableContextMenu = (event: React.MouseEvent<HTMLDivElement>, target: EditableElement) => {
    const { start, end } = editableSelection(target);
    const selectedLength = Math.max(0, end - start);
    const editable = !target.readOnly && !target.disabled;
    const actions: ContextMenuAction[] = [
      {
        label: "复制",
        icon: <Copy size={16} />,
        disabled: selectedLength === 0,
        onSelect: () => {
          void copyEditableSelection(target, start, end).then((copied) => {
            setNotice(copied
              ? { kind: "success", title: "已复制", message: "已复制选中的文本。" }
              : { kind: "warning", title: "没有可复制内容", message: "请先选择文本。" });
          }).catch(() => setNotice({ kind: "error", title: "复制失败", message: "无法访问剪贴板。" }));
        },
      },
      {
        label: "剪切",
        icon: <Copy size={16} />,
        disabled: !editable || selectedLength === 0,
        onSelect: () => {
          void copyEditableSelection(target, start, end).then((copied) => {
            if (!copied) {
              setNotice({ kind: "error", title: "剪切失败", message: "无法访问剪贴板。" });
              return;
            }
            setEditableValue(target, target.value.slice(0, start) + target.value.slice(end));
            restoreEditableSelection(target, start, start);
            setNotice({ kind: "success", title: "已剪切", message: "已剪切选中的文本。" });
          }).catch(() => setNotice({ kind: "error", title: "剪切失败", message: "无法访问剪贴板。" }));
        },
      },
      {
        label: "粘贴",
        icon: <Copy size={16} />,
        disabled: !editable,
        onSelect: () => {
          void pasteIntoEditable(target, start, end).then((pasted) => {
            setNotice(pasted
              ? { kind: "success", title: "已粘贴", message: "剪贴板内容已插入输入框。" }
              : { kind: "error", title: "粘贴失败", message: "无法访问剪贴板。" });
          }).catch(() => setNotice({ kind: "error", title: "粘贴失败", message: "无法访问剪贴板。" }));
        },
      },
      {
        label: "全选",
        icon: <Copy size={16} />,
        disabled: target.value.length === 0,
        onSelect: () => {
          restoreEditableSelection(target, 0, target.value.length);
        },
      },
    ];
    setContextMenu({ x: event.clientX, y: event.clientY, title: "编辑操作", actions });
  };
  const selectSession = (session: SessionSummary) => {
    setInboxQueueOnly(false);
    if (selectedId === session.session_key) resetLatest();
    setSelectedId(session.session_key);
    const context = currentAttention(session);
    setAttentionSelection((selection) => ({ context: context === null ? null : { ...context }, nonce: selection.nonce + 1 }));
    setMobileDetailOpen(true);
    window.requestAnimationFrame(() => {
      const workspace = document.querySelector<HTMLElement>(".workspace");
      if (workspace && workspace.getBoundingClientRect().top >= window.innerHeight) workspace.scrollIntoView();
    });
  };
  const handleBatch = () => {
    const eventKeys = [...batchEventKeys];
    if (eventKeys.length === 0 || eventKeys.length > MAX_ATTENTION_REGISTRY_ENTRIES) return;
    if (!window.confirm(`确认将当前筛选中的 ${eventKeys.length} 个关注事件标记为已处理？`)) return;
    if (!handleMany(eventKeys)) return;
    setLastBatchEventKeys([...eventKeys]);
    setBatchUndoMessage(null);
  };
  const handleBatchUndo = () => {
    if (lastBatchEventKeys === null || !undoMany(lastBatchEventKeys)) return;
    const undoneCount = lastBatchEventKeys.length;
    setLastBatchEventKeys(null);
    setBatchUndoMessage(`已撤销 ${undoneCount} 个关注事件`);
  };
  const clearBatchUndoForEvent = (eventKey: string) => {
    setBatchUndoMessage(null);
    setLastBatchEventKeys((eventKeys) => eventKeys?.includes(eventKey) ? null : eventKeys);
  };
  const handleAttention = (eventKey: string) => {
    clearBatchUndoForEvent(eventKey);
    handle(eventKey);
  };
  const undoAttention = (eventKey: string) => {
    clearBatchUndoForEvent(eventKey);
    undo(eventKey);
  };

  const suppressNativeContextMenu = (event: React.MouseEvent<HTMLDivElement>) => {
    const target = editableTarget(event.target);
    if (target !== null) {
      event.preventDefault();
      event.stopPropagation();
      showEditableContextMenu(event, target);
      return;
    }
    if (!isDesktopRuntime) {
      closeContextMenu();
      return;
    }
    event.preventDefault();
    event.stopPropagation();
    closeContextMenu();
  };

  return (
    <div className={`app-shell ${isCompact ? "compact-shell" : ""} ${reducedMotion ? "reduced-motion" : ""}`} onContextMenu={suppressNativeContextMenu}>
      <header className="topbar glass">
        {!isCompact && <button
          className="icon-button sidebar-toggle"
          type="button"
          aria-label={projectCollapsed ? "展开项目列" : "折叠项目列"}
          title={projectCollapsed ? "展开项目列" : "折叠项目列"}
          aria-pressed={projectCollapsed}
          onClick={() => setProjectCollapsed((collapsed) => !collapsed)}
        ><SidebarSimple size={18} weight="bold" /></button>}
        <div className="brand">Codex Session Monitor</div>
        <div className="project"><FolderSimple size={20} /><span><small>{selectedProject === "all" ? "监控范围" : "当前项目"}</small><strong>{projectPath}</strong></span></div>
        <div className="top-actions">
          <span className={`connection ${connectionState === "online" ? "online" : "offline"}`}>
            <i />{connectionState === "online" ? "实时连接" : connectionState === "stale" ? "数据可能过期" : connectionState === "connecting" ? "正在连接" : connectionState === "startup_failed" ? "桌面服务启动失败" : "正在重连"}
            {retryCount > 0 && ` · ${retryCount} 次`}
          </span>
          <span className="last-sync">最后同步：{lastSyncedAt === null ? "尚未同步" : relativeTime(lastSyncedAt)}</span>
          <span className="readonly">本地 · 只读</span>
          <button
            className="icon-button notification-toggle"
            type="button"
            aria-label={notifications.enabled ? "关闭桌面提醒" : "开启桌面提醒"}
            title={!notifications.supported ? "浏览器不支持桌面提醒" : notifications.permission === "denied" ? "桌面提醒权限已拒绝" : notifications.enabled ? "关闭桌面提醒" : "开启桌面提醒"}
            onClick={() => { void handleNotificationToggle(); }}
          >
            {notifications.enabled ? <Bell size={18} weight="bold" /> : <BellSlash size={18} weight="bold" />}
          </button>
          <button className="icon-button" type="button" aria-label="设置" title="设置" onClick={(event) => { setDiagnosticsOrigin(dialogOriginFromEvent(event)); setDiagnosticsOpen(true); }}><Gear size={18} weight="bold" /></button>
          <ThemeToggle theme={theme} onChange={handleThemeChange} />
        </div>
      </header>
      {(!isCompact || !mobileDetailOpen) && <AttentionBar sessions={scoped} filter={filter} isHandled={isHandled} onFilter={setFilter} />}
      {(!isCompact || compactPane === "sessions") && (!isCompact || !mobileDetailOpen) && <LocatorControls
        query={query}
        timeRange={timeRange}
        sort={sort}
        batchCount={batchEventKeys.length}
        batchTooLarge={batchTooLarge}
        lastBatchCount={lastBatchEventKeys?.length ?? 0}
        batchUndoMessage={batchUndoMessage}
        memoryOnly={memoryOnly}
        onQuery={setQuery}
        onTimeRange={setTimeRange}
        onSort={setSort}
        onClear={() => { setQuery(""); setFilter("all"); setTimeRange("all"); setSort("attention"); }}
        onBatch={handleBatch}
        onBatchUndo={handleBatchUndo}
      />}
      {isCompact && !mobileDetailOpen && <nav className="compact-tabs glass" aria-label="移动端导航">
        <button type="button" className={compactPane === "projects" ? "active" : ""} onClick={() => setCompactPane("projects")}>查看项目</button>
        <button type="button" className={compactPane === "sessions" ? "active" : ""} onClick={() => setCompactPane("sessions")}>查看会话</button>
      </nav>}
      <div
        ref={workbenchRef}
        className={`workbench ${isCompact ? "compact-layout" : ""} ${mobileDetailOpen ? "mobile-detail-open" : ""} ${projectCollapsed ? "project-collapsed" : ""} ${sessionCollapsed ? "session-collapsed" : ""}`}
        style={{
          "--project-width": `${projectWidth}rem`,
          "--session-width": `${sessionWidth}rem`,
        } as React.CSSProperties}
      >
        {(!isCompact && !projectCollapsed || (isCompact && !mobileDetailOpen && compactPane === "projects")) && <ProjectNavigation
          projects={config?.projects ?? []}
          sessions={sessions}
          selected={selectedProject}
          importing={importing}
          removing={removingProject}
          isHandled={isHandled}
          onSelect={(projectKey) => { setInboxQueueOnly(false); setSelectedProject(projectKey); setSelectedId(null); setMobileDetailOpen(false); setCompactPane("sessions"); }}
          onImport={() => { void handleImport(); }}
          onRemove={(project) => { void handleRemove(project); }}
          onContextMenu={showProjectContextMenu}
        />}
        {!isCompact && !projectCollapsed && <div
          className="project-resizer"
          role="separator"
          aria-label="调整项目列宽度"
          aria-orientation="vertical"
          aria-valuemin={PROJECT_SIDEBAR_MIN_WIDTH_REM}
          aria-valuemax={PROJECT_SIDEBAR_MAX_WIDTH_REM}
          aria-valuenow={Math.round(projectWidth)}
          tabIndex={0}
          onPointerDown={startProjectResize}
          onKeyDown={(event) => {
            if (event.key === "ArrowLeft") { event.preventDefault(); nudgeProjectWidth(-1); }
            if (event.key === "ArrowRight") { event.preventDefault(); nudgeProjectWidth(1); }
          }}
        />}
        {(!isCompact && !sessionCollapsed || (isCompact && !mobileDetailOpen && compactPane === "sessions")) && <SessionQueue
          sessions={ordered}
          selectedId={selectedId}
          showProject={selectedProject === "all"}
          isHandled={isHandled}
          onSelect={selectSession}
          onToggle={() => setSessionCollapsed((collapsed) => !collapsed)}
          onContextMenu={showSessionContextMenu}
        />}
        {!isCompact && !sessionCollapsed && <div
          className="session-resizer"
          role="separator"
          aria-label="调整会话列宽度"
          aria-orientation="vertical"
          aria-valuemin={SESSION_SIDEBAR_MIN_WIDTH_REM}
          aria-valuemax={SESSION_SIDEBAR_MAX_WIDTH_REM}
          aria-valuenow={Math.round(sessionWidth)}
          tabIndex={0}
          onPointerDown={startSessionResize}
          onKeyDown={(event) => {
            if (event.key === "ArrowLeft") { event.preventDefault(); nudgeSessionWidth(-1); }
            if (event.key === "ArrowRight") { event.preventDefault(); nudgeSessionWidth(1); }
          }}
        />}
        {(!isCompact || mobileDetailOpen) && <Workspace
          session={selected}
          detail={detail}
          detailState={detailState}
          anchorState={anchorState}
          attentionSelection={attentionSelection}
          currentContext={selectedCurrentContext}
          attentionHandled={selectedAttentionHandled}
          memoryOnly={memoryOnly}
          historicalWindow={historicalWindow}
          mobileOpen={isCompact && mobileDetailOpen}
          sessionQueueCollapsed={!isCompact && sessionCollapsed}
          onOpenSessionQueue={() => setSessionCollapsed(false)}
          onHandle={() => { if (attentionSelection.context !== null) handleAttention(attentionSelection.context.event_key); }}
          onUndo={() => { if (attentionSelection.context !== null) undoAttention(attentionSelection.context.event_key); }}
          onBack={() => {
            setMobileDetailOpen(false);
            window.requestAnimationFrame(() => document.querySelector<HTMLElement>(".queue")?.scrollIntoView());
          }}
          onLoadEarlier={loadEarlier}
          onResetLatest={resetLatest}
          onRetry={retryDetail}
        />}
      </div>
      {showBackToTop && <button className="back-to-top" type="button" aria-label="回到顶部" title="回到顶部" onClick={() => {
        window.scrollTo({ top: 0 });
        document.querySelector<HTMLElement>(".timeline")?.scrollTo({ top: 0 });
      }}>
        <ArrowUp size={20} weight="bold" />
      </button>}
      {diagnosticsOpen && <DiagnosticsDialog notifications={notifications} origin={diagnosticsOrigin} notices={notices} onCloseNotice={closeNotice} onCheckForUpdate={openUpdateFromSettings} onNotice={(nextNotice) => setNotice(nextNotice)} onClose={() => { setDiagnosticsOpen(false); setDiagnosticsOrigin(null); }} />}
      {updateOpen && <UpdateDialog
        recovery={recovery}
        initialUpdate={availableUpdate}
        origin={updateOrigin}
        onUpdate={setAvailableUpdate}
        onClose={() => {
          if (availableUpdate !== null) localStorage.setItem(LAST_DISMISSED_UPDATE_KEY, availableUpdate.version);
          setUpdateOpen(false);
          setUpdateOrigin(null);
        }}
      />}
      <ContextMenu menu={contextMenu} onClose={closeContextMenu} />
      {!diagnosticsOpen && !updateOpen && <NoticeToast notices={notices} onClose={closeNotice} />}
    </div>
  );
}
