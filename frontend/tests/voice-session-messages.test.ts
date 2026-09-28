/**
 * Every reason the recording-session contract names, and the sentence the
 * person reads for it (2026-09-29).
 *
 * THE OWNER'S SCREEN. "That recording wasn't clear enough to transcribe. Try
 * again, closer to the microphone." was shown for a 181,427 ms dictation on
 * 2026-09-24 that the speech engine never listened to past its first 30
 * seconds. Before this change lib/voice.ts showed that sentence for EVERY
 * empty transcript whether or not anyone judged the audio (measured against
 * origin/dev 2026-09-29: confidence null, 'unclear' at 181 s, and a reply
 * with no confidence at all all produced it), and one generic "Please try
 * again" for a sign-out, an unsupported format, an unreachable proxy and a
 * dropped network, none of which a retry could fix and all of which threw the
 * audio away.
 *
 * THE RULES THESE TESTS HOLD. The microphone sentence survives only on the
 * legacy road, only where the server judged the audio unclear or low. Every
 * failure on the session road says whether the recording was kept. The
 * sentences are written out here in full, from the contract, rather than read
 * back from the module: a test that compared the module with itself would
 * pass whatever the module said.
 */
import { createHash } from 'node:crypto';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import {
  DEFAULT_SESSION_CONFIG,
  VOICE_MESSAGES,
  VoiceSession,
  createMemoryOutbox,
  describeCaptureError,
  describeOutcome,
  openSession,
  parseSessionState,
  retranscribeSession,
  transcribe,
  type OutboxStore,
  type SessionResult,
} from '@/lib/voice';
import { FakeSessionServer, SESSION_ID, sliceBlob, type Injection } from './voice-session-fake';

const MICROPHONE = "That recording wasn't clear enough to transcribe. Try again, closer to the microphone.";

const turn = () => new Promise<void>((resolve) => setImmediate(resolve));

/** SHA-256 off the thread pool, so each part lands before the next slice (see voice-session-upload). */
const sha256 = async (bytes: Uint8Array) => createHash('sha256').update(bytes).digest('hex');

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['setTimeout', 'clearTimeout', 'setInterval', 'clearInterval', 'Date'] });
});
afterEach(() => {
  vi.useRealTimers();
});

function answer(status: number, body: unknown): Response {
  const text = typeof body === 'string' ? body : JSON.stringify(body);
  return {
    ok: status >= 200 && status < 300,
    status,
    headers: { get: () => null },
    text: async () => text,
    json: async () => JSON.parse(text),
  } as unknown as Response;
}

async function open(status: number, body: unknown) {
  return openSession(
    { clientKey: '0b7c5d8e-1f2a-4b3c-8d4e-5f6a7b8c9d0e', mimeType: 'audio/webm;codecs=opus' },
    { fetchImpl: vi.fn(async () => answer(status, body)) as unknown as typeof fetch },
  );
}

function state(over: Record<string, unknown> = {}) {
  const parsed = parseSessionState({
    session_id: SESSION_ID,
    status: 'done',
    rev: 9,
    audio_ms: 754_000,
    segments: [],
    gaps: [],
    outcome: 'transcribed',
    text: 'hello there',
    stored: { kept: true, retention_days: 0 },
    ...over,
  });
  if (!parsed) throw new Error('bad state');
  return parsed;
}

function messageOf(result: SessionResult): string {
  if (result.kind === 'error') return result.error.message;
  if (result.kind === 'text') return result.notices.join(' ');
  return '';
}

// ---------------------------------------------------------------------------
// Refusals when the recording is opened
// ---------------------------------------------------------------------------

describe('opening a recording', () => {
  it.each([
    [401, { detail: 'Not authenticated' }, 'You were signed out, so nothing was recorded. Sign in and try again.', false],
    [
      403,
      { detail: 'Voice input is turned off for your account. Ask an administrator.', reason: 'voice_off' },
      'Voice input is turned off for your account. Ask an administrator.',
      false,
    ],
    [404, { detail: 'off', reason: 'voice_unavailable' }, "Voice input isn't available on this server right now.", false],
    [
      415,
      { detail: 'audio/x-matroska is not a supported audio format.', reason: 'unsupported_format' },
      'audio/x-matroska is not a supported audio format. Try Chrome, Edge or Safari.',
      false,
    ],
    [429, { detail: 'slow down', reason: 'rate_limited' }, 'Too many recordings were started just now. Wait a moment and try again.', true],
    [507, { detail: 'disk', reason: 'storage_full' }, "The server has no space left for recordings, so this one didn't start.", false],
    [
      507,
      { detail: 'quota', reason: 'quota_exceeded' },
      "Your recordings have used all the space your account has, so this one didn't start. Delete some on the Recordings page (/recordings) to record again.",
      false,
    ],
  ])('%i %j reads as its own sentence', async (status, body, message, retryable) => {
    const result = await open(status, body);
    expect(result).toEqual({ kind: 'error', error: { message, retryable } });
  });

  // 2026-09-29 (fix/voice-server-hardening decision): a full server no longer
  // refuses a dictation. The legacy road has its own pool, so a short one
  // still works; the recorder says it is not stored and stops at 10 minutes.
  it('takes the ten-minute road, and says it is not stored, when the server is full', async () => {
    expect(await open(503, { detail: 'full', reason: 'capacity_full' })).toEqual({
      kind: 'legacy',
      reason: 'capacity_full',
    });
    expect(VOICE_MESSAGES.capacityLegacyHint).toBe(
      // "just before": it stops 5 s early since 2026-09-29 (item J).
      "Too many people are recording right now, so this recording isn't saved to your account and stops just before 10 minutes.",
    );
  });

  it('takes the ten-minute road, with a line saying so, when the server has no sessions', async () => {
    expect(await open(404, { detail: 'Long recordings are not enabled.', reason: 'sessions_off' })).toEqual({
      kind: 'legacy',
    });
    // An orchestrator older than the contract answers FastAPI's own 404.
    expect(await open(404, { detail: 'Not Found' })).toEqual({ kind: 'legacy' });
    // "just before": it stops 5 s early since 2026-09-29 (item J).
    expect(VOICE_MESSAGES.legacyHint).toBe("Long recordings aren't enabled here, so this one stops just before 10 minutes.");
  });

  it('offers to end a recording running elsewhere instead of opening a second', async () => {
    const result = await open(409, {
      detail: 'already recording',
      reason: 'session_active',
      session_id: SESSION_ID,
      audio_ms: 90_000,
    });
    expect(result).toEqual({ kind: 'active', sessionId: SESSION_ID, audioMs: 90_000 });
    expect(VOICE_MESSAGES.sessionActive).toBe(
      "You're already recording in another tab or on another device. End that recording first.",
    );
    expect(VOICE_MESSAGES.sessionActiveAction).toBe('End that recording');
  });

  it('tries an unreachable server three times and then says it could not reach it', async () => {
    const fetchImpl = vi.fn(async () => answer(502, { detail: 'down', reason: 'proxy_unreachable' }));
    const pending = openSession({ clientKey: 'k', mimeType: 'audio/webm' }, { fetchImpl: fetchImpl as unknown as typeof fetch });
    await vi.advanceTimersByTimeAsync(10_000);
    expect(await pending).toEqual({
      kind: 'error',
      error: { message: "Can't reach the server, so recording didn't start. Check your connection.", retryable: true },
    });
    expect(fetchImpl).toHaveBeenCalledTimes(3);
  });

  it('reads an edge error page as the network, not as the server refusing', async () => {
    const fetchImpl = vi.fn(async () => answer(524, '<html>A timeout occurred</html>'));
    const pending = openSession({ clientKey: 'k', mimeType: 'audio/webm' }, { fetchImpl: fetchImpl as unknown as typeof fetch });
    await vi.advanceTimersByTimeAsync(10_000);
    expect((await pending).kind).toBe('error');
    expect(fetchImpl).toHaveBeenCalledTimes(3);
  });

  it('keeps the four microphone-permission sentences it always had', () => {
    expect(describeCaptureError({ name: 'NotAllowedError' }).message).toBe(
      'Microphone access is blocked. Allow it for this site in your browser settings, then try again.',
    );
    expect(describeCaptureError({ name: 'NotFoundError' }).message).toBe('No microphone was found. Connect one and try again.');
    expect(describeCaptureError({ name: 'NotReadableError' }).message).toBe(
      'The microphone is in use by another application. Close it and try again.',
    );
    expect(describeCaptureError({ name: 'Other' }).message).toBe(
      'Recording could not start. Check your microphone and try again.',
    );
  });
});

// ---------------------------------------------------------------------------
// Refusals while recording: the server stops taking parts
// ---------------------------------------------------------------------------

async function interruptedAt(
  injection: Injection,
  store: OutboxStore = createMemoryOutbox(),
  extraSlicesAfter = 0,
  createRefusal?: Injection,
): Promise<{ result: SessionResult; server: FakeSessionServer }> {
  // Four parts (0:20) go up; the fifth is refused.
  const server = new FakeSessionServer({
    inject: ({ method, seq, path }) =>
      method === 'PUT' && seq === 4
        ? injection
        : method === 'POST' && path === '/api/audio/sessions'
          ? createRefusal
          : undefined,
  });
  server.status = 'recording';
  const session = new VoiceSession(
    { sessionId: SESSION_ID, mimeType: 'audio/webm', config: DEFAULT_SESSION_CONFIG },
    { fetchImpl: server.fetch as unknown as typeof fetch, store, random: () => 0.5, sha256 },
  );
  await session.open();
  for (let idx = 0; idx < 5 + extraSlicesAfter; idx += 1) {
    session.addSlice(sliceBlob(idx), (idx + 1) * 5000);
    await vi.advanceTimersByTimeAsync(5000);
    for (let t = 0; t < 30; t += 1) await turn();
  }
  const pending = session.end('person', (5 + extraSlicesAfter) * 5000);
  for (let i = 0; i < 10; i += 1) {
    await vi.advanceTimersByTimeAsync(2000);
    for (let t = 0; t < 30; t += 1) await turn();
  }
  return { result: await pending, server };
}

describe('the server stops taking parts mid-recording', () => {
  it('a sign-out keeps the unsent seconds on a device that can keep them, and says which', async () => {
    const persistent = { ...createMemoryOutbox(), persistent: true } as OutboxStore;
    const kept = await interruptedAt({ status: 401, body: { detail: 'Not authenticated' } }, persistent);
    // "…unless someone else signs in…" since 2026-09-29: the outbox of an
    // account is deleted when another account signs in on the same browser.
    expect(messageOf(kept.result)).toBe(
      'You were signed out. Everything up to 0:20 is saved on the server; the last few seconds are kept on this device and will upload when you sign in again, unless someone else signs in on this browser first.',
    );
    const lost = await interruptedAt({ status: 401, body: { detail: 'Not authenticated' } });
    expect(messageOf(lost.result)).toBe(
      'You were signed out. Everything up to 0:20 is saved on the server; the last few seconds will be lost if you close this tab.',
    );
  });

  it.each([
    [
      { status: 404, body: { detail: 'gone', reason: 'not_found' } },
      'This recording is no longer on the server. It was discarded or has expired.',
    ],
    [
      { status: 409, body: { detail: 'two writers', reason: 'part_conflict', next_part: 4 } },
      'This recording is also being uploaded from another tab, so this tab stopped. The copy on the server is kept.',
    ],
    [
      { status: 415, body: { detail: 'That is not WebM.', reason: 'unsupported_format' } },
      'That is not WebM. Try Chrome, Edge or Safari.',
    ],
  ])('%j', async (injection, message) => {
    const { result } = await interruptedAt(injection as Injection);
    expect(result.kind).toBe('error');
    expect(messageOf(result)).toBe(message);
  });

  // 2026-09-29: in each case below the fifth part (0:20-0:25) was refused
  // and is still on this device. It used to be deleted with the outbox and the
  // sentence said nothing of it; it is now KEPT, the sentence says so, and
  // "Upload the rest" is offered. (These slices are not WebM, so no new
  // session can continue them: tests/voice-session-edges covers that road.)
  const uploadRest = (result: SessionResult) =>
    result.kind === 'withdrawn' ? null : result.offer?.kind === 'upload_rest' ? result.offer.label : null;

  it('voice turned off mid-recording keeps what the server did not take', async () => {
    const { result } = await interruptedAt({
      status: 403,
      body: { detail: 'Voice input is turned off for your account. Ask an administrator.', reason: 'voice_off' },
    });
    expect(result.kind).toBe('error');
    expect(messageOf(result)).toBe(
      'Voice input is turned off for your account. Ask an administrator. The last 0:05 of this recording is kept on this device.',
    );
  });

  it('a session the server closed for silence says so, still delivers what was said, and keeps the rest', async () => {
    // An idle-closed session is continued in a new one at once
    // (voice-session-edges.test.ts); this is the sentence when that is refused.
    const { result } = await interruptedAt(
      {
        status: 409,
        body: { detail: 'closed', reason: 'session_closed', status: 'finishing', ended_by: 'idle' },
      },
      undefined,
      0,
      { status: 409, body: { detail: 'no', reason: 'not_continuable' } },
    );
    expect(result.kind).toBe('text');
    expect(messageOf(result)).toBe(
      "This recording was closed after 10 minutes with no audio arriving. Everything up to 0:20 is saved on the server. The last 0:05 is kept on this device, because the server would not take it after that. You're not being recorded now.",
    );
    expect(uploadRest(result)).toBe('Upload the rest');
  });

  it('a session ended from another tab says so, and keeps the rest', async () => {
    const { result } = await interruptedAt({
      status: 409,
      body: { detail: 'closed', reason: 'session_closed', status: 'finishing', ended_by: 'person' },
    });
    expect(messageOf(result)).toBe(
      'This recording was ended from another tab. What reached the server is saved; the last 0:05 is kept on this device, because the server would not take it after that.',
    );
    expect(uploadRest(result)).toBe('Upload the rest');
  });

  it('a full disk stops the recording where it is, transcribes everything before it, and keeps the rest', async () => {
    const { result } = await interruptedAt({ status: 507, body: { detail: 'full', reason: 'storage_full' } });
    expect(result.kind).toBe('text');
    expect(messageOf(result)).toBe(
      'The server ran out of space, so recording stopped at 0:20. Everything up to then is saved and is being transcribed; the last 0:05 is kept on this device.',
    );
    expect(uploadRest(result)).toBe('Upload the rest');
  });

  it('retries corrupt, cut-short, rate-limited and unsaveable parts silently', async () => {
    for (const status of [408, 422, 429, 500, 502, 503]) {
      let refused = false;
      const server = new FakeSessionServer({
        inject: ({ method, seq }) => {
          if (method === 'PUT' && seq === 1 && !refused) {
            refused = true;
            return { status, body: { detail: 'again', reason: 'whatever' } };
          }
          return undefined;
        },
      });
      server.status = 'recording';
      const interrupts: unknown[] = [];
      const session = new VoiceSession(
        { sessionId: SESSION_ID, mimeType: 'audio/webm', config: DEFAULT_SESSION_CONFIG },
        { fetchImpl: server.fetch as unknown as typeof fetch, store: createMemoryOutbox(), random: () => 0.5, sha256 },
        { onInterrupt: (i) => interrupts.push(i) },
      );
      await session.open();
      for (let idx = 0; idx < 3; idx += 1) {
        session.addSlice(sliceBlob(idx), (idx + 1) * 5000);
        await vi.advanceTimersByTimeAsync(5000);
        for (let t = 0; t < 30; t += 1) await turn();
      }
      expect(interrupts).toEqual([]);
      expect(server.appendedSlices).toEqual([0, 1, 2]);
    }
  });

  it('tells the person when saving has been failing for over a minute, and not before', async () => {
    const server = new FakeSessionServer({
      inject: ({ method, seq }) =>
        method === 'PUT' && seq === 0
          ? { status: 503, body: { detail: 'disk', reason: 'storage_unavailable' } }
          : undefined,
    });
    server.status = 'recording';
    const seen: boolean[] = [];
    const session = new VoiceSession(
      { sessionId: SESSION_ID, mimeType: 'audio/webm', config: DEFAULT_SESSION_CONFIG },
      { fetchImpl: server.fetch as unknown as typeof fetch, store: createMemoryOutbox(), random: () => 0.5, sha256 },
      { onProgress: (p) => seen.push(p.storageTrouble) },
    );
    await session.open();
    session.addSlice(sliceBlob(0), 5000);
    for (let t = 0; t < 30; t += 1) await turn();
    expect(seen.some(Boolean)).toBe(false);
    for (let s = 0; s < 20; s += 1) {
      await vi.advanceTimersByTimeAsync(5000);
      for (let t = 0; t < 10; t += 1) await turn();
    }
    expect(seen[seen.length - 1]).toBe(true);
    expect(VOICE_MESSAGES.storageTrouble).toBe(
      "The server can't save audio right now. Still recording on this device; it will upload when the server recovers.",
    );
  });

  it('says what the bar shows while the connection is gone', () => {
    expect(VOICE_MESSAGES.offlineRecording).toBe(
      'Connection lost. Still recording; the audio will upload when the connection is back.',
    );
    expect(VOICE_MESSAGES.offlineFinishing('0:35')).toBe('Waiting for a connection to upload the last 0:35 of your recording…');
  });
});

// ---------------------------------------------------------------------------
// What a finished recording came to
// ---------------------------------------------------------------------------

describe('what a finished recording came to', () => {
  it('transcribed: the words, and nothing said about them', () => {
    expect(describeOutcome(state())).toEqual({
      kind: 'text',
      text: 'hello there',
      notices: [],
      offer: null,
      sessionId: SESSION_ID,
    });
  });

  it('transcribed with low segments: the words, and one line to check them', () => {
    const result = describeOutcome(
      state({ segments: [{ i: 0, start_ms: 0, end_ms: 5000, text: 'hello', low: true }] }),
    );
    expect(result.kind).toBe('text');
    expect(messageOf(result)).toBe('Some parts were hard to make out — check the text before you send it.');
  });

  it('transcribed with gaps: the words, the gaps by time, and a Retry that re-reads the saved audio', () => {
    const result = describeOutcome(
      state({
        outcome: 'transcribed_with_gaps',
        gaps: [
          { start_ms: 250_000, end_ms: 280_000, reason: 'engine_unavailable' },
          { start_ms: 1_325_000, end_ms: 1_355_000, reason: 'engine_refused' },
          { start_ms: 60_000, end_ms: 62_000, reason: 'dropped_as_noise' },
        ],
      }),
    );
    expect(result.kind).toBe('text');
    if (result.kind !== 'text') return;
    expect(result.text).toBe('hello there');
    expect(result.offer).toMatchObject({
      kind: 'retranscribe',
      scope: 'gaps',
      label: 'Retry',
      replaces: 'hello there',
      message:
        "The speech engine couldn't transcribe 2 parts (4:10–4:40, 22:05–22:35). The audio is saved. Press Retry to transcribe them.",
    });
  });

  it('no speech: says the recording is saved and points at the right microphone, not a closer one', () => {
    const result = describeOutcome(state({ outcome: 'no_speech', text: '', speech_ms: 0 }), { peakLevel: 0.4 });
    expect(messageOf(result)).toBe(
      "No speech was detected in this 12:34 recording. If you did speak, check that the right microphone is selected and isn't muted. The recording is saved.",
    );
    const silentMeter = describeOutcome(state({ outcome: 'no_speech', text: '' }), { peakLevel: 0.01 });
    expect(messageOf(silentMeter)).toBe(
      "The microphone picked up no sound at all during this recording. Check that the right microphone is selected and isn't muted.",
    );
  });

  it('no words: sound, but nothing that was speech', () => {
    expect(messageOf(describeOutcome(state({ outcome: 'no_words', text: '' })))).toBe(
      'Sound was detected, but no words could be made out. It may have been background noise or music. The recording is saved.',
    );
  });

  it('engine unavailable: saved, with a Retry', () => {
    const result = describeOutcome(state({ outcome: 'engine_unavailable', status: 'failed', text: '' }));
    expect(result.kind).toBe('error');
    if (result.kind !== 'error') return;
    expect(result.error).toEqual({
      message:
        'The speech engine was unavailable, so nothing was transcribed yet. Your 12:34 recording is saved. Press Retry, or try again in a few minutes.',
      retryable: true,
    });
    expect(result.offer).toMatchObject({ kind: 'retranscribe', label: 'Retry' });
  });

  it('undecodable: kept exactly as it arrived, and no retry that cannot help', () => {
    const result = describeOutcome(state({ outcome: 'undecodable', status: 'failed', text: '' }));
    expect(result).toMatchObject({
      kind: 'error',
      error: {
        message: "The server couldn't read the audio in this recording. The file is saved exactly as it arrived.",
        retryable: false,
      },
      offer: null,
    });
  });

  it('an outcome this client has never heard of is the generic sentence, which still says it was saved', () => {
    expect(messageOf(describeOutcome(state({ outcome: 'from_the_future', text: '' })))).toBe(
      'Something went wrong on the server. Your recording up to 12:34 is saved.',
    );
    // … and one with words still delivers the words.
    expect(describeOutcome(state({ outcome: 'from_the_future' })).kind).toBe('text');
  });

  it('counts an hour-long recording in hours', () => {
    expect(messageOf(describeOutcome(state({ outcome: 'no_words', text: '', audio_ms: 7_265_000 })))).not.toContain(
      '121:05',
    );
    expect(messageOf(describeOutcome(state({ outcome: 'engine_unavailable', text: '', audio_ms: 7_265_000 })))).toContain(
      '2:01:05',
    );
  });
});

// ---------------------------------------------------------------------------
// Retry on a saved recording
// ---------------------------------------------------------------------------

describe('Retry on a saved recording', () => {
  const offer = {
    kind: 'retranscribe' as const,
    sessionId: SESSION_ID,
    scope: 'gaps' as const,
    replaces: 'hello',
    message: 'm',
    label: 'Retry',
  };
  it.each([
    [409, { reason: 'session_busy', detail: 'busy' }, 'This recording is still being transcribed.'],
    [410, { reason: 'audio_deleted', detail: 'gone' }, "This recording's audio has been deleted, so it can't be transcribed again."],
    [404, { reason: 'not_found', detail: 'gone' }, 'This recording is no longer on the server. It was discarded or has expired.'],
  ])('%i', async (status, body, message) => {
    const result = await retranscribeSession(offer, {
      fetchImpl: vi.fn(async () => answer(status, body)) as unknown as typeof fetch,
    });
    expect(messageOf(result)).toBe(message);
  });
});

// ---------------------------------------------------------------------------
// The microphone sentence
// ---------------------------------------------------------------------------

describe('"closer to the microphone" is kept only where the server judged the audio', () => {
  it('appears nowhere on the session road', () => {
    const everySentence = Object.values(VOICE_MESSAGES)
      .map((m) => (typeof m === 'function' ? (m as (...a: unknown[]) => string)('0:30', '0:40') : m))
      .join('\n');
    expect(everySentence).not.toMatch(/closer to the microphone/i);
    expect(everySentence).not.toMatch(/wasn't clear enough/i);
  });

  it('on the legacy road: kept for a clip the server re-listened to and judged unclear', async () => {
    const result = await transcribe(new Blob(['x']), {
      durationMs: 40_000,
      mimeType: 'audio/webm',
      fetchImpl: vi.fn(async () => answer(200, { text: '', confidence: 'unclear' })) as unknown as typeof fetch,
    });
    expect('error' in result && result.error.message).toBe(MICROPHONE);
  });

  it('on the legacy road: NOT for the owner’s 181 s recording the engine judged by its first 30 s alone', async () => {
    const result = await transcribe(new Blob(['x']), {
      durationMs: 181_427,
      mimeType: 'audio/webm',
      fetchImpl: vi.fn(async () => answer(200, { text: '', confidence: 'unclear' })) as unknown as typeof fetch,
    });
    expect('error' in result && result.error.message).toBe(
      // 2026-09-29 (backend verifier item K): 'unclear' is also what the
      // server says when it judged the heard words invented, or the decoder
      // returned nothing with the gate open, so the sentence claims only what
      // is true of all three.
      'No words came back for that recording, and the server could not tell whether anything was said. A recording this long is judged by its first 30 seconds, so a quiet start can empty all of it: start speaking right away, or attach long recordings as a file.',
    );
  });

  it('on the legacy road: NOT when the server gave no judgement at all', async () => {
    const result = await transcribe(new Blob(['x']), {
      durationMs: 20_000,
      mimeType: 'audio/webm',
      fetchImpl: vi.fn(async () => answer(200, { text: '' })) as unknown as typeof fetch,
    });
    expect('error' in result && result.error.message).toBe('No words came back, and the server did not say why.');
  });
});

describe('the bar and the confirmations', () => {
  it('says where the recording is kept, and for how long when there is a limit', () => {
    expect(VOICE_MESSAGES.saved(0)).toBe('Saved to your account');
    expect(VOICE_MESSAGES.saved(30)).toBe('Saved to your account · kept 30 days');
  });
  it('asks before discarding a long recording, and says it is saved until then', () => {
    expect(VOICE_MESSAGES.discardConfirm('12:34')).toBe(
      'Discard this 12:34 recording? It is saved on the server until you discard it.',
    );
  });
  it('explains a stop the person did not make', () => {
    expect(VOICE_MESSAGES.recorderError('4:02')).toBe(
      'Recording stopped unexpectedly at 4:02. Everything up to then is saved and is being transcribed.',
    );
    expect(VOICE_MESSAGES.pausedHidden('3:00', '5:30')).toBe(
      'Recording paused while the screen was off (3:00 to 5:30). Keep the screen on while recording. Everything else is saved.',
    );
  });
});
