/**
 * The server half of the console gate: read the cookie, ask once, remember the
 * answer for the rest of THIS request.
 *
 * `cache()` is React's per-request memo, and it is what lets both the layout
 * and the page resolve the session without asking the orchestrator twice for
 * one page load. It does NOT cache across requests — capabilities are
 * recomputed upstream per request (CONTRACT §6), and a gate that remembered
 * "allowed" from a minute ago would be the one place a demoted admin's console
 * stayed open.
 *
 * Only the session cookie is forwarded, not the whole Cookie header. The
 * orchestrator's /auth/me reads exactly one cookie, and everything else the
 * browser happens to be carrying — analytics, preferences, another app's
 * crumbs — has no business crossing this hop.
 *
 * Kept apart from session.ts so the decision itself stays importable without
 * `next/headers`, which only exists inside a request — and so this module,
 * which can only run on a server, is never pulled into a client bundle by an
 * import somebody added to the shared file. (No `server-only` marker: that
 * package is not a dependency of this app, and adding one for a single import
 * is not worth the lockfile churn; the next/headers import has the same
 * effect, since it does not resolve in a client component.)
 */

import { cache } from 'react';
import { cookies } from 'next/headers';
import { SESSION_COOKIE } from '@/lib/auth';
import { resolveConsoleSession, type ConsoleSession } from './session';

export const consoleSession = cache(async (): Promise<ConsoleSession> => {
  const jar = await cookies();
  const token = jar.get(SESSION_COOKIE)?.value;
  if (!token) return { state: 'signed-out' };
  return resolveConsoleSession(`${SESSION_COOKIE}=${token}`);
});
