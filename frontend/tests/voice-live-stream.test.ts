/**
 * The live stream's client (lib/voiceLive.ts LiveStream and LiveCapture)
 * against a WebSocket whose server side the test plays
 * (tests/voice-live-fakes.ts), on fake timers.
 *
 * The protocol is the build spec's section 2, and each block below holds one
 * promise of it:
 *   - `start` first, with the whole contract, then binary 40 ms frames only
 *     after `ready`, replayed from sample 0 so the opening words are heard;
 *   - a drop reconnects on 0.5, 1, 2, 4, 8, then 15 s (±20%), starting over
 *     only once a connection brought words back or stayed up 10 s, resumes
 *     where the last final ended with the numbering carried on, and never
 *     retries a refusal retrying cannot fix;
 *   - the resume point keeps 15 s of the ring behind it, so the frames that
 *     arrive during the handshake never overwrite it (the review's endless
 *     reconnect, 2026-09-30), and an overwrite that happens anyway backs off;
 *   - whether the live words are the whole recording (done, no gap, nothing
 *     skipped), and a language chosen mid-recording (a new stream from the
 *     last committed word, once the choice has stood for 500 ms), and whether
 *     the English-only model wrote any of them, by the model each connection
 *     runs on: `ready.language` when the server says it, else the start's;
 *   - backpressure at 256 KiB queued, released below 64 KiB, and a ring that
 *     overflowed meanwhile starts again after the backoff;
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
  LIVE_LANGUAGE_DEBOUNCE_MS,
  LIVE_RESUME_HEADROOM_SECONDS,
  LIVE_RING_SECONDS,
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
  // Until 2026-09-30 this ended "starts over once one is ready": `ready`
  // alone reset the backoff, which let a stream refused right after every
  // ready reconnect twice a second for the whole recording (review finding 5,
  // held by 'backs off to 15 s when every stream fails right after ready').
  it('reconnects after 0.5, 1, 2, 4, 8 and then every 15 s, and starts over once one brought words back', () => {
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
    FakeWebSocket.last.say({ type: 'partial', u: 0, text: 'hello', start_sample: 0, end_sample: 640 });
    const before = FakeWebSocket.instances.length;
    FakeWebSocket.last.hangUp(1006);
    vi.advanceTimersByTime(500);
    expect(FakeWebSocket.instances.length).toBe(before + 1);
  });

  it('backs off to 15 s when every stream fails right after ready (the review’s flapping engine)', () => {
    const { push } = setup();
    push(0);
    for (const wait of [500, 1000, 2000, 4000, 8000, 15_000, 15_000]) {
      const ws = FakeWebSocket.last;
      ws.handshake();
      ws.say({ type: 'error', code: 'engine_unavailable', message: 'x', retryable: true });
      ws.hangUp(4503);
      const before = FakeWebSocket.instances.length;
      vi.advanceTimersByTime(wait - 1);
      expect(FakeWebSocket.instances.length).toBe(before);
      vi.advanceTimersByTime(1);
      expect(FakeWebSocket.instances.length).toBe(before + 1);
    }
  });

  it('starts the backoff over after a connection stayed up 10 s, and not after 9 s', () => {
    const run = (upMs: number) => {
      resetLiveFakes();
      const { push } = setup();
      push(0);
      FakeWebSocket.last.hangUp(1006); // attempt 1: 0.5 s
      vi.advanceTimersByTime(500);
      FakeWebSocket.last.hangUp(1006); // attempt 2: 1 s
      vi.advanceTimersByTime(1000);
      FakeWebSocket.last.handshake();
      vi.advanceTimersByTime(upMs); // no words: silence, or a speaker thinking
      const before = FakeWebSocket.instances.length;
      FakeWebSocket.last.hangUp(1006);
      vi.advanceTimersByTime(500);
      return FakeWebSocket.instances.length - before;
    };
    expect(run(10_000)).toBe(1); // proven: 0.5 s again
    expect(run(9_000)).toBe(0); // not yet: the third wait, 2 s
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

  // Until 2026-09-30 a reconnect resumed from the ring's OLDEST sample here,
  // which the next frame overwrites before `ready` (review finding 1): the
  // resume point now keeps LIVE_RESUME_HEADROOM_SECONDS of the ring behind it.
  it('resumes a minute back when the last final is older than that, with the headroom behind it', () => {
    const { push, ring } = setup({ ring: new PcmRing(LIVE_RING_SECONDS) });
    push(0, 2000); // 80 s, no final: the 75 s ring has dropped the first 5 s
    FakeWebSocket.last.hangUp(1006);
    vi.advanceTimersByTime(500);
    const resume = FakeWebSocket.last.handshake().resume_from_sample as number;
    expect(resume).toBe(80 * 16_000 - 60 * 16_000);
    expect(resume - ring.start).toBe(LIVE_RESUME_HEADROOM_SECONDS * 16_000);
    expectPcmFrom(FakeWebSocket.last, resume, 60 * 16_000);
  });

  it('keeps a quarter of a ring shorter than the headroom behind the resume point', () => {
    const { push } = setup({ ring: new PcmRing(1) });
    push(0, 75); // 3 s into a 1 s ring: it holds [32,000, 48,000)
    FakeWebSocket.last.hangUp(1006);
    vi.advanceTimersByTime(500);
    expect(FakeWebSocket.last.handshake().resume_from_sample).toBe(32_000 + 4_000);
  });

  it('never resumes further back than resume_max_s', () => {
    const { push } = setup({ ring: new PcmRing(LIVE_RING_SECONDS), resumeMaxS: 20 });
    push(0, 1000); // 40 s
    FakeWebSocket.last.hangUp(1006);
    vi.advanceTimersByTime(500);
    expect(FakeWebSocket.last.handshake().resume_from_sample).toBe(40 * 16_000 - 20 * 16_000);
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

/** Every sample `ws` sent is the next of `length` samples from `from` (a loop: toEqual on a minute of PCM is slow). */
function expectPcmFrom(ws: FakeWebSocket, from: number, length: number) {
  const pcm = ws.pcm;
  expect(pcm.length).toBe(length);
  for (let i = 0; i < length; i += 1) {
    if (pcm[i] !== (from + i) % 30000) throw new Error(`sample ${from + i} was sent as ${pcm[i]}`);
  }
}

describe('a reconnect whose resume point the handshake could overwrite (review, 2026-09-30)', () => {
  /**
   * The review's shape: the speaker says something at 2 s, then thinks for a
   * minute and a half with no final, and the network blips. The reconnect's
   * resume point is chosen when its socket opens, and the microphone keeps
   * adding 40 ms frames until the server's `ready`.
   */
  function blipAMinuteAfterTheLastFinal(ring: PcmRing) {
    const s = setup({ ring, resumeMaxS: 60 });
    let next = 0;
    const more = (count: number) => {
      s.push(next, count);
      next += count * 640;
    };
    more(1);
    FakeWebSocket.last.handshake();
    more(50);
    FakeWebSocket.last.say({ type: 'final', u: 0, text: 'hello there', start_sample: 0, end_sample: 32_000 });
    more(2200); // 88 s more, and no final
    FakeWebSocket.last.hangUp(1006);
    vi.advanceTimersByTime(500);
    return { ...s, more };
  }

  it('streams again, on one socket, in the review’s 20-round probe (a 60 s ring, resume_max_s 60)', () => {
    const { stream, more } = blipAMinuteAfterTheLastFinal(new PcmRing(60));
    const sockets = FakeWebSocket.instances.length;
    let audioSent = 0;
    for (let round = 0; round < 20; round += 1) {
      const ws = FakeWebSocket.last;
      ws.accept(); // `start` goes out with the resume point
      const start = ws.texts[0]!;
      more(1); // 40 ms later a frame lands, before `ready`
      ws.say({ type: 'ready', v: 1, resume_from_sample: start.resume_from_sample, next_u: start.next_u });
      audioSent += ws.frames.length;
      if (ws.frames.length > 0) break;
    }
    // Before the fix: 21 sockets, 0 frames, 20 gaps.
    expect(audioSent).toBeGreaterThan(0);
    expect(FakeWebSocket.instances.length).toBe(sockets);
    expect(stream.gaps).toBe(0);
  });

  it('replays a full minute from the production ring, with the headroom behind it', () => {
    const { ring, stream, more } = blipAMinuteAfterTheLastFinal(new PcmRing(LIVE_RING_SECONDS));
    const ws = FakeWebSocket.last;
    ws.accept();
    const start = ws.texts[0]!;
    const resume = start.resume_from_sample as number;
    more(1);
    ws.say({ type: 'ready', v: 1, resume_from_sample: resume, next_u: start.next_u });
    expect(resume).toBe(2251 * 640 - 60 * 16_000);
    expectPcmFrom(ws, resume, ring.end - resume);
    expect(FakeWebSocket.instances).toHaveLength(2);
    expect(stream.gaps).toBe(0);
  });

  it('backs off, never reconnecting at once, when a handshake outlasts the headroom anyway', () => {
    const { push, stream } = setup({ ring: new PcmRing(1) }); // headroom: a quarter second
    push(0, 75);
    FakeWebSocket.last.hangUp(1006); // never ready: the first wait, 0.5 s
    vi.advanceTimersByTime(500);
    let next = 75 * 640;
    for (const wait of [1000, 2000]) {
      const ws = FakeWebSocket.last;
      ws.accept();
      const start = ws.texts[0]!;
      push(next, 10); // 0.4 s of frames before ready: past the headroom
      next += 10 * 640;
      ws.say({ type: 'ready', v: 1, resume_from_sample: start.resume_from_sample, next_u: start.next_u });
      expect(ws.frames).toHaveLength(0);
      expect(ws.closedWith).toBe(1000);
      expect(FakeWebSocket.last).toBe(ws); // not at once
      vi.advanceTimersByTime(wait - 1);
      expect(FakeWebSocket.last).toBe(ws);
      vi.advanceTimersByTime(1);
      expect(FakeWebSocket.last).not.toBe(ws);
    }
    expect(stream.gaps).toBe(2);
  });
});

describe('whether the live transcript is the whole recording', () => {
  async function finishWithDone(stream: LiveStream) {
    void stream.finish();
    FakeWebSocket.last.say({ type: 'done' });
    await vi.advanceTimersByTimeAsync(0);
  }

  it('is, once done came after the flush, with no gap and nothing skipped', async () => {
    const { push, stream } = setup();
    push(0, 50);
    FakeWebSocket.last.handshake();
    FakeWebSocket.last.say({ type: 'final', u: 0, text: 'All of it.', start_sample: 0, end_sample: 32_000 });
    expect(stream.complete).toBe(false); // not before the end
    await finishWithDone(stream);
    expect(stream.end).toBe('done');
    expect(stream.complete).toBe(true);
  });

  it('still is after a reconnect that resumed where the last final ended', async () => {
    const { push, stream } = setup();
    push(0, 100);
    FakeWebSocket.last.handshake();
    FakeWebSocket.last.say({ type: 'final', u: 0, text: 'One.', start_sample: 0, end_sample: 32_000 });
    FakeWebSocket.last.hangUp(1006);
    vi.advanceTimersByTime(500);
    expect(FakeWebSocket.last.handshake().resume_from_sample).toBe(32_000);
    await finishWithDone(stream);
    expect(stream.complete).toBe(true);
  });

  it('is not when a reconnect had to start after audio no final covered', async () => {
    const { push, stream } = setup({ ring: new PcmRing(LIVE_RING_SECONDS) });
    push(0);
    FakeWebSocket.last.handshake();
    FakeWebSocket.last.say({ type: 'final', u: 0, text: 'Early words.', start_sample: 0, end_sample: 16_000 });
    push(640, 1999); // 80 s in all, nothing final after the first second
    FakeWebSocket.last.hangUp(1006);
    vi.advanceTimersByTime(500);
    expect(FakeWebSocket.last.handshake().resume_from_sample).toBe(20 * 16_000);
    await finishWithDone(stream);
    expect(stream.end).toBe('done');
    expect(stream.complete).toBe(false);
  });

  it('is not when the ring lost audio before it could be sent', async () => {
    const { push, stream } = setup({ ring: new PcmRing(1) });
    push(0);
    FakeWebSocket.last.bufferedAmount = 300 * 1024;
    FakeWebSocket.last.holdBuffer = true;
    FakeWebSocket.last.handshake();
    push(640, 49);
    vi.advanceTimersByTime(500);
    FakeWebSocket.last.handshake();
    await finishWithDone(stream);
    expect(stream.gaps).toBe(1);
    expect(stream.complete).toBe(false);
  });

  it('is not when refused for good, nor when done never came', async () => {
    const refused = setup();
    refused.push(0);
    FakeWebSocket.last.handshake();
    FakeWebSocket.last.say({ type: 'final', u: 0, text: 'Heard.', start_sample: 0, end_sample: 640 });
    FakeWebSocket.last.say({ type: 'error', code: 'session_closed', message: 'x', retryable: false });
    FakeWebSocket.last.hangUp(4404);
    expect(refused.stream.end).toBe('refused');
    expect(refused.stream.complete).toBe(false);

    resetLiveFakes();
    const late = setup();
    late.push(0);
    FakeWebSocket.last.handshake();
    void late.stream.finish();
    await vi.advanceTimersByTimeAsync(LIVE_FINISH_BUDGET_MS);
    expect(late.stream.end).toBe('timeout');
    expect(late.stream.complete).toBe(false);
  });
});

describe('a language chosen mid-recording', () => {
  // Until 2026-09-30 a choice replaced the connection at once; since the
  // review's finding 4 it does so once the choice has stood for 500 ms.
  it('starts again in the new language once the choice has stood for 500 ms, from the last committed word, losing and repeating nothing', async () => {
    const { push, stream, transcript } = setup({ language: 'auto' });
    push(0, 100); // 4 s
    const first = FakeWebSocket.last;
    first.handshake();
    first.say({ type: 'final', u: 0, text: 'Hello everyone.', start_sample: 0, end_sample: 32_000 });
    first.say({ type: 'partial', u: 1, text: 'aaj hum', start_sample: 33_000, end_sample: 60_000 });
    stream.setLanguage('hi');
    expect(stream.currentLanguage).toBe('hi');
    vi.advanceTimersByTime(LIVE_LANGUAGE_DEBOUNCE_MS - 1);
    expect(first.closedWith).toBeNull();
    expect(FakeWebSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(1);
    expect(first.closedWith).toBe(1000);
    expect(FakeWebSocket.instances).toHaveLength(2); // no backoff for a choice
    const second = FakeWebSocket.last;
    expect(second.handshake()).toMatchObject({ language: 'hi', resume_from_sample: 32_000, next_u: 1 });
    // The utterance under way is heard again, in Hindi; the committed one is not sent again.
    expectPcmFrom(second, 32_000, 64_000 - 32_000);
    // Whatever the old connection still says is not this stream's any more.
    first.say({ type: 'final', u: 1, text: 'aaj hum', start_sample: 33_000, end_sample: 60_000 });
    expect(transcript.text()).toBe('Hello everyone. aaj hum');
    second.say({ type: 'partial', u: 1, text: 'आज हम', start_sample: 33_000, end_sample: 60_000 });
    expect(transcript.text()).toBe('Hello everyone. आज हम');
    second.say({ type: 'final', u: 1, text: 'आज हम बात करेंगे।', start_sample: 33_000, end_sample: 64_000 });
    expect(transcript.committed.map((u) => u.text)).toEqual(['Hello everyone.', 'आज हम बात करेंगे।']);
    void stream.finish();
    second.say({ type: 'done' });
    await vi.advanceTimersByTimeAsync(0);
    expect(stream.complete).toBe(true);
    expect(stream.currentLanguage).toBe('hi');
  });

  it('is said by the next attempt when a reconnect is waiting out its backoff, and not sooner', () => {
    const { push, stream } = setup();
    push(0);
    FakeWebSocket.last.hangUp(1006);
    stream.setLanguage('en');
    expect(FakeWebSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(500);
    expect(FakeWebSocket.last.handshake().language).toBe('en');
    // The debounce that ends meanwhile finds the connection already in English.
    vi.advanceTimersByTime(LIVE_LANGUAGE_DEBOUNCE_MS);
    expect(FakeWebSocket.instances).toHaveLength(2);
  });

  it('changes nothing for the language already in use, or after Stop', () => {
    const { push, stream } = setup({ language: 'en' });
    push(0);
    FakeWebSocket.last.handshake();
    stream.setLanguage('en');
    vi.advanceTimersByTime(LIVE_LANGUAGE_DEBOUNCE_MS);
    expect(FakeWebSocket.instances).toHaveLength(1);
    void stream.finish();
    stream.setLanguage('hi');
    vi.advanceTimersByTime(LIVE_LANGUAGE_DEBOUNCE_MS);
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(FakeWebSocket.last.closedWith).toBeNull();
    expect(stream.currentLanguage).toBe('en');
  });

  // The review's finding 4: native radios walk auto -> en -> hi -> auto with a
  // key held down; 30 changes in a second opened 30 sockets, each counted
  // against the gateway's 30 connections a minute.
  it('opens one connection for a burst of 30 changes in a second, in the language the burst ended on', () => {
    const { push, stream } = setup({ language: 'auto' });
    push(0, 25);
    const first = FakeWebSocket.last;
    first.handshake();
    // Every press a real change: hi, auto, en, hi, ... ending on en (k = 29).
    const cycle = ['hi', 'auto', 'en'] as const;
    for (let k = 0; k < 30; k += 1) {
      stream.setLanguage(cycle[k % 3]!);
      vi.advanceTimersByTime(33);
    }
    expect(FakeWebSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(LIVE_LANGUAGE_DEBOUNCE_MS);
    expect(FakeWebSocket.instances).toHaveLength(2);
    expect(first.closedWith).toBe(1000);
    expect(FakeWebSocket.last.handshake().language).toBe('en');
    vi.advanceTimersByTime(10_000);
    expect(FakeWebSocket.instances).toHaveLength(2);
  });

  it('opens none when the choice comes back to the language in use within the 500 ms', () => {
    const { push, stream } = setup({ language: 'auto' });
    push(0, 25);
    const first = FakeWebSocket.last;
    first.handshake();
    stream.setLanguage('en');
    vi.advanceTimersByTime(300);
    stream.setLanguage('auto');
    vi.advanceTimersByTime(5 * LIVE_LANGUAGE_DEBOUNCE_MS);
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(first.closedWith).toBeNull();
    expect(stream.currentLanguage).toBe('auto');
  });

  it('drops a change still inside the 500 ms at Stop: the last words are heard in the language the open connection asked for', async () => {
    const { push, stream } = setup({ language: 'auto' });
    push(0, 25);
    const ws = FakeWebSocket.last;
    ws.handshake();
    stream.setLanguage('en');
    vi.advanceTimersByTime(200);
    let finished = false;
    void stream.finish().then(() => (finished = true));
    vi.advanceTimersByTime(LIVE_LANGUAGE_DEBOUNCE_MS);
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(ws.texts.map((t) => t.type)).toEqual(['start', 'flush']);
    // The final-text policy reads the language the words were heard in.
    expect(stream.currentLanguage).toBe('auto');
    ws.say({ type: 'done' });
    await vi.advanceTimersByTimeAsync(0);
    expect(finished).toBe(true);
    expect(stream.complete).toBe(true);
  });

  it('drops it on cancel too', () => {
    const { push, stream } = setup({ language: 'auto' });
    push(0, 25);
    FakeWebSocket.last.handshake();
    stream.setLanguage('hi');
    stream.abort();
    vi.advanceTimersByTime(5 * LIVE_LANGUAGE_DEBOUNCE_MS);
    expect(FakeWebSocket.instances).toHaveLength(1);
    expect(FakeWebSocket.last.closedWith).toBe(1000);
  });
});

describe('which model wrote the live words', () => {
  it('knows a final with words came from the English-only model when its connection asked for "en"', () => {
    const { push, stream } = setup({ language: 'en' });
    push(0, 25);
    const ws = FakeWebSocket.last;
    ws.handshake();
    ws.say({ type: 'partial', u: 0, text: 'May aaj', start_sample: 0, end_sample: 4_000 });
    expect(stream.englishModelFinals).toBe(false); // a partial commits nothing
    ws.say({ type: 'final', u: 0, text: '  ', start_sample: 0, end_sample: 4_000 });
    expect(stream.englishModelFinals).toBe(false); // nor does a final with no words
    ws.say({ type: 'final', u: 1, text: 'May aaj of his jar a who.', start_sample: 4_000, end_sample: 8_000 });
    expect(stream.englishModelFinals).toBe(true);
  });

  it('keeps knowing it after a switch from English to Hindi (the review’s finding 3)', () => {
    const { push, stream, transcript } = setup({ language: 'en' });
    push(0, 25);
    const first = FakeWebSocket.last;
    first.handshake();
    first.say({ type: 'final', u: 0, text: 'May aaj of his jar a who.', start_sample: 0, end_sample: 8_000 });
    stream.setLanguage('hi');
    vi.advanceTimersByTime(LIVE_LANGUAGE_DEBOUNCE_MS);
    const second = FakeWebSocket.last;
    expect(second.handshake()).toMatchObject({ language: 'hi', resume_from_sample: 8_000, next_u: 1 });
    second.say({ type: 'final', u: 1, text: 'मीटिंग दस बजे है', start_sample: 8_000, end_sample: 16_000 });
    // The English model's attempt at the opening is still the start of the text.
    expect(transcript.text()).toBe('May aaj of his jar a who. मीटिंग दस बजे है');
    expect(stream.englishModelFinals).toBe(true);
  });

  it('goes by the connection that sent the final, not by the choice still inside its 500 ms', () => {
    const toHindi = setup({ language: 'en' });
    toHindi.push(0, 25);
    FakeWebSocket.last.handshake();
    toHindi.stream.setLanguage('hi');
    // The English connection is still the one in use: its final is the English model's.
    FakeWebSocket.last.say({ type: 'final', u: 0, text: 'Hello.', start_sample: 0, end_sample: 8_000 });
    expect(toHindi.stream.englishModelFinals).toBe(true);

    resetLiveFakes();
    const toEnglish = setup({ language: 'hi' });
    toEnglish.push(0, 25);
    FakeWebSocket.last.handshake();
    toEnglish.stream.setLanguage('en');
    FakeWebSocket.last.say({ type: 'final', u: 0, text: 'नमस्ते', start_sample: 0, end_sample: 8_000 });
    expect(toEnglish.stream.englishModelFinals).toBe(false);
    vi.advanceTimersByTime(LIVE_LANGUAGE_DEBOUNCE_MS);
    FakeWebSocket.last.handshake();
    FakeWebSocket.last.say({ type: 'final', u: 1, text: 'Thank you.', start_sample: 8_000, end_sample: 16_000 });
    expect(toEnglish.stream.englishModelFinals).toBe(true);
  });

  it('is never set by Auto or Hindi, nor by a late or repeated final', () => {
    const { push, stream } = setup({ language: 'auto' });
    push(0, 25);
    const ws = FakeWebSocket.last;
    ws.handshake();
    ws.say({ type: 'final', u: 0, text: 'Hello there.', start_sample: 0, end_sample: 8_000 });
    ws.say({ type: 'final', u: 0, text: 'Hello there.', start_sample: 0, end_sample: 8_000 });
    expect(stream.englishModelFinals).toBe(false);
  });
});

// Build spec 14.2 (2026-09-30): release 2's gateway may put an "auto" stream
// on the English-only model (for people whose recent dictations were all
// English) and says which in `ready.language`; release 1 says nothing.
describe('the model each connection runs on', () => {
  it('is the one ready names, else the one the start asked for; finals on English are the English model’s', () => {
    // Auto, which the server put on the English model.
    const routed = setup({ language: 'auto' });
    routed.push(0, 25);
    FakeWebSocket.last.handshake({ language: 'en' });
    expect(routed.stream.hearingLanguage).toBe('en');
    FakeWebSocket.last.say({ type: 'final', u: 0, text: 'May aaj of his jar a who.', start_sample: 0, end_sample: 8_000 });
    expect(routed.stream.englishModelFinals).toBe(true);

    // No language in ready (release 1): the start's, exactly as before.
    resetLiveFakes();
    const plain = setup({ language: 'en' });
    plain.push(0, 25);
    FakeWebSocket.last.handshake();
    expect(plain.stream.hearingLanguage).toBe('en');
    FakeWebSocket.last.say({ type: 'final', u: 0, text: 'Hello.', start_sample: 0, end_sample: 8_000 });
    expect(plain.stream.englishModelFinals).toBe(true);

    resetLiveFakes();
    const auto = setup({ language: 'auto' });
    auto.push(0, 25);
    FakeWebSocket.last.handshake();
    FakeWebSocket.last.say({ type: 'final', u: 0, text: 'Hello.', start_sample: 0, end_sample: 8_000 });
    expect(auto.stream.hearingLanguage).toBe('auto');
    expect(auto.stream.englishModelFinals).toBe(false);
  });

  it('believes the server over the start, either way, and reads its word loosely', () => {
    const toHindi = setup({ language: 'en' });
    toHindi.push(0, 25);
    FakeWebSocket.last.handshake({ language: 'hi' });
    FakeWebSocket.last.say({ type: 'final', u: 0, text: 'नमस्ते', start_sample: 0, end_sample: 8_000 });
    expect(toHindi.stream.hearingLanguage).toBe('hi');
    expect(toHindi.stream.englishModelFinals).toBe(false);

    resetLiveFakes();
    const shouted = setup({ language: 'auto' });
    shouted.push(0, 25);
    FakeWebSocket.last.handshake({ language: ' EN ' });
    FakeWebSocket.last.say({ type: 'final', u: 0, text: 'Hello.', start_sample: 0, end_sample: 8_000 });
    expect(shouted.stream.englishModelFinals).toBe(true);

    // Not a language at all, or a blank one: the start's language stands.
    for (const odd of [42, null, '', '   ', { code: 'en' }]) {
      resetLiveFakes();
      const each = setup({ language: 'auto' });
      each.push(0, 25);
      FakeWebSocket.last.handshake({ language: odd });
      FakeWebSocket.last.say({ type: 'final', u: 0, text: 'Hello.', start_sample: 0, end_sample: 8_000 });
      expect(each.stream.hearingLanguage).toBe('auto');
      expect(each.stream.englishModelFinals).toBe(false);
    }
  });

  it('is each connection’s own: English-model words stay counted when a later one runs on another model', () => {
    const { push, stream, transcript } = setup({ language: 'auto' });
    push(0, 50);
    const first = FakeWebSocket.last;
    first.handshake({ language: 'en' });
    first.say({ type: 'final', u: 0, text: 'May aaj.', start_sample: 0, end_sample: 8_000 });
    first.hangUp(1006);
    vi.advanceTimersByTime(600);
    const second = FakeWebSocket.last;
    expect(second).not.toBe(first);
    // The same choice, put on the multilingual model this time.
    expect(second.handshake({ language: 'auto' })).toMatchObject({ language: 'auto', resume_from_sample: 8_000 });
    expect(stream.hearingLanguage).toBe('auto');
    second.say({ type: 'final', u: 1, text: 'मीटिंग दस बजे है', start_sample: 8_000, end_sample: 16_000 });
    expect(transcript.text()).toBe('May aaj. मीटिंग दस बजे है');
    expect(stream.englishModelFinals).toBe(true);
  });

  it('is the choice while a change of it is waiting, and before a connection has said anything', () => {
    const { push, stream } = setup({ language: 'auto' });
    expect(stream.hearingLanguage).toBe('auto'); // no connection yet
    push(0, 25);
    const first = FakeWebSocket.last;
    first.handshake({ language: 'en' });
    expect(stream.hearingLanguage).toBe('en');
    stream.setLanguage('hi');
    expect(stream.hearingLanguage).toBe('hi'); // the next start asks for it
    vi.advanceTimersByTime(LIVE_LANGUAGE_DEBOUNCE_MS);
    const second = FakeWebSocket.last;
    expect(stream.hearingLanguage).toBe('hi'); // connecting
    second.handshake();
    expect(stream.hearingLanguage).toBe('hi');
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

  // Until 2026-09-30 the new connection was opened at once; an overwrite that
  // recurs on every handshake then reconnected in a tight loop (review finding
  // 1), so it goes through the backoff now, and the resume point keeps its
  // headroom (a quarter of this 1 s ring).
  it('starts again after the backoff when the ring overflowed meanwhile, and counts the gap', () => {
    const { push, stream } = setup({ ring: new PcmRing(1) });
    push(0);
    const first = FakeWebSocket.last;
    first.holdBuffer = true;
    first.bufferedAmount = 300 * 1024; // the uplink is stuck
    first.handshake();
    expect(first.frames).toHaveLength(0);
    push(640, 49); // 2 s more into a 1 s ring: it holds [16,000, 32,000)
    expect(stream.gaps).toBe(1);
    expect(first.closedWith).toBe(1000);
    expect(FakeWebSocket.instances).toHaveLength(1);
    vi.advanceTimersByTime(500);
    const second = FakeWebSocket.last;
    expect(second).not.toBe(first);
    expect(second.handshake().resume_from_sample).toBe(32_000 - 16_000 + 4_000);
    expect(second.pcm).toEqual([...samplesAt(20_000, 12_000)]);
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

  it('keeps the handshake’s headroom in the ring it builds itself (the production wiring)', async () => {
    const { capture, node } = await attached();
    node!.started(48000);
    node!.frames(0, 1);
    openOn(capture); // resume_max_s 60, as the orchestrator sends it
    FakeWebSocket.last.handshake();
    node!.frames(640, 50);
    FakeWebSocket.last.say({ type: 'final', u: 0, text: 'hello there', start_sample: 0, end_sample: 32_000 });
    node!.frames(51 * 640, 2200);
    FakeWebSocket.last.hangUp(1006);
    vi.advanceTimersByTime(500);
    const ws = FakeWebSocket.last;
    ws.accept();
    const start = ws.texts[0]!;
    node!.frame(2251 * 640); // before ready
    ws.say({ type: 'ready', v: 1, resume_from_sample: start.resume_from_sample, next_u: start.next_u });
    expect(capture.ring.capacity).toBe(LIVE_RING_SECONDS * 16_000);
    expect(ws.frames.length).toBeGreaterThan(0);
    expect(FakeWebSocket.instances).toHaveLength(2);
  });

  it('says whether its words are the whole recording, and hears the rest in another language', async () => {
    const { capture, node } = await attached();
    node!.frames(0, 25);
    expect(capture.language).toBeNull(); // no stream yet
    openOn(capture);
    expect(capture.language).toBe('auto');
    FakeWebSocket.last.handshake();
    FakeWebSocket.last.say({ type: 'final', u: 0, text: 'Hi.', start_sample: 0, end_sample: 8_000 });
    capture.setLanguage('en');
    expect(capture.language).toBe('en');
    vi.advanceTimersByTime(LIVE_LANGUAGE_DEBOUNCE_MS);
    const second = FakeWebSocket.last;
    expect(second.handshake()).toMatchObject({ language: 'en', resume_from_sample: 8_000, next_u: 1 });
    expect(capture.complete()).toBe(false); // not before its end
    const { done } = capture.finish();
    node!.flushed(16_000);
    await vi.advanceTimersByTimeAsync(0);
    second.say({ type: 'done' });
    await done;
    expect(capture.complete()).toBe(true);
    expect(capture.text()).toBe('Hi.');
    // "Hi." came from the multilingual model, before the switch.
    expect(capture.englishModelFinals()).toBe(false);
  });

  it('says when the English-only model wrote any of its words', async () => {
    const { capture, node } = await attached();
    node!.frames(0, 25);
    expect(capture.englishModelFinals()).toBe(false); // no stream yet
    capture.open({ config: LIVE, sessionId: SID, recorderStartedAt: Date.now(), language: 'en', location: HERE });
    FakeWebSocket.last.handshake();
    FakeWebSocket.last.say({ type: 'final', u: 0, text: 'Hello.', start_sample: 0, end_sample: 8_000 });
    expect(capture.englishModelFinals()).toBe(true);
  });

  // The review's finding 5: every insert waited out the whole finish budget
  // behind a stream slow to say done.
  it('stops waiting for a stream that never says done after the time it is given, and at once when it does', async () => {
    const { capture, node } = await attached();
    node!.frames(0, 25);
    openOn(capture);
    FakeWebSocket.last.handshake();
    capture.finish();
    node!.flushed(16_000);
    await vi.advanceTimersByTimeAsync(0);
    let waited = false;
    void capture.settled(1000).then(() => (waited = true));
    await vi.advanceTimersByTimeAsync(999);
    expect(waited).toBe(false);
    await vi.advanceTimersByTimeAsync(1);
    expect(waited).toBe(true);
    // The stream itself still has the rest of its budget.
    expect(capture.stream!.state).not.toBe('closed');
    let early = false;
    void capture.settled(1000).then(() => (early = true));
    await vi.advanceTimersByTimeAsync(100);
    FakeWebSocket.last.say({ type: 'done' });
    await vi.advanceTimersByTimeAsync(0);
    expect(early).toBe(true);
    expect(capture.complete()).toBe(true);
  });

  it('hears the rest in the language the server runs the stream on (spec 14.2)', async () => {
    const { capture, node } = await attached();
    node!.frames(0, 25);
    expect(capture.hearingLanguage).toBeNull(); // no stream yet
    openOn(capture);
    FakeWebSocket.last.handshake({ language: 'en' });
    expect(capture.language).toBe('auto'); // what the person chose
    expect(capture.hearingLanguage).toBe('en'); // what the server runs
    FakeWebSocket.last.say({ type: 'error', code: 'voice_off', message: 'x', retryable: false });
    FakeWebSocket.last.hangUp(4403);
    expect(capture.hearingLanguage).toBeNull();
  });

  it('has no language once refused for good, and is not complete', async () => {
    const { capture, node } = await attached();
    node!.frames(0, 2);
    openOn(capture);
    FakeWebSocket.last.handshake();
    FakeWebSocket.last.say({ type: 'error', code: 'voice_off', message: 'x', retryable: false });
    FakeWebSocket.last.hangUp(4403);
    expect(capture.language).toBeNull();
    expect(capture.complete()).toBe(false);
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
