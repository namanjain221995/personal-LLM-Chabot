/**
 * The recording-session proxy's own guards (security review item 13,
 * fix/voice-recorder-edges, 2026-09-29).
 *
 *  - It forwards no Origin, so the orchestrator's Origin check never fires,
 *    and the session cookie is SameSite=Lax, which does not separate sibling
 *    subdomains: a page on another *.techsarasolutions.com host could drive
 *    a person's recordings. The browser's own Sec-Fetch-Site decides here.
 *  - The orchestrator parses these bodies as JSON whatever the content type
 *    says, so a cross-site text/plain form post would have reached it.
 *  - There was no upstream deadline: an orchestrator that never answered held
 *    the handler for ever. A long-poll must still be given more than its 25 s.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { callSessionApi } from '@/lib/voice';

const ID = '0123456789abcdef0123456789abcdef';
const params = <T>(p: T) => ({ params: Promise.resolve(p) });
let upstream: ReturnType<typeof vi.fn>;

beforeEach(() => {
  vi.stubEnv('ORCHESTRATOR_URL', 'http://orchestrator.test:8080');
  vi.stubEnv('MOCK_MODE', 'false');
  upstream = vi.fn(async () => Response.json({ ok: true }, { status: 200 }));
  vi.stubGlobal('fetch', upstream);
});
afterEach(() => {
  vi.useRealTimers();
  vi.unstubAllEnvs();
  vi.unstubAllGlobals();
});

function post(site: string | null, type = 'application/json'): Request {
  const headers: Record<string, string> = { cookie: 'ts_session=abc', 'content-type': type };
  if (site) headers['sec-fetch-site'] = site;
  return new Request('http://x/', { method: 'POST', headers, body: '{"client_key":"k"}' });
}

describe('requests from another site are refused before they reach the orchestrator', () => {
  it.each([['cross-site'], ['same-site']])('Sec-Fetch-Site: %s → 403', async (site) => {
    const { POST } = await import('../app/api/audio/sessions/route');
    const res = await POST(post(site));
    expect(res.status).toBe(403);
    expect(await res.json()).toMatchObject({ reason: 'cross_site' });
    expect(upstream).not.toHaveBeenCalled();
  });

  it('on every method: a sibling site cannot delete or read a recording either', async () => {
    const { DELETE, GET } = await import('../app/api/audio/sessions/[id]/route');
    const del = await DELETE(new Request('http://x/', { method: 'DELETE', headers: { 'sec-fetch-site': 'same-site' } }), params({ id: ID }));
    const get = await GET(new Request('http://x/?wait_s=25', { headers: { 'sec-fetch-site': 'cross-site' } }), params({ id: ID }));
    expect([del.status, get.status]).toEqual([403, 403]);
    expect(upstream).not.toHaveBeenCalled();
  });

  it.each([['same-origin'], ['none'], [null]])('Sec-Fetch-Site: %s is let through', async (site) => {
    const { POST } = await import('../app/api/audio/sessions/route');
    const res = await POST(post(site));
    expect(res.status).toBe(200);
    expect(upstream).toHaveBeenCalledTimes(1);
  });
});

describe('the JSON routes take JSON only', () => {
  it.each([
    ['create', async (req: Request) => (await import('../app/api/audio/sessions/route')).POST(req)],
    ['finish', async (req: Request) => (await import('../app/api/audio/sessions/[id]/finish/route')).POST(req, params({ id: ID }))],
    [
      'retranscribe',
      async (req: Request) => (await import('../app/api/audio/sessions/[id]/retranscribe/route')).POST(req, params({ id: ID })),
    ],
  ])('%s with text/plain → 415, not forwarded', async (_name, call) => {
    const res = await call(post('same-origin', 'text/plain'));
    expect(res.status).toBe(415);
    expect(upstream).not.toHaveBeenCalled();
  });

  it('a part is audio, not JSON, and still goes through', async () => {
    const { PUT } = await import('../app/api/audio/sessions/[id]/parts/[seq]/route');
    const req = new Request('http://x/', {
      method: 'PUT',
      headers: { 'content-type': 'audio/webm;codecs=opus', 'x-part-sha256': 'f'.repeat(64), 'sec-fetch-site': 'same-origin' },
      body: new Uint8Array([1, 2, 3]),
      duplex: 'half',
    } as RequestInit & { duplex: 'half' });
    expect((await PUT(req, params({ id: ID, seq: '0' }))).status).toBe(200);
  });
});

describe('an orchestrator that never answers does not hold the handler', () => {
  const hang = () =>
    upstream.mockImplementation(
      (_url: string, init: RequestInit) =>
        new Promise<Response>((_, reject) =>
          init.signal?.addEventListener('abort', () => reject(new DOMException('aborted', 'AbortError'))),
        ),
    );

  it('gives up after 30 s with 504 proxy_timeout, which the browser reads as "send it again"', async () => {
    vi.useFakeTimers();
    hang();
    const { POST } = await import('../app/api/audio/sessions/[id]/finish/route');
    let res: Response | null = null;
    void POST(post('same-origin'), params({ id: ID })).then((r) => (res = r));
    await vi.advanceTimersByTimeAsync(29_000);
    expect(res).toBeNull();
    await vi.advanceTimersByTimeAsync(2000);
    expect(res!.status).toBe(504);
    const body = await res!.json();
    expect(body).toMatchObject({ reason: 'proxy_timeout' });
    const reply = await callSessionApi(vi.fn(async () => Response.json(body, { status: 504 })) as unknown as typeof fetch, '/x', {});
    expect(reply.kind).toBe('unreachable');
  });

  it('gives a 25 s long-poll its 25 s and more, and no more than 40', async () => {
    vi.useFakeTimers();
    hang();
    const { GET } = await import('../app/api/audio/sessions/[id]/route');
    let res: Response | null = null;
    void GET(new Request('http://x/?cursor=0&since_rev=3&wait_s=25'), params({ id: ID })).then((r) => (res = r));
    await vi.advanceTimersByTimeAsync(39_000);
    expect(res).toBeNull();
    await vi.advanceTimersByTimeAsync(2000);
    expect(res!.status).toBe(504);
  });

  it('gives a part time to arrive in proportion to its size', async () => {
    vi.useFakeTimers();
    hang();
    const { PUT } = await import('../app/api/audio/sessions/[id]/parts/[seq]/route');
    const body = new Uint8Array(160_000); // 10 s of audio at 128 kb/s
    let res: Response | null = null;
    void PUT(
      new Request('http://x/', {
        method: 'PUT',
        headers: { 'content-type': 'audio/webm', 'content-length': String(body.byteLength), 'x-part-sha256': 'f'.repeat(64) },
        body,
        duplex: 'half',
      } as RequestInit & { duplex: 'half' }),
      params({ id: ID, seq: '0' }),
    ).then((r) => (res = r));
    await vi.advanceTimersByTimeAsync(39_000); // 30 s + 10 s for 160,000 bytes at 16 KiB/s
    expect(res).toBeNull();
    await vi.advanceTimersByTimeAsync(2000);
    expect(res!.status).toBe(504);
  });
});
