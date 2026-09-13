/**
 * The developer console's BFF (/api/devplatform/*).
 *
 * Three promises are pinned here, in order of how much they would cost to
 * break:
 *
 *  1. IT FORWARDS ONLY WHAT IT KNOWS. The console speaks a fixed list of
 *     operations; anything else is a 404 from this process and never reaches
 *     the network. A proxy that forwards whatever it is handed turns every
 *     future orchestrator route into a console-reachable one by accident.
 *  2. IT NEVER CARRIES A BROWSER-SUPPLIED CREDENTIAL. CONTRACT §1 keeps the
 *     session and the API key on separate surfaces; an Authorization header
 *     arriving at this handler must not be relayed.
 *  3. THE PLAYGROUND ACTUALLY STREAMS. Piped through the buffering proxy, a
 *     generation would arrive in one piece at the end — which is why the
 *     stream test reads a chunk from a response whose upstream stream has NOT
 *     closed. That assertion is the difference between streaming and waiting.
 */
import { existsSync, readFileSync } from 'node:fs';
import { fileURLToPath } from 'node:url';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  CONSOLE_UPSTREAM_BASE,
  DELETE,
  GET,
  PATCH,
  POST,
  PUT,
  consoleOperations,
  consoleQuery,
  consoleRouteAllowed,
} from '@/app/api/devplatform/[...path]/route';
import { consolePaths } from '@/components/devplatform/paths';

const ctx = (...path: string[]) => ({ params: Promise.resolve({ path }) });

interface Call {
  url: string;
  init: RequestInit;
}

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

beforeEach(() => {
  vi.stubEnv('ORCHESTRATOR_URL', 'http://orchestrator:8080');
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

// ---------------------------------------------------------------------------
// The allowlist
// ---------------------------------------------------------------------------

describe('the console proxy forwards only the operations it declares', () => {
  it('accepts every path and method the console actually calls', () => {
    const p = 'proj_0123456789abcdef01234567';
    const k = 'key_0123456789abcdef01234567';
    const w = 'whe_0123456789abcdef01234567';
    const calls: [string, string][] = [
      [consolePaths.overview(), 'GET'],
      [consolePaths.projects(), 'GET'],
      [consolePaths.projects(), 'POST'],
      [consolePaths.project(p), 'PATCH'],
      [consolePaths.keys(p), 'GET'],
      [consolePaths.keys(p), 'POST'],
      [consolePaths.revokeKey(p, k), 'POST'],
      [consolePaths.limits(p), 'GET'],
      [consolePaths.limits(p), 'PUT'],
      [consolePaths.logs(p), 'GET'],
      [consolePaths.webhooks(p), 'GET'],
      [consolePaths.webhooks(p), 'POST'],
      [consolePaths.webhook(p, w), 'PATCH'],
      [consolePaths.webhook(p, w), 'DELETE'],
      [consolePaths.testWebhook(p, w), 'POST'],
      [consolePaths.usage(), 'GET'],
      [consolePaths.models(), 'GET'],
      [consolePaths.model('techsara-35b'), 'PUT'],
      [consolePaths.playground(), 'POST'],
    ];
    for (const [path, method] of calls) {
      expect(consoleRouteAllowed(path.split('/'), method), `${method} ${path}`).toBe(true);
    }
    // And nothing else: every allowlisted operation is one the console calls.
    const used = new Set(calls.map(([path, method]) => `${method} ${path}`));
    const declared = consoleOperations().flatMap((op) =>
      op.methods.map((m) => `${m} ${op.path}`),
    );
    const concrete = (line: string) =>
      [...used].some((u) => {
        const [um, up] = u.split(' ') as [string, string];
        const [dm, dp] = line.split(' ') as [string, string];
        if (um !== dm) return false;
        const a = up.split('/');
        const b = dp.split('/');
        return a.length === b.length && b.every((seg, i) => seg === ':id' || seg === a[i]);
      });
    for (const line of declared) expect(concrete(line), line).toBe(true);
  });

  it('no longer relays the four operations nothing in the console called', () => {
    // The wave-2 review measured a rotate POST reaching the orchestrator from
    // any session with no console caller and no test. Dead surface is gone.
    expect(consoleRouteAllowed(['keys', 'key_abc', 'rotate'], 'POST')).toBe(false);
    expect(consoleRouteAllowed(['projects', 'proj_a', 'keys', 'key_b', 'rotate'], 'POST')).toBe(false);
    expect(consoleRouteAllowed(['logs', 'req_abc'], 'GET')).toBe(false);
    expect(consoleRouteAllowed(['webhooks', 'whe_abc', 'deliveries'], 'GET')).toBe(false);
    expect(consoleRouteAllowed(['settings'], 'GET')).toBe(false);
    // Nor the service-account list or project detail, which no panel reads.
    expect(consoleRouteAllowed(['projects', 'proj_a', 'service-accounts'], 'GET')).toBe(false);
    expect(consoleRouteAllowed(['projects', 'proj_a'], 'GET')).toBe(false);
  });

  it('no longer speaks the paths the orchestrator never served', () => {
    expect(consoleRouteAllowed(['keys'], 'GET')).toBe(false);
    expect(consoleRouteAllowed(['keys', 'key_abc', 'revoke'], 'POST')).toBe(false);
    expect(consoleRouteAllowed(['limits', 'proj_abc'], 'PUT')).toBe(false);
    expect(consoleRouteAllowed(['logs'], 'GET')).toBe(false);
    expect(consoleRouteAllowed(['webhooks'], 'POST')).toBe(false);
    expect(consoleRouteAllowed(['models', 'techsara-35b'], 'PATCH')).toBe(false);
    expect(consoleRouteAllowed(['playground'], 'POST')).toBe(false);
  });

  it('refuses a path the console does not speak', () => {
    expect(consoleRouteAllowed(['members'], 'GET')).toBe(false);
    expect(consoleRouteAllowed(['audit'], 'GET')).toBe(false);
    expect(consoleRouteAllowed([], 'GET')).toBe(false);
    // A real orchestrator surface, and still not this proxy's business.
    expect(consoleRouteAllowed(['analytics', 'export'], 'GET')).toBe(false);
  });

  it('refuses a method that resource does not offer', () => {
    expect(consoleRouteAllowed(['projects'], 'DELETE')).toBe(false);
    expect(consoleRouteAllowed(['usage'], 'POST')).toBe(false);
    expect(consoleRouteAllowed(['playground', 'execute'], 'GET')).toBe(false);
    expect(consoleRouteAllowed(['models'], 'PUT')).toBe(false);
  });

  it('refuses an identifier that is not an identifier', () => {
    expect(consoleRouteAllowed(['projects', '..'], 'PATCH')).toBe(false);
    expect(consoleRouteAllowed(['projects', 'a/b'], 'PATCH')).toBe(false);
    expect(consoleRouteAllowed(['projects', 'x'.repeat(65)], 'PATCH')).toBe(false);
    expect(consoleRouteAllowed(['projects', ''], 'PATCH')).toBe(false);
  });
});

// ---------------------------------------------------------------------------
// The allowlist against the real router
// ---------------------------------------------------------------------------

const CONSOLE_API = fileURLToPath(
  new URL('../../orchestrator/app/apiplatform/console_api.py', import.meta.url),
);

describe('the allowlist and the orchestrator console router', () => {
  it.skipIf(!existsSync(CONSOLE_API))(
    'names every operation exactly as console_api.py routes it',
    () => {
      const source = readFileSync(CONSOLE_API, 'utf8');
      const prefix = /APIRouter\(prefix="([^"]+)"/.exec(source)?.[1];
      expect(prefix).toBe(CONSOLE_UPSTREAM_BASE);

      const upstream = new Set<string>();
      for (const match of source.matchAll(/@router\.(get|post|put|patch|delete)\("([^"]*)"\)/g)) {
        const method = (match[1] as string).toUpperCase();
        const path = (match[2] as string)
          .replace(/^\//, '')
          .split('/')
          .map((seg) => (/^\{[^}]+\}$/.test(seg) ? ':id' : seg))
          .join('/');
        upstream.add(`${method} ${path}`);
      }
      expect(upstream.size).toBeGreaterThan(10);

      for (const op of consoleOperations()) {
        for (const method of op.methods) {
          expect(upstream.has(`${method} ${op.path}`), `${method} ${op.path}`).toBe(true);
        }
      }
    },
  );
});

// ---------------------------------------------------------------------------
// The query allowlist
// ---------------------------------------------------------------------------

describe('the query string the proxy forwards', () => {
  const logs = ['projects', 'proj_1', 'logs'];

  it('rebuilds the query from the parameters the operation declares', () => {
    expect(consoleQuery(logs, 'GET', '?status=failed&limit=100')).toBe(
      '?status=failed&limit=100',
    );
    expect(consoleQuery(['usage'], 'GET', '?project_id=proj_1&days=30')).toBe(
      '?project_id=proj_1&days=30',
    );
    expect(consoleQuery(logs, 'GET', '')).toBe('');
    // An empty value is "not set", and is not forwarded.
    expect(consoleQuery(logs, 'GET', '?status=&limit=100')).toBe('?limit=100');
  });

  it('refuses a value outside the bound the orchestrator declares', () => {
    expect(consoleQuery(logs, 'GET', '?limit=999999999')).toBeNull();
    expect(consoleQuery(logs, 'GET', '?limit=0')).toBeNull();
    expect(consoleQuery(logs, 'GET', '?limit=201')).toBeNull();
    expect(consoleQuery(logs, 'GET', '?limit=-1')).toBeNull();
    expect(consoleQuery(logs, 'GET', '?limit=1e3')).toBeNull();
    expect(consoleQuery(logs, 'GET', '?status=deleted')).toBeNull();
    expect(consoleQuery(['usage'], 'GET', '?days=94')).toBeNull();
    expect(consoleQuery(['usage'], 'GET', '?project_id=../x')).toBeNull();
  });

  it('refuses a parameter the operation does not take', () => {
    expect(consoleQuery(logs, 'GET', '?offset=-1')).toBeNull();
    expect(consoleQuery(['projects'], 'GET', '?workspace_id=w2')).toBeNull();
    expect(consoleQuery(['playground', 'execute'], 'POST', '?project_id=proj_1')).toBeNull();
    expect(consoleQuery(['usage'], 'GET', '?range=30d')).toBeNull();
  });

  it('refuses a repeated parameter rather than guessing which one wins', () => {
    expect(consoleQuery(logs, 'GET', '?limit=10&limit=999999')).toBeNull();
  });

  it('answers the reviewer\'s unbounded request with 400 and no network call', async () => {
    const calls = capture(() => json({ requests: [] }));
    const res = await GET(
      new Request(
        'http://localhost:3001/api/devplatform/projects/proj_1/logs?limit=999999999&offset=-1',
        { headers: { cookie: 'ts_session=abc' } },
      ),
      ctx('projects', 'proj_1', 'logs'),
    );
    expect(res.status).toBe(400);
    expect(calls).toHaveLength(0);
  });

  it('forwards the canonical query, not the bytes the browser typed', async () => {
    const calls = capture(() => json({ requests: [] }));
    await GET(
      new Request(
        'http://localhost:3001/api/devplatform/projects/proj_1/logs?limit=0100&status=failed',
        { headers: { cookie: 'ts_session=abc' } },
      ),
      ctx('projects', 'proj_1', 'logs'),
    );
    expect(calls[0]!.url).toBe(
      `http://orchestrator:8080${CONSOLE_UPSTREAM_BASE}/projects/proj_1/logs?limit=100&status=failed`,
    );
  });
});

describe('an unknown console endpoint', () => {
  it('answers 404 without calling the orchestrator at all', async () => {
    const calls = capture(() => json({ ok: true }));
    const res = await GET(
      new Request('http://localhost:3001/api/devplatform/members'),
      ctx('members'),
    );
    expect(res.status).toBe(404);
    expect(calls).toHaveLength(0);
  });

  it('answers 404 for a method the resource does not offer', async () => {
    const calls = capture(() => json({ ok: true }));
    const res = await DELETE(
      new Request('http://localhost:3001/api/devplatform/projects', {
        method: 'DELETE',
      }),
      ctx('projects'),
    );
    expect(res.status).toBe(404);
    expect(calls).toHaveLength(0);
  });

  it('says nothing about what does exist', async () => {
    capture(() => json({ ok: true }));
    const res = await GET(
      new Request('http://localhost:3001/api/devplatform/secrets'),
      ctx('secrets'),
    );
    await expect(res.json()).resolves.toEqual({
      message: 'Unknown console endpoint.',
    });
  });
});

// ---------------------------------------------------------------------------
// JSON passthrough
// ---------------------------------------------------------------------------

describe('an allowed console call', () => {
  it('reaches the orchestrator console API with the cookie and the query', async () => {
    const calls = capture(() => json({ series: [] }));
    const res = await GET(
      new Request(
        'http://localhost:3001/api/devplatform/usage?project_id=proj_1&days=30',
        { headers: { cookie: 'ts_session=abc' } },
      ),
      ctx('usage'),
    );
    expect(res.status).toBe(200);
    expect(calls).toHaveLength(1);
    expect(calls[0]!.url).toBe(
      'http://orchestrator:8080/admin/api/developers/usage?project_id=proj_1&days=30',
    );
    expect((calls[0]!.init.headers as Record<string, string>).cookie).toBe(
      'ts_session=abc',
    );
  });

  it('never relays an Authorization header the browser supplied', async () => {
    const calls = capture(() => json({ projects: [] }));
    await GET(
      new Request('http://localhost:3001/api/devplatform/projects', {
        headers: {
          cookie: 'ts_session=abc',
          authorization: 'Bearer tsk_live_deadbeef_secret',
        },
      }),
      ctx('projects'),
    );
    const headers = calls[0]!.init.headers as Record<string, string>;
    const names = Object.keys(headers).map((k) => k.toLowerCase());
    expect(names).not.toContain('authorization');
    expect(JSON.stringify(headers)).not.toContain('tsk_live');
  });

  it('re-encodes the identifier segment so it cannot smuggle a path', async () => {
    const calls = capture(() => json({ ok: true }));
    await PUT(
      new Request('http://localhost:3001/api/devplatform/models/techsara-35b', {
        method: 'PUT',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ enabled: false }),
      }),
      ctx('models', 'techsara-35b'),
    );
    expect(calls[0]!.url).toBe(
      `http://orchestrator:8080${CONSOLE_UPSTREAM_BASE}/models/techsara-35b`,
    );
  });

  it("carries a PATCH through for a webhook's enable switch", async () => {
    const calls = capture(() => json({ webhook: {} }));
    const res = await PATCH(
      new Request('http://localhost:3001/api/devplatform/projects/proj_1/webhooks/whe_1', {
        method: 'PATCH',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ status: 'active' }),
      }),
      ctx('projects', 'proj_1', 'webhooks', 'whe_1'),
    );
    expect(res.status).toBe(200);
    expect(calls[0]!.init.method).toBe('PATCH');
    expect(calls[0]!.url).toBe(
      `http://orchestrator:8080${CONSOLE_UPSTREAM_BASE}/projects/proj_1/webhooks/whe_1`,
    );
  });

  it('carries a PUT through for the limits form, under the project', async () => {
    const calls = capture(() => json({ limits: {} }));
    const res = await PUT(
      new Request('http://localhost:3001/api/devplatform/projects/proj_1/limits', {
        method: 'PUT',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ rpm: 120 }),
      }),
      ctx('projects', 'proj_1', 'limits'),
    );
    expect(res.status).toBe(200);
    expect(calls[0]!.init.method).toBe('PUT');
    expect(calls[0]!.url).toBe(
      `http://orchestrator:8080${CONSOLE_UPSTREAM_BASE}/projects/proj_1/limits`,
    );
  });
});

// ---------------------------------------------------------------------------
// The playground stream
// ---------------------------------------------------------------------------

/** A stream that emits one frame and then stays open, like a live generation. */
function openStream(first: string): ReadableStream<Uint8Array> {
  return new ReadableStream({
    start(controller) {
      controller.enqueue(new TextEncoder().encode(first));
      // Deliberately never closed: a buffering proxy would hang here forever,
      // which is exactly the failure this shape exists to catch.
    },
  });
}

describe('the playground stream', () => {
  it('hands the first frame to the browser before the generation has finished', async () => {
    vi.stubGlobal('fetch', async () =>
      new Response(openStream('event: response.created\ndata: {"sequence_number":1}\n\n'), {
        status: 200,
        headers: { 'content-type': 'text/event-stream', 'x-request-id': 'req_42' },
      }),
    );
    const res = await POST(
      new Request('http://localhost:3001/api/devplatform/playground/execute', {
        method: 'POST',
        headers: { 'content-type': 'application/json', cookie: 'ts_session=abc' },
        body: JSON.stringify({ model: 'techsara-35b', input: 'hello', stream: true }),
      }),
      ctx('playground', 'execute'),
    );
    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toContain('text/event-stream');
    expect(res.headers.get('x-request-id')).toBe('req_42');
    expect(res.headers.get('x-accel-buffering')).toBe('no');

    const reader = (res.body as ReadableStream<Uint8Array>).getReader();
    const chunk = await reader.read();
    expect(new TextDecoder().decode(chunk.value)).toContain('response.created');
    await reader.cancel();
  });

  it('forwards the session cookie and asks for an event stream', async () => {
    const calls = capture(
      () =>
        new Response(openStream('event: response.created\ndata: {}\n\n'), {
          status: 200,
          headers: { 'content-type': 'text/event-stream' },
        }),
    );
    const res = await POST(
      new Request('http://localhost:3001/api/devplatform/playground/execute', {
        method: 'POST',
        headers: { 'content-type': 'application/json', cookie: 'ts_session=abc' },
        body: JSON.stringify({ input: 'hi' }),
      }),
      ctx('playground', 'execute'),
    );
    const headers = calls[0]!.init.headers as Record<string, string>;
    expect(headers.cookie).toBe('ts_session=abc');
    expect(headers.accept).toBe('text/event-stream');
    expect(calls[0]!.url).toBe(
      `http://orchestrator:8080${CONSOLE_UPSTREAM_BASE}/playground/execute`,
    );
    await (res.body as ReadableStream).cancel();
  });

  it('never relays an Authorization header on the streaming path either', async () => {
    const calls = capture(
      () =>
        new Response(openStream('data: {}\n\n'), {
          status: 200,
          headers: { 'content-type': 'text/event-stream' },
        }),
    );
    const res = await POST(
      new Request('http://localhost:3001/api/devplatform/playground/execute', {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          cookie: 'ts_session=abc',
          authorization: 'Bearer tsk_live_deadbeef_secret',
        },
        body: JSON.stringify({ input: 'hi' }),
      }),
      ctx('playground', 'execute'),
    );
    const headers = calls[0]!.init.headers as Record<string, string>;
    expect(Object.keys(headers).map((k) => k.toLowerCase())).not.toContain(
      'authorization',
    );
    await (res.body as ReadableStream).cancel();
  });

  it('relays a refusal as the status and sentence the orchestrator sent', async () => {
    capture(() => json({ message: 'This model is not available.' }, 404, { 'x-request-id': 'req_404' }));
    const res = await POST(
      new Request('http://localhost:3001/api/devplatform/playground/execute', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ model: 'nope', input: 'hi' }),
      }),
      ctx('playground', 'execute'),
    );
    expect(res.status).toBe(404);
    expect(res.headers.get('x-request-id')).toBe('req_404');
    await expect(res.json()).resolves.toEqual({
      message: 'This model is not available.',
    });
  });

  it('keeps Retry-After, the RateLimit family and the request id on a refusal', async () => {
    capture(() =>
      json(
        { error: { message: 'Slow down.', code: 'rate_limit_error' } },
        429,
        {
          'retry-after': '7',
          ratelimit: '"requests";r=0;t=7',
          'ratelimit-policy': '"requests";q=60;w=60',
          'x-ratelimit-remaining-requests': '0',
          'x-request-id': 'req_429',
          'x-internal-host': 'vllm-head',
        },
      ),
    );
    const res = await POST(
      new Request('http://localhost:3001/api/devplatform/playground/execute', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ model: 'techsara-35b', input: 'hi', stream: true }),
      }),
      ctx('playground', 'execute'),
    );
    expect(res.status).toBe(429);
    expect(res.headers.get('retry-after')).toBe('7');
    expect(res.headers.get('ratelimit')).toBe('"requests";r=0;t=7');
    expect(res.headers.get('ratelimit-policy')).toBe('"requests";q=60;w=60');
    expect(res.headers.get('x-ratelimit-remaining-requests')).toBe('0');
    expect(res.headers.get('x-request-id')).toBe('req_429');
    // An allowlist, not a mirror.
    expect(res.headers.get('x-internal-host')).toBeNull();
  });

  it('keeps the RateLimit family on a stream too, with the SSE headers winning', async () => {
    vi.stubGlobal('fetch', async () =>
      new Response(openStream('event: response.created\ndata: {}\n\n'), {
        status: 200,
        headers: {
          'content-type': 'text/event-stream',
          'cache-control': 'public, max-age=60',
          ratelimit: '"requests";r=59;t=60',
          'x-request-id': 'req_ok',
        },
      }),
    );
    const res = await POST(
      new Request('http://localhost:3001/api/devplatform/playground/execute', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ model: 'techsara-35b', input: 'hi', stream: true }),
      }),
      ctx('playground', 'execute'),
    );
    expect(res.headers.get('ratelimit')).toBe('"requests";r=59;t=60');
    expect(res.headers.get('x-request-id')).toBe('req_ok');
    expect(res.headers.get('cache-control')).toBe('no-store, no-cache, no-transform');
    expect(res.headers.get('content-type')).toContain('text/event-stream');
    await (res.body as ReadableStream).cancel();
  });

  it('relays a non-streamed JSON answer as JSON rather than labelling it a stream', async () => {
    capture(() => json({ id: 'resp_1', status: 'completed' }, 200, { 'x-request-id': 'req_json' }));
    const res = await POST(
      new Request('http://localhost:3001/api/devplatform/playground/execute', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ model: 'techsara-35b', input: 'hi' }),
      }),
      ctx('playground', 'execute'),
    );
    expect(res.headers.get('content-type')).toContain('application/json');
    expect(res.headers.get('x-request-id')).toBe('req_json');
    await expect(res.json()).resolves.toEqual({ id: 'resp_1', status: 'completed' });
  });

  it("forwards the deployment's trusted client address and scheme, never the caller's", async () => {
    vi.stubEnv('TRUSTED_CLIENT_IP_HEADER', 'cf-connecting-ip');
    vi.stubEnv('TRUSTED_FORWARDED_PROTO', 'https');
    const calls = capture(
      () =>
        new Response(openStream('data: {}\n\n'), {
          status: 200,
          headers: { 'content-type': 'text/event-stream' },
        }),
    );
    const res = await POST(
      new Request('http://localhost:3001/api/devplatform/playground/execute', {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          'cf-connecting-ip': '203.0.113.9',
          'x-forwarded-for': '10.0.0.1',
        },
        body: JSON.stringify({ input: 'hi' }),
      }),
      ctx('playground', 'execute'),
    );
    const headers = calls[0]!.init.headers as Record<string, string>;
    expect(headers['x-forwarded-for']).toBe('203.0.113.9');
    expect(headers['x-forwarded-proto']).toBe('https');
    await (res.body as ReadableStream).cancel();
  });

  it('forwards no address at all when the deployment names no trusted header', async () => {
    const calls = capture(
      () =>
        new Response(openStream('data: {}\n\n'), {
          status: 200,
          headers: { 'content-type': 'text/event-stream' },
        }),
    );
    const res = await POST(
      new Request('http://localhost:3001/api/devplatform/playground/execute', {
        method: 'POST',
        headers: { 'content-type': 'application/json', 'x-forwarded-for': '10.0.0.1' },
        body: JSON.stringify({ input: 'hi' }),
      }),
      ctx('playground', 'execute'),
    );
    const headers = calls[0]!.init.headers as Record<string, string>;
    expect(headers['x-forwarded-for']).toBeUndefined();
    await (res.body as ReadableStream).cancel();
  });

  it('refuses a declared body over the console cap before opening a socket', async () => {
    const calls = capture(() => json({ ok: true }));
    const res = await POST(
      new Request('http://localhost:3001/api/devplatform/playground/execute', {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          'content-length': String(8 * 1024 * 1024),
        },
        body: JSON.stringify({ input: 'x' }),
      }),
      ctx('playground', 'execute'),
    );
    expect(res.status).toBe(413);
    expect(calls).toHaveLength(0);
  });

  it('answers 502 rather than throwing when the orchestrator is unreachable', async () => {
    vi.stubGlobal('fetch', async () => {
      throw new TypeError('fetch failed');
    });
    const res = await POST(
      new Request('http://localhost:3001/api/devplatform/playground/execute', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ input: 'hi' }),
      }),
      ctx('playground', 'execute'),
    );
    expect(res.status).toBe(502);
  });
});
