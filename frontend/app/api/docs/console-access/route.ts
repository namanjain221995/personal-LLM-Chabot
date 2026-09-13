/**
 * GET /api/docs/console-access — may this browser's session open the
 * developer console? `{ allowed: boolean }`, always 200.
 *
 * The public docs link to the console, and the console answers anyone
 * without `api.console.access` with a 404 (CONTRACT §6). The docs header asks
 * here after it mounts and draws the Console link only on `true`.
 *
 * WHY A ROUTE AND NOT THE DOCS LAYOUT. next.config.mjs serves every /docs
 * response `Cache-Control: public, max-age=300`, so the docs HTML must be the
 * same for every reader: a link rendered into it for an admin could be stored
 * by a shared cache and served to members. This answer is per session, so it
 * travels on its own URL, marked `private, no-store`.
 *
 * WHY 200 FOR "NO". The docs are public; most readers are signed out. A 401
 * here (as /api/auth/me gives) would print a red "Failed to load resource" in
 * the console of every signed-out docs page view. "No" is an answer, not an
 * error. It discloses nothing /api does not: the question is only ever about
 * the caller's own session, and the answer is resolved by the console's own
 * gate (components/devplatform/server.ts) so it cannot drift from it.
 */

import { consoleLinkAllowed } from '@/components/docs/consoleAccess';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

export async function GET(): Promise<Response> {
  const allowed = await consoleLinkAllowed();
  return Response.json(
    { allowed },
    { headers: { 'cache-control': 'private, no-store' } },
  );
}
