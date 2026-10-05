/**
 * POST /api/auth/logout — revoke the session and clear the cookie.
 *
 * Through proxyToOrchestrator so the clearing Set-Cookie reaches the
 * browser. Safe to call signed out (the orchestrator answers {ok:true}).
 *
 * 2026-10-02 (chat media): every answer also carries
 * `Clear-Site-Data: "cache"`. Stored chat photos are served `private,
 * max-age=31536000, immutable`, so a browser keeps every photo it showed in
 * its HTTP cache for a year and serves it again WITHOUT asking the server —
 * no session check runs. Logout already erases this account's IndexedDB
 * because this may be a shared machine; without this header the next person
 * at the keyboard could still open the last account's photos from history.
 * Sent on every outcome (the orchestrator unreachable included): the browser
 * signs out locally either way. Only the HTTP cache: cookies are the
 * orchestrator's to clear, and local storage is wiped per account by
 * `clearActiveUserData`.
 */

import { handleMockAuth } from '@/lib/mockApi';
import { proxyToOrchestrator } from '@/lib/proxy';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function POST(req: Request): Promise<Response> {
  const res =
    process.env.MOCK_MODE === 'true'
      ? await handleMockAuth(req, ['logout'])
      : await proxyToOrchestrator(req, '/auth/logout');
  const headers = new Headers(res.headers);
  headers.set('clear-site-data', '"cache"');
  return new Response(res.body, { status: res.status, statusText: res.statusText, headers });
}
