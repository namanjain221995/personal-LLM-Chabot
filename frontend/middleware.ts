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
  // Pages only. Three namespaces are excluded and nothing else:
  //
  //   /api/    route handlers, which answer with statuses (a fetch cannot
  //            follow a redirect to a login PAGE);
  //   /_next/  the build output, which together with dotted static assets has
  //            to load on /login itself;
  //   v1       the public developer API (CONTRACT §1), which reads exactly one
  //            credential — the Authorization header — and must never be
  //            redirected, cookie-gated, or told anything about a session.
  //
  // THE `v1(?:/|$)` IS ALSO THE FIX (2026-09-13). Spelled `v1/`, the exclusion
  // covered every path UNDER the namespace and not the namespace itself, so a
  // request for the bare `/v1` — which app/v1/[[...path]]/route.ts serves,
  // because an OPTIONAL catch-all matches its own root — fell through to the
  // page gate and was redirected to /login when no session cookie was present
  // and let through when one was. That is worse than the redirect: it made the
  // one surface the contract calls cookie-blind behave DIFFERENTLY depending on
  // whether a cookie was there, which is the session-awareness §1 forbids. It
  // is the mirror image of the `/api` bug the slashes fixed, found by the
  // wave-1 verifier in the same file.
  //
  // THE SLASHES ARE THE FIX (2026-09-12). This used to read `(?!api|_next|…)`,
  // which excludes every path whose first characters are "api" — the developer
  // console page at /api itself included. That page is capability-gated
  // (CONTRACT §6) and would have shipped with no edge gate whatsoever, for the
  // sake of a prefix only ever meant to name the route-handler namespace.
  //
  // authRedirect re-checks the same exclusions, so widening this matcher
  // cannot silently widen the gate.
  matcher: ['/((?!api/|_next/|v1(?:/|$)|.*\\..*).*)'],
};
