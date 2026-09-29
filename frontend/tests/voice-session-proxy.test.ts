/**
 * The Next proxy for recording sessions (app/api/audio/sessions/**, 2026-09-29).
 *
 * Three promises, each of which a proxy can break without anything else
 * noticing:
 *
 *   1. NOTHING IN THE URL CAN STEER IT. The session cookie rides along on
 *      every request, so a path segment that is not exactly a 32-hex session
 *      id or a plain part number must never become a request at all.
 *   2. IT ADDS AND REMOVES NOTHING. The bytes of a part, its SHA-256, the
 *      status and the flat {detail, reason} body all pass through as they
 *      are; the browser's outbox depends on reading the orchestrator's answer,
 *      not the proxy's paraphrase of it.
 *   3. WHEN IT CANNOT REACH THE ORCHESTRATOR IT SAYS SO in a way the browser
 *      reads as "send it again", never as a refusal.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { callSessionApi } from '@/lib/voice';

const ID = '0123456789abcdef0123456789abcdef';
const ORCH = 'http://orchestrator.test:8080';

let upstream: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.stubEnv('ORCHESTRATOR_URL', ORCH);
  vi.stubEnv('MOCK_MODE', 'false');
  upstream = vi.fn(async () => Response.json({ ok: true }, { status: 200 }));
  vi.stubGlobal('fetch', upstream);
});
afterEach(() => {
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

const params = <T>(p: T) => ({ params: Promise.resolve(p) });

function partRequest(url: string, extra: Record<string, string> = {}): Request {
  return new Request(url, {
    method: 'PUT',
    headers: {
      cookie: 'ts_session=abc',
      'content-type': 'audio/webm;codecs=opus',
      'x-part-sha256': 'f'.repeat(64),
      'x-part-end-ms': '15000',
      authorization: 'Bearer should-not-travel',
      origin: 'https://evil.example',
      ...extra,
    },
    body: new Uint8Array([1, 2, 3, 4]),
    duplex: 'half',
  } as RequestInit & { duplex: 'half' });
}

describe('path segments are checked before any request exists', () => {
  it.each([
    ['../../admin/api/members'],
    ['..%2F..%2Fadmin'],
    [ID.toUpperCase()],
    [ID.slice(1)],
    [`${ID}0`],
    [''],
  ])('session id %j is a 404 that never reaches the orchestrator', async (id) => {
    const { PUT } = await import('../app/api/audio/sessions/[id]/parts/[seq]/route');
    const res = await PUT(partRequest(`http://x/api/audio/sessions/x/parts/0`), params({ id, seq: '0' }));
    expect(res.status).toBe(404);
    expect(await res.json()).toMatchObject({ reason: 'not_found' });
    expect(upstream).not.toHaveBeenCalled();
  });

  it.each([['-1'], ['01'], ['1.5'], ['abc'], ['9999999999'], ['1e3']])(
    'part number %j is a 400 that never reaches the orchestrator',
    async (seq) => {
      const { PUT } = await import('../app/api/audio/sessions/[id]/parts/[seq]/route');
      const res = await PUT(partRequest('http://x/'), params({ id: ID, seq }));
      expect(res.status).toBe(400);
      expect(await res.json()).toMatchObject({ reason: 'bad_request' });
      expect(upstream).not.toHaveBeenCalled();
    },
  );

  it('every route with an id checks it', async () => {
    const routes = await Promise.all([
      import('../app/api/audio/sessions/[id]/route'),
      import('../app/api/audio/sessions/[id]/finish/route'),
      import('../app/api/audio/sessions/[id]/retranscribe/route'),
      import('../app/api/audio/sessions/[id]/audio/route'),
    ]);
    const bad = params({ id: '../x' });
    const req = (method: string) => new Request('http://x/', { method });
    const answers = await Promise.all([
      routes[0].GET(req('GET'), bad),
      routes[0].DELETE(req('DELETE'), bad),
      routes[1].POST(req('POST'), bad),
      routes[2].POST(req('POST'), bad),
      routes[3].GET(req('GET'), bad),
    ]);
    expect(answers.map((r) => r.status)).toEqual([404, 404, 404, 404, 404]);
    expect(upstream).not.toHaveBeenCalled();
  });
});

describe('a part passes through untouched', () => {
  it('streams the bytes with the hash, the cookie and the cursor, and nothing else', async () => {
    upstream.mockResolvedValueOnce(
      Response.json({ accepted: 3, duplicate: false, session_id: ID, status: 'recording' }, { status: 200 }),
    );
    const { PUT } = await import('../app/api/audio/sessions/[id]/parts/[seq]/route');
    const res = await PUT(
      partRequest(`http://x/api/audio/sessions/${ID}/parts/3?cursor=2`),
      params({ id: ID, seq: '3' }),
    );
    expect(res.status).toBe(200);
    expect(await res.json()).toMatchObject({ accepted: 3, duplicate: false });

    const [url, init] = upstream.mock.calls[0] as [string, RequestInit & { duplex?: string }];
    expect(url).toBe(`${ORCH}/audio/sessions/${ID}/parts/3?cursor=2`);
    expect(init.method).toBe('PUT');
    expect(init.duplex).toBe('half');
    expect(init.headers).toEqual({
      cookie: 'ts_session=abc',
      'content-type': 'audio/webm;codecs=opus',
      'x-part-sha256': 'f'.repeat(64),
      'x-part-end-ms': '15000',
    });
    // The body is the browser's stream itself, not a copy the proxy buffered.
    expect(init.body).toBeInstanceOf(ReadableStream);
    const sent = new Uint8Array(await new Response(init.body as ReadableStream).arrayBuffer());
    expect([...sent]).toEqual([1, 2, 3, 4]);
    expect(res.headers.get('cache-control')).toContain('no-store');
  });

  it('keeps a refusal’s status and flat body, so the browser can read its reason', async () => {
    upstream.mockResolvedValueOnce(
      Response.json({ detail: 'gap', reason: 'out_of_order', next_part: 2 }, { status: 409 }),
    );
    const { PUT } = await import('../app/api/audio/sessions/[id]/parts/[seq]/route');
    const res = await PUT(partRequest('http://x/'), params({ id: ID, seq: '5' }));
    expect(res.status).toBe(409);
    expect(await res.json()).toEqual({ detail: 'gap', reason: 'out_of_order', next_part: 2 });
  });

  it('answers 502 proxy_unreachable when the orchestrator is down, which the browser retries', async () => {
    upstream.mockRejectedValueOnce(new TypeError('fetch failed'));
    const { PUT } = await import('../app/api/audio/sessions/[id]/parts/[seq]/route');
    const res = await PUT(partRequest('http://x/'), params({ id: ID, seq: '0' }));
    expect(res.status).toBe(502);
    const body = await res.clone().json();
    expect(body).toEqual({ detail: 'The recording service is unreachable.', reason: 'proxy_unreachable' });
    // And the client files it under "not the server speaking": send again.
    const reply = await callSessionApi(vi.fn(async () => res) as unknown as typeof fetch, '/x', {});
    expect(reply).toEqual({ kind: 'unreachable', status: 502 });
  });

  it('forwards the browser’s abort, so a withdrawn upload does not run on', async () => {
    // The upstream signal also carries the proxy's own deadline since
    // 2026-09-29, so it is not the browser's signal object any more; what is
    // held is the behaviour: the browser going away aborts the upstream call.
    const { PUT } = await import('../app/api/audio/sessions/[id]/parts/[seq]/route');
    const browser = new AbortController();
    const req = new Request(partRequest('http://x/'), { signal: browser.signal });
    await PUT(req, params({ id: ID, seq: '0' }));
    const upstreamSignal = (upstream.mock.calls[0]![1] as RequestInit).signal!;
    expect(upstreamSignal.aborted).toBe(false);
    browser.abort();
    expect(upstreamSignal.aborted).toBe(true);
  });
});

describe('the other session routes', () => {
  it('opens a session with the JSON body and the cookie', async () => {
    upstream.mockResolvedValueOnce(Response.json({ session_id: ID, status: 'recording' }, { status: 201 }));
    const { POST } = await import('../app/api/audio/sessions/route');
    const res = await POST(
      new Request('http://x/api/audio/sessions', {
        method: 'POST',
        headers: { cookie: 'ts_session=abc', 'content-type': 'application/json' },
        body: JSON.stringify({ client_key: 'k', mime_type: 'audio/webm' }),
      }),
    );
    expect(res.status).toBe(201);
    const [url, init] = upstream.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${ORCH}/audio/sessions`);
    expect(await new Response(init.body as ReadableStream).json()).toEqual({ client_key: 'k', mime_type: 'audio/webm' });
  });

  it('long-polls with the query forwarded verbatim', async () => {
    const { GET } = await import('../app/api/audio/sessions/[id]/route');
    await GET(
      new Request(`http://x/api/audio/sessions/${ID}?cursor=4&since_rev=9&wait_s=25`, {
        headers: { cookie: 'ts_session=abc' },
      }),
      params({ id: ID }),
    );
    const [url, init] = upstream.mock.calls[0] as [string, RequestInit];
    expect(url).toBe(`${ORCH}/audio/sessions/${ID}?cursor=4&since_rev=9&wait_s=25`);
    expect(init.method).toBe('GET');
    expect(init.body).toBeUndefined();
  });

  it('passes a discard’s 204 through with no body', async () => {
    upstream.mockResolvedValueOnce(new Response(null, { status: 204 }));
    const { DELETE } = await import('../app/api/audio/sessions/[id]/route');
    const res = await DELETE(new Request('http://x/', { method: 'DELETE' }), params({ id: ID }));
    expect(res.status).toBe(204);
    expect(await res.text()).toBe('');
  });

  it('serves a stored recording by range, for seeking an hour without downloading it', async () => {
    upstream.mockResolvedValueOnce(
      new Response(new Uint8Array([9, 9, 9]), {
        status: 206,
        headers: {
          'content-type': 'audio/webm',
          'content-range': 'bytes 100-102/57914640',
          'content-length': '3',
          'accept-ranges': 'bytes',
          'content-disposition': 'attachment; filename="recording-20260929-0105.webm"',
          'x-recording-complete': 'true',
          'set-cookie': 'not=forwarded',
        },
      }),
    );
    const { GET } = await import('../app/api/audio/sessions/[id]/audio/route');
    const res = await GET(
      new Request('http://x/', { headers: { range: 'bytes=100-102', cookie: 'ts_session=abc' } }),
      params({ id: ID }),
    );
    expect((upstream.mock.calls[0]![1] as RequestInit).headers).toEqual({
      cookie: 'ts_session=abc',
      range: 'bytes=100-102',
    });
    expect(res.status).toBe(206);
    expect(res.headers.get('content-range')).toBe('bytes 100-102/57914640');
    expect(res.headers.get('content-type')).toBe('audio/webm');
    expect(res.headers.get('x-recording-complete')).toBe('true');
    expect(res.headers.get('cache-control')).toContain('no-store');
    expect(res.headers.get('set-cookie')).toBeNull();
    expect([...new Uint8Array(await res.arrayBuffer())]).toEqual([9, 9, 9]);
  });

  it('says voice is unavailable in mock mode, without calling anything', async () => {
    vi.stubEnv('MOCK_MODE', 'true');
    const { POST } = await import('../app/api/audio/sessions/route');
    const res = await POST(new Request('http://x/', { method: 'POST', body: '{}' }));
    expect(res.status).toBe(404);
    expect(await res.json()).toMatchObject({ reason: 'voice_unavailable' });
    expect(upstream).not.toHaveBeenCalled();
  });
});
