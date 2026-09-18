/**
 * QA round 2 for the /api/memory/* proxy: the seams rounds 0 and 1 left
 * open — non-ASCII digits, a huge or deeply nested path, a clear-all
 * parameter on the wrong method, and an orchestrator that is not there.
 * In a real `next dev` (MOCK_MODE, Chromium's worker, 2026-09-18) HEAD and
 * OPTIONS answered 404 and a trailing slash was a 308 to the bare path.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { DELETE, GET } from '@/app/api/memory/[...path]/route';
import { listFacts } from '@/lib/memory';

const ctx = (...path: string[]) => ({ params: Promise.resolve({ path }) });

function capture(respond: () => Response | Promise<Response> = () => json({ facts: [] })) {
  const calls: { url: string; method: string }[] = [];
  vi.stubGlobal('fetch', async (url: string | URL, init?: RequestInit) => {
    calls.push({ url: String(url), method: String(init?.method ?? 'GET') });
    return respond();
  });
  return calls;
}

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), { status, headers: { 'content-type': 'application/json' } });

const del = (url = 'http://localhost:3001/api/memory/facts/x') =>
  new Request(url, { method: 'DELETE', headers: { cookie: 'ts_session=a' } });

beforeEach(() => {
  vi.stubEnv('ORCHESTRATOR_URL', 'http://orchestrator:8080');
  vi.stubEnv('MOCK_MODE', 'false');
});
afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe('QA2: only ASCII digits are an id', () => {
  for (const id of ['٤٢', '۴۲', '४२', '0', '00', '-1', '1_000', '1e3', 'NaN', 'Infinity']) {
    it(`DELETE facts/${JSON.stringify(id)} is a 404 that never leaves`, async () => {
      const calls = capture();
      const res = await DELETE(del(), ctx('facts', id));
      expect(res.status).toBe(404);
      expect(calls).toHaveLength(0);
    });
  }
});

describe('QA2: size of the path', () => {
  it('a one-megabyte id is refused without going upstream', async () => {
    const calls = capture();
    const res = await DELETE(del(), ctx('facts', '9'.repeat(1_000_000)));
    expect(res.status).toBe(404);
    expect(calls).toHaveLength(0);
  });

  it('ten thousand path segments are refused without going upstream', async () => {
    const calls = capture();
    const parts = Array.from({ length: 10_000 }, () => 'facts');
    const res = await GET(new Request('http://localhost:3001/api/memory/facts'), ctx(...parts));
    expect(res.status).toBe(404);
    expect(calls).toHaveLength(0);
  });
});

describe('QA2: the clear-all parameter only means something on DELETE', () => {
  it('GET facts?confirm=all is the plain list, and the parameter is not forwarded', async () => {
    const calls = capture();
    const res = await GET(
      new Request('http://localhost:3001/api/memory/facts?confirm=all', {
        headers: { cookie: 'ts_session=a' },
      }),
      ctx('facts'),
    );
    expect(res.status).toBe(200);
    expect(calls).toEqual([{ url: 'http://orchestrator:8080/memory/facts', method: 'GET' }]);
  });
});

describe('QA2: an orchestrator that is not there', () => {
  it('the list answers 502, and the panel\'s loader reads it as a failure, not an empty memory', async () => {
    vi.stubGlobal('fetch', async () => {
      throw new TypeError('fetch failed');
    });
    const res = await GET(
      new Request('http://localhost:3001/api/memory/facts', { headers: { cookie: 'ts_session=a' } }),
      ctx('facts'),
    );
    expect(res.status).toBe(502);
    vi.unstubAllGlobals();
    await expect(listFacts(async () => res.clone())).rejects.toThrow(/502/);
  });
});
