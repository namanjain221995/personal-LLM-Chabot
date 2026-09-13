/**
 * The public API's same-origin edge, app/v1/[[...path]]/route.ts (2026-09-13,
 * docs/developer-platform/CONTRACT.md §1, §2, §9, §10, §11, §12).
 *
 * Until `api.techsarasolutions.com` exists, every `/v1` request in production
 * arrives at the Next process first, so this handler is on the path of every
 * paid, quota-bearing API call. Eight things are pinned here, each of which
 * was either wrong in the proxies this one is modelled on or is the kind of
 * thing a refactor quietly breaks:
 *
 *  1. the bearer key crosses the hop — no proxy in this repository forwarded
 *     `Authorization` before (the wave-1 audit), and a dropped credential is a
 *     401 for a key that is perfectly good;
 *  2. the session cookie does NOT cross it, in either direction (§1);
 *  3. an SSE answer reaches the caller incrementally, not when the generation
 *     ends (§10);
 *  4. the upstream status survives exactly, and a 429 keeps `Retry-After` and
 *     the RateLimit headers a client needs to retry politely (§12);
 *  5. an oversized body is refused HERE, with the contract's envelope, before
 *     the orchestrator is called at all (§8, §12);
 *  6. a client that goes away aborts the upstream request rather than leaving
 *     it to run for nobody;
 *  7. this edge adds no credential of its own and forwards no header the
 *     caller chose to describe itself with;
 *  8. nothing about the inside of the system — the orchestrator's host, a
 *     redirect's Location, a Set-Cookie — reaches the caller (§9).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  DEFAULT_PUBLIC_API_BODY_BYTES,
  GET,
  OPTIONS,
  POST,
  RESPONSE_HEADER_ALLOWLIST,
  publicApiBodyBytes,
  upstreamPathFor,
} from '@/app/v1/[[...path]]/route';

interface Call {
  url: string;
  init: RequestInit;
}

/** The optional catch-all's context: `/v1` itself resolves to no segments. */
const ctx = (...path: string[]) => ({
  params: Promise.resolve(path.length ? { path } : {}),
});

function capture(response: () => Response | Promise<Response>): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal('fetch', async (url: string | URL, init?: RequestInit) => {
    calls.push({ url: String(url), init: init ?? {} });
    return response();
  });
  return calls;
}

const sentHeaders = (call: Call) => call.init.headers as Record<string, string>;

const encoder = new TextEncoder();
const decoder = new TextDecoder();

/** A bearer key in the contract's shape (§5). Never logged, never relayed. */
const KEY = 'Bearer tsk_live_0123456789abcdef_Zm9vYmFyYmF6cXV4Y29ycmVjdA==xK9p2Q';

const post = (body: string, headers: Record<string, string> = {}) =>
  new Request('http://localhost:3001/v1/responses', {
    method: 'POST',
    body,
    headers: { 'content-type': 'application/json', ...headers },
  });

/**
 * Fail loudly instead of hanging. A buffering regression (reading the upstream
 * body before answering) makes the awaited value arrive only when the whole
 * generation has finished, which without this reads as a test-runner timeout
 * rather than as the defect it is.
 */
function withTimeout<T>(promise: Promise<T>, what: string): Promise<T> {
  return Promise.race([
    promise,
    new Promise<T>((_resolve, reject) =>
      setTimeout(
        () => reject(new Error(`${what} did not arrive — is the edge buffering?`)),
        1_000,
      ),
    ),
  ]);
}

/** Poll until a condition holds, so a test can act mid-flight. */
async function waitUntil(predicate: () => boolean): Promise<void> {
  for (let i = 0; i < 200; i += 1) {
    if (predicate()) return;
    await new Promise((resolve) => setTimeout(resolve, 5));
  }
  throw new Error('condition never became true');
}

let errors: string[] = [];

beforeEach(() => {
  errors = [];
  vi.stubEnv('ORCHESTRATOR_URL', 'http://orchestrator:8080');
  vi.stubEnv('PUBLIC_API_MAX_BODY_BYTES', '');
  // The default posture everywhere but the Cloudflare tunnel: no ingress
  // header is believed, so nothing is forwarded about where a caller came from.
  vi.stubEnv('TRUSTED_CLIENT_IP_HEADER', '');
  vi.stubEnv('TRUSTED_FORWARDED_PROTO', '');
  vi.spyOn(console, 'error').mockImplementation((...args: unknown[]) => {
    errors.push(args.map(String).join(' '));
  });
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// 1 + 2 + 7. The credential the API reads, and the one it must never see
// ---------------------------------------------------------------------------

describe('the credential', () => {
  it('forwards the Authorization header upstream, byte for byte', async () => {
    const calls = capture(() => Response.json({ id: 'resp_1' }));
    await POST(post('{"model":"techsara-35b","input":"hi"}', { authorization: KEY }), ctx('responses'));
    expect(sentHeaders(calls[0]).authorization).toBe(KEY);
  });

  it('forwards Idempotency-Key unchanged, because the server scopes replays by it', async () => {
    const calls = capture(() => Response.json({ id: 'resp_1' }));
    await POST(
      post('{"model":"techsara-35b","input":"hi"}', {
        authorization: KEY,
        'idempotency-key': 'customer-request-abc-123',
      }),
      ctx('responses'),
    );
    expect(sentHeaders(calls[0])['idempotency-key']).toBe('customer-request-abc-123');
  });

  it('never forwards the session cookie, whatever the caller attached', async () => {
    // CONTRACT §1: a surface that read both credentials would be a confused
    // deputy — any page on the internet could drive it with an ambient cookie.
    const calls = capture(() => Response.json({ id: 'resp_1' }));
    await POST(
      post('{"model":"techsara-35b","input":"hi"}', {
        authorization: KEY,
        cookie: 'ts_session=a-real-signed-in-session',
      }),
      ctx('responses'),
    );
    const headers = sentHeaders(calls[0]);
    expect(headers.cookie).toBeUndefined();
    expect(Object.keys(headers).map((k) => k.toLowerCase())).not.toContain('cookie');
  });

  it('adds no credential of its own when the caller sent none', async () => {
    // The 401 belongs to the orchestrator, which is the only thing entitled to
    // decide it. An edge that held a service token would let an unauthenticated
    // caller borrow it.
    const calls = capture(() => Response.json({ error: {} }, { status: 401 }));
    await POST(post('{"model":"techsara-35b","input":"hi"}'), ctx('responses'));
    const headers = sentHeaders(calls[0]);
    expect(headers.authorization).toBeUndefined();
    expect(headers.cookie).toBeUndefined();
  });

  it('forwards no header the caller chose to describe its own origin with', async () => {
    const calls = capture(() => Response.json({ id: 'resp_1' }));
    await POST(
      post('{"model":"techsara-35b","input":"hi"}', {
        authorization: KEY,
        'cf-connecting-ip': '198.51.100.7',
        'x-forwarded-for': '198.51.100.8',
        'x-real-ip': '198.51.100.9',
        'true-client-ip': '198.51.100.10',
        'x-forwarded-host': 'evil.example',
        'x-request-id': 'req_forged_by_the_caller',
      }),
      ctx('responses'),
    );
    const headers = sentHeaders(calls[0]);
    for (const name of [
      'cf-connecting-ip',
      'x-forwarded-for',
      'x-real-ip',
      'true-client-ip',
      'x-forwarded-host',
      'x-request-id',
    ]) {
      expect(headers[name]).toBeUndefined();
    }
  });

  it('forwards Origin, because the project allowlist is the server to check', async () => {
    // CONTRACT §3: the ACTUAL request is authorized against the project's
    // allowed_origins, and a stripped Origin would make every request look
    // like a server-to-server one.
    const calls = capture(() => Response.json({ id: 'resp_1' }));
    await POST(
      post('{"model":"techsara-35b","input":"hi"}', {
        authorization: KEY,
        origin: 'https://customer.example',
      }),
      ctx('responses'),
    );
    expect(sentHeaders(calls[0]).origin).toBe('https://customer.example');
  });
});

// ---------------------------------------------------------------------------
// 3. Streaming — token by token, not at the end (§10)
// ---------------------------------------------------------------------------

describe('a streamed response', () => {
  /** An upstream SSE body whose frames this test controls. */
  function openStream(): {
    response: () => Response;
    push: (text: string) => void;
    close: () => void;
  } {
    let controller!: ReadableStreamDefaultController<Uint8Array>;
    const body = new ReadableStream<Uint8Array>({
      start(c) {
        controller = c;
      },
    });
    return {
      response: () =>
        new Response(body, {
          status: 200,
          headers: { 'content-type': 'text/event-stream; charset=utf-8' },
        }),
      push: (text: string) => controller.enqueue(encoder.encode(text)),
      close: () => controller.close(),
    };
  }

  it('reaches the caller one event at a time, while the generation is still running', async () => {
    const upstream = openStream();
    capture(upstream.response);
    const res = await withTimeout(
      POST(post('{"model":"techsara-35b","input":"hi","stream":true}', { authorization: KEY }), ctx('responses')),
      'the response',
    );
    expect(res.status).toBe(200);
    const reader = res.body!.getReader();

    upstream.push('event: response.created\ndata: {"sequence_number":1}\n\n');
    const first = await withTimeout(reader.read(), 'the first event');
    expect(decoder.decode(first.value)).toContain('response.created');

    // Still open: the first event arrived before the second was even written,
    // which is the whole difference between streaming and buffering.
    upstream.push('event: response.output_text.delta\ndata: {"sequence_number":2}\n\n');
    const second = await withTimeout(reader.read(), 'the second event');
    expect(decoder.decode(second.value)).toContain('output_text.delta');

    upstream.close();
    await expect(withTimeout(reader.read(), 'the end of the stream')).resolves.toMatchObject({
      done: true,
    });
  });

  it('answers with the streaming headers the contract names', async () => {
    const upstream = openStream();
    capture(upstream.response);
    const res = await withTimeout(
      POST(post('{"model":"techsara-35b","input":"hi","stream":true}', { authorization: KEY }), ctx('responses')),
      'the response',
    );
    expect(res.headers.get('content-type')).toBe('text/event-stream; charset=utf-8');
    // The one that matters in production: an intermediary that buffers a
    // text/event-stream holds every token until the end, which looks exactly
    // like a hung model.
    expect(res.headers.get('x-accel-buffering')).toBe('no');
    expect(res.headers.get('cache-control')).toBe('no-store, no-cache, no-transform');
    expect(res.headers.get('content-length')).toBeNull();
    upstream.close();
  });
});

// ---------------------------------------------------------------------------
// 4. The status and the headers a client retries on (§9, §12)
// ---------------------------------------------------------------------------

describe('the upstream answer', () => {
  it.each([200, 201, 202, 400, 401, 403, 404, 409, 413, 422, 429, 500, 503, 504])(
    'relays status %i exactly, never collapsing it onto a proxy status',
    async (status) => {
      capture(() =>
        new Response(JSON.stringify({ error: { code: 'x' } }), {
          status,
          headers: { 'content-type': 'application/json', 'retry-after': '7' },
        }),
      );
      const res = await GET(
        new Request('http://localhost:3001/v1/models', { headers: { authorization: KEY } }),
        ctx('models'),
      );
      expect(res.status).toBe(status);
    },
  );

  it('keeps a 429 a 429, with its Retry-After and its RateLimit headers', async () => {
    // A throttled client that cannot see these can only guess, and guessing
    // is how a rate limit turns into a retry storm.
    capture(() =>
      new Response(JSON.stringify({ error: { code: 'rate_limit_error' } }), {
        status: 429,
        headers: {
          'content-type': 'application/json',
          'retry-after': '12',
          ratelimit: '"requests";r=0;t=12',
          'ratelimit-policy': '"requests";q=60;w=60',
          'x-ratelimit-remaining-requests': '0',
          'x-request-id': 'req_abc123',
        },
      }),
    );
    const res = await POST(post('{"model":"techsara-35b","input":"hi"}', { authorization: KEY }), ctx('responses'));
    expect(res.status).toBe(429);
    expect(res.headers.get('retry-after')).toBe('12');
    expect(res.headers.get('ratelimit')).toBe('"requests";r=0;t=12');
    expect(res.headers.get('ratelimit-policy')).toBe('"requests";q=60;w=60');
    expect(res.headers.get('x-ratelimit-remaining-requests')).toBe('0');
    expect(res.headers.get('x-request-id')).toBe('req_abc123');
    await expect(res.json()).resolves.toEqual({ error: { code: 'rate_limit_error' } });
  });

  it('keeps the WWW-Authenticate a 403 carries, so a caller learns which scope it lacks', async () => {
    // RFC 6750 §3. The orchestrator names the missing scope in this header and
    // the authentication page documents it; the edge dropped it until the
    // documentation was executed against it (2026-09-13).
    capture(() =>
      new Response(JSON.stringify({ error: { code: 'insufficient_scope' } }), {
        status: 403,
        headers: {
          'content-type': 'application/json',
          'www-authenticate': 'Bearer error="insufficient_scope", scope="responses.write"',
        },
      }),
    );
    const res = await POST(post('{"model":"techsara-35b","input":"hi"}', { authorization: KEY }), ctx('responses'));
    expect(res.status).toBe(403);
    expect(res.headers.get('www-authenticate')).toBe('Bearer error="insufficient_scope", scope="responses.write"');
  });

  it('relays the CORS answer the orchestrator gave a preflight', async () => {
    // Next answers OPTIONS itself when a route does not export one — with an
    // Allow header and no CORS headers at all, which fails every browser
    // preflight. CONTRACT §3 puts that answer in the orchestrator.
    const calls = capture(() =>
      new Response(null, {
        status: 204,
        headers: {
          'access-control-allow-origin': 'https://customer.example',
          'access-control-allow-methods': 'GET, POST, OPTIONS',
          'access-control-allow-headers': 'authorization, content-type, idempotency-key',
          'access-control-max-age': '600',
          vary: 'Origin',
        },
      }),
    );
    const res = await OPTIONS(
      new Request('http://localhost:3001/v1/responses', {
        method: 'OPTIONS',
        headers: {
          origin: 'https://customer.example',
          'access-control-request-method': 'POST',
          'access-control-request-headers': 'authorization, idempotency-key',
        },
      }),
      ctx('responses'),
    );
    expect(res.status).toBe(204);
    expect(res.headers.get('access-control-allow-origin')).toBe('https://customer.example');
    expect(res.headers.get('access-control-allow-methods')).toBe('GET, POST, OPTIONS');
    expect(res.headers.get('access-control-max-age')).toBe('600');
    // The preflight's own headers had to reach the server for it to answer.
    const sent = sentHeaders(calls[0]);
    expect(sent['access-control-request-method']).toBe('POST');
    expect(sent.origin).toBe('https://customer.example');
  });

  it('does not relay Access-Control-Allow-Credentials, even if upstream sent one', async () => {
    // CONTRACT §3: it is never sent, so no browser can be told to attach a
    // cookie to /v1. "Never" has to include "never relayed".
    capture(() =>
      new Response('{}', {
        status: 200,
        headers: {
          'content-type': 'application/json',
          'access-control-allow-origin': 'https://customer.example',
          'access-control-allow-credentials': 'true',
        },
      }),
    );
    const res = await GET(
      new Request('http://localhost:3001/v1/models', { headers: { authorization: KEY } }),
      ctx('models'),
    );
    expect(res.headers.get('access-control-allow-credentials')).toBeNull();
    expect(RESPONSE_HEADER_ALLOWLIST as readonly string[]).not.toContain(
      'access-control-allow-credentials',
    );
  });

  it('never relays a Set-Cookie down to an API caller', async () => {
    capture(() =>
      new Response('{}', {
        status: 200,
        headers: {
          'content-type': 'application/json',
          'set-cookie': 'ts_session=leaked; Path=/; HttpOnly',
        },
      }),
    );
    const res = await GET(
      new Request('http://localhost:3001/v1/models', { headers: { authorization: KEY } }),
      ctx('models'),
    );
    expect(res.headers.get('set-cookie')).toBeNull();
  });

  it('does not mistake a 304 for a redirect', async () => {
    // 304 is in the 3xx range and carries no Location: it is a cache
    // validator, not a redirect, and refusing it would be this edge inventing
    // a failure out of an ordinary answer.
    capture(() => new Response(null, { status: 304 }));
    const res = await GET(
      new Request('http://localhost:3001/v1/models', {
        headers: { authorization: KEY, 'if-none-match': 'W/"abc"' },
      }),
      ctx('models'),
    );
    expect(res.status).toBe(304);
    expect(res.body).toBeNull();
  });

  it('answers a 204 without trying to give it a body', async () => {
    // `new Response(bytes, {status: 204})` throws, which would turn a legal
    // empty answer into a 500 from this process.
    capture(() => new Response(null, { status: 204 }));
    const res = await POST(
      post('{"model":"techsara-35b","input":"hi"}', { authorization: KEY }),
      ctx('responses', 'resp_1', 'cancel'),
    );
    expect(res.status).toBe(204);
    expect(res.body).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 5. The body cap (§8, §12) — refused here, and never at the server's expense
// ---------------------------------------------------------------------------

describe('the request body cap', () => {
  it('is the contract’s 1 MiB by default, and reads the server’s own setting', async () => {
    expect(DEFAULT_PUBLIC_API_BODY_BYTES).toBe(1024 * 1024);
    expect(publicApiBodyBytes()).toBe(1024 * 1024);
    // The same environment variable app/publicapi/models.py reads, so the two
    // halves of the cap cannot drift apart.
    vi.stubEnv('PUBLIC_API_MAX_BODY_BYTES', '2048');
    expect(publicApiBodyBytes()).toBe(2048);
    vi.stubEnv('PUBLIC_API_MAX_BODY_BYTES', 'nonsense');
    expect(publicApiBodyBytes()).toBe(1024 * 1024);
  });

  it('refuses a declared oversize body with 413 before the orchestrator is called', async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
    vi.stubEnv('PUBLIC_API_MAX_BODY_BYTES', '1024');
    const headers = new Headers();
    headers.set('content-type', 'application/json');
    headers.set('authorization', KEY);
    headers.set('content-length', String(1024 * 64));
    const res = await POST(
      new Request('http://localhost:3001/v1/responses', {
        method: 'POST',
        headers,
        body: 'x'.repeat(64),
      }),
      ctx('responses'),
    );
    expect(res.status).toBe(413);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('refuses a body that declares no length and then runs over the cap', async () => {
    // The branch that actually matters: an attacker wanting to spend this
    // process’s memory simply omits Content-Length, so the count has to run
    // over the bytes that arrive rather than over what the caller claimed.
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
    vi.stubEnv('PUBLIC_API_MAX_BODY_BYTES', '1024');
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        for (let i = 0; i < 8; i += 1) controller.enqueue(encoder.encode('x'.repeat(512)));
        controller.close();
      },
    });
    const req = new Request('http://localhost:3001/v1/responses', {
      method: 'POST',
      headers: { 'content-type': 'application/json', authorization: KEY },
      body,
      // undici requires this when a request body is a stream.
      duplex: 'half',
    } as RequestInit & { duplex: 'half' });
    expect(req.headers.get('content-length')).toBeNull();
    const res = await POST(req, ctx('responses'));
    expect(res.status).toBe(413);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('answers the 413 in the contract’s error envelope, not a proxy’s own shape', async () => {
    vi.stubGlobal('fetch', vi.fn());
    vi.stubEnv('PUBLIC_API_MAX_BODY_BYTES', '1024');
    const headers = new Headers();
    headers.set('content-type', 'application/json');
    headers.set('content-length', String(1024 * 64));
    const res = await POST(
      new Request('http://localhost:3001/v1/responses', {
        method: 'POST',
        headers,
        body: 'x'.repeat(64),
      }),
      ctx('responses'),
    );
    const body = (await res.json()) as { error: Record<string, unknown> };
    // Five keys, always present (CONTRACT §9), and the code/type pairing of
    // app/publicapi/errors.py.
    expect(Object.keys(body.error).sort()).toEqual([
      'code',
      'message',
      'param',
      'request_id',
      'type',
    ]);
    expect(body.error.code).toBe('request_too_large');
    expect(body.error.type).toBe('invalid_request_error');
    expect(body.error.param).toBeNull();
    // No invented correlation id: this request reached no other system, and an
    // id that appears nowhere sends a support conversation hunting a ghost.
    expect(body.error.request_id).toBeNull();
    expect(res.headers.get('x-request-id')).toBeNull();
  });

  it('lets a body at the cap through untouched', async () => {
    const calls = capture(() => Response.json({ id: 'resp_1' }));
    vi.stubEnv('PUBLIC_API_MAX_BODY_BYTES', '1024');
    const payload = JSON.stringify({ model: 'techsara-35b', input: 'x'.repeat(900) });
    expect(payload.length).toBeLessThanOrEqual(1024);
    const res = await POST(post(payload, { authorization: KEY }), ctx('responses'));
    expect(res.status).toBe(200);
    expect(decoder.decode(calls[0].init.body as ArrayBuffer)).toBe(payload);
  });
});

// ---------------------------------------------------------------------------
// 6. A client that goes away
// ---------------------------------------------------------------------------

describe('an abandoned request', () => {
  it('aborts the upstream call and answers 499', async () => {
    // Asserting the COMPOSITION, not the type: a signal that is merely "an
    // AbortSignal" can be a ceiling of the proxy’s own, and a refactor that
    // dropped the caller’s would leave the generation running for nobody —
    // paid for, admitted into a NORMAL lane, and read by no one.
    let seen: AbortSignal | undefined;
    vi.stubGlobal('fetch', (_url: string | URL, init?: RequestInit) => {
      seen = init?.signal ?? undefined;
      return new Promise<Response>((_resolve, reject) => {
        init?.signal?.addEventListener('abort', () => {
          const err = new Error('aborted');
          err.name = 'AbortError';
          reject(err);
        });
      });
    });
    const controller = new AbortController();
    const req = new Request('http://localhost:3001/v1/responses', {
      method: 'POST',
      body: '{"model":"techsara-35b","input":"hi"}',
      headers: { 'content-type': 'application/json', authorization: KEY },
      signal: controller.signal,
    });
    const pending = POST(req, ctx('responses'));
    await waitUntil(() => seen !== undefined);
    expect(seen!.aborted).toBe(false);

    controller.abort();
    const res = await withTimeout(pending, 'the 499');
    expect(seen!.aborted).toBe(true);
    expect(res.status).toBe(499);
    expect(res.body).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 8. Nothing about the inside gets out (§9)
// ---------------------------------------------------------------------------

describe('what a failure discloses', () => {
  it('names no orchestrator host when the service cannot be reached', async () => {
    vi.stubEnv('ORCHESTRATOR_URL', 'http://orchestrator.internal:8080');
    vi.stubGlobal('fetch', async () => {
      throw new TypeError('fetch failed: connect ECONNREFUSED 172.18.0.4:8080');
    });
    const res = await POST(post('{"model":"techsara-35b","input":"hi"}', { authorization: KEY }), ctx('responses'));
    // 502 is not in CONTRACT §9’s closed table, so it is not an answer this
    // API may give; model_unavailable is, and it is retryable.
    expect(res.status).toBe(503);
    expect(res.headers.get('retry-after')).toBe('30');
    const text = await res.text();
    expect(text).not.toContain('orchestrator.internal');
    expect(text).not.toContain('172.18.0.4');
    expect(text).not.toContain('ECONNREFUSED');
    expect(JSON.parse(text).error.code).toBe('model_unavailable');
    expect(JSON.parse(text).error.type).toBe('service_unavailable_error');
  });

  it('refuses to relay a redirect, which could only point at something inside', async () => {
    capture(() =>
      new Response(null, {
        status: 302,
        headers: { location: 'http://vllm:8000/v1/responses' },
      }),
    );
    const res = await GET(
      new Request('http://localhost:3001/v1/models', { headers: { authorization: KEY } }),
      ctx('models'),
    );
    expect(res.status).toBe(500);
    expect(res.headers.get('location')).toBeNull();
    const body = (await res.json()) as { error: { code: string } };
    expect(body.error.code).toBe('internal_error');
    expect(JSON.stringify(body)).not.toContain('vllm');
  });

  it('logs the unreachable service server-side, where an engineer may read it', async () => {
    vi.stubEnv('ORCHESTRATOR_URL', 'http://orchestrator.internal:8080');
    vi.stubGlobal('fetch', async () => {
      throw new TypeError('fetch failed', {
        cause: { code: 'ECONNREFUSED', message: 'connect to orchestrator.internal:8080' },
      });
    });
    await POST(post('{"model":"techsara-35b","input":"hi"}', { authorization: KEY }), ctx('responses'));
    const log = errors.join('\n');
    expect(log).toContain('route="/v1"');
    // The undici code is the part that says WHICH failure it was — a refused
    // socket and a headers timeout are different problems with different fixes.
    expect(log).toContain('ECONNREFUSED');
    // Neither the key nor the URL that failed: a log line is exactly the kind
    // of thing that gets pasted into a ticket.
    expect(log).not.toContain('tsk_live_');
    expect(log).not.toContain('orchestrator.internal');
  });
});

// ---------------------------------------------------------------------------
// The path itself
// ---------------------------------------------------------------------------

describe('the upstream path', () => {
  it('keeps the /v1 prefix, the segments and the query string', () => {
    expect(upstreamPathFor(['responses'], '')).toBe('/v1/responses');
    expect(upstreamPathFor(['responses', 'resp_1', 'cancel'], '')).toBe(
      '/v1/responses/resp_1/cancel',
    );
    expect(upstreamPathFor(['usage'], '?start=2026-09-01&end=2026-09-13')).toBe(
      '/v1/usage?start=2026-09-01&end=2026-09-13',
    );
  });

  it('is /v1 itself when the optional catch-all matched no segment', () => {
    // The bare path is a real request — the residual the wave-1 verifier found
    // was that middleware redirected it to /login.
    expect(upstreamPathFor([], '')).toBe('/v1');
  });

  it('re-encodes a segment so nothing can smuggle a separator upstream', () => {
    // Next has already percent-decoded these; re-encoding is what stops
    // `resp_1/../../admin/api/members` becoming a different URL upstream.
    expect(upstreamPathFor(['responses', 'resp_1/../../admin'], '')).toBe(
      '/v1/responses/resp_1%2F..%2F..%2Fadmin',
    );
  });

  it('sends the request to the orchestrator’s /v1 surface', async () => {
    const calls = capture(() => Response.json({ data: [] }));
    await GET(
      new Request('http://localhost:3001/v1/models?limit=5', { headers: { authorization: KEY } }),
      ctx('models'),
    );
    expect(calls[0].url).toBe('http://orchestrator:8080/v1/models?limit=5');
  });
});
