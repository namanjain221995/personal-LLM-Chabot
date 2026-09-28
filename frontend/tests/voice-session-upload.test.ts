/**
 * The session road's upload protocol (2026-09-29), driven against a fake
 * orchestrator that implements the contract (tests/voice-session-fake.ts).
 *
 * WHAT THE OWNER ASKED FOR, AND WHAT EACH TEST HERE HOLDS THE CODE TO.
 *
 *   "as long as i talk More then 1 hr" — two hours is the stress case: 1,440
 *   parts of 5 s, and nothing stops, refuses or runs out.
 *
 *   A phone cannot hold an hour. The old recorder kept every chunk until Stop
 *   and posted one body: 9,652,200 bytes for ten minutes, measured against
 *   origin/dev on 2026-09-29, which would be 115.8 MB for two hours. Here the
 *   tab holds only what the server has not acknowledged.
 *
 *   A dropped network loses nothing. Every part stays on the device until a
 *   200 says it is on the server's disk, and is sent again, byte for byte,
 *   until it is. The server's copy is checked slice by slice: 0, 1, 2 … N-1,
 *   no gap and no repeat, however many requests failed on the way.
 */
import { createHash } from 'node:crypto';
import 'fake-indexeddb/auto';
import { IDBFactory } from 'fake-indexeddb';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  DEFAULT_SESSION_CONFIG,
  OUTBOX_STALE_MS,
  VoiceSession,
  createMemoryOutbox,
  openOutbox,
  type OutboxStore,
  type SessionInterrupt,
  type SessionProgress,
} from '@/lib/voice';
import {
  FakeSessionServer,
  SESSION_ID,
  SLICE_BYTES,
  sliceBlob,
  type FakeOptions,
} from './voice-session-fake';

/** One real event-loop turn: Blob reads and SHA-256 resolve on these, not on timers. */
const turn = () => new Promise<void>((resolve) => setImmediate(resolve));

async function until(cond: () => boolean, turns = 400): Promise<void> {
  for (let i = 0; i < turns; i += 1) {
    if (cond()) return;
    await turn();
  }
  throw new Error('condition never became true');
}

beforeEach(() => {
  // setImmediate stays real: it is how these tests let real async work land.
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
});
afterEach(() => {
  vi.useRealTimers();
});

function harness(options: FakeOptions = {}, store: OutboxStore = createMemoryOutbox()) {
  const server = new FakeSessionServer(options);
  server.status = 'recording';
  const progress: SessionProgress[] = [];
  const interrupts: SessionInterrupt[] = [];
  const session = new VoiceSession(
    { sessionId: SESSION_ID, mimeType: 'audio/webm;codecs=opus', config: DEFAULT_SESSION_CONFIG },
    { fetchImpl: server.fetch as unknown as typeof fetch, store, random: () => 0.5, sha256 },
    { onProgress: (p) => progress.push(p), onInterrupt: (i) => interrupts.push(i) },
  );
  return { server, session, progress, interrupts, store };
}

/**
 * SHA-256 without the thread pool, so a hash lands on the next turn rather
 * than whenever libuv gets to it. The fake server checks it with node:crypto
 * too; `sha256Hex` (WebCrypto) is exercised by the recorder tests.
 */
const sha256 = async (bytes: Uint8Array) => createHash('sha256').update(bytes).digest('hex');

/**
 * Record `count` slices of 5 s. With `settle`, each upload is allowed to land
 * before the next slice arrives, which is what a connection that keeps up
 * with 128.7 kb/s does; without it the slices simply keep coming.
 */
async function talk(
  h: ReturnType<typeof harness>,
  from: number,
  count: number,
  onEach?: (idx: number) => void,
  settle = true,
): Promise<void> {
  for (let idx = from; idx < from + count; idx += 1) {
    h.session.addSlice(sliceBlob(idx), (idx + 1) * 5000);
    onEach?.(idx);
    await vi.advanceTimersByTimeAsync(5000);
    if (settle) await until(() => h.server.appendedSlices.length >= idx + 1 || h.interrupts.length > 0);
    else for (let t = 0; t < 20; t += 1) await turn();
  }
}

function range(n: number): number[] {
  return Array.from({ length: n }, (_, i) => i);
}

describe('parts go up while the person is still talking', () => {
  it('sends each 5 s slice as the next numbered part, one at a time, hashed', async () => {
    const h = harness();
    await h.session.open();
    await talk(h, 0, 6);
    await until(() => h.server.nextPart === 6);

    const puts = h.server.log.filter((r) => r.method === 'PUT');
    expect(puts.map((r) => r.seq)).toEqual([0, 1, 2, 3, 4, 5]);
    // The fake server recomputes the SHA-256 of every body and refuses a
    // mismatch with 422, so six accepted parts prove six correct hashes.
    expect(h.server.appendedSlices).toEqual(range(6));
    expect(puts.every((r) => r.bytes === SLICE_BYTES)).toBe(true);
    expect(puts[2]!.headers['x-part-end-ms']).toBe('15000');
    expect(puts[0]!.headers['content-type']).toBe('audio/webm;codecs=opus');
  });

  it('brings the transcript back with the parts, as it forms', async () => {
    const h = harness();
    await h.session.open();
    await talk(h, 0, 3);
    await until(() => h.server.nextPart === 3);
    const last = h.progress[h.progress.length - 1]!;
    expect(last.preview).toBe('w0 w1 w2');
    // The browser asks only for segments it does not have yet.
    const cursors = h.server.log.filter((r) => r.method === 'PUT').map((r) => new URL(r.url, 'http://x').searchParams.get('cursor'));
    expect(cursors).toEqual(['0', '1', '2']);
  });
});

describe('a part that fails is sent again and the session carries on', () => {
  it('survives a network error, a lost acknowledgement and two refusals on one part', async () => {
    // Part 3: no response at all, twice; then the server stores it but the
    // answer is lost (the replay must be recognised as a duplicate, not
    // appended twice); then a 503 and a 429; then it goes through.
    const faults = ['network', 'network', 'drop', 503, 429];
    const h = harness({
      inject: ({ method, seq, attempt }) => {
        if (method !== 'PUT' || seq !== 3 || attempt >= faults.length) return undefined;
        const f = faults[attempt]!;
        if (f === 'network') return { network: true };
        if (f === 'drop') return { dropResponse: true };
        return { status: f as number, body: { detail: 'busy', reason: f === 503 ? 'storage_unavailable' : 'rate_limited' } };
      },
    });
    await h.session.open();
    // The recorder does not wait for the network: slices keep coming.
    await talk(h, 0, 10, undefined, false);
    // Backoff is 1, 2, 4, 8, 16 s: let it run out.
    for (let i = 0; i < 30; i += 1) {
      await vi.advanceTimersByTimeAsync(2000);
      for (let t = 0; t < 10; t += 1) await turn();
    }
    await until(() => h.server.appendedSlices.length === 10);

    const part3 = h.server.putsFor(3);
    expect(part3).toHaveLength(6);
    // The same part every time, not a re-cut: identical size on every attempt.
    expect(new Set(part3.map((r) => r.headers['x-part-sha256'])).size).toBe(1);
    // Nothing lost, nothing doubled, nothing reordered — the lost ack included.
    expect(h.server.appendedSlices).toEqual(range(10));
    expect(h.interrupts).toEqual([]);
    // The person was told the connection dropped while it was down …
    expect(h.progress.some((p) => p.offline)).toBe(true);
    // … and not once it came back.
    expect(h.progress[h.progress.length - 1]!.offline).toBe(false);
  });

  it('coalesces what queued up during an outage into a few parts, under the part limit', async () => {
    let offline = true;
    const h = harness({
      inject: ({ method }) => (method === 'PUT' && offline ? { network: true } : undefined),
    });
    await h.session.open();
    // Five minutes of talking with no connection: 60 slices, 4.8 MB.
    for (let idx = 0; idx < 60; idx += 1) {
      h.session.addSlice(sliceBlob(idx), (idx + 1) * 5000);
      await vi.advanceTimersByTimeAsync(5000);
      for (let t = 0; t < 6; t += 1) await turn();
    }
    offline = false;
    h.session.nudge(); // the browser's `online` event
    await vi.advanceTimersByTimeAsync(31_000);
    await until(() => h.server.appendedSlices.length === 60);
    expect(h.server.appendedSlices).toEqual(range(60));
    // One 80 KB slice was in flight when the outage began; the other 59 went
    // up as one part of 4.7 MB rather than 59 requests.
    expect(h.server.nextPart).toBeLessThanOrEqual(3);
    for (const r of h.server.log.filter((x) => x.method === 'PUT')) {
      expect(r.bytes).toBeLessThanOrEqual(DEFAULT_SESSION_CONFIG.partLimitBytes);
    }
  });

  it('splits a part the server says is too large, and sends it again', async () => {
    let offline = true;
    let refusedOnce = false;
    const h = harness({
      inject: ({ method, seq }) => {
        if (method !== 'PUT') return undefined;
        if (offline) return { network: true };
        // Part 0 is the one slice that was in flight when the outage began;
        // part 1 is the seven that queued behind it, coalesced into one.
        if (!refusedOnce && seq === 1) {
          refusedOnce = true;
          return { status: 413, body: { detail: 'too large', reason: 'part_too_large' } };
        }
        return undefined;
      },
    });
    await h.session.open();
    for (let idx = 0; idx < 8; idx += 1) {
      h.session.addSlice(sliceBlob(idx), (idx + 1) * 5000);
      await vi.advanceTimersByTimeAsync(5000);
      for (let t = 0; t < 6; t += 1) await turn();
    }
    offline = false;
    h.session.nudge();
    await vi.advanceTimersByTimeAsync(40_000);
    await until(() => h.server.appendedSlices.length === 8);
    expect(h.server.appendedSlices).toEqual(range(8));
    expect(h.interrupts).toEqual([]);
    // The refused seven-slice part 1 came back as three slices, then four.
    const bytesOf = (seq: number) => h.server.putsFor(seq).map((r) => r.bytes);
    expect(bytesOf(1)).toEqual([7 * SLICE_BYTES, 3 * SLICE_BYTES]);
    expect(bytesOf(2)).toEqual([4 * SLICE_BYTES]);
  });
});

describe('two hours, the stress case', () => {
  it('uploads 1,440 parts while recording, holds at most two slices, and finishes', async () => {
    const h = harness();
    await h.session.open();
    let peakPending = 0;
    await talk(h, 0, 1440, () => {
      peakPending = Math.max(peakPending, h.session.stats().heldBytes);
    });
    await until(() => h.server.nextPart === 1440);
    const stats = h.session.stats();

    expect(h.server.appendedSlices).toEqual(range(1440));
    expect(stats.slicesAdded).toBe(1440);
    expect(stats.bytesAcked).toBe(1440 * SLICE_BYTES);
    // THE MEMORY BOUND. Two hours is 115,829,280 bytes of audio; the tab
    // never held more than the slice being uploaded plus the one arriving.
    expect(stats.peakHeldBytes).toBeLessThanOrEqual(2 * SLICE_BYTES);
    expect(stats.heldBytes).toBe(0);

    const result = h.session.end('person', 7_200_000);
    await vi.advanceTimersByTimeAsync(0);
    await until(() => h.server.status === 'done', 2000).catch(() => undefined);
    await vi.advanceTimersByTimeAsync(5000);
    const settled = await result;
    expect(settled.kind).toBe('text');
    if (settled.kind !== 'text') return;
    expect(settled.text.split(' ')).toHaveLength(1440);
    expect(h.server.finishBody).toEqual({ last_part: 1439, duration_ms: 7_200_000, ended_by: 'person' });
  }, 120_000);

  it('drops one answer in ten over the same two hours and still stores a byte-identical recording', async () => {
    const h = harness({
      inject: ({ method, seq, attempt }) =>
        method === 'PUT' && seq !== null && seq % 10 === 7 && attempt === 0 ? { dropResponse: true } : undefined,
    });
    await h.session.open();
    await talk(h, 0, 1440);
    await vi.advanceTimersByTimeAsync(10_000);
    await until(() => h.server.appendedSlices.length === 1440, 4000);
    // 144 acknowledgements were lost; every replay was answered as a duplicate.
    expect(h.server.appendedSlices).toEqual(range(1440));
    expect(h.server.log.filter((r) => r.method === 'PUT').length).toBeGreaterThanOrEqual(1440 + 144);
    expect(h.session.stats().peakHeldBytes).toBeLessThanOrEqual(3 * SLICE_BYTES);
  }, 120_000);
});

describe('what the server lost cannot be invented', () => {
  it('ends at what the server holds when it asks for parts this device already let go of', async () => {
    let skip = false;
    const h = harness({
      inject: ({ method, seq }) =>
        method === 'PUT' && seq === 4 && skip
          ? { status: 409, body: { detail: 'gap', reason: 'out_of_order', next_part: 2 } }
          : undefined,
    });
    await h.session.open();
    await talk(h, 0, 4);
    skip = true;
    await talk(h, 4, 1);
    expect(h.interrupts).toEqual([{ kind: 'lost_parts' }]);
    const result = h.session.end('person', 25_000);
    await vi.advanceTimersByTimeAsync(5000);
    await until(() => h.server.status === 'done', 2000).catch(() => undefined);
    await vi.advanceTimersByTimeAsync(5000);
    const settled = await result;
    expect(h.server.finishBody).toMatchObject({ last_part: null, ended_by: 'lost_parts' });
    expect(settled.kind).toBe('text');
    if (settled.kind === 'text') {
      expect(settled.notices[0]).toBe(
        "The part of your recording after 0:20 never reached the server and isn't on this device any more, so the recording ends there. Everything before 0:20 is saved and transcribed.",
      );
    }
  });
});

describe('the outbox on disk (IndexedDB)', () => {
  it('keeps unacknowledged audio on disk, not in the tab', async () => {
    const factory = new IDBFactory();
    const store = await openOutbox(factory);
    expect(store.persistent).toBe(true);
    const h = harness({ inject: ({ method }) => (method === 'PUT' ? { network: true } : undefined) }, store);
    await h.session.open();
    for (let idx = 0; idx < 12; idx += 1) {
      h.session.addSlice(sliceBlob(idx), (idx + 1) * 5000);
      await vi.advanceTimersByTimeAsync(5000);
      for (let t = 0; t < 20; t += 1) await turn();
    }
    // A minute offline: twelve slices waiting, and only the one being retried
    // is in the tab's memory.
    expect(h.session.stats().heldBytes).toBeLessThanOrEqual(SLICE_BYTES);
    const records = await store.listRecords();
    expect(records).toHaveLength(1);
    expect(records[0]!.nextSlice).toBe(12);
  });

  it('a reloaded tab adopts the recording and the server ends up with every slice once', async () => {
    const factory = new IDBFactory();
    let offline = false;
    const inject: FakeOptions['inject'] = ({ method }) =>
      method === 'PUT' && offline ? { network: true } : undefined;
    const first = harness({ inject }, await openOutbox(factory));
    await first.session.open();
    await talk(first, 0, 5);
    await until(() => first.server.nextPart === 5);
    offline = true;
    for (let idx = 5; idx < 11; idx += 1) {
      first.session.addSlice(sliceBlob(idx), (idx + 1) * 5000);
      await vi.advanceTimersByTimeAsync(5000);
      for (let t = 0; t < 20; t += 1) await turn();
    }
    // The tab dies here. Clearing every timer is what a closed tab does to its
    // own: the retry that was waiting never fires and the record stops being
    // touched. A new page opens the same IndexedDB once it has gone stale.
    vi.clearAllTimers();
    offline = false;

    const store = await openOutbox(factory);
    const records = await store.listRecords();
    expect(records).toHaveLength(1);
    const now = Date.now() + 10 * 60_000;
    const claimed = await store.claim(SESSION_ID, now - OUTBOX_STALE_MS, now);
    expect(claimed).not.toBeNull();
    const adopted = await VoiceSession.adopt(claimed!, {
      store,
      fetchImpl: first.server.fetch as unknown as typeof fetch,
      random: () => 0.5,
      sha256,
    });
    const result = adopted.end('page_hidden', claimed!.durationMs);
    for (let i = 0; i < 20; i += 1) {
      await vi.advanceTimersByTimeAsync(2000);
      for (let t = 0; t < 20; t += 1) await turn();
    }
    const settled = await result;
    expect(first.server.appendedSlices).toEqual(range(11));
    expect(first.server.finishBody).toMatchObject({ ended_by: 'page_hidden' });
    expect(settled.kind).toBe('text');
    expect(await store.listRecords()).toEqual([]);
  });

  it('never sends an acknowledged slice again under a new number after a crash between the two writes', async () => {
    const factory = new IDBFactory();
    const store = await openOutbox(factory);
    const h = harness({}, store);
    await h.session.open();
    await talk(h, 0, 3);
    await until(() => h.server.nextPart === 3);
    // The crash: the record already says slices 0-2 are acknowledged, but
    // their bytes were never deleted. Put them back as if the delete failed.
    vi.clearAllTimers(); // the tab that wrote them is gone
    for (const idx of [0, 1, 2]) await store.putSlice(SESSION_ID, idx, (idx + 1) * 5000, sliceBlob(idx));
    const record = (await store.loadRecord(SESSION_ID))!;
    expect(record.firstUnacked).toBe(3);
    const adopted = await VoiceSession.adopt(
      { ...record, aliveAt: 0 },
      { store, fetchImpl: h.server.fetch as unknown as typeof fetch, random: () => 0.5, sha256 },
    );
    const result = adopted.end('page_hidden', 15_000);
    for (let i = 0; i < 10; i += 1) {
      await vi.advanceTimersByTimeAsync(2000);
      for (let t = 0; t < 20; t += 1) await turn();
    }
    await result;
    expect(h.server.appendedSlices).toEqual(range(3));
    expect(h.server.log.filter((r) => r.method === 'PUT')).toHaveLength(3);
  });
});
