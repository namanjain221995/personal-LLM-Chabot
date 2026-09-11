/**
 * 512 MB documents (2026-09-02): the transport, not the engine, owned the old
 * 25 MB cap — a PDF travelled as base64 inside the chat JSON. Big documents
 * now stream to /api/upload (purpose=document) and the chat request carries a
 * reference; past Cloudflare's 100 MB edge cap they travel in parts.
 *
 * 2026-09-10 (upload reliability): the same client, made RESUMABLE. What is
 * pinned here is everything a 400 MB video on a hotel network depends on —
 * the threshold, the per-part hash, which failures are retried and which are
 * final, resuming from what the SERVER says it has rather than from byte 0,
 * an idempotent complete, and an abort that never finalises an upload the
 * person cancelled. See docs/upload-reliability/API.md.
 */
import { createHash } from 'node:crypto';
import { describe, expect, it, vi, afterEach } from 'vitest';
import {
  CHUNK_PART_BYTES,
  CHUNK_THRESHOLD_BYTES,
  UploadError,
  uploadDocumentFile,
  type UploadProgress,
} from '@/lib/uploadDocument';
import { toOrchestratorChatRequest, PDF_ONLY_PROMPT } from '@/lib/orchestrator';
import { DELETE, GET, PUT } from '@/app/api/upload/chunked/[...path]/route';

afterEach(() => {
  vi.unstubAllGlobals();
  vi.useRealTimers();
});

const okJson = (body: unknown) => ({
  ok: true,
  status: 200,
  json: async () => body,
  text: async () => JSON.stringify(body),
});

const failJson = (status: number, body: unknown) => ({
  ok: false,
  status,
  json: async () => body,
  text: async () => JSON.stringify(body),
});

const failText = (status: number, body: string) => ({
  ok: false,
  status,
  json: async () => {
    throw new SyntaxError('not JSON');
  },
  text: async () => body,
});

function fakeFile(size: number, name = 'big.pdf'): File {
  // File.slice is all the uploader touches — a real half-gigabyte Blob would
  // make the suite pay for bytes the code never reads. The slice IS real, so
  // the SHA-256 the client computes is a real digest of real bytes.
  return {
    name,
    size,
    slice(start: number) {
      return new Blob([`part:${start}`]);
    },
  } as unknown as File;
}

/**
 * Only the clock this client actually waits on.
 *
 * Faking the whole timer surface takes `queueMicrotask`/`setImmediate` with
 * it, and Node's own `Blob.arrayBuffer()` — which the part hash reads — never
 * settles then, so a backoff test would hang on the hash rather than on the
 * backoff it is about.
 */
const FAKE_ONLY_TIMERS: Array<'setTimeout' | 'clearTimeout'> = ['setTimeout', 'clearTimeout'];

/**
 * Run a pending upload to its end under fake timers.
 *
 * One `advanceTimersByTimeAsync` is not enough: the next backoff is only
 * SCHEDULED once the fetch and hash promises that follow the previous one
 * have settled, so the advance has to be repeated, yielding in between, until
 * the upload itself has settled.
 */
async function drain<T>(promise: Promise<T>): Promise<void> {
  let settled = false;
  const watch = promise.then(
    () => (settled = true),
    () => (settled = true),
  );
  // Bounded by REAL time, not by a fixed number of advances. Between two
  // backoffs the upload awaits things that are not timers — a fetch, and
  // crypto.subtle.digest over a 64 MiB part — and on a loaded machine those
  // resolve later than the advances that were meant to outlast them. A fixed
  // count ran out first, left a scheduled fake backoff with nobody to advance
  // it, and the test hung until vitest's own timeout; it passed alone and
  // failed in a full run, which is the worst way for a test to fail.
  const deadline = Date.now() + 4000;
  while (!settled && Date.now() < deadline) {
    await vi.advanceTimersByTimeAsync(RETRY_CAP_MS + 1);
    // Yield to the macrotask queue so non-timer work can finish before the
    // next advance; setImmediate is not faked (see FAKE_ONLY_TIMERS).
    await new Promise((resolve) => setImmediate(resolve));
  }
  await watch;
}

/** The client's own ceiling on one backoff — see uploadDocument.ts. */
const RETRY_CAP_MS = 8000;

/** The last path segment pair of a chunked URL, for readable assertions. */
const partIndex = (url: string) => Number(url.slice(url.lastIndexOf('/') + 1));

describe('uploadDocumentFile · which road a file takes', () => {
  it('small files take ONE request, with purpose=document', async () => {
    const calls: Array<{ url: string; init: RequestInit }> = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init: RequestInit) => {
        calls.push({ url, init });
        return okJson({ upload_id: 'a'.repeat(32), filename: 'small.pdf', bytes: 1024 });
      }),
    );
    const ref = await uploadDocumentFile(fakeFile(1024, 'small.pdf'), 'conv-1');
    expect(ref).toEqual({ upload_id: 'a'.repeat(32), name: 'small.pdf', bytes: 1024 });
    expect(calls).toHaveLength(1);
    expect(calls[0].url).toBe('/api/upload');
    const form = calls[0].init.body as FormData;
    expect(form.get('purpose')).toBe('document');
    expect(form.get('conversation_id')).toBe('conv-1');
  });

  it('EXACTLY at the threshold is still one request; one byte over is chunked', async () => {
    const urls: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        urls.push(String(url));
        return okJson({ upload_id: 'b'.repeat(32), filename: 'edge.pdf' });
      }),
    );
    await uploadDocumentFile(fakeFile(CHUNK_THRESHOLD_BYTES, 'edge.pdf'), 'conv-1');
    expect(urls).toEqual(['/api/upload']);

    urls.length = 0;
    await uploadDocumentFile(fakeFile(CHUNK_THRESHOLD_BYTES + 1, 'edge.pdf'), 'conv-1');
    expect(urls[0]).toBe('/api/upload/chunked/init');
  });

  it('a 512 MB file becomes init + ceil(size/part) parts + complete', async () => {
    const urls: string[] = [];
    let initForm: FormData | null = null;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init: RequestInit) => {
        urls.push(String(url));
        if (String(url).endsWith('/init')) initForm = init.body as FormData;
        return okJson({ upload_id: 'b'.repeat(32), filename: 'big.pdf' });
      }),
    );
    const size = 512 * 1024 * 1024;
    const ref = await uploadDocumentFile(fakeFile(size), 'conv-2');
    expect(ref.upload_id).toBe('b'.repeat(32));
    const parts = Math.ceil(size / CHUNK_PART_BYTES);
    expect(urls[0]).toBe('/api/upload/chunked/init');
    expect(urls.length).toBe(parts + 2); // init + parts + complete
    expect(urls[1]).toContain('/part/0');
    expect(urls[urls.length - 2]).toContain(`/part/${parts - 1}`);
    expect(urls[urls.length - 1]).toContain('/complete');
    // T-04/F10: the server can only refuse a MISSING FINAL PART if it was
    // told how many to expect.
    expect(initForm!.get('size')).toBe(String(size));
    expect(initForm!.get('parts')).toBe(String(parts));
    expect(initForm!.get('part_size')).toBe(String(CHUNK_PART_BYTES));
    // Every part clears the Cloudflare edge cap with room to spare.
    expect(CHUNK_PART_BYTES).toBeLessThan(95 * 1024 * 1024);
    expect(CHUNK_THRESHOLD_BYTES).toBeLessThan(100 * 1024 * 1024);
  });

  it('reports uploading → finalizing → uploaded for a single-shot upload', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => okJson({ upload_id: 'c'.repeat(32) })));
    const states: string[] = [];
    await uploadDocumentFile(fakeFile(2048, 'small.pdf'), 'conv-1', 'document', {
      onProgress: (p: UploadProgress) => states.push(p.state),
    });
    // No invented percentage in between: one request either arrives or not.
    expect(states).toEqual(['uploading', 'finalizing', 'uploaded']);
  });
});

describe('uploadDocumentFile · every part carries its hash', () => {
  it('sends X-Part-SHA256 matching the digest of the part it is on', async () => {
    const seen: Array<{ index: number; hash: string | undefined }> = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init: RequestInit) => {
        const u = String(url);
        if (u.includes('/part/')) {
          const headers = (init.headers ?? {}) as Record<string, string>;
          seen.push({ index: partIndex(u), hash: headers['X-Part-SHA256'] });
        }
        return okJson({ upload_id: 'd'.repeat(32), filename: 'big.pdf' });
      }),
    );
    await uploadDocumentFile(fakeFile(CHUNK_THRESHOLD_BYTES + 1), 'conv-3');
    expect(seen.length).toBeGreaterThan(0);
    // The fake file's slice is `part:<start>` — a known buffer.
    expect(seen[0].hash).toBe(createHash('sha256').update('part:0').digest('hex'));
    expect(seen[1].hash).toBe(
      createHash('sha256').update(`part:${CHUNK_PART_BYTES}`).digest('hex'),
    );
  });

  it('omits the header rather than failing where crypto.subtle does not exist', async () => {
    const headers: Array<Record<string, string> | undefined> = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init: RequestInit) => {
        if (String(url).includes('/part/')) {
          headers.push(init.headers as Record<string, string> | undefined);
        }
        return okJson({ upload_id: 'e'.repeat(32) });
      }),
    );
    // A plain-http LAN address is not a secure context, and there is no
    // subtle there. The upload must still work: the server only checks the
    // hash it is given.
    vi.stubGlobal('crypto', { randomUUID: () => 'x' });
    await uploadDocumentFile(fakeFile(CHUNK_THRESHOLD_BYTES + 1), 'conv-3');
    expect(headers.length).toBeGreaterThan(0);
    expect(headers[0]).toBeUndefined();
  });
});

describe('uploadDocumentFile · what is retried and what is not', () => {
  it('retries a 503 part with backoff and then succeeds', async () => {
    vi.useFakeTimers({ toFake: FAKE_ONLY_TIMERS });
    let partAttempts = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        const u = String(url);
        if (u.includes('/part/0')) {
          partAttempts += 1;
          if (partAttempts < 3) return failJson(503, { detail: 'upstream restarting' });
        }
        return okJson({ upload_id: 'f'.repeat(32), filename: 'big.pdf' });
      }),
    );
    const promise = uploadDocumentFile(fakeFile(CHUNK_THRESHOLD_BYTES + 1), 'conv-4');
    // Nothing here waits on wall-clock time except the backoff itself.
    await drain(promise);
    const ref = await promise;
    expect(ref.upload_id).toBe('f'.repeat(32));
    expect(partAttempts).toBe(3);
  });

  it('does NOT retry a 413, and says so', async () => {
    let attempts = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        if (String(url).includes('/part/')) {
          attempts += 1;
          return failJson(413, { detail: 'part exceeds 90 MB' });
        }
        return okJson({ upload_id: 'g'.repeat(32) });
      }),
    );
    const err = await uploadDocumentFile(
      fakeFile(CHUNK_THRESHOLD_BYTES + 1),
      'conv-5',
    ).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(UploadError);
    const failure = err as UploadError;
    expect(failure.status).toBe(413);
    expect(failure.retryable).toBe(false);
    // A refusal is surfaced verbatim: it is the one thing the person can act on.
    expect(failure.message).toMatch(/part exceeds 90 MB/);
    expect(attempts).toBe(1);
  });

  it('turns a proxy HTML page into a sentence naming the status', async () => {
    vi.useFakeTimers({ toFake: FAKE_ONLY_TIMERS });
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        failText(502, '<html><head><title>502 Bad Gateway</title></head><body>…</body></html>'),
      ),
    );
    const promise = uploadDocumentFile(fakeFile(1024, 'small.pdf'), 'conv-6').catch(
      (e: unknown) => e,
    );
    await drain(promise);
    const err = (await promise) as UploadError;
    expect(err).toBeInstanceOf(UploadError);
    expect(err.message).toContain('502');
    expect(err.message).not.toContain('<');
    expect(err.retryable).toBe(true);
  });
});

describe('uploadDocumentFile · resuming instead of starting over', () => {
  it('sends only the parts the server says are missing', async () => {
    const size = CHUNK_PART_BYTES * 4;
    const sent: number[] = [];
    let completes = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        const u = String(url);
        if (u.endsWith('/init')) throw new Error('a resume must not re-init');
        if (u.includes('/part/')) {
          sent.push(partIndex(u));
          return okJson({ received: 1, accepted_parts: [0, 1, 2, ...sent] });
        }
        if (u.endsWith('/complete')) {
          completes += 1;
          return okJson({ upload_id: 'h'.repeat(32), filename: 'big.mp4', bytes: size });
        }
        // The resume call itself.
        return okJson({
          upload_id: 'h'.repeat(32),
          status: 'uploading',
          accepted_parts: [0, 1, 2],
          bytes_received: CHUNK_PART_BYTES * 3,
          result: null,
        });
      }),
    );
    const ref = await uploadDocumentFile(fakeFile(size, 'big.mp4'), 'conv-7', 'video', {
      resume: { uploadId: 'h'.repeat(32) },
    });
    expect(sent).toEqual([3]);
    expect(completes).toBe(1);
    expect(ref).toEqual({ upload_id: 'h'.repeat(32), name: 'big.mp4', bytes: size });
  });

  it('a session the server already finalised returns its stored result, sending nothing', async () => {
    const calls: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        calls.push(String(url));
        return okJson({
          upload_id: 'i'.repeat(32),
          status: 'complete',
          accepted_parts: [0, 1],
          result: { upload_id: 'i'.repeat(32), filename: 'done.mp4', bytes: 7 },
        });
      }),
    );
    const ref = await uploadDocumentFile(fakeFile(CHUNK_PART_BYTES * 2, 'done.mp4'), 'c', 'video', {
      resume: { uploadId: 'i'.repeat(32) },
    });
    expect(ref).toEqual({ upload_id: 'i'.repeat(32), name: 'done.mp4', bytes: 7 });
    expect(calls).toEqual(['/api/upload/chunked/c/' + 'i'.repeat(32)]);
  });

  it('refuses to resume a session that was opened for a different file', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () =>
        okJson({
          upload_id: 'm'.repeat(32),
          status: 'uploading',
          // The session belongs to a 999-byte file; this one is not it.
          expected_bytes: 999,
          accepted_parts: [0],
          result: null,
        }),
      ),
    );
    const err = await uploadDocumentFile(
      fakeFile(CHUNK_PART_BYTES * 2, 'other.mp4'),
      'conv-11',
      'video',
      { resume: { uploadId: 'm'.repeat(32) } },
    ).catch((e: unknown) => e);
    expect(err).toBeInstanceOf(UploadError);
    expect((err as UploadError).code).toBe('session_mismatch');
    expect((err as UploadError).retryable).toBe(false);
  });

  it('a complete replayed after a proxy blip still yields ONE reference', async () => {
    vi.useFakeTimers({ toFake: FAKE_ONLY_TIMERS });
    const parts: number[] = [];
    let completes = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        const u = String(url);
        if (u.includes('/part/')) {
          parts.push(partIndex(u));
          return okJson({ accepted_parts: parts });
        }
        if (u.endsWith('/complete')) {
          completes += 1;
          // The first answer never reaches us; the replay returns the SAME
          // body, because the server finalises exactly once.
          if (completes === 1) return failJson(504, { detail: 'gateway timeout' });
          return okJson({ upload_id: 'j'.repeat(32), filename: 'big.pdf', bytes: 99 });
        }
        return okJson({ upload_id: 'j'.repeat(32) });
      }),
    );
    const promise = uploadDocumentFile(fakeFile(CHUNK_THRESHOLD_BYTES + 1), 'conv-8');
    await drain(promise);
    const ref = await promise;
    expect(completes).toBe(2);
    expect(ref).toEqual({ upload_id: 'j'.repeat(32), name: 'big.pdf', bytes: 99 });
    // The bytes were not sent twice to get there.
    expect(parts).toEqual([0, 1]);
  });

  it('re-reads the session after a transient failure and skips what landed', async () => {
    vi.useFakeTimers({ toFake: FAKE_ONLY_TIMERS });
    const size = CHUNK_PART_BYTES * 3;
    const sent: number[] = [];
    let part1Attempts = 0;
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        const u = String(url);
        if (u.endsWith('/init')) return okJson({ upload_id: 'k'.repeat(32), accepted_parts: [] });
        if (u.endsWith('/complete')) return okJson({ upload_id: 'k'.repeat(32), filename: 'x.mp4' });
        if (u.includes('/part/')) {
          const index = partIndex(u);
          if (index === 1) {
            part1Attempts += 1;
            // Every attempt at part 1 fails transiently: the ACK is what is
            // lost, not the bytes — the session below shows it arrived.
            return failJson(502, { detail: 'bad gateway' });
          }
          sent.push(index);
          return okJson({ accepted_parts: sent });
        }
        // The session read after the failed pass.
        return okJson({
          upload_id: 'k'.repeat(32),
          status: 'uploading',
          accepted_parts: [0, 1],
          result: null,
        });
      }),
    );
    const promise = uploadDocumentFile(fakeFile(size, 'x.mp4'), 'conv-9', 'video');
    await drain(promise);
    await promise;
    // Five attempts at part 1, then the session says it landed after all, so
    // only part 2 remains — part 0 is never re-sent.
    expect(part1Attempts).toBe(5);
    expect(sent).toEqual([0, 2]);
  });
});

describe('uploadDocumentFile · cancelling', () => {
  it('an abort mid-part rejects as AbortError and never sends complete', async () => {
    const controller = new AbortController();
    const urls: string[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string) => {
        const u = String(url);
        urls.push(u);
        if (u.includes('/part/0')) controller.abort();
        return okJson({ upload_id: 'l'.repeat(32) });
      }),
    );
    const err = await uploadDocumentFile(
      fakeFile(CHUNK_THRESHOLD_BYTES + 1),
      'conv-10',
      'document',
      { signal: controller.signal },
    ).catch((e: unknown) => e);
    expect((err as Error).name).toBe('AbortError');
    expect(urls.some((u) => u.endsWith('/complete'))).toBe(false);
    expect(urls.some((u) => u.includes('/part/1'))).toBe(false);
  });
});

describe('the chunked proxy carries every method the rail uses', () => {
  const ctx = (...path: string[]) => ({ params: Promise.resolve({ path }) });
  const conv = '7c9b6cb2-beb8-4e71-affa-9bfc7bad676d';

  it('GET is the resume call: no body, and the upstream status passes through', async () => {
    const seen: Array<{ url: string; init: RequestInit }> = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init: RequestInit) => {
        seen.push({ url: String(url), init });
        return new Response(JSON.stringify({ detail: 'no such upload' }), {
          status: 404,
          headers: { 'content-type': 'application/json' },
        });
      }),
    );
    const res = await GET(
      new Request(`http://t/api/upload/chunked/${conv}/${'a'.repeat(32)}`),
      ctx(conv, 'a'.repeat(32)),
    );
    expect(res.status).toBe(404);
    await expect(res.json()).resolves.toEqual({ detail: 'no such upload' });
    expect(seen[0].url).toBe(`http://localhost:8080/uploads/chunked/${conv}/${'a'.repeat(32)}`);
    // undici rejects a GET that declares a body at all, streamed or not.
    expect(seen[0].init.body).toBeUndefined();
    expect((seen[0].init as { duplex?: string }).duplex).toBeUndefined();
  });

  it('DELETE gives a session back, and 204 comes through without a body', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => new Response(null, { status: 204 })));
    const res = await DELETE(
      new Request(`http://t/api/upload/chunked/${conv}/${'b'.repeat(32)}`, {
        method: 'DELETE',
      }),
      ctx(conv, 'b'.repeat(32)),
    );
    // Constructing a Response with a body at 204 throws — so it must not.
    expect(res.status).toBe(204);
    expect(res.body).toBeNull();
  });

  it('a part PUT still streams, and carries the hash header untouched', async () => {
    const seen: RequestInit[] = [];
    vi.stubGlobal(
      'fetch',
      vi.fn(async (_url: string, init: RequestInit) => {
        seen.push(init);
        return new Response('{}', { status: 200, headers: { 'content-type': 'application/json' } });
      }),
    );
    const hash = createHash('sha256').update('part').digest('hex');
    const res = await PUT(
      new Request(`http://t/api/upload/chunked/${conv}/${'c'.repeat(32)}/part/0`, {
        method: 'PUT',
        body: 'part',
        headers: { 'x-part-sha256': hash },
      }),
      ctx(conv, 'c'.repeat(32), 'part', '0'),
    );
    expect(res.status).toBe(200);
    const headers = seen[0].headers as Record<string, string>;
    expect(headers['x-part-sha256']).toBe(hash);
    expect((seen[0] as { duplex?: string }).duplex).toBe('half');
    expect(seen[0].body).toBeTruthy();
  });

  it('refuses a path it did not mint, before any request leaves', async () => {
    const fetchSpy = vi.fn();
    vi.stubGlobal('fetch', fetchSpy);
    const res = await GET(
      new Request('http://t/api/upload/chunked/../../etc'),
      ctx('..', '..', 'etc'),
    );
    expect(res.status).toBe(400);
    expect(fetchSpy).not.toHaveBeenCalled();
  });
});

describe('the chat translator and streamed documents', () => {
  it('forwards pdf_uploads to the orchestrator', () => {
    const out = toOrchestratorChatRequest({
      session_id: 's',
      messages: [{ role: 'user', content: 'compare them' }],
      pdf_uploads: [{ upload_id: 'd'.repeat(32), name: 'contract.pdf' }],
    } as never);
    expect(out?.pdf_uploads).toEqual([
      { upload_id: 'd'.repeat(32), name: 'contract.pdf' },
    ]);
  });

  it('a wordless reference-only send is a document question, not a 400', () => {
    const out = toOrchestratorChatRequest({
      session_id: 's',
      messages: [],
      pdf_uploads: [{ upload_id: 'e'.repeat(32), name: 'x.pdf' }],
    } as never);
    expect(out).not.toBeNull();
    expect(out?.message).toBe(PDF_ONLY_PROMPT);
  });

  it('an inline-only send keeps its exact v1 key set', () => {
    const out = toOrchestratorChatRequest({
      session_id: 's',
      messages: [{ role: 'user', content: 'hi' }],
    } as never);
    expect(out && 'pdf_uploads' in out).toBe(false);
  });
});
