/**
 * Typed client for the server-side history API (V2 §3c) as exposed through
 * the /api/history/* proxy. Pure fetch wrappers with an injectable fetch so
 * the sync logic in lib/history.ts is unit-testable offline.
 */

import type { Meta } from './types';

export interface ServerConversationSummary {
  id: string;
  title: string;
  updated_at?: unknown;
  created_at?: unknown;
  /** V3 §1 — booleans on new backends, absent on pre-V3 ones. */
  pinned?: unknown;
  archived?: unknown;
}

export interface ServerMessage {
  role: string;
  content: string;
  meta?: Meta | null;
  /** Server row id. Present on messages READ from the server; absent on ones
   *  being pushed to it (the server assigns it). It is the only stable handle
   *  a client has on a stored message — see MessageFeedback below. */
  id?: number;
  /** Thumbs, stored server-side since 2026-08-11. */
  feedback?: 'up' | 'down' | null;
}

export interface ServerConversation {
  id: string;
  title: string;
  messages: ServerMessage[];
}

export type FetchLike = (
  input: string,
  init?: RequestInit,
) => Promise<Response>;

/**
 * Normalizes whatever the server calls a timestamp into epoch milliseconds:
 * numeric seconds or milliseconds, an ISO string, or SQLite's naive-UTC
 * `CURRENT_TIMESTAMP` ("YYYY-MM-DD HH:MM:SS"). Unparseable values fall back.
 */
export function toEpoch(value: unknown, fallback: number): number {
  if (typeof value === 'number' && Number.isFinite(value)) {
    return value < 1e11 ? value * 1000 : value; // seconds vs milliseconds
  }
  if (typeof value === 'string' && value) {
    const normalized = /[zZ]|[+-]\d\d:?\d\d$/.test(value)
      ? value
      : `${value.replace(' ', 'T')}Z`;
    const t = Date.parse(normalized);
    if (!Number.isNaN(t)) return t;
  }
  return fallback;
}

/** status 0 = network failure; anything else is the HTTP status. */
export class HistoryApiError extends Error {
  constructor(
    public status: number,
    message: string,
  ) {
    super(message);
    this.name = 'HistoryApiError';
  }
}

export function isNotFound(err: unknown): boolean {
  return err instanceof HistoryApiError && err.status === 404;
}

/** 409 — the id already exists (create), or a replace would SHRINK a thread. */
export function isConflict(err: unknown): boolean {
  return err instanceof HistoryApiError && err.status === 409;
}

/** Network failure (as opposed to a rejection the server actually sent). */
export function isUnreachable(err: unknown): boolean {
  return !(err instanceof HistoryApiError) || err.status === 0;
}

/** V3 §1: `PUT /history/conversations/{id}` accepts any subset of these. */
export interface ConversationPatch {
  title?: string;
  pinned?: boolean;
  archived?: boolean;
}

export interface ListOptions {
  /** V3 §1: false/omitted = active chats only, true = archived only. */
  archived?: boolean;
}

export interface HistoryApi {
  list(options?: ListOptions): Promise<ServerConversationSummary[]>;
  get(id: string): Promise<ServerConversation>;
  create(id: string | undefined, title: string): Promise<void>;
  update(id: string, patch: ConversationPatch): Promise<void>;
  remove(id: string): Promise<void>;
  appendMessage(id: string, message: ServerMessage): Promise<void>;
  /**
   * Thumbs up/down on one stored message, or null to clear it.
   *
   * Keyed by the SERVER message id, which is why `ServerMessage.id` exists:
   * the browser's own ids are useless here — a live message carries a random
   * uuid and a rehydrated one a positional `srv-<conversation>-<index>`, so
   * feedback kept client-side was silently lost on reload.
   */
  setFeedback(
    id: string,
    messageId: number,
    feedback: 'up' | 'down' | null,
  ): Promise<void>;
  /**
   * Replace the whole thread atomically. The server REFUSES (409) when the
   * incoming thread is shorter than what it stores — the guard that stops a
   * stale local cache from destroying a conversation.
   */
  replaceMessages(id: string, messages: ServerMessage[]): Promise<void>;
  /**
   * Drop every message after the first `keep` — the only sanctioned shrink,
   * used exclusively by a user-confirmed regenerate. `expectedTotal` guards
   * against deleting turns another tab appended (409).
   */
  truncateMessages(
    id: string,
    keep: number,
    expectedTotal: number,
  ): Promise<void>;
}

const BASE = '/api/history/conversations';

export function createHistoryApi(fetchFn?: FetchLike): HistoryApi {
  const doFetch: FetchLike = fetchFn ?? ((input, init) => fetch(input, init));

  async function request(
    method: string,
    path: string,
    body?: unknown,
  ): Promise<unknown> {
    let res: Response;
    try {
      res = await doFetch(`${BASE}${path}`, {
        method,
        cache: 'no-store',
        ...(body !== undefined
          ? {
              headers: { 'Content-Type': 'application/json' },
              body: JSON.stringify(body),
            }
          : {}),
      });
    } catch {
      throw new HistoryApiError(0, 'History server unreachable.');
    }
    if (!res.ok) {
      throw new HistoryApiError(
        res.status,
        `History request failed with status ${res.status}.`,
      );
    }
    try {
      return await res.json();
    } catch {
      return null;
    }
  }

  return {
    async list(options) {
      const query = options?.archived ? '?archived=true' : '';
      const body = await request('GET', query);
      return Array.isArray(body) ? (body as ServerConversationSummary[]) : [];
    },
    async get(id) {
      const body = await request('GET', `/${encodeURIComponent(id)}`);
      const conv = (body ?? {}) as Partial<ServerConversation>;
      return {
        id: typeof conv.id === 'string' ? conv.id : id,
        title: typeof conv.title === 'string' ? conv.title : 'Conversation',
        messages: Array.isArray(conv.messages) ? conv.messages : [],
      };
    },
    async create(id, title) {
      await request('POST', '', id !== undefined ? { id, title } : { title });
    },
    async update(id, patch) {
      await request('PUT', `/${encodeURIComponent(id)}`, patch);
    },
    async remove(id) {
      await request('DELETE', `/${encodeURIComponent(id)}`);
    },
    async appendMessage(id, message) {
      await request('POST', `/${encodeURIComponent(id)}/messages`, message);
    },
    async setFeedback(id, messageId, feedback) {
      await request(
        'PUT',
        `/${encodeURIComponent(id)}/messages/${messageId}/feedback`,
        { feedback },
      );
    },
    async replaceMessages(id, messages) {
      await request('PUT', `/${encodeURIComponent(id)}/messages`, { messages });
    },
    async truncateMessages(id, keep, expectedTotal) {
      await request('POST', `/${encodeURIComponent(id)}/truncate`, {
        keep,
        expected_total: expectedTotal,
      });
    },
  };
}

/* --------------------------------------------------- V4 §2: chat search */

/** One conversation that matched, as `GET /history/search` returns it. */
export interface ServerSearchResult {
  id: string;
  title: string;
  updated_at?: unknown;
  pinned?: unknown;
  archived?: unknown;
  /** ~120-char window around the hit; null for title-only matches. */
  snippet?: unknown;
  matched_in?: unknown;
}

export interface SearchOptions {
  limit?: number;
  /** Aborting a superseded search is how the palette avoids stale results. */
  signal?: AbortSignal;
  fetchFn?: FetchLike;
}

/**
 * `GET /history/search?q=&limit=` through the cookie-forwarding proxy.
 *
 * Deliberately NOT part of `HistoryApi`: that interface is the offline-sync
 * store's contract (and is stubbed wholesale in tests), while search is a
 * live-only, read-only lookup with no cache behind it.
 *
 * Rejects with `HistoryApiError` like the rest of this client; an abort
 * rejects with the original `AbortError` so the caller can tell "superseded"
 * apart from "failed".
 */
export async function searchConversations(
  query: string,
  options: SearchOptions = {},
): Promise<unknown> {
  const params = new URLSearchParams({ q: query });
  if (options.limit !== undefined) params.set('limit', String(options.limit));
  const doFetch: FetchLike =
    options.fetchFn ?? ((input, init) => fetch(input, init));

  let res: Response;
  try {
    res = await doFetch(`/api/history/search?${params.toString()}`, {
      cache: 'no-store',
      signal: options.signal,
    });
  } catch (err) {
    if (err instanceof Error && err.name === 'AbortError') throw err;
    throw new HistoryApiError(0, 'History server unreachable.');
  }
  if (!res.ok) {
    throw new HistoryApiError(
      res.status,
      `Search failed with status ${res.status}.`,
    );
  }
  try {
    return await res.json();
  } catch {
    return null;
  }
}
