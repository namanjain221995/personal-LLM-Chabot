/**
 * NEW-24 — the streaming pipeline's frame coalescing, at the source.
 *
 * `lib/streams.ts` commits one VISUAL update per display frame instead of one
 * per token. The thing to prove is that this is purely a change in how often
 * the view is TOLD, and never a change to what it is told: no token may be
 * buffered, dropped, duplicated, reordered or left behind by any ending the
 * stream can have.
 *
 * `requestAnimationFrame` is stubbed with a manual queue so every assertion
 * is about ordering rather than timing — nothing here sleeps or races.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import type { ChatMessage } from '@/lib/types';

const saved: { id: string; messages: ChatMessage[] }[] = [];

vi.mock('@/lib/history', () => ({
  newId: () => `a${Math.random().toString(36).slice(2, 10)}`,
  getHistoryStore: () => ({
    saveMessages: (id: string, messages: ChatMessage[]) => {
      saved.push({ id, messages });
    },
    get: () => null,
    load: async () => null,
  }),
}));
vi.mock('@/lib/auth', () => ({ handleSessionEnd: () => undefined }));

let frames: (FrameRequestCallback | null)[] = [];
/** Run everything booked for the next frame — exactly one display frame. */
function paint(): void {
  const due = frames;
  frames = [];
  for (const cb of due) if (cb) cb(0);
}

/**
 * A stream body a test feeds by hand.
 *
 * `abort` reproduces what a real `fetch` does when its signal fires: the
 * reader rejects with an AbortError. Stop depends on that, so a harness that
 * only closed the stream would be testing a different ending.
 */
function controllable() {
  let enqueue!: (chunk: string) => void;
  let finish!: () => void;
  let abort!: () => void;
  const encoder = new TextEncoder();
  const body = new ReadableStream<Uint8Array>({
    start(c) {
      enqueue = (chunk) => c.enqueue(encoder.encode(chunk));
      finish = () => c.close();
      abort = () => c.error(new DOMException('Aborted', 'AbortError'));
    },
  });
  return { body, enqueue, finish, abort };
}

const tokenEvent = (text: string) =>
  `event: token\ndata: ${JSON.stringify({ text })}\n\n`;

const PREFS = {
  model: 'fast',
  effort: 'low',
  agent: false,
  webSearch: false,
  deepResearch: false,
  salesforce: false,
  sfLive: false,
} as never;

/** Let the async generator inside consume() drain what has been enqueued. */
async function settle(): Promise<void> {
  for (let i = 0; i < 12; i += 1) await Promise.resolve();
}

async function openStream(conversationId: string) {
  const { startStream, subscribeStreams, getLiveStream } = await import(
    '@/lib/streams'
  );
  const pipe = controllable();
  vi.stubGlobal(
    'fetch',
    vi.fn(async (url: string, init?: { signal?: AbortSignal }) => {
      // POST /api/chat/stop is fire-and-forget; only the generation carries
      // the signal the abort travels on.
      if (String(url).includes('/stop')) return { ok: true, status: 200 };
      init?.signal?.addEventListener('abort', () => pipe.abort());
      return { ok: true, status: 200, body: pipe.body };
    }),
  );
  const seen: string[] = [];
  const unsubscribe = subscribeStreams((id) => seen.push(id));
  const running = startStream({
    conversationId,
    turns: [
      { id: 'u1', role: 'user', content: 'Ask', status: 'done', createdAt: 1 },
    ] as ChatMessage[],
    prefs: PREFS,
  });
  await settle();
  const content = () =>
    getLiveStream(conversationId)?.messages.at(-1)?.content ?? '';
  return { ...pipe, seen, unsubscribe, running, content, getLiveStream };
}

beforeEach(() => {
  saved.length = 0;
  frames = [];
  vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => {
    frames.push(cb);
    return frames.length;
  });
  vi.stubGlobal('cancelAnimationFrame', (handle: number) => {
    frames[handle - 1] = null;
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.resetModules();
});

describe('token fidelity (TEST 1)', () => {
  it('reproduces the deltas exactly, Unicode and newlines included', async () => {
    const s = await openStream('c1');
    const deltas = ['Hello', ' ', 'world', '\n', 'नमस्ते', ' 😊'];
    for (const d of deltas) s.enqueue(tokenEvent(d));
    await settle();
    s.enqueue('event: done\ndata: {}\n\n');
    s.finish();
    await s.running;

    expect(saved.at(-1)?.messages.at(-1)?.content).toBe('Hello world\nनमस्ते 😊');
  });

  it('survives an event split across chunk boundaries', async () => {
    const s = await openStream('c1');
    const whole = tokenEvent('sp lit');
    s.enqueue(whole.slice(0, 9));
    await settle();
    s.enqueue(whole.slice(9));
    await settle();
    s.enqueue('event: done\ndata: {}\n\n');
    s.finish();
    await s.running;

    expect(saved.at(-1)?.messages.at(-1)?.content).toBe('sp lit');
  });
});

describe('frame coalescing (TEST 2)', () => {
  it('commits once per frame, not once per token', async () => {
    const s = await openStream('c1');
    s.seen.length = 0;

    // The first token always commits immediately — time to first token is not
    // something coalescing is allowed to spend.
    s.enqueue(tokenEvent('A'));
    await settle();
    expect(s.seen.length).toBe(1);

    // The next four arrive before the browser can paint: one booked frame.
    s.seen.length = 0;
    for (const t of ['B', 'C', 'D', 'E']) {
      s.enqueue(tokenEvent(t));
      await settle();
    }
    expect(s.seen.length).toBe(0); // nothing committed yet
    expect(s.content()).toBe('ABCDE'); // but the state is already complete

    paint();
    expect(s.seen).toEqual(['c1']); // exactly ONE commit for the four deltas
    expect(s.content()).toBe('ABCDE');
  });

  it('books a new frame for the next burst rather than starving it', async () => {
    const s = await openStream('c1');
    s.enqueue(tokenEvent('1'));
    await settle();
    s.seen.length = 0;

    s.enqueue(tokenEvent('2'));
    await settle();
    paint();
    s.enqueue(tokenEvent('3'));
    await settle();
    paint();

    expect(s.seen).toEqual(['c1', 'c1']);
    expect(s.content()).toBe('123');
  });
});

describe('terminal flush', () => {
  it('renders content still pending when `done` arrives (TEST 3)', async () => {
    const s = await openStream('c1');
    s.enqueue(tokenEvent('Hello '));
    await settle();
    s.seen.length = 0;
    // Pending: booked for a frame that has NOT run.
    s.enqueue(tokenEvent('world!'));
    await settle();
    expect(s.seen.length).toBe(0);

    s.enqueue('event: done\ndata: {}\n\n');
    s.finish();
    await s.running;

    // Delivered synchronously by `done`, without waiting for the frame.
    expect(s.seen).toContain('c1');
    expect(saved.at(-1)?.messages.at(-1)?.content).toBe('Hello world!');
    expect(saved.at(-1)?.messages.at(-1)?.status).toBe('done');

    // And the frame that was booked cannot repaint afterwards.
    const commits = s.seen.length;
    paint();
    expect(s.seen.length).toBe(commits);
  });

  it('keeps content received before an `error` (TEST 4)', async () => {
    const s = await openStream('c1');
    s.enqueue(tokenEvent('partial answer'));
    await settle();
    s.enqueue(`event: error\ndata: ${JSON.stringify({ message: 'boom' })}\n\n`);
    s.finish();
    await s.running;

    const last = saved.at(-1)?.messages.at(-1);
    expect(last?.content).toBe('partial answer');
    expect(last?.status).toBe('error');
    expect(last?.errorMessage).toBe('boom');
  });

  it('keeps content received before Stop, and nothing lands after (TEST 5)', async () => {
    const { stopStream } = await import('@/lib/streams');
    const s = await openStream('c1');
    s.enqueue(tokenEvent('kept text'));
    await settle();
    s.seen.length = 0;
    s.enqueue(tokenEvent(' and this too'));
    await settle();

    stopStream('c1');
    await s.running;

    const last = saved.at(-1)?.messages.at(-1);
    expect(last?.content).toBe('kept text and this too');
    expect(last?.status).toBe('stopped');

    const commits = s.seen.length;
    expect(commits).toBeGreaterThan(0);
    paint(); // a stale frame must not repaint after termination
    expect(s.seen.length).toBe(commits);
  });
});

describe('ownership (TEST 6, M-10)', () => {
  it('a frame booked by one stream cannot speak for the next', async () => {
    const { startStream, subscribeStreams, getLiveStream } = await import(
      '@/lib/streams'
    );
    const first = controllable();
    const second = controllable();
    let body = first.body;
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: true, status: 200, body })),
    );

    const runA = startStream({
      conversationId: 'chat-A',
      turns: [
        { id: 'u1', role: 'user', content: 'A?', status: 'done', createdAt: 1 },
      ] as ChatMessage[],
      prefs: PREFS,
    });
    await settle();
    first.enqueue(tokenEvent('first'));
    await settle();

    const seen: string[] = [];
    const unsubscribe = subscribeStreams((id) => seen.push(id));
    // A frame is now booked for chat-A and has not run.
    first.enqueue(tokenEvent(' pending'));
    await settle();
    expect(seen.length).toBe(0);
    const booked = frames.filter(Boolean);
    expect(booked).toHaveLength(1);
    const stale = booked[0] as FrameRequestCallback;

    // chat-A's generation is replaced by a NEW stream for the same id before
    // that frame runs — the reload / re-send case M-10 is about.
    body = second.body;
    const runB = startStream({
      conversationId: 'chat-A',
      turns: [
        { id: 'u2', role: 'user', content: 'B?', status: 'done', createdAt: 2 },
      ] as ChatMessage[],
      prefs: PREFS,
    });
    await settle();

    seen.length = 0;
    paint(); // the OLD stream's callback fires here
    expect(seen).toEqual([]); // and says nothing

    // Belt and braces: fire the old callback DIRECTLY, as an engine that ran
    // it despite the cancellation would. The frame carries the identity of
    // the stream that booked it, so it stays silent on its own merits and not
    // merely because `register` got to cancel it first.
    seen.length = 0;
    stale(0);
    expect(seen).toEqual([]);

    // The live view belongs to the new stream, with none of A's text in it.
    expect(getLiveStream('chat-A')?.messages.at(-1)?.content).toBe('');
    expect(getLiveStream('chat-A')?.messages[0].content).toBe('B?');

    second.enqueue('event: done\ndata: {}\n\n');
    second.finish();
    first.enqueue('event: done\ndata: {}\n\n');
    first.finish();
    await Promise.all([runA, runB]);
    unsubscribe();
  });
});

describe('unsubscribed view (TEST 7)', () => {
  it('a pending frame updates nothing once the view has gone', async () => {
    const s = await openStream('c1');
    s.enqueue(tokenEvent('one'));
    await settle();
    s.enqueue(tokenEvent(' two'));
    await settle();

    s.unsubscribe(); // the view unmounted
    s.seen.length = 0;
    paint();
    expect(s.seen).toEqual([]);

    // The stream itself is untouched — the text is all still there.
    expect(s.content()).toBe('one two');
    s.enqueue('event: done\ndata: {}\n\n');
    s.finish();
    await s.running;
    expect(saved.at(-1)?.messages.at(-1)?.content).toBe('one two');
  });
});

describe('long output (TEST 9)', () => {
  it('2000 deltas reproduce exactly and commit no more than once per frame', async () => {
    const s = await openStream('c1');
    s.seen.length = 0;
    const parts: string[] = [];
    for (let i = 0; i < 2000; i += 1) {
      const text = i % 50 === 49 ? `\n\n## Part ${i}\n\n` : `w${i} `;
      parts.push(text);
      s.enqueue(tokenEvent(text));
      // eslint-disable-next-line no-await-in-loop
      await settle();
      if (i % 25 === 0) paint(); // a display frame every 25 deltas
    }
    s.enqueue('event: done\ndata: {}\n\n');
    s.finish();
    await s.running;

    expect(saved.at(-1)?.messages.at(-1)?.content).toBe(parts.join(''));
    // 2000 deltas, 80 painted frames: commits are bounded by frames, not by
    // tokens. (The first token commits immediately, hence the +1 headroom.)
    expect(s.seen.length).toBeLessThanOrEqual(82);
  });
});

describe('event types stay separate', () => {
  it('only token text reaches the answer; reasoning and status do not', async () => {
    const s = await openStream('c1');
    s.enqueue(`event: reasoning\ndata: ${JSON.stringify({ text: 'thinking' })}\n\n`);
    s.enqueue(tokenEvent('answer'));
    s.enqueue(`event: status\ndata: ${JSON.stringify({ text: 'Searching…' })}\n\n`);
    s.enqueue(tokenEvent(' text'));
    await settle();
    s.enqueue('event: done\ndata: {}\n\n');
    s.finish();
    await s.running;

    const last = saved.at(-1)?.messages.at(-1);
    expect(last?.content).toBe('answer text');
    expect(last?.reasoning).toBe('thinking');
    // A terminal patch retires the live progress line.
    expect(last?.searchStatus).toBeUndefined();
  });
});
