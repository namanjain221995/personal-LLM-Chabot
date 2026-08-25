/**
 * H-03: the /api/reports/[filename] download proxy.
 *
 * Two halves are covered.
 *
 * `isSafeReportName` is the security-relevant half with no network in it.
 * Next.js has already percent-decoded the segment by the time it reaches the
 * handler, so both the decoded and the still-encoded shapes are checked.
 *
 * `GET` is the half that carries the bytes. The assertions here are mostly
 * about things NOT happening: the response must not be re-encoded (a report is
 * a binary file), the upstream status must not be flattened (a deleted report
 * is a 404, not a proxy failure), and a rejected filename must never become an
 * outbound request. There is deliberately NO extension allowlist to assert —
 * the proxy is format-agnostic by design, and the per-format table below
 * exists to prove that a new format needs no change here.
 */
import { afterEach, describe, expect, it, vi } from 'vitest';

import { GET, isSafeReportName } from '@/app/api/reports/[filename]/route';

describe('isSafeReportName', () => {
  it('accepts the filenames the orchestrator generates', () => {
    expect(isSafeReportName('data-report-sample-opportunities-csv-20260824-170305.pdf')).toBe(true);
    expect(isSafeReportName('q3-review-20260101-000000.docx')).toBe(true);
    expect(isSafeReportName('query-export-20260101-000000.xlsx')).toBe(true);
  });

  it.each([
    ['empty', ''],
    ['whitespace only', '   '],
    ['dot segment', '..'],
    ['decoded traversal', '../../etc/passwd'],
    ['forward slash', 'sub/file.pdf'],
    ['backslash', 'sub\\file.pdf'],
    ['absolute path', '/etc/passwd'],
    ['hidden file', '.env'],
    ['null byte', 'a\0b.pdf'],
    // Decoded once by Next; a double-encoded attempt still reads as %2e here
    // and must not travel upstream as literal text.
    ['still-encoded dot', '%2e%2e/passwd'],
    ['still-encoded slash', 'a%2fb.pdf'],
    ['untrimmed', ' report.pdf'],
  ])('rejects %s', (_label, name) => {
    expect(isSafeReportName(name)).toBe(false);
  });
});

// --- GET ---------------------------------------------------------------------

/** The route receives `params` as a promise (Next dynamic route contract). */
const ctx = (filename: string) => ({ params: Promise.resolve({ filename }) });
const req = () => new Request('http://localhost:3001/api/reports/x');

const bytes = (...values: number[]) => new Uint8Array(values);
const text = (value: string) => new TextEncoder().encode(value);

/**
 * Every fixture carries at least one byte that is not valid UTF-8 (0x00,
 * 0x80, 0xFE, 0xFF). If anything on the path decoded the body to a string and
 * re-encoded it, those bytes would come back as U+FFFD and the comparison
 * would fail — which is exactly the regression this guards.
 */
const FORMATS = [
  {
    ext: 'pdf',
    type: 'application/pdf',
    body: bytes(0x25, 0x50, 0x44, 0x46, 0x2d, 0x31, 0x2e, 0x37, 0x0a, 0x00, 0xff, 0x80),
  },
  {
    ext: 'docx',
    type: 'application/vnd.openxmlformats-officedocument.wordprocessingml.document',
    body: bytes(0x50, 0x4b, 0x03, 0x04, 0x14, 0x00, 0x00, 0xfe, 0xff, 0x80),
  },
  {
    ext: 'xlsx',
    type: 'application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
    body: bytes(0x50, 0x4b, 0x03, 0x04, 0x0a, 0x00, 0xff, 0x00, 0x81),
  },
  {
    ext: 'csv',
    type: 'text/csv; charset=utf-8',
    body: text('employee_id,name,department\nE001,Aisha Rahman,Engineering\n'),
  },
  { ext: 'txt', type: 'text/plain; charset=utf-8', body: text('25 rows x 9 columns\n') },
  { ext: 'md', type: 'text/markdown; charset=utf-8', body: text('# Data Report\n\n| a |\n') },
  {
    ext: 'html',
    type: 'text/html; charset=utf-8',
    body: text('<!doctype html><meta charset="utf-8"><title>Data Report</title>'),
  },
] as const;

const nameFor = (ext: string) => `data-report-employees-test-csv-20260825-140000.${ext}`;

/**
 * Stub the upstream and hand back the spy, so callers can assert on it.
 *
 * The parameters are declared even though the stub ignores them: without a
 * signature, `mock.calls` types as `[]` and indexing it is a compile error.
 */
function stubUpstream(response: Response) {
  const fetchMock = vi.fn(async (_url: string, _init?: RequestInit) => response);
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

afterEach(() => {
  vi.unstubAllGlobals();
  vi.unstubAllEnvs();
});

describe('GET — successful passthrough, every format', () => {
  it.each(FORMATS.map((f) => [f.ext, f] as const))(
    'passes a .%s through byte for byte',
    async (ext, format) => {
      vi.stubEnv('ORCHESTRATOR_URL', 'http://orchestrator:8080');
      const filename = nameFor(ext);
      const disposition = `attachment; filename="${filename}"`;
      const fetchMock = stubUpstream(
        new Response(format.body, {
          status: 200,
          headers: { 'content-type': format.type, 'content-disposition': disposition },
        }),
      );

      const res = await GET(req(), ctx(filename));

      expect(res.status).toBe(200);
      expect(res.headers.get('content-type')).toBe(format.type);
      expect(res.headers.get('content-disposition')).toBe(disposition);

      // The bytes, unchanged.
      const received = new Uint8Array(await res.arrayBuffer());
      expect(Array.from(received)).toEqual(Array.from(format.body));

      // Exactly one upstream call, to the encoded report path.
      expect(fetchMock).toHaveBeenCalledOnce();
      expect(fetchMock.mock.calls[0][0]).toBe(
        `http://orchestrator:8080/reports/${encodeURIComponent(filename)}`,
      );
    },
  );

  it('never converts a binary body to text', async () => {
    // A lone 0x80 is an invalid UTF-8 continuation byte: any decode/encode
    // round trip turns it into EF BF BD (U+FFFD).
    const raw = bytes(0x80, 0x81, 0xfe, 0xff, 0x00);
    stubUpstream(
      new Response(raw, {
        status: 200,
        headers: { 'content-type': 'application/octet-stream' },
      }),
    );
    const received = new Uint8Array(
      await (await GET(req(), ctx('report-20260101-000000.bin'))).arrayBuffer(),
    );
    expect(Array.from(received)).toEqual([0x80, 0x81, 0xfe, 0xff, 0x00]);
    expect(received).not.toContain(0xef); // no U+FFFD replacement char
  });

  it('preserves Content-Length when the orchestrator supplies it', async () => {
    const body = bytes(0x25, 0x50, 0x44, 0x46, 0x00, 0xff);
    stubUpstream(
      new Response(body, {
        status: 200,
        headers: {
          'content-type': 'application/pdf',
          'content-length': String(body.length),
        },
      }),
    );
    const res = await GET(req(), ctx('report-20260101-000000.pdf'));
    expect(res.headers.get('content-length')).toBe(String(body.length));
  });

  it('marks downloads no-store so a stale report is never served', async () => {
    stubUpstream(
      new Response(bytes(0x25, 0x50), {
        status: 200,
        headers: { 'content-type': 'application/pdf' },
      }),
    );
    const res = await GET(req(), ctx('report-20260101-000000.pdf'));
    expect(res.headers.get('cache-control')).toBe('no-store');
  });

  it('falls back to octet-stream when the upstream names no type', async () => {
    stubUpstream(new Response(bytes(0x01, 0x02), { status: 200 }));
    const res = await GET(req(), ctx('report-20260101-000000.bin'));
    expect(res.headers.get('content-type')).toBe('application/octet-stream');
  });
});

describe('GET — upstream failures keep their meaning', () => {
  it('keeps an upstream 404 a 404 — a deleted report is not a proxy failure', async () => {
    stubUpstream(new Response('not found', { status: 404 }));
    const res = await GET(req(), ctx('gone-20260101-000000.pdf'));
    expect(res.status).toBe(404);
    await expect(res.json()).resolves.toMatchObject({
      message: expect.stringContaining('no longer exists'),
    });
  });

  it('keeps an upstream 500 a 500', async () => {
    stubUpstream(new Response('boom', { status: 500 }));
    const res = await GET(req(), ctx('report-20260101-000000.pdf'));
    expect(res.status).toBe(500);
    await expect(res.json()).resolves.toMatchObject({
      message: expect.stringContaining('could not be downloaded'),
    });
  });

  it.each([400, 403, 500, 502, 503])('passes an upstream %i through', async (status) => {
    stubUpstream(new Response('x', { status }));
    expect((await GET(req(), ctx('report-20260101-000000.pdf'))).status).toBe(status);
  });

  it('reports a 200 with no body as a proxy failure, not a success', async () => {
    stubUpstream(new Response(null, { status: 200 }));
    expect((await GET(req(), ctx('report-20260101-000000.pdf'))).status).toBe(502);
  });

  it('reports an unreachable orchestrator as 502', async () => {
    vi.stubGlobal('fetch', async () => {
      throw new Error('ECONNREFUSED');
    });
    expect((await GET(req(), ctx('report-20260101-000000.pdf'))).status).toBe(502);
  });

  it('leaks no upstream detail into the failure body', async () => {
    stubUpstream(
      new Response('Traceback: connect ECONNREFUSED 10.0.0.4:8080', { status: 500 }),
    );
    const body = await (await GET(req(), ctx('report-20260101-000000.pdf'))).text();
    expect(body).not.toMatch(/Traceback|ECONNREFUSED|10\.0\.0\.4/);
  });
});

describe('GET — an unsafe name never becomes a request', () => {
  it.each([
    ['decoded traversal', '../../etc/passwd'],
    ['dot segment', '..'],
    ['still-encoded traversal', '%2e%2e/passwd'],
    ['double-encoded, decoded once by Next', '%2e%2e%2fetc%2fpasswd'],
    ['still-encoded separator', 'a%2fb.pdf'],
    ['forward slash', 'sub/file.pdf'],
    ['backslash', 'sub\\file.pdf'],
    ['absolute path', '/etc/passwd'],
    ['hidden file', '.env'],
    ['null byte', 'a\0b.pdf'],
    ['empty', ''],
    ['untrimmed', ' report.pdf'],
  ])('rejects %s with 400 and never calls upstream', async (_label, name) => {
    const fetchMock = vi.fn();
    vi.stubGlobal('fetch', fetchMock);

    const res = await GET(req(), ctx(name));

    expect(res.status).toBe(400);
    // The point of the guard: a traversal attempt must not even be attempted.
    expect(fetchMock).not.toHaveBeenCalled();
  });

  it('says nothing about why, beyond "invalid"', async () => {
    vi.stubGlobal('fetch', vi.fn());
    const body = await (await GET(req(), ctx('../../etc/passwd'))).text();
    expect(body).not.toMatch(/etc\/passwd|orchestrator|8080/);
  });
});

describe('GET — no format is privileged', () => {
  it('treats an unknown extension exactly like a known one', async () => {
    // There is no allowlist. This asserts the ABSENCE of one: a format the
    // proxy has never heard of must pass through on the same code path.
    const body = text('future format');
    stubUpstream(
      new Response(body, {
        status: 200,
        headers: { 'content-type': 'application/x-future' },
      }),
    );
    const res = await GET(req(), ctx('report-20260101-000000.future'));
    expect(res.status).toBe(200);
    expect(res.headers.get('content-type')).toBe('application/x-future');
    expect(new Uint8Array(await res.arrayBuffer())).toEqual(body);
  });

  it('accepts every generated extension by name', () => {
    for (const ext of ['pdf', 'docx', 'xlsx', 'csv', 'txt', 'md', 'html']) {
      expect(isSafeReportName(nameFor(ext))).toBe(true);
    }
  });
});
