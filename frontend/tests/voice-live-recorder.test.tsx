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
 *   - when the stored transcript cannot be had, the live words are OFFERED;
 *   - the live words redraw at most ten times a second;
 *   - a second Stop inside the stop event's delay no longer strands the
 *     recording (the audit's stop() race).
 */
import { act, cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react';
import { Blob as NodeBlob } from 'node:buffer';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Composer } from '@/components/Composer';
import { useVoiceRecorder } from '@/components/useVoiceRecorder';
import { DEFAULT_PREFS } from '@/lib/prefs';
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

async function startRecording() {
  const onTranscript = vi.fn();
  const view = renderHook(() => useVoiceRecorder({ onTranscript, maxMs: 10 * 60 * 1000 }));
  await act(async () => {
    view.result.current.start();
  });
  await until(() => view.result.current.state === 'recording', 'recording started');
  return { view, onTranscript, rec: FakeRecorder.last! };
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

/** The worklet's first frames, then the socket's handshake. */
async function goLive(frames = 25): Promise<FakeWebSocket> {
  const node = FakeWorkletNode.last;
  await act(async () => {
    node.started(48000);
    node.frames(0, frames);
  });
  const ws = FakeWebSocket.last;
  await act(async () => {
    ws.handshake();
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
