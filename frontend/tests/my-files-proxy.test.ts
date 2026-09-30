/**
 * The two "My files" proxies, /api/files/mine and /api/files/mine/summary.
 *
 * They carry JSON only (file bytes stream through /api/uploads/.../file and
 * /api/audio/sessions/.../audio, never through proxyToOrchestrator, which
 * buffers a whole body). What is pinned here: the session cookie goes up,
 * ONLY the query parameters the orchestrator reads go up (a `user_id` a
 * browser adds is dropped before it can mean anything), a caller's forwarding
 * headers are never relayed, anything but GET is a 404, the orchestrator's
 * status comes back as it was, and MOCK_MODE answers without an orchestrator.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import * as listRoute from '@/app/api/files/mine/route';
import * as summaryRoute from '@/app/api/files/mine/summary/route';

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

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

beforeEach(() => {
  vi.stubEnv('ORCHESTRATOR_URL', 'http://orchestrator:8080');
  vi.stubEnv('MOCK_MODE', 'false');
  vi.stubEnv('TRUSTED_CLIENT_IP_HEADER', '');
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe('GET /api/files/mine', () => {
  it('forwards the cookie and only the parameters the orchestrator reads', async () => {
    const calls = capture(() => json({ items: [], next_cursor: null }));
    const res = await listRoute.GET(
      new Request(
        'http://localhost:3001/api/files/mine?q=budget&kind=document,recording&since=2026-09-01T00%3A00%3A00.000Z' +
          '&until=2026-10-01T00%3A00%3A00.000Z&min_bytes=10&max_bytes=99&sort=largest&limit=50&cursor=abc' +
          '&user_id=7&owner=7&debug=1',
        { headers: { cookie: 'ts_session=abc', 'x-forwarded-for': '203.0.113.9', 'cf-connecting-ip': '203.0.113.9' } },
      ),
    );
    expect(res.status).toBe(200);
    expect(calls).toHaveLength(1);
    const upstream = new URL(calls[0]!.url);
    expect(upstream.origin + upstream.pathname).toBe('http://orchestrator:8080/files/mine');
    expect(Object.fromEntries(upstream.searchParams)).toEqual({
      q: 'budget',
      kind: 'document,recording',
      since: '2026-09-01T00:00:00.000Z',
      until: '2026-10-01T00:00:00.000Z',
      min_bytes: '10',
      max_bytes: '99',
      sort: 'largest',
      limit: '50',
      cursor: 'abc',
    });
    const headers = calls[0]!.init.headers as Record<string, string>;
    expect(headers.cookie).toBe('ts_session=abc');
    expect(headers['x-forwarded-for']).toBeUndefined();
    expect(headers['cf-connecting-ip']).toBeUndefined();
    expect(calls[0]!.init.method).toBe('GET');
  });

  it('forwards a long name-sort cursor whole, up to the orchestrator bound, and nothing longer', async () => {
    // A name-sort cursor carries the last row's name: 255 CJK characters are
    // ~1,530 characters of JSON escapes and ~2,000 of base64. Dropping it
    // would silently fetch page one again.
    const calls = capture(() => json({ items: [], next_cursor: null }));
    const long = 'A'.repeat(6_000);
    await listRoute.GET(new Request(`http://localhost:3001/api/files/mine?sort=name&cursor=${long}`));
    expect(new URL(calls[0]!.url).searchParams.get('cursor')).toBe(long);
    const tooLong = 'A'.repeat(16_385);
    await listRoute.GET(new Request(`http://localhost:3001/api/files/mine?sort=name&cursor=${tooLong}`));
    expect(new URL(calls[1]!.url).searchParams.get('cursor')).toBeNull();
  });

  it('sends a bare request when the browser sent no parameters', async () => {
    const calls = capture(() => json({ items: [], next_cursor: null }));
    await listRoute.GET(new Request('http://localhost:3001/api/files/mine'));
    expect(calls[0]!.url).toBe('http://orchestrator:8080/files/mine');
  });

  it('passes a 401 and a 5xx through as they were', async () => {
    capture(() => json({ detail: 'Not signed in.' }, 401));
    const signedOut = await listRoute.GET(new Request('http://localhost:3001/api/files/mine'));
    expect(signedOut.status).toBe(401);
    await expect(signedOut.json()).resolves.toEqual({ detail: 'Not signed in.' });

    capture(() => json({ detail: 'boom' }, 503));
    const down = await listRoute.GET(new Request('http://localhost:3001/api/files/mine'));
    expect(down.status).toBe(503);
  });

  it('answers 404 to every method but GET, and asks the orchestrator nothing', async () => {
    const calls = capture(() => json({}));
    for (const method of ['POST', 'PUT', 'PATCH', 'DELETE'] as const) {
      const res = await listRoute[method]();
      expect(res.status, method).toBe(404);
    }
    expect(calls).toHaveLength(0);
  });

  it('serves MOCK_MODE from memory, signed in only', async () => {
    vi.stubEnv('MOCK_MODE', 'true');
    const calls = capture(() => json({}));
    const signedIn = await listRoute.GET(
      new Request('http://localhost:3001/api/files/mine?kind=recording', { headers: { cookie: 'ts_session=mock-session' } }),
    );
    expect(signedIn.status).toBe(200);
    const body = (await signedIn.json()) as { items: Array<{ kind: string }>; retention: unknown };
    expect(body.items.length).toBeGreaterThan(0);
    expect(body.items.every((i) => i.kind === 'recording')).toBe(true);
    expect(body.retention).toBeTruthy();
    const signedOut = await listRoute.GET(new Request('http://localhost:3001/api/files/mine'));
    expect(signedOut.status).toBe(401);
    expect(calls).toHaveLength(0);
  });
});

describe('GET /api/files/mine/summary', () => {
  it('forwards the filters the counts depend on, and not the kind, sort or page', async () => {
    const calls = capture(() => json({ kinds: {}, total: { count: 0, bytes: 0 } }));
    await summaryRoute.GET(
      new Request(
        'http://localhost:3001/api/files/mine/summary?q=a&since=s&until=u&min_bytes=1&max_bytes=2&kind=video&sort=name&cursor=c&limit=5&user_id=9',
        { headers: { cookie: 'ts_session=abc' } },
      ),
    );
    const upstream = new URL(calls[0]!.url);
    expect(upstream.pathname).toBe('/files/mine/summary');
    expect(Object.fromEntries(upstream.searchParams)).toEqual({
      q: 'a',
      since: 's',
      until: 'u',
      min_bytes: '1',
      max_bytes: '2',
    });
  });

  it('is GET only and has a MOCK_MODE answer', async () => {
    const calls = capture(() => json({}));
    const res = await summaryRoute.DELETE();
    expect(res.status).toBe(404);
    vi.stubEnv('MOCK_MODE', 'true');
    const mock = await summaryRoute.GET(
      new Request('http://localhost:3001/api/files/mine/summary', { headers: { cookie: 'ts_session=mock-session' } }),
    );
    const body = (await mock.json()) as { kinds: Record<string, { count: number }>; total: { count: number } };
    expect(body.total.count).toBe(Object.values(body.kinds).reduce((n, k) => n + k.count, 0));
    expect(calls).toHaveLength(0);
  });
});
