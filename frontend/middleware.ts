/**
 * Edge page gating (enterprise auth retrofit).
 *
 * Decides ONE thing, from cookie PRESENCE alone: does this page request get
 * through, bounce to /login (signed out), or bounce home (signed in but on
 * /login)? Validity is the server's job — every /api/* proxy forwards the
 * cookie and the orchestrator answers 401 when it is stale, so the worst a
 * forged/expired cookie buys here is one page load that immediately 401s.
 * The decision itself lives in lib/auth.ts (authRedirect) so it is
 * unit-testable without Next.
 *
 * Note: Next 16 renamed this convention to proxy.ts; middleware.ts remains
 * supported (deprecated) with identical behavior — see
 * node_modules/next/dist/docs/01-app/03-api-reference/03-file-conventions/proxy.md.
 */

import { NextResponse, type NextRequest } from 'next/server';

import { authRedirect, SESSION_COOKIE } from '@/lib/auth';

export function middleware(req: NextRequest): NextResponse {
  const target = authRedirect(
    req.nextUrl.pathname,
    req.cookies.has(SESSION_COOKIE),
  );
  if (target) return NextResponse.redirect(new URL(target, req.url));
  return NextResponse.next();
}

export const config = {
  // Pages only: /api/* answers statuses (never redirects — a fetch cannot
  // follow one to a login PAGE), and /_next/* plus dotted static assets must
  // load on /login itself. authRedirect re-checks the same exclusions, so
  // widening this matcher cannot silently widen the gate.
  matcher: ['/((?!api|_next|.*\\..*).*)'],
};
