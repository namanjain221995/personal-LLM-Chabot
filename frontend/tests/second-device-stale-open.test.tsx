// @vitest-environment jsdom
/**
 * The second device shows what the first one sent (2026-10-02, chat media
 * e2e, 3 of 3 real-browser runs).
 *
 * Desktop B had the chat cached and open. Phone A then sent a new turn (a
 * photo and a PDF). B went to / and back to ?c=<conv>: it fetched the
 * conversation LIST but never the conversation, and kept rendering its cached
 * thread without A's turn. The owner's words: "open the chat on another device
 * and the photo is not there".
 *
 * The cause predates chat media (reproduced at origin/dev 3fead415): a
 * non-forced load served the cache whenever its ids matched what this browser
 * had last PUSHED, and nothing compared that with the list's `updated_at`,
 * which the refresh had just folded in.
 *
 * Everything here is REAL except the network: the real ChatApp, the real
 * history store (blob engine over an in-memory Storage that outlives a
 * "reload"), the real history API client. The server is a small in-memory one
 * that moves `updated_at` on every write, as the orchestrator does.
 */

import { act, cleanup, fireEvent, screen, waitFor } from '@testing-library/react';
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

const server = new Map<string, ServerConversation>();
let nextRowId = 1;
/** Every request, as `METHOD path`. */
let requests: string[] = [];
let chats = 0;

const stamp = (c: ServerConversation) =>
  new Date(Date.UTC(2026, 8, 13, 0, 0, c.version)).toISOString();

function addRows(id: string, rows: Omit<Row, 'id'>[]) {
  const c = server.get(id)!;
  for (const r of rows) c.rows.push({ ...r, id: nextRowId++ });
  c.version += 1;
}

/* ------------------------------------------------------- the real store */

// The REAL history module. Its Storage outlives the store, so dropping the
// singleton and building a new one over the same data is a page reload.
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

type Reply = { ok: boolean; status: number; body?: unknown; json?: unknown };
const ok = (json: unknown, status = 200): Reply => ({ ok: true, status, json });
const refuse = (status: number, json: unknown): Reply => ({ ok: false, status, json });

/** The Nth /api/chat answer's text. */
const streamed = (n: number) => `Streamed answer number ${n}.`;

function sse(text: string): Reply {
  const enc = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      c.enqueue(enc.encode(`event: token\ndata: ${JSON.stringify({ text })}\n\n`));
      c.enqueue(enc.encode('event: done\ndata: {}\n\n'));
      c.close();
    },
  });
  return { ok: true, status: 200, body };
}

function handleHistory(method: string, path: string, body: unknown): Reply {
  const parts = path.split('?')[0].split('/').filter(Boolean).map(decodeURIComponent);
  if (parts.length === 0) {
    if (method === 'GET') {
      if (path.includes('archived=true')) return ok([]);
      return ok(
        [...server.entries()].map(([id, c]) => ({ id, title: c.title, updated_at: stamp(c) })),
      );
    }
    if (method === 'POST') {
      const { id, title } = body as { id: string; title: string };
      if (server.has(id)) return refuse(409, { detail: 'exists' });
      server.set(id, { title, rows: [], version: 1 });
      return ok({ id }, 201);
    }
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
  if (sub === 'messages' && method === 'POST') {
    const m = body as { role: string; content: string; meta?: Record<string, unknown> };
    const row = { id: nextRowId++, role: m.role, content: m.content, meta: m.meta ?? null };
    c.rows.push(row);
    c.version += 1;
    return ok({ id: row.id });
  }
  if (sub === 'messages' && method === 'PUT') {
    const { messages, expected_updated_at } = body as {
      messages: { role: string; content: string; meta?: Record<string, unknown> }[];
      expected_updated_at?: string;
    };
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
      else if (u === '/api/chat') r = sse(streamed(++chats));
      else if (u.startsWith('/api/chat/')) r = refuse(404, { detail: 'not found' });
      else if (u.startsWith('/api/chat-media/')) r = ok({ items: [] });
      else if (u.startsWith('/api/history/conversations')) {
        const body =
          typeof init?.body === 'string' ? (JSON.parse(init.body) as unknown) : undefined;
        r = handleHistory(method, u.slice('/api/history/conversations'.length), body);
      } else r = ok({});
      return { ok: r.ok, status: r.status, body: r.body, json: async () => r.json ?? {} };
    }),
  );
}

/* ---------------------------------------------------------- the helpers */

const CONV = 'conv-photo';
const FIRST_ANSWER = 'The receipt totals 42 rupees.';
const A_QUESTION = 'And what does this second photo with the invoice say?';
const A_ANSWER = 'The second photo shows invoice 7731, due on Friday.';
const A_PHOTO = {
  attachment_id: 'img-phone-0002',
  name: 'invoice.jpg',
  mime: 'image/jpeg',
  width: 1200,
  height: 900,
};

/** What B saw before A wrote: a photo turn, its answer, and a thanks. */
function seedChat() {
  server.set(CONV, { title: 'Receipts', rows: [], version: 1 });
  addRows(CONV, [
    {
      role: 'user',
      content: 'What does this receipt total?',
      meta: {
        images: [
          {
            attachment_id: 'img-phone-0001',
            name: 'receipt.jpg',
            mime: 'image/jpeg',
            width: 800,
            height: 600,
          },
        ],
      },
    },
    { role: 'assistant', content: FIRST_ANSWER, meta: { route: 'vision' } },
    { role: 'user', content: 'Thanks.', meta: null },
    { role: 'assistant', content: 'You are welcome.', meta: { route: 'chat' } },
  ]);
}

/** Phone A's new turn: a photo and a PDF, and the answer to it. */
function phoneSendsATurn() {
  addRows(CONV, [
    {
      role: 'user',
      content: A_QUESTION,
      meta: {
        images: [A_PHOTO],
        attachments: [
          { id: 'f'.repeat(32), name: 'invoice.pdf', kind: 'pdf', attachment_id: 'doc-phone-0001' },
        ],
      },
    },
    { role: 'assistant', content: A_ANSWER, meta: { route: 'vision' } },
  ]);
}

async function settle(rounds = 10) {
  for (let i = 0; i < rounds; i += 1) {
    await act(async () => {
      await Promise.resolve();
    });
  }
}

/** Open the app on ?c=<conv> and wait until the thread is on screen. */
async function openApp(text = FIRST_ANSWER) {
  window.history.replaceState(null, '', `/?c=${CONV}`);
  renderApp(ChatApp, Providers);
  await screen.findByText(text, undefined, { timeout: 4000 });
  await settle();
  await act(async () => {
    await history.getHistoryStore().flush();
  });
  await settle();
}

/** Leave the page and come back to the same chat on the same browser. */
async function reload(text = FIRST_ANSWER) {
  cleanup();
  history.__reloadForTest();
  requests = [];
  await openApp(text);
}

const conversationGets = () =>
  requests.filter((r) => r === `GET /api/history/conversations/${CONV}`).length;
const listGets = () =>
  requests.filter((r) => r === 'GET /api/history/conversations').length;
const thumbs = () =>
  [...document.querySelectorAll('img[data-testid="stored-image"]')].map((n) =>
    n.getAttribute('src'),
  );

async function sendFromComposer(text: string, answer: string) {
  await act(async () => {
    fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), {
      target: { value: text },
    });
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
  });
  await screen.findByText(answer, undefined, { timeout: 4000 });
  await waitFor(() => expect(streamingIds()).toEqual([]), { timeout: 4000 });
  await act(async () => {
    await history.getHistoryStore().flush();
  });
  await settle();
}

/** One 8-second poll tick, run to completion. */
async function pollTick() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(8000);
  });
  await act(async () => {
    await history.getHistoryStore().flush();
  });
  await settle();
}

async function becomeVisible() {
  await act(async () => {
    document.dispatchEvent(new Event('visibilitychange'));
  });
  await settle();
  await act(async () => {
    await history.getHistoryStore().flush();
  });
  await settle();
}

beforeEach(() => {
  server.clear();
  nextRowId = 1;
  requests = [];
  chats = 0;
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
  Element.prototype.scrollTo = Element.prototype.scrollTo ?? (() => undefined);
  installFetch();
  // Only the interval: the 8-second poll is stepped by hand.
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
  window.localStorage.clear();
});

afterEach(async () => {
  await waitFor(() => expect(streamingIds()).toEqual([]), { timeout: 4000 });
  await act(async () => {
    await history.getHistoryStore().flush();
  });
  await settle(2);
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
});

/* ============================================================ the tests */

describe('a chat another device wrote to since this browser cached it', () => {
  it('reopening fetches it exactly once and shows the new turn with its stored photo', async () => {
    seedChat();
    await openApp();
    expect(screen.queryByText(A_ANSWER)).toBeNull();

    phoneSendsATurn();
    await reload();

    await screen.findByText(A_ANSWER, undefined, { timeout: 4000 });
    expect(screen.getByText(A_QUESTION)).toBeTruthy();
    expect(thumbs()).toContain(`/api/chat-media/${CONV}/img-phone-0002?size=thumb`);
    // The PDF chip rides meta.attachments, rebuilt from the server copy.
    expect(screen.getAllByText('invoice.pdf').length).toBeGreaterThan(0);
    expect(listGets()).toBe(1);
    expect(conversationGets()).toBe(1);

    // Learned once: the poll does not fetch it again while nothing changes.
    await pollTick();
    await pollTick();
    expect(conversationGets()).toBe(1);

    // And the cache now holds A's turn, so the next reopen needs no fetch.
    await reload(A_ANSWER);
    expect(conversationGets()).toBe(0);
    expect(thumbs()).toContain(`/api/chat-media/${CONV}/img-phone-0002?size=thumb`);
  });

  it('reopening a chat nobody touched makes no conversation request at all', async () => {
    seedChat();
    await openApp();
    expect(conversationGets()).toBe(1); // the first visit: nothing cached yet

    await reload();
    await pollTick();
    expect(listGets()).toBe(1);
    expect(conversationGets()).toBe(0);
  });

  it('a tab that comes back into view picks up the new turn on the open chat', async () => {
    seedChat();
    await openApp();
    requests = [];

    phoneSendsATurn();
    // While the tab sat in view, nothing changed what it shows: the poll does
    // not fetch the list or the conversation.
    await pollTick();
    expect(screen.queryByText(A_ANSWER)).toBeNull();
    expect(conversationGets()).toBe(0);

    await becomeVisible();
    await screen.findByText(A_ANSWER, undefined, { timeout: 4000 });
    expect(thumbs()).toContain(`/api/chat-media/${CONV}/img-phone-0002?size=thumb`);
    expect(listGets()).toBe(1);
    expect(conversationGets()).toBe(1);

    // Coming back again with nothing new asks for the list at most; the
    // conversation is not fetched again.
    await becomeVisible();
    await pollTick();
    expect(conversationGets()).toBe(1);
  });

  it('opening it from the sidebar asks the list once and fetches only the chat that moved on', async () => {
    // The clock too: the sidebar's list read is spaced by time.
    vi.useRealTimers();
    vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval', 'Date'], now: Date.now() });
    seedChat();
    server.set('conv-other', { title: 'Other chat', rows: [], version: 1 });
    addRows('conv-other', [
      { role: 'user', content: 'Unrelated question.', meta: null },
      { role: 'assistant', content: 'Unrelated answer.', meta: { route: 'chat' } },
    ]);
    await openApp();

    const click = async (title: string) => {
      const rows = await screen.findAllByRole('button', { name: title });
      await act(async () => {
        fireEvent.click(rows[0]);
      });
      await settle();
      await act(async () => {
        await history.getHistoryStore().flush();
      });
      await settle();
    };

    await click('Other chat');
    await screen.findByText('Unrelated answer.', undefined, { timeout: 4000 });
    phoneSendsATurn();
    await pollTick(); // time passes on the other chat; nothing is fetched for this one
    requests = [];

    await click('Receipts');
    await screen.findByText(A_ANSWER, undefined, { timeout: 4000 });
    expect(thumbs()).toContain(`/api/chat-media/${CONV}/img-phone-0002?size=thumb`);
    expect(listGets()).toBe(1);
    expect(conversationGets()).toBe(1);
    expect(requests.filter((r) => r.includes('conv-other') && r.startsWith('GET'))).toEqual([]);

    // Straight back and forth with nothing new: no list (too soon) and no fetch.
    await click('Other chat');
    await click('Receipts');
    expect(listGets()).toBe(1);
    expect(conversationGets()).toBe(1);
  });

  it('the open chat still streams normally after it was brought up to date', async () => {
    seedChat();
    await openApp();
    // This desktop's own turn first, so the cache holds turns only it named.
    await sendFromComposer('Is that with tax?', streamed(1));
    const ids = () => history.getHistoryStore().get(CONV)!.messages.map((m) => m.id);
    const before = ids();
    expect(before).toHaveLength(6);

    phoneSendsATurn();
    await reload(streamed(1));
    await screen.findByText(A_ANSWER, undefined, { timeout: 4000 });
    expect(conversationGets()).toBe(1);
    // The read kept the ids of the turns this browser already held (the view
    // keeps them too); only A's two new turns took server-numbered ones.
    expect(ids().slice(0, 6)).toEqual(before);
    expect(ids()).toHaveLength(8);

    await sendFromComposer('When is it due?', streamed(2));

    // A's turn stayed on screen under the new exchange, and the server holds
    // the whole thread in order.
    expect(screen.getByText(A_ANSWER)).toBeTruthy();
    expect(server.get(CONV)!.rows.map((r) => r.content)).toEqual([
      'What does this receipt total?',
      FIRST_ANSWER,
      'Thanks.',
      'You are welcome.',
      'Is that with tax?',
      streamed(1),
      A_QUESTION,
      A_ANSWER,
      'When is it due?',
      streamed(2),
    ]);
  });
});

describe('the store: what makes a cached thread stale', () => {
  function storeOver(get: (id: string) => Promise<unknown>, list: () => unknown[]) {
    const data = new Map<string, string>();
    const gets: string[] = [];
    const api = {
      update: async () => undefined,
      list: async () => list(),
      get: async (id: string) => {
        gets.push(id);
        return get(id);
      },
    } as unknown as import('@/lib/historyApi').HistoryApi;
    const store = history.createServerHistoryStore({
      storage: {
        getItem: (k) => data.get(k) ?? null,
        setItem: (k, v) => void data.set(k, v),
        removeItem: (k) => void data.delete(k),
      },
      api,
    });
    return { store, gets };
  }
  const at = (s: number) => new Date(Date.UTC(2026, 8, 13, 0, 0, s)).toISOString();
  const thread = (n: number) =>
    Array.from({ length: n }, (_, i) => ({
      id: i + 1,
      role: i % 2 ? 'assistant' : 'user',
      content: `turn ${i}`,
    }));

  it('a rename stamped with this browser clock does not hide a newer server write', async () => {
    let version = 2;
    const { store, gets } = storeOver(
      async (id) => ({ id, title: 'T', messages: thread(version * 2), updatedAt: at(version) }),
      () => [{ id: 'c1', title: 'T', updated_at: at(version) }],
    );
    await store.refreshActive?.();
    await store.load('c1');
    expect(gets).toEqual(['c1']);
    // Today's local clock is weeks past the server's stamps: updatedAt alone
    // would call every later list "older than what we hold".
    store.rename('c1', 'Renamed here');
    version = 3;
    await store.refreshActive?.();
    expect(store.isStale?.('c1')).toBe(true);
    expect((await store.load('c1'))?.messages).toHaveLength(6);
    expect(store.isStale?.('c1')).toBe(false);
    await store.refreshActive?.();
    await store.load('c1');
    expect(gets).toEqual(['c1', 'c1']);
  });

  it('a failed read stays stale for the next open; a 404 does not', async () => {
    let failWith: number | null = null;
    let version = 2;
    const { store, gets } = storeOver(
      async (id) => {
        if (failWith !== null) {
          throw new (await import('@/lib/historyApi')).HistoryApiError(failWith, 'nope');
        }
        return { id, title: 'T', messages: thread(4), updatedAt: at(version) };
      },
      () => [{ id: 'c1', title: 'T', updated_at: at(version) }],
    );
    await store.refreshActive?.();
    await store.load('c1');
    version = 3;
    await store.refreshActive?.();
    failWith = 0; // offline
    await store.load('c1');
    expect(store.isStale?.('c1')).toBe(true);
    failWith = 404;
    await store.load('c1');
    expect(store.isStale?.('c1')).toBe(false);
    expect(gets).toEqual(['c1', 'c1', 'c1']);
  });
});
