/**
 * QA round 1 (security lens) for the /api/memory/* proxy: what a hostile
 * caller can steer, and what must not leave this process. Written by QA,
 * adopted unchanged in the repair round so the forwarded-header set,
 * no-store, the unanchored-id and confirm-smuggle cases stay pinned.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { DELETE, GET } from '@/app/api/memory/[...path]/route';

const ctx = (...path: string[]) => ({ params: Promise.resolve({ path }) });

interface Call {
  url: string;
  init: RequestInit;
}

function capture(response: () => Response = () => json({ ok: true })): Call[] {
  const calls: Call[] = [];
  vi.stubGlobal('fetch', async (url: string | URL, init?: RequestInit) => {
    calls.push({ url: String(url), init: init ?? {} });
    return response();
  });
  return calls;
}

const json = (body: unknown, status = 200, extra: Record<string, string> = {}) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json', ...extra },
  });

beforeEach(() => {
  vi.stubEnv('ORCHESTRATOR_URL', 'http://orchestrator:8080');
  vi.stubEnv('MOCK_MODE', 'false');
  vi.stubEnv('TRUSTED_CLIENT_IP_HEADER', '');
  vi.stubEnv('TRUSTED_FORWARDED_PROTO', '');
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe('QA: nothing the caller says about itself goes upstream', () => {
  it('only cookie / user-agent / content-type leave; forwarding and auth headers do not', async () => {
    const calls = capture(() => json({ facts: [] }));
    await GET(
      new Request('http://localhost:3001/api/memory/facts', {
        headers: {
          cookie: 'ts_session=abc',
          'user-agent': 'qa',
          'x-forwarded-for': '10.0.0.9',
          forwarded: 'for=10.0.0.9',
          'x-real-ip': '10.0.0.9',
          'x-user-id': '2',
          authorization: 'Bearer someone-elses',
        },
      }),
      ctx('facts'),
    );
    expect(calls).toHaveLength(1);
    const sent = Object.keys(calls[0].init.headers as Record<string, string>).sort();
    expect(sent).toEqual(['cookie', 'user-agent']);
  });

  it('a request with no cookie goes upstream with no credential at all', async () => {
    const calls = capture(() => json({ detail: 'not signed in' }, 401));
    const res = await GET(new Request('http://localhost:3001/api/memory/facts'), ctx('facts'));
    expect((calls[0].init.headers as Record<string, string>).cookie).toBeUndefined();
    // Signed out is relayed as signed out, never turned into an empty list.
    expect(res.status).toBe(401);
  });
});

describe('QA: personal data is never cacheable on the way back', () => {
  it('GET facts answers with cache-control: no-store when the orchestrator names none', async () => {
    capture(() => json({ facts: [{ id: 1, fact: 'x' }] }));
    const res = await GET(
      new Request('http://localhost:3001/api/memory/facts', { headers: { cookie: 'ts_session=a' } }),
      ctx('facts'),
    );
    expect(res.headers.get('cache-control')).toBe('no-store');
  });

  it('an upstream response header outside the allowlist is dropped', async () => {
    capture(() => json({ facts: [] }, 200, { 'x-internal-user': '7', server: 'uvicorn' }));
    const res = await GET(
      new Request('http://localhost:3001/api/memory/facts', { headers: { cookie: 'ts_session=a' } }),
      ctx('facts'),
    );
    expect(res.headers.get('x-internal-user')).toBeNull();
    expect(res.headers.get('server')).toBeNull();
  });
});

describe('QA: the upstream path cannot be steered', () => {
  const badIds = [
    '42\n',
    '42 ',
    ' 42',
    '+42',
    '042',
    '42.0',
    '0x2a',
    '４２', // full-width "42"
    '42?confirm=all',
    '42#x',
    '42/',
    '4 2',
    '1234567890123456789', // 19 digits
  ];
  for (const id of badIds) {
    it(`DELETE facts/${JSON.stringify(id)} never leaves`, async () => {
      const calls = capture();
      const res = await DELETE(
        new Request('http://localhost:3001/api/memory/facts/x', {
          method: 'DELETE',
          headers: { cookie: 'ts_session=a' },
        }),
        ctx('facts', id),
      );
      expect(res.status).toBe(404);
      expect(calls).toHaveLength(0);
    });
  }

  for (const seg of ['Facts', 'facts ', 'facts.json', 'facts/', '']) {
    it(`GET ${JSON.stringify(seg)} is not the list`, async () => {
      const calls = capture();
      const res = await GET(
        new Request('http://localhost:3001/api/memory/x', { headers: { cookie: 'ts_session=a' } }),
        ctx(seg),
      );
      expect(res.status).toBe(404);
      expect(calls).toHaveLength(0);
    });
  }

  it('a second confirm does not smuggle a clear-all past a first one that is not "all"', async () => {
    const calls = capture();
    const res = await DELETE(
      new Request('http://localhost:3001/api/memory/facts?confirm=yes&confirm=all', {
        method: 'DELETE',
        headers: { cookie: 'ts_session=a' },
      }),
      ctx('facts'),
    );
    expect(res.status).toBe(404);
    expect(calls).toHaveLength(0);
  });

  it('the 18-digit ceiling is still an id (fits BIGINT)', async () => {
    const calls = capture(() => json({ detail: 'fact not found' }, 404));
    await DELETE(
      new Request('http://localhost:3001/api/memory/facts/x', {
        method: 'DELETE',
        headers: { cookie: 'ts_session=a' },
      }),
      ctx('facts', '999999999999999999'),
    );
    expect(calls.map((c) => c.url)).toEqual([
      'http://orchestrator:8080/memory/facts/999999999999999999',
    ]);
  });

  it('a delete-one never carries the query string along', async () => {
    const calls = capture(() => json({ deleted: 5 }));
    await DELETE(
      new Request('http://localhost:3001/api/memory/facts/5?confirm=all&user_id=2', {
        method: 'DELETE',
        headers: { cookie: 'ts_session=a' },
      }),
      ctx('facts', '5'),
    );
    expect(calls.map((c) => c.url)).toEqual(['http://orchestrator:8080/memory/facts/5']);
  });
});
