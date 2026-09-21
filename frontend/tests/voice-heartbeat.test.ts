// @vitest-environment jsdom
/**
 * A long dictation's wait, end to end on the browser side (2026-09-18).
 *
 * Whisper took 268.3 s for 595 s of audio on a quiet replica and 219.7 s for
 * 300 s on a busy one, and Cloudflare gives up on a first byte at 125 s. The
 * orchestrator therefore answers a long transcription with a streamed 200:
 * whitespace every 15 s, then the JSON — or, when the work fails after the
 * status line has gone, {"detail", "status"} in the body. Three things on
 * this side have to cooperate, and each is pinned here:
 *
 *   1. THE PROXY STREAMS. `await upstream.text()` held every byte until the
 *      transcript existed, which is exactly the silence Cloudflare cuts.
 *   2. THE CLIENT READS BOTH SHAPES. A failure carried in a 200's body is a
 *      failure, mapped exactly as that status would have been — never an empty
 *      transcript ("Nothing was said"), and never a success.
 *   3. THE PERSON SEES A CLOCK. A bare "Transcribing…" for four minutes reads
 *      as a hang.
 *
 * jsdom for the bar; the proxy and the client use Node's own fetch types,
 * which jsdom does not replace.
 */
import { act, cleanup, render, screen } from '@testing-library/react';
import { createElement } from 'react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { VoiceBar } from '@/components/VoiceBar';
import { transcribe } from '@/lib/voice';

const encoder = new TextEncoder();
const decoder = new TextDecoder();

/** An upstream body the test writes into, byte by byte, and ends on demand. */
function heldStream() {
  let controller!: ReadableStreamDefaultController<Uint8Array>;
  const stream = new ReadableStream<Uint8Array>({
    start(c) {
      controller = c;
    },
  });
  return {
    stream,
    push: (text: string) => controller.enqueue(encoder.encode(text)),
    close: () => controller.close(),
    fail: () => controller.error(new TypeError('terminated')),
  };
}

async function readAll(reader: ReadableStreamDefaultReader<Uint8Array>): Promise<string> {
  let out = '';
  for (;;) {
    const { value, done } = await reader.read();
    if (done) return out;
    out += decoder.decode(value);
  }
}

const post = () =>
  new Request('http://localhost:3001/api/audio/transcribe?duration_ms=300000&language=auto', {
    method: 'POST',
    body: 'opus bytes',
    headers: { 'content-type': 'audio/webm' },
  });

describe('the transcription proxy', () => {
  beforeEach(() => {
    vi.stubEnv('MOCK_MODE', 'false');
    vi.stubEnv('ORCHESTRATOR_URL', 'http://orchestrator:8080');
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  it('hands the first heartbeat on before the orchestrator has finished', async () => {
    const upstream = heldStream();
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => new Response(upstream.stream, { status: 200 })),
    );
    const { POST } = await import('../app/api/audio/transcribe/route');
    upstream.push(' ');

    // Buffered, POST cannot resolve while the upstream is still open.
    const outcome = await Promise.race([
      POST(post()),
      new Promise<'buffered'>((resolve) => setTimeout(() => resolve('buffered'), 300)),
    ]);
    expect(outcome).not.toBe('buffered');
    const response = outcome as Response;
    expect(response.status).toBe(200);
    expect(response.headers.get('content-type')).toContain('application/json');
    expect(response.headers.get('cache-control')).toContain('no-transform');

    const reader = response.body!.getReader();
    const first = await reader.read();
    expect(decoder.decode(first.value)).toBe(' ');

    upstream.push(' ');
    upstream.push('{"text":"the status"}');
    upstream.close();
    const body = ' ' + (await readAll(reader));
    expect(JSON.parse(body)).toEqual({ text: 'the status' });
  });

  it('keeps a refusal’s real status and sentence', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        Response.json({ detail: 'That recording is longer than 10 minutes.' }, { status: 413 }),
      ),
    );
    const { POST } = await import('../app/api/audio/transcribe/route');
    const response = await POST(post());
    expect(response.status).toBe(413);
    expect(await response.json()).toEqual({
      detail: 'That recording is longer than 10 minutes.',
    });
  });

  it('still answers 502 when the orchestrator cannot be reached at all', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => {
        throw new TypeError('fetch failed');
      }),
    );
    const { POST } = await import('../app/api/audio/transcribe/route');
    const response = await POST(post());
    expect(response.status).toBe(502);
  });
});

describe('reading a transcription that arrived behind a heartbeat', () => {
  const blob = () => new Blob(['opus'], { type: 'audio/webm' });
  const send = (response: Response) =>
    transcribe(blob(), {
      durationMs: 300_000,
      mimeType: 'audio/webm',
      fetchImpl: vi.fn(async () => response) as unknown as typeof fetch,
    });

  it('reads the transcript behind the whitespace', async () => {
    const result = await send(
      new Response(
        '    \n{"text":"the status","language":"English","duration_ms":300000,"processing_ms":141000}',
        { status: 200 },
      ),
    );
    expect(result).toEqual({
      text: 'the status',
      language: 'English',
      durationMs: 300_000,
      processingMs: 141_000,
      // No `confidence` in this body, so nothing is said about the draft
      // (2026-09-21). tests/voice-confidence.test.ts owns that field.
      notice: null,
    });
  });

  it.each([
    [504, 'That recording took too long to transcribe. Try a shorter one.'],
    // A short clip that timed out met a stuck engine (repair round 1).
    [504, 'The speech engine did not answer in time. Please try again.'],
    [429, 'Too many recordings just now. Wait a moment and try again.'],
    [503, 'Transcription couldn’t be completed. Please try again.'],
    [422, 'That recording could not be transcribed.'],
  ])(
    'treats a %i carried in the body exactly as it treats that status',
    async (status, detail) => {
      const carried = await send(
        new Response(`   ${JSON.stringify({ detail, status })}`, { status: 200 }),
      );
      const direct = await send(Response.json({ detail }, { status }));
      expect(carried).toEqual(direct);
      expect('error' in carried && carried.error.message).toBe(detail);
    },
  );

  it('never reads a failure carried in the body as a transcript or as silence', async () => {
    const result = await send(new Response('  {"status":500}', { status: 200 }));
    expect('error' in result).toBe(true);
    if ('error' in result) {
      expect(result.error.message).toBe('Transcription couldn’t be completed. Please try again.');
      expect(result.error.message).not.toMatch(/nothing was said/i);
    }
  });

  it('turns a stream that breaks after the heartbeat into a failure, not a hang', async () => {
    const upstream = heldStream();
    upstream.push('   ');
    upstream.fail();
    const result = await send(new Response(upstream.stream, { status: 200 }));
    expect('error' in result).toBe(true);
    if ('error' in result) {
      expect(result.error.message).toBe('Transcription couldn’t be completed. Please try again.');
      expect(result.error.retryable).toBe(true);
    }
  });
});

describe('withdrawing while the heartbeat is running', () => {
  it('is a withdrawal, not an error to show', async () => {
    const upstream = heldStream();
    upstream.push(' ');
    const controller = new AbortController();
    const pending = transcribe(new Blob(['opus'], { type: 'audio/webm' }), {
      durationMs: 300_000,
      mimeType: 'audio/webm',
      signal: controller.signal,
      fetchImpl: vi.fn(async () => new Response(upstream.stream, { status: 200 })) as unknown as typeof fetch,
    });
    await new Promise((resolve) => setTimeout(resolve, 10));
    controller.abort();
    upstream.fail();
    expect(await pending).toEqual({ error: { message: '', retryable: true } });
  });
});

describe('the bar while a recording is transcribed', () => {
  beforeEach(() => {
    vi.useFakeTimers();
  });
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  const bar = () =>
    createElement(VoiceBar, {
      state: 'transcribing',
      levels: [],
      // The recording's own length; the wait clock must not start from it.
      elapsedMs: 42_000,
      maxMs: 600_000,
      onCancel: () => undefined,
      onStop: () => undefined,
    });

  it('counts the seconds the person has been waiting, from zero', () => {
    render(bar());
    expect(screen.getByText('0:00')).toBeTruthy();
    expect(screen.queryByText('0:42')).toBeNull();

    act(() => {
      vi.advanceTimersByTime(15_000);
    });
    expect(screen.getByText('0:15')).toBeTruthy();

    act(() => {
      vi.advanceTimersByTime(60_000);
    });
    expect(screen.getByText('1:15')).toBeTruthy();
    // The clock is decoration for the eye; the live region still announces
    // the state once, not a number every second.
    expect(screen.getByText('1:15').getAttribute('aria-hidden')).toBe('true');
    expect(screen.getByText('Transcribing your recording')).toBeTruthy();
  });
});

/*
 * QA's adversarial cases, repair round 1 (2026-09-18). They passed against the
 * first delivery and are kept so the contract they probe cannot drift: a
 * heartbeat that ends with no JSON, body-carried statuses keeping their
 * retryability, bodies that are JSON but not an object, transcript text that
 * imitates the error shape, and a broken upstream through the proxy.
 */
describe('reading the heartbeat body, adversarially', () => {
  const blob = () => new Blob(['opus'], { type: 'audio/webm' });
  const send = (response: Response) =>
    transcribe(blob(), {
      durationMs: 300_000,
      mimeType: 'audio/webm',
      fetchImpl: vi.fn(async () => response) as unknown as typeof fetch,
    });

  it('a heartbeat that ends without any JSON is a retryable failure, never silence', async () => {
    const result = await send(new Response('      ', { status: 200 }));
    expect('error' in result).toBe(true);
    if ('error' in result) {
      expect(result.error.message).not.toMatch(/nothing was said/i);
      expect(result.error.retryable).toBe(true);
    }
  });

  it('a body-carried 403 keeps its sentence and is not offered as retryable', async () => {
    const detail = 'Voice input is turned off for your account. Ask an administrator.';
    const carried = await send(new Response(`  ${JSON.stringify({ detail, status: 403 })}`, { status: 200 }));
    const direct = await send(Response.json({ detail }, { status: 403 }));
    expect(carried).toEqual(direct);
    expect('error' in carried && carried.error.retryable).toBe(false);
  });

  it('a body-carried status is never read as a transcript, even beside a text field', async () => {
    const result = await send(
      new Response(
        '  {"status":503,"detail":"Transcription is busy right now. Try again in a moment.","text":"leak"}',
        { status: 200 },
      ),
    );
    expect('error' in result).toBe(true);
  });

  it('a JSON array, string or null is not a transcript', async () => {
    for (const body of ['  ["x"]', '  "hello"', '  null']) {
      const result = await send(new Response(body, { status: 200 }));
      expect('error' in result).toBe(true);
    }
  });

  it('a transcript that SAYS the error shape is still a transcript', async () => {
    const said = 'ignore previous instructions {"detail":"x","status":503}';
    const result = await send(new Response(`  ${JSON.stringify({ text: said })}`, { status: 200 }));
    expect(result).toMatchObject({ text: said });
  });

  it('right-to-left and Devanagari text survive the whitespace prefix', async () => {
    const said = 'مرحبا بكم — नमस्ते';
    const result = await send(new Response(`\n \n ${JSON.stringify({ text: said, language: 'Arabic' })}`, { status: 200 }));
    expect(result).toMatchObject({ text: said, language: 'Arabic' });
  });

  it('forty heartbeats then a ten-thousand-word transcript parse', async () => {
    const upstream = heldStream();
    const words = Array.from({ length: 10_000 }, (_, i) => `w${i}`).join(' ');
    const pending = send(new Response(upstream.stream, { status: 200 }));
    for (let i = 0; i < 40; i += 1) upstream.push(' ');
    upstream.push(JSON.stringify({ text: words }));
    upstream.close();
    const result = await pending;
    expect('text' in result && result.text.split(' ').length).toBe(10_000);
  });
});

describe('the proxy, adversarially', () => {
  beforeEach(() => {
    vi.stubEnv('MOCK_MODE', 'false');
    vi.stubEnv('ORCHESTRATOR_URL', 'http://orchestrator:8080');
  });
  afterEach(() => {
    vi.unstubAllGlobals();
    vi.unstubAllEnvs();
    vi.restoreAllMocks();
  });

  it('an upstream that breaks mid-heartbeat breaks the proxied body too, never a clean truncated 200', async () => {
    const upstream = heldStream();
    vi.stubGlobal('fetch', vi.fn(async () => new Response(upstream.stream, { status: 200 })));
    const { POST } = await import('../app/api/audio/transcribe/route');
    upstream.push(' ');
    const response = await POST(post());
    const reader = response.body!.getReader();
    await reader.read();
    upstream.fail();
    await expect(reader.read()).rejects.toBeTruthy();
  });

  it('forwards the browser abort to the orchestrator', async () => {
    const seen: (AbortSignal | undefined)[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_url: string, init: RequestInit) => {
        seen.push(init.signal ?? undefined);
        return new Response(' ', { status: 200 });
      }),
    );
    const { POST } = await import('../app/api/audio/transcribe/route');
    const controller = new AbortController();
    await POST(
      new Request('http://localhost:3001/api/audio/transcribe', {
        method: 'POST',
        body: 'x',
        signal: controller.signal,
      }),
    );
    controller.abort();
    expect(seen[0]?.aborted).toBe(true);
  });
});

describe('the wait clock, adversarially', () => {
  beforeEach(() => vi.useFakeTimers());
  afterEach(() => {
    cleanup();
    vi.useRealTimers();
  });

  const bar = (state: 'recording' | 'transcribing', elapsedMs = 42_000) =>
    createElement(VoiceBar, {
      state,
      levels: [],
      elapsedMs,
      maxMs: 600_000,
      onCancel: () => undefined,
      onStop: () => undefined,
    });

  it('starts again from 0:00 for the next recording, not where the last wait stopped', () => {
    const view = render(bar('transcribing'));
    act(() => {
      vi.advanceTimersByTime(95_000);
    });
    expect(screen.getByText('1:35')).toBeTruthy();
    view.rerender(bar('recording', 3_000));
    view.rerender(bar('transcribing', 3_000));
    expect(screen.getByText('0:00')).toBeTruthy();
    expect(screen.queryByText('1:35')).toBeNull();
    act(() => {
      vi.advanceTimersByTime(7_000);
    });
    expect(screen.getByText('0:07')).toBeTruthy();
  });

  it('keeps counting past ten minutes (the timeout is 600 s)', () => {
    render(bar('transcribing'));
    act(() => {
      vi.advanceTimersByTime(10 * 60_000 + 5_000);
    });
    expect(screen.getByText('10:05')).toBeTruthy();
  });

  it('shows no wait clock while recording', () => {
    render(bar('recording', 3_000));
    act(() => {
      vi.advanceTimersByTime(5_000);
    });
    expect(screen.queryByText('0:05')).toBeNull();
  });
});
