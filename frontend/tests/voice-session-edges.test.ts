/**
 * The recorder's edges, fixed on fix/voice-recorder-edges (2026-09-29), at the
 * level of one VoiceSession. Each test states what the person was promised;
 * the defects were confirmed by the two verdicts on
 * feat/frontend-recorder-and-truth (agents a7720217aef71e0d9 and
 * aa5cae578cec429ac) and by the coordinator's review.
 *
 *   defect 3  no deadline on any request: one half-open PUT stalled every part
 *             behind it (7 PUTs, 6 of 138 slices stored in 11 minutes).
 *   defect 8  offline longer than the server's 600 s idle close: the 132
 *             slices recorded offline were deleted from IndexedDB on
 *             reconnect. They now go into a NEW session that `continues` the
 *             closed one, or stay on this device with "Upload the rest".
 *   defect 1  X while the server was unreachable dropped everything here and
 *             left the "discarded" recording on the server.
 *   defect 4  Retry after a one-character edit appended the whole transcript
 *             a second time (79,889 -> 159,786 characters).
 *   defect 5  one origin-wide outbox for every account on a browser.
 *
 * The server is tests/voice-edge-server.ts, which carries the backend's rules
 * (idle close, one live recording per person, 404 for someone else's
 * session) that the single-session fake leaves out.
 */
import { createHash } from 'node:crypto';
import 'fake-indexeddb/auto';
import { IDBFactory } from 'fake-indexeddb';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  DEFAULT_SESSION_CONFIG,
  VOICE_MESSAGES,
  VoiceSession,
  createMemoryOutbox,
  describeOutcome,
  flushTombstones,
  mergeEdits,
  openOutbox,
  openOwnerOutbox,
  outboxDbName,
  parseSessionState,
  placeRetranscript,
  replaceTranscript,
  shiftSpan,
  tombstonesFor,
  wipeOtherOutboxes,
  type ContainerCuts,
  type OutboxStore,
  type SessionInterrupt,
  type SessionProgress,
  type SessionResult,
} from '@/lib/voice';
import { EDGE_SLICE, EdgeServer, edgeSlice } from './voice-edge-server';
import { chromeSlices, concat, walk } from './webm-walk';

// Each test drives minutes of fake time and waits, bounded by the wall
// clock, for real async work (WebCrypto, IndexedDB). Under the full suite's
// parallel load that takes far longer than vitest's 5 s default, and a test
// cut off there keeps running into the next one.
vi.setConfig({ testTimeout: 60_000 });

const turn = () => new Promise<void>((resolve) => setImmediate(resolve));
const sha256 = async (bytes: Uint8Array) => createHash('sha256').update(bytes).digest('hex');
const range = (from: number, to: number) => Array.from({ length: to - from }, (_, i) => from + i);

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
});
afterEach(() => {
  vi.useRealTimers();
});

/** Let fake time pass in steps, with real turns in between for the async work to land. */
async function pass(ms: number, step = 1000, turns = 10): Promise<void> {
  for (let t = 0; t < ms; t += step) {
    await vi.advanceTimersByTimeAsync(Math.min(step, ms - t));
    for (let i = 0; i < turns; i += 1) await turn();
  }
}

/**
 * A container stand-in: the init segment is 8 bytes of 0xEE and every cut's
 * lead 4 bytes of 0xDD, so a continuation's part 0 must start with exactly
 * those 12 bytes. (The real WebM tracker is exercised on real Chrome bytes
 * below and in voice-webm-cuts.test.ts.)
 */
const FRONT = new Uint8Array([...new Array(8).fill(0xee), ...new Array(4).fill(0xdd)]);
const stubCuts = (): ContainerCuts => ({
  init: new Uint8Array(8).fill(0xee),
  lead: () => new Uint8Array(4).fill(0xdd),
  feed: () => undefined,
});

function harness(
  server: EdgeServer,
  store: OutboxStore = createMemoryOutbox(),
  opts: { cuts?: (mime: string) => ContainerCuts | null; mimeType?: string } = {},
) {
  const progress: SessionProgress[] = [];
  const interrupts: SessionInterrupt[] = [];
  return {
    server,
    store,
    progress,
    interrupts,
    async open() {
      // The create goes through the server, as the recorder's does.
      const res = await server.fetch('/api/audio/sessions', {
        method: 'POST',
        body: JSON.stringify({ client_key: `k-${Math.random()}`, mime_type: 'audio/webm' }),
      });
      const state = parseSessionState(JSON.parse(await res.text()))!;
      const session = new VoiceSession(
        {
          sessionId: state.sessionId,
          mimeType: opts.mimeType ?? 'audio/webm;codecs=opus',
          config: { ...DEFAULT_SESSION_CONFIG, idleCloseS: server.idleS },
        },
        {
          fetchImpl: server.fetch as unknown as typeof fetch,
          store,
          random: () => 0.5,
          sha256,
          cuts: opts.cuts ?? stubCuts,
        },
        { onProgress: (p) => progress.push(p), onInterrupt: (i) => interrupts.push(i) },
      );
      await session.open();
      return session;
    },
  };
}

/** One 5 s slice per step; the recorder does not wait for the network. */
async function record(session: VoiceSession, from: number, count: number, blob = edgeSlice): Promise<void> {
  for (let idx = from; idx < from + count; idx += 1) {
    session.addSlice(blob(idx), (idx + 1) * 5000);
    await pass(5000, 5000, 12);
  }
}

const textOf = (r: SessionResult | null): string | null => (r && r.kind === 'text' ? r.text : null);
const realBlob = (i: number) => new Blob([chromeSlices[i]!.slice().buffer as ArrayBuffer], { type: 'audio/webm' });

/** Every slice the server holds for a recording, across its sessions in order. */
function stored(server: EdgeServer): number[] {
  return [...server.sessions.values()].flatMap((s) => s.slices);
}

// ---------------------------------------------------------------------------
// defect 3: deadlines
// ---------------------------------------------------------------------------

describe('defect 3: a request that never answers', () => {
  it('a PUT on a half-open socket is given up and sent again; every slice of eleven minutes reaches the server', async () => {
    const server = new EdgeServer();
    server.idleS = 1e9; // this test is about the socket, not the idle close
    const h = harness(server);
    const session = await h.open();
    await record(session, 0, 6);
    expect(stored(server)).toEqual(range(0, 6));
    server.hangPuts = true;
    await record(session, 6, 132);
    server.hangPuts = false;
    session.nudge(); // the browser's `online`
    await pass(60_000);
    // Before 2026-09-29: 7 PUTs, 6 of 138 slices stored (tests the verifier ran on 74ad85e2).
    expect(stored(server)).toEqual(range(0, 138));
  }, 60_000);

  it('the online event restarts a stalled request at once, without waiting for its deadline', async () => {
    const server = new EdgeServer();
    const h = harness(server);
    const session = await h.open();
    await record(session, 0, 1);
    server.hangPuts = true;
    session.addSlice(edgeSlice(1), 10_000);
    await pass(2000, 500); // the PUT is in flight and will not answer
    server.hangPuts = false;
    session.nudge();
    await pass(500, 100); // well inside the part's own 20 s+ deadline
    expect(stored(server)).toEqual([0, 1]);
  });

  it('a finish that never answers is sent again, and the transcript still arrives', async () => {
    const server = new EdgeServer();
    const h = harness(server);
    const session = await h.open();
    await record(session, 0, 3);
    server.hangFinishes = 1;
    let result: SessionResult | null = null;
    void session.end('person', 15_000).then((r) => (result = r));
    await pass(120_000, 2000);
    expect(textOf(result)).toBe('w0 w1 w2');
  });

  it('a long-poll that never answers is asked again, and the transcript still arrives', async () => {
    const server = new EdgeServer();
    const h = harness(server);
    const session = await h.open();
    await record(session, 0, 3);
    server.hangPolls = 1;
    let result: SessionResult | null = null;
    void session.end('person', 15_000).then((r) => (result = r));
    await pass(120_000, 2000);
    expect(textOf(result)).toBe('w0 w1 w2');
  });
});

// ---------------------------------------------------------------------------
// defect 8: offline for longer than the server waits
// ---------------------------------------------------------------------------

describe('defect 8: offline for longer than the server idle close (600 s)', () => {
  it('eleven minutes offline while recording: the rest goes into a new session that continues the first, and nothing is lost', async () => {
    const server = new EdgeServer();
    server.frontBytes = FRONT.byteLength;
    const h = harness(server, await openOutbox(new IDBFactory()));
    const session = await h.open();
    const first = [...server.sessions.keys()][0]!;
    await record(session, 0, 24); // two minutes online
    server.online = false;
    await record(session, 24, 132); // eleven minutes offline; the server closes it at ten
    expect(server.sessions.get(first)!.endedBy).toBe('idle');
    expect(h.progress.some((p) => p.offlineLong)).toBe(true);
    server.online = true;
    session.nudge();
    await pass(30_000);
    await record(session, 156, 2); // still recording: nothing stopped it
    expect(h.interrupts).toEqual([]);
    const result = session.end('person', 158 * 5000);
    await pass(30_000);
    const settled = await result;

    const sessions = [...server.sessions.values()];
    expect(sessions).toHaveLength(2);
    expect(sessions[0]!.slices).toEqual(range(0, 24));
    expect(sessions[1]!.continues).toBe(first);
    // Byte for byte: the server decodes a linked continuation after the bytes it continues.
    expect(sessions[1]!.front).toBeNull();
    // Before 2026-09-29: 24 slices on the server, 0 left on the device (132 deleted).
    expect(stored(server)).toEqual(range(0, 158));
    expect(settled.kind).toBe('text');
    if (settled.kind === 'text') {
      expect(settled.text).toBe(range(0, 158).map((i) => `w${i}`).join(' '));
    }
  }, 60_000);

  it('Stop pressed offline and the connection back twelve minutes later: the tail still goes up, as a continuation', async () => {
    const server = new EdgeServer();
    server.frontBytes = FRONT.byteLength;
    const h = harness(server, await openOutbox(new IDBFactory()));
    const session = await h.open();
    await record(session, 0, 12);
    server.online = false;
    await record(session, 12, 60); // five minutes offline, then Stop
    const result = session.end('person', 72 * 5000);
    await pass(7 * 60_000, 30_000);
    server.online = true;
    session.nudge();
    await pass(60_000);
    const settled = await result;
    // Before 2026-09-29: 60 of the 72 slices (5:00) were deleted.
    expect(stored(server)).toEqual(range(0, 72));
    expect(textOf(settled)?.split(' ')).toHaveLength(72);
  }, 60_000);

  it('a continuation the server refuses leaves the rest on this device, says so, and "Upload the rest" sends it later', async () => {
    const server = new EdgeServer();
    server.frontBytes = FRONT.byteLength;
    server.refuseContinuation = { status: 409, reason: 'session_active' };
    const store = await openOutbox(new IDBFactory());
    const h = harness(server, store);
    const session = await h.open();
    const first = [...server.sessions.keys()][0]!;
    await record(session, 0, 24);
    server.online = false;
    await record(session, 24, 132);
    server.online = true;
    session.nudge();
    await pass(10_000);
    const result = session.end('person', 156 * 5000);
    await pass(20_000);
    const settled = await result;
    expect(stored(server)).toEqual(range(0, 24));
    // Kept on this device, not deleted: every one of the 132.
    expect((await store.readSlices(first, 0, 1000)).map((s) => s.idx)).toEqual(range(24, 156));
    expect(settled.kind).toBe('text');
    if (settled.kind !== 'text') return;
    expect(settled.notices[0]).toBe(
      "This recording was closed after 10 minutes with no audio arriving. Everything up to 2:00 is saved on the server. The last 11:00 is kept on this device, because the server would not take it after that. You're not being recorded now.",
    );
    expect(settled.offer).toMatchObject({ kind: 'upload_rest', label: 'Upload the rest', replaces: settled.text });

    // Later, the person presses "Upload the rest".
    server.refuseContinuation = null;
    const record0 = (await store.loadRecord(first))!;
    expect(record0.held).toMatchObject({ reason: 'session_closed', endedBy: 'idle' });
    const again = await VoiceSession.adopt(
      record0,
      { fetchImpl: server.fetch as unknown as typeof fetch, store, random: () => 0.5, sha256, cuts: stubCuts },
      {},
      { continueAnyClose: true },
    );
    const second = again.end('person', record0.durationMs);
    await pass(30_000);
    const done = await second;
    expect(stored(server)).toEqual(range(0, 156));
    expect(textOf(done)).toBe(range(0, 156).map((i) => `w${i}`).join(' '));
    expect(await store.readSlices(first, 0, 1000)).toEqual([]);
  }, 60_000);

  it('from REAL Chrome bytes: the linked continuation is the rest of the stream byte for byte, which the server decodes after what it continues', async () => {
    const server = new EdgeServer();
    server.keepBytes = true;
    const h = harness(server, await openOutbox(new IDBFactory()), { cuts: undefined });
    const session = await realSession(server, h.store);
    await record(session, 0, 2, realBlob);
    server.online = false;
    await record(session, 2, 2, realBlob);
    await pass(11 * 60_000, 30_000); // the server closes it while this device holds slices 2 and 3
    server.online = true;
    session.nudge();
    await pass(30_000);
    const result = session.end('person', 20_000);
    await pass(10_000);
    await result;
    const [original, continuation] = [...server.sessions.values()];
    expect(continuation!.continues).toBe(original!.id);
    expect(concat(original!.bytes)).toEqual(concat(chromeSlices.slice(0, 2)));
    // It does not open like a WebM file (so the server chains it) ...
    expect([...concat(continuation!.bytes).subarray(0, 4)]).not.toEqual([0x1a, 0x45, 0xdf, 0xa3]);
    // ... and after the bytes it continues it is exactly the recording: the
    // one byte of the cut block that ended the first session included.
    expect(concat([...original!.bytes, ...continuation!.bytes])).toEqual(concat(chromeSlices));
    expect(walk(concat([...original!.bytes, ...continuation!.bytes])).blocks).toEqual(walk(concat(chromeSlices)).blocks);
  }, 60_000);

  it('from REAL Chrome bytes: the rest of a recording closed for a full disk goes up, when asked, as a recording of its own that opens as a stream', async () => {
    const server = new EdgeServer();
    server.keepBytes = true;
    const store = await openOutbox(new IDBFactory());
    const session = await realSession(server, store);
    await record(session, 0, 2, realBlob);
    const real = server.fetch.getMockImplementation()!;
    let full = true;
    server.fetch.mockImplementation(async (input, init = {}) => {
      if (full && (init.method ?? 'GET') === 'PUT') {
        for (const x of server.sessions.values()) {
          if (x.status === 'recording') {
            x.status = 'finishing';
            x.endedBy = 'storage_full';
          }
        }
        const body = JSON.stringify({ detail: 'full', reason: 'storage_full' });
        return { ok: false, status: 507, headers: { get: () => null }, text: async () => body } as unknown as Response;
      }
      return real(input, init);
    });
    await record(session, 2, 2, realBlob);
    const first = session.end('person', 20_000);
    await pass(20_000);
    const held = await first;
    expect(held.kind === 'withdrawn' ? null : held.offer?.kind).toBe('upload_rest');
    const original = [...server.sessions.values()][0]!;
    full = false; // space again; the person presses "Upload the rest"
    const again = await VoiceSession.adopt(
      (await store.loadRecord(original.id))!,
      { fetchImpl: server.fetch as unknown as typeof fetch, store, random: () => 0.5, sha256 },
      {},
      { continueAnyClose: true },
    );
    const second = again.end('person', 20_000);
    await pass(30_000);
    await second;
    const own = [...server.sessions.values()][1]!;
    expect(own.continues).toBeNull(); // the server links only idle-closed recordings
    // It opens as WebM, and holds exactly the blocks from the cut on, at their times.
    const bytes = concat(own.bytes);
    expect([...bytes.subarray(0, 4)]).toEqual([0x1a, 0x45, 0xdf, 0xa3]);
    const cut = chromeSlices[0]!.byteLength + chromeSlices[1]!.byteLength;
    const expected = walk(concat(chromeSlices)).blocks.filter((b) => b.at + 3 + b.size > cut);
    expect(walk(bytes).blocks.map((b) => [b.t, b.size])).toEqual(expected.map((b) => [b.t, b.size]));
    expect(await store.readSlices(original.id, 0, 100)).toEqual([]);
  }, 60_000);
});

async function realSession(server: EdgeServer, store: OutboxStore): Promise<VoiceSession> {
  const res = await server.fetch('/api/audio/sessions', {
    method: 'POST',
    body: JSON.stringify({ client_key: `real-${Math.random()}`, mime_type: 'audio/webm' }),
  });
  const state = parseSessionState(JSON.parse(await res.text()))!;
  // No stub: the recorder's own WebmCutTracker reads the real slices.
  const s = new VoiceSession(
    { sessionId: state.sessionId, mimeType: 'audio/webm;codecs=opus', config: DEFAULT_SESSION_CONFIG },
    { fetchImpl: server.fetch as unknown as typeof fetch, store, random: () => 0.5, sha256 },
  );
  await s.open();
  return s;
}

// ---------------------------------------------------------------------------
// defect 1: a discard is owed until the server confirms it
// ---------------------------------------------------------------------------

function memoryStorage() {
  const m = new Map<string, string>();
  return {
    getItem: (k: string) => m.get(k) ?? null,
    setItem: (k: string, v: string) => void m.set(k, v),
    removeItem: (k: string) => void m.delete(k),
    get length() {
      return m.size;
    },
    key: (i: number) => [...m.keys()][i] ?? null,
  };
}

describe('defect 1: X while the server cannot be reached', () => {
  it('is never reported done before the server said so, and the DELETE is owed until it does', async () => {
    const server = new EdgeServer();
    const storage = memoryStorage();
    const tombstones = tombstonesFor('u1', storage);
    const h = harness(server);
    const res = await server.fetch('/api/audio/sessions', {
      method: 'POST',
      body: JSON.stringify({ client_key: 'x', mime_type: 'audio/webm' }),
    });
    const id = parseSessionState(JSON.parse(await res.text()))!.sessionId;
    const session = new VoiceSession(
      { sessionId: id, mimeType: 'audio/webm', config: DEFAULT_SESSION_CONFIG },
      { fetchImpl: server.fetch as unknown as typeof fetch, store: h.store, random: () => 0.5, sha256, tombstones },
    );
    await session.open();
    await record(session, 0, 24);
    server.online = false;
    let outcome: string | undefined;
    void session.discard().then((o) => (outcome = o));
    await pass(20_000);
    expect(outcome).toBe('pending');
    expect(server.sessions.get(id)!.status).toBe('recording');
    // Durable: the owed DELETE is in this account's storage, holding no audio.
    expect(tombstonesFor('u1', storage).list().map((t) => t.sessionId)).toEqual([id]);
    expect(await h.store.listRecords()).toEqual([]);

    server.online = true;
    const flushed = await flushTombstones(tombstonesFor('u1', storage), {
      fetchImpl: server.fetch as unknown as typeof fetch,
    });
    expect(flushed).toEqual({ deleted: [id], pending: [] });
    expect(server.sessions.get(id)!.status).toBe('cancelled');
    expect(tombstonesFor('u1', storage).list()).toEqual([]);
  });

  it('a discard owed by one account is never sent with another account’s cookie', () => {
    const storage = memoryStorage();
    tombstonesFor('u1', storage).add('a'.repeat(32), 1);
    expect(tombstonesFor('u2', storage).list()).toEqual([]);
  });
});

// ---------------------------------------------------------------------------
// defect 5: one outbox per account
// ---------------------------------------------------------------------------

describe('defect 5: accounts on a shared browser', () => {
  it('each account has its own outbox database, and the others are deleted when a different account is seen', async () => {
    const factory = new IDBFactory();
    const storage = memoryStorage();
    const a = await openOwnerOutbox('u1', factory, storage);
    await a.saveRecord({
      sessionId: 'a'.repeat(32),
      mimeType: 'audio/webm',
      partMs: 5000,
      partLimitBytes: 1,
      idleCloseS: 600,
      aliveAt: 0,
      nextSeq: 0,
      nextSlice: 1,
      firstUnacked: 0,
      formed: null,
      ended: true,
      endedBy: 'person',
      durationMs: 5000,
      ackedMs: 0,
    });
    await a.putSlice('a'.repeat(32), 0, 5000, edgeSlice(0));
    const b = await openOwnerOutbox('u2', factory, storage);
    expect(await b.listRecords()).toEqual([]); // B never even sees A's record
    await wipeOtherOutboxes('u2', factory, storage);
    const names = (await factory.databases()).map((d) => d.name).sort();
    expect(names).toEqual([outboxDbName('u2')]);
    expect(outboxDbName('u2')).toBe('techsara-voice-outbox:u2');
  });
});

// ---------------------------------------------------------------------------
// defect 4: Retry never doubles the transcript
// ---------------------------------------------------------------------------

describe('defect 4: a re-transcribed recording goes where the first one went', () => {
  const first = Array.from({ length: 9000 }, (_, i) => `word${i}`).join(' ');

  it('never appends a second copy when the first was edited (the verifier’s 9,000-word case)', () => {
    const edited = first.replace('word17', 'Word17');
    const again = `${first} filled`;
    const out = replaceTranscript(edited, first, again);
    // Before 2026-09-29: 79,889 -> 159,786 characters (ratio 2.00).
    expect(out.length).toBeLessThan(edited.length * 1.2);
  });

  it('keeps the person’s edit AND fills the gaps, when it knows where the transcript went', () => {
    const base = 'we met on monday and agreed the budget';
    const filled = 'we met on monday at ten and agreed the budget';
    const draft = `Notes: ${base.replace('budget', 'Budget')}`;
    const span = { start: 7, end: draft.length };
    const placed = placeRetranscript(draft, span, base, filled);
    expect(placed?.text).toBe('Notes: we met on monday at ten and agreed the Budget');
    expect(placed?.span).toEqual({ start: 7, end: placed!.text.length });
  });

  it('asks rather than guessing when the person changed the very words the Retry changes', () => {
    // The person wrote Tuesday where the first transcript said monday; the
    // new transcript hears that same word as Monday. Neither can win quietly.
    expect(mergeEdits('we met on monday', 'we met on Tuesday', 'we met on Monday at ten')).toBeNull();
    const draft = 'we met on Tuesday';
    expect(placeRetranscript(draft, { start: 0, end: draft.length }, 'we met on monday', 'we met on Monday at ten')).toBeNull();
    // An edit next to a gap is not a collision: both survive.
    expect(mergeEdits('we met on monday', 'we met on Tuesday', 'we met on monday at ten')).toBe('we met on Tuesday at ten');
  });

  it('asks when it cannot tell where the transcript went (the span is lost and the text is not there verbatim)', () => {
    expect(placeRetranscript('something else entirely', null, 'we met on monday', 'we met on monday at ten')).toBeNull();
  });

  it('follows the transcript through the person’s edits before, inside and after it', () => {
    const before = 'Hi. we met on monday';
    const span = { start: 4, end: before.length };
    expect(shiftSpan(span, before, `Hello there. ${before.slice(4)}`)).toEqual({ start: 13, end: 29 });
    expect(shiftSpan(span, before, `${before} ok`)).toEqual(span);
    expect(shiftSpan(span, before, 'Hi. we met on Monday')).toEqual(span);
    expect(shiftSpan(span, before, 'Hi. ')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// what the person is told
// ---------------------------------------------------------------------------

describe('outcomes the server may add', () => {
  it("a 'partial' transcript (the decoder died part-way) is treated like one with gaps: the words, and a Retry", () => {
    const state = parseSessionState({
      session_id: 'a'.repeat(32),
      status: 'done',
      outcome: 'partial',
      text: 'what was heard',
      audio_ms: 60_000,
      gaps: [{ start_ms: 40_000, end_ms: 60_000, reason: 'engine_unavailable' }],
    })!;
    const result = describeOutcome(state);
    expect(result.kind).toBe('text');
    expect(result.kind === 'text' && result.offer?.kind).toBe('retranscribe');
  });
});

describe('the server’s capacity, in words', () => {
  it('an engine that cannot be reached is not called busy with other recordings', () => {
    const state = parseSessionState({ session_id: 'a'.repeat(32), status: 'recording', waiting_on: 'engine_unavailable' });
    expect(state?.waitingOn).toBe('engine_unavailable');
    expect(VOICE_MESSAGES.behind('0:42', 'engine_unavailable')).not.toMatch(/busy with other recordings/);
    expect(VOICE_MESSAGES.engineUnavailableLive).toBe(
      'The speech service is unavailable right now; your audio is saved and will be transcribed when it is back.',
    );
  });

  it('a continuation waiting for its earlier part from the voice archive says so, and that nothing is lost', () => {
    // The server's `waiting_on: "archive"` (orchestrator/app/dictation.py
    // _Live._bring_back): the recording this one continues moved to the
    // archive server, which is not answering; the session waits for it.
    const state = parseSessionState({ session_id: 'a'.repeat(32), status: 'recording', waiting_on: 'archive' });
    expect(state?.waitingOn).toBe('archive');
    expect(VOICE_MESSAGES.behind('2:00', 'archive')).toBe(
      'Transcript 2:00 behind — the earlier part of this recording is on the archive server, which isn’t answering right now; nothing is lost',
    );
  });

  it('a full quota mid-recording keeps the rest on this device and points at the Recordings page', async () => {
    const server = new EdgeServer();
    const h = harness(server);
    const session = await h.open();
    await record(session, 0, 4);
    const refuse = server.fetch.getMockImplementation()!;
    server.fetch.mockImplementation(async (input, init = {}) => {
      if ((init.method ?? 'GET') === 'PUT') {
        // As the server does on its 507s: this part is not stored and the
        // session is finished with what it has.
        for (const s of server.sessions.values()) {
          if (s.status === 'recording') {
            s.status = 'finishing';
            s.endedBy = 'storage_full';
          }
        }
        const body = JSON.stringify({ detail: 'quota', reason: 'quota_full' });
        return { ok: false, status: 507, headers: { get: () => null }, text: async () => body } as unknown as Response;
      }
      return refuse(input, init);
    });
    await record(session, 4, 1);
    const result = session.end('person', 25_000);
    await pass(10_000);
    const settled = await result;
    const message = settled.kind === 'text' ? settled.notices[0] : settled.kind === 'error' ? settled.error.message : '';
    expect(message).toBe(
      'Your recordings have used all the space your account has, so recording stopped at 0:20. Everything up to then is saved; the last 0:05 is kept on this device. Delete some on the Recordings page (/recordings), then press Upload the rest.',
    );
  });
});

void EDGE_SLICE;
