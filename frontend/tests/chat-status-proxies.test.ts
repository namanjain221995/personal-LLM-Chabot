/**
 * The three routes a tab uses to ask what the SERVER is doing:
 * /api/chat/active, /api/chat/requests/[id] and /api/chat/attach/[id].
 *
 * INF-2 / fe-chat F4: `active` used to answer 200 `{active: []}` whenever the
 * orchestrator could not be reached — the same answer a healthy idle server
 * gives. During a deploy's recreate window (and the launcher recreates the
 * orchestrator BEFORE the frontend, so the old bundle serves that answer for
 * the whole window) a reloaded tab was told "nothing is running" when the
 * truth was "I could not ask", skipped its re-attach, and put the red "never
 * sent" notice on a live generation.
 *
 * The requests route exists for the same reason in reverse: its 404 body is
 * what tells a NEW frontend against an OLD backend apart from a genuine
 * "no such intent", so nothing here may rewrite it.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

beforeEach(() => {
  vi.stubEnv('MOCK_MODE', 'false');
  vi.stubEnv('ORCHESTRATOR_URL', 'http://orchestrator:8080');
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

const json = (body: unknown, status = 200) =>
  new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json' },
  });

async function active(req: Request) {
  const mod = await import('../app/api/chat/active/route');
  return mod.GET(req);
}

async function request(req: Request, id: string) {
  const mod = await import('../app/api/chat/requests/[id]/route');
  return mod.GET(req, { params: Promise.resolve({ id }) });
}

async function attach(req: Request, id: string) {
  const mod = await import('../app/api/chat/attach/[id]/route');
  return mod.GET(req, { params: Promise.resolve({ id }) });
}

const get = (url: string, headers: Record<string, string> = {}) =>
  new Request(`http://localhost:3001${url}`, { headers });

describe('GET /api/chat/active', () => {
  it('passes a healthy answer through', async () => {
    vi.stubGlobal('fetch', async () => json({ active: ['c-1'] }));
    const res = await active(get('/api/chat/active'));
    expect(res.status).toBe(200);
    await expect(res.json()).resolves.toEqual({ active: ['c-1'] });
  });

  it('reports an unreachable orchestrator as 502, NOT as an empty list', async () => {
    vi.stubGlobal('fetch', async () => {
      throw new TypeError('fetch failed');
    });
    const res = await active(get('/api/chat/active'));
    expect(res.status).toBe(502);
    await expect(res.json()).resolves.toMatchObject({ code: 'NETWORK_ERROR' });
  });

  it('passes an upstream refusal through with its own status', async () => {
    vi.stubGlobal('fetch', async () => json({ detail: 'starting up' }, 503));
    expect((await active(get('/api/chat/active'))).status).toBe(503);
  });

  it('forwards the session cookie — generations are owner-scoped', async () => {
    let sent: string | undefined;
    vi.stubGlobal('fetch', async (_url: string, init?: RequestInit) => {
      sent = (init?.headers as Record<string, string>)?.cookie;
      return json({ active: [] });
    });
    await active(get('/api/chat/active', { cookie: 'ts_session=abc' }));
    expect(sent).toBe('ts_session=abc');
  });
});

describe('GET /api/chat/requests/[intent]', () => {
  it('passes the status body through verbatim', async () => {
    const body = {
      intent_id: 'i1',
      status: 'interrupted',
      generation_id: 'g1',
      attempt: 2,
      resumable: true,
      answer_persisted: false,
      live: false,
    };
    vi.stubGlobal('fetch', async () => json(body));
    const res = await request(get('/api/chat/requests/i1'), 'i1');
    expect(res.status).toBe(200);
    await expect(res.json()).resolves.toEqual(body);
  });

  it('keeps the 404 body, so "unknown intent" is not confused with "no such route"', async () => {
    vi.stubGlobal('fetch', async () => json({ detail: 'unknown intent' }, 404));
    const res = await request(get('/api/chat/requests/i1'), 'i1');
    expect(res.status).toBe(404);
    await expect(res.json()).resolves.toEqual({ detail: 'unknown intent' });
  });

  it('an old backend answers "Not Found", and that survives too', async () => {
    vi.stubGlobal('fetch', async () => json({ detail: 'Not Found' }, 404));
    const res = await request(get('/api/chat/requests/i1'), 'i1');
    await expect(res.json()).resolves.toEqual({ detail: 'Not Found' });
  });

  it('reports its own transport failure as 502, never as a 404', async () => {
    vi.stubGlobal('fetch', async () => {
      throw new TypeError('fetch failed');
    });
    const res = await request(get('/api/chat/requests/i1'), 'i1');
    expect(res.status).toBe(502);
  });

  it('refuses an id that is not an intent id', async () => {
    vi.stubGlobal('fetch', async () => json({}));
    expect((await request(get('/api/chat/requests/x'), 'a/../b')).status).toBe(400);
  });

  it('forwards the session cookie', async () => {
    let sent: string | undefined;
    vi.stubGlobal('fetch', async (_url: string, init?: RequestInit) => {
      sent = (init?.headers as Record<string, string>)?.cookie;
      return json({ status: 'completed' });
    });
    await request(get('/api/chat/requests/i1', { cookie: 'ts_session=abc' }), 'i1');
    expect(sent).toBe('ts_session=abc');
  });
});

describe('GET /api/chat/attach/[conversation]', () => {
  it('says 404 only when the SERVER says there is nothing to attach to', async () => {
    vi.stubGlobal('fetch', async () => json({ detail: 'no active generation' }, 404));
    const res = await attach(get('/api/chat/attach/c-1'), 'c-1');
    expect(res.status).toBe(404);
    await expect(res.json()).resolves.toMatchObject({ code: 'NOT_FOUND' });
  });

  it('reports an unreachable orchestrator as 502 — never as "finished"', async () => {
    vi.stubGlobal('fetch', async () => {
      throw new TypeError('fetch failed');
    });
    const res = await attach(get('/api/chat/attach/c-1'), 'c-1');
    expect(res.status).toBe(502);
    await expect(res.json()).resolves.toMatchObject({ code: 'NETWORK_ERROR' });
  });

  it('maps any other upstream status to 502 with a code that says so', async () => {
    vi.stubGlobal('fetch', async () => json({ detail: 'boom' }, 500));
    const res = await attach(get('/api/chat/attach/c-1'), 'c-1');
    expect(res.status).toBe(502);
    await expect(res.json()).resolves.toMatchObject({
      code: 'ORCHESTRATOR_UNAVAILABLE',
    });
  });

  it('a dead session still reads as sign-in, not as finished', async () => {
    vi.stubGlobal('fetch', async () => json({ detail: 'no' }, 401));
    const res = await attach(get('/api/chat/attach/c-1'), 'c-1');
    expect(res.status).toBe(401);
    await expect(res.json()).resolves.toMatchObject({ code: 'UNAUTHENTICATED' });
  });
});
