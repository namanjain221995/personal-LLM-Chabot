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
 *
 * And, since the no-timeout design (2026-09-13, revision 2):
 *
 *  9. with V1_GATEWAY_URL set, the request goes to the v1-gateway, and falls
 *     back to the orchestrator only when the gateway provably never saw it;
 * 10. a refused connect before the first byte is retried for ≤110 s, and a
 *     failure after the request was sent is never re-sent, with
 *     `x-should-retry: false` on an unkeyed generation sent without the
 *     gateway (a gateway-tagged one is left for the SDK to retry into its run);
 * 11. no duration timer applies to /v1: the gateway hop's dispatcher has both
 *     undici timers at 0, and the direct path keeps only a 300 s silence limit;
 * 12. audio, file and part bodies stream upstream, capped as they pass, and an
 *     early upstream answer drains the rest instead of cutting the caller;
 * 13. the internal attach protocol (`X-TechSara-*`, `: ts-seq=N`) never
 *     crosses this hop in either direction.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { createHash } from 'node:crypto';
import http from 'node:http';
import type { AddressInfo } from 'node:net';

import {
  BodySource,
  DEFAULT_PUBLIC_API_AUDIO_BODY_BYTES,
  DEFAULT_PUBLIC_API_BODY_BYTES,
  DEFAULT_PUBLIC_API_FILES_MAX_BODY_BYTES,
  DEFAULT_PUBLIC_API_FILES_PART_MAX_BYTES,
  DEFAULT_PUBLIC_API_MEDIA_BODY_BYTES,
  DEFAULT_PUBLIC_API_POOLING_BODY_BYTES,
  DELETE,
  GET,
  OPTIONS,
  POST,
  PUT,
  REQUEST_HEADER_ALLOWLIST,
  RESPONSE_HEADER_ALLOWLIST,
  RESPONSE_HEADER_PREFIXES,
  DEFAULT_V1_EDGE_UPSTREAM_SILENCE_S,
  V1_DISPATCHER_OPTIONS,
  isConnectPhaseError,
  publicApiBodyBytes,
  publicApiBodyBytesFor,
  publicApiBodyRuleFor,
  stripTsSeqComments,
  upstreamPathFor,
  v1DirectDispatcher,
  v1DirectDispatcherOptions,
  v1Dispatcher,
  v1GatewayUrl,
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
  vi.stubEnv('PUBLIC_API_MAX_MEDIA_BODY_BYTES', '');
  vi.stubEnv('PUBLIC_API_MAX_AUDIO_BODY_BYTES', '');
  vi.stubEnv('PUBLIC_API_MAX_POOLING_BODY_BYTES', '');
  vi.stubEnv('PUBLIC_API_FILES_MAX_BODY_BYTES', '');
  vi.stubEnv('PUBLIC_API_FILES_PART_MAX_BYTES', '');
  // The direct path unless a test says otherwise, and no connect retry: a
  // stub that throws ECONNREFUSED must answer now, not after 110 s. The retry
  // has its own tests below.
  vi.stubEnv('V1_GATEWAY_URL', '');
  vi.stubEnv('V1_EDGE_CONNECT_RETRY_S', '0');
  vi.stubEnv('V1_EDGE_CONNECT_RETRY_INTERVAL_S', '');
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
    // /v1/responses takes the MEDIA cap since 2026-09-13 (image input).
    vi.stubEnv('PUBLIC_API_MAX_MEDIA_BODY_BYTES', '1024');
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
    // /v1/responses takes the MEDIA cap since 2026-09-13 (image input).
    vi.stubEnv('PUBLIC_API_MAX_MEDIA_BODY_BYTES', '1024');
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
    // /v1/responses takes the MEDIA cap since 2026-09-13 (image input).
    vi.stubEnv('PUBLIC_API_MAX_MEDIA_BODY_BYTES', '1024');
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
    // /v1/responses takes the MEDIA cap since 2026-09-13 (image input).
    vi.stubEnv('PUBLIC_API_MAX_MEDIA_BODY_BYTES', '1024');
    const payload = JSON.stringify({ model: 'techsara-35b', input: 'x'.repeat(900) });
    expect(payload.length).toBeLessThanOrEqual(1024);
    const res = await POST(post(payload, { authorization: KEY }), ctx('responses'));
    expect(res.status).toBe(200);
    expect(decoder.decode(calls[0].init.body as ArrayBuffer)).toBe(payload);
  });
});

/**
 * 2026-09-13: image input on the generating routes, audio on
 * /v1/audio/transcriptions, 2,048-input embeddings, and the Files API's
 * uploads. One cap for everything would either refuse every real recording at
 * the edge (1 MiB) or hand every JSON route 90 MiB of this process's memory;
 * each path gets the size the orchestrator enforces for it, from the same
 * environment variable.
 */
describe('the per-path body caps', () => {
  it('are 90 MiB for audio, 65 MiB for a file part, 20 MiB for generation, 8 MiB for pooling and 1 MiB elsewhere', () => {
    expect(DEFAULT_PUBLIC_API_AUDIO_BODY_BYTES).toBe(94_371_840);
    expect(DEFAULT_PUBLIC_API_MEDIA_BODY_BYTES).toBe(20_971_520);
    expect(DEFAULT_PUBLIC_API_POOLING_BODY_BYTES).toBe(8_388_608);
    expect(DEFAULT_PUBLIC_API_FILES_MAX_BODY_BYTES).toBe(68_157_440);
    expect(DEFAULT_PUBLIC_API_FILES_PART_MAX_BYTES).toBe(67_108_864);
    expect(publicApiBodyBytesFor(['audio', 'transcriptions'])).toBe(94_371_840);
    expect(publicApiBodyBytesFor(['responses'])).toBe(20_971_520);
    expect(publicApiBodyBytesFor(['chat', 'completions'])).toBe(20_971_520);
    expect(publicApiBodyBytesFor(['embeddings'])).toBe(8_388_608);
    expect(publicApiBodyBytesFor(['rerank'])).toBe(8_388_608);
    expect(publicApiBodyBytesFor(['files'])).toBe(68_157_440);
    expect(publicApiBodyBytesFor(['uploads', 'upload_1', 'parts'])).toBe(68_157_440);
    expect(publicApiBodyBytesFor(['uploads', 'upload_1', 'parts', '3'], 'PUT')).toBe(67_108_864);
    // Exact paths and methods, not prefixes: a cancel carries no image, the
    // upload's own creation and completion are small JSON.
    expect(publicApiBodyBytesFor(['responses', 'resp_1', 'cancel'])).toBe(1024 * 1024);
    expect(publicApiBodyBytesFor(['audio'])).toBe(1024 * 1024);
    expect(publicApiBodyBytesFor(['uploads'])).toBe(1024 * 1024);
    expect(publicApiBodyBytesFor(['uploads', 'upload_1', 'complete'])).toBe(1024 * 1024);
    expect(publicApiBodyBytesFor(['uploads', 'upload_1', 'parts', '3'], 'POST')).toBe(1024 * 1024);
  });

  it('stream the audio, file and part bodies, buffer the JSON ones, and read nothing on a GET', () => {
    expect(publicApiBodyRuleFor('POST', ['audio', 'transcriptions']).mode).toBe('stream');
    expect(publicApiBodyRuleFor('POST', ['files']).mode).toBe('stream');
    expect(publicApiBodyRuleFor('POST', ['uploads', 'u', 'parts']).mode).toBe('stream');
    expect(publicApiBodyRuleFor('PUT', ['uploads', 'u', 'parts', '0'])).toEqual({
      cap: 67_108_864,
      mode: 'stream',
      requireLength: true,
    });
    for (const path of [['responses'], ['chat', 'completions'], ['embeddings'], ['uploads'], ['uploads', 'u', 'complete']]) {
      expect(publicApiBodyRuleFor('POST', path).mode, path.join('/')).toBe('buffer');
    }
    expect(publicApiBodyRuleFor('DELETE', ['files', 'file-1']).mode).toBe('buffer');
    for (const method of ['GET', 'HEAD', 'OPTIONS']) {
      expect(publicApiBodyRuleFor(method, ['files', 'file-1', 'content']).mode).toBe('none');
    }
  });

  it('read the orchestrator’s own variables, and ignore nonsense', () => {
    vi.stubEnv('PUBLIC_API_MAX_AUDIO_BODY_BYTES', '4096');
    vi.stubEnv('PUBLIC_API_MAX_MEDIA_BODY_BYTES', '2048');
    vi.stubEnv('PUBLIC_API_MAX_POOLING_BODY_BYTES', '3072');
    vi.stubEnv('PUBLIC_API_FILES_MAX_BODY_BYTES', '5120');
    vi.stubEnv('PUBLIC_API_FILES_PART_MAX_BYTES', '6144');
    vi.stubEnv('PUBLIC_API_MAX_BODY_BYTES', '1024');
    expect(publicApiBodyBytesFor(['audio', 'transcriptions'])).toBe(4096);
    expect(publicApiBodyBytesFor(['responses'])).toBe(2048);
    expect(publicApiBodyBytesFor(['embeddings'])).toBe(3072);
    expect(publicApiBodyBytesFor(['files'])).toBe(5120);
    expect(publicApiBodyBytesFor(['uploads', 'u', 'parts', '1'], 'PUT')).toBe(6144);
    expect(publicApiBodyBytesFor(['usage'])).toBe(1024);
    vi.stubEnv('PUBLIC_API_MAX_AUDIO_BODY_BYTES', '-5');
    expect(publicApiBodyBytesFor(['audio', 'transcriptions'])).toBe(94_371_840);
  });

  it('let a 9 MiB image request through to /v1/responses but not to /v1/embeddings', async () => {
    const calls = capture(() => Response.json({ ok: true }));
    const payload = JSON.stringify({ model: 'techsara-35b', input: 'x'.repeat(9 * 1024 * 1024) });

    const accepted = await POST(post(payload, { authorization: KEY }), ctx('responses'));
    const refused = await POST(
      new Request('http://localhost:3001/v1/embeddings', {
        method: 'POST',
        body: payload,
        headers: { 'content-type': 'application/json', authorization: KEY },
      }),
      ctx('embeddings'),
    );

    expect(accepted.status).toBe(200);
    expect(refused.status).toBe(413);
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe('http://orchestrator:8080/v1/responses');
  });
});

// ---------------------------------------------------------------------------
// 12. Streamed bodies: audio, files and upload parts
// ---------------------------------------------------------------------------

/** A fetch stub that reads a streamed body the way undici does, and fails
 * the way undici fails when the stream errors. */
function captureStreaming(respond: (body: Uint8Array) => Response | Promise<Response>) {
  const calls: (Call & { bytes?: Uint8Array })[] = [];
  vi.stubGlobal('fetch', async (url: string | URL, init?: RequestInit) => {
    const call: Call & { bytes?: Uint8Array } = { url: String(url), init: init ?? {} };
    calls.push(call);
    const body = init?.body;
    let bytes = new Uint8Array(0);
    if (body instanceof ReadableStream) {
      try {
        bytes = new Uint8Array(await new Response(body).arrayBuffer());
      } catch (cause) {
        throw new TypeError('fetch failed', { cause });
      }
    } else if (body instanceof ArrayBuffer) {
      bytes = new Uint8Array(body);
    }
    call.bytes = bytes;
    return respond(bytes);
  });
  return calls;
}

function streamedRequest(
  url: string,
  chunks: Uint8Array[],
  init: { method?: string; headers?: Record<string, string> } = {},
): { req: Request; stats: { reads: number; cancelled: boolean } } {
  const stats = { reads: 0, cancelled: false };
  let index = 0;
  const body = new ReadableStream<Uint8Array>(
    {
      pull(controller) {
        stats.reads += 1;
        if (index < chunks.length) controller.enqueue(chunks[index++]);
        else controller.close();
      },
      cancel() {
        stats.cancelled = true;
      },
    },
    { highWaterMark: 0 },
  );
  const req = new Request(url, {
    method: init.method ?? 'POST',
    headers: init.headers,
    body,
    duplex: 'half',
  } as RequestInit & { duplex: 'half' });
  return { req, stats };
}

describe('a streamed request body', () => {
  it('carries a multipart audio upload byte for byte, boundary and all, as a stream', async () => {
    const calls = captureStreaming(() => Response.json({ text: 'hello', usage: null }));
    const form = new FormData();
    const audio = new Uint8Array(5 * 1024 * 1024);
    for (let i = 0; i < audio.length; i += 997) audio[i] = i % 251;
    form.set('model', 'techsara-whisper');
    form.set('file', new Blob([audio], { type: 'audio/wav' }), 'clip.wav');
    const source = new Request('http://localhost:3001/v1/audio/transcriptions', {
      method: 'POST',
      body: form,
      headers: { authorization: KEY },
    });
    const contentType = source.headers.get('content-type') ?? '';
    const expected = new Uint8Array(await source.clone().arrayBuffer());

    const res = await POST(source, ctx('audio', 'transcriptions'));

    expect(res.status).toBe(200);
    expect(contentType.startsWith('multipart/form-data; boundary=')).toBe(true);
    expect(sentHeaders(calls[0])['content-type']).toBe(contentType);
    // A stream, not a buffer: undici needs `duplex: 'half'` for exactly that.
    expect(calls[0].init.body).toBeInstanceOf(ReadableStream);
    expect((calls[0].init as { duplex?: string }).duplex).toBe('half');
    expect(calls[0].bytes!.byteLength).toBe(expected.byteLength);
    expect(Buffer.compare(Buffer.from(calls[0].bytes!), Buffer.from(expected))).toBe(0);
  });

  it('refuses an upload that declares more than its cap with 413 before the orchestrator is called', async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
    vi.stubEnv('PUBLIC_API_MAX_AUDIO_BODY_BYTES', '4096');
    const { req } = streamedRequest('http://localhost:3001/v1/audio/transcriptions', [new Uint8Array(8)], {
      headers: { authorization: KEY, 'content-length': '8192', 'content-type': 'multipart/form-data; boundary=x' },
    });
    const res = await POST(req, ctx('audio', 'transcriptions'));
    expect(res.status).toBe(413);
    expect(((await res.json()) as { error: { code: string } }).error.code).toBe('request_too_large');
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('refuses an upload that declares nothing and runs over its cap, mid-stream, with the same 413', async () => {
    captureStreaming(() => Response.json({ never: true }));
    vi.stubEnv('PUBLIC_API_MAX_AUDIO_BODY_BYTES', '4096');
    const { req, stats } = streamedRequest(
      'http://localhost:3001/v1/audio/transcriptions',
      Array.from({ length: 16 }, () => new Uint8Array(1024)),
      { headers: { authorization: KEY, 'content-type': 'multipart/form-data; boundary=x' } },
    );
    const res = await POST(req, ctx('audio', 'transcriptions'));
    expect(res.status).toBe(413);
    expect(((await res.json()) as { error: { code: string } }).error.code).toBe('request_too_large');
    // Stopped at the cap: five 1 KiB reads cross 4 KiB, and nothing after.
    expect(stats.reads).toBeLessThanOrEqual(6);
  });

  it('answers a raw part PUT without Content-Length with the orchestrator’s 411, and never calls it', async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
    const { req } = streamedRequest('http://localhost:3001/v1/uploads/upload_1/parts/0', [new Uint8Array(4)], {
      method: 'PUT',
      headers: { authorization: KEY, 'content-type': 'application/octet-stream' },
    });
    const res = await PUT(req, ctx('uploads', 'upload_1', 'parts', '0'));
    expect(res.status).toBe(411);
    const body = (await res.json()) as { error: Record<string, unknown> };
    expect(body.error).toEqual({
      message: 'This endpoint requires a Content-Length header.',
      type: 'invalid_request_error',
      code: 'invalid_request_error',
      param: 'Content-Length',
      request_id: null,
    });
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('forwards a raw part’s declared Content-Length and its digest headers', async () => {
    const calls = captureStreaming((bytes) => Response.json({ received: bytes.byteLength }));
    const part = new Uint8Array(3000).fill(7);
    const { req } = streamedRequest('http://localhost:3001/v1/uploads/upload_1/parts/2', [part.subarray(0, 1000), part.subarray(1000)], {
      method: 'PUT',
      headers: {
        authorization: KEY,
        'content-type': 'application/octet-stream',
        'content-length': '3000',
        'x-part-sha256': 'ab'.repeat(32),
        'content-digest': 'sha-256=:q80=:',
      },
    });
    const res = await PUT(req, ctx('uploads', 'upload_1', 'parts', '2'));
    expect(res.status).toBe(200);
    const sent = sentHeaders(calls[0]);
    expect(sent['content-length']).toBe('3000');
    expect(sent['x-part-sha256']).toBe('ab'.repeat(32));
    expect(sent['content-digest']).toBe('sha-256=:q80=:');
    expect(calls[0].bytes!.byteLength).toBe(3000);
  });

  it('drains the rest of the caller’s body after an early 401, instead of cancelling it', async () => {
    // Files design §12.3, measured: cancelling makes openai-node report
    // "Connection error" and retry the part three times; draining lets it
    // read the 401.
    vi.stubGlobal('fetch', async () =>
      Response.json({ error: { code: 'invalid_api_key' } }, { status: 401 }),
    );
    const chunks = Array.from({ length: 40 }, () => new Uint8Array(1024));
    const { req, stats } = streamedRequest('http://localhost:3001/v1/files', chunks, {
      headers: { 'content-type': 'multipart/form-data; boundary=x' },
    });
    const res = await POST(req, ctx('files'));
    expect(res.status).toBe(401);
    // Drained at once, before anyone reads the answer: the caller's SDK is
    // still sending, and only reads the envelope once its body is accepted.
    await waitUntil(() => stats.reads >= 41);
    expect(stats.cancelled).toBe(false);
    await res.text();
  });

  it('drains what an upstream left unread once its successful answer has been relayed', async () => {
    // Rare, but a 2xx can also arrive before the body is done; the caller's
    // connection must still be read to its end rather than left stalled.
    vi.stubGlobal('fetch', async () => Response.json({ id: 'upload_1', object: 'upload' }));
    const chunks = Array.from({ length: 40 }, () => new Uint8Array(1024));
    const { req, stats } = streamedRequest('http://localhost:3001/v1/uploads/upload_1/parts', chunks, {
      headers: { 'content-type': 'multipart/form-data; boundary=x' },
    });
    const res = await POST(req, ctx('uploads', 'upload_1', 'parts'));
    expect(res.status).toBe(200);
    await res.text();
    await waitUntil(() => stats.reads >= 41);
    expect(stats.cancelled).toBe(false);
  });

  it('streams a 24 MiB part through to a real server with the same sha256', async () => {
    // The real transport, not a stub: undici, the /v1 dispatcher and a node
    // http server that hashes what arrives.
    const received = createHash('sha256');
    let length = 0;
    const server = http.createServer((request, response) => {
      request.on('data', (chunk: Buffer) => {
        received.update(chunk);
        length += chunk.length;
      });
      request.on('end', () => {
        response.writeHead(200, { 'content-type': 'application/json' });
        response.end(JSON.stringify({ length, te: request.headers['transfer-encoding'] ?? null }));
      });
    });
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
    try {
      vi.stubEnv('ORCHESTRATOR_URL', `http://127.0.0.1:${(server.address() as AddressInfo).port}`);
      const sent = createHash('sha256');
      const chunks: Uint8Array[] = [];
      for (let i = 0; i < 24; i += 1) {
        const chunk = new Uint8Array(1024 * 1024);
        for (let j = 0; j < chunk.length; j += 4093) chunk[j] = (i * 31 + j) % 256;
        sent.update(chunk);
        chunks.push(chunk);
      }
      const { req } = streamedRequest('http://localhost:3001/v1/uploads/upload_1/parts/0', chunks, {
        method: 'PUT',
        headers: { authorization: KEY, 'content-length': String(24 * 1024 * 1024), 'content-type': 'application/octet-stream' },
      });
      const res = await PUT(req, ctx('uploads', 'upload_1', 'parts', '0'));
      expect(res.status).toBe(200);
      expect(await res.json()).toEqual({ length: 24 * 1024 * 1024, te: null });
      expect(received.digest('hex')).toBe(sent.digest('hex'));
    } finally {
      server.close();
    }
  });
});

describe('the body source', () => {
  it('replays the chunks an attempt took when that attempt never connected', async () => {
    const chunks = [encoder.encode('alpha-'), encoder.encode('beta-'), encoder.encode('gamma')];
    const { req } = streamedRequest('http://x/v1/files', chunks);
    const source = new BodySource(req.body, 1024);
    // An attempt that pulls one chunk and dies, as undici does on ECONNREFUSED.
    const first = source.attempt().getReader();
    expect(decoder.decode((await first.read()).value)).toBe('alpha-');
    expect(source.canReplay).toBe(true);
    const whole = await new Response(source.attempt()).text();
    expect(whole).toBe('alpha-beta-gamma');
  });

  it('refuses to replay once more than a MiB has left the caller’s stream', async () => {
    const { req } = streamedRequest('http://x/v1/files', [new Uint8Array(700_000), new Uint8Array(700_000), new Uint8Array(10)]);
    const source = new BodySource(req.body, 10 * 1024 * 1024);
    const reader = source.attempt().getReader();
    await reader.read();
    await reader.read();
    expect(source.canReplay).toBe(false);
  });

  it('reads everything, replay included, for the buffered path, and returns null over the cap', async () => {
    const { req } = streamedRequest('http://x/v1/responses', [encoder.encode('{"a":'), encoder.encode('1}')]);
    const source = new BodySource(req.body, 64);
    await source.attempt().getReader().read();
    expect(decoder.decode((await source.readAll())!)).toBe('{"a":1}');

    const big = streamedRequest('http://x/v1/responses', [new Uint8Array(40), new Uint8Array(40)]);
    expect(await new BodySource(big.req.body, 64).readAll()).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 9. The v1-gateway
// ---------------------------------------------------------------------------

describe('with V1_GATEWAY_URL set', () => {
  it('relays to the gateway, with the same path, the allowlisted headers and the trusted client address', async () => {
    vi.stubEnv('V1_GATEWAY_URL', 'http://v1-gateway:8090/');
    vi.stubEnv('TRUSTED_CLIENT_IP_HEADER', 'cf-connecting-ip');
    const calls = captureStreaming(() => Response.json({ id: 'resp_1' }));
    const res = await POST(
      post('{"model":"techsara-35b","input":"hi"}', {
        authorization: KEY,
        'cf-connecting-ip': '203.0.113.7',
        cookie: 'ts_session=secret',
        'x-stainless-retry-count': '1',
      }),
      ctx('responses'),
    );
    expect(res.status).toBe(200);
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe('http://v1-gateway:8090/v1/responses');
    // The gateway hop too runs without undici's 300 s timers.
    expect((calls[0].init as { dispatcher?: object }).dispatcher).toBe(v1Dispatcher());
    const sent = sentHeaders(calls[0]);
    expect(sent.authorization).toBe(KEY);
    expect(sent['x-stainless-retry-count']).toBe('1');
    // The gateway reads the caller's address from the same configured header.
    expect(sent['cf-connecting-ip']).toBe('203.0.113.7');
    expect(sent['x-forwarded-for']).toBe('203.0.113.7');
    expect(sent.cookie).toBeUndefined();
    // The gateway buffers and re-sends JSON itself; this hop only streams.
    expect(calls[0].init.body).toBeInstanceOf(ReadableStream);
    expect(decoder.decode(calls[0].bytes)).toBe('{"model":"techsara-35b","input":"hi"}');
  });

  it('ignores a gateway URL that is not an absolute http(s) URL', () => {
    for (const bad of ['v1-gateway:8090', 'ftp://x', 'not a url']) {
      vi.stubEnv('V1_GATEWAY_URL', bad);
      expect(v1GatewayUrl(), bad).toBeNull();
    }
    vi.stubEnv('V1_GATEWAY_URL', '  ');
    expect(v1GatewayUrl()).toBeNull();
  });

  it('falls back to the orchestrator, with the whole body, when the gateway refuses the connection', async () => {
    vi.stubEnv('V1_GATEWAY_URL', 'http://v1-gateway:8090');
    const urls: string[] = [];
    const bodies: string[] = [];
    vi.stubGlobal('fetch', async (url: string | URL, init?: RequestInit) => {
      urls.push(String(url));
      if (String(url).startsWith('http://v1-gateway')) {
        // undici pulls one chunk of a streamed body before it knows the
        // connection failed (measured) — the fallback must still send it.
        await (init!.body as ReadableStream<Uint8Array>).getReader().read();
        throw new TypeError('fetch failed', { cause: { code: 'ENOTFOUND' } });
      }
      const body = init!.body;
      bodies.push(body instanceof ReadableStream ? await new Response(body).text() : decoder.decode(body as ArrayBuffer));
      return Response.json({ ok: true });
    });
    const { req } = streamedRequest('http://localhost:3001/v1/audio/transcriptions', [
      encoder.encode('--x\r\n'),
      encoder.encode('audio-bytes'),
      encoder.encode('\r\n--x--\r\n'),
    ], { headers: { authorization: KEY, 'content-type': 'multipart/form-data; boundary=x' } });
    const res = await POST(req, ctx('audio', 'transcriptions'));
    expect(res.status).toBe(200);
    expect(urls).toEqual(['http://v1-gateway:8090/v1/audio/transcriptions', 'http://orchestrator:8080/v1/audio/transcriptions']);
    expect(bodies).toEqual(['--x\r\naudio-bytes\r\n--x--\r\n']);
    expect(errors.join('\n')).toContain('v1-gateway unreachable, relaying direct');
  });

  it('does not re-send to the orchestrator when the gateway failed after the request was sent, and leaves the SDK free to retry into the durable run', async () => {
    // 2026-09-14, review: the gateway tagged the request, so an identical SDK
    // retry attaches; `x-should-retry: false` belongs to the gateway-less path.
    vi.stubEnv('V1_GATEWAY_URL', 'http://v1-gateway:8090');
    const urls: string[] = [];
    vi.stubGlobal('fetch', async (url: string | URL) => {
      urls.push(String(url));
      throw new TypeError('fetch failed', { cause: { code: 'UND_ERR_SOCKET' } });
    });
    const res = await POST(post('{"model":"techsara-35b","input":"hi"}', { authorization: KEY }), ctx('responses'));
    expect(urls).toHaveLength(1);
    expect(res.status).toBe(503);
    expect(res.headers.get('retry-after')).toBe('30');
    expect(res.headers.get('x-should-retry')).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 10. A refused connect before the first byte
// ---------------------------------------------------------------------------

describe('a refused connect on the direct path', () => {
  const refused = () => new TypeError('fetch failed', { cause: { code: 'ECONNREFUSED' } });

  it('classifies only the failures that prove nothing was sent as connect-phase', () => {
    for (const code of ['ECONNREFUSED', 'ENOTFOUND', 'EAI_AGAIN', 'EHOSTUNREACH', 'ENETUNREACH', 'UND_ERR_CONNECT_TIMEOUT']) {
      expect(isConnectPhaseError(new TypeError('fetch failed', { cause: { code } })), code).toBe(true);
    }
    for (const code of ['ECONNRESET', 'UND_ERR_SOCKET', 'EPIPE', 'ETIMEDOUT', 'UND_ERR_HEADERS_TIMEOUT']) {
      expect(isConnectPhaseError(new TypeError('fetch failed', { cause: { code } })), code).toBe(false);
    }
    // Happy eyeballs: both address families refused, codes on the members.
    const aggregate = new TypeError('fetch failed', {
      cause: Object.assign(new AggregateError([Object.assign(new Error('a'), { code: 'ECONNREFUSED' })]), {}),
    });
    expect(isConnectPhaseError(aggregate)).toBe(true);
    expect(isConnectPhaseError(new Error('no code'))).toBe(false);
  });

  it('retries with the same bytes until the orchestrator answers, and answers once', async () => {
    vi.stubEnv('V1_EDGE_CONNECT_RETRY_S', '5');
    vi.stubEnv('V1_EDGE_CONNECT_RETRY_INTERVAL_S', '0.02');
    const bodies: string[] = [];
    vi.stubGlobal('fetch', async (_url: string | URL, init?: RequestInit) => {
      bodies.push(decoder.decode(init!.body as ArrayBuffer));
      if (bodies.length < 4) throw refused();
      return Response.json({ id: 'resp_1' });
    });
    const payload = '{"model":"techsara-35b","input":"hi"}';
    const res = await POST(post(payload, { authorization: KEY }), ctx('responses'));
    expect(res.status).toBe(200);
    expect(bodies).toEqual([payload, payload, payload, payload]);
  });

  it('gives up inside its budget with 503 Retry-After 30, never past it', async () => {
    vi.stubEnv('V1_EDGE_CONNECT_RETRY_S', '0.5');
    vi.stubEnv('V1_EDGE_CONNECT_RETRY_INTERVAL_S', '0.1');
    let tries = 0;
    vi.stubGlobal('fetch', async () => {
      tries += 1;
      throw refused();
    });
    const started = Date.now();
    const res = await POST(post('{"model":"techsara-35b","input":"hi"}', { authorization: KEY }), ctx('responses'));
    const elapsed = Date.now() - started;
    expect(res.status).toBe(503);
    expect(res.headers.get('retry-after')).toBe('30');
    // A refused connect proves nothing was sent: the SDK may retry it.
    expect(res.headers.get('x-should-retry')).toBeNull();
    expect(elapsed).toBeLessThanOrEqual(600);
    expect(tries).toBeGreaterThanOrEqual(4);
  });

  it('never re-sends a request that may have reached the orchestrator, and marks an unkeyed generation not retryable', async () => {
    vi.stubEnv('V1_EDGE_CONNECT_RETRY_S', '5');
    vi.stubEnv('V1_EDGE_CONNECT_RETRY_INTERVAL_S', '0.02');
    let tries = 0;
    vi.stubGlobal('fetch', async () => {
      tries += 1;
      throw new TypeError('fetch failed', { cause: { code: 'UND_ERR_SOCKET' } });
    });
    const unkeyed = await POST(post('{"model":"techsara-35b","input":"hi"}', { authorization: KEY }), ctx('responses'));
    expect(tries).toBe(1);
    expect(unkeyed.status).toBe(503);
    expect(unkeyed.headers.get('x-should-retry')).toBe('false');

    const keyed = await POST(
      post('{"model":"techsara-35b","input":"hi"}', { authorization: KEY, 'idempotency-key': 'k-1' }),
      ctx('chat', 'completions'),
    );
    expect(keyed.headers.get('x-should-retry')).toBeNull();
    const read = await GET(new Request('http://localhost:3001/v1/models', { headers: { authorization: KEY } }), ctx('models'));
    expect(read.headers.get('x-should-retry')).toBeNull();
    expect(tries).toBe(3);
  });

  it('stops retrying the moment the caller leaves', async () => {
    vi.stubEnv('V1_EDGE_CONNECT_RETRY_S', '30');
    vi.stubEnv('V1_EDGE_CONNECT_RETRY_INTERVAL_S', '1');
    let tries = 0;
    vi.stubGlobal('fetch', async () => {
      tries += 1;
      throw refused();
    });
    const controller = new AbortController();
    const pending = POST(
      new Request('http://localhost:3001/v1/responses', {
        method: 'POST',
        body: '{}',
        headers: { 'content-type': 'application/json' },
        signal: controller.signal,
      }),
      ctx('responses'),
    );
    await waitUntil(() => tries === 1);
    controller.abort();
    const res = await withTimeout(pending, 'the 499');
    expect(res.status).toBe(499);
    expect(tries).toBe(1);
  });

  /**
   * The design's numbers in real time (no-timeout design T5 test 1): refused
   * for 95 s then up → 200; refused for 115 s → 503 at ≤111 s. Two minutes
   * each, so they run only with V1_EDGE_LONG_TESTS=1.
   */
  const long = process.env.V1_EDGE_LONG_TESTS === '1' ? it : it.skip;

  async function listenLater(delayMs: number): Promise<{ port: number; close: () => void }> {
    const probe = http.createServer();
    await new Promise<void>((resolve) => probe.listen(0, '127.0.0.1', resolve));
    const port = (probe.address() as AddressInfo).port;
    await new Promise<void>((resolve) => probe.close(() => resolve()));
    const server = http.createServer((_q, r) => {
      r.writeHead(200, { 'content-type': 'application/json' });
      r.end('{"id":"resp_late"}');
    });
    const timer = setTimeout(() => server.listen(port, '127.0.0.1'), delayMs);
    return { port, close: () => { clearTimeout(timer); server.close(); } };
  }

  long('recovers when the orchestrator comes back after 95 s, with default settings', async () => {
    vi.stubEnv('V1_EDGE_CONNECT_RETRY_S', '');
    const late = await listenLater(95_000);
    try {
      vi.stubEnv('ORCHESTRATOR_URL', `http://127.0.0.1:${late.port}`);
      const started = Date.now();
      const res = await POST(post('{"model":"techsara-35b","input":"hi"}', { authorization: KEY }), ctx('responses'));
      expect(res.status).toBe(200);
      expect(await res.json()).toEqual({ id: 'resp_late' });
      const elapsed = Date.now() - started;
      expect(elapsed).toBeGreaterThanOrEqual(95_000);
      expect(elapsed).toBeLessThan(100_000);
    } finally {
      late.close();
    }
  }, 130_000);

  long('answers 503 Retry-After 30 by 111 s when the orchestrator is gone for 115 s, with default settings', async () => {
    vi.stubEnv('V1_EDGE_CONNECT_RETRY_S', '');
    const late = await listenLater(115_000);
    try {
      vi.stubEnv('ORCHESTRATOR_URL', `http://127.0.0.1:${late.port}`);
      const started = Date.now();
      const res = await POST(post('{"model":"techsara-35b","input":"hi"}', { authorization: KEY }), ctx('responses'));
      const elapsed = Date.now() - started;
      expect(res.status).toBe(503);
      expect(res.headers.get('retry-after')).toBe('30');
      expect(elapsed).toBeGreaterThanOrEqual(100_000);
      expect(elapsed).toBeLessThanOrEqual(111_000);
    } finally {
      late.close();
    }
  }, 130_000);
});

// ---------------------------------------------------------------------------
// 11. No undici timer on /v1
// ---------------------------------------------------------------------------

describe('the /v1 dispatcher', () => {
  it('gives the gateway hop both timers at 0 and the direct path a 300 s silence limit, each with a fresh connection per request', async () => {
    expect(V1_DISPATCHER_OPTIONS).toEqual({ headersTimeout: 0, bodyTimeout: 0, pipelining: 0 });
    expect(DEFAULT_V1_EDGE_UPSTREAM_SILENCE_S).toBe(300);
    expect(v1DirectDispatcherOptions()).toEqual({ headersTimeout: 300_000, bodyTimeout: 300_000, pipelining: 0 });
    vi.stubEnv('V1_EDGE_UPSTREAM_SILENCE_S', '0');
    expect(v1DirectDispatcherOptions()).toEqual({ headersTimeout: 0, bodyTimeout: 0, pipelining: 0 });
    vi.stubEnv('V1_EDGE_UPSTREAM_SILENCE_S', 'soon');
    expect(v1DirectDispatcherOptions().bodyTimeout).toBe(300_000);
    vi.unstubAllEnvs();

    const dispatcher = v1Dispatcher();
    const direct = v1DirectDispatcher();
    expect(dispatcher).toBeDefined();
    expect(direct).toBeDefined();
    expect(direct).not.toBe(dispatcher);
    // The class global fetch itself dispatches through, not a lookalike.
    const global = (globalThis as Record<symbol, object>)[Symbol.for('undici.globalDispatcher.1')];
    expect(dispatcher).toBeInstanceOf(global.constructor);
    expect(direct).toBeInstanceOf(global.constructor);
    expect(v1Dispatcher()).toBe(dispatcher);
    expect(v1DirectDispatcher()).toBe(direct);

    const calls = capture(() => Response.json({ data: [] }));
    await GET(new Request('http://localhost:3001/v1/models', { headers: { authorization: KEY } }), ctx('models'));
    await POST(post('{"model":"techsara-35b","input":"hi"}', { authorization: KEY }), ctx('responses'));
    expect(calls).toHaveLength(2);
    for (const call of calls) expect((call.init as { dispatcher?: object }).dispatcher).toBe(direct);
  });

  /** A real server on the other side of the real transport, for the silence tests. */
  async function withServer(
    handler: http.RequestListener,
    body: (origin: string) => Promise<void>,
  ): Promise<void> {
    const server = http.createServer(handler);
    server.headersTimeout = 0;
    server.requestTimeout = 0;
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
    try {
      await body(`http://127.0.0.1:${(server.address() as AddressInfo).port}`);
    } finally {
      server.closeAllConnections();
      server.close();
    }
  }

  it('answers 503 when the orchestrator sends no first byte within the silence limit on the direct path', async () => {
    vi.stubEnv('V1_EDGE_UPSTREAM_SILENCE_S', '1');
    await withServer(
      () => undefined, // accepts the request and never answers: a stuck event loop
      async (origin) => {
        vi.stubEnv('ORCHESTRATOR_URL', origin);
        const started = Date.now();
        const res = await POST(post('{"model":"techsara-35b","input":"hi"}', { authorization: KEY }), ctx('responses'));
        const elapsed = Date.now() - started;
        expect(res.status).toBe(503);
        expect(res.headers.get('retry-after')).toBe('30');
        // It may have been read: an unkeyed generation must not be re-sent blind.
        expect(res.headers.get('x-should-retry')).toBe('false');
        expect(elapsed).toBeGreaterThanOrEqual(900);
        expect(elapsed).toBeLessThan(5_000);
        expect(errors.join('\n')).toMatch(/HEADERS_TIMEOUT|Headers Timeout/i);
      },
    );
  });

  it('cuts a relayed answer that falls silent for the silence limit on the direct path', async () => {
    vi.stubEnv('V1_EDGE_UPSTREAM_SILENCE_S', '1');
    await withServer(
      (_request, response) => {
        response.writeHead(200, { 'content-type': 'text/event-stream' });
        response.write('data: first\n\n'); // then nothing, with the socket open
      },
      async (origin) => {
        vi.stubEnv('ORCHESTRATOR_URL', origin);
        const res = await POST(post('{"stream":true}', { authorization: KEY }), ctx('responses'));
        expect(res.status).toBe(200);
        const started = Date.now();
        await expect(res.text()).rejects.toThrow();
        expect(Date.now() - started).toBeLessThan(5_000);
      },
    );
  });

  it('does not count a slow streamed upload against the silence limit, and still limits the silence after it', async () => {
    vi.stubEnv('V1_EDGE_UPSTREAM_SILENCE_S', '1');
    const slowUpload = () => {
      // 2.4 s of upload, a chunk every 300 ms: more than twice the limit.
      const body = new ReadableStream<Uint8Array>({
        async start(controller) {
          for (let i = 0; i < 8; i += 1) {
            controller.enqueue(new Uint8Array(1024));
            await new Promise((resolve) => setTimeout(resolve, 300));
          }
          controller.close();
        },
      });
      return new Request('http://localhost:3001/v1/audio/transcriptions', {
        method: 'POST',
        headers: { authorization: KEY, 'content-type': 'multipart/form-data; boundary=x' },
        body,
        duplex: 'half',
      } as RequestInit & { duplex: 'half' });
    };
    let received = 0;
    let answer = true;
    await withServer(
      (request, response) => {
        request.on('data', (chunk: Buffer) => {
          received += chunk.length;
        });
        request.on('end', () => {
          if (!answer) return; // the upload landed, and then nothing: stuck
          response.writeHead(200, { 'content-type': 'application/json' });
          response.end(JSON.stringify({ received }));
        });
      },
      async (origin) => {
        vi.stubEnv('ORCHESTRATOR_URL', origin);
        const res = await POST(slowUpload(), ctx('audio', 'transcriptions'));
        expect(res.status).toBe(200);
        expect(await res.json()).toEqual({ received: 8 * 1024 });

        answer = false;
        received = 0;
        const started = Date.now();
        const stuck = await POST(slowUpload(), ctx('audio', 'transcriptions'));
        expect(stuck.status).toBe(503);
        expect(received).toBe(8 * 1024);
        expect(Date.now() - started).toBeGreaterThanOrEqual(2_400 + 900);
        expect(Date.now() - started).toBeLessThan(8_000);
      },
    );
  }, 20_000);

  it('keeps no silence limit on the gateway hop, which pings and watches the orchestrator itself', async () => {
    vi.stubEnv('V1_EDGE_UPSTREAM_SILENCE_S', '1');
    await withServer(
      (_request, response) => {
        setTimeout(() => {
          response.writeHead(200, { 'content-type': 'application/json' });
          response.end('{"late":true}');
        }, 2_500);
      },
      async (origin) => {
        vi.stubEnv('V1_GATEWAY_URL', origin);
        const res = await POST(post('{"model":"techsara-35b","input":"hi"}', { authorization: KEY }), ctx('responses'));
        expect(res.status).toBe(200);
        expect(await res.text()).toBe('{"late":true}');
      },
    );
  });

  it('opens a new connection for each request, so a keep-alive race can never cut a sent POST', async () => {
    let connections = 0;
    const server = http.createServer((_q, r) => r.end('{}'));
    server.on('connection', () => {
      connections += 1;
    });
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
    try {
      vi.stubEnv('ORCHESTRATOR_URL', `http://127.0.0.1:${(server.address() as AddressInfo).port}`);
      for (let i = 0; i < 3; i += 1) {
        const res = await POST(post('{}', { authorization: KEY }), ctx('responses'));
        await res.text();
      }
      expect(connections).toBe(3);
    } finally {
      server.close();
    }
  });

  /**
   * The point of the dispatchers, in real time: undici's defaults cut both of
   * these at 300 s, and the gateway hop must not; the direct path cuts at its
   * default 300 s and not before. Five minutes each, so V1_EDGE_LONG_TESTS=1 only.
   */
  const long = process.env.V1_EDGE_LONG_TESTS === '1' ? it : it.skip;

  long('cuts a direct-path answer silent for the default 300 s, and not one silent for 290 s', async () => {
    const server = http.createServer((request, response) => {
      response.writeHead(200, { 'content-type': 'text/event-stream' });
      response.write('data: first\n\n');
      if (request.url?.includes('models')) setTimeout(() => response.end('data: second\n\n'), 290_000);
    });
    server.headersTimeout = 0;
    server.requestTimeout = 0;
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
    try {
      vi.stubEnv('ORCHESTRATOR_URL', `http://127.0.0.1:${(server.address() as AddressInfo).port}`);
      const started = Date.now();
      const [quiet, stuck] = await Promise.all([
        GET(new Request('http://localhost:3001/v1/models', { headers: { authorization: KEY } }), ctx('models')),
        POST(post('{"stream":true}', { authorization: KEY }), ctx('responses')),
      ]);
      const settle = (body: Promise<string>) =>
        body.then(
          (text) => ({ ok: true, text, at: Date.now() - started }),
          () => ({ ok: false, text: '', at: Date.now() - started }),
        );
      const [quietOutcome, stuckOutcome] = await Promise.all([settle(quiet.text()), settle(stuck.text())]);
      expect(quietOutcome.ok).toBe(true);
      expect(quietOutcome.text).toBe('data: first\n\ndata: second\n\n');
      expect(stuckOutcome.ok).toBe(false);
      expect(stuckOutcome.at).toBeGreaterThanOrEqual(299_000);
      expect(stuckOutcome.at).toBeLessThan(310_000);
    } finally {
      server.closeAllConnections();
      server.close();
    }
  }, 330_000);

  long('waits 305 s for a first byte, and 305 s of silence mid-body, on the gateway hop without cutting either', async () => {
    const server = http.createServer((request, response) => {
      if (request.url?.startsWith('/v1/models')) {
        setTimeout(() => {
          response.writeHead(200, { 'content-type': 'application/json' });
          response.end('{"late":true}');
        }, 305_000);
        return;
      }
      response.writeHead(200, { 'content-type': 'text/event-stream' });
      response.write('data: first\n\n');
      setTimeout(() => response.end('data: second\n\n'), 305_000);
    });
    server.headersTimeout = 0;
    server.requestTimeout = 0;
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
    try {
      vi.stubEnv('V1_GATEWAY_URL', `http://127.0.0.1:${(server.address() as AddressInfo).port}`);
      const [late, silent] = await Promise.all([
        GET(new Request('http://localhost:3001/v1/models', { headers: { authorization: KEY } }), ctx('models')),
        POST(post('{"stream":true}', { authorization: KEY }), ctx('responses')),
      ]);
      const [lateBody, silentBody] = await Promise.all([late.text(), silent.text()]);
      expect(late.status).toBe(200);
      expect(lateBody).toBe('{"late":true}');
      expect(silentBody).toBe('data: first\n\ndata: second\n\n');
    } finally {
      server.close();
    }
  }, 330_000);
});

// ---------------------------------------------------------------------------
// 13. The internal attach protocol never crosses this hop
// ---------------------------------------------------------------------------

async function stripAll(chunks: string[]): Promise<string> {
  const source = new ReadableStream<Uint8Array>({
    start(controller) {
      for (const chunk of chunks) controller.enqueue(encoder.encode(chunk));
      controller.close();
    },
  });
  return new Response(source.pipeThrough(stripTsSeqComments())).text();
}

describe('the internal attach protocol', () => {
  const tagged =
    'event: response.created\ndata: {"sequence_number":1}\n\n: ts-seq=1\n\n' +
    ': ping\n\n' +
    'event: response.output_text.delta\ndata: {"sequence_number":2,"delta":"a: ts-seq=9"}\n\n: ts-seq=2\n\n' +
    'event: response.completed\ndata: {"sequence_number":3}\n: ts-seq=3\n\n';
  const clean =
    'event: response.created\ndata: {"sequence_number":1}\n\n' +
    ': ping\n\n' +
    'event: response.output_text.delta\ndata: {"sequence_number":2,"delta":"a: ts-seq=9"}\n\n' +
    'event: response.completed\ndata: {"sequence_number":3}\n\n';

  it('strips every ts-seq frame and keeps every other byte, however the stream is split', async () => {
    expect(await stripAll([tagged])).toBe(clean);
    // Split at every single position: a marker cut in half must still go.
    for (let at = 1; at < tagged.length; at += 1) {
      expect(await stripAll([tagged.slice(0, at), tagged.slice(at)]), `split at ${at}`).toBe(clean);
    }
    // One byte at a time.
    expect(await stripAll([...tagged])).toBe(clean);
  });

  it('handles CRLF line breaks, and leaves a comment that only looks similar alone', async () => {
    const crlf = tagged.replace(/\n/g, '\r\n');
    expect(await stripAll([crlf])).toBe(clean.replace(/\n/g, '\r\n'));
    const lookalikes = ': ts-seq=\n\n: ts-seq=12x\n\n:ts-seq=4\n\n: ts-sequence=1\n\ndata: x\n\n';
    expect(await stripAll([lookalikes])).toBe(': ts-seq=\n\n: ts-seq=12x\n\n: ts-sequence=1\n\ndata: x\n\n');
  });

  it('releases a data line before its line break arrives, so no event waits for the next', async () => {
    const reader = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(encoder.encode('data: {"delta":"hel'));
      },
    })
      .pipeThrough(stripTsSeqComments())
      .getReader();
    const first = await withTimeout(reader.read(), 'the partial line');
    expect(decoder.decode(first.value)).toBe('data: {"delta":"hel');
  });

  it('strips ts-seq frames from a relayed stream end to end', async () => {
    capture(() =>
      new Response(tagged, { status: 200, headers: { 'content-type': 'text/event-stream' } }),
    );
    const res = await POST(post('{"stream":true}', { authorization: KEY }), ctx('responses'));
    expect(await res.text()).toBe(clean);
  });

  it('sends no client x-techsara-* header upstream and relays none back', async () => {
    const calls = capture(() =>
      new Response('{}', {
        status: 200,
        headers: {
          'content-type': 'application/json',
          'x-techsara-run': 'resp_4f2b8c1d9e0a7b6c5d4e3f20',
          'x-should-retry': 'false',
        },
      }),
    );
    const res = await POST(
      post('{}', {
        authorization: KEY,
        'x-techsara-attempt': '00000000-0000-4000-8000-000000000000',
        'x-techsara-resume-after': '12',
        'x-techsara-attach-job': 'someone-elses',
      }),
      ctx('responses'),
    );
    for (const name of Object.keys(sentHeaders(calls[0]))) expect(name.startsWith('x-techsara-'), name).toBe(false);
    expect(res.headers.get('x-techsara-run')).toBeNull();
    expect(res.headers.get('x-should-retry')).toBe('false');
    for (const name of [...REQUEST_HEADER_ALLOWLIST, ...RESPONSE_HEADER_ALLOWLIST]) {
      expect(name.startsWith('x-techsara-'), name).toBe(false);
    }
  });

  it('exports the allowlists as plain arrays, for the gateway’s parity test', () => {
    expect(Array.isArray(REQUEST_HEADER_ALLOWLIST)).toBe(true);
    expect(Array.isArray(RESPONSE_HEADER_ALLOWLIST)).toBe(true);
    expect(Array.isArray(RESPONSE_HEADER_PREFIXES)).toBe(true);
    expect(REQUEST_HEADER_ALLOWLIST).toContain('x-stainless-retry-count');
    expect(RESPONSE_HEADER_ALLOWLIST).toContain('x-should-retry');
    for (const forbidden of ['cookie', 'content-length']) {
      expect(REQUEST_HEADER_ALLOWLIST as readonly string[]).not.toContain(forbidden);
    }
    for (const forbidden of ['set-cookie', 'access-control-allow-credentials', 'location', 'content-encoding', 'content-length']) {
      expect(RESPONSE_HEADER_ALLOWLIST as readonly string[]).not.toContain(forbidden);
    }
  });
});

// ---------------------------------------------------------------------------
// Byte downloads (Files design §12.5)
// ---------------------------------------------------------------------------

describe('a byte download', () => {
  it('keeps Content-Length and the range headers on GET /v1/files/{id}/content', async () => {
    const calls = capture(() =>
      new Response(new Uint8Array(100), {
        status: 206,
        headers: {
          'content-type': 'application/octet-stream',
          'content-length': '100',
          'content-range': 'bytes 0-99/5000',
          'accept-ranges': 'bytes',
          etag: '"abc"',
          'content-disposition': 'attachment; filename="report.pdf"',
          'content-security-policy': "sandbox; default-src 'none'",
        },
      }),
    );
    const res = await GET(
      new Request('http://localhost:3001/v1/files/file-6f1c2a9e0b7d4c3a8e5f1b2c/content', {
        headers: { authorization: KEY, range: 'bytes=0-99', 'if-none-match': '"old"' },
      }),
      ctx('files', 'file-6f1c2a9e0b7d4c3a8e5f1b2c', 'content'),
    );
    expect(res.status).toBe(206);
    expect(res.headers.get('content-length')).toBe('100');
    expect(res.headers.get('content-range')).toBe('bytes 0-99/5000');
    expect(res.headers.get('accept-ranges')).toBe('bytes');
    expect(res.headers.get('etag')).toBe('"abc"');
    expect(res.headers.get('content-disposition')).toBe('attachment; filename="report.pdf"');
    expect(sentHeaders(calls[0]).range).toBe('bytes=0-99');
    expect(sentHeaders(calls[0])['if-none-match']).toBe('"old"');
    expect((await res.arrayBuffer()).byteLength).toBe(100);
  });

  it('does not set Content-Length on any other route, where heartbeats may add bytes', async () => {
    capture(() =>
      new Response('{"object":"file"}', {
        status: 200,
        headers: { 'content-type': 'application/json', 'content-length': '17' },
      }),
    );
    const res = await GET(
      new Request('http://localhost:3001/v1/files/file-1', { headers: { authorization: KEY } }),
      ctx('files', 'file-1'),
    );
    expect(res.headers.get('content-length')).toBeNull();
  });

  it('forwards DELETE /v1/files/{id} to the orchestrator', async () => {
    const calls = capture(() => Response.json({ id: 'file-1', object: 'file', deleted: true }));
    const res = await DELETE(
      new Request('http://localhost:3001/v1/files/file-1', { method: 'DELETE', headers: { authorization: KEY } }),
      ctx('files', 'file-1'),
    );
    expect(res.status).toBe(200);
    expect(calls[0].init.method).toBe('DELETE');
    expect(calls[0].url).toBe('http://orchestrator:8080/v1/files/file-1');
  });
});

// ---------------------------------------------------------------------------
// 6. A client that goes away
// ---------------------------------------------------------------------------

describe('an abandoned request', () => {
  it('aborts the upstream call and answers 499', async () => {
    // Asserting the COMPOSITION, not the type: a signal that is merely "an
    // AbortSignal" can be a ceiling of the proxy’s own, and a refactor that
    // dropped the caller’s would leave the generation running for nobody.
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
    // The undici code is the part that says WHICH failure it was.
    expect(log).toContain('ECONNREFUSED');
    // Neither the key nor the URL that failed.
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
    // The resume route's query survives (CONTRACT §10).
    expect(upstreamPathFor(['responses', 'resp_1'], '?stream=true&starting_after=41')).toBe(
      '/v1/responses/resp_1?stream=true&starting_after=41',
    );
  });

  it('is /v1 itself when the optional catch-all matched no segment', () => {
    expect(upstreamPathFor([], '')).toBe('/v1');
  });

  it('re-encodes a segment so nothing can smuggle a separator upstream', () => {
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
