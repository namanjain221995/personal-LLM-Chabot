/**
 * The uploads file proxy speaks byte RANGES (2026-10-02, chat media,
 * docs/chat-media/CONTRACT.md §10).
 *
 * `/api/uploads/{conv}/{upload}/file` is the `src` of the chat's <video> and
 * <audio> players. A player asks for `Range: bytes=0-` first, then for
 * whatever range a seek lands in, and it only seeks if the answer was a 206
 * with a `content-range` and `accept-ranges: bytes`. The proxy used to drop
 * all three: it forwarded no Range and passed back only the type, the
 * disposition and the length, so a player got the whole file every time and
 * could not seek at all.
 *
 * The second block runs the route against a real HTTP server on loopback
 * that answers ranges the way Starlette's FileResponse does, so what is
 * pinned is undici's actual behaviour through the proxy, not a mock's.
 */

import { createServer, type IncomingHttpHeaders, type Server } from 'node:http';
import type { AddressInfo } from 'node:net';
import { afterAll, afterEach, beforeAll, describe, expect, it, vi } from 'vitest';

import { GET } from '@/app/api/uploads/[conversation]/[upload]/file/route';

const UPLOAD = 'a'.repeat(32);
const CONV = '7c9b6cb2-beb8-4e71-affa-9bfc7bad676d';

const ctx = (conversation: string, upload: string) => ({
  params: Promise.resolve({ conversation, upload }),
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

/* ======================================================= against a stub */

describe('the proxy forwards a range and relays the slice', () => {
  it('sends Range and If-Range up with the session, asks for identity, and nothing else', async () => {
    let sent: Record<string, string> = {};
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_url: string, init: RequestInit) => {
        sent = init.headers as Record<string, string>;
        return new Response('x', { status: 206, headers: { 'content-range': 'bytes 0-0/10' } });
      }),
    );
    await GET(
      new Request('http://x', {
        headers: {
          cookie: 'ts_session=s',
          range: 'bytes=100-',
          'if-range': '"etag-1"',
          authorization: 'Bearer leaked',
          'x-forwarded-for': '203.0.113.9',
          'accept-encoding': 'gzip, br',
        },
      }),
      ctx(CONV, UPLOAD),
    );
    expect(sent).toEqual({
      cookie: 'ts_session=s',
      range: 'bytes=100-',
      'if-range': '"etag-1"',
      // A range is a range of the STORED bytes; a compressed answer would
      // make every offset wrong.
      'accept-encoding': 'identity',
    });
  });

  it('relays a 206 with its content-range, accept-ranges, length and type, body intact', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response('LLO', {
          status: 206,
          headers: {
            'content-type': 'video/mp4',
            'content-length': '3',
            'content-range': 'bytes 2-4/11',
            'accept-ranges': 'bytes',
            'content-disposition': 'attachment; filename="clip.mp4"',
            etag: '"internal"',
            'set-cookie': 'not=forwarded',
          },
        }),
      ),
    );
    const res = await GET(
      new Request('http://x', { headers: { range: 'bytes=2-4' } }),
      ctx(CONV, UPLOAD),
    );
    expect(res.status).toBe(206);
    expect(res.headers.get('content-range')).toBe('bytes 2-4/11');
    expect(res.headers.get('accept-ranges')).toBe('bytes');
    expect(res.headers.get('content-length')).toBe('3');
    expect(res.headers.get('content-type')).toBe('video/mp4');
    expect(res.headers.get('cache-control')).toBe('no-store');
    // Only the allowlist comes back.
    expect(res.headers.get('set-cookie')).toBeNull();
    expect(res.headers.get('etag')).toBeNull();
    expect(await res.text()).toBe('LLO');
  });

  it('relays a 416 with the size, and no body, so the player can ask again inside the file', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response(null, { status: 416, headers: { 'content-range': 'bytes */11' } }),
      ),
    );
    const res = await GET(
      new Request('http://x', { headers: { range: 'bytes=500-' } }),
      ctx(CONV, UPLOAD),
    );
    expect(res.status).toBe(416);
    expect(res.headers.get('content-range')).toBe('bytes */11');
    expect(await res.text()).toBe('');
  });

  it('a whole-file answer still says ranges are accepted, so the player knows it may seek', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response('HELLO WORLD', {
          status: 200,
          headers: { 'content-length': '11', 'accept-ranges': 'bytes', 'content-type': 'audio/mpeg' },
        }),
      ),
    );
    const res = await GET(new Request('http://x'), ctx(CONV, UPLOAD));
    expect(res.status).toBe(200);
    expect(res.headers.get('accept-ranges')).toBe('bytes');
    expect(await res.text()).toBe('HELLO WORLD');
  });

  it('drops a length measured before an encoding the upstream applied anyway', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        new Response('decoded', {
          status: 200,
          headers: { 'content-length': '3', 'content-encoding': 'gzip' },
        }),
      ),
    );
    const res = await GET(new Request('http://x'), ctx(CONV, UPLOAD));
    expect(res.headers.get('content-length')).toBeNull();
  });

  it('still validates both ids before any request, Range or not', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const res = await GET(
      new Request('http://x', { headers: { range: 'bytes=0-' } }),
      ctx('../..', UPLOAD),
    );
    expect(res.status).toBe(400);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('an expired file is still the 410 "expired" answer for a ranged request', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response('{}', { status: 410 })));
    const res = await GET(
      new Request('http://x', { headers: { range: 'bytes=0-0' } }),
      ctx(CONV, UPLOAD),
    );
    expect(res.status).toBe(410);
    expect((await res.json()).message).toMatch(/expired/i);
  });
});

/* ======================================== through a real loopback server */

/** Eleven bytes, so every range is easy to read. */
const FILE = Buffer.from('HELLO WORLD');

/**
 * Answers like Starlette 1.6's FileResponse: `accept-ranges: bytes` always,
 * a 206 with the slice for one satisfiable range, and past the end a 416
 * whose content-range names the size. It refuses a request that asked for
 * compression, which is how the test proves `identity` really went up.
 */
function rangeServer(seen: IncomingHttpHeaders[]): Server {
  return createServer((req, res) => {
    seen.push(req.headers);
    if (req.url !== `/uploads/${CONV}/${UPLOAD}/file`) {
      res.writeHead(404).end();
      return;
    }
    // fetch() itself adds `identity` to a ranged request (the Fetch
    // standard), so a ranged one arrives as "identity, identity": every
    // listed coding must be identity, whoever listed it.
    const codings = (req.headers['accept-encoding'] ?? '').split(',').map((c) => c.trim());
    if (!codings.every((c) => c === 'identity')) {
      res.writeHead(500).end('compression was asked for');
      return;
    }
    const base = { 'content-type': 'video/webm', 'accept-ranges': 'bytes' };
    const m = /^bytes=(\d+)-(\d*)$/.exec(req.headers.range ?? '');
    if (!m) {
      res.writeHead(200, { ...base, 'content-length': String(FILE.length) }).end(FILE);
      return;
    }
    const start = Number(m[1]);
    const end = m[2] ? Math.min(Number(m[2]), FILE.length - 1) : FILE.length - 1;
    if (start >= FILE.length) {
      res.writeHead(416, { 'content-range': `bytes */${FILE.length}` }).end();
      return;
    }
    const slice = FILE.subarray(start, end + 1);
    res
      .writeHead(206, {
        ...base,
        'content-length': String(slice.length),
        'content-range': `bytes ${start}-${end}/${FILE.length}`,
      })
      .end(slice);
  });
}

describe('through a real HTTP server, as a player would ask', () => {
  const seen: IncomingHttpHeaders[] = [];
  let server: Server;

  beforeAll(async () => {
    server = rangeServer(seen);
    await new Promise<void>((resolve) => server.listen(0, '127.0.0.1', resolve));
  });
  afterAll(async () => {
    await new Promise<void>((resolve) => server.close(() => resolve()));
  });

  function ask(range?: string): Promise<Response> {
    const { port } = server.address() as AddressInfo;
    vi.stubEnv('ORCHESTRATOR_URL', `http://127.0.0.1:${port}`);
    return GET(
      new Request('http://app.test/api/uploads/x/y/file', {
        headers: range ? { range, cookie: 'ts_session=s' } : { cookie: 'ts_session=s' },
      }),
      ctx(CONV, UPLOAD),
    );
  }

  it('the first request of a player (bytes=0-) is a 206 for the whole file', async () => {
    const res = await ask('bytes=0-');
    expect(res.status).toBe(206);
    expect(res.headers.get('content-range')).toBe('bytes 0-10/11');
    expect(res.headers.get('accept-ranges')).toBe('bytes');
    expect(await res.text()).toBe('HELLO WORLD');
  });

  it('a seek gets exactly the slice it asked for', async () => {
    const res = await ask('bytes=6-10');
    expect(res.status).toBe(206);
    expect(res.headers.get('content-range')).toBe('bytes 6-10/11');
    expect(res.headers.get('content-length')).toBe('5');
    expect(await res.text()).toBe('WORLD');
    expect(seen.at(-1)?.range).toBe('bytes=6-10');
    expect(seen.at(-1)?.cookie).toBe('ts_session=s');
  });

  it('past the end is a 416 naming the size', async () => {
    const res = await ask('bytes=99-');
    expect(res.status).toBe(416);
    expect(res.headers.get('content-range')).toBe('bytes */11');
  });

  it('a download with no Range is the whole file, as before', async () => {
    const res = await ask();
    expect(res.status).toBe(200);
    expect(await res.text()).toBe('HELLO WORLD');
  });
});
