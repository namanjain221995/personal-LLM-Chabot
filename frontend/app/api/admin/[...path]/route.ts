/**
 * /api/admin/* — proxy to the orchestrator's /admin/api/* surface.
 *
 * Authorization lives entirely UPSTREAM (signed out → 401, missing
 * capability → 404, so the admin surface neither confirms its own existence
 * nor which objects exist), which keeps this a thin passthrough: path
 * segments are re-encoded, the query string travels as-is (q / role /
 * status / limit / offset / action / before_id …), and cookies flow both
 * directions via proxyToOrchestrator exactly like the /api/auth and
 * /api/history proxies.
 *
 * The two download endpoints are the exception. proxyToOrchestrator relays
 * only content-type, which would turn "Q3 pipeline.xlsx" into a nameless
 * blob — those are proxied locally so content-disposition (and
 * content-length) survive byte-for-byte, with the body streamed rather than
 * buffered. lib/proxy.ts itself stays untouched.
 */

import { orchestratorUrl, proxyToOrchestrator } from '@/lib/proxy';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

type Ctx = { params: Promise<{ path: string[] }> };

/**
 * The file-returning endpoints: members/{id}/uploads/{uid}/download,
 * members/{id}/reports/{filename}, and the analytics CSV export.
 * Everything else is JSON.
 */
export function isDownloadPath(parts: string[], method: string): boolean {
  if (method !== 'GET') return false;
  // usage-1m-20260903.csv, not a nameless blob rendered in the tab:
  // proxyToOrchestrator relays content-type only.
  if (parts.length === 2 && parts[0] === 'analytics' && parts[1] === 'export') {
    return true;
  }
  if (
    parts.length === 5 &&
    parts[0] === 'members' &&
    parts[2] === 'uploads' &&
    parts[4] === 'download'
  ) {
    return true;
  }
  return parts.length === 4 && parts[0] === 'members' && parts[2] === 'reports';
}

/** Read Set-Cookie headers portably (undici exposes getSetCookie()). */
function setCookiesOf(headers: Headers): string[] {
  const h = headers as Headers & { getSetCookie?: () => string[] };
  if (typeof h.getSetCookie === 'function') return h.getSetCookie();
  const single = headers.get('set-cookie');
  return single ? [single] : [];
}

/** Stream a file through with its download headers intact. */
async function proxyDownload(
  req: Request,
  upstreamPath: string,
): Promise<Response> {
  let upstream: Response;
  try {
    upstream = await fetch(`${orchestratorUrl()}${upstreamPath}`, {
      method: 'GET',
      headers: {
        ...(req.headers.get('cookie')
          ? { cookie: req.headers.get('cookie') as string }
          : {}),
      },
      cache: 'no-store',
      redirect: 'manual',
      signal: req.signal,
    });
  } catch {
    return Response.json(
      { message: 'The orchestrator is unreachable.' },
      { status: 502 },
    );
  }

  const headers = new Headers();
  headers.set(
    'content-type',
    upstream.headers.get('content-type') ?? 'application/octet-stream',
  );
  // Keeps the browser's saved file named after the real one.
  const disposition = upstream.headers.get('content-disposition');
  if (disposition) headers.set('content-disposition', disposition);
  // Only safe because the body below is piped through byte-for-byte.
  const length = upstream.headers.get('content-length');
  if (length) headers.set('content-length', length);
  headers.set('cache-control', 'no-store');
  for (const c of setCookiesOf(upstream.headers)) {
    headers.append('set-cookie', c);
  }

  return new Response(upstream.body, { status: upstream.status, headers });
}

async function handle(req: Request, ctx: Ctx): Promise<Response> {
  const { path } = await ctx.params;
  const parts = path ?? [];
  if (parts.length === 0) {
    return Response.json({ message: 'Unknown admin endpoint.' }, { status: 404 });
  }
  // Next has percent-decoded the segments; re-encode so nothing (a report
  // filename with spaces, say) can smuggle separators upstream.
  const { search } = new URL(req.url);
  const upstreamPath = `/admin/api/${parts
    .map(encodeURIComponent)
    .join('/')}${search}`;

  if (isDownloadPath(parts, req.method)) {
    return proxyDownload(req, upstreamPath);
  }
  return proxyToOrchestrator(req, upstreamPath);
}

export async function GET(req: Request, ctx: Ctx): Promise<Response> {
  return handle(req, ctx);
}

export async function POST(req: Request, ctx: Ctx): Promise<Response> {
  return handle(req, ctx);
}

export async function PUT(req: Request, ctx: Ctx): Promise<Response> {
  return handle(req, ctx);
}

export async function DELETE(req: Request, ctx: Ctx): Promise<Response> {
  return handle(req, ctx);
}

export async function PATCH(req: Request, ctx: Ctx): Promise<Response> {
  return handle(req, ctx);
}
