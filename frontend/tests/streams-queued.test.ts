/**
 * QUEUED, NOT FAILED (2026-09-12) — the stream layer in strict one-model
 * mode (docs/availability/CONTRACT.md §8.3).
 *
 * While the main model recovers, the orchestrator holds the request: the row
 * is `queued`, the stream says so in one exact sentence, and if the wait
 * outruns LLM_QUEUE_MAX_WAIT_S the stream ends with a terminal `error` frame
 * carrying `code: "MODEL_RECOVERING", resumable: true`. Two things must be
 * true of `lib/streams.ts` for that to reach a person truthfully:
 *
 * 1. the parked frame is a QUEUED state — the server's sentence kept, no
 *    error, the intent moved to `queued` and written down — never the
 *    generic failure every other `error` frame is;
 * 2. the reconnect loop treats `queued` from GET /chat/requests as
 *    live-but-waiting: a slow fixed cadence, no attempt charged, for as long
 *    as that is the answer — and the pass that finds `live` attaches.
 *
 * The clock is faked so the cadence is asserted, not waited for; every
 * server answer is scripted per ask.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ChatMessage } from '@/lib/types';

/* ------------------------------------------------------------ the store */

const saved: { id: string; messages: ChatMessage[] }[] = [];

/** The store: every save recorded, and the last one is what a load finds. */
function stored(id: string) {
  const last = [...saved].reverse().find((s) => s.id === id);
  return last
    ? { id, title: 'Chat', messages: last.messages, createdAt: 0, updatedAt: 0 }
    : null;
}

vi.mock('@/lib/history', () => ({
  newId: () => `a${Math.random().toString(36).slice(2, 10)}`,
  getHistoryStore: () => ({
    saveMessages: (id: string, messages: ChatMessage[]) => {
      saved.push({ id, messages: messages.map((m) => ({ ...m })) });
    },
    get: (id: string) => stored(id),
    load: async (id: string) => stored(id),
  }),
}));
vi.mock('@/lib/auth', () => ({ handleSessionEnd: () => undefined }));

/* ------------------------------------------------------------- the wire */

//: The exact sentences of orchestrator/app/continuity.py — never paraphrased.
const QUEUED_LINE = 'Main model is recovering—your request is safely queued.';
const EXPIRED_LINE =
  'The main model is still recovering. Your request is kept and will resume automatically.';

/** A stream body a test feeds by hand. */
function pipe() {
  let enqueue!: (chunk: string) => void;
  let finish!: () => void;
  let breakIt!: () => void;
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      enqueue = (chunk) => c.enqueue(encoder.encode(chunk));
      finish = () => c.close();
      breakIt = () => c.error(new Error('connection reset'));
    },
  });
  return { body, enqueue, finish, breakIt };
}

const frame = (event: string, data: unknown) =>
  `event: ${event}\ndata: ${JSON.stringify(data)}\n\n`;

interface Report {
  status: string;
  live?: boolean;
  resumable?: boolean;
  answer_persisted?: boolean;
}

/** What GET /chat/requests answers, ask by ask; the last entry repeats. */
let reports: Report[] = [];
let asks = 0;
let attachOpened = 0;
let chatPipe: ReturnType<typeof pipe>;
let attachPipe: ReturnType<typeof pipe>;

function installFetch() {
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string) => {
      const u = String(url);
      if (u.startsWith('/api/chat/requests/')) {
        asks += 1;
        const r = reports[Math.min(asks - 1, reports.length - 1)];
        return {
          ok: true,
          status: 200,
          json: async () => ({
            intent_id: 'i1',
            status: r.status,
            generation_id: 'g1',
            attempt: 1,
            resumable: r.resumable ?? true,
            answer_persisted: r.answer_persisted ?? false,
            live: r.live ?? false,
          }),
        };
      }
      if (u.startsWith('/api/chat/attach/')) {
        attachOpened += 1;
        return { ok: true, status: 200, body: attachPipe.body, json: async () => ({}) };
      }
      if (u === '/api/chat/stop') return { ok: true, status: 200, json: async () => ({}) };
      return { ok: true, status: 200, body: chatPipe.body, json: async () => ({}) };
    }),
  );
}

const PREFS = {
  model: 'fast',
  effort: 'low',
  agent: false,
  webSearch: false,
  deepResearch: false,
  salesforce: false,
  sfLive: false,
} as never;

/** Let the reader inside consume() drain what has been enqueued. */
async function settle(): Promise<void> {
  for (let i = 0; i < 12; i += 1) await Promise.resolve();
}

async function streams() {
  return import('@/lib/streams');
}

/** Send a turn with an intent and drive it to acceptance. */
async function accepted(conversationId = 'c1') {
  const { startStream, getLiveStream, reconnectPolicy } = await streams();
  reconnectPolicy.baseMs = 0;
  reconnectPolicy.jitter = false;
  reconnectPolicy.attempts = 3;
  reconnectPolicy.queuedMs = 10_000;
  const running = startStream({
    conversationId,
    turns: [
      {
        id: 'u1',
        role: 'user',
        content: 'What was decided?',
        createdAt: 1,
        meta: { intent: { id: 'i1', state: 'submitting' } },
      },
    ] as ChatMessage[],
    prefs: PREFS,
    intentId: 'i1',
    intentMessageId: 'u1',
  });
  await settle();
  chatPipe.enqueue(frame('meta', { generation_id: 'g1', intent_id: 'i1', attempt: 1 }));
  await settle();
  const view = () => getLiveStream(conversationId)!;
  const answer = () => view().messages.at(-1)!;
  const question = () => view().messages.find((m) => m.id === 'u1')!;
  return { running, view, answer, question, reconnectPolicy };
}

beforeEach(() => {
  saved.length = 0;
  reports = [];
  asks = 0;
  attachOpened = 0;
  chatPipe = pipe();
  attachPipe = pipe();
  installFetch();
  vi.useFakeTimers();
});

afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.resetModules();
});

/* ====================================================================== */
/* 1. The parked frame is a queued state, not a failure                   */
/* ====================================================================== */

describe('the MODEL_RECOVERING terminal frame', () => {
  it('renders as queued: the exact sentence, no error, the intent written down', async () => {
    const t = await accepted();
    chatPipe.enqueue(frame('status', { text: QUEUED_LINE }));
    await settle();
    expect(t.answer().searchStatus).toBe(QUEUED_LINE);
    // (3) Nothing generic may overwrite the line while the turn is parked: a
    // reasoning delta from before the engine went away, and the engine-less
    // meta that follows, both leave it exactly where it is.
    chatPipe.enqueue(frame('reasoning', { text: 'thinking about it' }));
    chatPipe.enqueue(frame('meta', { generation_id: 'g1', route: 'chat' }));
    await settle();
    expect(t.answer().searchStatus).toBe(QUEUED_LINE);

    chatPipe.enqueue(
      frame('error', { message: EXPIRED_LINE, code: 'MODEL_RECOVERING', resumable: true }),
    );
    chatPipe.finish();
    await t.running;

    // The stream and the answer are QUEUED — not 'error', not 'done'.
    expect(t.view().status).toBe('queued');
    const a = t.answer();
    expect(a.status).toBe('queued');
    expect(a.errorMessage).toBeUndefined();
    expect(a.errorCode).toBeUndefined();
    // The server's sentence, verbatim, in the one place a reload reads back.
    expect(a.meta?.error).toEqual({
      message: EXPIRED_LINE,
      code: 'MODEL_RECOVERING',
      status: null,
      resumable: true,
    });
    // The live progress line is retired: the parked state IS the status now.
    expect(a.searchStatus).toBeUndefined();
    // The send itself says queued, with the sentence, and it was written.
    expect(t.question().meta?.intent).toMatchObject({
      id: 'i1',
      state: 'queued',
      reason: EXPIRED_LINE,
      generation_id: 'g1',
    });
    const last = saved.at(-1)!;
    expect(last.messages.find((m) => m.id === 'u1')?.meta?.intent?.state).toBe('queued');
    // RC-2: the empty placeholder is not stored as though it were an answer.
    expect(last.messages.some((m) => m.role === 'assistant')).toBe(false);
    // And the reconnect is on, from zero: nothing has been given up.
    expect(t.view().reconnect).toEqual({ attempt: 0, statusUnknown: false, exhausted: false });
  });

  it('is the ONLY error frame treated this way — a plain failure still fails', async () => {
    const t = await accepted();
    chatPipe.enqueue(frame('error', { message: 'Engine core died' }));
    chatPipe.finish();
    await t.running;
    expect(t.view().status).toBe('error');
    expect(t.answer().status).toBe('error');
    expect(t.question().meta?.intent?.state).toBe('failed');
  });

  it('needs resumable: a MODEL_RECOVERING frame without it is not a queued promise', async () => {
    const t = await accepted();
    chatPipe.enqueue(frame('error', { message: EXPIRED_LINE, code: 'MODEL_RECOVERING' }));
    chatPipe.finish();
    await t.running;
    expect(t.view().status).toBe('error');
  });
});

/* ====================================================================== */
/* 2. The reconnect loop on `queued`                                      */
/* ====================================================================== */

describe('the reconnect loop while the server answers queued', () => {
  async function parked() {
    const t = await accepted();
    chatPipe.enqueue(
      frame('error', { message: EXPIRED_LINE, code: 'MODEL_RECOVERING', resumable: true }),
    );
    chatPipe.finish();
    await t.running;
    return t;
  }

  it('asks at the slow cadence and never spends the attempt budget', async () => {
    const t = await parked();
    reports = [{ status: 'queued' }];
    const { queuedMs, attempts } = t.reconnectPolicy;
    // Nothing before the first interval: the frame itself was the answer.
    await vi.advanceTimersByTimeAsync(queuedMs - 1);
    expect(asks).toBe(0);
    // Many more asks than the budget allows for an unknown state...
    const rounds = attempts * 4;
    for (let i = 0; i < rounds; i += 1) {
      await vi.advanceTimersByTimeAsync(queuedMs);
    }
    expect(asks).toBe(rounds);
    // ...and not one of them was charged: still queued, still watching.
    expect(t.view().status).toBe('queued');
    expect(t.view().reconnect).toEqual({ attempt: 0, statusUnknown: false, exhausted: false });
    expect(t.question().meta?.intent?.state).toBe('queued');
    expect(attachOpened).toBe(0);
  });

  it('attaches the moment `live` turns true, and streams the resumed answer', async () => {
    const t = await parked();
    reports = [{ status: 'queued' }, { status: 'queued' }, { status: 'queued', live: true }];
    const { queuedMs } = t.reconnectPolicy;
    await vi.advanceTimersByTimeAsync(queuedMs * 2);
    expect(asks).toBe(2);
    expect(attachOpened).toBe(0);
    await vi.advanceTimersByTimeAsync(queuedMs);
    expect(asks).toBe(3);
    expect(attachOpened).toBe(1);
    // The attach replaced the parked placeholder with a live one.
    expect(t.view().status).toBe('streaming');
    attachPipe.enqueue(frame('meta', { generation_id: 'g1', intent_id: 'i1', attempt: 2 }));
    attachPipe.enqueue(frame('token', { text: 'Resumed.' }));
    await settle();
    expect(t.answer().content).toBe('Resumed.');
    expect(t.answer().status).toBe('streaming');
    attachPipe.enqueue(frame('done', {}));
    attachPipe.finish();
    await settle();
    expect(t.view().status).toBe('done');
    expect(t.question().meta?.intent?.state).toBe('completed');
    // Exactly one ask more after the attach: none — the loop handed over.
    await vi.advanceTimersByTimeAsync(queuedMs * 3);
    expect(asks).toBe(3);
  });

  it('a stream that was merely interrupted becomes queued when the server says so', async () => {
    const t = await accepted();
    chatPipe.breakIt();
    await t.running;
    expect(t.view().status).toBe('error');
    expect(t.question().meta?.intent?.state).toBe('interrupted');
    // Attempt 0 waits backoffFor(0) = baseMs (0); the answer is `queued`.
    reports = [{ status: 'queued' }];
    await vi.advanceTimersByTimeAsync(1);
    expect(asks).toBe(1);
    expect(t.view().status).toBe('queued');
    expect(t.answer().status).toBe('queued');
    expect(t.answer().meta?.error?.code).toBe('MODEL_RECOVERING');
    expect(t.question().meta?.intent).toMatchObject({ state: 'queued' });
    expect(t.view().reconnect).toEqual({ attempt: 0, statusUnknown: false, exhausted: false });
    // From here on the cadence is the slow one, not the backoff.
    await vi.advanceTimersByTimeAsync(t.reconnectPolicy.queuedMs - 2);
    expect(asks).toBe(1);
    await vi.advanceTimersByTimeAsync(2);
    expect(asks).toBe(2);
  });

  it('still adopts the answer when the row completes without a live generation', async () => {
    const t = await parked();
    reports = [{ status: 'queued' }, { status: 'completed', answer_persisted: true }];
    await vi.advanceTimersByTimeAsync(t.reconnectPolicy.queuedMs * 2);
    expect(asks).toBe(2);
    expect(t.view().status).toBe('done');
    expect(t.view().reconnect).toBeUndefined();
  });
});
