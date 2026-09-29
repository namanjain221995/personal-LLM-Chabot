// @vitest-environment jsdom
/**
 * The recorder hook with a live transcript (2026-09-29), at the level the
 * composer uses it: the session road of tests/voice-session-recorder.test.tsx
 * (same fake server, a slice only when a test emits one), plus an
 * AudioContext with an AudioWorklet and a WebSocket whose other ends the test
 * plays (tests/voice-live-fakes.ts).
 *
 * The promises held here:
 *   - ONE context per recording, built at the permission grant (before the
 *     session answers), whose ONE source node feeds the meter and the tap, so
 *     the words spoken while the session is created are heard and replayed;
 *   - at Stop the worklet's last frame and the socket's flush go out BEFORE the
 *     context closes, and the microphone is released at once regardless;
 *   - cancel and pagehide close the socket at once with 1000;
 *   - the live path is best effort: whatever it does, the stored recording
 *     and its transcript are exactly what they would have been without it;
 *   - when the stored transcript cannot be had, the live words are OFFERED,
 *     and said to be part of the recording when the stream missed some;
 *   - the live words redraw at most ten times a second;
 *   - a second Stop inside the stop event's delay no longer strands the
 *     recording (the audit's stop() race);
 *   - at Stop the full pass goes in for English and the complete live
 *     transcript for Hindi or Hinglish (spec sections 10 and 12), one quiet
 *     line says which, and its button swaps the other in over the untouched
 *     words; the full pass's Retry and "Upload the rest" come only with the
 *     full pass, and nothing a reload finds may write over the live words;
 *   - a stream switched from English to Hindi keeps the full pass (spec 12);
 *   - once the full pass is in, the insert waits for the live stream only
 *     while it could still decide, and then for a second at most;
 *   - the bar's language control starts the live stream again in the chosen
 *     language, from the last committed word, once the choice has stood for
 *     half a second;
 *   - a session whose live words decide by themselves (Hindi chosen, or a
 *     fifth Devanagari) gets them the moment its live stream ends, not after
 *     the full pass; the full pass is offered once it is done and never put
 *     in by itself, and its failure leaves the words with a quiet note (spec
 *     13); the recording stays finishing until the server has its finish;
 *   - a stream the server says runs on the English model is English, whatever
 *     the bar asked for (spec 14.2);
 *   - an utterance that starts with the danda or comma of the one before is
 *     joined straight onto it, in the bar and in the draft (spec 14.3).
 */
import { act, cleanup, fireEvent, render, renderHook, screen, within } from '@testing-library/react';
import { IDBFactory, IDBKeyRange } from 'fake-indexeddb';
import { Blob as NodeBlob } from 'node:buffer';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Composer } from '@/components/Composer';
import { useVoiceRecorder } from '@/components/useVoiceRecorder';
import { DEFAULT_PREFS } from '@/lib/prefs';
import { OUTBOX_STALE_MS } from '@/lib/voice';
import { LIVE_LANGUAGE_DEBOUNCE_MS, LIVE_SETTLE_WAIT_MS } from '@/lib/voiceLive';
import { FakeSessionServer, SESSION_ID, sliceBytes, type FakeOptions } from './voice-session-fake';
import {
  FakeLiveAudioContext,
  FakeWebSocket,
  FakeWorkletNode,
  log,
  resetLiveFakes,
} from './voice-live-fakes';

const LIVE = { path: '/api/audio/sessions/{id}/live', sample_rate: 16000, frame_ms: 40, resume_max_s: 60 };

class FakeRecorder {
  static last: FakeRecorder | null = null;
  static isTypeSupported = (type: string) => type === 'audio/webm;codecs=opus';
  /**
   * Chromium's order (measured 2026-09-29): stop() makes the recorder
   * inactive at once, and the last slice and the stop event come 11.2 ms
   * later. Off, this fake fires them inside stop(), as the other files' do.
   */
  static asyncStop = false;
  state: 'inactive' | 'recording' = 'inactive';
  mimeType: string;
  timeslice: number | undefined;
  ondataavailable: ((event: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  onerror: (() => void) | null = null;
  next = 0;
  private owed: (() => void) | null = null;
  constructor(_stream: MediaStream, options?: { mimeType?: string }) {
    this.mimeType = options?.mimeType ?? '';
    FakeRecorder.last = this;
  }
  start(timeslice?: number) {
    this.timeslice = timeslice;
    this.state = 'recording';
  }
  emit() {
    const idx = this.next;
    this.next += 1;
    this.ondataavailable?.({ data: new NodeBlob([sliceBytes(idx)]) as unknown as Blob });
  }
  stop() {
    if (this.state === 'inactive') return;
    this.state = 'inactive';
    const finish = () => {
      this.emit(); // the spec: a final dataavailable, then stop
      this.onstop?.();
    };
    if (FakeRecorder.asyncStop) this.owed = finish;
    else finish();
  }
  /** The stop event a browser fires a few milliseconds after stop(). */
  fireStopEvent() {
    const owed = this.owed;
    this.owed = null;
    owed?.();
  }
}

// Real async work (WebCrypto) is waited for by the wall clock, up to 20 s
// per wait; under the full suite's parallel load that can pass vitest's 5 s
// default, and a test cut off there keeps running into the next one.
vi.setConfig({ testTimeout: 60_000 });

let tracks: Array<{ stop: ReturnType<typeof vi.fn> }> = [];
let server: FakeSessionServer;

const turn = () => new Promise<void>((resolve) => setImmediate(resolve));

/** Real async work (WebCrypto's SHA-256) lands on real turns; bounded by the wall clock. */
async function until(cond: () => boolean, what: string, budgetMs = 20_000): Promise<void> {
  const deadline = performance.now() + budgetMs;
  while (performance.now() < deadline) {
    if (cond()) return;
    await act(async () => {
      await turn();
    });
  }
  if (!cond()) throw new Error(`never happened: ${what}`);
}

/** Like `until`, letting the fake clock run too (the finish long-poll waits between answers). */
async function untilWithClock(cond: () => boolean, what: string, budgetMs = 20_000): Promise<void> {
  const deadline = performance.now() + budgetMs;
  while (performance.now() < deadline) {
    if (cond()) return;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    for (let t = 0; t < 20; t += 1) await act(async () => turn());
  }
  if (!cond()) throw new Error(`never happened: ${what}`);
}

function useServer(options: FakeOptions = {}, live: Record<string, unknown> | null = LIVE) {
  server = new FakeSessionServer({ ...options, config: { ...(options.config ?? {}), live } });
  vi.stubGlobal('fetch', server.fetch);
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
  resetLiveFakes();
  FakeRecorder.asyncStop = false;
  FakeRecorder.last = null;
  tracks = [];
  window.localStorage.clear();
  Object.defineProperty(navigator, 'mediaDevices', {
    configurable: true,
    writable: true,
    value: {
      getUserMedia: vi.fn(async () => {
        const track = { stop: vi.fn(), addEventListener: vi.fn() };
        tracks.push(track);
        return { getTracks: () => [track], getAudioTracks: () => [track] } as unknown as MediaStream;
      }),
    },
  });
  vi.stubGlobal('MediaRecorder', FakeRecorder);
  vi.stubGlobal('AudioContext', FakeLiveAudioContext);
  vi.stubGlobal('AudioWorkletNode', FakeWorkletNode);
  vi.stubGlobal('WebSocket', FakeWebSocket);
  vi.stubGlobal('requestAnimationFrame', vi.fn(() => 1));
  vi.stubGlobal('cancelAnimationFrame', vi.fn());
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
  useServer();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  Reflect.deleteProperty(navigator, 'mediaDevices');
});

/**
 * A signed-in account whose outbox is IndexedDB (fake-indexeddb, fresh), so a
 * record outlives the hook the way it outlives a reload. Without one the
 * outbox is the tab's memory, which a reload empties.
 */
function signedIn(owner = 'u1') {
  vi.stubGlobal('indexedDB', new IDBFactory());
  vi.stubGlobal('IDBKeyRange', IDBKeyRange);
  return vi.fn(async () => owner);
}

async function startRecording(resolveOwner?: () => Promise<string | null>) {
  const onTranscript = vi.fn();
  const view = renderHook(() =>
    useVoiceRecorder({ onTranscript, maxMs: 10 * 60 * 1000, ...(resolveOwner ? { resolveOwner } : {}) }),
  );
  await act(async () => {
    view.result.current.start();
  });
  await until(() => view.result.current.state === 'recording', 'recording started');
  return { view, onTranscript, rec: FakeRecorder.last! };
}

/** Every record in account u1's outbox, as a reload would find them. */
async function outboxRecords(): Promise<Array<Record<string, unknown>>> {
  return new Promise((resolve, reject) => {
    const req = indexedDB.open('techsara-voice-outbox:u1');
    req.onerror = () => reject(req.error);
    req.onsuccess = () => {
      const db = req.result;
      const all = db.transaction('records', 'readonly').objectStore('records').getAll();
      all.onerror = () => reject(all.error);
      all.onsuccess = () => {
        db.close();
        resolve(all.result as Array<Record<string, unknown>>);
      };
    };
  });
}

/** Wait, on real turns, until the outbox satisfies `cond` (its writes are real async work). */
async function untilOutbox(
  cond: (records: Array<Record<string, unknown>>) => boolean,
  what: string,
): Promise<Array<Record<string, unknown>>> {
  const deadline = performance.now() + 20_000;
  let records: Array<Record<string, unknown>> = [];
  while (performance.now() < deadline) {
    records = await outboxRecords();
    if (cond(records)) return records;
    await act(async () => {
      for (let t = 0; t < 10; t += 1) await turn();
    });
  }
  throw new Error(`never happened: ${what} (outbox: ${JSON.stringify(records.map((r) => ({ ...r, init: undefined })))})`);
}

type Ctx = Awaited<ReturnType<typeof startRecording>>;

/** One 5 s timeslice, uploaded before the next. */
async function talk(ctx: Ctx, slices = 1) {
  for (let i = 0; i < slices; i += 1) {
    const idx = ctx.rec.next;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
      ctx.rec.emit();
    });
    await until(() => server.appendedSlices.length >= idx + 1, `slice ${idx} on the server`);
  }
}

/** The worklet's first frames, then the socket's handshake (`ready` adds to its ready, e.g. a language). */
async function goLive(frames = 25, ready: Record<string, unknown> = {}): Promise<FakeWebSocket> {
  const node = FakeWorkletNode.last;
  await act(async () => {
    node.started(48000);
    node.frames(0, frames);
  });
  const ws = FakeWebSocket.last;
  await act(async () => {
    ws.handshake(ready);
  });
  return ws;
}

/** Past the redraw throttle (100 ms), so what the socket said is on screen. */
async function redraw() {
  await act(async () => {
    await vi.advanceTimersByTimeAsync(120);
  });
}

describe('the audio graph of a recording with a live transcript', () => {
  it('is built at the grant, before the session answers, on ONE context whose ONE source feeds meter and tap', async () => {
    let letCreateThrough: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => (letCreateThrough = resolve));
    const serverFetch = server.fetch;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string, init?: RequestInit) => {
        if (String(input) === '/api/audio/sessions' && init?.method === 'POST') await gate;
        return serverFetch(input, init);
      }),
    );
    const onTranscript = vi.fn();
    const view = renderHook(() => useVoiceRecorder({ onTranscript }));
    await act(async () => {
      view.result.current.start();
    });
    await until(() => FakeWorkletNode.instances.length === 1, 'the worklet on the context');
    // The session has not answered, and the tap is already listening.
    expect(view.result.current.state).toBe('requesting');
    expect(FakeLiveAudioContext.instances).toHaveLength(1);
    const context = FakeLiveAudioContext.last;
    expect(context.sources).toHaveLength(1);
    expect(context.sources[0]!.connections).toEqual([context.analysers[0], FakeWorkletNode.last]);
    // The opening words, spoken while the session is still being created.
    await act(async () => {
      FakeWorkletNode.last.started(48000);
      FakeWorkletNode.last.frames(0, 10);
    });
    expect(FakeWebSocket.instances).toHaveLength(0);
    letCreateThrough();
    await until(() => view.result.current.state === 'recording', 'recording started');
    const ws = FakeWebSocket.last;
    expect(ws.url).toBe(`ws://localhost:3000/api/audio/sessions/${SESSION_ID}/live`);
    const start = ws.handshake();
    expect(start.resume_from_sample).toBe(0);
    expect(start.language).toBe('auto');
    // The tap started before the recorder: sample 0 is before the recording's zero.
    expect(start.clock_offset_ms as number).toBeLessThanOrEqual(0);
    expect(start.clock_offset_ms as number).toBeGreaterThanOrEqual(-60_000);
    // Replayed from sample 0: nothing of the opening words is lost.
    expect(ws.pcm).toHaveLength(6400);
    expect(ws.pcm.slice(0, 3)).toEqual([0, 1, 2]);
    // Still exactly one context and one microphone.
    expect(FakeLiveAudioContext.instances).toHaveLength(1);
    expect(tracks).toHaveLength(1);
  });

  it('wakes a context that starts suspended, so neither the meter nor the tap hears silence', async () => {
    FakeLiveAudioContext.startSuspended = true;
    await startRecording();
    expect(FakeLiveAudioContext.last.resumed).toBe(1);
    expect(FakeLiveAudioContext.last.state).toBe('running');
  });

  it('asks the engine for the language this browser chose', async () => {
    window.localStorage.setItem('techsara-voice-language', 'hi');
    await startRecording();
    const ws = await goLive(2);
    expect(ws.texts[0]!.language).toBe('hi');
  });
});

describe('the live words in the bar', () => {
  it('shows committed words in ink and the partial muted, in a box anchored to its newest line', async () => {
    render(
      <Composer streaming={false} prefs={DEFAULT_PREFS} onPrefsChange={vi.fn()} onSend={vi.fn()} onStop={vi.fn()} />,
    );
    await act(async () => {
      fireEvent.click(screen.getByLabelText('Start voice input'));
    });
    await until(() => FakeRecorder.last?.state === 'recording', 'recording started');
    const ws = await goLive(25);
    await act(async () => {
      ws.say({ type: 'final', u: 0, text: 'Hello there.', start_sample: 0, end_sample: 8000 });
      ws.say({ type: 'partial', u: 1, text: 'how are', start_sample: 160_000, end_sample: 176_000 });
    });
    await redraw();
    const committed = screen.getByText('Hello there.');
    const partial = screen.getByText('how are');
    expect(committed.className).not.toContain('text-muted');
    expect(committed.closest('p')!.className).toContain('text-ink');
    expect(partial.className).toContain('text-muted');
    const box = screen.getByTestId('voice-transcript');
    expect(box.className.split(' ')).toEqual(
      expect.arrayContaining(['flex', 'flex-col', 'justify-end', 'overflow-hidden', 'max-h-20']),
    );
    // Not a live region, and not inside the one the bar announces through.
    expect(box.closest('[aria-live]')).toBeNull();
    expect(box.closest('[role="status"]')).toBeNull();

    // The stored recording's first words arrive: whisper covers 0-5 s now, so
    // the live utterance inside it gives way, and the one after it stays.
    const rec = FakeRecorder.last!;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
      rec.emit();
    });
    await until(() => server.appendedSlices.length >= 1, 'slice 0 on the server');
    await until(() => screen.queryByText('Hello there.') === null, 'the covered live words gone');
    expect(screen.getByText('w0')).toBeTruthy();
    expect(screen.getByText('how are')).toBeTruthy();
  });

  it('redraws at most ten times a second, however fast partials come', async () => {
    const onTranscript = vi.fn();
    const seen: string[] = [];
    const view = renderHook(() => {
      const r = useVoiceRecorder({ onTranscript });
      const p = r.progress?.live?.partial;
      if (p && seen[seen.length - 1] !== p) seen.push(p);
      return r;
    });
    await act(async () => {
      view.result.current.start();
    });
    await until(() => view.result.current.state === 'recording', 'recording started');
    const ws = await goLive(1);
    for (let k = 1; k <= 100; k += 1) {
      await act(async () => {
        ws.say({ type: 'partial', u: 0, text: `word ${k}`, start_sample: 0, end_sample: 640 });
        await vi.advanceTimersByTimeAsync(10);
      });
    }
    await redraw();
    expect(seen.length).toBeGreaterThanOrEqual(9);
    expect(seen.length).toBeLessThanOrEqual(11);
    expect(seen[seen.length - 1]).toBe('word 100');
  });
});

describe('Stop', () => {
  it('releases the microphone at once, gives the socket its last frame and flush, and only then closes the context', async () => {
    const ctx = await startRecording();
    const ws = await goLive(25);
    await talk(ctx, 1);
    log.length = 0;
    await act(async () => {
      ctx.view.result.current.stop();
    });
    // The hardware promise first: the microphone is off now.
    expect(tracks[0]!.stop).toHaveBeenCalled();
    expect(log).toEqual(['worklet:flush']);
    expect(FakeLiveAudioContext.last.state).toBe('running');
    await act(async () => {
      FakeWorkletNode.last.frame(16_000, 320); // the partial last frame
      FakeWorkletNode.last.flushed(16_320);
      await vi.advanceTimersByTimeAsync(0);
    });
    expect(log).toEqual(['worklet:flush', 'worklet:stop', 'ws:flush', 'context:close']);
    expect(ws.pcm).toHaveLength(16_320);
    await act(async () => {
      ws.say({ type: 'final', u: 0, text: 'Hello there.', start_sample: 0, end_sample: 16_000 });
      ws.say({ type: 'done' });
    });
    expect(ws.closedWith).toBe(1000);
    // The stored recording's own transcript is the one that goes in.
    await untilWithClock(() => ctx.view.result.current.state === 'idle', 'the recording finished');
    expect(ctx.onTranscript).toHaveBeenCalledTimes(1);
    expect(ctx.onTranscript.mock.calls[0]![0]).toBe('w0 w1');
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it('does not hold the stored recording’s finish for the live stream', async () => {
    const ctx = await startRecording();
    const ws = await goLive(5);
    await talk(ctx, 1);
    await act(async () => {
      ctx.view.result.current.stop();
    });
    // The worklet never answers and the server never says done: the finish
    // request still goes out at once.
    await until(() => server.finishBody !== null, 'the finish request');
    await untilWithClock(() => ctx.view.result.current.state === 'idle', 'the recording finished');
    expect(ctx.onTranscript).toHaveBeenCalledWith('w0 w1', null);
    expect(ws.texts.map((t) => t.type)).toContain('flush');
  });
});

describe('cancel and leaving the page', () => {
  it('closes the socket at once with 1000 on cancel, and drops the words', async () => {
    const ctx = await startRecording();
    const ws = await goLive(5);
    await act(async () => {
      ws.say({ type: 'partial', u: 0, text: 'never mind', start_sample: 0, end_sample: 3200 });
    });
    await act(async () => {
      ctx.view.result.current.cancel();
    });
    expect(ws.closedWith).toBe(1000);
    expect(FakeWorkletNode.last.port.posted.map((m) => m.type)).toContain('stop');
    expect(FakeLiveAudioContext.last.state).toBe('closed');
    expect(ctx.view.result.current.state).toBe('idle');
    expect(ctx.view.result.current.progress?.live ?? null).toBeNull();
    await until(() => server.deleted, 'the stored recording deleted');
    expect(ctx.onTranscript).not.toHaveBeenCalled();
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it('closes the socket when the page goes away', async () => {
    await startRecording();
    const ws = await goLive(2);
    await act(async () => {
      window.dispatchEvent(new Event('pagehide'));
    });
    expect(ws.closedWith).toBe(1000);
  });

  it('reconnects at once when the browser comes back online', async () => {
    await startRecording();
    const ws = await goLive(2);
    await act(async () => {
      ws.hangUp(1006);
      await vi.advanceTimersByTimeAsync(100);
    });
    expect(FakeWebSocket.instances).toHaveLength(1);
    await act(async () => {
      window.dispatchEvent(new Event('online'));
    });
    expect(FakeWebSocket.instances).toHaveLength(2);
  });
});

describe('a live path that fails leaves the stored recording exactly as it was', () => {
  async function recordAndStop() {
    const ctx = await startRecording();
    await talk(ctx, 2);
    await act(async () => {
      ctx.view.result.current.stop();
    });
    await untilWithClock(() => ctx.view.result.current.state === 'idle', 'the recording finished');
    expect(ctx.view.result.current.error).toBeNull();
    expect(ctx.view.result.current.followUp).toBeNull();
    expect(ctx.onTranscript).toHaveBeenCalledWith('w0 w1 w2', null);
    expect(server.appendedSlices).toEqual([0, 1, 2]);
    return ctx;
  }

  it('when the browser refuses to open the socket', async () => {
    FakeWebSocket.refuse = true;
    const ctx = await startRecording();
    await act(async () => {
      FakeWorkletNode.last.frames(0, 5);
      await vi.advanceTimersByTimeAsync(20_000);
    });
    expect(FakeWebSocket.instances).toHaveLength(0);
    await talk(ctx, 2);
    await act(async () => {
      ctx.view.result.current.stop();
    });
    await untilWithClock(() => ctx.view.result.current.state === 'idle', 'the recording finished');
    expect(ctx.onTranscript).toHaveBeenCalledWith('w0 w1 w2', null);
  });

  it('when the server refuses the stream for good', async () => {
    const ctx = await startRecording();
    const ws = await goLive(5);
    await act(async () => {
      ws.say({ type: 'final', u: 0, text: 'Heard once.', start_sample: 0, end_sample: 3200 });
      ws.say({ type: 'error', code: 'live_unavailable', message: 'x', retryable: false });
      ws.hangUp(4404);
    });
    await redraw();
    // Refused for good: the bar is back to the stored recording's words only.
    expect(ctx.view.result.current.progress?.live ?? null).toBeNull();
    await talk(ctx, 2);
    await act(async () => {
      ctx.view.result.current.stop();
    });
    await untilWithClock(() => ctx.view.result.current.state === 'idle', 'the recording finished');
    expect(ctx.onTranscript).toHaveBeenCalledWith('w0 w1 w2', null);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it('when the worklet module does not load', async () => {
    FakeLiveAudioContext.addModule = async () => {
      throw new DOMException('refused by policy', 'AbortError');
    };
    await recordAndStop();
    expect(FakeWorkletNode.instances).toHaveLength(0);
    expect(FakeWebSocket.instances).toHaveLength(0);
  });

  it('in a browser without AudioWorklet, where nothing live is even attempted', async () => {
    FakeLiveAudioContext.withWorklet = false;
    await recordAndStop();
    expect(FakeLiveAudioContext.instances).toHaveLength(1);
    expect(FakeWebSocket.instances).toHaveLength(0);
  });

  it('on a server whose sessions have no live path, the tap is taken out again', async () => {
    useServer({}, null);
    await recordAndStop();
    expect(FakeWorkletNode.last.port.posted.map((m) => m.type)).toEqual(['stop']);
    expect(FakeWorkletNode.last.disconnected).toBe(true);
    expect(FakeWebSocket.instances).toHaveLength(0);
  });

  it('on the ten-minute legacy road, which never goes live', async () => {
    useServer();
    server.fetch.mockImplementationOnce(async () => ({
      ok: false,
      status: 404,
      headers: { get: () => null },
      text: async () => JSON.stringify({ detail: 'off', reason: 'sessions_off' }),
    }) as unknown as Response);
    const ctx = await startRecording();
    expect(ctx.view.result.current.mode).toBe('legacy');
    expect(FakeWorkletNode.last.port.posted.map((m) => m.type)).toEqual(['stop']);
    await act(async () => {
      FakeWorkletNode.last.frames(0, 5);
    });
    expect(FakeWebSocket.instances).toHaveLength(0);
  });
});

describe('when the stored transcript cannot be had', () => {
  it('offers the live words, and puts them in only when asked', async () => {
    useServer({ finalState: () => ({ outcome: 'no_words', text: null }) });
    const ctx = await startRecording();
    const ws = await goLive(25);
    await talk(ctx, 1);
    await act(async () => {
      ws.say({ type: 'final', u: 0, text: 'Hello there.', start_sample: 0, end_sample: 8000 });
      ctx.view.result.current.stop();
    });
    await act(async () => {
      FakeWorkletNode.last.flushed(16_000);
      await vi.advanceTimersByTimeAsync(0);
    });
    await act(async () => {
      ws.say({ type: 'final', u: 1, text: 'How are you?', start_sample: 8000, end_sample: 16_000 });
      ws.say({ type: 'done' });
    });
    await untilWithClock(() => ctx.view.result.current.followUp !== null, 'the follow-up');
    const followUp = ctx.view.result.current.followUp!;
    expect(ctx.view.result.current.state).toBe('idle');
    expect(ctx.view.result.current.error).toBeNull();
    expect(followUp.tone).toBe('error');
    expect(followUp.message).toBe(
      'Sound was detected, but no words could be made out. It may have been background noise or music. The recording is saved.',
    );
    expect(followUp.actionLabel).toBe('Insert live transcript');
    expect(ctx.onTranscript).not.toHaveBeenCalled();
    await act(async () => followUp.run());
    expect(ctx.onTranscript).toHaveBeenCalledWith('Hello there. How are you?', null);
    expect(ctx.view.result.current.followUp).toBeNull();
  });

  // The review's probe (2026-09-30): the stream was refused a few seconds in,
  // thirty more seconds were recorded, and "First minute only." was offered as
  // "Insert live transcript" beside "no words could be made out".
  it('says the live words are only part of the recording when the stream was refused mid-way', async () => {
    useServer({ finalState: () => ({ outcome: 'no_words', text: null }) });
    const ctx = await startRecording();
    const ws = await goLive(25);
    await act(async () => {
      ws.say({ type: 'final', u: 0, text: 'First minute only.', start_sample: 0, end_sample: 16_000 });
      ws.say({ type: 'error', code: 'session_closed', message: 'x', retryable: false });
      ws.hangUp(4404);
    });
    await talk(ctx, 6); // 30 s more that the live path never heard
    await act(async () => {
      ctx.view.result.current.stop();
    });
    await act(async () => {
      FakeWorkletNode.last.flushed(16_000);
      await vi.advanceTimersByTimeAsync(0);
    });
    await untilWithClock(() => ctx.view.result.current.followUp !== null, 'the follow-up');
    const followUp = ctx.view.result.current.followUp!;
    expect(followUp.actionLabel).toBe('Insert what was heard live (part of the recording)');
    expect(followUp.message).toBe(
      'Sound was detected, but no words could be made out. It may have been background noise or music. The recording is saved.',
    );
    expect(ctx.onTranscript).not.toHaveBeenCalled();
    await act(async () => followUp.run());
    expect(ctx.onTranscript).toHaveBeenCalledWith('First minute only.', null);
  });

  it('says so beside Retry too, when a reconnect had to skip audio no final covered', async () => {
    useServer({ finalState: () => ({ outcome: 'engine_unavailable', text: null }) });
    const ctx = await startRecording();
    const ws = await goLive(25);
    await act(async () => {
      ws.say({ type: 'final', u: 0, text: 'Before the gap.', start_sample: 0, end_sample: 8_000 });
      ws.hangUp(1006);
      // Eighty seconds of the tap while the connection is down: more than the
      // minute a reconnect replays. (The hook's backoff is jittered: 0.4-0.6 s.)
      FakeWorkletNode.last.frames(16_000, 2000);
      await vi.advanceTimersByTimeAsync(600);
    });
    const second = FakeWebSocket.last;
    expect(second).not.toBe(ws);
    await act(async () => {
      expect(second.handshake().resume_from_sample).toBe(16_000 + 2000 * 640 - 60 * 16_000);
      second.say({ type: 'final', u: 1, text: 'After the gap.', start_sample: 400_000, end_sample: 420_000 });
    });
    await talk(ctx, 1);
    await act(async () => {
      ctx.view.result.current.stop();
    });
    await act(async () => {
      FakeWorkletNode.last.flushed(16_000 + 2000 * 640);
      await vi.advanceTimersByTimeAsync(0);
      second.say({ type: 'done' });
    });
    await untilWithClock(() => ctx.view.result.current.followUp !== null, 'the Retry offer');
    const followUp = ctx.view.result.current.followUp!;
    expect(followUp.actionLabel).toBe('Retry');
    expect(followUp.secondaryLabel).toBe('Insert what was heard live (part of the recording)');
  });

  it('says what it always said when there are no live words to offer', async () => {
    useServer({ finalState: () => ({ outcome: 'no_words', text: null }) });
    const ctx = await startRecording();
    await talk(ctx, 1);
    await act(async () => {
      ctx.view.result.current.stop();
    });
    await untilWithClock(() => ctx.view.result.current.state !== 'finishing', 'the recording finished');
    expect(ctx.view.result.current.state).toBe('error');
    expect(ctx.view.result.current.followUp).toBeNull();
  });

  it('after an engine outage keeps Retry first, and Retry then REPLACES the live words it inserted', async () => {
    useServer({
      finalState: (words, s) => (s.retranscribes === 0 ? { outcome: 'engine_unavailable', text: null } : {}),
    });
    render(
      <Composer streaming={false} prefs={DEFAULT_PREFS} onPrefsChange={vi.fn()} onSend={vi.fn()} onStop={vi.fn()} />,
    );
    const box = screen.getByLabelText('Message') as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: 'Draft:' } });
    await act(async () => {
      fireEvent.click(screen.getByLabelText('Start voice input'));
    });
    await until(() => FakeRecorder.last?.state === 'recording', 'recording started');
    const rec = FakeRecorder.last!;
    const ws = await goLive(25);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
      rec.emit();
    });
    await until(() => server.appendedSlices.length >= 1, 'slice 0 on the server');
    await act(async () => {
      ws.say({ type: 'final', u: 0, text: 'Live words here.', start_sample: 0, end_sample: 16_000 });
      fireEvent.click(screen.getByLabelText('Stop recording and transcribe'));
    });
    await act(async () => {
      FakeWorkletNode.last.flushed(16_000);
      await vi.advanceTimersByTimeAsync(0);
      ws.say({ type: 'done' });
    });
    await untilWithClock(() => screen.queryByText('Retry') !== null, 'the Retry offer');
    expect(screen.getByText('Insert live transcript')).toBeTruthy();
    expect(box.value).toBe('Draft:');

    await act(async () => {
      fireEvent.click(screen.getByText('Insert live transcript'));
    });
    expect(box.value).toBe('Draft: Live words here.');
    // Retry is still there, and now replaces those words when it succeeds.
    expect(screen.queryByText('Insert live transcript')).toBeNull();
    await act(async () => {
      fireEvent.click(screen.getByText('Retry'));
    });
    await untilWithClock(() => box.value === 'Draft: w0 w1', 'the live words replaced in place');
    expect(server.retranscribes).toBe(1);
  });
});

describe('a second Stop before the stop event (the audit’s stop() race)', () => {
  it('still finishes the recording and delivers its words once, on the session road', async () => {
    FakeRecorder.asyncStop = true;
    const ctx = await startRecording();
    await talk(ctx, 2);
    await act(async () => {
      ctx.view.result.current.stop();
      // Double-click: the recorder is inactive, its stop event not yet fired.
      ctx.view.result.current.stop();
    });
    await act(async () => {
      ctx.rec.fireStopEvent();
    });
    await untilWithClock(() => ctx.onTranscript.mock.calls.length > 0, 'the words delivered');
    expect(ctx.onTranscript).toHaveBeenCalledTimes(1);
    expect(ctx.onTranscript).toHaveBeenCalledWith('w0 w1 w2', null);
    expect(server.finishBody).toMatchObject({ last_part: 2 });
    expect(ctx.view.result.current.state).toBe('idle');
    for (const t of tracks) expect(t.stop).toHaveBeenCalled();
  });

  it('still transcribes the recording on the legacy road', async () => {
    FakeRecorder.asyncStop = true;
    useServer();
    const transcribed = vi.fn();
    const serverFetch = server.fetch;
    let sessionsRefused = false;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string, init?: RequestInit) => {
        if (String(input) === '/api/audio/sessions' && !sessionsRefused) {
          sessionsRefused = true;
          return {
            ok: false,
            status: 404,
            headers: { get: () => null },
            text: async () => JSON.stringify({ detail: 'off', reason: 'sessions_off' }),
          } as unknown as Response;
        }
        if (String(input).startsWith('/api/audio/transcribe')) {
          transcribed(input);
          return {
            ok: true,
            status: 200,
            headers: { get: () => null },
            json: async () => ({ text: 'the legacy words', language: 'en', duration_ms: 3000, processing_ms: 90 }),
            text: async () => JSON.stringify({ text: 'the legacy words', language: 'en', duration_ms: 3000 }),
          } as unknown as Response;
        }
        return serverFetch(input, init);
      }),
    );
    const ctx = await startRecording();
    expect(ctx.view.result.current.mode).toBe('legacy');
    await act(async () => {
      await vi.advanceTimersByTimeAsync(3000);
      ctx.rec.emit();
    });
    await act(async () => {
      ctx.view.result.current.stop();
      ctx.view.result.current.stop();
    });
    await act(async () => {
      ctx.rec.fireStopEvent();
    });
    await untilWithClock(() => ctx.onTranscript.mock.calls.length > 0, 'the words delivered');
    expect(transcribed).toHaveBeenCalledTimes(1);
    expect(ctx.onTranscript).toHaveBeenCalledWith('the legacy words', null);
  });

  it('finishes it anyway if the browser never fires the stop event', async () => {
    FakeRecorder.asyncStop = true;
    const ctx = await startRecording();
    await talk(ctx, 1);
    await act(async () => {
      ctx.view.result.current.stop();
      ctx.view.result.current.stop();
    });
    // No stop event, ever: two seconds later the recording is finished with
    // what the server already has.
    await untilWithClock(() => ctx.onTranscript.mock.calls.length > 0, 'the words delivered');
    expect(ctx.onTranscript).toHaveBeenCalledWith('w0', null);
  });
});

// ---------------------------------------------------------------------------
// Which transcript goes in at Stop (build spec section 10, 2026-09-30)
// ---------------------------------------------------------------------------

/** Nemotron's kind of Hinglish: Devanagari with the English words in Latin. */
const HINGLISH = 'मैं आज office जा रहा हूँ, meeting दस बजे है।';
/** What whisper-large-v3 made of such speech in a fifth of the lecture segments: Urdu script. */
const WHISPER_URDU = 'میں آج آفس جا رہا ہوں، میٹنگ دس بجے ہے۔';

/** Stop, the worklet's last frame, and the live stream's `done`: the end on both paths. */
async function stopWithLiveDone(ctx: Ctx, ws: FakeWebSocket) {
  await act(async () => {
    ctx.view.result.current.stop();
  });
  await act(async () => {
    FakeWorkletNode.last.flushed(16_000);
    await vi.advanceTimersByTimeAsync(0);
  });
  await act(async () => {
    ws.say({ type: 'done' });
  });
  await untilWithClock(() => ctx.onTranscript.mock.calls.length > 0, 'the words delivered');
}

/**
 * The full pass done and offered on the line. A Hindi or Hinglish session's
 * live words go in when the live stream ends, before the full pass (spec 13,
 * 2026-09-30); "Use the other one" comes once the full pass is done.
 */
async function fullPassOffered(ctx: Ctx) {
  await untilWithClock(
    () => ctx.view.result.current.followUp?.actionLabel === 'Use the other one',
    'the full pass done and offered',
  );
}

/** Record one slice with these live words, and stop. */
async function dictate(
  liveWords: string,
  options: { language?: string; resolveOwner?: () => Promise<string | null> } = {},
) {
  if (options.language) window.localStorage.setItem('techsara-voice-language', options.language);
  const ctx = await startRecording(options.resolveOwner);
  const ws = await goLive(25);
  await talk(ctx, 1);
  await act(async () => {
    ws.say({ type: 'final', u: 0, text: liveWords, start_sample: 0, end_sample: 16_000 });
  });
  await stopWithLiveDone(ctx, ws);
  return { ...ctx, ws };
}

describe('which transcript goes in at Stop', () => {
  it('is the full pass for an English session, said in one quiet line that offers the live one', async () => {
    const ctx = await dictate('Hello there, this came in live.');
    expect(ctx.onTranscript).toHaveBeenCalledTimes(1);
    expect(ctx.onTranscript).toHaveBeenCalledWith('w0 w1', null);
    const line = ctx.view.result.current.followUp!;
    expect(line).toMatchObject({
      message: 'Inserted the full-pass transcript.',
      tone: 'info',
      actionLabel: 'Use the other one',
      busy: false,
    });
    await act(async () => line.run());
    // The live one, over the span the full pass went into, and only if untouched.
    expect(ctx.onTranscript).toHaveBeenLastCalledWith('Hello there, this came in live.', null, 'w0 w1', true);
    expect(ctx.view.result.current.followUp).toMatchObject({
      message: 'Inserted the live transcript.',
      actionLabel: 'Use the other one',
    });
    await act(async () => ctx.view.result.current.followUp!.run());
    expect(ctx.onTranscript).toHaveBeenLastCalledWith('w0 w1', null, 'Hello there, this came in live.', true);
  });

  it('is the live one for a Hinglish session: at least a fifth of its letters Devanagari', async () => {
    const ctx = await dictate(HINGLISH);
    expect(ctx.onTranscript).toHaveBeenCalledTimes(1);
    expect(ctx.onTranscript).toHaveBeenCalledWith(HINGLISH, null);
    // Since spec 13 the words go in before the full pass, which is offered once done.
    await fullPassOffered(ctx);
    expect(ctx.onTranscript).toHaveBeenCalledTimes(1);
    expect(ctx.view.result.current.followUp).toMatchObject({
      message: 'Inserted the live transcript.',
      actionLabel: 'Use the other one',
    });
  });

  it('is the live one when the person chose Hindi, whatever script the live words came in', async () => {
    const ctx = await dictate('aaj hum office jayenge', { language: 'hi' });
    expect(ctx.ws.texts[0]!.language).toBe('hi');
    expect(ctx.onTranscript).toHaveBeenCalledWith('aaj hum office jayenge', null);
  });

  it('is the live one, not whisper’s Urdu script, when whisper heard Urdu in Hinglish', async () => {
    useServer({ finalState: () => ({ text: WHISPER_URDU, language: 'Urdu', language_code: 'ur' }) });
    // Mostly Latin: under a fifth Devanagari, so whisper's language decides.
    const live = 'Main aaj office ja raha hoon, meeting दस baje hai.';
    const ctx = await dictate(live);
    expect(ctx.onTranscript).toHaveBeenCalledTimes(1);
    expect(ctx.onTranscript).toHaveBeenCalledWith(live, null);
    const line = ctx.view.result.current.followUp!;
    expect(line.message).toBe('Inserted the live transcript.');
    await act(async () => line.run());
    expect(ctx.onTranscript).toHaveBeenLastCalledWith(WHISPER_URDU, null, live, true);
  });

  it('falls back to the full pass when the Hindi live transcript missed part of the recording', async () => {
    window.localStorage.setItem('techsara-voice-language', 'hi');
    const ctx = await startRecording();
    const ws = await goLive(25);
    await act(async () => {
      ws.say({ type: 'final', u: 0, text: 'आज की बैठक', start_sample: 0, end_sample: 16_000 });
      ws.say({ type: 'error', code: 'session_closed', message: 'x', retryable: false });
      ws.hangUp(4404);
    });
    // Refused for good: there is nothing left for a language control to change.
    expect(ctx.view.result.current.language).toBeNull();
    await talk(ctx, 2);
    await act(async () => {
      ctx.view.result.current.stop();
    });
    await act(async () => {
      FakeWorkletNode.last.flushed(16_000);
      await vi.advanceTimersByTimeAsync(0);
    });
    await untilWithClock(() => ctx.onTranscript.mock.calls.length > 0, 'the words delivered');
    expect(ctx.onTranscript).toHaveBeenCalledWith('w0 w1 w2', null);
    expect(ctx.view.result.current.followUp).toMatchObject({
      message: 'Inserted the full-pass transcript.',
      actionLabel: 'Use what was heard live (part of the recording)',
    });
  });

  // The review's high finding (2026-09-30): this line used to carry the full
  // pass's Retry over the live words, and in Chromium Retry put whisper's Urdu
  // script where the complete Hindi transcript had been, with no way back.
  // Spec 12: the live words have no gap; the Retry comes with the full pass.
  const GAPPY = (_words: string[], s: FakeSessionServer) =>
    s.retranscribes === 0
      ? {
          outcome: 'transcribed_with_gaps',
          gaps: [{ start_ms: 5000, end_ms: 10_000, reason: 'engine_unavailable' }],
          language: 'Hindi',
          language_code: 'hi',
        }
      : { language: 'Hindi', language_code: 'hi' };

  it('offers no Retry over the live words it put in; the full pass brings its Retry with it, and takes it away again', async () => {
    useServer({ finalState: GAPPY });
    const ctx = await dictate(HINGLISH);
    expect(ctx.onTranscript).toHaveBeenCalledWith(HINGLISH, null);
    await fullPassOffered(ctx); // spec 13: the live words went in first
    const line = ctx.view.result.current.followUp!;
    expect(line).toMatchObject({ message: 'Inserted the live transcript.', tone: 'info', actionLabel: 'Use the other one' });
    expect(line.secondaryLabel ?? null).toBeNull();

    // The full pass goes in with the Retry for its gap.
    await act(async () => line.run());
    expect(ctx.onTranscript).toHaveBeenLastCalledWith('w0 w1', null, HINGLISH, true);
    const fullPass = ctx.view.result.current.followUp!;
    expect(fullPass.message).toBe(
      "Inserted the full-pass transcript. The speech engine couldn't transcribe 1 part (0:05–0:10). The audio is saved. Press Retry to transcribe them.",
    );
    expect(fullPass.actionLabel).toBe('Retry');
    expect(fullPass.secondaryLabel).toBe('Use the other one');

    // Back to the live words: the Retry goes with the full pass.
    await act(async () => fullPass.runSecondary!());
    expect(ctx.onTranscript).toHaveBeenLastCalledWith(HINGLISH, null, 'w0 w1', true);
    const liveAgain = ctx.view.result.current.followUp!;
    expect(liveAgain).toMatchObject({ message: 'Inserted the live transcript.', actionLabel: 'Use the other one' });
    expect(liveAgain.secondaryLabel ?? null).toBeNull();

    // Pressed with the full pass in, Retry replaces the full pass's words.
    await act(async () => liveAgain.run());
    await act(async () => ctx.view.result.current.followUp!.run());
    await untilWithClock(() => ctx.onTranscript.mock.calls.length === 5, 'the Retry delivered');
    expect(ctx.onTranscript.mock.calls[4]!.slice(0, 3)).toEqual(['w0 w1', null, 'w0 w1']);
    expect(server.retranscribes).toBe(1);
  });

  it('leaves nothing that a reload could offer over the live words', async () => {
    useServer({ finalState: GAPPY });
    const resolveOwner = signedIn();
    const ctx = await dictate(HINGLISH, { resolveOwner });
    expect(ctx.onTranscript).toHaveBeenCalledWith(HINGLISH, null);
    // Settled once the full pass is over (spec 13: the live words went in first).
    await fullPassOffered(ctx);
    // The record goes: before 2026-09-30 it kept the Retry, set to replace the live words.
    await untilOutbox((records) => records.length === 0, 'the record settled');
    ctx.view.unmount(); // reload
    await act(async () => {
      await vi.advanceTimersByTimeAsync(40_000);
    });
    const again = renderHook(() => useVoiceRecorder({ onTranscript: vi.fn(), resolveOwner }));
    await act(async () => {
      await vi.advanceTimersByTimeAsync(OUTBOX_STALE_MS + 2000); // the look at mount, and the second one
    });
    await until(() => resolveOwner.mock.calls.length >= 3, 'the reopened page looked for recordings');
    for (let t = 0; t < 50; t += 1) await act(async () => turn());
    expect(again.result.current.followUp).toBeNull();
    expect(server.retranscribes).toBe(0);
  });

  it('offers no "Upload the rest" over the live words either, and keeps the audio held on this device', async () => {
    const resolveOwner = signedIn();
    useServer({
      // The server runs out of space at the second part: the rest stays on this device.
      inject: ({ method, seq }) =>
        method === 'PUT' && seq === 1 ? { status: 507, body: { detail: 'full', reason: 'storage_full' } } : undefined,
      finalState: () => ({ language: 'Hindi', language_code: 'hi' }),
    });
    const ctx = await startRecording(resolveOwner);
    const ws = await goLive(25);
    await act(async () => {
      ws.say({ type: 'final', u: 0, text: HINGLISH, start_sample: 0, end_sample: 16_000 });
    });
    await talk(ctx, 1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
      ctx.rec.emit(); // refused: the recording stops here
    });
    await until(() => ctx.rec.state === 'inactive', 'the recorder stopped by the refusal');
    await act(async () => {
      FakeWorkletNode.last.flushed(16_000);
      await vi.advanceTimersByTimeAsync(0);
    });
    await act(async () => {
      ws.say({ type: 'done' });
    });
    await untilWithClock(() => ctx.onTranscript.mock.calls.length > 0, 'the words delivered');
    expect(ctx.onTranscript.mock.calls[0]![0]).toBe(HINGLISH);
    const line = ctx.view.result.current.followUp!;
    expect(line).toMatchObject({ message: 'Inserted the live transcript.', actionLabel: 'Use the other one' });
    expect(line.secondaryLabel ?? null).toBeNull();
    // Held audio is never deleted unasked; its record just names no text in the draft for "Upload the rest" to replace.
    const [held] = await untilOutbox(
      (records) => records.length === 1 && records[0]!.held != null && records[0]!.deliveredText === null,
      'the held record, tied to no text',
    );
    expect(held!.offer ?? null).toBeNull();

    // With the full pass in, "Upload the rest" is offered over its words.
    await act(async () => line.run());
    expect(ctx.onTranscript).toHaveBeenLastCalledWith('w0', null, HINGLISH, true);
    const fullPass = ctx.view.result.current.followUp!;
    expect(fullPass.actionLabel).toBe('Upload the rest');
    expect(fullPass.secondaryLabel).toBe('Use the other one');
    await untilOutbox((records) => records[0]?.deliveredText === 'w0', 'the held record tied to the full pass');
  });

  it('draws no line when there were no live words, as before', async () => {
    const ctx = await startRecording();
    await talk(ctx, 1);
    await act(async () => {
      ctx.view.result.current.stop();
    });
    await untilWithClock(() => ctx.onTranscript.mock.calls.length > 0, 'the words delivered');
    expect(ctx.onTranscript).toHaveBeenCalledWith('w0 w1', null);
    expect(ctx.view.result.current.followUp).toBeNull();
  });
});

describe('the swap in the composer', () => {
  it('puts the other transcript where the first went, back again, and never over words the person changed', async () => {
    render(
      <Composer streaming={false} prefs={DEFAULT_PREFS} onPrefsChange={vi.fn()} onSend={vi.fn()} onStop={vi.fn()} />,
    );
    const box = screen.getByLabelText('Message') as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: 'Draft:' } });
    await act(async () => {
      fireEvent.click(screen.getByLabelText('Start voice input'));
    });
    await until(() => FakeRecorder.last?.state === 'recording', 'recording started');
    const rec = FakeRecorder.last!;
    const ws = await goLive(25);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
      rec.emit();
    });
    await until(() => server.appendedSlices.length >= 1, 'slice 0 on the server');
    await act(async () => {
      ws.say({ type: 'final', u: 0, text: 'Live words here.', start_sample: 0, end_sample: 16_000 });
      fireEvent.click(screen.getByLabelText('Stop recording and transcribe'));
    });
    await act(async () => {
      FakeWorkletNode.last.flushed(16_000);
      await vi.advanceTimersByTimeAsync(0);
      ws.say({ type: 'done' });
    });
    await untilWithClock(() => box.value === 'Draft: w0 w1', 'the full pass in the draft');
    expect(screen.getByText('Inserted the full-pass transcript.')).toBeTruthy();

    // Typing AFTER the dictated words leaves them untouched.
    fireEvent.change(box, { target: { value: 'Draft: w0 w1 and then some' } });
    await act(async () => {
      fireEvent.click(screen.getByText('Use the other one'));
    });
    expect(box.value).toBe('Draft: Live words here. and then some');
    expect(screen.getByText('Inserted the live transcript.')).toBeTruthy();
    await act(async () => {
      fireEvent.click(screen.getByText('Use the other one'));
    });
    expect(box.value).toBe('Draft: w0 w1 and then some');

    // Changed inside: nothing is swapped, and the swap is not offered again.
    fireEvent.change(box, { target: { value: 'Draft: w0 W1 and then some' } });
    await act(async () => {
      fireEvent.click(screen.getByText('Use the other one'));
    });
    expect(box.value).toBe('Draft: w0 W1 and then some');
    expect(screen.getByText('The inserted words have been edited, so they were left as they are.')).toBeTruthy();
    expect(screen.queryByText('Use the other one')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// A switch from English to Hindi mid-recording (spec 12)
// ---------------------------------------------------------------------------

describe('a switch from English to Hindi mid-recording', () => {
  // The review's finding 3: the English-only model's guesses at the Hindi
  // before the switch went into the draft as a complete Hindi transcript.
  it('puts the full pass in, not the English model’s guesses at the opening, and offers the live words', async () => {
    window.localStorage.setItem('techsara-voice-language', 'en');
    const WHISPER_HINDI = 'मैं आज ऑफिस जा रहा हूँ मीटिंग दस बजे है';
    useServer({ finalState: () => ({ text: WHISPER_HINDI, language: 'Hindi', language_code: 'hi' }) });
    render(
      <Composer streaming={false} prefs={DEFAULT_PREFS} onPrefsChange={vi.fn()} onSend={vi.fn()} onStop={vi.fn()} />,
    );
    const box = screen.getByLabelText('Message') as HTMLTextAreaElement;
    await act(async () => {
      fireEvent.click(screen.getByLabelText('Start voice input'));
    });
    await until(() => FakeRecorder.last?.state === 'recording', 'recording started');
    const rec = FakeRecorder.last!;
    const ws = await goLive(25);
    expect(ws.texts[0]!.language).toBe('en');
    // What an English-only model makes of Hindi speech: English-looking words.
    await act(async () => {
      ws.say({ type: 'final', u: 0, text: 'May aaj of his jar a who.', start_sample: 0, end_sample: 8_000 });
    });
    await act(async () => {
      fireEvent.click(screen.getByRole('radio', { name: 'हिन्दी' }));
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(LIVE_LANGUAGE_DEBOUNCE_MS);
    });
    const next = FakeWebSocket.last;
    expect(next).not.toBe(ws);
    await act(async () => {
      expect(next.handshake()).toMatchObject({ language: 'hi', resume_from_sample: 8_000, next_u: 1 });
      next.say({ type: 'final', u: 1, text: 'मीटिंग दस बजे है', start_sample: 8_000, end_sample: 16_000 });
    });
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
      rec.emit();
    });
    await until(() => server.appendedSlices.length >= 1, 'slice 0 on the server');
    await act(async () => {
      fireEvent.click(screen.getByLabelText('Stop recording and transcribe'));
    });
    await act(async () => {
      FakeWorkletNode.last.flushed(16_000);
      await vi.advanceTimersByTimeAsync(0);
      next.say({ type: 'done' });
    });
    await untilWithClock(() => box.value !== '', 'words in the draft');
    expect(box.value).toBe(WHISPER_HINDI);
    expect(screen.getByText('Inserted the full-pass transcript.')).toBeTruthy();
    // The live words, English model's opening and all, are one click away.
    await act(async () => {
      fireEvent.click(screen.getByText('Use the other one'));
    });
    expect(box.value).toBe('May aaj of his jar a who. मीटिंग दस बजे है');
    expect(screen.getByText('Inserted the live transcript.')).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// A stream the server runs on another model than the bar asked for (spec 14.2)
// ---------------------------------------------------------------------------

describe('a stream the server runs on the English model', () => {
  // Release 2's gateway puts "auto" on the English-only model for people
  // whose last five dictations were English, and says so in `ready`. What
  // that model makes of Hindi is not a Hindi transcript, whatever whisper hears.
  it('gets the full pass for an Auto session even when whisper hears Hindi, with the live words one click away', async () => {
    const WHISPER_HINDI = 'मैं आज ऑफिस जा रहा हूँ मीटिंग दस बजे है';
    useServer({ finalState: () => ({ text: WHISPER_HINDI, language: 'Hindi', language_code: 'hi' }) });
    const ctx = await startRecording();
    const ws = await goLive(25, { language: 'en' });
    expect(ws.texts[0]!.language).toBe('auto');
    await talk(ctx, 1);
    await act(async () => {
      ws.say({ type: 'final', u: 0, text: 'May aaj of his jar a who.', start_sample: 0, end_sample: 16_000 });
    });
    await stopWithLiveDone(ctx, ws);
    expect(ctx.onTranscript).toHaveBeenCalledTimes(1);
    expect(ctx.onTranscript).toHaveBeenCalledWith(WHISPER_HINDI, null);
    const line = ctx.view.result.current.followUp!;
    expect(line).toMatchObject({ message: 'Inserted the full-pass transcript.', actionLabel: 'Use the other one' });
    await act(async () => line.run());
    expect(ctx.onTranscript).toHaveBeenLastCalledWith('May aaj of his jar a who.', null, WHISPER_HINDI, true);
  });
});

// ---------------------------------------------------------------------------
// How long the insert waits for the live stream once the full pass is in
// ---------------------------------------------------------------------------

describe('how long the insert waits for the live stream once the full pass is in', () => {
  /**
   * The fake clock in steps of 100 ms, with real turns between them, until
   * `cond`. The waits under test are timers; everything else (hashing the last
   * part, the requests) is real async work that must not move the clock.
   */
  async function inSteps(cond: () => boolean, what: string): Promise<void> {
    const deadline = performance.now() + 20_000;
    while (performance.now() < deadline) {
      if (cond()) return;
      await act(async () => {
        await vi.advanceTimersByTimeAsync(100);
      });
      for (let t = 0; t < 20; t += 1) await act(async () => turn());
    }
    if (!cond()) throw new Error(`never happened: ${what}`);
  }

  /** One slice with these live words, then Stop and the worklet's last frame; the live stream never says done. */
  async function stopWithoutDone(
    words: string,
    ready: Record<string, unknown> = {},
  ): Promise<{ ctx: Ctx; ws: FakeWebSocket }> {
    const ctx = await startRecording();
    const ws = await goLive(25, ready);
    await talk(ctx, 1);
    await act(async () => {
      ws.say({ type: 'final', u: 0, text: words, start_sample: 0, end_sample: 16_000 });
    });
    await act(async () => {
      ctx.view.result.current.stop();
    });
    await act(async () => {
      FakeWorkletNode.last.flushed(16_000);
      await vi.advanceTimersByTimeAsync(0);
    });
    // The last part and the finish go out on real turns alone.
    await until(() => server.finishBody !== null, 'the finish request');
    // The finish long-poll, until the server has the full pass; then its answer lands.
    await inSteps(() => server.status === 'done', 'the full pass done on the server');
    for (let t = 0; t < 50; t += 1) await act(async () => turn());
    return { ctx, ws };
  }

  /** Fake milliseconds from the full pass being done to its insert. */
  async function waitAfterFullPass(ctx: Ctx): Promise<number> {
    const doneAt = Date.now();
    await inSteps(() => ctx.onTranscript.mock.calls.length > 0, 'the words delivered');
    return Date.now() - doneAt;
  }

  // The review's finding 5: with the language on English, whose outcome the
  // live words cannot change, the insert still waited out the stream's whole
  // 3 s finish budget when it never said done (2 s after the full pass).
  it('does not wait at all when the live words cannot go in: English chosen', async () => {
    window.localStorage.setItem('techsara-voice-language', 'en');
    const { ctx } = await stopWithoutDone('Hello there.');
    expect(await waitAfterFullPass(ctx)).toBe(0);
    expect(ctx.onTranscript).toHaveBeenCalledWith('w0 w1', null);
    // The live words are still offered, said to be part of the recording: they never finished.
    expect(ctx.view.result.current.followUp).toMatchObject({
      message: 'Inserted the full-pass transcript.',
      actionLabel: 'Use what was heard live (part of the recording)',
    });
  });

  it('waits a second at most while they could still go in, then counts them incomplete', async () => {
    const { ctx } = await stopWithoutDone(HINGLISH);
    const waited = await waitAfterFullPass(ctx);
    expect(waited).toBeGreaterThanOrEqual(LIVE_SETTLE_WAIT_MS);
    expect(waited).toBeLessThanOrEqual(LIVE_SETTLE_WAIT_MS + 100);
    expect(ctx.onTranscript).toHaveBeenCalledWith('w0 w1', null);
    expect(ctx.view.result.current.followUp).toMatchObject({
      message: 'Inserted the full-pass transcript.',
      actionLabel: 'Use what was heard live (part of the recording)',
    });
  });

  // Spec 14.2: release 2's gateway runs "auto" on the English model for
  // people whose recent dictations were all English, and says so in ready.
  // Nothing the live stream still says can then make its words go in.
  it('does not wait at all for an Auto stream the server runs on the English model', async () => {
    const { ctx, ws } = await stopWithoutDone('Hello there.', { language: 'en' });
    expect(ws.texts[0]!.language).toBe('auto');
    expect(await waitAfterFullPass(ctx)).toBe(0);
    expect(ctx.onTranscript).toHaveBeenCalledWith('w0 w1', null);
  });

  it('still takes the live words of a Hindi session whose done comes within that second', async () => {
    const { ctx, ws } = await stopWithoutDone(HINGLISH);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(LIVE_SETTLE_WAIT_MS / 2);
    });
    expect(ctx.onTranscript).not.toHaveBeenCalled();
    await act(async () => {
      ws.say({ type: 'done' });
    });
    await inSteps(() => ctx.onTranscript.mock.calls.length > 0, 'the words delivered');
    expect(ctx.onTranscript).toHaveBeenCalledWith(HINGLISH, null);
    expect(ctx.view.result.current.followUp).toMatchObject({ message: 'Inserted the live transcript.' });
  });
});

// ---------------------------------------------------------------------------
// The language control in the bar
// ---------------------------------------------------------------------------

describe('the language control in the recording bar', () => {
  async function recordInComposer() {
    render(
      <Composer streaming={false} prefs={DEFAULT_PREFS} onPrefsChange={vi.fn()} onSend={vi.fn()} onStop={vi.fn()} />,
    );
    await act(async () => {
      fireEvent.click(screen.getByLabelText('Start voice input'));
    });
    await until(() => FakeRecorder.last?.state === 'recording', 'recording started');
  }

  it('offers Auto, English and हिन्दी outside the live region, and hears the rest in the one chosen', async () => {
    await recordInComposer();
    const ws = await goLive(25);
    await act(async () => {
      ws.say({ type: 'final', u: 0, text: 'Hello everyone.', start_sample: 0, end_sample: 8_000 });
      ws.say({ type: 'partial', u: 1, text: 'aaj hum', start_sample: 8_000, end_sample: 16_000 });
    });
    const group = screen.getByRole('radiogroup', { name: 'Language of the live transcript' });
    expect(group.closest('[aria-live]')).toBeNull();
    expect(group.closest('[role="status"]')).toBeNull();
    const radios = within(group).getAllByRole('radio') as HTMLInputElement[];
    expect(radios.map((r) => r.value)).toEqual(['auto', 'en', 'hi']);
    // Native radio buttons with one name: one Tab stop, and the arrow keys move the choice.
    expect(new Set(radios.map((r) => r.name)).size).toBe(1);
    expect(radios.every((r) => r.type === 'radio' && r.tabIndex === 0)).toBe(true);
    expect((screen.getByRole('radio', { name: 'Auto' }) as HTMLInputElement).checked).toBe(true);

    await act(async () => {
      fireEvent.click(screen.getByRole('radio', { name: 'हिन्दी' }));
    });
    expect(window.localStorage.getItem('techsara-voice-language')).toBe('hi');
    expect((screen.getByRole('radio', { name: 'हिन्दी' }) as HTMLInputElement).checked).toBe(true);
    // Once the choice has stood for half a second: a new stream in Hindi from
    // the last committed word; the old one is closed.
    expect(ws.closedWith).toBeNull();
    await act(async () => {
      await vi.advanceTimersByTimeAsync(LIVE_LANGUAGE_DEBOUNCE_MS);
    });
    expect(ws.closedWith).toBe(1000);
    const next = FakeWebSocket.last;
    expect(next).not.toBe(ws);
    expect(next.handshake()).toMatchObject({ language: 'hi', resume_from_sample: 8_000, next_u: 1 });
    expect(next.pcm).toHaveLength(16_000 - 8_000);
    // The committed words stay; the utterance under way is heard again.
    await act(async () => {
      next.say({ type: 'final', u: 1, text: 'आज हम', start_sample: 8_000, end_sample: 16_000 });
    });
    await redraw();
    expect(screen.getByTestId('voice-transcript').textContent).toBe('Hello everyone. आज हम');
  });

  // The review's finding 4: every change opened a socket at once, so walking
  // the options (a held arrow key) opened one per step.
  it('opens one connection for choices made in quick succession, in the last one chosen', async () => {
    await recordInComposer();
    const ws = await goLive(25);
    for (const name of ['English', 'हिन्दी', 'Auto', 'English']) {
      await act(async () => {
        fireEvent.click(screen.getByRole('radio', { name }));
        await vi.advanceTimersByTimeAsync(100);
      });
    }
    expect(FakeWebSocket.instances).toHaveLength(1);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(LIVE_LANGUAGE_DEBOUNCE_MS);
    });
    expect(FakeWebSocket.instances).toHaveLength(2);
    expect(ws.closedWith).toBe(1000);
    expect(FakeWebSocket.last.handshake().language).toBe('en');
    expect(window.localStorage.getItem('techsara-voice-language')).toBe('en');
    expect((screen.getByRole('radio', { name: 'English' }) as HTMLInputElement).checked).toBe(true);
  });

  it('is gone once the live stream is refused for good', async () => {
    await recordInComposer();
    const ws = await goLive(5);
    expect(screen.getByRole('radiogroup', { name: 'Language of the live transcript' })).toBeTruthy();
    await act(async () => {
      ws.say({ type: 'error', code: 'voice_off', message: 'x', retryable: false });
      ws.hangUp(4403);
    });
    expect(screen.queryByRole('radiogroup')).toBeNull();
  });

  it('is not drawn on a server whose sessions have no live path', async () => {
    useServer({}, null);
    await recordInComposer();
    expect(screen.queryByRole('radiogroup')).toBeNull();
  });

  it('starts the next recording in the language chosen last', async () => {
    window.localStorage.setItem('techsara-voice-language', 'en');
    await recordInComposer();
    const ws = await goLive(2);
    expect(ws.texts[0]!.language).toBe('en');
    expect((screen.getByRole('radio', { name: 'English' }) as HTMLInputElement).checked).toBe(true);
  });
});

// ---------------------------------------------------------------------------
// The live words of a Hindi session, before the full pass (spec 13)
// ---------------------------------------------------------------------------

/**
 * A server whose full pass runs until the test sets `server.status = 'done'`.
 * Named like a hook only because `useServer` is one to the linter.
 */
function useHeldFullPass(finalState: FakeOptions['finalState'] = () => ({ text: WHISPER_URDU, language: 'Urdu', language_code: 'ur' })) {
  useServer({ pollsBeforeDone: Number.MAX_SAFE_INTEGER, finalState });
}

/** One slice with these live words, Stop, the worklet's last frame and the live stream's done. */
async function stopAfter(ctx: Ctx, ws: FakeWebSocket, words: string): Promise<number> {
  await talk(ctx, 1);
  await act(async () => {
    ws.say({ type: 'final', u: 0, text: words, start_sample: 0, end_sample: 16_000 });
  });
  await act(async () => {
    ctx.view.result.current.stop();
  });
  await act(async () => {
    FakeWorkletNode.last.flushed(16_000);
    await vi.advanceTimersByTimeAsync(0);
  });
  const doneAt = Date.now();
  await act(async () => {
    ws.say({ type: 'done' });
  });
  return doneAt;
}

describe('the live words of a Hindi session, before the full pass', () => {
  // Measured in the browser end to end (2026-09-30 02:55): a 67 s Hindi
  // dictation's live stream said done 0.2 s after Stop; its insert waited
  // 26.6 s for whisper, and then put the live transcript in anyway.
  it.each([
    ['a Hinglish session, a fifth of whose letters are Devanagari', HINGLISH, null],
    ['Hindi chosen, whatever script the live words came in', 'aaj hum office jayenge', 'hi'],
  ])('go in for %s as soon as the live stream ends; the full pass is offered once it is done, not before', async (_what, words, language) => {
    useHeldFullPass();
    if (language) window.localStorage.setItem('techsara-voice-language', language);
    const ctx = await startRecording();
    const ws = await goLive(25);
    const doneAt = await stopAfter(ctx, ws, words);
    // Real turns only: no timer ran, so nothing waited on one.
    await until(() => ctx.onTranscript.mock.calls.length > 0, 'the live words in');
    expect(Date.now()).toBe(doneAt);
    expect(ctx.onTranscript).toHaveBeenCalledWith(words, null);
    // The full pass is not done; under load the finish request may not even have gone out yet.
    expect(server.status).not.toBe('done');
    await until(() => ctx.view.result.current.state === 'idle', 'idle once the server has the finish');
    expect(ctx.view.result.current.error).toBeNull();
    expect(ctx.view.result.current.followUp).toMatchObject({
      message: 'Inserted the live transcript.',
      tone: 'info',
      actionLabel: null,
    });
    // Twenty seconds of the full pass: nothing to press yet, and nothing else put in.
    for (let t = 0; t < 40; t += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(500);
      });
    }
    expect(ctx.view.result.current.followUp).toMatchObject({ message: 'Inserted the live transcript.', actionLabel: null });
    // Only once every part is in and the finish accepted: flipping a session
    // that is still recording makes the last part's PUT a 409 (closed elsewhere).
    await untilWithClock(() => server.status === 'finishing', 'the finish accepted');
    server.status = 'done';
    await fullPassOffered(ctx);
    // Offered, never put in by itself.
    expect(ctx.onTranscript).toHaveBeenCalledTimes(1);
    await act(async () => ctx.view.result.current.followUp!.run());
    expect(ctx.onTranscript).toHaveBeenLastCalledWith(WHISPER_URDU, null, words, true);
  });

  it('stays finishing, with the words already in, until the server has the recording’s finish', async () => {
    useHeldFullPass();
    let letFinishThrough: () => void = () => undefined;
    const gate = new Promise<void>((resolve) => (letFinishThrough = resolve));
    const serverFetch = server.fetch;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (input: string, init?: RequestInit) => {
        if (String(input).endsWith('/finish')) await gate;
        return serverFetch(input, init);
      }),
    );
    const ctx = await startRecording();
    const ws = await goLive(25);
    await stopAfter(ctx, ws, HINGLISH);
    await until(() => ctx.onTranscript.mock.calls.length > 0, 'the live words in');
    // Until the server has the finish it counts this as the person's one live
    // recording: a new one started now would be refused as another tab's.
    for (let t = 0; t < 50; t += 1) await act(async () => turn());
    expect(ctx.view.result.current.state).toBe('finishing');
    expect(ctx.view.result.current.followUp).toBeNull();
    letFinishThrough();
    await until(() => ctx.view.result.current.state === 'idle', 'idle once the finish is in');
    expect(server.status).toBe('finishing');
    expect(ctx.view.result.current.followUp).toMatchObject({ message: 'Inserted the live transcript.', actionLabel: null });
    expect(ctx.onTranscript).toHaveBeenCalledTimes(1);
  });

  it('keeps English, and Auto without Devanagari, waiting for the full pass', async () => {
    useHeldFullPass(() => ({ text: 'The full pass.', language: 'English', language_code: 'en' }));
    const ctx = await startRecording();
    const ws = await goLive(25);
    await stopAfter(ctx, ws, 'Hello there, this came in live.');
    for (let t = 0; t < 20; t += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(500);
      });
    }
    expect(ctx.onTranscript).not.toHaveBeenCalled();
    expect(ctx.view.result.current.state).toBe('finishing');
    // Only once every part is in and the finish accepted: flipping a session
    // that is still recording makes the last part's PUT a 409 (closed elsewhere).
    await untilWithClock(() => server.status === 'finishing', 'the finish accepted');
    server.status = 'done';
    await untilWithClock(() => ctx.onTranscript.mock.calls.length > 0, 'the full pass in');
    expect(ctx.onTranscript).toHaveBeenCalledWith('The full pass.', null);
    expect(ctx.view.result.current.followUp).toMatchObject({
      message: 'Inserted the full-pass transcript.',
      actionLabel: 'Use the other one',
    });
  });

  it('leaves the live words in place when the full pass then fails: a quiet note, and no Retry over them', async () => {
    const resolveOwner = signedIn();
    useServer({ finalState: () => ({ outcome: 'engine_unavailable', text: null }) });
    const ctx = await dictate(HINGLISH, { resolveOwner });
    expect(ctx.onTranscript).toHaveBeenCalledWith(HINGLISH, null);
    await untilWithClock(
      () => (ctx.view.result.current.followUp?.message ?? '').startsWith('Inserted the live transcript. '),
      'the full pass over',
    );
    const line = ctx.view.result.current.followUp!;
    expect(line).toMatchObject({
      message:
        'Inserted the live transcript. The full-pass transcript could not be made, so there is no other one to swap in.',
      tone: 'info',
      actionLabel: null,
    });
    expect(line.secondaryLabel ?? null).toBeNull();
    expect(ctx.onTranscript).toHaveBeenCalledTimes(1);
    expect(ctx.view.result.current.state).toBe('idle');
    expect(ctx.view.result.current.error).toBeNull();
    // Nothing a reload could offer over the live words: the record goes.
    await untilOutbox((records) => records.length === 0, 'the record settled');
    expect(server.retranscribes).toBe(0);
  });

  it('does not bring back a line the person dismissed before the full pass was done', async () => {
    useHeldFullPass();
    const resolveOwner = signedIn();
    const ctx = await startRecording(resolveOwner);
    const ws = await goLive(25);
    await stopAfter(ctx, ws, HINGLISH);
    await until(() => ctx.view.result.current.followUp !== null, 'the line');
    await act(async () => ctx.view.result.current.followUp!.dismiss());
    expect(ctx.view.result.current.followUp).toBeNull();
    // Only once every part is in and the finish accepted: flipping a session
    // that is still recording makes the last part's PUT a 409 (closed elsewhere).
    await untilWithClock(() => server.status === 'finishing', 'the finish accepted');
    server.status = 'done';
    for (let t = 0; t < 10; t += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(500);
      });
      for (let k = 0; k < 20; k += 1) await act(async () => turn());
    }
    // The full pass is over (its record settled), and the line stays dismissed.
    await untilOutbox((records) => records.length === 0, 'the record settled after the full pass');
    expect(ctx.view.result.current.followUp).toBeNull();
    expect(ctx.onTranscript).toHaveBeenCalledTimes(1);
  });

  it('never writes a late full pass over words the person changed: it only becomes the other one', async () => {
    useHeldFullPass();
    render(
      <Composer streaming={false} prefs={DEFAULT_PREFS} onPrefsChange={vi.fn()} onSend={vi.fn()} onStop={vi.fn()} />,
    );
    const box = screen.getByLabelText('Message') as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: 'Draft:' } });
    await act(async () => {
      fireEvent.click(screen.getByLabelText('Start voice input'));
    });
    await until(() => FakeRecorder.last?.state === 'recording', 'recording started');
    const rec = FakeRecorder.last!;
    const ws = await goLive(25);
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
      rec.emit();
    });
    await until(() => server.appendedSlices.length >= 1, 'slice 0 on the server');
    await act(async () => {
      ws.say({ type: 'final', u: 0, text: HINGLISH, start_sample: 0, end_sample: 16_000 });
      fireEvent.click(screen.getByLabelText('Stop recording and transcribe'));
    });
    await act(async () => {
      FakeWorkletNode.last.flushed(16_000);
      await vi.advanceTimersByTimeAsync(0);
      ws.say({ type: 'done' });
    });
    await until(() => box.value === `Draft: ${HINGLISH}`, 'the live words in the draft');
    expect(server.status).not.toBe('done');
    await until(() => screen.queryByText('Inserted the live transcript.') !== null, 'the line');
    expect(screen.queryByText('Use the other one')).toBeNull();
    // The person corrects a word of what went in, while the full pass runs.
    const edited = `Draft: ${HINGLISH.replace('office', 'ऑफिस')}`;
    fireEvent.change(box, { target: { value: edited } });
    // Only once every part is in and the finish accepted: flipping a session
    // that is still recording makes the last part's PUT a 409 (closed elsewhere).
    await untilWithClock(() => server.status === 'finishing', 'the finish accepted');
    server.status = 'done';
    await untilWithClock(() => screen.queryByText('Use the other one') !== null, 'the full pass offered');
    expect(box.value).toBe(edited);
    await act(async () => {
      fireEvent.click(screen.getByText('Use the other one'));
    });
    expect(box.value).toBe(edited);
    expect(screen.getByText('The inserted words have been edited, so they were left as they are.')).toBeTruthy();
  });
});

// ---------------------------------------------------------------------------
// An utterance that starts with the danda of the one before (spec 14.3)
// ---------------------------------------------------------------------------

describe('an utterance the engine began with the punctuation closing the one before', () => {
  it('is joined straight onto it, in the bar while recording and in the draft after', async () => {
    render(
      <Composer streaming={false} prefs={DEFAULT_PREFS} onPrefsChange={vi.fn()} onSend={vi.fn()} onStop={vi.fn()} />,
    );
    const box = screen.getByLabelText('Message') as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: 'Draft:' } });
    await act(async () => {
      fireEvent.click(screen.getByLabelText('Start voice input'));
    });
    await until(() => FakeRecorder.last?.state === 'recording', 'recording started');
    const rec = FakeRecorder.last!;
    const ws = await goLive(25);
    await act(async () => {
      ws.say({ type: 'final', u: 0, text: 'जंगल में है', start_sample: 0, end_sample: 8_000 });
      ws.say({ type: 'final', u: 1, text: '। फिर हम घर गए', start_sample: 8_000, end_sample: 12_000 });
      ws.say({ type: 'partial', u: 2, text: ', और', start_sample: 12_000, end_sample: 14_000 });
    });
    await redraw();
    expect(screen.getByTestId('voice-transcript').textContent).toBe('जंगल में है। फिर हम घर गए, और');
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
      rec.emit();
    });
    await until(() => server.appendedSlices.length >= 1, 'slice 0 on the server');
    await act(async () => {
      ws.say({ type: 'final', u: 2, text: ', और सो गए।', start_sample: 12_000, end_sample: 16_000 });
      fireEvent.click(screen.getByLabelText('Stop recording and transcribe'));
    });
    await act(async () => {
      FakeWorkletNode.last.flushed(16_000);
      await vi.advanceTimersByTimeAsync(0);
      ws.say({ type: 'done' });
    });
    await untilWithClock(() => box.value !== 'Draft:', 'words in the draft');
    expect(box.value).toBe('Draft: जंगल में है। फिर हम घर गए, और सो गए।');
  });
});
