/**
 * The backend-for-frontend's security behaviour (2026-09-12), ahead of the
 * developer platform putting a quota-bearing API and a capability-gated
 * console behind it (docs/developer-platform/CONTRACT.md).
 *
 * Seven things are pinned here, each of which was wrong or missing before:
 *
 *  1. a caller cannot choose the address the orchestrator audits and throttles
 *     by — no client-supplied forwarding header survives the hop, and the one
 *     this deployment does forward is named by configuration and shape-checked;
 *  2. Retry-After and the RateLimit headers reach the browser, because a
 *     rate-limited client that cannot see them can only guess;
 *  3. the upstream call carries the caller's abort signal and a ceiling of its
 *     own, so a closed tab and a wedged engine both stop costing something;
 *  4. bodies travel as bytes — the old UTF-8 round trip replaced every byte
 *     that was not valid UTF-8 with U+FFFD;
 *  5. /api/chat and /api/upload bound the body before reading it, at caps
 *     derived from what the client can actually send;
 *  6. the middleware matcher excludes the route-handler NAMESPACE (/api/) and
 *     not every path beginning with the letters "api", so the console page at
 *     /api is gated — and, since 2026-09-13, it excludes the PUBLIC API
 *     namespace as a whole, its bare root /v1 included, which the trailing
 *     slash in `v1/` used to leave cookie-gated (CONTRACT §1);
 *  7. /docs sits behind the session gate at every depth (CONTRACT §17).
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { authRedirect } from '../lib/auth';
import {
  BodyTooLargeError,
  boundedBodyStream,
  declaredBodyOverLimit,
  isBodyTooLarge,
  isIpLiteral,
  proxyToOrchestrator,
  readBoundedBody,
  trustedClientIp,
  trustedForwardedProto,
} from '../lib/proxy';
import { CHUNK_PART_BYTES, CHUNK_THRESHOLD_BYTES } from '../lib/uploadDocument';
import { config } from '../middleware';

interface Call {
  url: string;
  init: RequestInit;
}

/** Capture what reaches fetch, answering with `response()` each time. */
function capture(response: () => Response): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal('fetch', async (url: string | URL, init?: RequestInit) => {
    calls.push({ url: String(url), init: init ?? {} });
    return response();
  });
  return calls;
}

const json = (body: unknown, status = 200, headers: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json', ...headers },
  });

const sentHeaders = (call: Call) => call.init.headers as Record<string, string>;

beforeEach(() => {
  vi.stubEnv('MOCK_MODE', 'false');
  vi.stubEnv('ORCHESTRATOR_URL', 'http://orchestrator:8080');
  // The default posture: no ingress is trusted. Every deployment except the
  // Cloudflare tunnel runs this way, so it is what most of this file holds
  // the proxy to.
  vi.stubEnv('TRUSTED_CLIENT_IP_HEADER', '');
  vi.stubEnv('TRUSTED_FORWARDED_PROTO', '');
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

// ---------------------------------------------------------------------------
// 1. The caller does not get to say where it came from
//
// Production runs the orchestrator with AUTH_TRUST_PROXY_HEADERS=true, so the
// X-Forwarded-For this proxy sends becomes the audit trail's idea of "where
// from" AND the key the login throttle locks out. The old proxy copied that
// value straight off the inbound request.
// ---------------------------------------------------------------------------

/** Every forwarding header a caller might try, all set at once. */
const FORGED = {
  'cf-connecting-ip': '198.51.100.7',
  'x-forwarded-for': '198.51.100.8',
  'x-real-ip': '198.51.100.9',
  'true-client-ip': '198.51.100.10',
  'x-client-ip': '198.51.100.11',
  forwarded: 'for=198.51.100.12',
  'x-forwarded-proto': 'https',
  'x-forwarded-host': 'evil.example',
};

describe('forwarded identity — nothing the caller sent survives the hop', () => {
  it('forwards no forwarding header at all when no ingress is trusted', async () => {
    const calls = capture(() => json({ ok: true }));
    await proxyToOrchestrator(
      new Request('http://localhost:3001/api/auth/me', {
        headers: { ...FORGED, cookie: 'ts_session=abc', 'user-agent': 'Firefox' },
      }),
      '/auth/me',
    );
    const headers = sentHeaders(calls[0]);
    for (const name of Object.keys(FORGED)) {
      expect(headers[name]).toBeUndefined();
    }
    // The two headers that were always legitimate still go.
    expect(headers.cookie).toBe('ts_session=abc');
    expect(headers['user-agent']).toBe('Firefox');
  });

  it('does not launder a forged Cf-Connecting-IP into X-Forwarded-For', async () => {
    // The P1 itself: this exact request used to reach the orchestrator as
    // `x-forwarded-for: 198.51.100.7`, chosen by whoever sent it.
    const calls = capture(() => json({ ok: true }));
    await proxyToOrchestrator(
      new Request('http://localhost:3001/api/auth/login', {
        method: 'POST',
        headers: {
          'cf-connecting-ip': '198.51.100.7',
          'content-type': 'application/json',
        },
        body: '{}',
      }),
      '/auth/login',
    );
    expect(sentHeaders(calls[0])['x-forwarded-for']).toBeUndefined();
  });

  it('forwards the named ingress header, and only under one name', async () => {
    vi.stubEnv('TRUSTED_CLIENT_IP_HEADER', 'cf-connecting-ip');
    const calls = capture(() => json({ ok: true }));
    await proxyToOrchestrator(
      new Request('http://localhost:3001/api/auth/me', { headers: FORGED }),
      '/auth/me',
    );
    const headers = sentHeaders(calls[0]);
    expect(headers['x-forwarded-for']).toBe('198.51.100.7');
    // Even the trusted header does not travel under its own name: the
    // orchestrator reads one spelling, and re-sending the rest would hand the
    // next hop the same choice this fix took away.
    expect(headers['cf-connecting-ip']).toBeUndefined();
    expect(headers['x-real-ip']).toBeUndefined();
    expect(headers.forwarded).toBeUndefined();
  });

  it('ignores every other forwarding header once one is trusted', async () => {
    vi.stubEnv('TRUSTED_CLIENT_IP_HEADER', 'cf-connecting-ip');
    const calls = capture(() => json({ ok: true }));
    await proxyToOrchestrator(
      new Request('http://localhost:3001/api/auth/me', {
        // No cf-connecting-ip: this request did not come through the ingress.
        headers: { 'x-forwarded-for': '198.51.100.8', 'x-real-ip': '198.51.100.9' },
      }),
      '/auth/me',
    );
    expect(sentHeaders(calls[0])['x-forwarded-for']).toBeUndefined();
  });

  it('takes the client from the head of a chain and drops the hops', async () => {
    vi.stubEnv('TRUSTED_CLIENT_IP_HEADER', 'x-forwarded-for');
    const calls = capture(() => json({ ok: true }));
    await proxyToOrchestrator(
      new Request('http://localhost:3001/api/auth/me', {
        headers: { 'x-forwarded-for': '203.0.113.9, 10.0.0.1, 10.0.0.2' },
      }),
      '/auth/me',
    );
    expect(sentHeaders(calls[0])['x-forwarded-for']).toBe('203.0.113.9');
  });

  it.each([
    ['a hostname', 'evil.example'],
    // A CR or LF cannot be tested through this path at all: undici refuses to
    // build a header value containing one, which is the layer below this.
    ['a smuggled second token', '203.0.113.9 X-Admin:1'],
    ['an out-of-range quad', '999.1.1.1'],
    ['an address with a port', '203.0.113.9:8080'],
    ['nothing at all', '   '],
  ])('forwards nothing when the trusted header carries %s', async (_what, value) => {
    vi.stubEnv('TRUSTED_CLIENT_IP_HEADER', 'cf-connecting-ip');
    const calls = capture(() => json({ ok: true }));
    const headers = new Headers();
    headers.set('cf-connecting-ip', value);
    await proxyToOrchestrator(
      new Request('http://localhost:3001/api/auth/me', { headers }),
      '/auth/me',
    );
    expect(sentHeaders(calls[0])['x-forwarded-for']).toBeUndefined();
  });

  it('accepts an IPv6 address from the trusted header', () => {
    vi.stubEnv('TRUSTED_CLIENT_IP_HEADER', 'cf-connecting-ip');
    const req = new Request('http://localhost:3001/api/auth/me', {
      headers: { 'cf-connecting-ip': '2001:db8::42' },
    });
    expect(trustedClientIp(req)).toBe('2001:db8::42');
  });

  it.each([
    ['203.0.113.9', true],
    ['0.0.0.0', true],
    ['255.255.255.255', true],
    ['2001:db8::42', true],
    ['::ffff:203.0.113.9', true],
    ['256.0.0.1', false],
    ['203.0.113', false],
    ['localhost', false],
    ['203.0.113.9:8080', false],
    ['2001:db8::42:8080:1:2:3:4:5', false],
    ['203.0.113.9 ', false],
    ['203.0.113.9,10.0.0.1', false],
    ['', false],
  ])('isIpLiteral(%s) is %s', (value, expected) => {
    expect(isIpLiteral(value)).toBe(expected);
  });

  it('states the forwarded scheme from configuration, never from the caller', async () => {
    const calls = capture(() => json({ ok: true }));
    await proxyToOrchestrator(
      new Request('http://localhost:3001/api/auth/me', {
        headers: { 'x-forwarded-proto': 'http' },
      }),
      '/auth/me',
    );
    // A forged `http` would strip Secure off a cookie issued over TLS wherever
    // AUTH_COOKIE_SECURE is left at `auto`.
    expect(sentHeaders(calls[0])['x-forwarded-proto']).toBeUndefined();

    vi.stubEnv('TRUSTED_FORWARDED_PROTO', 'https');
    const withProto = capture(() => json({ ok: true }));
    await proxyToOrchestrator(
      new Request('http://localhost:3001/api/auth/me', {
        headers: { 'x-forwarded-proto': 'http' },
      }),
      '/auth/me',
    );
    expect(sentHeaders(withProto[0])['x-forwarded-proto']).toBe('https');
  });

  it('ignores a configured scheme that is not http or https', () => {
    vi.stubEnv('TRUSTED_FORWARDED_PROTO', 'gopher');
    expect(trustedForwardedProto()).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 2. The headers a rate-limited client needs
// ---------------------------------------------------------------------------

describe('response headers — the allowlist', () => {
  it('relays the retry and rate-limit headers a 429 is useless without', async () => {
    capture(() =>
      json({ detail: 'slow down' }, 429, {
        'retry-after': '30',
        ratelimit: '"requests";r=0;t=30',
        'ratelimit-policy': '"requests";q=60;w=60',
        'x-request-id': 'req_abc123',
        'x-ratelimit-remaining': '0',
        'content-disposition': 'attachment; filename="usage.csv"',
      }),
    );
    const res = await proxyToOrchestrator(
      new Request('http://localhost:3001/api/auth/me'),
      '/auth/me',
    );
    expect(res.status).toBe(429);
    expect(res.headers.get('retry-after')).toBe('30');
    expect(res.headers.get('ratelimit')).toBe('"requests";r=0;t=30');
    expect(res.headers.get('ratelimit-policy')).toBe('"requests";q=60;w=60');
    expect(res.headers.get('x-request-id')).toBe('req_abc123');
    expect(res.headers.get('x-ratelimit-remaining')).toBe('0');
    expect(res.headers.get('content-disposition')).toBe(
      'attachment; filename="usage.csv"',
    );
  });

  it('drops everything not on the list', async () => {
    capture(() =>
      json({ ok: true }, 200, {
        server: 'uvicorn',
        'x-powered-by': 'fastapi',
        'x-internal-host': 'orchestrator.internal',
      }),
    );
    const res = await proxyToOrchestrator(
      new Request('http://localhost:3001/api/auth/me'),
      '/auth/me',
    );
    expect(res.headers.get('server')).toBeNull();
    expect(res.headers.get('x-powered-by')).toBeNull();
    expect(res.headers.get('x-internal-host')).toBeNull();
  });

  it('still relays Set-Cookie, which is the whole reason this proxy exists', async () => {
    capture(() =>
      json({ ok: true }, 200, {
        'set-cookie': 'ts_session=rotated; Path=/; HttpOnly; SameSite=Lax',
      }),
    );
    const res = await proxyToOrchestrator(
      new Request('http://localhost:3001/api/auth/me'),
      '/auth/me',
    );
    expect(res.headers.get('set-cookie')).toContain('ts_session=rotated');
  });

  it('keeps no-store when the orchestrator has no opinion', async () => {
    capture(() => json({ ok: true }));
    const res = await proxyToOrchestrator(
      new Request('http://localhost:3001/api/auth/me'),
      '/auth/me',
    );
    expect(res.headers.get('cache-control')).toBe('no-store');
  });
});

// ---------------------------------------------------------------------------
// 3. Cancellation and a ceiling
// ---------------------------------------------------------------------------

describe('the upstream call is bounded', () => {
  it('passes a signal to the orchestrator', async () => {
    const calls = capture(() => json({ ok: true }));
    await proxyToOrchestrator(
      new Request('http://localhost:3001/api/auth/me'),
      '/auth/me',
    );
    expect(calls[0].init.signal).toBeInstanceOf(AbortSignal);
    expect(calls[0].init.signal?.aborted).toBe(false);
  });

  it('answers 499 when the browser has already gone', async () => {
    const controller = new AbortController();
    controller.abort();
    vi.stubGlobal('fetch', async () => {
      throw new DOMException('The operation was aborted.', 'AbortError');
    });
    const res = await proxyToOrchestrator(
      new Request('http://localhost:3001/api/auth/me', {
        signal: controller.signal,
      }),
      '/auth/me',
    );
    expect(res.status).toBe(499);
    expect(res.body).toBeNull();
  });

  it('answers 504 when the orchestrator never answers', async () => {
    // The wedged-engine shape: the socket is open, the request is accepted,
    // nothing ever comes back. Without a timeout this handler waited forever.
    vi.stubGlobal(
      'fetch',
      (_url: string, init: RequestInit) =>
        new Promise((_resolve, reject) => {
          init.signal?.addEventListener('abort', () =>
            reject(new DOMException('The operation was aborted.', 'AbortError')),
          );
        }),
    );
    const res = await proxyToOrchestrator(
      new Request('http://localhost:3001/api/auth/me'),
      '/auth/me',
      { timeoutMs: 10 },
    );
    expect(res.status).toBe(504);
  });

  it('still answers 502 for a refused socket', async () => {
    vi.stubGlobal('fetch', async () => {
      throw new Error('connect ECONNREFUSED');
    });
    const res = await proxyToOrchestrator(
      new Request('http://localhost:3001/api/auth/me'),
      '/auth/me',
    );
    expect(res.status).toBe(502);
  });
});

// ---------------------------------------------------------------------------
// 4. Bodies are bytes
// ---------------------------------------------------------------------------

describe('request bodies survive the proxy byte for byte', () => {
  it('forwards bytes that are not valid UTF-8 unchanged', async () => {
    // 0xFF and a truncated two-byte sequence: `await req.text()` turns each of
    // these into U+FFFD, so the old proxy delivered something other than what
    // the browser sent.
    const bytes = new Uint8Array([0x50, 0x4b, 0x03, 0x04, 0xff, 0xfe, 0xc3, 0x28]);
    const calls = capture(() => json({ ok: true }));
    await proxyToOrchestrator(
      new Request('http://localhost:3001/api/history/x', {
        method: 'PUT',
        body: bytes,
        headers: { 'content-type': 'application/octet-stream' },
      }),
      '/history/x',
    );
    expect(new Uint8Array(calls[0].init.body as ArrayBuffer)).toEqual(bytes);
  });

  it('forwards a JSON body unchanged', async () => {
    const payload = JSON.stringify({ title: 'Résumé — Q3', emoji: '🇬🇧' });
    const calls = capture(() => json({ ok: true }));
    await proxyToOrchestrator(
      new Request('http://localhost:3001/api/history/x', {
        method: 'PUT',
        body: payload,
        headers: { 'content-type': 'application/json' },
      }),
      '/history/x',
    );
    expect(new TextDecoder().decode(calls[0].init.body as ArrayBuffer)).toBe(payload);
  });

  it('sends no body at all for a GET', async () => {
    const calls = capture(() => json({ ok: true }));
    await proxyToOrchestrator(
      new Request('http://localhost:3001/api/auth/me'),
      '/auth/me',
    );
    expect(calls[0].init.body).toBeUndefined();
  });
});

// ---------------------------------------------------------------------------
// 5. Bounded bodies
// ---------------------------------------------------------------------------

describe('the shared bounded reader', () => {
  it('returns the bytes when the body fits', async () => {
    const req = new Request('http://t/x', { method: 'POST', body: 'hello' });
    const read = await readBoundedBody(req, 1024);
    expect(read && new TextDecoder().decode(read)).toBe('hello');
  });

  it('returns null the moment a body goes over, without reading the rest', async () => {
    const req = new Request('http://t/x', { method: 'POST', body: 'x'.repeat(200) });
    expect(await readBoundedBody(req, 50)).toBeNull();
  });

  it('reads an empty body as zero bytes rather than failing', async () => {
    const req = new Request('http://t/x', { method: 'POST' });
    expect((await readBoundedBody(req, 50))?.byteLength).toBe(0);
  });

  it.each([
    ['an oversized declaration', '4096', true],
    ['a declaration that is not a number', 'lots', true],
    ['a declaration under the cap', '10', false],
  ])('declaredBodyOverLimit sees %s', (_what, value, expected) => {
    const headers = new Headers();
    headers.set('content-length', value);
    const req = new Request('http://t/x', { method: 'POST', headers, body: 'x' });
    expect(declaredBodyOverLimit(req, 1024)).toBe(expected);
  });

  it('says nothing when no length is declared', () => {
    const req = new Request('http://t/x', { method: 'POST', body: 'x' });
    expect(declaredBodyOverLimit(req, 1024)).toBe(false);
  });

  it('errors a streamed body past the cap without buffering it', async () => {
    const source = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new Uint8Array(40));
        controller.enqueue(new Uint8Array(40));
        controller.close();
      },
    });
    const reader = boundedBodyStream(source, 50).getReader();
    await reader.read(); // the first 40 bytes pass straight through
    await expect(reader.read()).rejects.toThrow(BodyTooLargeError);
  });

  it('finds the refusal through the wrapper fetch throws', () => {
    const wrapped = new TypeError('fetch failed', {
      cause: new TypeError('terminated', { cause: new BodyTooLargeError(10) }),
    });
    expect(isBodyTooLarge(wrapped)).toBe(true);
    expect(isBodyTooLarge(new TypeError('fetch failed'))).toBe(false);
  });

  it('refuses an oversized body at the proxy with 413', async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
    const res = await proxyToOrchestrator(
      new Request('http://localhost:3001/api/history/x', {
        method: 'PUT',
        body: 'x'.repeat(200),
      }),
      '/history/x',
      { maxBodyBytes: 50 },
    );
    expect(res.status).toBe(413);
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

describe('/api/chat bounds its body', () => {
  const chatRoute = () => import('../app/api/chat/route');

  beforeEach(() => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
  });

  it('refuses a declared body over the cap before any byte is read', async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
    const { POST, MAX_CHAT_BODY_BYTES } = await chatRoute();
    const headers = new Headers();
    headers.set('content-type', 'application/json');
    headers.set('content-length', String(MAX_CHAT_BODY_BYTES + 1));
    const res = await POST(
      new Request('http://localhost:3001/api/chat', {
        method: 'POST',
        headers,
        body: JSON.stringify({ messages: [{ role: 'user', content: 'hi' }] }),
      }),
    );
    expect(res.status).toBe(413);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('still proxies a normal turn', async () => {
    const calls = capture(
      () =>
        new Response('event: done\ndata: {}\n\n', {
          status: 200,
          headers: { 'content-type': 'text/event-stream' },
        }),
    );
    const { POST } = await chatRoute();
    const res = await POST(
      new Request('http://localhost:3001/api/chat', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ messages: [{ role: 'user', content: 'hello' }] }),
      }),
    );
    expect(res.status).toBe(200);
    expect(calls[0].url).toBe('http://orchestrator:8080/chat');
  });

  it('still answers 400 for a body that is not JSON', async () => {
    const { POST } = await chatRoute();
    const res = await POST(
      new Request('http://localhost:3001/api/chat', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: 'not json',
      }),
    );
    expect(res.status).toBe(400);
  });

  it('admits everything the composer can actually attach', async () => {
    const { MAX_CHAT_BODY_BYTES } = await chatRoute();
    // Composer.tsx: five images at MAX_IMAGE_BYTES (10 MiB) plus one document
    // at INLINE_DOC_BYTES (25 MiB), all base64, is what one turn can carry.
    const worstCase = Math.ceil((5 * 10 + 25) * 1024 * 1024 * (4 / 3));
    expect(MAX_CHAT_BODY_BYTES).toBeGreaterThan(worstCase);
  });
});

describe('/api/upload bounds its body', () => {
  const uploadRoute = () => import('../app/api/upload/route');

  it('refuses a declared body over the cap before any byte is read', async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
    const { POST, MAX_UPLOAD_BODY_BYTES } = await uploadRoute();
    const headers = new Headers();
    headers.set('content-type', 'multipart/form-data; boundary=x');
    headers.set('content-length', String(MAX_UPLOAD_BODY_BYTES + 1));
    const res = await POST(
      new Request('http://localhost:3001/api/upload', {
        method: 'POST',
        headers,
        body: 'x',
      }),
    );
    expect(res.status).toBe(413);
    expect(fetchSpy).not.toHaveBeenCalled();
  });

  it('answers 413, not 502, when the body itself runs over', async () => {
    // The refusal comes out of the request stream, so it reaches the handler
    // as a failed fetch and has to be told apart from an unreachable service.
    vi.stubGlobal('fetch', async () => {
      throw new TypeError('fetch failed', { cause: new BodyTooLargeError(10) });
    });
    const { POST } = await uploadRoute();
    const res = await POST(
      new Request('http://localhost:3001/api/upload', {
        method: 'POST',
        headers: { 'content-type': 'multipart/form-data; boundary=x' },
        body: 'x'.repeat(20),
      }),
    );
    expect(res.status).toBe(413);
  });

  it('still streams a normal upload through, with its multipart headers', async () => {
    const calls = capture(() => json({ upload_id: 'up_1', files: 1 }));
    const { POST } = await uploadRoute();
    const res = await POST(
      new Request('http://localhost:3001/api/upload', {
        method: 'POST',
        headers: {
          'content-type': 'multipart/form-data; boundary=abc',
          cookie: 'ts_session=abc',
        },
        body: '--abc\r\n\r\n--abc--',
      }),
    );
    expect(res.status).toBe(200);
    expect(calls[0].url).toBe('http://orchestrator:8080/uploads');
    expect(sentHeaders(calls[0])['content-type']).toBe(
      'multipart/form-data; boundary=abc',
    );
    // Still a stream, not a buffer: a 512 MiB dataset must never become one.
    expect(calls[0].init.body).toBeInstanceOf(ReadableStream);
    expect((calls[0].init as { duplex?: string }).duplex).toBe('half');
  });

  it('admits every single-shot upload the client can start', async () => {
    const { MAX_UPLOAD_BODY_BYTES } = await uploadRoute();
    // A dataset never chunks: ChatApp.tsx posts it here whole, up to
    // MAX_DATASET_BYTES (Composer.tsx) = 512 MiB.
    expect(MAX_UPLOAD_BODY_BYTES).toBeGreaterThan(512 * 1024 * 1024);
    // Documents and videos chunk above the threshold and their parts go to
    // /api/upload/chunked/… — both still have to fit if they arrive here.
    expect(MAX_UPLOAD_BODY_BYTES).toBeGreaterThan(CHUNK_THRESHOLD_BYTES);
    expect(MAX_UPLOAD_BODY_BYTES).toBeGreaterThan(CHUNK_PART_BYTES);
  });
});

// ---------------------------------------------------------------------------
// 6. The matcher covers the pages it claims to
//
// `/((?!api|…))` excluded every path whose first characters are "api". The
// console page at /api (CONTRACT §6, capability-gated) would have shipped with
// no edge gate at all.
// ---------------------------------------------------------------------------

/** The matcher as Next compiles it: anchored over the whole pathname. */
const matcher = new RegExp(`^${config.matcher[0]}$`);

describe('the middleware matcher', () => {
  it.each([
    ['/api', true, 'the developer console PAGE — gated'],
    ['/apiary', true, 'an ordinary page that merely starts with those letters'],
    ['/docs', true, 'the developer documentation'],
    ['/docs/guides/webhooks', true, 'and every page under it'],
    ['/admin', true, 'the admin area'],
    ['/login', true, 'sign-in, which the gate bounces the OTHER way'],
    ['/', true, 'the chat itself'],
    ['/docs/quickstart', true, 'the page every reader starts on'],
    ['/apiv1', true, 'and a page that merely begins with those three letters'],
    ['/api/chat', false, 'a route handler answers statuses, never redirects'],
    ['/api/auth/me', false, 'and so does the session probe'],
    ['/v1', false, 'the API ROOT — the optional catch-all serves it'],
    ['/v1/', false, 'with or without the trailing slash'],
    ['/v1/responses', false, 'the public API reads a bearer key and no cookie'],
    ['/v1/models', false, 'all of it'],
    ['/_next/static/x.js', false, 'the build output'],
    ['/favicon.ico', false, 'a dotted asset, which must load on /login'],
  ])('%s is gated: %s', (path, gated) => {
    expect(matcher.test(path)).toBe(gated);
  });

  it('excludes the bare /v1, which the trailing slash used to let through', () => {
    // The wave-1 residual (2026-09-13). `v1/` excluded everything UNDER the
    // namespace and not the namespace itself, so /v1 — served by the optional
    // catch-all at app/v1/[[...path]]/route.ts — reached the page gate. That
    // made the one surface CONTRACT §1 calls cookie-blind behave DIFFERENTLY
    // depending on whether a session cookie was present: a redirect to /login
    // without one, a pass with one.
    expect(matcher.test('/v1')).toBe(false);
    expect(authRedirect('/v1', false)).toBeNull();
    expect(authRedirect('/v1', true)).toBeNull();
  });

  it('agrees with authRedirect, which re-checks the same exclusions', () => {
    // The console page: matched by the middleware AND refused by the decision.
    expect(authRedirect('/api', false)).toBe('/login');
    // The route handlers and the public API: let through by both.
    expect(authRedirect('/api/chat', false)).toBeNull();
    expect(authRedirect('/v1', false)).toBeNull();
    expect(authRedirect('/v1', true)).toBeNull();
    expect(authRedirect('/v1/responses', false)).toBeNull();
    expect(authRedirect('/v1/responses', true)).toBeNull();
  });
});

// ---------------------------------------------------------------------------
// 7. /docs — signed-in reading, CONTRACT §17
// ---------------------------------------------------------------------------

describe('the developer documentation', () => {
  it.each(['/docs', '/docs/quickstart', '/docs/guides/webhooks'])(
    'lets a signed-in member read %s, at any depth',
    (path) => {
      expect(authRedirect(path, true)).toBeNull();
    },
  );

  it.each(['/docs', '/docs/quickstart', '/docs/guides/webhooks'])(
    'sends a signed-out visitor at %s to sign-in',
    (path) => {
      // Not public: the contract grants the documentation to signed-in people.
      // A PUBLIC_PREFIXES entry would also have split the gate one segment
      // deep, publishing /docs/quickstart and bouncing /docs/guides/webhooks.
      expect(authRedirect(path, false)).toBe('/login');
    },
  );

  it('is a page as far as the matcher is concerned', () => {
    for (const path of ['/docs', '/docs/quickstart', '/docs/guides/webhooks']) {
      expect(matcher.test(path)).toBe(true);
    }
  });
});
