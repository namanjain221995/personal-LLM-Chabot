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
 * The composer toggle for this is GONE (2026-07-28): the effort level decides.
 * "auto" is therefore the only value the UI produces, and the level's ceiling
 * does the real gating server-side — Fast refuses to search whatever this says.
 * The union is kept because stored prefs and the API still carry all three.
 */
export type WebSearchMode = 'off' | 'auto' | 'on';

export interface ChatPrefs {
  /** Salesforce mode on (v1 behavior); off → mode "assistant". */
  salesforce: boolean;
  model: ModelChoice;
  effort: ReasoningEffort;
  agent: boolean;
  webSearch: WebSearchMode;
}

export const DEFAULT_PREFS: ChatPrefs = {
  salesforce: true,
  model: 'smart',
  effort: 'medium',
  agent: false,
  webSearch: 'auto',
};

const STORAGE_KEY = 'techsara.chatprefs.v1';
const DRAFT_SLOT = '__draft__';
/** Cap the map so years of conversations cannot bloat localStorage. */
const MAX_ENTRIES = 200;

function sanitize(raw: unknown): ChatPrefs {
  const p = (raw ?? {}) as Partial<ChatPrefs>;
  return {
    salesforce:
      typeof p.salesforce === 'boolean'
        ? p.salesforce
        : DEFAULT_PREFS.salesforce,
    model: p.model === 'fast' || p.model === 'smart' ? p.model : DEFAULT_PREFS.model,
    effort:
      p.effort === 'fast' ||
      p.effort === 'low' ||
      p.effort === 'medium' ||
      p.effort === 'high'
        ? p.effort
        : DEFAULT_PREFS.effort,
    // Both of these had composer toggles that are now gone, so a value saved
    // by the old UI can no longer be changed by the user. Left as-is, someone
    // who once switched search off would never search again, and someone who
    // once switched Agent on would run the slow path at every level — both
    // with no visible control to undo it. Migrate them to what the level says.
    agent: false,
    webSearch: p.webSearch === 'on' ? 'on' : DEFAULT_PREFS.webSearch,
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
