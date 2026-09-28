// @vitest-environment jsdom
/**
 * The recorder hook on the session road (2026-09-29): what the owner asked
 * for, held at the level the composer actually uses.
 *
 *   "as long as i talk More then 1 hr then Also it work"
 *     → a session is not stopped at ten minutes, and two hours records,
 *       uploads and transcribes without a ceiling anywhere.
 *   "user click on that audio button then it record right ?? and store"
 *     → every 5 s slice goes up WHILE recording, as a numbered part of one
 *       stored recording, and the tab never holds the whole of it.
 *   The owner's error: a quiet first 30 seconds emptied a 181 s recording and
 *   blamed the microphone. Here a recording whose first 30 seconds are silent
 *   comes back with every word after them, and nothing blames anything.
 *
 * BEFORE, measured against origin/dev on 2026-09-29 with the same fakes: the
 * recorder used a 1 s timeslice, made no request at all while recording,
 * stopped itself at exactly 600,000 ms, then posted ONE body of 9,652,200
 * bytes; one dropped request lost the whole recording with no retry.
 *
 * The browser is faked by hand (jsdom has no MediaRecorder): a slice arrives
 * only when a test emits one. The server is tests/voice-session-fake.ts. The
 * SHA-256 here is the real WebCrypto one the browser uses.
 */
import { act, cleanup, fireEvent, render, renderHook, screen } from '@testing-library/react';
import { Blob as NodeBlob } from 'node:buffer';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { Composer } from '@/components/Composer';
import { VoiceBar } from '@/components/VoiceBar';
import { useVoiceRecorder } from '@/components/useVoiceRecorder';
import { DEFAULT_PREFS } from '@/lib/prefs';
import { VoiceSession, type SessionProgress } from '@/lib/voice';
import { FakeSessionServer, SLICE_BYTES, sliceBytes, type FakeOptions } from './voice-session-fake';

const MICROPHONE = /closer to the microphone/i;

class FakeRecorder {
  static last: FakeRecorder | null = null;
  static isTypeSupported = (type: string) => type === 'audio/webm;codecs=opus';
  state: 'inactive' | 'recording' = 'inactive';
  mimeType: string;
  timeslice: number | undefined;
  ondataavailable: ((event: { data: Blob }) => void) | null = null;
  onstop: (() => void) | null = null;
  onerror: (() => void) | null = null;
  next = 0;
  constructor(_stream: MediaStream, options?: { mimeType?: string }) {
    this.mimeType = options?.mimeType ?? '';
    FakeRecorder.last = this;
  }
  start(timeslice?: number) {
    this.timeslice = timeslice;
    this.state = 'recording';
  }
  /** One timeslice of audio, as the browser would hand it over. */
  emit() {
    const idx = this.next;
    this.next += 1;
    this.ondataavailable?.({ data: new NodeBlob([sliceBytes(idx)]) as unknown as Blob });
  }
  stop() {
    if (this.state === 'inactive') return;
    this.state = 'inactive';
    this.emit(); // the spec: a final dataavailable, then stop
    this.onstop?.();
  }
}

let tracks: Array<{ stop: ReturnType<typeof vi.fn> }> = [];
let server: FakeSessionServer;

const turn = () => new Promise<void>((resolve) => setImmediate(resolve));
/**
 * Wait, in real event-loop turns, for real async work (WebCrypto's SHA-256
 * runs on the thread pool) to land. Bounded by wall-clock time, not by a turn
 * count: under the full suite's parallel load a hash takes many more turns.
 * `performance.now` is real; only Date and the timers are faked here.
 */
async function until(cond: () => boolean, budgetMs = 20_000): Promise<void> {
  const deadline = performance.now() + budgetMs;
  while (performance.now() < deadline) {
    if (cond()) return;
    await act(async () => {
      await turn();
    });
  }
  if (!cond()) throw new Error('condition never became true');
}

/** Like `until`, but lets the fake clock run too: the long-poll waits a second between unchanged answers. */
async function untilWithClock(cond: () => boolean, steps = 60): Promise<void> {
  for (let i = 0; i < steps; i += 1) {
    if (cond()) return;
    await act(async () => {
      await vi.advanceTimersByTimeAsync(500);
    });
    for (let t = 0; t < 20; t += 1) await act(async () => turn());
  }
  if (!cond()) throw new Error('condition never became true');
}

function useServer(options: FakeOptions = {}) {
  server = new FakeSessionServer(options);
  vi.stubGlobal('fetch', server.fetch);
}

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
  tracks = [];
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
  vi.stubGlobal('requestAnimationFrame', vi.fn(() => 1));
  vi.stubGlobal('cancelAnimationFrame', vi.fn());
  useServer();
});

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  Reflect.deleteProperty(navigator, 'mediaDevices');
});

/** The real method, taken before any test spies on it. */
const realAddSlice = VoiceSession.prototype.addSlice;

async function startRecording() {
  const onTranscript = vi.fn();
  // The hook builds its VoiceSession privately; the first slice hands us the
  // instance, so the test can read what the tab is actually holding.
  let session: VoiceSession | null = null;
  vi.spyOn(VoiceSession.prototype, 'addSlice').mockImplementation(function (
    this: VoiceSession,
    data: Blob,
    endMs: number,
  ) {
    // eslint-disable-next-line @typescript-eslint/no-this-alias -- capturing the instance IS the point
    session = this;
    return realAddSlice.call(this, data, endMs);
  });
  const view = renderHook(() => useVoiceRecorder({ onTranscript, maxMs: 10 * 60 * 1000 }));
  await act(async () => {
    view.result.current.start();
  });
  await until(() => view.result.current.state === 'recording');
  return { view, onTranscript, session: () => session as VoiceSession | null, rec: FakeRecorder.last! };
}

/** Talk for `slices` × 5 s, letting each part reach the server before the next. */
async function talk(ctx: Awaited<ReturnType<typeof startRecording>>, slices: number, wait = true) {
  for (let i = 0; i < slices; i += 1) {
    const idx = ctx.rec.next;
    // A timeslice is handed over at its END, as MediaRecorder does.
    await act(async () => {
      await vi.advanceTimersByTimeAsync(5000);
      ctx.rec.emit();
    });
    if (wait) await until(() => server.appendedSlices.length >= idx + 1);
  }
}

async function stopAndSettle(ctx: Awaited<ReturnType<typeof startRecording>>) {
  await act(async () => {
    ctx.view.result.current.stop();
  });
  for (let i = 0; i < 40 && ctx.view.result.current.state === 'finishing'; i += 1) {
    await act(async () => {
      await vi.advanceTimersByTimeAsync(1000);
    });
    for (let t = 0; t < 20; t += 1) await act(async () => turn());
  }
}

describe('a session is not stopped at ten minutes', () => {
  it('records two hours, uploading every 5 s while recording, and transcribes all of it', async () => {
    const ctx = await startRecording();
    expect(ctx.rec.timeslice).toBe(5000);
    expect(ctx.view.result.current.mode).toBe('session');
    expect(ctx.view.result.current.limitMs).toBeNull();

    await talk(ctx, 121); // 10:05
    expect(ctx.view.result.current.state).toBe('recording');
    await talk(ctx, 1440 - 121); // 2:00:00
    expect(ctx.view.result.current.state).toBe('recording');
    expect(ctx.view.result.current.elapsedMs).toBeGreaterThanOrEqual(7_200_000);

    // Every part went up while recording, not at the end.
    const puts = server.log.filter((r) => r.method === 'PUT');
    expect(puts).toHaveLength(1440);
    expect(Math.max(...puts.map((r) => r.bytes))).toBe(SLICE_BYTES);
    // The transcript came back as the person talked.
    expect(ctx.view.result.current.progress?.preview.endsWith('w1439')).toBe(true);

    await stopAndSettle(ctx);
    expect(ctx.view.result.current.state).toBe('idle');
    expect(ctx.view.result.current.error).toBeNull();
    expect(server.appendedSlices).toHaveLength(1441);
    expect(server.appendedSlices.every((idx, i) => idx === i)).toBe(true);
    const [text, notice] = ctx.onTranscript.mock.calls[0]!;
    expect(text.split(' ')).toHaveLength(1441);
    expect(notice).toBeNull();
    for (const t of tracks) expect(t.stop).toHaveBeenCalled();
  }, 240_000);
});

describe('the recorder does not accumulate the session in memory', () => {
  it('holds at most two 5 s slices at any moment of an hour, where the old recorder held all of it', async () => {
    const ctx = await startRecording();
    let peak = 0;
    for (let i = 0; i < 720; i += 1) {
      await talk(ctx, 1);
      peak = Math.max(peak, ctx.session()!.stats().heldBytes);
    }
    const stats = ctx.session()!.stats();
    // An hour at 128.7 kb/s is 57,914,640 bytes. The old recorder kept every
    // chunk in one array until Stop; this one lets each go once it is acked.
    expect(stats.bytesAdded).toBe(720 * SLICE_BYTES);
    expect(stats.bytesAcked).toBe(720 * SLICE_BYTES);
    expect(stats.peakHeldBytes).toBeLessThanOrEqual(2 * SLICE_BYTES);
    expect(peak).toBeLessThanOrEqual(2 * SLICE_BYTES);
    expect(stats.heldBytes).toBe(0);
  }, 240_000);
});

describe('a chunk that fails is retried and the session continues', () => {
  it('keeps recording through a dropped network and a lost answer, and loses nothing', async () => {
    useServer({
      inject: ({ method, seq, attempt }) => {
        if (method !== 'PUT' || seq !== 3) return undefined;
        if (attempt < 2) return { network: true };
        if (attempt === 2) return { dropResponse: true };
        return undefined;
      },
    });
    const ctx = await startRecording();
    for (let i = 0; i < 10; i += 1) {
      // The recorder does not wait for the network: a slice every 5 s.
      await talk(ctx, 1, false);
      for (let t = 0; t < 40; t += 1) await act(async () => turn());
      expect(ctx.view.result.current.state).toBe('recording');
      expect(ctx.view.result.current.error).toBeNull();
    }
    // (That the bar says "Connection lost" while this happens is the bar's
    // test below; progress.offline itself is asserted in voice-session-upload.)
    await untilWithClock(() => server.appendedSlices.length === 10);
    // Two sends with no answer, one whose answer was lost, and the replay the
    // server recognised as a duplicate: part 3 went up at least four times.
    expect(server.putsFor(3).length).toBeGreaterThanOrEqual(4);
    expect(new Set(server.putsFor(3).map((r) => r.headers['x-part-sha256'])).size).toBe(1);

    await stopAndSettle(ctx);
    expect(server.appendedSlices).toEqual([0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10]);
    expect(ctx.onTranscript).toHaveBeenCalledTimes(1);
    expect(ctx.view.result.current.error).toBeNull();
  }, 60_000);
});

describe('a recording whose first 30 seconds are quiet', () => {
  it('delivers every word after the pause, and blames no microphone', async () => {
    // Six 5 s slices of silence, as the owner's 181 s recording opened with
    // about 25 s of it; then speech.
    useServer({ quietSlices: 6 });
    const ctx = await startRecording();
    await talk(ctx, 6);
    expect(ctx.view.result.current.progress?.preview).toBe('');
    await talk(ctx, 30);
    // Words appear while recording, not only at the end.
    expect(ctx.view.result.current.progress?.preview.startsWith('w6 w7')).toBe(true);
    await stopAndSettle(ctx);
    expect(ctx.view.result.current.error).toBeNull();
    const [text] = ctx.onTranscript.mock.calls[0]!;
    expect(text.split(' ')[0]).toBe('w6');
    expect(text.split(' ')).toHaveLength(31);
    expect(String(ctx.view.result.current.error?.message ?? '')).not.toMatch(MICROPHONE);
  }, 60_000);
});

describe('what the recorder tells the person', () => {
  it('a sign-out mid-recording stops it, releases the microphone and says what was saved', async () => {
    useServer({
      inject: ({ method, seq }) =>
        method === 'PUT' && seq === 4 ? { status: 401, body: { detail: 'Not authenticated' } } : undefined,
    });
    const ctx = await startRecording();
    await talk(ctx, 4);
    await talk(ctx, 1, false);
    await until(() => ctx.view.result.current.state === 'error');
    expect(ctx.view.result.current.error).toEqual({
      message:
        'You were signed out. Everything up to 0:20 is saved on the server; the last few seconds will be lost if you close this tab.',
      retryable: false,
    });
    for (const t of tracks) expect(t.stop).toHaveBeenCalled();
  });

  it('asks before discarding a recording over a minute, and deletes it from the server when told to', async () => {
    const ctx = await startRecording();
    await talk(ctx, 13); // 1:05
    const confirm = vi.fn(() => false);
    vi.stubGlobal('confirm', confirm);
    await act(async () => ctx.view.result.current.cancel());
    expect(confirm).toHaveBeenCalledWith('Discard this 1:05 recording? It is saved on the server until you discard it.');
    expect(ctx.view.result.current.state).toBe('recording');
    confirm.mockReturnValue(true);
    await act(async () => ctx.view.result.current.cancel());
    await until(() => server.deleted);
    expect(ctx.view.result.current.state).toBe('idle');
    expect(ctx.onTranscript).not.toHaveBeenCalled();
  });

  it('falls back to the ten-minute road, and says so, on a server without sessions', async () => {
    useServer();
    server.fetch.mockImplementationOnce(async () => ({
      ok: false,
      status: 404,
      headers: { get: () => null },
      text: async () => JSON.stringify({ detail: 'off', reason: 'sessions_off' }),
    }) as unknown as Response);
    const ctx = await startRecording();
    expect(ctx.view.result.current.mode).toBe('legacy');
    expect(ctx.view.result.current.limitMs).toBe(600_000);
    expect(ctx.rec.timeslice).toBe(1000);
    expect(ctx.view.result.current.hint).toBe(
      "Long recordings aren't enabled here, so this one stops at 10 minutes.",
    );
  });
});

// ---------------------------------------------------------------------------
// The bar and the composer
// ---------------------------------------------------------------------------

const progress = (over: Partial<SessionProgress> = {}): SessionProgress => ({
  preview: '',
  tentative: '',
  audioMs: 0,
  backlogMs: 0,
  waitingOn: 'none',
  pendingParts: 0,
  pendingMs: 0,
  offline: false,
  storageTrouble: false,
  lastAckAt: null,
  retentionDays: 0,
  progressive: true,
  ...over,
});

function bar(over: Partial<Parameters<typeof VoiceBar>[0]> = {}) {
  return render(
    <VoiceBar
      state="recording"
      levels={[]}
      elapsedMs={9 * 60_000 + 50_000}
      maxMs={null}
      progress={progress()}
      onCancel={vi.fn()}
      onStop={vi.fn()}
      {...over}
    />,
  );
}

describe('the recording bar on the session road', () => {
  it('shows no countdown and no "will stop" warning at 9:50, because nothing stops', () => {
    bar();
    expect(screen.getByText('9:50')).toBeTruthy();
    expect(screen.queryByText(/^−/)).toBeNull();
    expect(screen.queryByTitle('Recording will stop at the limit')).toBeNull();
  });

  it('counts past an hour in hours', () => {
    bar({ elapsedMs: 2 * 3_600_000 + 5_000 });
    expect(screen.getByText('2:00:05')).toBeTruthy();
  });

  it('shows the words so far, the words still settling, and where the recording is kept', () => {
    bar({ progress: progress({ preview: 'the first sentence', tentative: 'and the sec', retentionDays: 30 }) });
    expect(screen.getByText(/the first sentence/)).toBeTruthy();
    expect(screen.getByText('and the sec')).toBeTruthy();
    expect(screen.getByText('Saved to your account · kept 30 days')).toBeTruthy();
  });

  it('says why the transcript is behind, and what the connection is doing', () => {
    const view = bar({ progress: progress({ backlogMs: 42_000, waitingOn: 'chat' }) });
    expect(
      screen.getByText('Transcript 0:42 behind — paused while someone is waiting for a chat answer'),
    ).toBeTruthy();
    view.unmount();
    bar({ progress: progress({ offline: true }) });
    expect(
      screen.getByText('Connection lost. Still recording; the audio will upload when the connection is back.'),
    ).toBeTruthy();
  });

  it('after Stop, says how much audio is left to finish, or what it is waiting for', () => {
    const view = bar({ state: 'finishing', progress: progress({ backlogMs: 35_000 }) });
    expect(screen.getByText('Finishing the last 0:35 of audio…')).toBeTruthy();
    view.unmount();
    bar({ state: 'finishing', progress: progress({ offline: true, pendingMs: 12_000 }) });
    expect(screen.getByText('Waiting for a connection to upload the last 0:12 of your recording…')).toBeTruthy();
  });

  it('keeps the legacy road’s countdown where there IS a ten-minute limit', () => {
    bar({ maxMs: 600_000, progress: null });
    expect(screen.getByText('−0:10')).toBeTruthy();
  });
});

describe('the composer on the session road', () => {
  beforeEach(() => {
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
  });

  it('puts the words in the draft, offers Retry for the gaps, and Retry fills them in place', async () => {
    // The engine missed 0:10-0:15 the first time; the saved audio is re-read.
    useServer({
      finalState: (words, s) =>
        s.retranscribes === 0
          ? {
              outcome: 'transcribed_with_gaps',
              gaps: [{ start_ms: 10_000, end_ms: 15_000, reason: 'engine_unavailable' }],
              text: words.filter((w) => w !== 'w2').join(' '),
            }
          : {},
    });
    render(
      <Composer streaming={false} prefs={DEFAULT_PREFS} onPrefsChange={vi.fn()} onSend={vi.fn()} onStop={vi.fn()} />,
    );
    const box = screen.getByLabelText('Message') as HTMLTextAreaElement;
    fireEvent.change(box, { target: { value: 'Draft:' } });
    await act(async () => {
      fireEvent.click(screen.getByLabelText('Start voice input'));
    });
    await until(() => FakeRecorder.last?.state === 'recording');
    const rec = FakeRecorder.last!;
    for (let i = 0; i < 4; i += 1) {
      await act(async () => {
        await vi.advanceTimersByTimeAsync(5000);
        rec.emit();
      });
      await until(() => server.appendedSlices.length >= i + 1);
    }
    expect(screen.getByText('Saved to your account')).toBeTruthy();
    expect(screen.getByText(/w0 w1 w2 w3/)).toBeTruthy();

    await act(async () => {
      fireEvent.click(screen.getByLabelText('Stop recording and transcribe'));
    });
    await untilWithClock(() => box.value !== 'Draft:');
    expect(box.value).toBe('Draft: w0 w1 w3 w4');
    expect(
      screen.getByText(
        "The speech engine couldn't transcribe 1 part (0:10–0:15). The audio is saved. Press Retry to transcribe them.",
      ),
    ).toBeTruthy();

    await act(async () => {
      fireEvent.click(screen.getByText('Retry'));
    });
    await untilWithClock(() => box.value === 'Draft: w0 w1 w2 w3 w4');
    expect(server.retranscribes).toBe(1);
    expect(screen.queryByText('Retry')).toBeNull();
  });
});
