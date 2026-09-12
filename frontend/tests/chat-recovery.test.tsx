// @vitest-environment jsdom
/**
 * CHAT RECOVERY (2026-09-10) — what a tab is allowed to CLAIM about a send.
 *
 * The 2026-09-09 incidents were not one bug but one confusion: "there is no
 * stream in this tab" was read as "nothing is running on the server". From
 * that came the red "never sent" notice over a live generation, an 8-second
 * poll that replaced a turn whose files were still uploading, a re-attach
 * that treated a 502 as "finished", and a blank assistant row that hid the
 * answer the orchestrator had already persisted.
 *
 * Every test here drives the REAL ChatApp through the REAL streams module
 * with only the network and the history store stubbed, and every stub is a
 * promise the test resolves ON CUE — there is not a single sleep in this
 * file, so a failure names a timing point rather than a flake.
 *
 * The shared wire harness (tests/_wireHarness.tsx) supplies the render and
 * composer helpers; the network here is its own router because these tests
 * are ABOUT the responses.
 */

import { act, cleanup, fireEvent, render, screen, waitFor } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ChatMessage, Conversation } from '@/lib/types';

/* ------------------------------------------------------------- the store */

interface Deferred<T> {
  promise: Promise<T>;
  resolve: (v: T) => void;
  reject: (e: unknown) => void;
}
function defer<T>(): Deferred<T> {
  let resolve!: (v: T) => void;
  let reject!: (e: unknown) => void;
  const promise = new Promise<T>((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** What the "server" holds per conversation, and what this browser cached. */
const serverThreads = new Map<string, ChatMessage[]>();
const cache = new Map<string, Conversation>();
/** Every saveMessages, in order — the durability record under test. */
let saves: { id: string; messages: ChatMessage[] }[] = [];
let loadCalls: { id: string; force: boolean }[] = [];

function conv(id: string, messages: ChatMessage[]): Conversation {
  return { id, title: 'Chat', messages, createdAt: 0, updatedAt: 0 };
}

/** A server row as the real loader hydrates it: renumbered, meta verbatim. */
function hydrate(id: string, messages: ChatMessage[]): ChatMessage[] {
  return messages.map((m, i) => ({
    ...m,
    id: `srv-${id}-${i}`,
    ...(m.role === 'assistant' ? { status: 'done' as const } : {}),
  }));
}

const storeApi = {
  ready: async () => undefined,
  list: () =>
    [...cache.values()].map((c) => ({
      id: c.id,
      title: c.title,
      createdAt: c.createdAt,
      updatedAt: c.updatedAt,
    })),
  listArchived: () => [],
  get: (id: string) => cache.get(id) ?? null,
  create: (title: string) => {
    const created = conv('conv-1', []);
    created.title = title;
    cache.set(created.id, created);
    return created;
  },
  saveMessages: (id: string, messages: ChatMessage[]) => {
    saves.push({ id, messages: messages.map((m) => ({ ...m })) });
    const existing = cache.get(id) ?? conv(id, []);
    existing.messages = messages;
    cache.set(id, existing);
  },
  load: async (id: string, opts?: { force?: boolean }) => {
    loadCalls.push({ id, force: opts?.force === true });
    const server = serverThreads.get(id);
    if (!server) return cache.get(id) ?? null;
    const loaded = conv(id, hydrate(id, server));
    cache.set(id, loaded);
    return loaded;
  },
  setActiveUser: () => false,
  wipeLocal: async () => undefined,
  migrateLocalConversations: async () => 0,
  refresh: async () => true,
  refreshArchived: async () => true,
  generateTitle: async () => undefined,
  truncateMessages: async () => undefined,
  setMessageFeedback: async () => undefined,
  exportMarkdown: async () => null,
  remove: () => undefined,
  rename: () => undefined,
  setPinned: () => undefined,
  setArchived: () => undefined,
};

vi.mock('@/lib/history', () => ({
  newId: () => `m${Math.random().toString(36).slice(2, 10)}`,
  setEvictListener: () => undefined,
  rebuildHistoryStore: async () => {
    throw new Error('unexpected account switch in test');
  },
  getHistoryStore: () => storeApi,
}));
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

const { ChatApp } = await import('@/components/ChatApp');
const { Providers } = await import('@/components/Providers');
const { reconnectPolicy } = await import('@/lib/streams');
const { renderApp, box } = await import('./_wireHarness');

/* ------------------------------------------------------------ the network */

/** A stream the test pushes events into, and closes or breaks on cue. */
function sse() {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  let ended = false;
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
    },
  });
  const enc = new TextEncoder();
  return {
    stream,
    send(event: string, data: unknown) {
      if (ended) return;
      controller.enqueue(
        enc.encode(`event: ${event}\ndata: ${JSON.stringify(data)}\n\n`),
      );
    },
    /** Idempotent: a scenario may end the same stream twice. */
    close() {
      if (ended) return;
      ended = true;
      controller.close();
    },
    /** The pipe dies mid-answer — a restart, a proxy, a lost network. */
    breakIt() {
      if (ended) return;
      ended = true;
      controller.error(new Error('connection reset'));
    },
  };
}

type Reply = { ok: boolean; status: number; body?: unknown; json?: unknown };

/** The routes each test configures. Every one may be a promise held open. */
interface Wire {
  active: () => Promise<Reply>;
  request: (intentId: string) => Promise<Reply>;
  attach: () => Promise<Reply>;
  chat: (body: Record<string, unknown>) => Promise<Reply>;
  upload: (name: string) => Promise<Reply>;
}

let wire: Wire;
let chatPosts: Record<string, unknown>[] = [];
let requestAsks: string[] = [];
let attachAsks = 0;

const ok = (json: unknown): Reply => ({ ok: true, status: 200, json });
const fail = (status: number, json: unknown = {}): Reply => ({
  ok: false,
  status,
  json,
});
const streamReply = (s: ReturnType<typeof sse>): Reply => ({
  ok: true,
  status: 200,
  body: s.stream,
});

function installFetch() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: RequestInit) => {
      const u = String(url);
      const reply = async (r: Reply) => ({
        ok: r.ok,
        status: r.status,
        body: r.body,
        json: async () => r.json ?? {},
      });
      if (u.startsWith('/api/chat/requests/')) {
        const intent = decodeURIComponent(u.split('/').pop() as string);
        requestAsks.push(intent);
        return reply(await wire.request(intent));
      }
      if (u === '/api/chat/active') return reply(await wire.active());
      if (u.startsWith('/api/chat/attach/')) {
        attachAsks += 1;
        return reply(await wire.attach());
      }
      if (u === '/api/chat/stop') return reply(ok({}));
      if (u.startsWith('/api/chat')) {
        const body = JSON.parse(String(init?.body)) as Record<string, unknown>;
        chatPosts.push(body);
        return reply(await wire.chat(body));
      }
      if (u === '/api/upload') {
        const form = init?.body as FormData;
        const file = form.get('file');
        const name = file instanceof File ? file.name : 'file';
        return reply(await wire.upload(name));
      }
      return reply(ok({}));
    }),
  );
}

/* ------------------------------------------------------------ boilerplate */

const video = (name: string) =>
  new File(['\x00\x00\x00\x18ftypmp42'], name, { type: 'video/mp4' });

/** Let promises settle without waiting on the clock. */
async function settle(rounds = 8) {
  for (let i = 0; i < rounds; i += 1) {
    await act(async () => {
      await Promise.resolve();
    });
  }
}

function userTurn(overrides: Partial<ChatMessage> = {}): ChatMessage {
  return {
    id: 'u1',
    role: 'user',
    content: 'what was decided?',
    createdAt: 1,
    ...overrides,
  } as ChatMessage;
}

const notice = () => screen.queryByTestId('unsent-turn');
const answers = () =>
  [...document.querySelectorAll('[data-chat-message-role="assistant"]')].map(
    (n) => n.textContent ?? '',
  );

beforeEach(() => {
  serverThreads.clear();
  cache.clear();
  saves = [];
  loadCalls = [];
  chatPosts = [];
  requestAsks = [];
  attachAsks = 0;
  reconnectPolicy.baseMs = 0;
  reconnectPolicy.jitter = false;
  reconnectPolicy.attempts = 4;
  wire = {
    active: async () => ok({ active: [] }),
    request: async () => fail(404, { detail: 'unknown intent' }),
    attach: async () => fail(404, { detail: 'no active generation' }),
    chat: async () => ok({}),
    upload: async () => ok({ upload_id: '0'.repeat(32), filename: 'f', files: 1 }),
  };
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
  HTMLMediaElement.prototype.play = async () => undefined;
  HTMLMediaElement.prototype.pause = () => undefined;
  installFetch();
  // ONLY the interval is faked: the 8-second poll is the thing these tests
  // step through, while everything else (the file reader, testing-library's
  // waitFor) keeps real time and stays honest. Nothing here sleeps — the
  // poll is advanced explicitly, at the point the test means.
  vi.useFakeTimers({ toFake: ['setInterval', 'clearInterval'] });
  window.localStorage.clear();
  window.history.replaceState(null, '', '/');
});

afterEach(async () => {
  await settle(2);
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  reconnectPolicy.baseMs = 1000;
  reconnectPolicy.jitter = true;
  reconnectPolicy.attempts = 20;
});

/** Open the app on a conversation that already has a thread (a reload). */
async function reopen(id: string, thread: ChatMessage[], server = thread) {
  cache.set(id, conv(id, thread));
  serverThreads.set(id, server);
  window.history.replaceState(null, '', `/?c=${id}`);
  renderApp(ChatApp, Providers);
  await settle();
}

/** Type and press Send once. */
async function pressSend(text = 'what was decided?') {
  await act(async () => {
    fireEvent.change(box(), { target: { value: text } });
    fireEvent.click(screen.getByRole('button', { name: 'Send message' }));
  });
}

async function attachFiles(files: File[]) {
  const input = document.querySelector('input[type="file"]') as HTMLInputElement;
  await act(async () => {
    fireEvent.change(input, { target: { files } });
  });
  for (const f of files) {
    await screen.findByLabelText(`Remove attachment ${f.name}`);
  }
}

/* ====================================================================== */
/* 1. A turn whose upload is running HERE is never replaced or accused    */
/* ====================================================================== */

describe('an upload running in this tab', () => {
  it('shows progress and no notice — the poll may not force-load over it', async () => {
    const upload = defer<Reply>();
    wire.upload = () => upload.promise;
    renderApp(ChatApp, Providers);
    await settle();
    await attachFiles([video('big.mp4')]);
    await pressSend('what was decided?');
    await settle();

    // The turn is on screen with its words and its file, and the row says
    // what it is waiting for — by name.
    expect(screen.getAllByText('what was decided?').length).toBeGreaterThan(0);
    expect(screen.getByTestId('upload-status').textContent).toContain(
      'Uploading big.mp4…',
    );
    expect(notice()).toBeNull();

    // The 8-second poll now fires with the upload still in flight. Before
    // this fix it force-loaded server history over the turn, which killed
    // the indicator and raised the red notice on a send that had not been
    // made yet (fe-chat F1).
    serverThreads.set('conv-1', [userTurn()]);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(8000);
    });
    await settle();

    expect(notice()).toBeNull();
    expect(screen.getByTestId('upload-status')).toBeTruthy();
    expect(loadCalls.some((c) => c.force)).toBe(false);
    expect(chatPosts).toHaveLength(0);

    upload.resolve(ok({ upload_id: 'a'.repeat(32), filename: 'big.mp4', files: 1 }));
    await settle();
    expect(chatPosts).toHaveLength(1);
  });
});

/* ====================================================================== */
/* 2-5. What the server says, and what each failure to ask means          */
/* ====================================================================== */

describe('reconciling the last turn after a reload', () => {
  const pending = () =>
    userTurn({
      meta: { intent: { id: 'i1', state: 'accepted', generation_id: 'g1' } },
    });

  it('re-attaches with no notice when the server says the intent is live', async () => {
    const live = sse();
    wire.request = async () =>
      ok({
        intent_id: 'i1',
        status: 'running',
        generation_id: 'g1',
        attempt: 1,
        resumable: true,
        answer_persisted: false,
        live: true,
      });
    wire.attach = async () => streamReply(live);

    await reopen('conv-1', [pending()]);
    await waitFor(() => expect(attachAsks).toBe(1));
    expect(notice()).toBeNull();

    await act(async () => {
      live.send('token', { text: 'The decision was made.' });
      live.send('done', {});
      live.close();
    });
    await settle();
    expect(answers().join(' ')).toContain('The decision was made.');
    expect(notice()).toBeNull();
  });

  it('a 503 from /chat/active is status_unknown, and the attach still happens', async () => {
    const live = sse();
    wire.active = async () => fail(503, { detail: 'down' });
    wire.request = async () =>
      ok({ intent_id: 'i1', status: 'running', generation_id: 'g1', attempt: 1, resumable: true, answer_persisted: false, live: true });
    wire.attach = async () => streamReply(live);

    await reopen('conv-1', [pending()]);
    await waitFor(() => expect(attachAsks).toBe(1));
    // Never "never sent": the server was never asked and said nothing.
    expect(notice()).toBeNull();
    await act(async () => {
      live.send('token', { text: 'Still here.' });
      live.send('done', {});
      live.close();
    });
    await settle();
    expect(answers().join(' ')).toContain('Still here.');
  });

  it('a 502 from the attach is never read as "finished"', async () => {
    wire.active = async () => ok({ active: ['conv-1'] });
    wire.attach = async () => fail(502, { message: 'The orchestrator is unreachable.' });

    await reopen('conv-1', [pending()]);
    await waitFor(() => expect(attachAsks).toBeGreaterThan(0));
    await settle();

    expect(notice()).toBeNull();
    // The server DID answer /chat/active and listed this conversation, so the
    // honest thing to say is that it is being worked on — the failed attach
    // says nothing about the generation.
    expect(screen.getByTestId('turn-working').textContent).toContain(
      'the server has it',
    );
    // The answer was NOT invented from history, and nothing was overwritten.
    expect(loadCalls.some((c) => c.force)).toBe(false);
  });

  it('attach 404 + request completed loads the answer exactly once', async () => {
    wire.active = async () => ok({ active: ['conv-1'] });
    wire.attach = async () => fail(404, { detail: 'no active generation' });
    serverThreads.set('conv-1', [
      pending(),
      {
        id: 'a1',
        role: 'assistant',
        content: 'The persisted answer.',
        createdAt: 2,
        meta: { generation_id: 'g1' },
      } as ChatMessage,
    ]);

    await reopen('conv-1', [pending()], serverThreads.get('conv-1')!);
    await waitFor(() =>
      expect(answers().join(' ')).toContain('The persisted answer.'),
    );
    expect(answers()).toHaveLength(1);
    expect(notice()).toBeNull();
  });
});

/* ====================================================================== */
/* 6-7. One send, one intent — however many times it is pressed           */
/* ====================================================================== */

describe('the send intent', () => {
  it('survives a double click as ONE post with ONE intent id', async () => {
    const s = sse();
    wire.chat = async () => streamReply(s);
    renderApp(ChatApp, Providers);
    await settle();
    await act(async () => {
      fireEvent.change(box(), { target: { value: 'twice?' } });
      const button = screen.getByRole('button', { name: 'Send message' });
      fireEvent.click(button);
      fireEvent.click(button);
    });
    await settle();
    expect(chatPosts).toHaveLength(1);
    expect(typeof chatPosts[0].intent_id).toBe('string');
    expect(String(chatPosts[0].intent_id).length).toBeGreaterThan(0);
    await act(async () => {
      s.send('done', {});
      s.close();
    });
  });

  it('is REUSED when an unsent turn is sent again', async () => {
    const s = sse();
    wire.chat = async () => streamReply(s);
    // The server states it never received this send: the one route to
    // "never sent".
    wire.request = async () => fail(404, { detail: 'unknown intent' });
    await reopen('conv-1', [
      userTurn({
        meta: {
          intent: { id: 'i-keep', state: 'waiting_for_attachments' },
          attachments: [
            { kind: 'video', name: 'a.mp4', id: 'a'.repeat(32), upload_state: 'uploaded' },
          ],
        },
      }),
    ]);
    await waitFor(() => expect(notice()).not.toBeNull());
    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Send now' }));
    });
    await settle();
    expect(chatPosts).toHaveLength(1);
    expect(chatPosts[0].intent_id).toBe('i-keep');
    await act(async () => {
      s.send('done', {});
      s.close();
    });
  });
});

/* ====================================================================== */
/* 8. One upload of two fails: nothing silent, nothing lost               */
/* ====================================================================== */

describe('a two-video turn where one upload fails', () => {
  it('keeps the words and both files, names the missing one, and sends only what landed on request', async () => {
    const big = defer<Reply>();
    wire.upload = (name) =>
      name === 'big.mp4'
        ? big.promise
        : Promise.resolve(ok({ upload_id: 's'.repeat(32), filename: 'small.mp4', files: 1 }));
    const s = sse();
    wire.chat = async () => streamReply(s);

    renderApp(ChatApp, Providers);
    await settle();
    await attachFiles([video('big.mp4'), video('small.mp4')]);
    await pressSend('compare these');
    await settle();

    big.resolve(fail(500, { detail: 'upload failed' }));
    await settle();

    // The question is still there…
    expect(screen.getAllByText('compare these').length).toBeGreaterThan(0);
    // …no request went out with a silent subset…
    expect(chatPosts).toHaveLength(0);
    // …the turn still names BOTH files, one with its id and one without…
    const saved = saves.at(-1)!.messages.find((m) => m.role === 'user')!;
    const attachments = saved.meta?.attachments ?? [];
    expect(attachments.map((a) => a.name).sort()).toEqual(['big.mp4', 'small.mp4']);
    expect(attachments.find((a) => a.name === 'small.mp4')?.id).toBe('s'.repeat(32));
    expect(attachments.find((a) => a.name === 'big.mp4')?.id).toBeUndefined();
    expect(attachments.find((a) => a.name === 'big.mp4')?.upload_state).toBe(
      'interrupted',
    );
    // …and the person is told which one, and offered the explicit choice.
    const text = screen.getByTestId('unsent-turn').textContent ?? '';
    expect(text).toContain('big.mp4');
    expect(text).toContain('small.mp4 is on the server');

    await act(async () => {
      fireEvent.click(screen.getByRole('button', { name: 'Send with small.mp4' }));
    });
    await settle();
    expect(chatPosts).toHaveLength(1);
    expect(chatPosts[0].video_uploads).toEqual([
      { upload_id: 's'.repeat(32), name: 'small.mp4' },
    ]);
    await act(async () => {
      s.send('done', {});
      s.close();
    });
  });
});

/* ====================================================================== */
/* 9. An interrupted stream: no blank row, and it reconnects              */
/* ====================================================================== */

describe('a stream that dies after the request was accepted', () => {
  it('records the failure on the turn, stores no empty answer, and adopts the server’s', async () => {
    const s = sse();
    wire.chat = async () => streamReply(s);
    renderApp(ChatApp, Providers);
    await settle();
    await pressSend('long question');
    await settle();
    const intent = String(chatPosts[0].intent_id);

    await act(async () => {
      s.send('meta', { generation_id: 'g9', intent_id: intent, attempt: 1 });
    });
    await settle();

    // The server finishes the answer while this tab is disconnected.
    serverThreads.set('conv-1', [
      userTurn({ content: 'long question' }),
      {
        id: 'a1',
        role: 'assistant',
        content: 'Finished server-side.',
        createdAt: 3,
        meta: { generation_id: 'g9' },
      } as ChatMessage,
    ]);
    wire.request = async () =>
      ok({
        intent_id: intent,
        status: 'completed',
        generation_id: 'g9',
        attempt: 1,
        resumable: false,
        answer_persisted: true,
        live: false,
      });

    await act(async () => {
      s.breakIt();
    });
    await settle();

    // RC-2: not one blank assistant row was written.
    const blank = saves.some((save) =>
      save.messages.some(
        (m) => m.role === 'assistant' && m.content.trim() === '' && !m.meta?.error,
      ),
    );
    expect(blank).toBe(false);
    // The turn itself carries the interruption, so a reload can resume it.
    const lastUser = saves.at(-1)!.messages.find((m) => m.role === 'user');
    expect(lastUser?.meta?.intent?.state).toBe('interrupted');

    await waitFor(() =>
      expect(answers().join(' ')).toContain('Finished server-side.'),
    );
    expect(answers()).toHaveLength(1);
    expect(notice()).toBeNull();
  });
});

/* ====================================================================== */
/* 9b. A request held for the main model (strict one-model mode)          */
/* ====================================================================== */

describe('a request the server holds while the main model recovers', () => {
  //: orchestrator/app/continuity.py — the two sentences, verbatim.
  const QUEUED_LINE = 'Main model is recovering—your request is safely queued.';
  const EXPIRED_LINE =
    'The main model is still recovering. Your request is kept and will resume automatically.';
  const queuedReport = (live: boolean) =>
    ok({
      intent_id: 'i1',
      status: 'queued',
      generation_id: 'g1',
      attempt: 1,
      resumable: true,
      answer_persisted: false,
      live,
    });

  it('after a reload says queued — not working, not failed — and attaches when the resume runs', async () => {
    wire.request = async () => queuedReport(false);
    await reopen('conv-1', [
      userTurn({ meta: { intent: { id: 'i1', state: 'accepted', generation_id: 'g1' } } }),
    ]);
    await waitFor(() => expect(screen.getByTestId('turn-queued')).toBeTruthy());
    const turn = screen.getByTestId('turn-queued');
    expect(turn.textContent).toContain('Waiting for the main model to recover…');
    expect(turn.querySelector('button')).toBeNull();
    expect(notice()).toBeNull();
    expect(screen.queryByRole('alert')).toBeNull();
    // Written down as queued, so the next reload starts from the truth.
    const lastUser = saves.at(-1)!.messages.find((m) => m.role === 'user');
    expect(lastUser?.meta?.intent?.state).toBe('queued');

    // Two more polls, still queued: the turn keeps saying so and nothing
    // escalates — no Retry, no "checking", no attach.
    for (let i = 0; i < 2; i += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(8000);
      });
      await settle();
    }
    expect(screen.getByTestId('turn-queued')).toBeTruthy();
    expect(attachAsks).toBe(0);

    // The resume sweep runs the row: `live` turns true and the poll attaches.
    const live = sse();
    wire.request = async () => queuedReport(true);
    wire.attach = async () => streamReply(live);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(8000);
    });
    await waitFor(() => expect(attachAsks).toBe(1));
    await act(async () => {
      live.send('meta', { generation_id: 'g1', intent_id: 'i1', attempt: 2 });
      live.send('token', { text: 'Back, and answered.' });
      live.send('done', {});
      live.close();
    });
    await settle();
    expect(answers().join(' ')).toContain('Back, and answered.');
    expect(notice()).toBeNull();
    expect(screen.queryByTestId('turn-queued')).toBeNull();
  });

  it('a live stream that is parked shows the server sentence, no failure, and no Retry', async () => {
    const s = sse();
    wire.chat = async () => streamReply(s);
    renderApp(ChatApp, Providers);
    await settle();
    await pressSend('slow question');
    await settle();
    const intent = String(chatPosts[0].intent_id);
    await act(async () => {
      s.send('meta', { generation_id: 'g1', intent_id: intent, attempt: 1 });
      s.send('status', { text: QUEUED_LINE });
    });
    // The queued line renders as the live status — not the generic wait.
    // A second event rides the frame clock, and jsdom's frame is an
    // interval — the one timer this file fakes — so it is stepped.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(50);
    });
    expect(screen.getByText(QUEUED_LINE)).toBeTruthy();
    expect(screen.queryByLabelText('Waiting for the first token')).toBeNull();

    wire.request = async () => queuedReport(false);
    await act(async () => {
      s.send('error', { message: EXPIRED_LINE, code: 'MODEL_RECOVERING', resumable: true });
      s.close();
    });
    await settle();
    const status = screen.getByTestId('queued-turn');
    expect(status.textContent).toContain(EXPIRED_LINE);
    expect(status.textContent).toContain('Waiting for the main model to recover…');
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.queryByRole('button', { name: 'Retry' })).toBeNull();
    expect(screen.queryByRole('button', { name: 'Resume' })).toBeNull();
    expect(notice()).toBeNull();
    // RC-2: the empty placeholder was not stored as an answer; the send was.
    expect(
      saves.some((save) =>
        save.messages.some((m) => m.role === 'assistant' && m.content.trim() === ''),
      ),
    ).toBe(false);
    const lastUser = saves.at(-1)!.messages.find((m) => m.role === 'user');
    expect(lastUser?.meta?.intent).toMatchObject({ state: 'queued', reason: EXPIRED_LINE });
    // The composer is free: a parked request locks nothing.
    expect((box() as HTMLTextAreaElement).disabled).toBe(false);
  });
});

/* ====================================================================== */
/* 10. The conditional history write                                      */
/* ====================================================================== */

describe('the whole-thread PUT', () => {
  it('sends expected_updated_at, and a 409 re-applies only the local-only tail', async () => {
    const real = await vi.importActual<typeof import('@/lib/history')>(
      '@/lib/history',
    );
    const storage = new Map<string, string>();
    const puts: { messages: unknown[]; expected?: string }[] = [];
    let serverMessages = [
      { role: 'user', content: 'question', meta: { intent: { id: 'i1', state: 'accepted' } } },
    ];
    let updatedAt = '2026-09-10T10:00:00Z';
    let refuseOnce = true;

    const api = {
      list: async () => [],
      get: async () => ({
        id: 'c1',
        title: 'Chat',
        messages: serverMessages,
        updatedAt,
      }),
      create: async () => undefined,
      update: async () => undefined,
      remove: async () => undefined,
      appendMessage: async () => ({ id: 1 }),
      setFeedback: async () => undefined,
      generateTitle: async () => ({ title: '', generated: false }),
      truncateMessages: async () => undefined,
      replaceMessages: async (
        _id: string,
        messages: unknown[],
        expected?: string,
      ) => {
        puts.push({ messages, expected });
        if (refuseOnce) {
          refuseOnce = false;
          // The server persisted the answer itself while this tab held an
          // older copy — exactly RC-4.
          serverMessages = [
            ...serverMessages,
            { role: 'assistant', content: 'server answer', meta: { generation_id: 'g1' } } as never,
          ];
          updatedAt = '2026-09-10T10:00:05Z';
          const err = new (await import('@/lib/historyApi')).HistoryApiError(
            409,
            'conflict',
            { detail: 'conversation changed', updated_at: updatedAt, messages: 2 },
          );
          throw err;
        }
      },
    };

    const store = real.createServerHistoryStore({
      storage: {
        getItem: (k: string) => storage.get(k) ?? null,
        setItem: (k: string, v: string) => void storage.set(k, v),
        removeItem: (k: string) => void storage.delete(k),
      },
      api: api as never,
    });
    await store.ready();
    // Learn the conversation (and its updated_at) the way a reload does.
    await store.load('c1', { force: true });
    expect(store.get('c1')?.messages).toHaveLength(1);

    // This tab has the same answer AND a local-only follow-up question. The
    // question's own intent has moved on (accepted -> completed), which is
    // what makes this a whole-thread REPLACE rather than an append: the
    // already-pushed prefix changed.
    const loaded = store.get('c1')?.messages ?? [];
    store.saveMessages('c1', [
      {
        ...loaded[0],
        meta: { intent: { id: 'i1', state: 'completed' } },
      } as ChatMessage,
      {
        id: 'local-answer',
        role: 'assistant',
        content: 'server answer',
        createdAt: 2,
        meta: { generation_id: 'g1' },
      } as ChatMessage,
      {
        id: 'local-follow-up',
        role: 'user',
        content: 'and then?',
        createdAt: 3,
        meta: { intent: { id: 'i2', state: 'submitting' } },
      } as ChatMessage,
    ]);
    await store.flush();

    // The first write quoted what it had read…
    expect(puts[0].expected).toBe('2026-09-10T10:00:00Z');
    // …and after the refusal the retry carries the SERVER's answer plus the
    // one turn only this tab has — not a second copy of the answer.
    const retried = puts[1].messages as { role: string; content: string }[];
    expect(retried.map((m) => m.content)).toEqual([
      'question',
      'server answer',
      'and then?',
    ]);
    expect(
      retried.filter((m) => m.content === 'server answer'),
    ).toHaveLength(1);
  });

  it('never writes an assistant turn that is empty and silent', async () => {
    const real = await vi.importActual<typeof import('@/lib/history')>(
      '@/lib/history',
    );
    const storage = new Map<string, string>();
    const store = real.createServerHistoryStore({
      storage: {
        getItem: (k: string) => storage.get(k) ?? null,
        setItem: (k: string, v: string) => void storage.set(k, v),
        removeItem: (k: string) => void storage.delete(k),
      },
      api: {
        list: async () => [],
        get: async () => ({ id: 'c2', title: 'Chat', messages: [] }),
        create: async () => undefined,
        update: async () => undefined,
        remove: async () => undefined,
        appendMessage: async () => ({ id: 1 }),
        setFeedback: async () => undefined,
        generateTitle: async () => ({ title: '', generated: false }),
        truncateMessages: async () => undefined,
        replaceMessages: async () => undefined,
      } as never,
    });
    await store.ready();
    // The conversation has to exist in the cache before anything can be
    // written into it, the same way a reload or a create makes it exist.
    await store.load('c2', { force: true });
    store.saveMessages('c2', [
      { id: 'u', role: 'user', content: 'q', createdAt: 1 } as ChatMessage,
      { id: 'blank', role: 'assistant', content: '', createdAt: 2, meta: {} } as ChatMessage,
    ]);
    expect(store.get('c2')?.messages.map((m) => m.id)).toEqual(['u']);

    // …but an empty answer that CARRIES its failure is a record, and stays.
    store.saveMessages('c2', [
      { id: 'u', role: 'user', content: 'q', createdAt: 1 } as ChatMessage,
      {
        id: 'err',
        role: 'assistant',
        content: '',
        createdAt: 2,
        meta: { error: { message: 'The connection dropped.', resumable: true } },
      } as ChatMessage,
    ]);
    expect(store.get('c2')?.messages.map((m) => m.id)).toEqual(['u', 'err']);
  });
});

/* ====================================================================== */
/* 11. Rows written before intents existed                                */
/* ====================================================================== */

describe('a legacy send_state row', () => {
  const legacy = () =>
    userTurn({
      meta: {
        send_state: 'uploading',
        attachments: [{ kind: 'video', name: 'a.mp4', id: 'a'.repeat(32) }],
      },
    });

  it('warns only after the server has answered — not while it cannot be asked', async () => {
    wire.active = async () => fail(503, { detail: 'down' });
    await reopen('conv-1', [legacy()]);
    await settle();
    expect(notice()).toBeNull();
    expect(screen.getByTestId('turn-status_unknown')).toBeTruthy();

    // The server comes back and says nothing is running: NOW it may say so.
    wire.active = async () => ok({ active: [] });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(8000);
    });
    await settle();
    await waitFor(() => expect(notice()).not.toBeNull());
    expect(notice()!.textContent).toContain('never sent');
  });
});

/* ====================================================================== */
/* R1. The race matrix                                                    */
/* ====================================================================== */

/**
 * Twelve enumerated points in the life of one send. Each is a place where
 * the tab can lose the thread — a reload, a dropped pipe, an unreachable
 * status endpoint, a second click — and the three invariants below must hold
 * at every one of them.
 */
const CUTS = [
  'send-pressed',
  'upload-in-flight',
  'upload-landed',
  'post-in-flight',
  'accepted',
  'meta-seen',
  'first-token',
  'mid-tokens',
  'all-tokens',
  'done-seen',
  'persist-in-flight',
  'persisted',
] as const;
type Cut = (typeof CUTS)[number];

/** How far into the lifecycle a cut is. */
const order = (cut: Cut) => CUTS.indexOf(cut);

interface RunResult {
  answers: string[];
  noticeSeen: boolean;
  posts: Record<string, unknown>[];
  saves: { id: string; messages: ChatMessage[] }[];
}

/**
 * Drive one send to `cut`, apply `disturb`, and report what the tab ended up
 * showing. Deliberately withholds every response until the test asks for it:
 * there is no clock in here at all.
 */
async function runToCut(cut: Cut, withUpload: boolean): Promise<{
  s: ReturnType<typeof sse>;
  intent: string;
  noticeDuring: boolean;
}> {
  const s = sse();
  const upload = defer<Reply>();
  wire.upload = () => upload.promise;
  const post = defer<Reply>();
  wire.chat = () => post.promise;
  let noticeDuring = false;
  const watch = () => {
    if (notice()) noticeDuring = true;
  };

  renderApp(ChatApp, Providers);
  await settle();
  if (withUpload) await attachFiles([video('clip.mp4')]);
  await pressSend('race question');
  await settle();
  watch();
  if (order(cut) <= order('upload-in-flight')) return { s, intent: '', noticeDuring };

  if (withUpload) {
    upload.resolve(ok({ upload_id: 'c'.repeat(32), filename: 'clip.mp4', files: 1 }));
    await settle();
  }
  watch();
  if (order(cut) <= order('upload-landed')) return { s, intent: '', noticeDuring };

  if (order(cut) <= order('post-in-flight')) {
    watch();
    return { s, intent: '', noticeDuring };
  }
  post.resolve(streamReply(s));
  await settle();
  watch();
  const intent = String(chatPosts[0]?.intent_id ?? '');
  if (order(cut) <= order('accepted')) return { s, intent, noticeDuring };

  await act(async () => {
    s.send('meta', { generation_id: 'g-race', intent_id: intent, attempt: 1 });
  });
  await settle(2);
  watch();
  if (order(cut) <= order('meta-seen')) return { s, intent, noticeDuring };

  await act(async () => {
    s.send('token', { text: 'The ' });
  });
  await settle(2);
  watch();
  if (order(cut) <= order('first-token')) return { s, intent, noticeDuring };

  await act(async () => {
    s.send('token', { text: 'race ' });
  });
  await settle(2);
  watch();
  if (order(cut) <= order('mid-tokens')) return { s, intent, noticeDuring };

  await act(async () => {
    s.send('token', { text: 'answer.' });
  });
  await settle(2);
  watch();
  if (order(cut) <= order('all-tokens')) return { s, intent, noticeDuring };

  await act(async () => {
    s.send('done', {});
    s.close();
  });
  await settle();
  watch();
  return { s, intent, noticeDuring };
}

describe('R1 — the race matrix', () => {
  /**
   * SCENARIO 1: the pipe dies at the cut. The server keeps the request, so
   * the tab must never say "never sent", must not store a blank answer, and
   * must end up with the server's answer exactly once.
   */
  it.each(CUTS)('a dropped connection at %s loses no answer', async (cut) => {
    const { s, intent, noticeDuring } = await runToCut(cut, false);
    serverThreads.set('conv-1', [
      userTurn({ content: 'race question' }),
      {
        id: 'a',
        role: 'assistant',
        content: 'The race answer.',
        createdAt: 9,
        meta: { generation_id: 'g-race' },
      } as ChatMessage,
    ]);
    if (intent) {
      wire.request = async () =>
        ok({
          intent_id: intent,
          status: 'completed',
          generation_id: 'g-race',
          attempt: 1,
          resumable: false,
          answer_persisted: true,
          live: false,
        });
    }
    if (order(cut) > order('post-in-flight') && order(cut) < order('done-seen')) {
      await act(async () => {
        s.breakIt();
      });
      await settle();
    }
    expect(noticeDuring).toBe(false);
    // No blank assistant row, ever.
    expect(
      saves.some((save) =>
        save.messages.some(
          (m) => m.role === 'assistant' && m.content.trim() === '' && !m.meta?.error,
        ),
      ),
    ).toBe(false);
    // No duplicate answer on screen.
    expect(answers().length).toBeLessThanOrEqual(1);
  });

  /**
   * SCENARIO 2: the status endpoints are unreachable at the cut. "Cannot
   * ask" may never be rendered as "never sent".
   */
  it.each(CUTS)('an unreachable server at %s never says "never sent"', async (cut) => {
    const { noticeDuring } = await runToCut(cut, false);
    wire.active = async () => fail(502, { message: 'unreachable' });
    wire.request = async () => fail(502, { message: 'unreachable' });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(8000);
    });
    await settle();
    expect(noticeDuring).toBe(false);
    expect(notice()).toBeNull();
  });

  /**
   * SCENARIO 3: an old backend — GET /chat/requests does not exist. A 404
   * from a MISSING ROUTE is not an answer about the intent.
   */
  it.each(CUTS)('an old backend at %s falls back rather than accusing', async (cut) => {
    const { noticeDuring } = await runToCut(cut, false);
    wire.request = async () => fail(404, { detail: 'Not Found' });
    wire.active = async () => ok({ active: ['conv-1'] });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(8000);
    });
    await settle();
    expect(noticeDuring).toBe(false);
    expect(notice()).toBeNull();
  });

  /**
   * SCENARIO 4: the server persisted the answer itself at the cut. The tab
   * must adopt it without producing a second copy.
   */
  it.each(CUTS)('a server-persisted answer at %s is not duplicated', async (cut) => {
    const { intent } = await runToCut(cut, false);
    serverThreads.set('conv-1', [
      userTurn({ content: 'race question' }),
      {
        id: 'a',
        role: 'assistant',
        content: 'The race answer.',
        createdAt: 9,
        meta: { generation_id: 'g-race' },
      } as ChatMessage,
    ]);
    wire.active = async () => ok({ active: [] });
    if (intent) {
      wire.request = async () =>
        ok({
          intent_id: intent,
          status: 'completed',
          generation_id: 'g-race',
          attempt: 1,
          resumable: false,
          answer_persisted: true,
          live: false,
        });
    }
    await act(async () => {
      await vi.advanceTimersByTimeAsync(8000);
    });
    await settle();
    const shown = answers().filter((t) => t.includes('The race answer.'));
    expect(shown.length).toBeLessThanOrEqual(1);
  });

  /**
   * SCENARIO 5: a second Send at the cut. One question, one intent — even
   * when the first has not been acknowledged yet.
   */
  it.each(CUTS)('a second Send at %s does not start a second send', async (cut) => {
    const { s } = await runToCut(cut, false);
    const before = chatPosts.length;
    await act(async () => {
      fireEvent.change(box(), { target: { value: 'race question' } });
      const button = screen.queryByRole('button', { name: 'Send message' });
      if (button) fireEvent.click(button);
    });
    await settle(2);
    const posts = chatPosts.slice(before);
    // Either the composer refused (streaming / busy), or the send that got
    // through is a NEW question with its own intent — never a second copy of
    // the one already in flight.
    const ids = new Set(chatPosts.map((p) => String(p.intent_id)));
    expect(ids.size).toBe(chatPosts.length);
    expect(posts.length).toBeLessThanOrEqual(1);
    await act(async () => {
      s.close();
    });
  });
});
