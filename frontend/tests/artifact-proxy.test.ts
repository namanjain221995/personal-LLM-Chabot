/**
 * The /api/artifacts/[[...path]] proxy (docs/artifact-studio/API.md).
 *
 * Two halves, like tests/reports-proxy.test.ts. `resolveArtifactPath` is the
 * closed grammar with no network in it: every route the API defines is
 * accepted and rebuilt from validated pieces — the empty path being the
 * listing — and everything else — a traversal, an encoded separator, an
 * unknown verb, a wrong id shape — is null. The handler half asserts what must NOT happen: a rejected path never
 * becomes an outbound request, a binary body is not re-encoded, a 206 and
 * its Content-Range travel through, and a Range / If-None-Match header
 * reaches the orchestrator.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

import {
  forwardedQuery,
  GET,
  HEAD,
  POST,
  resolveArtifactPath,
} from '@/app/api/artifacts/[[...path]]/route';

const ID = 'a3f9c2d1e4b5f6a7b8c9d0e1f2a3b4c5';
const JOB = 'ffffffffffffffffffffffffffffffff';

describe('resolveArtifactPath — the routes the API defines', () => {
  it.each([
    [[], '/artifacts', ['GET']],
    [['jobs', JOB], `/artifacts/jobs/${JOB}`, ['GET']],
    [['jobs', JOB, 'cancel'], `/artifacts/jobs/${JOB}/cancel`, ['POST']],
    [['jobs', JOB, 'retry'], `/artifacts/jobs/${JOB}/retry`, ['POST']],
    [[ID], `/artifacts/${ID}`, ['GET']],
    [[ID, 'convert'], `/artifacts/${ID}/convert`, ['POST']],
    [[ID, 'v', '2'], `/artifacts/${ID}/v/2`, ['GET']],
    [[ID, 'v', '2', 'file', 'pdf'], `/artifacts/${ID}/v/2/file/pdf`, ['GET', 'HEAD']],
    [[ID, 'v', '2', 'file', 'pptx'], `/artifacts/${ID}/v/2/file/pptx`, ['GET', 'HEAD']],
    [[ID, 'v', '2', 'preview'], `/artifacts/${ID}/v/2/preview`, ['GET', 'HEAD']],
    [[ID, 'v', '2', 'preview', '7.png'], `/artifacts/${ID}/v/2/preview/7.png`, ['GET']],
    [[ID, 'v', '2', 'sheets'], `/artifacts/${ID}/v/2/sheets`, ['GET']],
  ])('%j → %s', (segments, upstreamPath, methods) => {
    const r = resolveArtifactPath(segments as string[]);
    expect(r?.upstreamPath).toBe(upstreamPath);
    expect(r?.methods).toEqual(methods);
  });

  it('normalises numeric segments so "007" does not travel as text', () => {
    expect(resolveArtifactPath([ID, 'v', '7', 'preview', '7.png'])?.upstreamPath).toBe(
      `/artifacts/${ID}/v/7/preview/7.png`,
    );
  });

  it.each([
    ['dot segment', ['..']],
    ['traversal inside an id slot', ['..', 'v', '1']],
    ['traversal after a valid id', [ID, 'v', '1', '..', 'file', 'pdf']],
    ['still-encoded dot', ['%2e%2e']],
    ['still-encoded slash', [`${ID}%2fv%2f1`]],
    ['forward slash in a segment', [`${ID}/v/1`]],
    ['backslash', [`${ID}\\v`]],
    ['uppercase id', [ID.toUpperCase()]],
    ['short id', [ID.slice(0, 31)]],
    ['non-hex id', ['g'.repeat(32)]],
    ['unknown verb', [ID, 'delete']],
    ['unknown format', [ID, 'v', '1', 'file', 'exe']],
    ['format with traversal', [ID, 'v', '1', 'file', '../pdf']],
    ['negative version', [ID, 'v', '-1']],
    ['non-integer version', [ID, 'v', '1.5']],
    ['page 0', [ID, 'v', '1', 'preview', '0.png']],
    ['page without extension', [ID, 'v', '1', 'preview', '3']],
    ['page with a different extension', [ID, 'v', '1', 'preview', '3.svg']],
    ['sheets with a trailing segment', [ID, 'v', '1', 'sheets', 'x']],
    ['jobs without an id', ['jobs']],
    ['jobs with a bad id', ['jobs', 'not-an-id']],
    ['jobs with an unknown verb', ['jobs', JOB, 'delete']],
    ['too many segments', [ID, 'v', '1', 'file', 'pdf', 'x', 'y']],
    ['empty segment', [ID, '', 'v']],
  ])('refuses %s', (_label, segments) => {
    expect(resolveArtifactPath(segments as string[])).toBeNull();
  });
});

describe('forwardedQuery — only the parameters a route defines', () => {
  it('keeps a valid disposition and drops everything else', () => {
    expect(forwardedQuery('file', new URLSearchParams('disposition=inline&x=1'))).toBe(
      '?disposition=inline',
    );
    expect(forwardedQuery('file', new URLSearchParams('disposition=evil'))).toBe('');
  });
  it('accepts exactly the two preview widths', () => {
    expect(forwardedQuery('page', new URLSearchParams('w=240'))).toBe('?w=240');
    expect(forwardedQuery('page', new URLSearchParams('w=1400'))).toBe('?w=1400');
    expect(forwardedQuery('page', new URLSearchParams('w=9999'))).toBe('');
  });
  it('bounds the sheet window and keeps a sheet name with spaces', () => {
    expect(
      forwardedQuery('sheets', new URLSearchParams('sheet=Q3+Results&rows=200&cols=50')),
    ).toBe('?sheet=Q3+Results&rows=200&cols=50');
    expect(forwardedQuery('sheets', new URLSearchParams('rows=999999'))).toBe('');
    expect(forwardedQuery('sheets', new URLSearchParams('sheet=a%00b'))).toBe('');
  });
  it("caps a sheet name at Excel's 31 characters — the upstream's own limit", () => {
    const thirtyOne = 'S'.repeat(31);
    expect(forwardedQuery('sheets', new URLSearchParams({ sheet: thirtyOne }))).toBe(
      `?sheet=${thirtyOne}`,
    );
    expect(forwardedQuery('sheets', new URLSearchParams({ sheet: 'S'.repeat(32) }))).toBe('');
  });
  it('forwards a well-formed conversation id to the listing and drops anything else', () => {
    const conv = '4f1c2a9e-1b2c-4d3e-8f9a-0b1c2d3e4f5a';
    expect(forwardedQuery('list', new URLSearchParams({ conversation_id: conv }))).toBe(
      `?conversation_id=${conv}`,
    );
    expect(forwardedQuery('list', new URLSearchParams({ conversation_id: '../x' }))).toBe('');
    expect(forwardedQuery('list', new URLSearchParams({ conversation_id: 'a'.repeat(65) }))).toBe('');
    expect(forwardedQuery('list', new URLSearchParams('conversation_id='))).toBe('');
  });
  it('forwards nothing for a route with no parameters', () => {
    expect(forwardedQuery(null, new URLSearchParams('disposition=inline'))).toBe('');
  });
});

// --- handlers ---------------------------------------------------------------

const ctx = (path?: string[]) => ({ params: Promise.resolve({ path }) });
const req = (method = 'GET', headers: Record<string, string> = {}, url = 'http://localhost:3001/api/artifacts/x') =>
  new Request(url, { method, headers });

function stubUpstream(response: Response) {
  // Typed with fetch's arguments so `mock.calls[0]` reads as [url, init].
  const fetchMock = vi.fn<(url: string, init?: RequestInit) => Promise<Response>>(async () => response);
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe('handlers — an unsafe path never becomes a request', () => {
  it.each([
    ['traversal', ['..', 'v', '1']],
    ['encoded traversal', ['%2e%2e']],
    ['unknown verb', [ID, 'delete']],
    ['bad id', ['zz']],
    ['an empty segment', ['']],
  ])('answers 404 for %s without calling upstream', async (_label, path) => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const res = await GET(req(), ctx(path as string[]));
    expect(res.status).toBe(404);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('answers 404 for a method the route does not take, without calling upstream', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    // cancel is POST-only; a GET must not probe it.
    expect((await GET(req('GET'), ctx(['jobs', JOB, 'cancel']))).status).toBe(404);
    // a file is GET/HEAD; a POST must not reach it.
    expect((await POST(req('POST'), ctx([ID, 'v', '1', 'file', 'pdf']))).status).toBe(404);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('says nothing about why', async () => {
    vi.stubGlobal('fetch', vi.fn());
    const body = await (await GET(req(), ctx(['..', 'etc', 'passwd']))).text();
    expect(body).not.toMatch(/etc|passwd|orchestrator|8080/);
  });
});

describe('handlers — the listing (API.md row 1)', () => {
  it('maps the empty path to /artifacts with only a well-formed conversation_id', async () => {
    vi.stubEnv('ORCHESTRATOR_URL', 'http://orchestrator:8080');
    const fetchMock = stubUpstream(
      new Response('{"artifacts":[]}', { status: 200, headers: { 'content-type': 'application/json' } }),
    );
    const conv = 'c1';
    // Next hands an optional catch-all `undefined`, not `[]`, for the bare path.
    const res = await GET(
      req('GET', { cookie: 'ts_session=s1' }, `http://localhost:3001/api/artifacts?conversation_id=${conv}&junk=1`),
      ctx(undefined),
    );
    expect(res.status).toBe(200);
    expect(await res.json()).toEqual({ artifacts: [] });
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`http://orchestrator:8080/artifacts?conversation_id=${conv}`);
    expect((init?.headers as Record<string, string>).cookie).toBe('ts_session=s1');
  });

  it('drops a malformed conversation_id rather than forwarding it, and takes GET only', async () => {
    const fetchMock = stubUpstream(
      new Response('{"artifacts":[]}', { status: 200, headers: { 'content-type': 'application/json' } }),
    );
    await GET(req('GET', {}, 'http://localhost:3001/api/artifacts?conversation_id=..%2F..'), ctx([]));
    expect(fetchMock.mock.calls[0][0]).toMatch(/\/artifacts$/);
    expect((await POST(req('POST'), ctx([]))).status).toBe(404);
    expect(fetchMock).toHaveBeenCalledTimes(1);
  });
});

describe('handlers — passthrough', () => {
  it('pipes a PDF byte for byte with its headers, and forwards cookie + Range + If-None-Match', async () => {
    vi.stubEnv('ORCHESTRATOR_URL', 'http://orchestrator:8080');
    const body = new Uint8Array([0x25, 0x50, 0x44, 0x46, 0x00, 0xff, 0x80]);
    const fetchMock = stubUpstream(
      new Response(body, {
        status: 200,
        headers: {
          'content-type': 'application/pdf',
          'content-disposition': 'attachment; filename="brief-v1.pdf"',
          'content-length': String(body.length),
          'accept-ranges': 'bytes',
          etag: '"abc"',
          'cache-control': 'private, no-store',
        },
      }),
    );
    const res = await GET(
      req(
        'GET',
        { cookie: 'ts_session=s1', range: 'bytes=0-3', 'if-none-match': '"abc"' },
        `http://localhost:3001/api/artifacts/${ID}/v/1/file/pdf?disposition=attachment&junk=1`,
      ),
      ctx([ID, 'v', '1', 'file', 'pdf']),
    );
    expect(res.status).toBe(200);
    expect(new Uint8Array(await res.arrayBuffer())).toEqual(body);
    for (const [k, v] of [
      ['content-type', 'application/pdf'],
      ['content-disposition', 'attachment; filename="brief-v1.pdf"'],
      ['content-length', String(body.length)],
      ['accept-ranges', 'bytes'],
      ['etag', '"abc"'],
      ['cache-control', 'private, no-store'],
    ]) {
      expect(res.headers.get(k)).toBe(v);
    }
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`http://orchestrator:8080/artifacts/${ID}/v/1/file/pdf?disposition=attachment`);
    const sent = init?.headers as Record<string, string>;
    expect(sent.cookie).toBe('ts_session=s1');
    expect(sent.range).toBe('bytes=0-3');
    expect(sent['if-none-match']).toBe('"abc"');
  });

  it('passes a 206 with its Content-Range through', async () => {
    stubUpstream(
      new Response(new Uint8Array([1, 2, 3]), {
        status: 206,
        headers: { 'content-type': 'application/pdf', 'content-range': 'bytes 0-2/10' },
      }),
    );
    const res = await GET(req('GET', { range: 'bytes=0-2' }), ctx([ID, 'v', '1', 'file', 'pdf']));
    expect(res.status).toBe(206);
    expect(res.headers.get('content-range')).toBe('bytes 0-2/10');
    expect(new Uint8Array(await res.arrayBuffer())).toEqual(new Uint8Array([1, 2, 3]));
  });

  it('answers 304 with no body', async () => {
    stubUpstream(new Response(null, { status: 304, headers: { etag: '"abc"' } }));
    const res = await GET(req('GET', { 'if-none-match': '"abc"' }), ctx([ID, 'v', '1', 'file', 'pdf']));
    expect(res.status).toBe(304);
    expect(res.headers.get('etag')).toBe('"abc"');
    expect(await res.text()).toBe('');
  });

  it('HEAD returns the headers and no body', async () => {
    stubUpstream(
      new Response(null, {
        status: 200,
        headers: { 'content-type': 'application/pdf', 'content-length': '1234' },
      }),
    );
    const res = await HEAD(req('HEAD'), ctx([ID, 'v', '1', 'file', 'pdf']));
    expect(res.status).toBe(200);
    expect(res.headers.get('content-length')).toBe('1234');
    expect(await res.text()).toBe('');
  });

  it('defaults cache-control to private, no-store when the upstream sent none', async () => {
    stubUpstream(new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } }));
    const res = await GET(req(), ctx(['jobs', JOB]));
    expect(res.headers.get('cache-control')).toBe('private, no-store');
  });

  it('forwards a POST body and content-type', async () => {
    const fetchMock = stubUpstream(
      new Response('{"job_id":"x"}', { status: 200, headers: { 'content-type': 'application/json' } }),
    );
    const request = new Request(`http://localhost:3001/api/artifacts/${ID}/convert`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', cookie: 'ts_session=s1' },
      body: '{"format":"pdf"}',
    });
    const res = await POST(request, ctx([ID, 'convert']));
    expect(res.status).toBe(200);
    const [, init] = fetchMock.mock.calls[0];
    expect(init?.method).toBe('POST');
    expect(init?.body).toBe('{"format":"pdf"}');
  });

  it('refuses a POST body over the cap before reading it, declared or not', async () => {
    const fetchMock = stubUpstream(new Response('{}', { status: 200 }));
    const big = '{"format":"' + 'x'.repeat(4096) + '"}';
    const declared = new Request(`http://localhost:3001/api/artifacts/${ID}/convert`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', 'content-length': String(big.length), cookie: 'ts_session=s1' },
      body: big,
    });
    expect((await POST(declared, ctx([ID, 'convert']))).status).toBe(413);
    const undeclared = new Request(`http://localhost:3001/api/artifacts/${ID}/convert`, {
      method: 'POST',
      headers: { 'content-type': 'application/json', cookie: 'ts_session=s1' },
      body: big,
    });
    expect((await POST(undeclared, ctx([ID, 'convert']))).status).toBe(413);
    expect(fetchMock).not.toHaveBeenCalled();
  });
});

describe('handlers — failures are truthful and say nothing extra', () => {
  it.each([401, 403, 404, 410, 500])('passes a %s through', async (status) => {
    stubUpstream(new Response('x', { status }));
    expect((await GET(req(), ctx([ID, 'v', '1'])))?.status).toBe(status);
  });

  it("passes the API's own {detail} sentence through", async () => {
    stubUpstream(
      new Response(JSON.stringify({ detail: 'This version has no PDF.' }), {
        status: 400,
        headers: { 'content-type': 'application/json' },
      }),
    );
    const res = await GET(req(), ctx([ID, 'v', '1', 'file', 'pdf']));
    expect(res.status).toBe(400);
    expect(await res.json()).toEqual({ detail: 'This version has no PDF.' });
  });

  it('replaces a non-JSON error body with a sentence', async () => {
    stubUpstream(new Response('Traceback: connect ECONNREFUSED 10.0.0.4:8080', { status: 500 }));
    const body = await (await GET(req(), ctx([ID, 'v', '1']))).text();
    expect(body).not.toMatch(/Traceback|ECONNREFUSED|10\.0\.0\.4/);
  });

  it('reports an unreachable orchestrator as 502', async () => {
    vi.stubGlobal('fetch', async () => {
      throw new Error('ECONNREFUSED');
    });
    expect((await GET(req(), ctx([ID, 'v', '1']))).status).toBe(502);
  });

  it('reports a 200 with no body as a proxy failure', async () => {
    stubUpstream(new Response(null, { status: 200 }));
    expect((await GET(req(), ctx([ID, 'v', '1']))).status).toBe(502);
  });
});
