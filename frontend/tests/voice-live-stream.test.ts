/**
 * The live stream's client (lib/voiceLive.ts LiveStream and LiveCapture)
 * against a WebSocket whose server side the test plays
 * (tests/voice-live-fakes.ts), on fake timers.
 *
 * The protocol is the build spec's section 2, and each block below holds one
 * promise of it:
 *   - `start` first, with the whole contract, then binary 40 ms frames only
 *     after `ready`, replayed from sample 0 so the opening words are heard;
 *   - a drop reconnects on 0.5, 1, 2, 4, 8, then 15 s (±20%), resumes where
 *     the last final ended with the numbering carried on, and never retries a
 *     refusal retrying cannot fix;
 *   - backpressure at 256 KiB queued, released below 64 KiB, and a ring that
 *     overflowed meanwhile is resumed from its oldest sample;
 *   - Stop: the rest of the audio, then `flush`, nothing after it, `done`
 *     closes; never longer than the 3 s budget;
 *   - cancel closes at once with 1000; ping, pong timeout, client_stats;
 *   - the capture: worklet on the context, flush before the socket's flush,
 *     and redraws at most ten times a second.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { parseLiveConfig } from '@/lib/voice';
import {
  CaptureClock,
  LIVE_FINISH_BUDGET_MS,
  LIVE_SUBPROTOCOL,
  LiveCapture,
  LiveStream,
  LiveTranscript,
  PcmRing,
  type LiveEnd,
  type LiveView,
} from '@/lib/voiceLive';
import {
  FakeLiveAudioContext,
  FakeWebSocket,
  FakeWorkletNode,
  log,
  resetLiveFakes,
} from './voice-live-fakes';

const URL_ = 'ws://host.test/api/audio/sessions/abc/live';
const WS = FakeWebSocket as unknown as typeof WebSocket;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
  vi.setSystemTime(1_000_000);
  resetLiveFakes();
});

afterEach(() => {
  vi.useRealTimers();
});

/** A frame's samples: their own index, so what was sent can be checked sample by sample. */
const samplesAt = (index: number, length = 640) =>
  Int16Array.from({ length }, (_, i) => (index + i) % 30000);

function setup(over: { ring?: PcmRing; language?: 'auto' | 'en' | 'hi'; random?: () => number; resumeMaxS?: number } = {}) {
  const ring = over.ring ?? new PcmRing(60);
  const clock = new CaptureClock(48000);
  const transcript = new LiveTranscript();
  const ends: LiveEnd[] = [];
  let changes = 0;
  const stream = new LiveStream({
    url: URL_,
    ring,
    clock,
    transcript,
    recorderStartedAt: Date.now(),
    language: over.language ?? 'auto',
    resumeMaxS: over.resumeMaxS ?? 60,
    onChange: () => (changes += 1),
    onEnd: (end) => ends.push(end),
    WebSocketImpl: WS,
    now: () => Date.now(),
    random: over.random ?? (() => 0.5),
  });
  /** The worklet posting `count` frames from sample `from`. */
  const push = (from: number, count = 1, length = 640) => {
    for (let k = 0; k < count; k += 1) {
      const index = from + k * length;
      ring.push(index, samplesAt(index, length));
      clock.note(index, length, Date.now());
      stream.audioArrived();
    }
  };
  return { ring, clock, transcript, stream, ends, push, changes: () => changes };
}

describe('opening a live stream', () => {
  it('waits for audio, then connects once, to the session’s socket, with the subprotocol', () => {
    const { stream, push } = setup();
    stream.start();
    expect(FakeWebSocket.instances).toHaveLength(0);
    push(0);
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(FakeWebSocket.last.url).toBe(URL_);
    expect(FakeWebSocket.last.protocols).toEqual([LIVE_SUBPROTOCOL]);
    push(640);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it('says start first, with the whole contract and the chosen language', () => {
    const { push } = setup({ language: 'hi' });
    push(0);
    FakeWebSocket.last.accept();
    expect(FakeWebSocket.last.texts[0]).toEqual({
      type: 'start',
      v: 1,
      encoding: 'pcm_s16le',
      sample_rate: 16000,
      channels: 1,
      frame_ms: 40,
      source: 'mic',
      resume_from_sample: 0,
      next_u: 0,
      // Sample 639 arrived at the recorder's start: sample 0 was captured
      // 2.67 ms (a quantum) plus 39.9 ms (639 samples) before it.
      clock_offset_ms: -43,
      language: 'hi',
    });
  });

  it('sends no audio before ready, then every buffered frame from sample 0, as 1280-byte frames', () => {
    const { push } = setup();
    push(0, 5); // spoken while the session was being created
    const ws = FakeWebSocket.last;
    ws.accept();
    expect(ws.frames).toHaveLength(0);
    ws.say({ type: 'ready', v: 1, resume_from_sample: 0, next_u: 0 });
    expect(ws.frames.map((f) => f.byteLength)).toEqual([1280, 1280, 1280, 1280, 1280]);
    expect(ws.pcm).toEqual([...samplesAt(0, 3200)]);
    push(3200);
    expect(ws.frames).toHaveLength(6);
    expect(ws.texts.map((t) => t.type)).toEqual(['start']);
  });
});

describe('the words that come back', () => {
  it('replaces a partial, commits a final, and asks for a redraw each time', () => {
    const { push, transcript, changes } = setup();
    push(0, 10);
    FakeWebSocket.last.handshake();
    const ws = FakeWebSocket.last;
    ws.say({ type: 'partial', u: 0, text: 'hel', start_sample: 0, end_sample: 3200 });
    ws.say({ type: 'partial', u: 0, text: 'hello wor', start_sample: 0, end_sample: 6400 });
    expect(transcript.text()).toBe('hello wor');
    ws.say({ type: 'final', u: 0, text: 'Hello world.', start_sample: 0, end_sample: 6400 });
    expect(transcript.text()).toBe('Hello world.');
    expect(transcript.partial).toBeNull();
    expect(changes()).toBe(3);
    // Repeated: nothing to redraw.
    ws.say({ type: 'final', u: 0, text: 'Hello world.', start_sample: 0, end_sample: 6400 });
    expect(changes()).toBe(3);
  });

  it('ignores what is not an event it knows, and cuts text it cannot trust', () => {
    const { push, transcript, changes } = setup();
    push(0);
    FakeWebSocket.last.handshake();
    const ws = FakeWebSocket.last;
    ws.onmessage!({ data: 'not json' });
    ws.onmessage!({ data: new ArrayBuffer(8) });
    ws.say({ type: 'partial', u: -1, text: 'x' });
    ws.say({ type: 'partial', u: 0.5, text: 'x' });
    ws.say({ type: 'partial', u: 0, text: 42 });
    ws.say({ type: 'speech', active: true, sample: 10 });
    ws.say({ type: 'pong', t: 1 });
    expect(changes()).toBe(0);
    ws.say({ type: 'partial', u: 0, text: 'y'.repeat(5000), start_sample: 0, end_sample: 640 });
    expect(transcript.partial!.text.length).toBe(4000);
  });
});

describe('a dropped connection', () => {
  it('reconnects after 0.5, 1, 2, 4, 8 and then every 15 s, and starts over once one is ready', () => {
    const { push } = setup();
    push(0);
    const expected = [500, 1000, 2000, 4000, 8000, 15_000, 15_000];
    for (const delay of expected) {
      const before = FakeWebSocket.instances.length;
      FakeWebSocket.last.hangUp(1006);
      vi.advanceTimersByTime(delay - 1);
      expect(FakeWebSocket.instances.length).toBe(before);
      vi.advanceTimersByTime(1);
      expect(FakeWebSocket.instances.length).toBe(before + 1);
    }
    FakeWebSocket.last.handshake();
    const before = FakeWebSocket.instances.length;
    FakeWebSocket.last.hangUp(1006);
    vi.advanceTimersByTime(500);
    expect(FakeWebSocket.instances.length).toBe(before + 1);
  });

  it.each([
    [0, 400],
    [0.999, 600],
  ])('jitters each wait by up to 20%% (random %f: %i ms)', (random, wait) => {
    const { push } = setup({ random: () => random });
    push(0);
    FakeWebSocket.last.hangUp(1006);
    vi.advanceTimersByTime(wait - 1);
    expect(FakeWebSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(FakeWebSocket.instances).toHaveLength(2);
  });

  it('resumes where the last final ended, numbering on, and loses no word spoken meanwhile', () => {
    const { push, transcript } = setup();
    push(0, 100); // 4 s
    FakeWebSocket.last.handshake();
    const first = FakeWebSocket.last;
    first.say({ type: 'final', u: 0, text: 'One two.', start_sample: 0, end_sample: 32_000 });
    first.say({ type: 'partial', u: 1, text: 'three', start_sample: 33_000, end_sample: 40_000 });
    first.hangUp(1006);
    push(64_000, 25); // a second spoken while it was down
    vi.advanceTimersByTime(500);
    const second = FakeWebSocket.last;
    expect(second).not.toBe(first);
    const start = second.handshake();
    expect(start.resume_from_sample).toBe(32_000);
    expect(start.next_u).toBe(1);
    // The replay starts at sample 32,000 and runs to the newest sample.
    expect(second.pcm).toEqual([...samplesAt(32_000, 80_000 - 32_000)]);
    // The server's replayed final for u=0 changes nothing; u=1 continues.
    second.say({ type: 'final', u: 0, text: 'One two.', start_sample: 0, end_sample: 32_000 });
    second.say({ type: 'final', u: 1, text: 'Three four.', start_sample: 33_000, end_sample: 60_000 });
    expect(transcript.text()).toBe('One two. Three four.');
  });

  it('resumes from the oldest sample still held when the last final is older than that', () => {
    const { push } = setup({ ring: new PcmRing(1) });
    push(0, 75); // 3 s into a 1 s ring
    FakeWebSocket.last.hangUp(1006);
    vi.advanceTimersByTime(500);
    expect(FakeWebSocket.last.handshake().resume_from_sample).toBe(48_000 - 16_000);
  });

  it.each([4400, 4401, 4403, 4404, 4409, 4413])('does not retry close %i, and stops showing live words', (code) => {
    const { push, stream, ends, changes } = setup();
    push(0);
    FakeWebSocket.last.handshake();
    FakeWebSocket.last.say({ type: 'error', code: 'x', message: 'x', retryable: false });
    FakeWebSocket.last.hangUp(code);
    vi.advanceTimersByTime(120_000);
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(stream.state).toBe('failed');
    expect(stream.active).toBe(false);
    expect(ends).toEqual(['refused']);
    expect(changes()).toBe(1); // the redraw that drops the live words
  });

  it('does not retry an error the server marked not retryable, whatever the close code', () => {
    const { push } = setup();
    push(0);
    FakeWebSocket.last.handshake();
    FakeWebSocket.last.say({ type: 'error', code: 'rate_limited', message: 'x', retryable: false });
    FakeWebSocket.last.hangUp(4429);
    vi.advanceTimersByTime(60_000);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it.each([1006, 1012, 4408, 4429, 4500, 4503])('retries close %i with backoff', (code) => {
    const { push } = setup();
    push(0);
    FakeWebSocket.last.handshake();
    FakeWebSocket.last.say({ type: 'error', code: 'x', message: 'x', retryable: true });
    FakeWebSocket.last.hangUp(code);
    vi.advanceTimersByTime(500);
    expect(FakeWebSocket.instances).toHaveLength(2);
  });

  it('treats a socket the browser refuses to build like a drop', () => {
    const { push } = setup();
    FakeWebSocket.refuse = true;
    push(0);
    expect(FakeWebSocket.instances).toHaveLength(0);
    FakeWebSocket.refuse = false;
    vi.advanceTimersByTime(500);
    expect(FakeWebSocket.instances).toHaveLength(1);
  });

  it('goes at once when the browser comes back online', () => {
    const { push, stream } = setup();
    push(0);
    FakeWebSocket.last.hangUp(1006);
    vi.advanceTimersByTime(100);
    stream.nudge();
    expect(FakeWebSocket.instances).toHaveLength(2);
  });
});

describe('backpressure', () => {
  it('stops handing frames over past 256 KiB queued and resumes below 64 KiB', () => {
    const { push } = setup();
    push(0);
    const ws = FakeWebSocket.last;
    ws.holdBuffer = true;
    ws.handshake();
    push(640, 299); // 12 s of audio at once: a replay after an outage
    // 205 frames is the first count past 262,144 bytes (205 x 1280 = 262,400).
    expect(ws.frames).toHaveLength(205);
    ws.bufferedAmount = 100 * 1024;
    vi.advanceTimersByTime(200);
    expect(ws.frames).toHaveLength(205); // still above the low-water mark
    ws.bufferedAmount = 60 * 1024;
    ws.holdBuffer = false;
    vi.advanceTimersByTime(50);
    expect(ws.frames).toHaveLength(300);
    expect(ws.pcm).toEqual([...samplesAt(0, 300 * 640)]);
  });

  it('resumes from the oldest sample held when the ring overflowed meanwhile, and counts the gap', () => {
    const { push, stream } = setup({ ring: new PcmRing(1) });
    push(0);
    const first = FakeWebSocket.last;
    first.holdBuffer = true;
    first.bufferedAmount = 300 * 1024; // the uplink is stuck
    first.handshake();
    expect(first.frames).toHaveLength(0);
    push(640, 49); // 2 s more into a 1 s ring
    expect(stream.gaps).toBe(1);
    expect(first.closedWith).toBe(1000);
    const second = FakeWebSocket.last;
    expect(second).not.toBe(first);
    expect(second.handshake().resume_from_sample).toBe(32_000 - 16_000);
  });
});

describe('Stop', () => {
  it('sends the rest of the audio, then flush, then nothing; done closes it', async () => {
    const { push, stream, ends } = setup();
    push(0, 3);
    FakeWebSocket.last.handshake();
    const ws = FakeWebSocket.last;
    let finished = false;
    void stream.finish().then(() => (finished = true));
    expect(ws.texts.map((t) => t.type)).toEqual(['start', 'flush']);
    expect(ws.frames).toHaveLength(3);
    push(1920); // a frame that arrives after Stop is past the end the server was told
    expect(ws.frames).toHaveLength(3);
    ws.say({ type: 'final', u: 0, text: 'Last words.', start_sample: 0, end_sample: 1920 });
    ws.say({ type: 'done' });
    await vi.advanceTimersByTimeAsync(0);
    expect(finished).toBe(true);
    expect(ws.closedWith).toBe(1000);
    expect(ends).toEqual(['done']);
    expect(stream.state).toBe('closed');
  });

  it('never takes longer than the 3 s budget', async () => {
    const { push, stream, ends } = setup();
    push(0);
    FakeWebSocket.last.handshake();
    let finished = false;
    void stream.finish().then(() => (finished = true));
    await vi.advanceTimersByTimeAsync(LIVE_FINISH_BUDGET_MS - 1);
    expect(finished).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(finished).toBe(true);
    expect(FakeWebSocket.last.closedWith).toBe(1000);
    expect(ends).toEqual(['timeout']);
  });

  it('flushes right after ready when Stop came while still connecting', () => {
    const { push, stream } = setup();
    push(0, 2);
    void stream.finish();
    const ws = FakeWebSocket.last;
    ws.handshake();
    expect(ws.texts.map((t) => t.type)).toEqual(['start', 'flush']);
    expect(ws.frames).toHaveLength(2);
  });

  it('reconnects at once to deliver the end when Stop came during a backoff', () => {
    const { push, stream } = setup();
    push(0);
    FakeWebSocket.last.hangUp(1006);
    void stream.finish();
    expect(FakeWebSocket.instances).toHaveLength(2);
  });

  it('opens nothing for a recording with no audio at all', async () => {
    const { stream, ends } = setup();
    await stream.finish();
    expect(FakeWebSocket.instances).toHaveLength(0);
    expect(ends).toEqual(['nothing']);
  });
});

describe('cancel, liveness and statistics', () => {
  it('closes at once with 1000 on cancel and never reconnects', () => {
    const { push, stream, ends } = setup();
    push(0);
    FakeWebSocket.last.handshake();
    stream.abort();
    expect(FakeWebSocket.last.closedWith).toBe(1000);
    push(640, 10);
    vi.advanceTimersByTime(60_000);
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(ends).toEqual(['aborted']);
  });

  it('pings every 10 s, and starts again when nothing at all answers', () => {
    const { push } = setup();
    push(0);
    const first = FakeWebSocket.last;
    first.handshake();
    vi.advanceTimersByTime(10_000);
    const pings = first.texts.filter((t) => t.type === 'ping');
    expect(pings).toHaveLength(1);
    expect(typeof pings[0]!.t).toBe('number');
    first.say({ type: 'pong', t: pings[0]!.t });
    vi.advanceTimersByTime(10_000);
    expect(first.texts.filter((t) => t.type === 'ping')).toHaveLength(2);
    // Silence for ten seconds after that ping: the socket is dead.
    vi.advanceTimersByTime(11_000);
    expect(first.closedWith).toBe(1000);
    vi.advanceTimersByTime(500);
    expect(FakeWebSocket.instances).toHaveLength(2);
  });

  it('reports capture-to-screen latencies every 5 s, at most 64 per list, and nothing when there are none', () => {
    const { push, stream } = setup();
    push(0);
    const ws = FakeWebSocket.last;
    ws.handshake();
    // Frames arrive as a worklet posts them: one every 40 ms.
    let k = 0;
    const frame = () => {
      vi.advanceTimersByTime(40);
      k += 1;
      push(k * 640);
    };
    for (let i = 0; i < 125; i += 1) frame(); // five silent seconds
    expect(ws.texts.filter((t) => t.type === 'client_stats')).toHaveLength(0);
    for (let i = 0; i < 100; i += 1) {
      frame();
      ws.say({ type: 'partial', u: 0, text: `w${k}`, start_sample: 0, end_sample: (k + 1) * 640 });
      stream.rendered(Date.now() + 30); // drawn 30 ms after the frame arrived
    }
    ws.say({ type: 'final', u: 0, text: 'all the words', start_sample: 0, end_sample: (k + 1) * 640 });
    stream.rendered(Date.now() + 120);
    for (let i = 0; i < 150; i += 1) frame();
    const stats = ws.texts.filter((t) => t.type === 'client_stats');
    expect(stats.length).toBeGreaterThanOrEqual(1);
    for (const s of stats) {
      expect((s.partial_ms as number[]).length).toBeLessThanOrEqual(64);
      expect((s.final_ms as number[]).length).toBeLessThanOrEqual(64);
    }
    // From the capture of the last sample each covered: 30 ms to the screen
    // plus the 2.67 ms render quantum before the frame left the worklet.
    const partial = stats.flatMap((s) => s.partial_ms as number[]);
    expect(partial.length).toBeGreaterThan(0);
    expect(new Set(partial)).toEqual(new Set([33]));
    expect(stats.flatMap((s) => s.final_ms as number[])).toEqual([123]);
    // Never two reports within 5 s.
    const sentAt: number[] = [];
    let seen = 0;
    const counted = ws.texts.filter((t) => t.type === 'client_stats').length;
    for (let i = 0; i < 500; i += 1) {
      frame();
      // u=1: the final above committed u=0, and a partial for it is ignored.
      ws.say({ type: 'partial', u: 1, text: `x${k}`, start_sample: 0, end_sample: (k + 1) * 640 });
      stream.rendered();
      const n = ws.texts.filter((t) => t.type === 'client_stats').length;
      if (n > counted + seen) {
        seen += 1;
        sentAt.push(Date.now());
      }
    }
    expect(sentAt.length).toBeGreaterThanOrEqual(3);
    for (let i = 1; i < sentAt.length; i += 1) expect(sentAt[i]! - sentAt[i - 1]!).toBeGreaterThanOrEqual(5000);
  });
});

// ---------------------------------------------------------------------------
// The capture: worklet, ring and stream together
// ---------------------------------------------------------------------------

const LIVE = parseLiveConfig({ path: '/api/audio/sessions/{id}/live', sample_rate: 16000, frame_ms: 40, resume_max_s: 60 })!;
const SID = 'c'.repeat(32);
const HERE = { protocol: 'http:', host: 'host.test' };

function newCapture(onChange?: (view: LiveView | null) => void) {
  return new LiveCapture({
    onChange,
    WebSocketImpl: WS,
    AudioWorkletNodeImpl: FakeWorkletNode as unknown as typeof AudioWorkletNode,
    now: () => Date.now(),
    random: () => 0.5,
  });
}

async function attached(onChange?: (view: LiveView | null) => void) {
  const context = new FakeLiveAudioContext();
  const source = context.createMediaStreamSource({} as MediaStream);
  const capture = newCapture(onChange);
  const ok = await capture.attach(context as unknown as BaseAudioContext, source as unknown as AudioNode);
  return { capture, context, source, ok, node: FakeWorkletNode.instances[0] };
}

function openOn(capture: LiveCapture) {
  return capture.open({ config: LIVE, sessionId: SID, recorderStartedAt: Date.now(), language: 'auto', location: HERE });
}

describe('the capture on the meter’s context', () => {
  it('loads the worklet by URL and wires source → worklet → silent gain → destination', async () => {
    const { ok, context, source, node } = await attached();
    expect(ok).toBe(true);
    expect(context.audioWorklet!.urls).toEqual(['/voice/pcm-capture-worklet.js']);
    expect(node!.name).toBe('techsara-pcm-capture');
    expect(node!.options).toMatchObject({ numberOfInputs: 1, channelCount: 1, channelCountMode: 'explicit' });
    expect(source.connections).toEqual([node]);
    expect(node!.connections).toEqual([context.gains[0]]);
    expect(context.gains[0]!.gain.value).toBe(0);
    expect(context.gains[0]!.connections).toEqual([context.destination]);
  });

  it('needs an AudioWorklet, AudioWorkletNode and WebSocket, and says so without touching the graph', async () => {
    const context = new FakeLiveAudioContext();
    const deps = { WebSocketImpl: WS, AudioWorkletNodeImpl: FakeWorkletNode as unknown as typeof AudioWorkletNode };
    expect(LiveCapture.supported(context as unknown as BaseAudioContext, deps)).toBe(true);
    expect(LiveCapture.supported(context as unknown as BaseAudioContext, { ...deps, WebSocketImpl: 'x' as unknown as typeof WebSocket })).toBe(false);
    FakeLiveAudioContext.withWorklet = false;
    const old = new FakeLiveAudioContext();
    expect(LiveCapture.supported(old as unknown as BaseAudioContext, deps)).toBe(false);
    expect(LiveCapture.supported(null, deps)).toBe(false);
  });

  it('is simply off when the module cannot load (a 404, a CSP refusal)', async () => {
    FakeLiveAudioContext.addModule = async () => {
      throw new DOMException('AbortError');
    };
    const { ok, node } = await attached();
    expect(ok).toBe(false);
    expect(node).toBeUndefined();
  });

  it('keeps the words spoken before the session exists and replays them from sample 0', async () => {
    const { capture, node } = await attached();
    node!.started(44100);
    node!.frames(0, 10);
    expect(FakeWebSocket.instances).toHaveLength(0);
    expect(openOn(capture)).toBe(true);
    const ws = FakeWebSocket.last;
    expect(ws.url).toBe(`ws://host.test/api/audio/sessions/${SID}/live`);
    ws.handshake();
    expect(ws.pcm).toEqual([...samplesAt(0, 6400)]);
  });

  it('refuses a config whose socket the relay does not own, and lets the audio go', async () => {
    const { capture, node } = await attached();
    node!.frames(0, 2);
    const odd = parseLiveConfig({ path: '/elsewhere/{id}' })!;
    expect(capture.open({ config: odd, sessionId: SID, recorderStartedAt: 0, language: 'auto', location: HERE })).toBe(false);
    expect(FakeWebSocket.instances).toHaveLength(0);
    expect(node!.port.posted.map((m) => m.type)).toEqual(['stop']);
    expect(capture.ring.read(0, 640)).toBeNull();
  });

  it('at Stop asks the worklet for its last frame, then gives the socket the rest and its flush, then lets the context go', async () => {
    const { capture, node } = await attached();
    node!.frames(0, 3);
    openOn(capture);
    FakeWebSocket.last.handshake();
    log.length = 0;
    const { tapDone } = capture.finish();
    let released = false;
    void tapDone.then(() => {
      released = true;
      log.push('tapDone');
    });
    await vi.advanceTimersByTimeAsync(0);
    expect(log).toEqual(['worklet:flush']);
    expect(released).toBe(false);
    node!.frame(1920, 200); // the partial last frame
    node!.flushed(2120);
    await vi.advanceTimersByTimeAsync(0);
    expect(log).toEqual(['worklet:flush', 'worklet:stop', 'ws:flush', 'tapDone']);
    expect(FakeWebSocket.last.pcm).toEqual([...samplesAt(0, 2120)]);
    // Anything the worklet posts after its last frame is not this recording's.
    node!.frame(2120);
    expect(FakeWebSocket.last.pcm).toHaveLength(2120);
  });

  it('lets the context go after 300 ms if the worklet never answers the flush', async () => {
    const { capture, node } = await attached();
    node!.frames(0, 1);
    openOn(capture);
    FakeWebSocket.last.handshake();
    let released = false;
    void capture.finish().tapDone.then(() => (released = true));
    await vi.advanceTimersByTimeAsync(299);
    expect(released).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(released).toBe(true);
    expect(FakeWebSocket.last.texts.map((t) => t.type)).toEqual(['start', 'flush']);
  });

  it('never wires a worklet whose module finished loading after Stop', async () => {
    let loaded: () => void = () => undefined;
    FakeLiveAudioContext.addModule = () => new Promise<void>((resolve) => (loaded = resolve));
    const context = new FakeLiveAudioContext();
    const source = context.createMediaStreamSource({} as MediaStream);
    const capture = newCapture();
    const pending = capture.attach(context as unknown as BaseAudioContext, source as unknown as AudioNode);
    capture.finish();
    loaded();
    expect(await pending).toBe(false);
    expect(FakeWorkletNode.instances).toHaveLength(0);
    expect(source.connections).toEqual([]);
  });

  it('on cancel closes the socket at once with 1000 and takes the worklet out', async () => {
    const { capture, node } = await attached();
    node!.frames(0, 2);
    openOn(capture);
    FakeWebSocket.last.handshake();
    capture.abort();
    expect(FakeWebSocket.last.closedWith).toBe(1000);
    expect(node!.port.posted.map((m) => m.type)).toEqual(['stop']);
    expect(node!.disconnected).toBe(true);
    expect(node!.port.closed).toBe(true);
    expect(node!.port.onmessage).toBeNull();
    expect(capture.text()).toBe('');
  });

  it('redraws at most ten times a second, and the newest words always land', async () => {
    const views: Array<LiveView | null> = [];
    const { capture, node } = await attached((view) => views.push(view));
    node!.frames(0, 1);
    openOn(capture);
    const ws = FakeWebSocket.last;
    ws.handshake();
    // A partial every 10 ms for a second: a replay after an outage runs faster than speech.
    for (let k = 1; k <= 100; k += 1) {
      ws.say({ type: 'partial', u: 0, text: `word ${k}`, start_sample: 0, end_sample: 640 });
      vi.advanceTimersByTime(10);
    }
    vi.advanceTimersByTime(200);
    expect(views.length).toBeGreaterThanOrEqual(9);
    expect(views.length).toBeLessThanOrEqual(11);
    expect(views[views.length - 1]!.partial!.text).toBe('word 100');
  });

  it('draws nothing until there are words, and the words it has once there are', async () => {
    const { capture, node } = await attached();
    expect(capture.view()).toBeNull();
    node!.frames(0, 1);
    openOn(capture);
    expect(capture.view()).toBeNull();
    FakeWebSocket.last.handshake();
    FakeWebSocket.last.say({ type: 'final', u: 0, text: 'Hi there.', start_sample: 0, end_sample: 640 });
    const view = capture.view()!;
    expect(view.utterances.map((u) => u.text)).toEqual(['Hi there.']);
    expect(view.active).toBe(true);
    // A snapshot: a later final does not change a view already drawn.
    FakeWebSocket.last.say({ type: 'final', u: 1, text: 'More.', start_sample: 640, end_sample: 1280 });
    expect(view.utterances).toHaveLength(1);
    expect(capture.text()).toBe('Hi there. More.');
  });
});
