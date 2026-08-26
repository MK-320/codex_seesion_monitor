import { useCallback, useEffect, useRef, useState } from "react";
import {
  isPermissionGranted,
  requestPermission,
  sendNotification,
} from "@tauri-apps/plugin-notification";

import {
  isDesktopRuntime,
  isDesktopWindowFocused,
  sendAttentionNotification,
} from "./desktop";
import { needsAttention } from "./format";
import { isAttentionContext, type SessionSummary } from "./types";

const STORAGE_KEY = "attention-notifications-v2";
export const ACTIVITY_BASELINE_RESET_EVENT = "codex-monitor:activity-baseline-reset";
const EVENT_KEY_PATTERN = /^event:([0-9a-f]{64})$/;
const NOTIFIABLE_REASONS = new Set(["long_running_tool", "no_progress", "tool_error", "turn_aborted"]);

export type NotificationToggleResult = "enabled" | "disabled" | "denied" | "unsupported" | "error";

function notificationEventKey(newKeys: readonly string[]): string | null {
  if (newKeys.length !== 1) return null;
  return EVENT_KEY_PATTERN.exec(newKeys[0])?.[1] ?? null;
}

function readEnabled(): boolean {
  try {
    return localStorage.getItem(STORAGE_KEY) !== "false";
  } catch {
    return true;
  }
}

function writeEnabled(enabled: boolean): void {
  try {
    localStorage.setItem(STORAGE_KEY, String(enabled));
  } catch {
    // Persistence is optional; notification state remains usable in memory.
  }
}

export function useAttentionNotifications(
  sessions: SessionSummary[],
  hasSynced: boolean,
  isHandled: (eventKey: string) => boolean,
  onActivate: (eventKey: string | null) => void,
) {
  const browserSupported = "Notification" in window;
  const supported = isDesktopRuntime || browserSupported;
  const [permission, setPermission] = useState<NotificationPermission | "unsupported">(
    () => (isDesktopRuntime ? "default" : browserSupported ? Notification.permission : "unsupported"),
  );
  const [preferred, setPreferred] = useState(() => supported && readEnabled());
  const enabled = preferred && permission === "granted";
  const enabledRef = useRef(enabled);
  enabledRef.current = enabled;
  const previous = useRef<Set<string> | null>(null);
  const lastNotified = useRef(new Map<string, number>());

  useEffect(() => {
    const resetBaseline = () => {
      previous.current = null;
      lastNotified.current.clear();
    };
    window.addEventListener(ACTIVITY_BASELINE_RESET_EVENT, resetBaseline);
    return () => window.removeEventListener(ACTIVITY_BASELINE_RESET_EVENT, resetBaseline);
  }, []);

  useEffect(() => {
    if (!isDesktopRuntime || !preferred) return;
    let active = true;
    void isPermissionGranted()
      .then(async (granted) => {
        if (!granted) granted = (await requestPermission()) === "granted";
        if (!active) return;
        setPermission(granted ? "granted" : "denied");
      })
      .catch(() => {
        if (!active) return;
        setPermission("default");
      });
    return () => {
      active = false;
    };
  }, [preferred]);

  const toggle = useCallback(async (): Promise<NotificationToggleResult> => {
    if (!supported) return "unsupported";
    if (enabled) {
      setPreferred(false);
      return "disabled";
    }
    try {
      let granted = isDesktopRuntime
        ? await isPermissionGranted()
        : Notification.permission === "granted";
      if (!granted) {
        const result = isDesktopRuntime
          ? await requestPermission()
          : await Notification.requestPermission();
        granted = result === "granted";
      }
      setPermission(granted ? "granted" : "denied");
      if (granted) {
        setPreferred(true);
        return "enabled";
      }
      return "denied";
    } catch {
      setPermission("default");
      return "error";
    }
  }, [enabled, supported]);

  useEffect(() => {
    writeEnabled(preferred);
  }, [preferred]);

  useEffect(() => {
    if (!hasSynced) {
      previous.current = null;
      return;
    }
    const current = new Set<string>();
    const currentKinds = new Map<string, "no_progress" | "long_running_tool" | "mixed">();
    for (const session of sessions.filter((item) => needsAttention(item) && item.attention_reasons.some((reason) => NOTIFIABLE_REASONS.has(reason)))) {
      const context = session.attention_context;
      const key = isAttentionContext(context) ? context.event_key : session.session_key;
      const kind = session.activity_state === "no_progress" || session.activity_state === "long_running_tool"
        ? session.activity_state
        : "mixed";
      if (isAttentionContext(context)) {
        if (!isHandled(context.event_key)) {
          current.add(`event:${key}`);
          currentKinds.set(`event:${key}`, kind);
        }
      } else {
        current.add(`session:${key}`);
        currentKinds.set(`session:${key}`, kind);
      }
    }
    if (previous.current === null) {
      previous.current = current;
      return;
    }
    const now = Date.now();
    const newKeys = [...current].filter(
      (key) => !previous.current?.has(key) && now - (lastNotified.current.get(key) ?? 0) >= 60_000,
    );
    if (!enabled || newKeys.length === 0) {
      previous.current = current;
      return;
    }
    let cancelled = false;
    void (async () => {
      let inactive = document.visibilityState === "hidden";
      if (isDesktopRuntime && !inactive) {
        try {
          inactive = !(await isDesktopWindowFocused());
        } catch {
          return;
        }
      }
      if (cancelled || !enabledRef.current) return;
      previous.current = current;
      if (!inactive) return;
      for (const key of newKeys) lastNotified.current.set(key, now);
      const eventKey = notificationEventKey(newKeys);
      const newKinds = newKeys.map((key) => currentKinds.get(key) ?? "mixed");
      const kind = newKinds.every((value) => value === "no_progress")
        ? "no_progress"
        : newKinds.every((value) => value === "long_running_tool")
          ? "long_running_tool"
          : "mixed";
      const body = kind === "no_progress"
        ? `有 ${newKeys.length} 个会话长时间没有新进展`
        : kind === "long_running_tool"
          ? `有 ${newKeys.length} 个会话的工具执行时间较长`
          : `有 ${newKeys.length} 个会话需要关注`;
      if (isDesktopRuntime) {
        void sendAttentionNotification(newKeys.length, eventKey, kind).catch(() => {
          sendNotification({ title: "Codex Session Monitor", body });
        });
      } else {
        const notification = new Notification("Codex Session Monitor", { body });
        notification.onclick = () => {
          window.focus();
          onActivate(eventKey);
        };
      }
    })().catch(() => undefined);
    return () => {
      cancelled = true;
    };
  }, [enabled, hasSynced, isHandled, onActivate, sessions]);

  return { enabled, permission, supported, toggle };
}
