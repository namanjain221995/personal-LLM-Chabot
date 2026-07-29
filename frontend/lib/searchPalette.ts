/**
 * Headless model for the ChatGPT-style search palette (V4 §2).
 *
 * Same split as lib/conversationMenu.ts: every decision the palette makes —
 * how the wire payload is read, which date bucket a chat lands in, what the
 * keyboard does, whether a row shows a snippet, how the keystroke debounce
 * collapses, and which global shortcut fires — lives here as a pure function
 * so it is unit-tested in the node environment vitest runs in.
 * `components/SearchPalette.tsx` is then a thin rendering shell over it.
 */

import { toEpoch, type ServerSearchResult } from './historyApi';
import type { ConversationSummary } from './types';

/* ------------------------------------------------------------- results */

/** Which column the orchestrator matched on (V4 §2). */
export type SearchMatch = 'title' | 'message';

/** A search hit after normalization — one row per conversation. */
export interface SearchResult {
  id: string;
  title: string;
  /** Epoch milliseconds, whatever wire format the server used. */
  updatedAt: number;
  pinned: boolean;
  archived: boolean;
  /** ~120-char window around the hit; null for title-only matches. */
  snippet: string | null;
  matchedIn: SearchMatch;
}

/**
 * Reads `GET /history/search`. The endpoint answers `{results: [...]}`, but a
 * bare array is accepted too so the palette cannot be broken by a backend
 * that trims the envelope; anything unrecognizable degrades to "no hits"
 * rather than throwing into the render.
 */
export function parseSearchResults(
  body: unknown,
  fallbackTime = Date.now(),
): SearchResult[] {
  const rows = Array.isArray(body)
    ? body
    : body && typeof body === 'object' && Array.isArray((body as { results?: unknown }).results)
      ? ((body as { results: unknown[] }).results)
      : [];

  const out: SearchResult[] = [];
  for (const raw of rows) {
    if (!raw || typeof raw !== 'object') continue;
    const row = raw as ServerSearchResult;
    if (typeof row.id !== 'string' || !row.id) continue;
    const snippet =
      typeof row.snippet === 'string' && row.snippet.trim() ? row.snippet : null;
    out.push({
      id: row.id,
      title:
        typeof row.title === 'string' && row.title.trim()
          ? row.title
          : 'Conversation',
      updatedAt: toEpoch(row.updated_at, fallbackTime),
      pinned: row.pinned === true,
      archived: row.archived === true,
      snippet,
      matchedIn: row.matched_in === 'message' ? 'message' : 'title',
    });
  }
  return out;
}

/**
 * An empty query shows the conversations the shell already has in memory
 * (V4 §2) — no request, no spinner, and the palette is useful the instant it
 * opens. These are title-context rows, so they never carry a snippet.
 */
export function resultsFromSummaries(
  conversations: ConversationSummary[],
): SearchResult[] {
  return conversations.map((c) => ({
    id: c.id,
    title: c.title,
    updatedAt: c.updatedAt,
    pinned: c.pinned === true,
    archived: c.archived === true,
    snippet: null,
    matchedIn: 'title' as const,
  }));
}

/* --------------------------------------------------------- date groups */

export type DateGroupLabel = 'Today' | 'Yesterday' | 'Previous 7 Days' | 'Older';

/** Render order of the groups (V4 §2). */
export const DATE_GROUP_ORDER: DateGroupLabel[] = [
  'Today',
  'Yesterday',
  'Previous 7 Days',
  'Older',
];

function startOfDay(ts: number): number {
  const d = new Date(ts);
  d.setHours(0, 0, 0, 0);
  return d.getTime();
}

/**
 * Buckets a timestamp the way ChatGPT does — by LOCAL CALENDAR DAY, not by
 * elapsed hours: a chat from 11pm last night is "Yesterday" at 1am, never
 * "Today". Rounding the day delta keeps 23- and 25-hour DST days on the
 * right side of every boundary, and a clock-skewed future timestamp reads as
 * "Today" rather than falling off the end into "Older".
 */
export function dateGroup(updatedAt: number, now = Date.now()): DateGroupLabel {
  const days = Math.round((startOfDay(now) - startOfDay(updatedAt)) / 86_400_000);
  if (days <= 0) return 'Today';
  if (days === 1) return 'Yesterday';
  if (days <= 7) return 'Previous 7 Days';
  return 'Older';
}

/* --------------------------------------------------------- palette rows */

export type PaletteRow =
  | { kind: 'new-chat'; index: number }
  | { kind: 'conversation'; index: number; result: SearchResult };

export interface PaletteSection {
  /** null on the leading action section — "New chat" renders unlabelled. */
  label: DateGroupLabel | null;
  rows: PaletteRow[];
}

export interface PaletteModel {
  sections: PaletteSection[];
  /** Every row in RENDER order — this is the keyboard's index space. */
  rows: PaletteRow[];
}

/**
 * Turns search hits into what the palette draws: "New chat" always first
 * (V4 §2), then one section per non-empty date group in fixed order.
 *
 * Indices are assigned after bucketing, never from the server's order — the
 * server sorts pinned-first, so a pinned chat from last month arrives first
 * but renders down in "Older". Numbering by render order is what keeps
 * ArrowDown moving to the row the user can actually see next.
 */
export function buildPaletteModel(
  results: SearchResult[],
  now = Date.now(),
): PaletteModel {
  const buckets = new Map<DateGroupLabel, SearchResult[]>();
  for (const result of results) {
    const label = dateGroup(result.updatedAt, now);
    const bucket = buckets.get(label);
    if (bucket) bucket.push(result);
    else buckets.set(label, [result]);
  }

  const newChat: PaletteRow = { kind: 'new-chat', index: 0 };
  const rows: PaletteRow[] = [newChat];
  const sections: PaletteSection[] = [{ label: null, rows: [newChat] }];

  for (const label of DATE_GROUP_ORDER) {
    const bucket = buckets.get(label);
    if (!bucket || bucket.length === 0) continue;
    const sectionRows = bucket.map((result): PaletteRow => {
      const row: PaletteRow = { kind: 'conversation', index: rows.length, result };
      rows.push(row);
      return row;
    });
    sections.push({ label, rows: sectionRows });
  }

  return { sections, rows };
}

/**
 * A row shows its snippet only for CONTENT hits. On a title hit the matched
 * text is the row label itself, so repeating a slice of the first message
 * underneath would be noise, not evidence.
 */
export function rowSnippet(result: SearchResult): string | null {
  return result.matchedIn === 'message' ? result.snippet : null;
}

/* ------------------------------------------------------------ snippets */

/** Target width of a snippet window, in characters (V4 §2). */
export const SNIPPET_WIDTH = 120;

/**
 * A ~`width`-character window of `content` centered on the first hit, with
 * ellipses marking the sides that were cut. Whitespace is collapsed first so
 * a multi-line answer does not turn into a snippet of blank space. Returns
 * null when there is no hit to center on.
 *
 * The real snippets come from the orchestrator; this is the same rule in
 * TypeScript for MOCK_MODE, and it is what makes the windowing testable.
 */
export function buildSnippet(
  content: string,
  query: string,
  width = SNIPPET_WIDTH,
): string | null {
  const text = content.replace(/\s+/g, ' ').trim();
  const needle = query.trim().toLowerCase();
  if (!text || !needle) return null;

  const at = text.toLowerCase().indexOf(needle);
  if (at === -1) return null;
  if (text.length <= width) return text;

  const pad = Math.max(0, Math.floor((width - needle.length) / 2));
  const start = Math.min(Math.max(0, at - pad), Math.max(0, text.length - width));
  const end = Math.min(text.length, start + width);

  return `${start > 0 ? '…' : ''}${text.slice(start, end)}${
    end < text.length ? '…' : ''
  }`;
}

/* ------------------------------------------------------------ keyboard */

export type PaletteKeyAction =
  /** Move the highlight (aria-activedescendant) to `index`. */
  | { kind: 'move'; index: number }
  /** Open the highlighted row. */
  | { kind: 'activate' }
  /** Dismiss the palette and restore focus to the trigger. */
  | { kind: 'close' };

/**
 * The palette's keyboard map. Focus never leaves the text input — the
 * highlight is aria-activedescendant — so this deliberately owns ONLY the
 * keys a textbox does not need: arrows wrap through every row (New chat
 * included), Enter opens, Escape closes. Home/End/Tab are left alone so they
 * still move the caret and cycle the modal's focus trap.
 */
export function paletteKeyAction(
  key: string,
  current: number,
  count: number,
): PaletteKeyAction | null {
  if (key === 'Escape') return { kind: 'close' };
  if (count <= 0) return null;
  switch (key) {
    case 'ArrowDown':
      return { kind: 'move', index: (((current + 1) % count) + count) % count };
    case 'ArrowUp':
      return { kind: 'move', index: (((current - 1) % count) + count) % count };
    case 'Enter':
      return { kind: 'activate' };
    default:
      return null;
  }
}

/** Where Tab moves inside the focus trap; wraps at both ends (V4 §2). */
export function trapFocusIndex(
  current: number,
  count: number,
  backwards: boolean,
): number {
  if (count <= 0) return -1;
  const next = backwards ? current - 1 : current + 1;
  return ((next % count) + count) % count;
}

/* --------------------------------------------------------------- query */

/** Matches the endpoint's contract: 1–100 characters after trimming. */
export const SEARCH_MAX_QUERY = 100;

export function normalizeQuery(raw: string): string {
  return raw.trim().slice(0, SEARCH_MAX_QUERY);
}

/** Milliseconds after the last keystroke before the palette asks the server. */
export const SEARCH_DEBOUNCE_MS = 150;

export interface Debounced<Args extends unknown[]> {
  run(...args: Args): void;
  cancel(): void;
}

/**
 * Collapses a burst of calls into the last one. Typing "acme" must cost one
 * request, not four, and `cancel()` lets the palette drop a pending search
 * when it closes or when the query goes back to empty.
 */
export function createDebounce<Args extends unknown[]>(
  fn: (...args: Args) => void,
  delayMs: number = SEARCH_DEBOUNCE_MS,
): Debounced<Args> {
  let timer: ReturnType<typeof setTimeout> | null = null;
  return {
    run(...args: Args) {
      if (timer !== null) clearTimeout(timer);
      timer = setTimeout(() => {
        timer = null;
        fn(...args);
      }, delayMs);
    },
    cancel() {
      if (timer !== null) {
        clearTimeout(timer);
        timer = null;
      }
    },
  };
}

/* ----------------------------------------------------------- shortcuts */

export type ShortcutAction =
  | 'open-search'
  | 'new-chat'
  | 'close-palette'
  | 'stop-streaming'
  | 'focus-composer';

export interface ShortcutEvent {
  key: string;
  ctrlKey?: boolean;
  metaKey?: boolean;
  shiftKey?: boolean;
}

export interface ShortcutContext {
  paletteOpen: boolean;
  /** A stream is in flight — only then does Escape mean "stop". */
  streaming: boolean;
  /** Focus is in an input / textarea / contenteditable. */
  typing: boolean;
}

/**
 * The app's global keyboard map (V4 §2), matching ChatGPT:
 *
 * - Ctrl/Cmd + K       → open the search palette (V3 gave this to new chat);
 * - Ctrl/Cmd + Shift+O → new chat;
 * - Escape             → close the palette if it is open, else stop the stream;
 * - "/"                → focus the composer, but never while typing or while
 *                        the modal owns the keyboard.
 *
 * Every other modifier chord returns null so browser and OS shortcuts keep
 * working.
 */
export function shortcutAction(
  event: ShortcutEvent,
  ctx: ShortcutContext,
): ShortcutAction | null {
  const mod = event.ctrlKey === true || event.metaKey === true;
  const shift = event.shiftKey === true;
  const key = event.key.toLowerCase();

  if (mod) {
    if (shift && key === 'o') return 'new-chat';
    // Ctrl+Shift+K is the browser's; only the bare chord opens the palette,
    // and re-pressing it while open is a no-op rather than a toggle.
    if (!shift && key === 'k') return ctx.paletteOpen ? null : 'open-search';
    return null;
  }

  if (event.key === 'Escape') {
    if (ctx.paletteOpen) return 'close-palette';
    return ctx.streaming ? 'stop-streaming' : null;
  }

  if (event.key === '/') {
    return ctx.paletteOpen || ctx.typing ? null : 'focus-composer';
  }

  return null;
}
