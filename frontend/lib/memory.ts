/**
 * What the assistant saved about the signed-in person, and how to remove it
 * (B11). The browser half of the orchestrator's memory_api.py.
 *
 * The orchestrator has listed and deleted facts since V10, but nothing in the
 * frontend called it: 132 production facts written under the old extraction
 * rules (task requests, one jailbreak attempt) could be neither seen nor
 * deleted by the people they describe. These types stay here rather than in
 * lib/types.ts because nothing else in the app reads them.
 */

import type { FetchLike } from './auth';

/**
 * One saved fact, as db.list_user_facts returns it.
 *
 * `source` (V40) is 'stated' when the extractor read it out of the person's
 * own message and 'manual' when they added it through /memory/facts. NULL
 * means the row was written before V40 and nobody recorded where it came
 * from — exactly the rows a person most needs to be able to review.
 */
export interface MemoryFact {
  id: number;
  fact: string;
  source_conversation_id?: string | null;
  source: string | null;
  source_excerpt: string | null;
  created_at: string | null;
  updated_at: string | null;
}

/**
 * What a person types to clear everything. A phrase rather than a click
 * because the clear-all cannot be undone and the panel sits one mis-tap away
 * from the per-row delete.
 */
export const CLEAR_ALL_PHRASE = 'delete all';

/** The label a person reads for where a fact came from. */
export function sourceLabel(source: string | null | undefined): string {
  if (source === 'stated') return 'From your message';
  if (source === 'manual') return 'Added by you';
  return 'Origin unknown';
}

/** Is this a fact whose origin nobody recorded (a pre-V40 row)? */
export function isUnknownSource(source: string | null | undefined): boolean {
  return source !== 'stated' && source !== 'manual';
}

/** A quote longer than this stops being "short" in a one-line meta row. */
const EXCERPT_MAX = 140;

let graphemes: Intl.Segmenter | null | undefined;

/**
 * Walk what a reader sees as characters. `String.slice` cuts UTF-16 code
 * units, so an emoji straddling the limit became a lone surrogate that
 * Chromium painted as a visible U+FFFD (QA, 2026-09-18: "aaaa…a�…"). A
 * grapheme walk also keeps a family emoji or a flag whole; code points are
 * the fallback where Intl.Segmenter is missing, and never split a surrogate
 * pair either. Lazy, so a caller that stops early never segments the rest.
 */
function characters(text: string): Iterable<string> {
  if (graphemes === undefined) {
    graphemes =
      typeof Intl !== 'undefined' && typeof Intl.Segmenter === 'function'
        ? new Intl.Segmenter(undefined, { granularity: 'grapheme' })
        : null;
  }
  if (!graphemes) return text;
  const segments = graphemes.segment(text);
  return (function* () {
    for (const s of segments) yield s.segment;
  })();
}

/**
 * One line of text, at most `max` characters, with "…" where it was cut.
 * Whitespace is collapsed and the ends trimmed; nothing else is rewritten.
 *
 * Stops reading at character max+1: segmenting the whole of every 500-char
 * excerpt on every render was part of why 10,000 rows took 8.7 s to delete
 * one in Chromium (QA, 2026-09-18).
 */
export function clipText(text: string, max: number): string {
  const flat = text.replace(/\s+/g, ' ').trim();
  // Cheap exit: a string no longer than `max` code units cannot be longer
  // than `max` characters.
  if (flat.length <= max) return flat;
  const kept: string[] = [];
  for (const ch of characters(flat)) {
    if (kept.length === max) return `${kept.join('').trimEnd()}…`;
    kept.push(ch);
  }
  return flat;
}

/**
 * The source excerpt as a short, one-line quote — or null when there is
 * nothing to quote. The excerpt is the person's own words, so it is only
 * trimmed and whitespace-collapsed, never rewritten.
 */
export function excerptQuote(excerpt: string | null | undefined): string | null {
  const flat = (excerpt ?? '').replace(/\s+/g, ' ').trim();
  if (!flat) return null;
  return clipText(flat, EXCERPT_MAX);
}

/**
 * The date a row shows: when its CURRENT text was written. db.update_user_fact
 * rewrites fact, source, source_excerpt and updated_at but leaves created_at,
 * so a fact rewritten today from a message sent today showed its first-save
 * date under today's quote (QA, 2026-09-18: "I moved to Globex last week…"
 * dated Mar 2) and the rows, sorted by updated_at, read out of order.
 */
export function factShownAt(fact: Pick<MemoryFact, 'created_at' | 'updated_at'>): string | null {
  return fact.updated_at ?? fact.created_at;
}

/** A timestamp the panel can format, or null (formatDay echoes junk back). */
function timestamp(value: unknown): string | null {
  return typeof value === 'string' && Number.isFinite(Date.parse(value)) ? value : null;
}

/**
 * The key two fact strings are compared by: the orchestrator's own dedupe
 * key in memory_api.add_facts (collapsed whitespace, lowercase, no trailing
 * full stop). The chip's `memory_updated` list is matched against the panel's
 * rows with it, so "Just saved" marks the same rows the server considers one.
 */
export function factKey(fact: string): string {
  return fact.replace(/\s+/g, ' ').trim().toLowerCase().replace(/\.+$/, '');
}

/** A request that reached the server and was refused (status kept for callers). */
export class MemoryRequestError extends Error {
  constructor(readonly status: number) {
    super(`memory request failed with ${status}`);
    this.name = 'MemoryRequestError';
  }
}

/** A 200 whose body is not the {facts: [...]} list the orchestrator sends. */
export class MemoryShapeError extends Error {
  constructor() {
    super('memory list response has no facts array');
    this.name = 'MemoryShapeError';
  }
}

/** Did the server say the session is gone (as opposed to a network fault)? */
export function isSessionEnded(err: unknown): boolean {
  return err instanceof MemoryRequestError && err.status === 401;
}

/**
 * A row the panel can show AND delete. The id must be one the delete route
 * accepts (lib/memoryRoutes: a positive integer, no sign, fraction or
 * exponent) and one a JS number holds exactly: 0, -1, 1.5 or a 19-digit id
 * would render with a delete that can only fail, or that names another row
 * once rounded. The orchestrator's ids are identity bigints from 1, so this
 * drops nothing it sends.
 */
function isFact(value: unknown): value is MemoryFact {
  if (!value || typeof value !== 'object') return false;
  const v = value as Record<string, unknown>;
  return (
    typeof v.id === 'number' &&
    Number.isSafeInteger(v.id) &&
    v.id > 0 &&
    typeof v.fact === 'string'
  );
}

/** GET /api/memory/facts. Throws on a refusal or a network failure. */
export async function listFacts(
  fetchFn: FetchLike = fetch,
  signal?: AbortSignal,
): Promise<MemoryFact[]> {
  const res = await fetchFn('/api/memory/facts', { cache: 'no-store', signal });
  if (!res.ok) throw new MemoryRequestError(res.status);
  const body = (await res.json()) as { facts?: unknown } | null;
  // memory_api.list_facts always answers {facts: [...]}. Any other body is a
  // response we do not understand, and saying "Nothing saved yet" about it
  // would tell a person their memory is empty when it may not be.
  if (!body || !Array.isArray(body.facts)) throw new MemoryShapeError();
  return body.facts.filter(isFact).map((f) => ({
    ...f,
    source: typeof f.source === 'string' ? f.source : null,
    source_excerpt: typeof f.source_excerpt === 'string' ? f.source_excerpt : null,
    created_at: timestamp(f.created_at),
    updated_at: timestamp(f.updated_at),
  }));
}

/** memory_api.delete_fact's 404 detail: the fact is not (or no longer) there. */
export const FACT_NOT_FOUND = 'fact not found';

/**
 * DELETE /api/memory/facts/{id}. The orchestrator's "fact not found" 404
 * means the fact is already gone (a second tab, or a clear-all elsewhere),
 * which is the outcome the person asked for, so it resolves rather than
 * throws. Any other 404 is a refusal: the proxy's own "Unknown memory
 * endpoint." 404 used to pass as a success, so the row vanished and "Deleted
 * from memory" showed while the fact stayed saved (QA, 2026-09-18).
 */
export async function deleteFact(id: number, fetchFn: FetchLike = fetch): Promise<void> {
  const res = await fetchFn(`/api/memory/facts/${encodeURIComponent(String(id))}`, {
    method: 'DELETE',
  });
  if (res.ok) return;
  if (res.status === 404) {
    const body = (await res.json().catch(() => null)) as { detail?: unknown } | null;
    if (body?.detail === FACT_NOT_FOUND) return;
  }
  throw new MemoryRequestError(res.status);
}

/**
 * DELETE /api/memory/facts?confirm=all — every fact this person has. Returns
 * how many the server removed. Without `confirm=all` the server answers 422
 * and the proxy 404, so the parameter is the second lock behind the typed
 * confirmation, not the only one.
 */
export async function clearFacts(fetchFn: FetchLike = fetch): Promise<number> {
  const res = await fetchFn('/api/memory/facts?confirm=all', { method: 'DELETE' });
  if (!res.ok) throw new MemoryRequestError(res.status);
  const body = (await res.json().catch(() => ({}))) as { deleted?: unknown };
  return typeof body.deleted === 'number' ? body.deleted : 0;
}
