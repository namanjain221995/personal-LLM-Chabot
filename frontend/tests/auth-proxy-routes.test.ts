/**
 * The /api/auth/* proxy routes (enterprise auth retrofit).
 *
 * Three things must be true of every one of them, and were NOT true of the
 * old /api/auth/me: the browser's Cookie header reaches the orchestrator
 * (the session is HttpOnly — the proxy is the only carrier), every upstream
 * Set-Cookie comes back down (login/logout are useless otherwise), and the
 * status passes through honestly — a 401 must arrive as a 401, not as the
 * 502 the old handler collapsed everything onto.
 *
 * MOCK_MODE is covered too: the mock backend must exercise the same flow
 * (login sets the cookie, /me without it is a 401) or the demo path would
 * silently skip the auth retrofit entirely.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const ME_PAYLOAD = {
  username: 'naman',
  user: { id: 7, name: 'Naman', email: 'naman@techsara.test' },
  workspace: { id: 'ws-1', name: 'TechSara', role: 'admin' },
  capabilities: ['members.read'],
};

beforeEach(() => {
  vi.stubEnv('MOCK_MODE', 'false');
  vi.stubEnv('ORCHESTRATOR_URL', 'http://orchestrator:8080');
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
  vi.restoreAllMocks();
});

/** Import fresh so the stubbed env is read at call time. One helper per
 *  route (static specifiers, distinct types) keeps both vite's
 *  dynamic-import analysis and tsc happy. */
const meRoute = () => import('../app/api/auth/me/route');
const loginRoute = () => import('../app/api/auth/login/route');
const logoutRoute = () => import('../app/api/auth/logout/route');
const invitationRoute = () =>
  import('../app/api/auth/invitations/[token]/route');

function upstream(
  status: number,
  body: unknown,
  headers: Record<string, string> = {},
) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { 'content-type': 'application/json', ...headers },
  });
}

describe('/api/auth/me — honest passthrough', () => {
  it('forwards the browser cookie upstream and relays the payload', async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      return upstream(200, ME_PAYLOAD);
    });
    const { GET } = await meRoute();
    const res = await GET(
      new Request('http://localhost:3001/api/auth/me', {
        headers: { cookie: 'ts_session=abc123' },
      }),
    );
    expect(res.status).toBe(200);
    await expect(res.json()).resolves.toEqual(ME_PAYLOAD);
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe('http://orchestrator:8080/auth/me');
    expect(
      (calls[0].init?.headers as Record<string, string>).cookie,
    ).toBe('ts_session=abc123');
  });

  it('passes a 401 through as a 401 — not the old 502', async () => {
    vi.stubGlobal('fetch', async () =>
      upstream(401, { detail: 'Not signed in.' }),
    );
    const { GET } = await meRoute();
    const res = await GET(new Request('http://localhost:3001/api/auth/me'));
    expect(res.status).toBe(401);
  });

  it('answers 502 only when the orchestrator is actually unreachable', async () => {
    vi.stubGlobal('fetch', async () => {
      throw new Error('connect ECONNREFUSED');
    });
    const { GET } = await meRoute();
    const res = await GET(new Request('http://localhost:3001/api/auth/me'));
    expect(res.status).toBe(502);
  });

  it('relays a rotated session cookie back to the browser', async () => {
    vi.stubGlobal('fetch', async () =>
      upstream(200, ME_PAYLOAD, {
        'set-cookie': 'ts_session=rotated; Path=/; HttpOnly; SameSite=Lax',
      }),
    );
    const { GET } = await meRoute();
    const res = await GET(new Request('http://localhost:3001/api/auth/me'));
    expect(res.headers.get('set-cookie')).toContain('ts_session=rotated');
  });
});

describe('/api/auth/login — Set-Cookie is the whole point', () => {
  it('forwards the credentials and relays the session cookie', async () => {
    const calls: Array<{ url: string; init?: RequestInit }> = [];
    vi.stubGlobal('fetch', async (url: string, init?: RequestInit) => {
      calls.push({ url, init });
      return upstream(200, ME_PAYLOAD, {
        'set-cookie': 'ts_session=fresh; Path=/; HttpOnly; SameSite=Lax',
      });
    });
    const { POST } = await loginRoute();
    const res = await POST(
      new Request('http://localhost:3001/api/auth/login', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ email: 'naman@techsara.test', password: 'x' }),
      }),
    );
    expect(res.status).toBe(200);
    expect(res.headers.get('set-cookie')).toContain('ts_session=fresh');
    expect(calls[0].url).toBe('http://orchestrator:8080/auth/login');
    expect(calls[0].init?.method).toBe('POST');
    expect(JSON.parse(calls[0].init?.body as string)).toEqual({
      email: 'naman@techsara.test',
      password: 'x',
    });
  });

  it.each([401, 429])('passes a %i through untouched', async (status) => {
    vi.stubGlobal('fetch', async () =>
      upstream(status, { detail: 'no' }),
    );
    const { POST } = await loginRoute();
    const res = await POST(
      new Request('http://localhost:3001/api/auth/login', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: '{}',
      }),
    );
    expect(res.status).toBe(status);
  });
});

describe('/api/auth/logout — the clearing cookie must come back', () => {
  it('forwards the session cookie and relays the clear', async () => {
    const calls: Array<{ init?: RequestInit }> = [];
    vi.stubGlobal('fetch', async (_url: string, init?: RequestInit) => {
      calls.push({ init });
      return upstream(200, { ok: true }, {
        'set-cookie': 'ts_session=; Path=/; Max-Age=0; HttpOnly',
      });
    });
    const { POST } = await logoutRoute();
    const res = await POST(
      new Request('http://localhost:3001/api/auth/logout', {
        method: 'POST',
        headers: { cookie: 'ts_session=abc123' },
      }),
    );
    expect(res.status).toBe(200);
    expect(res.headers.get('set-cookie')).toContain('Max-Age=0');
    expect(
      (calls[0].init?.headers as Record<string, string>).cookie,
    ).toBe('ts_session=abc123');
  });
});

describe('/api/auth/invitations/[token]', () => {
  it('encodes the token into the upstream path', async () => {
    const urls: string[] = [];
    vi.stubGlobal('fetch', async (url: string) => {
      urls.push(url);
      return upstream(200, { email: 'a@b.c' });
    });
    const { GET } = await invitationRoute();
    const res = await GET(
      new Request('http://localhost:3001/api/auth/invitations/t%2Fx'),
      { params: Promise.resolve({ token: 't/x' }) },
    );
    expect(res.status).toBe(200);
    expect(urls[0]).toBe(
      'http://orchestrator:8080/auth/invitations/t%2Fx',
    );
  });

  it('passes the deliberate 404 through', async () => {
    vi.stubGlobal('fetch', async () => upstream(404, { detail: 'gone' }));
    const { GET } = await invitationRoute();
    const res = await GET(
      new Request('http://localhost:3001/api/auth/invitations/dead'),
      { params: Promise.resolve({ token: 'dead' }) },
    );
    expect(res.status).toBe(404);
  });
});

describe('MOCK_MODE exercises the same session flow', () => {
  beforeEach(() => {
    vi.stubEnv('MOCK_MODE', 'true');
    // Nothing may touch the network in mock mode.
    vi.stubGlobal('fetch', async () => {
      throw new Error('mock mode must not fetch');
    });
  });

  it('login sets the session cookie and answers the ME_PAYLOAD shape', async () => {
    const { POST } = await loginRoute();
    const res = await POST(
      new Request('http://localhost:3001/api/auth/login', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ email: 'a@b.c', password: 'pw' }),
      }),
    );
    expect(res.status).toBe(200);
    expect(res.headers.get('set-cookie')).toContain('ts_session=');
    const body = (await res.json()) as {
      username: string;
      user: { id: number };
      capabilities: string[];
    };
    expect(body.username).toBe('local');
    expect(typeof body.user.id).toBe('number');
    expect(Array.isArray(body.capabilities)).toBe(true);
  });

  it('rejects empty credentials like the real endpoint', async () => {
    const { POST } = await loginRoute();
    const res = await POST(
      new Request('http://localhost:3001/api/auth/login', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify({ email: '', password: '' }),
      }),
    );
    expect(res.status).toBe(401);
  });

  it('me is a 401 without the cookie, and the payload with it', async () => {
    const { GET } = await meRoute();
    const signedOut = await GET(
      new Request('http://localhost:3001/api/auth/me'),
    );
    expect(signedOut.status).toBe(401);

    const signedIn = await GET(
      new Request('http://localhost:3001/api/auth/me', {
        headers: { cookie: 'ts_session=mock-session' },
      }),
    );
    expect(signedIn.status).toBe(200);
    const body = (await signedIn.json()) as { user: { id: number } };
    expect(body.user.id).toBe(1);
  });

  it('logout clears the cookie and is safe when signed out', async () => {
    const { POST } = await logoutRoute();
    const res = await POST(
      new Request('http://localhost:3001/api/auth/logout', {
        method: 'POST',
      }),
    );
    expect(res.status).toBe(200);
    expect(res.headers.get('set-cookie')).toContain('Max-Age=0');
    await expect(res.json()).resolves.toEqual({ ok: true });
  });
});
