/**
 * The two chat-media proxies (2026-10-02, docs/chat-media/CONTRACT.md §4 and
 * §10): /api/chat-media/[conversation] (list, upload) and
 * /api/chat-media/[conversation]/[attachment] (one photo's bytes).
 *
 * What they must do, each for a reason:
 *   · validate every path segment (and `size`) BEFORE any fetch exists — a
 *     malformed id must never become an upstream path, cookie attached;
 *   · stream bytes both ways, never buffer: a 64 MiB upload and a photo
 *     download cross the process a chunk at a time;
 *   · forward the session cookie and If-None-Match, and relay
 *     200/304/404/410 with the caching headers — `private, immutable` plus
 *     the ETag is what lets a browser keep a photo instead of asking again;
 *   · cap an upload at 64 MiB, on the declaration and on the bytes.
 */

import { afterEach, describe, expect, it, vi } from 'vitest';
import { GET as getPhoto } from '@/app/api/chat-media/[conversation]/[attachment]/route';
import { GET as listPhotos, POST as postPhotos } from '@/app/api/chat-media/[conversation]/route';
import { MAX_MEDIA_BODY_BYTES } from '@/app/api/chat-media/_media';

const CONV = 'conv-media-1';
const ATT = 'img-aaaa-0001';
const ORCH = 'http://orch.test';

const photoCtx = (conversation: string, attachment: string) => ({
  params: Promise.resolve({ conversation, attachment }),
});
const convCtx = (conversation: string) => ({ params: Promise.resolve({ conversation }) });

const IMMUTABLE = 'private, max-age=31536000, immutable';

function upstreamPhoto(body: BodyInit | null = new Uint8Array([1, 2, 3]), extra: Record<string, string> = {}) {
  return new Response(body, {
    status: 200,
    headers: {
      'content-type': 'image/webp',
      'content-length': '3',
      'content-disposition': 'inline; filename="image.webp"',
      'cache-control': IMMUTABLE,
      etag: '"abc123-t"',
      'x-content-type-options': 'nosniff',
      'content-security-policy': "default-src 'none'; sandbox",
      ...extra,
    },
  });
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

function stubOrchestrator(answer: (url: string, init?: RequestInit) => Response | Promise<Response>) {
  vi.stubEnv('ORCHESTRATOR_URL', ORCH);
  vi.stubEnv('MOCK_MODE', 'false');
  const fetchMock = vi.fn(async (url: string, init?: RequestInit) => answer(url, init));
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

/* ============================================================ GET bytes */

describe('GET /api/chat-media/[conversation]/[attachment]', () => {
  it('validates both segments and the size before any fetch', async () => {
    const fetchMock = stubOrchestrator(() => upstreamPhoto());
    const bad: Array<[string, string, string]> = [
      ['../..', ATT, 'thumb'],
      ['a/b', ATT, 'thumb'],
      ['a'.repeat(65), ATT, 'thumb'],
      [CONV, 'short', 'thumb'],
      [CONV, '../../etc/passwd', 'thumb'],
      [CONV, 'a'.repeat(65), 'thumb'],
      [CONV, ATT, 'huge'],
      [CONV, '%E0%A4%A', 'thumb'],
    ];
    for (const [conv, att, size] of bad) {
      const res = await getPhoto(
        new Request(`http://x/api/chat-media/c/a?size=${size}`),
        photoCtx(conv, att),
      );
      expect(res.status).toBe(400);
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('forwards the cookie and If-None-Match, asks for identity, and the size', async () => {
    const fetchMock = stubOrchestrator(() => upstreamPhoto());
    await getPhoto(
      new Request('http://x/api/chat-media/c/a?size=thumb', {
        headers: { cookie: 'ts_session=s1', 'if-none-match': '"abc123-t"' },
      }),
      photoCtx(CONV, ATT),
    );
    const [url, init] = fetchMock.mock.calls[0];
    expect(url).toBe(`${ORCH}/chat-media/${CONV}/${ATT}?size=thumb`);
    const headers = init?.headers as Record<string, string>;
    expect(headers.cookie).toBe('ts_session=s1');
    expect(headers['if-none-match']).toBe('"abc123-t"');
    expect(headers['accept-encoding']).toBe('identity');
  });

  it('defaults to the full size', async () => {
    const fetchMock = stubOrchestrator(() => upstreamPhoto());
    await getPhoto(new Request('http://x/api/chat-media/c/a'), photoCtx(CONV, ATT));
    expect(fetchMock.mock.calls[0][0]).toBe(`${ORCH}/chat-media/${CONV}/${ATT}?size=full`);
  });

  it('relays 200 with the contract headers — immutable caching, ETag, nosniff, sandbox', async () => {
    stubOrchestrator(() => upstreamPhoto());
    const res = await getPhoto(new Request('http://x/?size=thumb'), photoCtx(CONV, ATT));
    expect(res.status).toBe(200);
    expect(res.headers.get('cache-control')).toBe(IMMUTABLE);
    expect(res.headers.get('etag')).toBe('"abc123-t"');
    expect(res.headers.get('content-type')).toBe('image/webp');
    expect(res.headers.get('content-length')).toBe('3');
    expect(res.headers.get('content-disposition')).toBe('inline; filename="image.webp"');
    expect(res.headers.get('x-content-type-options')).toBe('nosniff');
    expect(res.headers.get('content-security-policy')).toBe("default-src 'none'; sandbox");
    expect(new Uint8Array(await res.arrayBuffer())).toEqual(new Uint8Array([1, 2, 3]));
  });

  it('keeps nosniff and the sandbox even if the upstream ever omits them', async () => {
    stubOrchestrator(
      () => new Response(new Uint8Array([1]), { status: 200, headers: { 'content-type': 'image/png' } }),
    );
    const res = await getPhoto(new Request('http://x/'), photoCtx(CONV, ATT));
    expect(res.headers.get('x-content-type-options')).toBe('nosniff');
    expect(res.headers.get('content-security-policy')).toBe("default-src 'none'; sandbox");
  });

  it('streams: it answers before the upstream body has finished', async () => {
    let release!: () => void;
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        controller.enqueue(new Uint8Array([7]));
        // The rest never arrives until the test says so. A proxy that
        // buffered would never return.
        release = () => {
          controller.enqueue(new Uint8Array([8]));
          controller.close();
        };
      },
    });
    stubOrchestrator(() => upstreamPhoto(body, { 'content-length': '2' }));
    const res = await getPhoto(new Request('http://x/'), photoCtx(CONV, ATT));
    expect(res.status).toBe(200);
    const reader = res.body!.getReader();
    expect((await reader.read()).value).toEqual(new Uint8Array([7]));
    release();
    expect((await reader.read()).value).toEqual(new Uint8Array([8]));
  });

  it('relays 304 with no body, and the ETag', async () => {
    stubOrchestrator(
      () => new Response(null, { status: 304, headers: { etag: '"abc123"', 'cache-control': IMMUTABLE } }),
    );
    const res = await getPhoto(
      new Request('http://x/', { headers: { 'if-none-match': '"abc123"' } }),
      photoCtx(CONV, ATT),
    );
    expect(res.status).toBe(304);
    expect(res.body).toBeNull();
    expect(res.headers.get('etag')).toBe('"abc123"');
    expect(res.headers.get('cache-control')).toBe(IMMUTABLE);
  });

  it('relays 404 and 410 as themselves, never cached, in its own words', async () => {
    for (const [status, code] of [
      [404, 'not_found'],
      [410, 'media_missing'],
    ] as const) {
      stubOrchestrator(() => Response.json({ detail: 'upstream prose' }, { status }));
      const res = await getPhoto(new Request('http://x/'), photoCtx(CONV, ATT));
      expect(res.status).toBe(status);
      expect(res.headers.get('cache-control')).toBe('no-store');
      const body = await res.json();
      expect(body).toEqual({ code });
      expect(JSON.stringify(body)).not.toContain('upstream prose');
    }
  });

  it('passes a 401 through, and answers 502 when the orchestrator is unreachable', async () => {
    stubOrchestrator(() => Response.json({}, { status: 401 }));
    expect((await getPhoto(new Request('http://x/'), photoCtx(CONV, ATT))).status).toBe(401);
    stubOrchestrator(() => {
      throw new TypeError('fetch failed');
    });
    expect((await getPhoto(new Request('http://x/'), photoCtx(CONV, ATT))).status).toBe(502);
  });
});

/* ===================================================== list and upload */

describe('GET /api/chat-media/[conversation]', () => {
  it('validates, forwards the cookie and relays the list uncached', async () => {
    const fetchMock = stubOrchestrator(() => Response.json({ items: [{ attachment_id: ATT }] }));
    expect((await listPhotos(new Request('http://x/'), convCtx('../x'))).status).toBe(400);
    expect(fetchMock).not.toHaveBeenCalled();

    const res = await listPhotos(
      new Request('http://x/', { headers: { cookie: 'ts_session=s1' } }),
      convCtx(CONV),
    );
    expect(fetchMock.mock.calls[0][0]).toBe(`${ORCH}/chat-media/${CONV}`);
    expect((fetchMock.mock.calls[0][1]?.headers as Record<string, string>).cookie).toBe('ts_session=s1');
    expect(res.headers.get('cache-control')).toBe('no-store');
    expect(await res.json()).toEqual({ items: [{ attachment_id: ATT }] });
  });
});

describe('POST /api/chat-media/[conversation]', () => {
  function multipart(init: { headers?: Record<string, string>; body?: BodyInit } = {}) {
    const form = new FormData();
    form.append('file', new Blob([new Uint8Array([1, 2, 3])], { type: 'image/png' }), 'image-1.png');
    form.append('attachment_id', 'bf-aaaaaaaa');
    form.append('source', 'backfill');
    const probe = new Request('http://x/', { method: 'POST', body: form });
    return new Request('http://x/', {
      method: 'POST',
      headers: {
        'content-type': probe.headers.get('content-type')!,
        cookie: 'ts_session=s1',
        ...(init.headers ?? {}),
      },
      body: init.body ?? probe.body,
      duplex: 'half',
    } as RequestInit & { duplex: 'half' });
  }

  it('validates the conversation before any fetch', async () => {
    const fetchMock = stubOrchestrator(() => Response.json({ items: [] }));
    for (const bad of ['../..', 'a b', '', 'a'.repeat(65)]) {
      expect((await postPhotos(multipart(), convCtx(bad))).status).toBe(400);
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('refuses a cross-site write', async () => {
    const fetchMock = stubOrchestrator(() => Response.json({ items: [] }));
    const res = await postPhotos(
      multipart({ headers: { 'sec-fetch-site': 'same-site' } }),
      convCtx(CONV),
    );
    expect(res.status).toBe(403);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('refuses anything that is not multipart', async () => {
    const fetchMock = stubOrchestrator(() => Response.json({ items: [] }));
    const res = await postPhotos(
      new Request('http://x/', {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: '{}',
      }),
      convCtx(CONV),
    );
    expect(res.status).toBe(400);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('refuses a body declared over 64 MiB without reading a byte', async () => {
    const fetchMock = stubOrchestrator(() => Response.json({ items: [] }));
    const res = await postPhotos(
      multipart({ headers: { 'content-length': String(MAX_MEDIA_BODY_BYTES + 1) } }),
      convCtx(CONV),
    );
    expect(res.status).toBe(413);
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('streams the multipart body (duplex half) with its boundary and the cookie', async () => {
    let received: { duplex?: string; body?: unknown; headers?: Record<string, string> } = {};
    let bytes = 0;
    const fetchMock = stubOrchestrator(async (_url, init) => {
      received = init as typeof received;
      // Read the forwarded stream the way undici would.
      const reader = (init?.body as ReadableStream<Uint8Array>).getReader();
      for (;;) {
        const { done, value } = await reader.read();
        if (done) break;
        bytes += value.byteLength;
      }
      return Response.json({ items: [{ attachment_id: 'bf-aaaaaaaa', created: true }] });
    });
    const req = multipart();
    const res = await postPhotos(req, convCtx(CONV));
    expect(res.status).toBe(200);
    expect(fetchMock.mock.calls[0][0]).toBe(`${ORCH}/chat-media/${CONV}`);
    expect(received.duplex).toBe('half');
    expect(received.body).toBeInstanceOf(ReadableStream);
    expect(received.headers?.['content-type']).toMatch(/^multipart\/form-data; boundary=/);
    expect(received.headers?.cookie).toBe('ts_session=s1');
    expect(bytes).toBeGreaterThan(3);
    expect(await res.json()).toEqual({ items: [{ attachment_id: 'bf-aaaaaaaa', created: true }] });
  });

  it('cuts a body that runs past the cap as it streams, with 413', async () => {
    stubOrchestrator(async (_url, init) => {
      const reader = (init?.body as ReadableStream<Uint8Array>).getReader();
      for (;;) {
        // The bounded stream errors the read once the cap is passed; undici
        // would surface that as the fetch's own TypeError with this cause.
        try {
          const { done } = await reader.read();
          if (done) break;
        } catch (cause) {
          throw new TypeError('fetch failed', { cause });
        }
      }
      return Response.json({ items: [] });
    });
    const chunk = new Uint8Array(8 * 1024 * 1024);
    let sent = 0;
    const body = new ReadableStream<Uint8Array>({
      pull(controller) {
        if (sent > MAX_MEDIA_BODY_BYTES + chunk.byteLength) {
          controller.close();
          return;
        }
        sent += chunk.byteLength;
        controller.enqueue(chunk);
      },
    });
    const res = await postPhotos(
      new Request('http://x/', {
        method: 'POST',
        headers: { 'content-type': 'multipart/form-data; boundary=x' },
        body,
        duplex: 'half',
      } as RequestInit & { duplex: 'half' }),
      convCtx(CONV),
    );
    expect(res.status).toBe(413);
  });

  it('relays the orchestrator status: 507 stays 507, 415 stays 415', async () => {
    for (const status of [415, 507, 404]) {
      stubOrchestrator(() => Response.json({ code: 'x' }, { status }));
      const res = await postPhotos(multipart(), convCtx(CONV));
      expect(res.status).toBe(status);
      expect(res.headers.get('cache-control')).toBe('no-store');
    }
  });
});
