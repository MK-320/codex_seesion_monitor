import { useCallback, useEffect, useMemo, useRef, useState } from "react";

import {
  apiFetch,
  DesktopStartupError,
  isDesktopRuntime,
  monitorWebSocket,
  pickProjectDirectories,
} from "./desktop";
import type { LayoutPreferences, MonitorConfig, Project, RealtimeEvent, SessionDetail, SessionSummary } from "./types";

export type ConnectionState = "connecting" | "online" | "reconnecting" | "stale" | "startup_failed";
export type DetailState = "loading" | "error" | "not_found" | "ready";
export type AnchorState = "idle" | "locating" | "located" | "fallback" | "error";
export const MAX_DETAIL_TURNS = 60;

function stalePresentation(session: SessionSummary): SessionSummary {
  if (session.status !== "running") return session;
  const attentionReasons = session.attention_reasons.filter((reason) => reason === "tool_error" || reason === "turn_aborted");
  const context = session.attention_context;
  const attentionContext = context !== null && context !== undefined
    && context.reason !== "stuck"
    && context.reason !== "long_running_tool"
    && context.reason !== "no_progress"
    ? context
    : null;
  return {
    ...session,
    activity_state: "data_stale",
    attention_reasons: attentionReasons,
    attention_context: attentionContext,
  };
}

export interface ProjectImportResult {
  projects: Project[];
  errors: string[];
}

export function useMonitorConfig() {
  const [config, setConfig] = useState<MonitorConfig | null>(null);

  const refresh = useCallback(() => {
    return apiFetch("/api/config")
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json() as Promise<MonitorConfig>;
      })
      .then(setConfig)
      .catch(() => setConfig(null));
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const pickProject = useCallback(async (): Promise<ProjectImportResult> => {
    if (isDesktopRuntime) {
      const selectedRoots = await pickProjectDirectories();
      if (selectedRoots.length === 0) return { projects: [], errors: [] };
      if (selectedRoots.length === 1) {
        const selected = selectedRoots[0];
        const response = await apiFetch("/api/projects", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ project_root: selected, persist: true }),
        });
        const body = await response.json() as Project | { detail?: string };
        if (!response.ok) throw new Error("detail" in body ? body.detail || `HTTP ${response.status}` : `HTTP ${response.status}`);
        const project = body as Project;
        setConfig((current) => ({
          activity_alert_seconds: current?.activity_alert_seconds ?? 300,
          layout: current?.layout ?? null,
          projects: [...(current?.projects ?? []).filter((item) => item.project_key !== project.project_key), project],
        }));
        return { projects: [project], errors: [] };
      }
      const response = await apiFetch("/api/projects/batch", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ project_roots: selectedRoots, persist: true }),
      });
      if (response.status === 405) {
        const projects: Project[] = [];
        const errors: string[] = [];
        for (const selectedRoot of selectedRoots) {
          try {
            const fallbackResponse = await apiFetch("/api/projects", {
              method: "POST",
              headers: { "Content-Type": "application/json" },
              body: JSON.stringify({ project_root: selectedRoot, persist: true }),
            });
            const fallbackBody = await fallbackResponse.json() as Project | { detail?: string };
            if (!fallbackResponse.ok) throw new Error("detail" in fallbackBody ? fallbackBody.detail || `HTTP ${fallbackResponse.status}` : `HTTP ${fallbackResponse.status}`);
            projects.push(fallbackBody as Project);
          } catch (error: unknown) {
            errors.push(`${selectedRoot}: ${error instanceof Error ? error.message : "导入失败"}`);
          }
        }
        if (projects.length === 0) throw new Error(errors.join("；") || "导入失败");
        setConfig((current) => ({
          activity_alert_seconds: current?.activity_alert_seconds ?? 300,
          layout: current?.layout ?? null,
          projects: [...(current?.projects ?? []).filter((item) => !projects.some((project) => project.project_key === item.project_key)), ...projects],
        }));
        return { projects, errors };
      }
      const body = await response.json() as {
        projects?: Project[];
        errors?: Array<{ project_root?: string; detail?: string }>;
        detail?: string;
      };
      if (!response.ok) throw new Error(body.detail || `HTTP ${response.status}`);
      const projects = body.projects ?? [];
      const errors = (body.errors ?? []).map((error) =>
        `${error.project_root ?? "项目"}: ${error.detail ?? "导入失败"}`,
      );

      if (projects.length === 0) {
        throw new Error(errors.join("；") || "导入失败");
      }
      setConfig((current) => ({
        activity_alert_seconds: current?.activity_alert_seconds ?? 300,
        layout: current?.layout ?? null,
        projects: [...(current?.projects ?? []).filter((item) => !projects.some((project) => project.project_key === item.project_key)), ...projects],
      }));
      return { projects, errors };
    }
    const response = await apiFetch("/api/projects/pick", { method: "POST" });
    if (response.status === 204) return { projects: [], errors: [] };
    const body = await response.json() as Project | { detail?: string };
    if (!response.ok) throw new Error("detail" in body ? body.detail || `HTTP ${response.status}` : `HTTP ${response.status}`);
    const project = body as Project;
    setConfig((current) => ({
      activity_alert_seconds: current?.activity_alert_seconds ?? 300,
      layout: current?.layout ?? null,
      projects: [...(current?.projects ?? []).filter((item) => item.project_key !== project.project_key), project],
    }));
    return { projects: [project], errors: [] };
  }, []);

  const removeProject = useCallback(async (projectKey: string): Promise<void> => {
    const response = await apiFetch(`/api/projects/${encodeURIComponent(projectKey)}`, {
      method: "DELETE",
    });
    if (!response.ok && response.status !== 404) throw new Error(`HTTP ${response.status}`);
    setConfig((current) => current === null ? null : {
      ...current,
      projects: current.projects.filter((project) => project.project_key !== projectKey),
    });
  }, []);

  const saveLayout = useCallback(async (layout: LayoutPreferences): Promise<void> => {
    const response = await apiFetch("/api/config/layout", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(layout),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    setConfig((current) => current === null ? null : { ...current, layout });
  }, []);

  return { config, pickProject, removeProject, saveLayout };
}

export function useSessions() {
  const [sessions, setSessions] = useState<Map<string, SessionSummary>>(new Map());
  const [connectionState, setConnectionState] = useState<ConnectionState>("connecting");
  const [lastSyncedAt, setLastSyncedAt] = useState<number | null>(null);
  const [retryCount, setRetryCount] = useState(0);

  useEffect(() => {
    let active = true;
    let socket: WebSocket | null = null;
    let retry = 0;
    let hasSynced = false;
    let timer: number | undefined;

    const apply = (event: RealtimeEvent) => {
      hasSynced = true;
      setLastSyncedAt(event.generated_at);
      setConnectionState("online");
      setSessions((current) => {
        if (event.event === "snapshot") {
          return new Map(event.data.map((session) => [session.session_key, session]));
        }
        const next = new Map(current);
        next.set(event.session_key, event.data);
        return next;
      });
    };

    const scheduleReconnect = () => {
      setConnectionState(hasSynced ? "stale" : "reconnecting");
      const delays = [1000, 2000, 5000, 10000];
      retry += 1;
      setRetryCount(retry);
      timer = window.setTimeout(() => { void connect(); }, delays[Math.min(retry - 1, delays.length - 1)]);
    };

    const connect = async () => {
      if (!active) return;
      try {
        socket = await monitorWebSocket();
      } catch (error) {
        if (!active) return;
        if (error instanceof DesktopStartupError) {
          setConnectionState("startup_failed");
          return;
        }
        scheduleReconnect();
        return;
      }
      if (!active) {
        socket.close();
        return;
      }
      socket.addEventListener("open", () => {
        retry = 0;
        setRetryCount(0);
      });
      socket.addEventListener("message", (message) => {
        const event = JSON.parse(message.data) as { protocol_version?: number };
        if (event.protocol_version !== 1) {
          socket?.close();
          return;
        }
        apply(event as RealtimeEvent);
      });
      socket.addEventListener("close", () => {
        if (active) scheduleReconnect();
      });
      socket.addEventListener("error", () => socket?.close());
    };

    apiFetch("/api/sessions")
      .then((response) => {
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        return response.json() as Promise<SessionSummary[]>;
      })
      .then((data) => {
        if (!active) return;
        setSessions(new Map(data.map((session) => [session.session_key, session])));
        setConnectionState("connecting");
        void connect();
      })
      .catch((error: unknown) => {
        if (!active) return;
        if (error instanceof DesktopStartupError) {
          setConnectionState("startup_failed");
          return;
        }
        setConnectionState("reconnecting");
        setRetryCount(1);
        timer = window.setTimeout(() => { void connect(); }, 2000);
      });

    return () => {
      active = false;
      if (timer !== undefined) window.clearTimeout(timer);
      socket?.close();
    };
  }, []);

  return {
    sessions: useMemo(
      () => [...sessions.values()].map((session) => connectionState === "online" ? session : stalePresentation(session)),
      [connectionState, sessions],
    ),
    connectionState,
    lastSyncedAt,
    retryCount,
  };
}

export function useSessionDetail(
  sessionKey: string | null,
  version: number | null,
  anchorTurnId: string | null,
  selectionNonce: number,
) {
  const [detail, setDetail] = useState<SessionDetail | null>(null);
  const [state, setState] = useState<DetailState | "idle">("idle");
  const [anchorState, setAnchorState] = useState<AnchorState>("idle");
  const [retry, setRetry] = useState(0);
  const [historicalWindow, setHistoricalWindow] = useState(false);
  const [latestSelectionNonce, setLatestSelectionNonce] = useState<number | null>(null);
  const [fallbackSelectionNonce, setFallbackSelectionNonce] = useState<number | null>(null);
  const generation = useRef(0);
  const request = useRef<AbortController | null>(null);
  const validAnchorTurnId = anchorTurnId !== null && anchorTurnId.length >= 1 && anchorTurnId.length <= 256
    ? anchorTurnId
    : null;
  const requestedAnchor = validAnchorTurnId !== null && latestSelectionNonce !== selectionNonce
    ? validAnchorTurnId
    : null;
  const fallback = fallbackSelectionNonce === selectionNonce && requestedAnchor === null;
  const refreshVersion = requestedAnchor === null ? version : null;

  useEffect(() => {
    generation.current += 1;
    const currentGeneration = generation.current;
    request.current?.abort();
    if (sessionKey === null) {
      setDetail(null);
      setState("idle");
      setAnchorState("idle");
      return;
    }
    const controller = new AbortController();
    request.current = controller;
    setState("loading");
    setAnchorState(requestedAnchor !== null || fallback ? "locating" : "idle");
    const anchorQuery = requestedAnchor === null
      ? ""
      : `?anchor_turn_id=${encodeURIComponent(requestedAnchor)}`;
    void (async () => {
      try {
        const response = await apiFetch(
          `/api/sessions/${encodeURIComponent(sessionKey)}${anchorQuery}`,
          { signal: controller.signal },
        );
        if (response.status === 404) {
          const body = await response.json().catch(() => null) as { detail?: unknown } | null;
          if (requestedAnchor !== null && body?.detail === "Attention anchor not found") {
            if (generation.current !== currentGeneration) return;
            setLatestSelectionNonce(selectionNonce);
            setFallbackSelectionNonce(selectionNonce);
            return;
          }
          throw new Error("not_found");
        }
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const value = await response.json() as SessionDetail;
        if (generation.current !== currentGeneration) return;
        setDetail(value);
        setHistoricalWindow(false);
        setState("ready");
        setAnchorState(requestedAnchor !== null ? "located" : fallback ? "fallback" : "idle");
      } catch (error: unknown) {
        if (error instanceof DOMException && error.name === "AbortError") return;
        if (generation.current !== currentGeneration) return;
        setDetail(null);
        setState(error instanceof Error && error.message === "not_found" ? "not_found" : "error");
        setAnchorState(requestedAnchor !== null || fallback ? "error" : "idle");
      }
    })();
    return () => controller.abort();
  }, [fallback, refreshVersion, requestedAnchor, retry, selectionNonce, sessionKey]);

  const loadEarlier = useCallback(async () => {
    const before = detail?.next_before;
    if (sessionKey === null || before === null || before === undefined) return;
    const currentGeneration = generation.current;
    request.current?.abort();
    const controller = new AbortController();
    request.current = controller;
    try {
      const response = await apiFetch(
        `/api/sessions/${encodeURIComponent(sessionKey)}?limit=20&before=${before}`,
        { signal: controller.signal },
      );
      if (!response.ok) throw new Error(`HTTP ${response.status}`);
      const earlier = await response.json() as SessionDetail;
      if (generation.current !== currentGeneration) return;
      setDetail((current) => {
        if (current === null) return earlier;
        const turns = [...earlier.turns, ...current.turns];
        if (turns.length > MAX_DETAIL_TURNS) setHistoricalWindow(true);
        return {
          ...current,
          turns: turns.slice(0, MAX_DETAIL_TURNS),
          has_earlier: earlier.has_earlier,
          next_before: earlier.next_before,
        };
      });
    } catch (error) {
      if (!(error instanceof DOMException && error.name === "AbortError")) setState("error");
    }
  }, [detail, sessionKey]);

  const resetLatest = useCallback(() => {
    setLatestSelectionNonce(selectionNonce);
    setFallbackSelectionNonce(null);
    setAnchorState("idle");
    setRetry((value) => value + 1);
  }, [selectionNonce]);
  const retryDetail = useCallback(() => setRetry((value) => value + 1), []);
  return { detail, state, anchorState, historicalWindow, loadEarlier, resetLatest, retry: retryDetail };
}
