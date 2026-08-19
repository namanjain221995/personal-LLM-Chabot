/**
 * Per-conversation composer preferences (V2 §4c): Salesforce toggle, model
 * choice, reasoning effort, agent mode. Persisted client-side (localStorage)
 * keyed by conversation id — a UI convenience, deliberately not part of the
 * server history schema. A "draft" slot holds the prefs of a not-yet-created
 * conversation; they are adopted by the conversation on first send.
 */

import type { ModelChoice, ReasoningEffort } from './types';
import type { StorageLike } from './history';

/**
 * Phase 1: web search — off (never), auto (model decides), on (force).
 *
 * The always-visible toggle is GONE (2026-07-28): by default the effort level
 * decides ("auto"), and its ceiling does the real gating server-side — Fast
 * refuses to search whatever this says. Since 2026-08-05 the composer's "+"
 * menu can force "on", but only while Salesforce is OFF — Salesforce mode
 * never searches the web, so sanitize() below keeps the pair coherent.
 * "off" survives in the union because stored prefs and the API still carry it.
 */
export type WebSearchMode = 'off' | 'auto' | 'on';

export interface ChatPrefs {
  /** Salesforce mode on (v1 behavior); off → mode "assistant". */
  salesforce: boolean;
  /**
   * Live Salesforce (2026-08-06): answers query the org directly — any
   * object or field the read-only integration user can see — instead of the
   * 30-minute synced copy. Only meaningful while `salesforce` is on; the
   * server ignores it otherwise and sanitize() keeps the pair coherent.
   */
  sfLive: boolean;
  model: ModelChoice;
  effort: ReasoningEffort;
  agent: boolean;
  webSearch: WebSearchMode;
}

export const DEFAULT_PREFS: ChatPrefs = {
  salesforce: true,
  sfLive: false,
  model: 'smart',
  effort: 'think',
  agent: false,
  webSearch: 'auto',
};

const STORAGE_KEY = 'techsara.chatprefs.v1';
const DRAFT_SLOT = '__draft__';
/** Cap the map so years of conversations cannot bloat localStorage. */
const MAX_ENTRIES = 200;

/** Pre-collapse levels stored by earlier builds normalize to the 3-level
 *  ladder (2026-08-19): a saved 'high' keeps thinking as 'think', a saved
 *  'extra_high' keeps best-of-N as 'max'. */
const LEGACY_EFFORTS: Record<string, ReasoningEffort> = {
  low: 'fast',
  medium: 'think',
  high: 'think',
  extra_high: 'max',
};

function sanitizeEffort(value: unknown): ReasoningEffort {
  if (value === 'fast' || value === 'think' || value === 'max') return value;
  if (typeof value === 'string' && value in LEGACY_EFFORTS) {
    return LEGACY_EFFORTS[value];
  }
  return DEFAULT_PREFS.effort;
}

function sanitize(raw: unknown): ChatPrefs {
  const p = (raw ?? {}) as Partial<ChatPrefs>;
  const salesforce =
    typeof p.salesforce === 'boolean' ? p.salesforce : DEFAULT_PREFS.salesforce;
  return {
    salesforce,
    // Live Salesforce is a sub-mode of Salesforce: without the parent toggle
    // there is no menu item to undo it, so it never survives salesforce=off.
    sfLive: p.sfLive === true && salesforce,
    model: p.model === 'fast' || p.model === 'smart' ? p.model : DEFAULT_PREFS.model,
    effort: sanitizeEffort(p.effort),
    // Both of these had composer toggles that are now gone, so a value saved
    // by the old UI can no longer be changed by the user. Left as-is, someone
    // who once switched search off would never search again, and someone who
    // once switched Agent on would run the slow path at every level — both
    // with no visible control to undo it. Migrate them to what the level says.
    agent: false,
    // A forced web search ('on', settable from the "+" menu since 2026-08-05)
    // is only coherent with Salesforce OFF: Salesforce mode never searches
    // the web, its menu hides the toggle, so a stored 'on' there would be
    // invisible and un-undoable — exactly the trap the agent migration above
    // exists for. Normalize it away on load.
    webSearch: p.webSearch === 'on' && !salesforce ? 'on' : DEFAULT_PREFS.webSearch,
  };
}

function readMap(storage: StorageLike): Record<string, unknown> {
  try {
    const raw = storage.getItem(STORAGE_KEY);
    if (!raw) return {};
    const parsed = JSON.parse(raw);
    return parsed && typeof parsed === 'object' && !Array.isArray(parsed)
      ? (parsed as Record<string, unknown>)
      : {};
  } catch {
    return {};
  }
}

function writeMap(storage: StorageLike, map: Record<string, unknown>): void {
  const keys = Object.keys(map);
  if (keys.length > MAX_ENTRIES) {
    // Insertion order ≈ age; drop the oldest non-draft entries.
    for (const k of keys.slice(0, keys.length - MAX_ENTRIES)) {
      if (k !== DRAFT_SLOT) delete map[k];
    }
  }
  try {
    storage.setItem(STORAGE_KEY, JSON.stringify(map));
  } catch {
    // Preferences are a convenience — never let quota break the app.
  }
}

/** Load prefs for a conversation (null = the new-chat draft slot). */
export function loadPrefs(
  storage: StorageLike,
  conversationId: string | null,
): ChatPrefs {
  const map = readMap(storage);
  const key = conversationId ?? DRAFT_SLOT;
  return key in map ? sanitize(map[key]) : { ...DEFAULT_PREFS };
}

/** Save prefs for a conversation (null = the new-chat draft slot). */
export function savePrefs(
  storage: StorageLike,
  conversationId: string | null,
  prefs: ChatPrefs,
): void {
  const map = readMap(storage);
  map[conversationId ?? DRAFT_SLOT] = sanitize(prefs);
  writeMap(storage, map);
}

/** First send of a new chat: the draft prefs become the conversation's. */
export function adoptDraftPrefs(
  storage: StorageLike,
  conversationId: string,
): ChatPrefs {
  const prefs = loadPrefs(storage, null);
  const map = readMap(storage);
  map[conversationId] = sanitize(prefs);
  delete map[DRAFT_SLOT];
  writeMap(storage, map);
  return prefs;
}

/** Forget a deleted conversation's prefs. */
export function removePrefs(storage: StorageLike, conversationId: string): void {
  const map = readMap(storage);
  if (conversationId in map) {
    delete map[conversationId];
    writeMap(storage, map);
  }
}
