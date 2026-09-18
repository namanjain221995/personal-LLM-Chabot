/**
 * The /api/memory/* proxy (B11).
 *
 * The orchestrator has served GET /memory/facts and DELETE /memory/facts/{id}
 * since V10, and the frontend had no route to either: 132 facts written in
 * production under the old extraction rules sat where their owners could
 * neither see nor delete them. This route is the missing layer, and it is an
 * ALLOWLIST, not a passthrough — the three calls the memory panel makes are
 * forwarded and every other path, method and query string stops here with a
 * 404 before anything reaches the orchestrator.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import {
  DELETE,
  GET,
  OPTIONS,
  PATCH,
  POST,
  PUT,
} from '@/app/api/memory/[...path]/route';
import { classifyMemoryPath } from '@/lib/memoryRoutes';

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

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });

const req = (path: string, method = 'GET') =>
  new Request(`http://localhost:3001/api/memory/${path}`, {
    method,
    headers: { cookie: 'ts_session=abc' },
  });

const cookieOf = (call: Call) =>
  (call.init.headers as Record<string, string>).cookie;

beforeEach(() => {
  vi.stubEnv('ORCHESTRATOR_URL', 'http://orchestrator:8080');
  vi.stubEnv('MOCK_MODE', 'false');
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe('memory proxy — the three allowlisted calls', () => {
  it('GET facts forwards the cookie and the method, and relays the body', async () => {
    const facts = [{ id: 7, fact: 'Prefers metric units', source: 'stated' }];
    const calls = capture(() => json({ facts }));
    const res = await GET(req('facts'), ctx('facts'));
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe('http://orchestrator:8080/memory/facts');
    expect(calls[0].init.method).toBe('GET');
    expect(cookieOf(calls[0])).toBe('ts_session=abc');
    expect(res.status).toBe(200);
    await expect(res.json()).resolves.toEqual({ facts });
  });

  it('DELETE facts/<numeric id> forwards the cookie, the method and the id', async () => {
    const calls = capture(() => json({ deleted: 42 }));
    const res = await DELETE(req('facts/42', 'DELETE'), ctx('facts', '42'));
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe('http://orchestrator:8080/memory/facts/42');
    expect(calls[0].init.method).toBe('DELETE');
    expect(cookieOf(calls[0])).toBe('ts_session=abc');
    await expect(res.json()).resolves.toEqual({ deleted: 42 });
  });

  it('DELETE facts?confirm=all is the clear-all, and carries only that parameter', async () => {
    const calls = capture(() => json({ deleted: 3 }));
    const res = await DELETE(
      req('facts?confirm=all&user_id=2&limit=9', 'DELETE'),
      ctx('facts'),
    );
    expect(calls).toHaveLength(1);
    // Nothing the caller appended rides along: the upstream query is built
    // by name, never copied.
    expect(calls[0].url).toBe('http://orchestrator:8080/memory/facts?confirm=all');
    expect(calls[0].init.method).toBe('DELETE');
    expect(cookieOf(calls[0])).toBe('ts_session=abc');
    await expect(res.json()).resolves.toEqual({ deleted: 3 });
  });

  it('GET facts drops any query string the caller invents', async () => {
    const calls = capture(() => json({ facts: [] }));
    await GET(req('facts?user_id=2'), ctx('facts'));
    expect(calls[0].url).toBe('http://orchestrator:8080/memory/facts');
  });

  it("relays the orchestrator's own status — another person's id is its 404", async () => {
    capture(() => json({ detail: 'fact not found' }, 404));
    const res = await DELETE(req('facts/99', 'DELETE'), ctx('facts', '99'));
    expect(res.status).toBe(404);
    await expect(res.json()).resolves.toEqual({ detail: 'fact not found' });
  });
});

describe('memory proxy — everything else is a 404 that never leaves this process', () => {
  const rejected: [string, string, string[]][] = [
    // [method, url path, route segments]
    ['GET', '', []],
    ['GET', 'users', ['users']],
    ['GET', 'facts/42', ['facts', '42']],
    ['GET', 'facts/42/x', ['facts', '42', 'x']],
    ['DELETE', 'facts/abc', ['facts', 'abc']],
    ['DELETE', 'facts/1e3', ['facts', '1e3']],
    ['DELETE', 'facts/-1', ['facts', '-1']],
    ['DELETE', 'facts/0', ['facts', '0']],
    ['DELETE', 'facts/%2E%2E', ['facts', '..']],
    ['DELETE', 'facts/42/x', ['facts', '42', 'x']],
    ['DELETE', 'facts/12345678901234567890', ['facts', '12345678901234567890']],
    // The clear-all without its confirmation is not a route at all here.
    ['DELETE', 'facts', ['facts']],
    ['DELETE', 'facts?confirm=yes', ['facts']],
    ['DELETE', 'facts?confirm=ALL', ['facts']],
    ['DELETE', 'secrets', ['secrets']],
  ];

  for (const [method, path, parts] of rejected) {
    it(`${method} /api/memory/${path || '(root)'}`, async () => {
      const calls = capture();
      const handler = method === 'GET' ? GET : DELETE;
      const res = await handler(req(path, method), ctx(...parts));
      expect(res.status).toBe(404);
      expect(calls).toHaveLength(0);
    });
  }

  it('POST, PUT and PATCH are 404 on every path, the add-a-fact route included', async () => {
    const calls = capture();
    for (const [handler, method] of [
      [POST, 'POST'],
      [PUT, 'PUT'],
      [PATCH, 'PATCH'],
    ] as const) {
      for (const parts of [['facts'], ['facts', '42']]) {
        const res = await handler(
          new Request(`http://localhost:3001/api/memory/${parts.join('/')}`, {
            method,
            headers: { cookie: 'ts_session=abc', 'content-type': 'application/json' },
            body: JSON.stringify({ facts: ['injected'] }),
          }),
          ctx(...parts),
        );
        expect(res.status).toBe(404);
      }
    }
    expect(calls).toHaveLength(0);
  });

  it('OPTIONS is a 404 too, not an Allow header listing the methods', async () => {
    // Without an OPTIONS export, Next answered 204 with
    // "allow: DELETE, GET, HEAD, OPTIONS, PATCH, POST, PUT" (QA, 2026-09-18).
    const calls = capture();
    for (const parts of [['facts'], ['facts', '42'], ['anything']]) {
      const res = await OPTIONS(
        new Request(`http://localhost:3001/api/memory/${parts.join('/')}`, {
          method: 'OPTIONS',
          headers: { cookie: 'ts_session=abc' },
        }),
        ctx(...parts),
      );
      expect(res.status).toBe(404);
      expect(res.headers.get('allow')).toBeNull();
    }
    expect(calls).toHaveLength(0);
  });
});

describe('classifyMemoryPath', () => {
  const none = new URLSearchParams();
  const all = new URLSearchParams('confirm=all');

  it('names the three calls and nothing else', () => {
    expect(classifyMemoryPath(['facts'], 'GET', none)).toEqual({ kind: 'list' });
    expect(classifyMemoryPath(['facts', '42'], 'DELETE', none)).toEqual({
      kind: 'delete-one',
      id: '42',
    });
    expect(classifyMemoryPath(['facts'], 'DELETE', all)).toEqual({
      kind: 'clear-all',
    });
    expect(classifyMemoryPath(['facts'], 'HEAD', none).kind).toBe('reject');
    expect(classifyMemoryPath(['facts'], 'POST', all).kind).toBe('reject');
    expect(classifyMemoryPath(['facts', '42'], 'GET', all).kind).toBe('reject');
  });
});

describe('memory proxy — MOCK_MODE is served by mockApi', () => {
  it('lists, deletes one, refuses an unconfirmed clear, and clears', async () => {
    vi.stubEnv('MOCK_MODE', 'true');
    const calls = capture();

    const listed = await GET(req('facts'), ctx('facts'));
    const { facts } = (await listed.json()) as {
      facts: { id: number; source: string | null }[];
    };
    expect(facts.length).toBeGreaterThanOrEqual(3);
    // One of each provenance, so the panel's three labels are all demo-able.
    expect(new Set(facts.map((f) => f.source))).toEqual(
      new Set(['stated', 'manual', null]),
    );

    const first = facts[0].id;
    const deleted = await DELETE(
      req(`facts/${first}`, 'DELETE'),
      ctx('facts', String(first)),
    );
    await expect(deleted.json()).resolves.toEqual({ deleted: first });
    const again = await DELETE(
      req(`facts/${first}`, 'DELETE'),
      ctx('facts', String(first)),
    );
    expect(again.status).toBe(404);

    // The mock sits BEHIND the allowlist, so an unconfirmed clear never
    // reaches it and nothing is removed.
    const unconfirmed = await DELETE(req('facts', 'DELETE'), ctx('facts'));
    expect(unconfirmed.status).toBe(404);
    const still = (await (await GET(req('facts'), ctx('facts'))).json()) as {
      facts: unknown[];
    };
    expect(still.facts).toHaveLength(facts.length - 1);

    const cleared = await DELETE(req('facts?confirm=all', 'DELETE'), ctx('facts'));
    await expect(cleared.json()).resolves.toEqual({ deleted: facts.length - 1 });
    const after = await GET(req('facts'), ctx('facts'));
    await expect(after.json()).resolves.toEqual({ facts: [] });

    expect(calls).toHaveLength(0);
  });
});
