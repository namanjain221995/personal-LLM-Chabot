// @vitest-environment jsdom
/**
 * "TRY AGAIN" IS A VERSION (2026-09-13) — the duplicate answers.
 *
 * Production: nine "Try again" clicks on one question. Since V29 the server
 * stores every answer itself, before `done`, and the whole-thread PUT that
 * was meant to replace the old answer is refused (409). Regenerate still
 * truncated — but only when THIS TAB showed more than one answer, i.e. only
 * when an 8-second poll had loaded the server's rows. So one click deleted
 * six earlier answers with no confirmation, and the next two, six seconds
 * apart with no poll between them, stacked three copies of the same answer
 * with no `‹ 1 / 3 ›` control.
 *
 * Everything here is REAL except the network: the real ChatApp, the real
 * streams module, the real history store and its sync, the real history API
 * client. The network is a small in-memory SERVER that behaves the way the
 * orchestrator does since V29 — it stores each /chat answer itself (with
 * `meta.branch` from `answer_branch`, the server half of this fix), moves
 * `updated_at` when it does, refuses a stale conditional PUT with
 * `conversation changed`, refuses a shrinking PUT, and truncates only when
 * asked. What is asserted is what that server ends up HOLDING, and what the
 * page shows — never a label on its own.
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
  /** Monotonic, rendered as an ISO string exactly as the API sends it. */
  version: number;
}

const server = new Map<string, ServerConversation>();
let nextRowId = 1;
let generations = 0;
let chatPosts: Record<string, unknown>[] = [];
let truncateCalls: { id: string; body: unknown }[] = [];
let putOutcomes: number[] = [];
/**
 * When the server writes the answer relative to the browser's own save of the
 * accepted turn. `late` is production's usual order (a generation takes
 * seconds, the save milliseconds): that save lands, and the whole-thread PUT
 * after `done` is judged by the shrink guard alone. `early` is the order the
 * incident log shows (16 PUTs refused with 409): the server has moved on
 * before the browser's first save, so every conditional PUT is refused.
 */
let answerTiming: 'early' | 'late' = 'late';
/** The next POST /api/chat is answered with this status instead (then cleared). */
let failNextChatWith: number | null = null;
/** Conversations /api/chat/active reports as running. */
let activeIds: string[] = [];
/** What GET /api/chat/attach answers; 404 when unset. */
let attachReply: (() => Reply) | null = null;
/** Lets a held attach stream end, so a failing test cannot leak it. */
let releaseHeld: () => void = () => undefined;

const stamp = (c: ServerConversation) =>
  new Date(Date.UTC(2026, 8, 13, 0, 0, c.version)).toISOString();

function touch(c: ServerConversation) {
  c.version += 1;
}

function seed(id: string, rows: Omit<Row, 'id'>[]) {
  server.set(id, {
    title: 'Marigolds',
    rows: rows.map((r) => ({ ...r, id: nextRowId++ })),
    version: 1,
  });
}

/* ------------------------------------------------------- the real store */

// The REAL history module; only the singleton is swapped for one bound to an
// in-memory Storage, so each test starts from an empty browser.
vi.mock('@/lib/history', async (importOriginal) => {
  const actual = await importOriginal<typeof import('@/lib/history')>();
  let store: ReturnType<typeof actual.createServerHistoryStore> | null = null;
  return {
    ...actual,
    getHistoryStore: () => {
      if (!store) {
        const data = new Map<string, string>();
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
    __resetStoreForTest: () => {
      store = null;
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
  __resetStoreForTest: () => void;
};
const { ChatApp } = await import('@/components/ChatApp');
const { Providers } = await import('@/components/Providers');
const { renderApp } = await import('./_wireHarness');
const { getLiveStream, streamingIds } = await import('@/lib/streams');

/* ------------------------------------------------------------ the wire */

type Reply = { ok: boolean; status: number; body?: unknown; json?: unknown };
const ok = (json: unknown, status = 200): Reply => ({ ok: true, status, json });
const refuse = (status: number, json: unknown): Reply => ({ ok: false, status, json });

function sseReply(
  events: [string, unknown][],
  beforeDone: () => Promise<void>,
  /** How many events go out before `beforeDone` is awaited. */
  upFront = 1,
): Reply {
  const enc = new TextEncoder();
  const frame = (event: string, data: unknown) =>
    enc.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`);
  const stream = new ReadableStream<Uint8Array>({
    async start(c) {
      const leading = events.slice(0, upFront);
      const rest = events.slice(upFront);
      for (const e of leading) c.enqueue(frame(...e));
      // The server stores the answer at the END of a generation, long after
      // the browser has marked its turn accepted and saved that — so the
      // browser's own saves go first, exactly as in production.
      await beforeDone();
      for (const e of rest) c.enqueue(frame(...e));
      c.close();
    },
  });
  return { ok: true, status: 200, body: stream };
}

function handleChat(body: Record<string, unknown>): Reply {
  chatPosts.push(body);
  if (failNextChatWith !== null) {
    const status = failNextChatWith;
    failNextChatWith = null;
    return refuse(status, { code: 'MODEL_UNAVAILABLE' });
  }
  generations += 1;
  const n = generations;
  const conversationId = String(body.conversation_id);
  const intentId = String(body.intent_id);
  const generationId = `g${n}`;
  const branch = body.answer_branch as { self: string; parent?: string } | undefined;
  const content = `Marigold answer number ${n}.`;
  const finalMeta = {
    route: 'chat',
    generation_id: generationId,
    intent_id: intentId,
    attempt: 1,
  };
  return sseReply(
    [
      ['meta', { generation_id: generationId, intent_id: intentId, attempt: 1 }],
      ['token', { text: content }],
      ['meta', finalMeta],
      ['done', {}],
    ],
    async () => {
      if (answerTiming === 'late') {
        // Let the browser read the leading meta and queue its save, then let
        // that save reach the server before the answer does.
        for (let i = 0; i < 5; i += 1) await new Promise((r) => setTimeout(r, 0));
        await history.getHistoryStore().flush();
      }
      const c = server.get(conversationId);
      if (!c) return;
      // main.py _store_answer: the engine's meta, the generation id, and —
      // the server half of this fix — `meta.branch` from `answer_branch`.
      c.rows.push({
        id: nextRowId++,
        role: 'assistant',
        content,
        meta: { ...finalMeta, ...(branch ? { branch } : {}) },
      });
      touch(c);
    },
  );
}

function handleHistory(method: string, path: string, body: unknown): Reply {
  const parts = path.split('/').filter(Boolean).map(decodeURIComponent);
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
  const id = parts[0].split('?')[0];
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
  if (sub === 'truncate') {
    truncateCalls.push({ id, body });
    const { keep, expected_total } = body as { keep: number; expected_total: number };
    if (expected_total !== c.rows.length) {
      return refuse(409, { detail: `conversation changed: expected ${expected_total}` });
    }
    c.rows = c.rows.slice(0, keep);
    touch(c);
    return ok({ id, count: c.rows.length });
  }
  if (sub === 'messages' && parts.length === 2 && method === 'POST') {
    const m = body as { role: string; content: string; meta?: Record<string, unknown> };
    const gen = m.meta?.generation_id;
    const existing = gen ? c.rows.find((r) => r.meta?.generation_id === gen) : undefined;
    if (existing) return ok({ id: existing.id, deduplicated: true });
    const row = { id: nextRowId++, role: m.role, content: m.content, meta: m.meta ?? null };
    c.rows.push(row);
    touch(c);
    return ok({ id: row.id });
  }
  if (sub === 'messages' && parts.length === 2 && method === 'PUT') {
    // db.replace_messages: the conditional stamp, then the shrink guard.
    const { messages, expected_updated_at } = body as {
      messages: { role: string; content: string; meta?: Record<string, unknown> }[];
      expected_updated_at?: string;
    };
    if (expected_updated_at !== undefined && expected_updated_at !== stamp(c)) {
      putOutcomes.push(409);
      return refuse(409, {
        detail: 'conversation changed',
        updated_at: stamp(c),
        messages: c.rows.length,
      });
    }
    if (messages.length < c.rows.length) {
      putOutcomes.push(409);
      return refuse(409, {
        detail: `refusing to shrink conversation from ${c.rows.length} to ${messages.length} messages`,
      });
    }
    c.rows = messages.map((m) => ({
      id: nextRowId++,
      role: m.role,
      content: m.content,
      meta: m.meta ?? null,
    }));
    touch(c);
    putOutcomes.push(200);
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
      const body = init?.body ? JSON.parse(String(init.body)) : undefined;
      let r: Reply;
      if (u === '/api/chat/active') r = ok({ active: activeIds });
      else if (u.startsWith('/api/chat/requests/')) {
        const intent = decodeURIComponent(u.split('/').pop() as string);
        const known = chatPosts.find((p) => p.intent_id === intent);
        r = known
          ? ok({
              intent_id: intent,
              status: 'completed',
              generation_id: `g${chatPosts.indexOf(known) + 1}`,
              attempt: 1,
              resumable: false,
              answer_persisted: true,
              live: false,
            })
          : refuse(404, { detail: 'unknown intent' });
      } else if (u.startsWith('/api/chat/attach/')) {
        r = attachReply ? attachReply() : refuse(404, { detail: 'no active generation' });
      }
      else if (u === '/api/chat') r = handleChat(body as Record<string, unknown>);
      else if (u.startsWith('/api/history/conversations')) {
        r = handleHistory(method, u.slice('/api/history/conversations'.length), body);
      } else r = ok({});
      return {
        ok: r.ok,
        status: r.status,
        body: r.body,
        json: async () => r.json ?? {},
      };
    }),
  );
}

/* ---------------------------------------------------------- the helpers */

const QUESTION_BRANCH = 'b-question';

/** The conversation as production had it: one question, one answer. */
function seedAnsweredQuestion(id = 'conv-1') {
  seed(id, [
    {
      role: 'user',
      content: 'Tell me about marigolds.',
      meta: {
        branch: { self: QUESTION_BRANCH },
        intent: { id: 'i-first', state: 'completed' },
      },
    },
    {
      role: 'assistant',
      content: 'Marigold answer number 0.',
      // A composer send carries no answer_branch, so its row has none.
      meta: { route: 'chat', generation_id: 'g0', intent_id: 'i-first', attempt: 1 },
    },
  ]);
}

async function settle(rounds = 10) {
  for (let i = 0; i < rounds; i += 1) {
    await act(async () => {
      await Promise.resolve();
    });
  }
}

async function open(id = 'conv-1') {
  window.history.replaceState(null, '', `/?c=${id}`);
  renderApp(ChatApp, Providers);
  await screen.findByText('Marigold answer number 0.', undefined, { timeout: 4000 });
  await settle();
}

const shownAnswers = () =>
  [...document.querySelectorAll('[data-chat-message-role="assistant"]')].map(
    (n) => n.textContent ?? '',
  );

/** Press the Try again on the answer currently on screen, and let it finish. */
async function tryAgain(expectedAnswer: number) {
  const buttons = await screen.findAllByRole('button', { name: 'Try again' });
  await act(async () => {
    fireEvent.click(buttons[buttons.length - 1]);
  });
  await waitFor(
    () =>
      expect(shownAnswers().join(' ')).toContain(`Marigold answer number ${expectedAnswer}.`),
    { timeout: 4000 },
  );
  // The stream is over and every save it queued has reached the server.
  await waitFor(() => expect(screen.queryByRole('button', { name: 'Stop generating' })).toBeNull());
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

const serverRows = (id = 'conv-1') => server.get(id)?.rows ?? [];
const serverAnswers = (id = 'conv-1') => serverRows(id).filter((r) => r.role === 'assistant');

/** The shape a person would describe: what the server holds, what they see. */
function outcome() {
  return {
    rows: serverRows().map((r) => `${r.role}:${r.content}`),
    parents: serverAnswers().map(
      (r) => (r.meta?.branch as { parent?: string } | undefined)?.parent ?? null,
    ),
    truncates: truncateCalls.length,
    onScreen: shownAnswers().length,
    navigator: screen.queryAllByText(/^\d+ \/ \d+$/).map((n) => n.textContent),
    // ConfirmDialog is an alertdialog (the sidebar drawer is a plain dialog).
    dialog: screen.queryByRole('alertdialog'),
  };
}

beforeEach(() => {
  server.clear();
  nextRowId = 1;
  generations = 0;
  chatPosts = [];
  truncateCalls = [];
  putOutcomes = [];
  answerTiming = 'late';
  failNextChatWith = null;
  activeIds = [];
  attachReply = null;
  history.__resetStoreForTest();
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
  // Only the interval: the 8-second poll is what these tests step through.
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
  window.localStorage.clear();
});

afterEach(async () => {
  releaseHeld();
  releaseHeld = () => undefined;
  // A stream outlives its view (that is the product): let every one finish
  // and every save it queued land, so nothing from this test writes into the
  // next test's server.
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

describe.each(['late', 'early'] as const)('when the server stores the answer %s', (timing) => {
  beforeEach(() => {
    answerTiming = timing;
  });

  describe('Try again on an answered question', () => {
    it('three clicks with no poll between them make three versions of the answer, not three stacked copies', async () => {
      seedAnsweredQuestion();
      await open();

      await tryAgain(1);
      await tryAgain(2);
      await tryAgain(3);

      // The two timings really do take the two sync paths: every conditional
      // PUT refused, or the browser's saves accepted.
      if (timing === 'early') expect(new Set(putOutcomes)).toEqual(new Set([409]));
      else expect(putOutcomes).toContain(200);
      const seen = outcome();
      expect(seen.truncates).toBe(0);
      expect(seen.rows).toEqual([
        'user:Tell me about marigolds.',
        'assistant:Marigold answer number 0.',
        'assistant:Marigold answer number 1.',
        'assistant:Marigold answer number 2.',
        'assistant:Marigold answer number 3.',
      ]);
      // The first answer came from a plain send (no branch, so its parent is
      // positional — the question). Every regenerate names the question.
      expect(seen.parents).toEqual([null, QUESTION_BRANCH, QUESTION_BRANCH, QUESTION_BRANCH]);
      expect(seen.onScreen).toBe(1);
      expect(shownAnswers()[0]).toContain('Marigold answer number 3.');
      expect(seen.navigator).toEqual(['4 / 4']);
      expect(seen.dialog).toBeNull();
    });

    it('three clicks with a poll tick after each end in exactly the same versions', async () => {
      seedAnsweredQuestion();
      await open();

      await tryAgain(1);
      await pollTick();
      await tryAgain(2);
      await pollTick();
      await tryAgain(3);
      await pollTick();

      const seen = outcome();
      expect(seen.truncates).toBe(0);
      expect(seen.rows).toEqual([
        'user:Tell me about marigolds.',
        'assistant:Marigold answer number 0.',
        'assistant:Marigold answer number 1.',
        'assistant:Marigold answer number 2.',
        'assistant:Marigold answer number 3.',
      ]);
      expect(seen.parents).toEqual([null, QUESTION_BRANCH, QUESTION_BRANCH, QUESTION_BRANCH]);
      expect(seen.onScreen).toBe(1);
      expect(seen.navigator).toEqual(['4 / 4']);
      expect(seen.dialog).toBeNull();
    });

    it('sends every click as a new intent that names the question as the answer parent', async () => {
      seedAnsweredQuestion();
      await open();
      await tryAgain(1);
      await pollTick();
      await tryAgain(2);
      await tryAgain(3);

      const intents = chatPosts.map((p) => p.intent_id);
      expect(new Set(intents).size).toBe(3);
      for (const post of chatPosts) {
        expect(post.answer_branch).toEqual({
          self: `b-${String(post.intent_id)}`,
          parent: QUESTION_BRANCH,
        });
      }
    });

    it('a click after a poll tick on an OLDER version still deletes nothing and lands on the new version', async () => {
      seedAnsweredQuestion();
      await open();
      await tryAgain(1);
      await tryAgain(2);
      await tryAgain(3);
      await pollTick();

      // Back to the first answer…
      for (let i = 0; i < 3; i += 1) {
        await act(async () => {
          fireEvent.click(screen.getByRole('button', { name: 'Previous version' }));
        });
      }
      expect(screen.getByText('1 / 4')).toBeTruthy();
      expect(shownAnswers()[0]).toContain('Marigold answer number 0.');

      // …and Try again from there.
      await tryAgain(4);
      await pollTick();

      const seen = outcome();
      expect(seen.truncates).toBe(0);
      expect(serverAnswers()).toHaveLength(5);
      expect(seen.navigator).toEqual(['5 / 5']);
      expect(shownAnswers()[0]).toContain('Marigold answer number 4.');
      expect(seen.dialog).toBeNull();
    });
  });

  describe('Try again on an older answer with later turns', () => {
    it('keeps the later turns, reachable under the version they were asked on, with no confirmation', async () => {
      seed('conv-1', [
        {
          role: 'user',
          content: 'Tell me about marigolds.',
          meta: { branch: { self: QUESTION_BRANCH }, intent: { id: 'i-first', state: 'completed' } },
        },
        {
          role: 'assistant',
          content: 'Marigold answer number 0.',
          meta: { route: 'chat', generation_id: 'g0', intent_id: 'i-first' },
        },
        {
          role: 'user',
          content: 'And how often should I water them?',
          meta: { intent: { id: 'i-second', state: 'completed' } },
        },
        {
          role: 'assistant',
          content: 'Water marigolds once a week.',
          meta: { route: 'chat', generation_id: 'g-water', intent_id: 'i-second' },
        },
      ]);
      await open();
      expect(screen.getByText('Water marigolds once a week.')).toBeTruthy();

      // Try again on the FIRST answer, which used to ask "This will delete all
      // messages after this point (2 messages)" and then truncate.
      const buttons = await screen.findAllByRole('button', { name: 'Try again' });
      await act(async () => {
        fireEvent.click(buttons[0]);
      });
      expect(screen.queryByRole('alertdialog')).toBeNull();
      await waitFor(() =>
        expect(shownAnswers().join(' ')).toContain('Marigold answer number 1.'),
      );
      await act(async () => {
        await history.getHistoryStore().flush();
      });
      await pollTick();

      expect(truncateCalls).toHaveLength(0);
      expect(serverRows().map((r) => r.content)).toEqual([
        'Tell me about marigolds.',
        'Marigold answer number 0.',
        'And how often should I water them?',
        'Water marigolds once a week.',
        'Marigold answer number 1.',
      ]);
      // On screen: the new version, and the follow-up belongs to version 1.
      expect(screen.getByText('2 / 2')).toBeTruthy();
      expect(screen.queryByText('Water marigolds once a week.')).toBeNull();
      await act(async () => {
        fireEvent.click(screen.getByRole('button', { name: 'Previous version' }));
      });
      expect(screen.getByText('Water marigolds once a week.')).toBeTruthy();
      expect(screen.getByText('1 / 2')).toBeTruthy();
    });
  });
});

describe('a Try again on a question whose stored label is stale', () => {
  it('asks for a new answer instead of reusing an intent the server already answered', async () => {
    // The question's meta was last saved while its send was `accepted`; the
    // answer landed afterwards and carries that intent. Reusing the intent
    // would make the real server replay that answer: a click that does nothing.
    seed('conv-1', [
      {
        role: 'user',
        content: 'Tell me about marigolds.',
        meta: { branch: { self: QUESTION_BRANCH }, intent: { id: 'i-first', state: 'accepted' } },
      },
      {
        role: 'assistant',
        content: 'Marigold answer number 0.',
        meta: { route: 'chat', generation_id: 'g0', intent_id: 'i-first' },
      },
    ]);
    await open();
    await tryAgain(1);
    expect(chatPosts).toHaveLength(1);
    expect(chatPosts[0].intent_id).not.toBe('i-first');
    expect(chatPosts[0].answer_branch).toEqual({
      self: `b-${String(chatPosts[0].intent_id)}`,
      parent: QUESTION_BRANCH,
    });
  });
});

describe('a Try again whose request fails', () => {
  it('is retried as the same send: the same intent_id and the identical answer_branch', async () => {
    seedAnsweredQuestion();
    await open();
    failNextChatWith = 503;
    const buttons = await screen.findAllByRole('button', { name: 'Try again' });
    await act(async () => {
      fireEvent.click(buttons[buttons.length - 1]);
    });
    const retry = await screen.findByRole('button', { name: 'Retry' }, { timeout: 4000 });
    await act(async () => {
      await history.getHistoryStore().flush();
    });
    await act(async () => {
      fireEvent.click(retry);
    });
    await waitFor(() =>
      expect(shownAnswers().join(' ')).toContain('Marigold answer number 1.'),
    );
    await act(async () => {
      await history.getHistoryStore().flush();
    });
    await pollTick();

    expect(chatPosts).toHaveLength(2);
    expect(chatPosts[1].intent_id).toBe(chatPosts[0].intent_id);
    expect(chatPosts[1].answer_branch).toEqual(chatPosts[0].answer_branch);
    const branch = { self: `b-${String(chatPosts[0].intent_id)}`, parent: QUESTION_BRANCH };
    expect(chatPosts[0].answer_branch).toEqual(branch);
    const seen = outcome();
    expect(seen.truncates).toBe(0);
    // The failed attempt's record is stored by the browser (RC-2: a reload
    // must show the failure) and the server has no way to know the retry
    // replaced it, so it may still be there — under the SAME branch as the
    // answer, which is what makes it that answer's superseded attempt rather
    // than a version of its own.
    const answers = serverAnswers().filter((r) => !r.meta?.error);
    const records = serverAnswers().filter((r) => r.meta?.error);
    expect(answers.map((r) => r.content)).toEqual([
      'Marigold answer number 0.',
      'Marigold answer number 1.',
    ]);
    expect(answers[1].meta?.branch).toEqual(branch);
    for (const record of records) expect(record.meta?.branch).toEqual(branch);
    expect(seen.onScreen).toBe(1);
    expect(shownAnswers()[0]).toContain('Marigold answer number 1.');
    expect(seen.navigator).toEqual(['2 / 2']);
  });
});

describe('an ordinary send', () => {
  it('carries no answer_branch key in a conversation without versions', async () => {
    seedAnsweredQuestion();
    await open();
    await act(async () => {
      fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), {
        target: { value: 'And how often should I water them?' },
      });
      fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
    });
    await waitFor(() => expect(chatPosts).toHaveLength(1));
    await waitFor(() =>
      expect(shownAnswers().join(' ')).toContain('Marigold answer number 1.'),
    );
    expect('answer_branch' in chatPosts[0]).toBe(false);
    expect(typeof chatPosts[0].intent_id).toBe('string');
  });

  it('announces where its answer goes once the conversation has versions', async () => {
    seedAnsweredQuestion();
    await open();
    await tryAgain(1);
    await act(async () => {
      fireEvent.change(screen.getByRole('textbox', { name: 'Message' }), {
        target: { value: 'And how often should I water them?' },
      });
      fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
    });
    await waitFor(() => expect(chatPosts).toHaveLength(2));
    await waitFor(() =>
      expect(shownAnswers().join(' ')).toContain('Marigold answer number 2.'),
    );
    const followUp = chatPosts[1];
    const branch = followUp.answer_branch as { self: string; parent?: string };
    expect(branch.self).toBe(`b-${String(followUp.intent_id)}`);
    // The parent is the follow-up question's own branch id, not the answer
    // it follows.
    const stored = history.getHistoryStore().get('conv-1')?.messages ?? [];
    const asked = stored.find((m) => m.content === 'And how often should I water them?');
    expect(branch.parent).toBe(asked?.meta?.branch?.self);
  });
});

describe('a reload while a Try again is still generating', () => {
  /** Replays the leading meta and the text at once, then holds until released. */
  function heldAttach(intentId: string, text: string, branch: { self: string; parent?: string }) {
    let release!: () => void;
    const released = new Promise<void>((r) => {
      release = r;
    });
    releaseHeld = () => release();
    const meta = { route: 'chat', generation_id: 'g-live', intent_id: intentId, attempt: 1 };
    attachReply = () =>
      sseReply(
        [
          ['meta', { generation_id: 'g-live', intent_id: intentId, attempt: 1 }],
          ['token', { text }],
          ['meta', meta],
          ['done', {}],
        ],
        async () => {
          await released;
          const c = server.get('conv-1');
          if (!c) return;
          c.rows.push({ id: nextRowId++, role: 'assistant', content: text, meta: { ...meta, branch } });
          touch(c);
          activeIds = [];
        },
        2,
      );
    return () => release();
  }

  async function finish(release: () => void) {
    await act(async () => {
      release();
    });
    await waitFor(() => expect(streamingIds()).toEqual([]), { timeout: 4000 });
    await act(async () => {
      await history.getHistoryStore().flush();
    });
    await pollTick();
  }

  it('keeps the earlier version in the stream it re-joins, and ends as a version', async () => {
    const recorded = { self: 'b-i-regen', parent: QUESTION_BRANCH };
    seed('conv-1', [
      {
        role: 'user',
        content: 'Tell me about marigolds.',
        meta: {
          branch: { self: QUESTION_BRANCH },
          intent: { id: 'i-regen', state: 'accepted', answer_branch: recorded },
        },
      },
      {
        role: 'assistant',
        content: 'Marigold answer number 0.',
        meta: { route: 'chat', generation_id: 'g0', intent_id: 'i-first' },
      },
    ]);
    activeIds = ['conv-1'];
    const release = heldAttach('i-regen', 'Marigold answer number 9.', recorded);

    window.history.replaceState(null, '', '/?c=conv-1');
    renderApp(ChatApp, Providers);
    await screen.findByText('Marigold answer number 9.', undefined, { timeout: 4000 });
    // Mid-stream the re-joined generation holds the whole thread, and its
    // answer is filed under the branch the send recorded.
    const live = getLiveStream('conv-1');
    expect(live?.messages.map((m) => m.content)).toEqual([
      'Tell me about marigolds.',
      'Marigold answer number 0.',
      'Marigold answer number 9.',
    ]);
    expect(live?.messages[2].meta?.branch).toEqual(recorded);

    await finish(release);
    const seen = outcome();
    expect(seen.truncates).toBe(0);
    expect(seen.rows).toEqual([
      'user:Tell me about marigolds.',
      'assistant:Marigold answer number 0.',
      'assistant:Marigold answer number 9.',
    ]);
    expect(seen.parents).toEqual([null, QUESTION_BRANCH]);
    expect(seen.onScreen).toBe(1);
    expect(seen.navigator).toEqual(['2 / 2']);
  });

  it('draws a re-joined Try again of an OLDER answer under its own question, not the last one', async () => {
    const recorded = { self: 'b-i-regen', parent: QUESTION_BRANCH };
    seed('conv-1', [
      {
        role: 'user',
        content: 'Tell me about marigolds.',
        meta: {
          branch: { self: QUESTION_BRANCH },
          intent: { id: 'i-regen', state: 'accepted', answer_branch: recorded },
        },
      },
      {
        role: 'assistant',
        content: 'Marigold answer number 0.',
        meta: { route: 'chat', generation_id: 'g0', intent_id: 'i-first' },
      },
      {
        role: 'user',
        content: 'And how often should I water them?',
        meta: { intent: { id: 'i-second', state: 'completed' } },
      },
      {
        role: 'assistant',
        content: 'Water marigolds once a week.',
        meta: { route: 'chat', generation_id: 'g-water', intent_id: 'i-second' },
      },
    ]);
    activeIds = ['conv-1'];
    const release = heldAttach('i-regen', 'Marigold answer number 9.', recorded);

    window.history.replaceState(null, '', '/?c=conv-1');
    renderApp(ChatApp, Providers);
    await screen.findByText('Marigold answer number 9.', undefined, { timeout: 4000 });
    // On screen: the first question and its new answer — the follow-up
    // belongs to the version it was asked on and is not drawn above it.
    expect(screen.getByText('Tell me about marigolds.')).toBeTruthy();
    expect(screen.queryByText('And how often should I water them?')).toBeNull();
    expect(shownAnswers()).toHaveLength(1);

    await finish(release);
    expect(truncateCalls).toHaveLength(0);
    expect(serverRows().map((r) => r.content)).toEqual([
      'Tell me about marigolds.',
      'Marigold answer number 0.',
      'And how often should I water them?',
      'Water marigolds once a week.',
      'Marigold answer number 9.',
    ]);
    expect(screen.getByText('2 / 2')).toBeTruthy();
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Previous version' }));
    });
    expect(screen.getByText('Water marigolds once a week.')).toBeTruthy();
  });
});
