// @vitest-environment jsdom
/**
 * A photo the server stored by itself shows on every device
 * (docs/chat-media/STORE-ALWAYS.md §2, 2026-10-03).
 *
 * Production, 01:59 IST: the owner's phone tab predated the deploy, so its
 * old JavaScript sent the invoice photo with no `image_ids` and wrote no
 * `meta.images`. The server now stores such a photo under
 * `ix-<intent_id>-<index>`; the turn carries that intent. These pin the
 * browser's half: find those ids in the chat's list (one read per chat per
 * page load, never per turn), show them like any stored photo, write
 * `meta.images` once so the next load needs no list, and say "not stored"
 * only when the list holds nothing for the turn.
 *
 * Everything is REAL except the network: the real ChatApp, the real history
 * store (blob engine over an in-memory Storage that outlives a "reload"), the
 * real history API client, and a small server that moves `updated_at` on
 * every write and refuses a stale PUT, as the orchestrator does.
 *
 * Fixture ids are low-entropy on purpose (gitleaks, memory note "gitleaks
 * trips on fabricated UUID fixtures").
 */

import { act, cleanup, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

/* ---------------------------------------------------------- the server */

interface Row {
  id: number;
  role: string;
  content: string;
  meta: Record<string, unknown> | null;
}

interface ServerConversation {
  title: string;
  rows: Row[];
  version: number;
}

interface MediaItem {
  attachment_id: string;
  mime?: string;
  width?: number;
  height?: number;
}

const server = new Map<string, ServerConversation>();
let nextRowId = 1;
/** Every request, as `METHOD path`. */
let requests: string[] = [];
/** Every PUT body's messages. */
let puts: Array<Array<{ role: string; content: string; meta?: Record<string, unknown> }>> = [];
/** GET /api/chat-media/{conv}: the items, or null for a 502. */
let mediaItems: MediaItem[] | null = [];
/** Refuse the next N PUTs as "conversation changed" (another writer moved it). */
let movePuts = 0;

const stamp = (c: ServerConversation) =>
  new Date(Date.UTC(2026, 9, 3, 0, 0, c.version)).toISOString();

function addRows(id: string, rows: Omit<Row, 'id'>[]) {
  const c = server.get(id)!;
  for (const r of rows) c.rows.push({ ...r, id: nextRowId++ });
  c.version += 1;
}

/* ------------------------------------------------------- the real store */

vi.mock('@/lib/history', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/history')>();
  const data = new Map<string, string>();
  let store: ReturnType<typeof actual.createServerHistoryStore> | null = null;
  return {
    ...actual,
    getHistoryStore: () => {
      if (!store) {
        store = actual.createServerHistoryStore({
          storage: {
            getItem: (k) => data.get(k) ?? null,
            setItem: (k, v) => void data.set(k, v),
            removeItem: (k) => void data.delete(k),
          },
        });
      }
      return store;
    },
    __reloadForTest: () => {
      store = null;
    },
    __wipeForTest: () => {
      store = null;
      data.clear();
    },
  };
});
vi.mock('@/lib/auth', () => ({
  fetchMe: async () => ({ ok: true, username: 'tester', user: null, features: {} }),
  userScopeKey: () => 'tester',
  redirectToLogin: () => undefined,
  handleSessionEnd: async () => undefined,
  isAccessEnded: () => false,
}));
vi.mock('@/lib/salesforceApi', () => ({
  fetchSalesforceContext: async () => ({ options: [], pending: null }),
  cancelClarification: async () => undefined,
  shouldShowStarter: () => false,
}));
vi.mock('@/lib/compact', () => ({
  isCompacting: () => false,
  requestCompact: async () => null,
}));

const history = (await import('@/lib/history')) as typeof import('@/lib/history') & {
  __reloadForTest: () => void;
  __wipeForTest: () => void;
};
const { ChatApp } = await import('@/components/ChatApp');
const { Providers } = await import('@/components/Providers');
const { renderApp } = await import('./_wireHarness');
const { streamingIds } = await import('@/lib/streams');

/* ------------------------------------------------------------ the wire */

type Reply = { ok: boolean; status: number; json?: unknown };
const ok = (json: unknown, status = 200): Reply => ({ ok: true, status, json });
const refuse = (status: number, json: unknown): Reply => ({ ok: false, status, json });

function handleHistory(method: string, path: string, body: unknown): Reply {
  const parts = path.split('?')[0].split('/').filter(Boolean).map(decodeURIComponent);
  if (parts.length === 0) {
    if (method === 'GET') {
      if (path.includes('archived=true')) return ok([]);
      return ok(
        [...server.entries()].map(([id, c]) => ({ id, title: c.title, updated_at: stamp(c) })),
      );
    }
    return ok({});
  }
  const id = parts[0];
  const c = server.get(id);
  if (!c) return refuse(404, { detail: 'not found' });
  if (parts.length === 1) {
    if (method === 'GET') {
      return ok({ id, title: c.title, messages: c.rows, updated_at: stamp(c) });
    }
    return ok({ id });
  }
  const sub = parts[1];
  if (sub === 'title') return ok({ title: c.title, generated: false });
  if (sub === 'messages' && method === 'PUT') {
    const { messages, expected_updated_at } = body as {
      messages: { role: string; content: string; meta?: Record<string, unknown> }[];
      expected_updated_at?: string;
    };
    puts.push(messages);
    if (movePuts > 0) {
      movePuts -= 1;
      c.version += 1; // another writer moved it
      return refuse(409, { detail: 'conversation changed', updated_at: stamp(c) });
    }
    if (expected_updated_at !== undefined && expected_updated_at !== stamp(c)) {
      return refuse(409, { detail: 'conversation changed', updated_at: stamp(c) });
    }
    if (messages.length < c.rows.length) {
      return refuse(409, { detail: 'refusing to shrink conversation' });
    }
    c.rows = messages.map((m) => ({
      id: nextRowId++,
      role: m.role,
      content: m.content,
      meta: m.meta ?? null,
    }));
    c.version += 1;
    return ok({ id, count: c.rows.length });
  }
  return ok({});
}

function installFetch() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const u = String(url);
      const method = (init?.method ?? 'GET').toUpperCase();
      requests.push(`${method} ${u}`);
      let r: Reply;
      if (u === '/api/chat/active') r = ok({ active: [] });
      else if (u.startsWith('/api/chat/')) r = refuse(404, { detail: 'not found' });
      else if (u.startsWith('/api/chat-media/') && method === 'GET') {
        r = mediaItems === null ? refuse(502, { code: 'unreachable' }) : ok({ items: mediaItems });
      } else if (u.startsWith('/api/chat-media/')) r = refuse(404, { detail: 'not found' });
      else if (u.startsWith('/api/history/conversations')) {
        const body =
          typeof init?.body === 'string' ? (JSON.parse(init.body) as unknown) : undefined;
        r = handleHistory(method, u.slice('/api/history/conversations'.length), body);
      } else r = ok({});
      return { ok: r.ok, status: r.status, json: async () => r.json ?? {} };
    }),
  );
}

/* ---------------------------------------------------------- the helpers */

const CONV = 'conv-invoice';
/** newIntentId's shape (32 hex), low-entropy for the secret scanner. */
const INTENT = 'ab12'.repeat(8);
const OTHER_INTENT = 'cd34'.repeat(8);
const QUESTION = 'What is the total on this invoice?';
const ANSWER = 'The invoice totals 4,250 rupees.';

const IX = (i: number) => `ix-${INTENT}-${i}`;

/** The owner's turn as the OLD page saved it: an intent, no meta.images. */
function seedOldPageTurn() {
  server.set(CONV, { title: 'Invoice', rows: [], version: 1 });
  addRows(CONV, [
    { role: 'user', content: QUESTION, meta: { intent: { id: INTENT, state: 'completed' } } },
    { role: 'assistant', content: ANSWER, meta: { route: 'vision', intent_id: INTENT } },
  ]);
}

/** Two photos, listed oldest first but deliberately out of index order, plus noise. */
function storedTwoPhotos() {
  mediaItems = [
    { attachment_id: IX(1), mime: 'image/png', width: 800, height: 800 },
    { attachment_id: `ix-${OTHER_INTENT}-0`, mime: 'image/jpeg', width: 10, height: 10 },
    { attachment_id: IX(0), mime: 'image/jpeg', width: 1600, height: 1200 },
    { attachment_id: 'img-phone-0001', mime: 'image/jpeg', width: 640, height: 480 },
  ];
}

async function settle(rounds = 10) {
  for (let i = 0; i < rounds; i += 1) {
    await act(async () => {
      await Promise.resolve();
    });
  }
}

async function openApp(text = ANSWER) {
  window.history.replaceState(null, '', `/?c=${CONV}`);
  renderApp(ChatApp, Providers);
  await screen.findByText(text, undefined, { timeout: 4000 });
  await settle();
}

/** Run the backfill's idle tick and every push it causes to completion. */
async function drain() {
  await act(async () => {
    await new Promise((r) => setTimeout(r, 20));
  });
  await act(async () => {
    await history.getHistoryStore().flush();
  });
  await settle();
}

async function reload(text = ANSWER) {
  cleanup();
  history.__reloadForTest();
  requests = [];
  puts = [];
  await openApp(text);
}

const listReads = () => requests.filter((r) => r === `GET /api/chat-media/${CONV}`).length;
const thumbs = () =>
  [...document.querySelectorAll('img[data-testid="stored-image"]')].map((n) =>
    n.getAttribute('src'),
  );
const thumbFor = (i: number) => `/api/chat-media/${CONV}/${IX(i)}?size=thumb`;
const serverUserMeta = () => server.get(CONV)!.rows.find((r) => r.role === 'user')!.meta;

beforeEach(() => {
  server.clear();
  nextRowId = 1;
  requests = [];
  puts = [];
  mediaItems = [];
  movePuts = 0;
  history.__wipeForTest();
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: false,
    media: query,
    onchange: null,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => false,
  }));
  // Idle time arrives at once in a test.
  vi.stubGlobal('requestIdleCallback', (cb: () => void) => setTimeout(cb, 0));
  Element.prototype.scrollTo = Element.prototype.scrollTo ?? (() => undefined);
  installFetch();
  window.localStorage.clear();
});

afterEach(async () => {
  await waitFor(() => expect(streamingIds()).toEqual([]), { timeout: 4000 });
  await act(async () => {
    await history.getHistoryStore().flush();
  });
  await settle(2);
  cleanup();
  vi.unstubAllGlobals();
});

/* ============================================================ the tests */

describe('a second device opens a chat whose photo an old page sent', () => {
  it('shows the ix- photos in send order, then writes meta.images ONCE', async () => {
    seedOldPageTurn();
    storedTwoPhotos();
    await openApp();

    await waitFor(() => expect(thumbs()).toEqual([thumbFor(0), thumbFor(1)]));
    // The box is reserved from the listed size, like any meta.images thumb.
    const first = document.querySelector('img[data-testid="stored-image"]')!;
    expect(first.getAttribute('width')).toBe('1600');
    expect(first.getAttribute('height')).toBe('1200');
    expect(screen.queryByTestId('legacy-photo-note')).toBeNull();

    await drain();
    // One read of the list for the whole chat, one PUT carrying the refs.
    expect(listReads()).toBe(1);
    expect(puts).toHaveLength(1);
    expect(serverUserMeta()?.images).toEqual([
      { attachment_id: IX(0), mime: 'image/jpeg', width: 1600, height: 1200 },
      { attachment_id: IX(1), mime: 'image/png', width: 800, height: 800 },
    ]);
    // The intent is kept as it was.
    expect((serverUserMeta()?.intent as { id: string }).id).toBe(INTENT);

    // The next load needs no list: the reference is on the turn now.
    await reload();
    await drain();
    expect(thumbs()).toEqual([thumbFor(0), thumbFor(1)]);
    expect(listReads()).toBe(0);
    expect(puts).toHaveLength(0);
  });

  it('re-applies the reference once when its PUT is refused as stale (409)', async () => {
    seedOldPageTurn();
    storedTwoPhotos();
    movePuts = 1;
    await openApp();
    await waitFor(() => expect(thumbs()).toHaveLength(2));
    await drain();
    await drain();
    expect(puts.length).toBeGreaterThanOrEqual(2);
    expect(puts.length).toBeLessThanOrEqual(3);
    expect((serverUserMeta()?.images as unknown[]).length).toBe(2);
    expect(listReads()).toBe(1);
  });

  it('says "not stored" only when the list holds nothing for the turn', async () => {
    seedOldPageTurn();
    mediaItems = [{ attachment_id: `ix-${OTHER_INTENT}-0`, mime: 'image/png' }];
    await openApp();
    expect(await screen.findByTestId('legacy-photo-note')).toBeTruthy();
    expect(thumbs()).toEqual([]);
    await drain();
    // Nothing to write.
    expect(puts).toHaveLength(0);
  });

  it('says nothing while the list cannot be read, and shows nothing it does not know', async () => {
    seedOldPageTurn();
    mediaItems = null;
    await openApp();
    await waitFor(() => expect(listReads()).toBeGreaterThanOrEqual(1));
    await drain();
    expect(screen.queryByTestId('legacy-photo-note')).toBeNull();
    expect(thumbs()).toEqual([]);
    expect(puts).toHaveLength(0);
  });

  it('a chat open here picks up another device’s old-page photo turn on return', async () => {
    server.set(CONV, { title: 'Invoice', rows: [], version: 1 });
    addRows(CONV, [
      { role: 'user', content: 'Hello there.', meta: { intent: { id: OTHER_INTENT, state: 'completed' } } },
      { role: 'assistant', content: 'Hello! How can I help?', meta: { route: 'chat' } },
    ]);
    mediaItems = [];
    await openApp('Hello! How can I help?');
    await drain();
    expect(listReads()).toBe(1);

    // The phone's old page sends the photo; the server stores it as ix-.
    addRows(CONV, [
      { role: 'user', content: QUESTION, meta: { intent: { id: INTENT, state: 'completed' } } },
      { role: 'assistant', content: ANSWER, meta: { route: 'vision', intent_id: INTENT } },
    ]);
    mediaItems = [{ attachment_id: IX(0), mime: 'image/jpeg', width: 1600, height: 1200 }];

    await act(async () => {
      document.dispatchEvent(new Event('visibilitychange'));
    });
    await screen.findByText(ANSWER, undefined, { timeout: 4000 });
    await waitFor(() => expect(thumbs()).toEqual([thumbFor(0)]));
    expect(screen.queryByTestId('legacy-photo-note')).toBeNull();
    // The list is read again because the chat moved, not per turn.
    expect(listReads()).toBe(2);
    await drain();
    expect(serverUserMeta()).toEqual({ intent: { id: OTHER_INTENT, state: 'completed' } });
    const photoTurn = server.get(CONV)!.rows.find((r) => r.content === QUESTION)!;
    expect(photoTurn.meta?.images).toEqual([
      { attachment_id: IX(0), mime: 'image/jpeg', width: 1600, height: 1200 },
    ]);
  });

  it('never asks per turn: ten text turns with intents cost one list read', async () => {
    server.set(CONV, { title: 'Long', rows: [], version: 1 });
    for (let i = 0; i < 10; i += 1) {
      const intent = `${i}`.repeat(32);
      addRows(CONV, [
        { role: 'user', content: `question ${i}`, meta: { intent: { id: intent, state: 'completed' } } },
        { role: 'assistant', content: `answer ${i}`, meta: { route: 'chat', intent_id: intent } },
      ]);
    }
    await openApp('answer 9');
    await drain();
    expect(listReads()).toBe(1);
    expect(puts).toHaveLength(0);
  });
});
