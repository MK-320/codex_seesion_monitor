import { useCallback, useRef, useState } from "react";

export { isAttentionContext } from "./types";

const STORAGE_KEY = "codex-monitor.attention-registry.v1";
const TTL_MS = 30 * 24 * 60 * 60 * 1000;
const MAX_ENTRIES = 1000;
export const MAX_ATTENTION_REGISTRY_ENTRIES = MAX_ENTRIES;

const EVENT_KEY_PATTERN = /^[0-9a-f]{64}$/;

interface RegistryEntry {
  event_key: string;
  handled_at: number;
}

interface AttentionRegistryV1 {
  schema_version: 1;
  entries: RegistryEntry[];
}

export interface AttentionRegistry {
  isHandled: (eventKey: string) => boolean;
  handle: (eventKey: string) => void;
  undo: (eventKey: string) => void;
  handleMany: (eventKeys: readonly string[]) => boolean;
  undoMany: (eventKeys: readonly string[]) => boolean;
  memoryOnly: boolean;
}

function isRecord(value: unknown): value is Record<string, unknown> {
  if (typeof value !== "object" || value === null || Array.isArray(value)) return false;
  const prototype = Object.getPrototypeOf(value);
  return prototype === Object.prototype || prototype === null;
}

function cleanEntries(
  entries: readonly unknown[],
  currentTime: number,
  preferredKeys?: ReadonlySet<string>,
): Map<string, number> {
  const deduplicated = new Map<string, number>();
  for (const value of entries) {
    if (!isRecord(value)) continue;
    const eventKey = value.event_key;
    const handled_at = value.handled_at;
    if (
      typeof eventKey !== "string" ||
      !EVENT_KEY_PATTERN.test(eventKey) ||
      typeof handled_at !== "number" ||
      !Number.isFinite(handled_at) ||
      !Number.isInteger(handled_at) ||
      handled_at < 0 ||
      handled_at > currentTime ||
      currentTime - handled_at >= TTL_MS
    ) {
      continue;
    }
    if (handled_at > (deduplicated.get(eventKey) ?? -1)) deduplicated.set(eventKey, handled_at);
  }
  return new Map(
    [...deduplicated]
      .sort(([leftKey, leftTime], [rightKey, rightTime]) =>
        rightTime - leftTime ||
        Number(preferredKeys?.has(rightKey) ?? false) - Number(preferredKeys?.has(leftKey) ?? false) ||
        (leftKey < rightKey ? -1 : leftKey > rightKey ? 1 : 0),
      )
      .slice(0, MAX_ENTRIES),
  );
}

function loadRegistry(currentTime: number): { entries: Map<string, number>; memoryOnly: boolean } {
  let serialized: string | null;
  try {
    serialized = localStorage.getItem(STORAGE_KEY);
  } catch {
    return { entries: new Map(), memoryOnly: true };
  }
  if (serialized === null) return { entries: new Map(), memoryOnly: false };
  try {
    const value: unknown = JSON.parse(serialized);
    if (!isRecord(value) || value.schema_version !== 1 || !Array.isArray(value.entries)) {
      return { entries: new Map(), memoryOnly: false };
    }
    return { entries: cleanEntries(value.entries, currentTime), memoryOnly: false };
  } catch {
    return { entries: new Map(), memoryOnly: false };
  }
}

function toEntries(entries: ReadonlyMap<string, number>): RegistryEntry[] {
  return [...entries].map(([event_key, handled_at]) => ({ event_key, handled_at }));
}

export function useAttentionRegistry(now = Date.now): AttentionRegistry {
  const [initial] = useState(() => loadRegistry(now()));
  const entriesRef = useRef(initial.entries);
  const memoryOnlyRef = useRef(initial.memoryOnly);
  const [memoryOnly, setMemoryOnly] = useState(initial.memoryOnly);
  const [, setRevision] = useState(0);

  const commit = useCallback((candidate: Map<string, number>, currentTime: number, preferredKeys?: ReadonlySet<string>) => {
    const next = cleanEntries(toEntries(candidate), currentTime, preferredKeys);
    entriesRef.current = next;
    if (!memoryOnlyRef.current) {
      const stored: AttentionRegistryV1 = { schema_version: 1, entries: toEntries(next) };
      try {
        localStorage.setItem(STORAGE_KEY, JSON.stringify(stored));
      } catch {
        memoryOnlyRef.current = true;
        setMemoryOnly(true);
      }
    }
    setRevision((revision) => revision + 1);
  }, []);

  const isHandled = useCallback((eventKey: string) => entriesRef.current.has(eventKey), []);

  const handle = useCallback(
    (eventKey: string) => {
      if (!EVENT_KEY_PATTERN.test(eventKey)) return;
      const currentTime = now();
      const next = new Map(entriesRef.current);
      next.set(eventKey, currentTime);
      commit(next, currentTime, new Set([eventKey]));
    },
    [commit, now],
  );

  const undo = useCallback(
    (eventKey: string) => {
      if (!entriesRef.current.has(eventKey)) return;
      const currentTime = now();
      const next = new Map(entriesRef.current);
      next.delete(eventKey);
      commit(next, currentTime);
    },
    [commit, now],
  );

  const handleMany = useCallback(
    (eventKeys: readonly string[]) => {
      if (eventKeys.length > MAX_ENTRIES || eventKeys.some((key) => !EVENT_KEY_PATTERN.test(key))) return false;
      const currentTime = now();
      const next = new Map(entriesRef.current);
      for (const eventKey of eventKeys) next.set(eventKey, currentTime);
      commit(next, currentTime, new Set(eventKeys));
      return true;
    },
    [commit, now],
  );

  const undoMany = useCallback(
    (eventKeys: readonly string[]) => {
      if (eventKeys.length > MAX_ENTRIES || eventKeys.some((key) => !EVENT_KEY_PATTERN.test(key))) return false;
      const next = new Map(entriesRef.current);
      for (const eventKey of eventKeys) next.delete(eventKey);
      commit(next, now());
      return true;
    },
    [commit, now],
  );

  return { isHandled, handle, undo, handleMany, undoMany, memoryOnly };
}
