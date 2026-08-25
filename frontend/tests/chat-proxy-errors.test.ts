/**
 * The chat proxy's failure path: status preservation, a body with nothing in
 * it, and the server-side log that replaces what the UI no longer shows.
 *
 * The bug these lock down: the route collapsed every upstream status onto
 * 502/503 and the client then classified failures by running a regex over the
 * error SENTENCE, so a real 404, a backend 500 and a model timeout all became
 * "the orchestrator is unreachable".
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { formatProxyError, requestIdOf } from '../lib/serverLog';

const CHAT_BODY = JSON.stringify({
  messages: [{ role: 'user', content: 'hello' }],
});

const post = (init?: RequestInit) =>
  new Request('http://localhost:3001/api/chat', {
    method: 'POST',
    body: CHAT_BODY,
    headers: { 'content-type': 'application/json' },
    ...init,
  });

let errors: string[] = [];

beforeEach(() => {
  errors = [];
  vi.stubEnv('MOCK_MODE', 'false');
  vi.stubEnv('ORCHESTRATOR_URL', 'http://orchestrator:8080');
  vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => {
    errors.push(args.map(String).join(' '));
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

/** Import fresh so the stubbed env is read at call time. */
async function POST(req: Request) {
  const mod = await import('../app/api/chat/route');
  return mod.POST(req);
}

describe('chat proxy — upstream statuses survive', () => {
  it.each([
    [404, 'NOT_FOUND'],
    [500, 'APPLICATION_ERROR'],
    [502, 'MODEL_UNAVAILABLE'],
    [503, 'ORCHESTRATOR_UNAVAILABLE'],
    [504, 'TIMEOUT'],
  ])('passes %i through with category %s', async (status, category) => {
    vi.stubGlobal('fetch', async () =>
      new Response(JSON.stringify({ detail: 'upstream said something' }), {
        status,
        headers: { 'content-type': 'application/json' },
      }),
    );
    const res = await POST(post());
    expect(res.status).toBe(status);
    await expect(res.json()).resolves.toEqual({ code: category });
  });

  it('does not turn a 500 into a 502 any more', async () => {
    vi.stubGlobal('fetch', async () => new Response('boom', { status: 500 }));
    expect((await POST(post())).status).toBe(500);
  });
});

describe('chat proxy — the body carries nothing to leak', () => {
  it('never forwards the upstream sentence to the browser', async () => {
    const secret =
      "Error code: 500 - {'error': {'message': 'connect ECONNREFUSED 10.0.0.4:8080'}}";
    vi.stubGlobal('fetch', async () =>
      new Response(JSON.stringify({ detail: secret }), {
        status: 500,
        headers: { 'content-type': 'application/json' },
      }),
    );
    const res = await POST(post());
    const text = await res.text();
    expect(text).not.toMatch(/ECONNREFUSED|10\.0\.0\.4|8080/);
    expect(JSON.parse(text)).toEqual({ code: 'APPLICATION_ERROR' });
  });

  it('reports a refused socket with no status of its own', async () => {
    vi.stubGlobal('fetch', async () => {
      const err = new Error('fetch failed');
      (err as { cause?: unknown }).cause = { code: 'ECONNREFUSED' };
      throw err;
    });
    const res = await POST(post());
    // The proxy answers 502 for "could not complete the upstream call", and
    // the code tells the page there was never an HTTP status at all.
    expect(res.status).toBe(502);
    await expect(res.json()).resolves.toEqual({ code: 'NETWORK_ERROR' });
  });

  it('classifies an undici timeout as TIMEOUT, not as unreachable', async () => {
    vi.stubGlobal('fetch', async () => {
      const err = new Error('headers timeout');
      (err as { cause?: unknown }).cause = { code: 'UND_ERR_HEADERS_TIMEOUT' };
      throw err;
    });
    const res = await POST(post());
    expect(res.status).toBe(504);
    await expect(res.json()).resolves.toEqual({ code: 'TIMEOUT' });
  });

  it('still answers 499 for a client abort', async () => {
    vi.stubGlobal('fetch', async () => {
      const err = new Error('aborted');
      err.name = 'AbortError';
      throw err;
    });
    expect((await POST(post())).status).toBe(499);
  });
});

describe('chat proxy — server-side logging', () => {
  it('logs the real upstream sentence the UI no longer shows', async () => {
    vi.stubGlobal('fetch', async () =>
      new Response(JSON.stringify({ detail: 'model worker crashed' }), {
        status: 503,
        headers: { 'content-type': 'application/json' },
      }),
    );
    await POST(post());
    const line = errors.join('\n');
    expect(line).toContain('[chat-proxy:error]');
    expect(line).toContain('status=503');
    expect(line).toContain('category="ORCHESTRATOR_UNAVAILABLE"');
    expect(line).toContain('route="/api/chat"');
    expect(line).toContain('model worker crashed');
    expect(line).toMatch(/duration_ms=\d+/);
    expect(line).toMatch(/retryable=(true|false)/);
    expect(line).toMatch(/timestamp="\d{4}-\d{2}-\d{2}T/);
  });

  it('logs the correlation id when one was supplied', async () => {
    vi.stubGlobal('fetch', async () => new Response('x', { status: 500 }));
    await POST(post({ headers: { 'x-request-id': 'req-42' } }));
    expect(errors.join('\n')).toContain('request_id="req-42"');
  });

  it('logs the transport exception code', async () => {
    vi.stubGlobal('fetch', async () => {
      const err = new Error('fetch failed');
      (err as { cause?: unknown }).cause = { code: 'ECONNREFUSED' };
      throw err;
    });
    await POST(post());
    expect(errors.join('\n')).toContain('ECONNREFUSED');
  });

  it('redacts credentials an upstream echoed back', async () => {
    vi.stubGlobal('fetch', async () =>
      new Response(
        JSON.stringify({
          detail:
            'auth failed: Authorization: Bearer sk-abcdef1234567890 for postgres://app:p4ssw0rd@db:5432/x',
        }),
        { status: 500, headers: { 'content-type': 'application/json' } },
      ),
    );
    await POST(post());
    const line = errors.join('\n');
    expect(line).not.toMatch(/sk-abcdef1234567890|p4ssw0rd/);
    expect(line).toMatch(/redacted/i);
  });
});

describe('serverLog helpers', () => {
  it('formats one greppable line', () => {
    const line = formatProxyError({
      route: '/api/chat',
      status: 503,
      category: 'MODEL_UNAVAILABLE',
      message: 'model upstream timed out',
      requestId: 'abc',
      durationMs: 5320.7,
      retryable: true,
    });
    expect(line).not.toContain('\n');
    expect(line).toContain('duration_ms=5321');
    expect(line).toContain('category="MODEL_UNAVAILABLE"');
  });

  it('writes status=none rather than a fake number', () => {
    expect(
      formatProxyError({ route: '/api/chat', status: null, category: 'NETWORK_ERROR' }),
    ).toContain('status="none"');
  });

  it('takes a correlation id from the usual headers, else null', () => {
    expect(
      requestIdOf(new Request('http://x/', { headers: { 'x-request-id': 'r1' } })),
    ).toBe('r1');
    expect(
      requestIdOf(new Request('http://x/', { headers: { 'x-correlation-id': 'c1' } })),
    ).toBe('c1');
    expect(requestIdOf(new Request('http://x/'))).toBeNull();
  });
});

describe('the log keeps what the UI gives up', () => {
  it('records a traceback for the engineer while the browser sees none of it', async () => {
    const raw =
      'Traceback (most recent call last):\n  File "engine.py", line 4\n' +
      'RuntimeError: CUDA out of memory on cuda:0';
    vi.stubGlobal('fetch', async () =>
      new Response(JSON.stringify({ detail: raw }), {
        status: 500,
        headers: { 'content-type': 'application/json' },
      }),
    );
    const res = await POST(post());
    // Browser: status + category, nothing else.
    await expect(res.json()).resolves.toEqual({ code: 'APPLICATION_ERROR' });
    // Log: the real cause, on one line.
    const line = errors.join('\n');
    expect(line).toContain('CUDA out of memory');
    expect(line).toContain('Traceback');
    expect(line.split('\n').filter((l) => l.includes('chat-proxy:error'))).toHaveLength(1);
  });
});

describe('dev-only simulation through the real chat path', () => {
  it.each([
    ['/simulate 404', 404, 'NOT_FOUND'],
    ['/simulate 500', 500, 'APPLICATION_ERROR'],
    ['/simulate 502', 502, 'MODEL_UNAVAILABLE'],
    ['/simulate 503', 503, 'ORCHESTRATOR_UNAVAILABLE'],
    ['/simulate 504', 504, 'TIMEOUT'],
  ])('%s fails the send with %i', async (text, status, code) => {
    const spy = vi.fn();
    vi.stubGlobal('fetch', spy);
    const res = await POST(
      new Request('http://localhost:3001/api/chat', {
        method: 'POST',
        body: JSON.stringify({ messages: [{ role: 'user', content: text }] }),
        headers: { 'content-type': 'application/json' },
      }),
    );
    expect(res.status).toBe(status);
    await expect(res.json()).resolves.toEqual({ code });
    // No service was touched: the simulation short-circuits before any call.
    expect(spy).not.toHaveBeenCalled();
    expect(errors.join('\n')).toContain('SIMULATED_ERROR');
  });

  it('leaves an ordinary message that mentions simulation alone', async () => {
    vi.stubGlobal('fetch', async () =>
      new Response('data: {}\n\n', {
        status: 200,
        headers: { 'content-type': 'text/event-stream' },
      }),
    );
    const res = await POST(
      new Request('http://localhost:3001/api/chat', {
        method: 'POST',
        body: JSON.stringify({
          messages: [{ role: 'user', content: 'how do I simulate 503?' }],
        }),
        headers: { 'content-type': 'application/json' },
      }),
    );
    expect(res.status).toBe(200);
  });
});
