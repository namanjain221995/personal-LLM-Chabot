/**
 * /api/public/shares/{token} — the ONE anonymous endpoint in this app.
 *
 * No cookie is required and none is needed: the token in the path is the
 * credential, and the orchestrator verifies it against a stored hash. The
 * proxy still forwards a session when the browser has one, because a
 * WORKSPACE-visibility link is checked against it upstream.
 *
 * The response is deliberately uncacheable and unindexable. A secret-bearing
 * URL must not sit in a shared cache, and a shared conversation must not
 * arrive in a search result.
 */

import { proxyToOrchestrator } from '@/lib/proxy';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

type Ctx = { params: Promise<{ token: string }> };

export async function GET(req: Request, ctx: Ctx): Promise<Response> {
  const { token } = await ctx.params;
  const res = await proxyToOrchestrator(
    req,
    `/public/shares/${encodeURIComponent(token)}`,
  );
  const headers = new Headers(res.headers);
  headers.set('Cache-Control', 'private, no-store');
  headers.set('X-Robots-Tag', 'noindex, nofollow, noarchive');
  headers.set('Referrer-Policy', 'no-referrer');
  return new Response(res.body, { status: res.status, headers });
}
