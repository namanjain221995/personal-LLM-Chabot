/**
 * The /api/admin/* proxy: cookies and the query string travel upstream,
 * Set-Cookie travels back, and the two download endpoints keep their bytes
 * AND their download headers. content-disposition is the one header
 * proxyToOrchestrator drops — which is exactly why downloads bypass it, and
 * exactly what these tests pin.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  DELETE,
  GET,
  POST,
  isDownloadPath,
} from '@/app/api/admin/[...path]/route';

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

describe('admin proxy — JSON passthrough', () => {
  it('forwards the cookie and the whole query string', async () => {
    const calls = capture(() => json({ members: [], total: 0 }));
    const res = await GET(
      new Request(
        'http://localhost:3001/api/admin/members?q=jo&role=admin&status=active&limit=25&offset=50',
        { headers: { cookie: 'ts_session=abc' } },
      ),
      ctx('members'),
    );
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe(
      'http://orchestrator:8080/admin/api/members?q=jo&role=admin&status=active&limit=25&offset=50',
    );
    expect((calls[0].init.headers as Record<string, string>).cookie).toBe(
      'ts_session=abc',
    );
    expect(res.status).toBe(200);
    await expect(res.json()).resolves.toEqual({ members: [], total: 0 });
  });

  it('relays Set-Cookie back down', async () => {
    capture(() =>
      json({ ok: true }, 200, {
        'set-cookie': 'ts_session=renewed; Path=/; HttpOnly',
      }),
    );
    const res = await GET(
      new Request('http://localhost:3001/api/admin/overview'),
      ctx('overview'),
    );
    expect(res.headers.get('set-cookie')).toContain('ts_session=renewed');
  });

  it('forwards a POST body and content-type', async () => {
    const calls = capture(() => json({ id: 'inv-1' }));
    await POST(
      new Request('http://localhost:3001/api/admin/invitations', {
        method: 'POST',
        body: JSON.stringify({ email: 'ada@corp.com', role: 'member' }),
        headers: { 'content-type': 'application/json' },
      }),
      ctx('invitations'),
    );
    expect(calls[0].init.method).toBe('POST');
    expect(calls[0].init.body).toBe(
      JSON.stringify({ email: 'ada@corp.com', role: 'member' }),
    );
    expect(
      (calls[0].init.headers as Record<string, string>)['content-type'],
    ).toBe('application/json');
  });

  it('forwards DELETE to the encoded member path', async () => {
    const calls = capture(() => json({ ok: true }));
    await DELETE(
      new Request('http://localhost:3001/api/admin/members/7', {
        method: 'DELETE',
      }),
      ctx('members', '7'),
    );
    expect(calls[0].init.method).toBe('DELETE');
    expect(calls[0].url).toBe('http://orchestrator:8080/admin/api/members/7');
  });

  it('passes an upstream 404 {detail} through untouched', async () => {
    capture(() => json({ detail: 'No such member.' }, 404));
    const res = await GET(
      new Request('http://localhost:3001/api/admin/members/99'),
      ctx('members', '99'),
    );
    expect(res.status).toBe(404);
    await expect(res.json()).resolves.toEqual({ detail: 'No such member.' });
  });

  it('answers 502 when the orchestrator is unreachable', async () => {
    vi.stubGlobal('fetch', async () => {
      throw new Error('fetch failed');
    });
    const res = await GET(
      new Request('http://localhost:3001/api/admin/overview'),
      ctx('overview'),
    );
    expect(res.status).toBe(502);
  });
});

describe('admin proxy — downloads', () => {
  it('recognises exactly the file endpoints', () => {
    expect(
      isDownloadPath(['members', '7', 'uploads', 'u1', 'download'], 'GET'),
    ).toBe(true);
    expect(isDownloadPath(['members', '7', 'reports', 'q3.xlsx'], 'GET')).toBe(
      true,
    );
    // The usage CSV: without this it arrives as a nameless blob, because
    // proxyToOrchestrator relays content-type and nothing else.
    expect(isDownloadPath(['analytics', 'export'], 'GET')).toBe(true);
    expect(isDownloadPath(['analytics'], 'GET')).toBe(false);
    expect(isDownloadPath(['analytics', 'export'], 'POST')).toBe(false);
    // Method and shape both matter.
    expect(isDownloadPath(['members', '7', 'reports', 'q3.xlsx'], 'POST')).toBe(
      false,
    );
    expect(isDownloadPath(['members', '7', 'uploads', 'u1'], 'GET')).toBe(false);
    expect(isDownloadPath(['members', '7', 'uploads'], 'GET')).toBe(false);
    expect(isDownloadPath(['invitations'], 'GET')).toBe(false);
  });

  it('relays binary content-type, content-disposition and the exact bytes', async () => {
    const bytes = new Uint8Array([0x50, 0x4b, 0x03, 0x04]);
    const calls: Call[] = [];
    vi.stubGlobal('fetch', async (url: string | URL, init?: RequestInit) => {
      calls.push({ url: String(url), init: init ?? {} });
      return new Response(bytes, {
        status: 200,
        headers: {
          'content-type':
            'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
          'content-disposition': 'attachment; filename="Q3 pipeline.xlsx"',
        },
      });
    });
    const res = await GET(
      new Request(
        'http://localhost:3001/api/admin/members/7/uploads/u1/download',
        { headers: { cookie: 'ts_session=abc' } },
      ),
      ctx('members', '7', 'uploads', 'u1', 'download'),
    );
    expect(calls[0].url).toBe(
      'http://orchestrator:8080/admin/api/members/7/uploads/u1/download',
    );
    expect((calls[0].init.headers as Record<string, string>).cookie).toBe(
      'ts_session=abc',
    );
    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toBe(
      'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    );
    expect(res.headers.get('content-disposition')).toBe(
      'attachment; filename="Q3 pipeline.xlsx"',
    );
    expect(new Uint8Array(await res.arrayBuffer())).toEqual(bytes);
  });

  it('re-encodes a report filename before it goes upstream', async () => {
    const calls = capture(
      () =>
        new Response(new Uint8Array([1]), {
          status: 200,
          headers: { 'content-type': 'application/pdf' },
        }),
    );
    // Next has already percent-decoded the segment by the time it reaches
    // params — the proxy must re-encode it.
    await GET(
      new Request(
        'http://localhost:3001/api/admin/members/7/reports/q3%20report.pdf',
      ),
      ctx('members', '7', 'reports', 'q3 report.pdf'),
    );
    expect(calls[0].url).toBe(
      'http://orchestrator:8080/admin/api/members/7/reports/q3%20report.pdf',
    );
  });

  it('passes a download 404 through as its upstream status', async () => {
    capture(() => json({ detail: 'No such report.' }, 404));
    const res = await GET(
      new Request('http://localhost:3001/api/admin/members/7/reports/gone.pdf'),
      ctx('members', '7', 'reports', 'gone.pdf'),
    );
    expect(res.status).toBe(404);
  });

  it('answers 502 when the file fetch itself fails', async () => {
    vi.stubGlobal('fetch', async () => {
      throw new Error('fetch failed');
    });
    const res = await GET(
      new Request(
        'http://localhost:3001/api/admin/members/7/uploads/u1/download',
      ),
      ctx('members', '7', 'uploads', 'u1', 'download'),
    );
    expect(res.status).toBe(502);
  });
});
