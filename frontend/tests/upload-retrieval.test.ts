/**
 * PHASE 3 — the bytes were never gone, only unreachable.
 *
 * A dataset streams to /api/upload and stays in the orchestrator's workspace
 * until the TTL sweeps it. The browser kept only a reference, and there was no
 * endpoint to turn that reference back into bytes — so a reload reported a file
 * the server was still answering questions about as "no longer available in
 * this browser session".
 *
 * These cover the read side end to end on the frontend: the proxy's own
 * validation and status handling, and the resolution ladder that now ends at
 * the server instead of at a shrug.
 *
 * The orchestrator half (ownership, 404-vs-410, path safety) is enforced in
 * app/uploads.py and app/core/upload_paths.py; its path-safety logic is pure
 * stdlib and was executed directly against traversal, symlink-escape and
 * malformed-id cases, because this project has no runnable pytest here.
 */

import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { GET, isSafeUploadRef } from '@/app/api/uploads/[conversation]/[upload]/file/route';
import {
  clearAttachments,
  fetchUploadBlob,
  rememberAttachmentFiles,
  resolveAttachment,
  resolveAttachmentAsync,
  uploadFileUrl,
  uploadRefFor,
  attachmentsForResend,
  isDatasetTurn,
} from '@/lib/attachments';

const UPLOAD = 'a'.repeat(32);
const CONV = '7c9b6cb2-beb8-4e71-affa-9bfc7bad676d';

const ctx = (conversation: string, upload: string) => ({
  params: Promise.resolve({ conversation, upload }),
});

beforeEach(() => clearAttachments());
afterEach(() => vi.unstubAllGlobals());

/* ============================================================ the proxy */

describe('P3 · the upload proxy validates before it calls anything', () => {
  it('P3-04 — a malformed upload id never becomes a request', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    for (const bad of ['', '../../etc', 'A'.repeat(32), 'a'.repeat(31), 'g'.repeat(32)]) {
      const res = await GET(new Request('http://x'), ctx(CONV, bad));
      expect(res.status).toBe(400);
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('P3-03/P3-05 — a traversal-shaped conversation id is refused outright', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    for (const bad of ['../..', 'a/b', '..', 'a'.repeat(65), '', 'a b']) {
      const res = await GET(new Request('http://x'), ctx(bad, UPLOAD));
      expect(res.status).toBe(400);
    }
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('accepts exactly the shapes the orchestrator mints', () => {
    expect(isSafeUploadRef(CONV, UPLOAD)).toBe(true);
    expect(isSafeUploadRef(CONV, '0123456789abcdef0123456789abcdef')).toBe(true);
    expect(isSafeUploadRef('conv_1-2', UPLOAD)).toBe(true);
    expect(isSafeUploadRef(CONV, UPLOAD.toUpperCase())).toBe(false);
    expect(isSafeUploadRef('../etc', UPLOAD)).toBe(false);
  });

  it('P3-07 — forwards a successful download, body and headers intact', async () => {
    const body = new ReadableStream();
    vi.stubGlobal(
      'fetch',
      vi.fn(async (url: string, init: RequestInit) => {
        expect(String(url)).toContain(`/uploads/${CONV}/${UPLOAD}/file`);
        // The session must ride along — the OWNER check happens upstream.
        expect((init.headers as Record<string, string>).cookie).toBe('ts_session=x');
        return {
          ok: true,
          status: 200,
          body,
          headers: new Headers({
            'content-type': 'text/csv',
            'content-disposition': 'attachment; filename="sales.csv"',
            'content-length': '42',
          }),
        };
      }),
    );
    const res = await GET(
      new Request('http://x', { headers: { cookie: 'ts_session=x' } }),
      ctx(CONV, UPLOAD),
    );
    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toBe('text/csv');
    expect(res.headers.get('content-disposition')).toBe(
      'attachment; filename="sales.csv"',
    );
    expect(res.headers.get('cache-control')).toBe('no-store');
  });

  it('P3-08 — passes upstream statuses through instead of flattening them', async () => {
    for (const [status, expected] of [
      [401, 401],
      [404, 404],
      [410, 410],
      [500, 500],
    ] as const) {
      vi.stubGlobal(
        'fetch',
        vi.fn(async () => ({ ok: false, status, body: null, headers: new Headers() })),
      );
      const res = await GET(new Request('http://x'), ctx(CONV, UPLOAD));
      expect(res.status).toBe(expected);
    }
  });

  it('P3-06 — an expired upload says so, and is not confused with a 404', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: false, status: 410, body: null, headers: new Headers() })),
    );
    const res = await GET(new Request('http://x'), ctx(CONV, UPLOAD));
    expect(res.status).toBe(410);
    expect((await res.json()).message).toMatch(/expired/i);
  });

  it('an unreachable orchestrator is a 502, not a crash', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('ECONNREFUSED'); }));
    const res = await GET(new Request('http://x'), ctx(CONV, UPLOAD));
    expect(res.status).toBe(502);
  });
});

/* =================================================== the fetch helper */

describe('P3 · fetchUploadBlob', () => {
  it('builds a same-origin URL and never leaks a filesystem path', () => {
    const url = uploadFileUrl({ conversationId: CONV, uploadId: UPLOAD });
    expect(url).toBe(`/api/uploads/${CONV}/${UPLOAD}/file`);
    expect(url.startsWith('/api/')).toBe(true);
  });

  it('reports 410 as expired and everything else as unavailable', async () => {
    const cases: Array<[number, string]> = [
      [410, 'expired'],
      [404, 'unavailable'],
      [401, 'unavailable'],
      [500, 'unavailable'],
    ];
    for (const [status, expected] of cases) {
      vi.stubGlobal(
        'fetch',
        vi.fn(async () => ({ ok: status === 200, status, blob: async () => new Blob() })),
      );
      const out = await fetchUploadBlob({ conversationId: CONV, uploadId: UPLOAD });
      expect(out.status).toBe(expected);
    }
  });

  it('an aborted or offline fetch is unavailable, never "expired"', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => { throw new Error('aborted'); }));
    const out = await fetchUploadBlob({ conversationId: CONV, uploadId: UPLOAD });
    expect(out.status).toBe('unavailable');
  });
});

/* ================================================ the resolution ladder */

describe('P3 · resolveAttachment ladder', () => {
  const datasetMessage = {
    id: 'm1',
    meta: { attachments: [{ id: UPLOAD, name: 'sales.csv', kind: 'dataset' }] },
  };

  it('P3-09 — memory wins, and no request is made', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    rememberAttachmentFiles('m1', [
      { name: 'sales.csv', mime: 'text/csv', blob: new Blob(['a,b\n1,2\n']) },
    ]);

    const out = await resolveAttachmentAsync('m1', 0, {
      name: 'sales.csv',
      upload: { conversationId: CONV, uploadId: UPLOAD },
    });
    expect(out.kind).toBe('text');
    expect(out.blob).not.toBeNull();
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('a persisted image preview also wins over the network', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const out = await resolveAttachmentAsync('m1', 0, {
      name: 'shot.png',
      dataUrl:
        'data:image/png;base64,iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg==',
      upload: { conversationId: CONV, uploadId: UPLOAD },
    });
    expect(out.kind).toBe('image');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('P3-10/P3-11 — with nothing local, the server supplies previewable bytes', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: true,
        status: 200,
        blob: async () => new Blob(['col_a,col_b\n1,2\n'], { type: 'text/csv' }),
      })),
    );
    const out = await resolveAttachmentAsync('gone', 0, {
      name: 'sales.csv',
      upload: { conversationId: CONV, uploadId: UPLOAD },
    });
    expect(out.kind).toBe('text');
    expect(out.size).toBeGreaterThan(0);
    expect(await out.blob!.text()).toContain('col_a');
  });

  it('the renderer is chosen by our allowlist, not by what the server sent', async () => {
    // A server content-type of text/html must not turn a .csv into a page.
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: true,
        status: 200,
        blob: async () => new Blob(['<script>x</script>'], { type: 'text/html' }),
      })),
    );
    const out = await resolveAttachmentAsync('gone', 0, {
      name: 'sales.csv',
      upload: { conversationId: CONV, uploadId: UPLOAD },
    });
    expect(out.kind).toBe('text');
  });

  it('an executable format stays unrenderable even when fetched', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({
        ok: true,
        status: 200,
        blob: async () => new Blob(['<svg onload=alert(1)>'], { type: 'image/svg+xml' }),
      })),
    );
    const out = await resolveAttachmentAsync('gone', 0, {
      name: 'evil.svg',
      upload: { conversationId: CONV, uploadId: UPLOAD },
    });
    expect(out.kind).toBe('none');
  });

  it('P3-06 — a swept upload resolves to `expired`, not `unavailable`', async () => {
    vi.stubGlobal(
      'fetch',
      vi.fn(async () => ({ ok: false, status: 410, blob: async () => new Blob() })),
    );
    const out = await resolveAttachmentAsync('gone', 0, {
      name: 'sales.csv',
      upload: { conversationId: CONV, uploadId: UPLOAD },
    });
    expect(out.kind).toBe('expired');
  });

  it('with no upload id there is no server tier, and no request', async () => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);
    const out = await resolveAttachmentAsync('gone', 0, { name: 'spec.pdf' });
    expect(out.kind).toBe('unavailable');
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('uploadRefFor reads the id Phase 2 made durable', () => {
    expect(uploadRefFor(CONV, datasetMessage, 0)).toEqual({
      conversationId: CONV,
      uploadId: UPLOAD,
    });
    // A PDF/image never has an upload row, and neither does a pre-Phase-2 row.
    expect(uploadRefFor(CONV, { meta: { attachments: [{ name: 'a.pdf', kind: 'pdf' }] } }, 0)).toBeNull();
    expect(uploadRefFor(null, datasetMessage, 0)).toBeNull();
  });

  it('the synchronous resolver is unchanged for render paths', () => {
    expect(resolveAttachment('nope', 0, { name: 'x.csv' }).kind).toBe('unavailable');
  });
});

/* ============================================ dataset regenerate (P3-12) */

describe('P3-12 · regenerating a dataset turn does not demand a re-attach', () => {
  const datasetTurn = {
    id: 'u1',
    pdfName: 'sales.csv',
    meta: { attachments: [{ id: UPLOAD, name: 'sales.csv', kind: 'dataset' }] },
  };

  it('reports nothing missing — the profile is already server-side', () => {
    const out = attachmentsForResend(datasetTurn);
    expect(out.missing).toBe(false);
    expect(out.attachments).toEqual([]);
  });

  it('still reports missing for a DOCUMENT whose bytes really are gone', () => {
    // The PDF path is untouched: its payload only ever lived in this tab.
    expect(attachmentsForResend({ id: 'u2', pdfName: 'spec.pdf' }).missing).toBe(true);
  });

  it('identifies a dataset turn for the chat request as well', () => {
    // Both halves of the fix come from one predicate: no bytes to resend, AND
    // the resend must still declare itself a dataset or it rebuilds NEW-14.
    expect(isDatasetTurn(datasetTurn)).toBe(true);
    expect(isDatasetTurn({ meta: { attachments: [{ kind: 'pdf' }] } })).toBe(false);
    expect(isDatasetTurn({})).toBe(false);
  });
});
