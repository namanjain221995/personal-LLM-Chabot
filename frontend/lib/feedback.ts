/**
 * Per-message thumbs feedback (owner request 2026-08-05, ChatGPT-style
 * like/dislike in the action row). Client-side only: persisted in
 * localStorage keyed by message id — there is no server endpoint for
 * feedback yet, so this is a preference the user can see stick, not
 * telemetry. Follows the prefs.ts storage pattern (capped map, never throw).
 */

import type { StorageLike } from './history';

export type MessageFeedback = 'up' | 'down';

const STORAGE_KEY = 'techsara.feedback.v1';
/** Cap so years of chatting cannot bloat localStorage. */
const MAX_ENTRIES = 1000;

/**
 * What clicking a thumb does to the stored state: clicking the same thumb
 * again clears it, clicking the other switches. Pure so it is unit-testable.
 */
export function toggleFeedback(
  prev: MessageFeedback | null,
  clicked: MessageFeedback,
): MessageFeedback | null {
  return prev === clicked ? null : clicked;
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
    // Insertion order ≈ age; drop the oldest entries.
    for (const k of keys.slice(0, keys.length - MAX_ENTRIES)) delete map[k];
  }
  try {
    storage.setItem(STORAGE_KEY, JSON.stringify(map));
  } catch {
    // Feedback is a convenience — never let quota break the app.
  }
}

export function loadFeedback(
  storage: StorageLike,
  messageId: string,
): MessageFeedback | null {
  const v = readMap(storage)[messageId];
  return v === 'up' || v === 'down' ? v : null;
}

/** null clears the entry entirely rather than storing a tombstone. */
export function saveFeedback(
  storage: StorageLike,
  messageId: string,
  feedback: MessageFeedback | null,
): void {
  const map = readMap(storage);
  if (feedback === null) delete map[messageId];
  else map[messageId] = feedback;
  writeMap(storage, map);
}
