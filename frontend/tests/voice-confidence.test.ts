/**
 * What the person sees when the engine was not sure (audit #24 reframed,
 * 2026-09-21).
 *
 * The language selector this started as was REFUSED: forcing a language
 * mistranscribes the code-switching majority, and the long-form recipe
 * measured worse. What the orchestrator already knew and threw away was
 * CONFIDENCE — the engine's own no-speech probability and its plausibility
 * check — and the cost of throwing it away was a lie: every empty draft came
 * back as "Nothing was said in that recording.", including the drafts the
 * orchestrator had emptied itself.
 *
 * Measured on the production worker replica 2026-09-21, one request per clip,
 * the speech rows cut from one real recording:
 *
 *   clip                        no_speech_prob   words   what the person gets
 *   8 s clean speech                    0.0088      17   the text, no line
 *   8 s of the same, 26 dB down         0.0238      17   the text, no line
 *   20 s pink noise at -25 dBFS         0.0352      14   the text AND a line
 *   20 s digital silence                0.7082       0   "Nothing was said."
 *
 * The noise clip is the finding: fourteen invented words in "Nynorsk" went
 * into the composer as fact. They still go in — nothing on this side can tell
 * them from a short real answer, and deleting a real draft is worse — but
 * they no longer go in silently.
 */
import { describe, expect, it, vi } from 'vitest';

import { transcribe } from '@/lib/voice';

const CLEAR =
  "That recording wasn't clear enough to transcribe. Try again, closer to the microphone.";
const CHECK_IT = 'That was hard to make out — check the text before you send it.';
const UNSAID = 'No words came back, and the server did not say why.';

const blob = () => new Blob(['opus'], { type: 'audio/webm' });

const send = (payload: Record<string, unknown>) =>
  transcribe(blob(), {
    durationMs: 20_000,
    mimeType: 'audio/webm',
    fetchImpl: vi.fn(async () => Response.json(payload, { status: 200 })) as unknown as typeof fetch,
  });

const reply = (over: Record<string, unknown>) => ({
  text: '',
  language: null,
  language_code: null,
  duration_ms: 20_000,
  processing_ms: 900,
  upload_ms: 20,
  engine_ms: 880,
  confidence: null,
  ...over,
});

describe('the four clips, as the composer shows them', () => {
  it('clean speech arrives as text with nothing said about it', async () => {
    const result = await send(
      reply({ text: 'the quarterly numbers are ready', language: 'English', confidence: null }),
    );
    expect('error' in result).toBe(false);
    expect((result as { text: string }).text).toBe('the quarterly numbers are ready');
    expect((result as { notice: string | null }).notice).toBeNull();
  });

  it('quiet speech the engine was still sure of gets no line either', async () => {
    // Measured: 26 dB down, no_speech_prob 0.0238, the same seventeen words.
    const result = await send(
      reply({ text: 'the quarterly numbers are ready', language: 'English', confidence: null }),
    );
    expect((result as { notice: string | null }).notice).toBeNull();
  });

  it('words decoded from noise arrive WITH a line, and are not withheld', async () => {
    const result = await send(
      reply({ text: 'Og så skal vi se på det her', language: 'Nynorsk', confidence: 'low' }),
    );
    expect('error' in result).toBe(false);
    // The draft is still there: a person can read it and delete it, which is
    // something no threshold on this side can do for them.
    expect((result as { text: string }).text).toBe('Og så skal vi se på det her');
    expect((result as { notice: string | null }).notice).toBe(CHECK_IT);
  });

  it('measured silence is still reported as silence', async () => {
    const result = await send(reply({ confidence: 'silent' }));
    expect('error' in result).toBe(true);
    expect((result as { error: { message: string } }).error.message).toBe(
      'Nothing was said in that recording.',
    );
  });
});

describe('an empty draft is never called silence unless silence was measured', () => {
  // 'unclear' — the second opinion was declined, or the words were dropped
  // as invented. 'low' — a caution that still came back empty. null — a
  // server that said nothing about it. None of them measured silence.
  it('confidence unclear does not claim the room was quiet', async () => {
    const result = await send(reply({ confidence: 'unclear' }));
    expect('error' in result).toBe(true);
    const message = (result as { error: { message: string } }).error.message;
    expect(message).toBe(CLEAR);
    expect(message).not.toContain('Nothing was said');
  });

  // 2026-09-29: 'low' is the server's word for "there IS a draft and it may be
  // invented" (orchestrator/app/asr.py CONFIDENCE_LOW). An empty draft marked
  // 'low' is not a judgement that the audio was unclear, so it is not blamed
  // on the microphone; it was, until then.
  it('confidence low on an empty draft does not claim the room was quiet, nor blame the microphone', async () => {
    const result = await send(reply({ confidence: 'low' }));
    expect('error' in result).toBe(true);
    const message = (result as { error: { message: string } }).error.message;
    expect(message).toBe(UNSAID);
    expect(message).not.toContain('microphone');
    expect(message).not.toContain('Nothing was said');
  });

  // 2026-09-29: "closer to the microphone" is kept ONLY where the server
  // judged the audio unclear. A server that said nothing about it used to get
  // the microphone sentence too, which blamed the one thing nobody had judged.
  it('no confidence at all is not blamed on the microphone either', async () => {
    const result = await send(reply({ confidence: null }));
    const message = (result as { error: { message: string } }).error.message;
    expect(message).toBe(UNSAID);
    expect(message).not.toContain('microphone');
    expect(message).not.toContain('Nothing was said');
  });

  it('a value this client does not know is treated as no opinion', async () => {
    const result = await send(reply({ confidence: 'extremely-confident' }));
    expect((result as { error: { message: string } }).error.message).toBe(UNSAID);
  });

  it('a long unclear clip names the first 30 seconds, not the microphone', async () => {
    // The owner's recording, 2026-09-24: 181,427 ms, empty, in 2,175 ms. The
    // engine judged only its first 30 s, and over 120 s no second listen is
    // taken, so moving the microphone could not have helped.
    const result = await transcribe(blob(), {
      durationMs: 181_427,
      mimeType: 'audio/webm',
      fetchImpl: vi.fn(async () =>
        Response.json(reply({ confidence: 'unclear', duration_ms: 181_427 }), { status: 200 }),
      ) as unknown as typeof fetch,
    });
    const message = (result as { error: { message: string } }).error.message;
    expect(message).toBe(
      'The first 30 seconds of that recording sounded silent, so the rest of it was not transcribed. Start speaking right away, or attach long recordings as a file.',
    );
    expect(message).not.toContain('microphone');
  });

  it('every empty draft is retryable, whatever the reason', async () => {
    for (const confidence of ['silent', 'unclear', 'low', null]) {
      const result = await send(reply({ confidence }));
      expect((result as { error: { retryable: boolean } }).error.retryable).toBe(true);
    }
  });
});

describe('the line is a line, not a wall and not a dialog', () => {
  it('is one short sentence', async () => {
    const result = await send(reply({ text: 'maybe words', confidence: 'low' }));
    const notice = (result as { notice: string }).notice;
    expect(notice.length).toBeLessThan(80);
    expect(notice.split('. ').length).toBeLessThanOrEqual(2);
  });

  it('never names the engine, the model or a probability', async () => {
    const result = await send(reply({ text: 'maybe words', confidence: 'low' }));
    const notice = (result as { notice: string }).notice.toLowerCase();
    for (const leak of ['whisper', 'no_speech', 'probability', 'model', 'gpu', '0.0']) {
      expect(notice).not.toContain(leak);
    }
  });
});

describe('a failure is still a failure', () => {
  it('a confidence word never turns a 503 into a transcript', async () => {
    const result = await transcribe(blob(), {
      durationMs: 20_000,
      mimeType: 'audio/webm',
      fetchImpl: vi.fn(async () =>
        Response.json(
          { detail: 'Transcription is busy right now. Try again in a moment.', confidence: 'low' },
          { status: 503 },
        ),
      ) as unknown as typeof fetch,
    });
    expect('error' in result).toBe(true);
    expect((result as { error: { message: string } }).error.message).toBe(
      'Transcription is busy right now. Try again in a moment.',
    );
  });
});
