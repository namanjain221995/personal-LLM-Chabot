/**
 * The middleware's page-gating decision (lib/auth.ts authRedirect), pure so
 * it can be pinned down without Next: no cookie → pages bounce to /login
 * (except the public ones), cookie → /login bounces home, and everything
 * that is not a page — /api/*, /_next/*, dotted static assets — is left
 * alone in BOTH states. Presence only: validity is the server's job.
 */
import { describe, expect, it } from 'vitest';

import { authRedirect, SESSION_COOKIE } from '../lib/auth';

describe('signed out (no ts_session cookie)', () => {
  it.each(['/', '/admin', '/admin/members', '/settings'])(
    'bounces the page %s to /login',
    (path) => {
      expect(authRedirect(path, false)).toBe('/login');
    },
  );

  it.each(['/login', '/accept-invite', '/access-removed'])(
    'lets the public page %s through',
    (path) => {
      expect(authRedirect(path, false)).toBeNull();
    },
  );

  it('treats a trailing slash as the same page', () => {
    expect(authRedirect('/login/', false)).toBeNull();
    expect(authRedirect('/admin/', false)).toBe('/login');
  });

  it.each([
    '/api/auth/login',
    '/api/chat',
    '/_next/static/chunks/main.js',
    '/favicon.ico',
    '/logo.png',
  ])('never redirects the non-page %s', (path) => {
    expect(authRedirect(path, false)).toBeNull();
  });
});

describe('signed in (cookie present)', () => {
  it('bounces /login home', () => {
    expect(authRedirect('/login', true)).toBe('/');
    expect(authRedirect('/login/', true)).toBe('/');
  });

  it.each(['/', '/admin', '/accept-invite'])(
    'lets %s through',
    (path) => {
      expect(authRedirect(path, true)).toBeNull();
    },
  );

  it.each(['/api/auth/logout', '/_next/image', '/favicon.ico'])(
    'leaves the non-page %s alone',
    (path) => {
      expect(authRedirect(path, true)).toBeNull();
    },
  );
});

describe('contract details', () => {
  it('gates on the ts_session cookie by name', () => {
    // middleware.ts checks req.cookies.has(SESSION_COOKIE); the name is the
    // orchestrator's contract and must not drift.
    expect(SESSION_COOKIE).toBe('ts_session');
  });

  it('does not treat a dotted DIRECTORY segment as a page', () => {
    // Only the last segment decides asset-ness: /v1.2/report is a page.
    expect(authRedirect('/v1.2/report', false)).toBe('/login');
  });
});

// ---------------------------------------------------------------------------
// A shared conversation is the one signed-out page with a variable path
//
// PUBLIC_PAGES is an exact-match Set and cannot express `/share/<token>`, so
// this is the only prefix rule in the gate. It is also the only place a
// signed-out request is let through, which is why the depth guard matters as
// much as the prefix.
// ---------------------------------------------------------------------------

describe('the public share page', () => {
  it('lets a signed-out visitor through', () => {
    expect(authRedirect('/share/abc123.def456', false)).toBeNull();
    expect(authRedirect('/share/abc123.def456/', false)).toBeNull();
  });

  it('lets a signed-in visitor through too', () => {
    expect(authRedirect('/share/abc123.def456', true)).toBeNull();
  });

  it('does NOT make anything deeper public', () => {
    // `startsWith('/share/')` alone would open every one of these.
    expect(authRedirect('/share/abc/admin', false)).toBe('/login');
    expect(authRedirect('/share/abc/def/ghi', false)).toBe('/login');
  });

  it('does not make the bare prefix public', () => {
    expect(authRedirect('/share', false)).toBe('/login');
    expect(authRedirect('/share/', false)).toBe('/login');
  });

  it('leaves every other page exactly as it was', () => {
    expect(authRedirect('/', false)).toBe('/login');
    expect(authRedirect('/admin', false)).toBe('/login');
    expect(authRedirect('/admin/members', false)).toBe('/login');
    expect(authRedirect('/sharealike', false)).toBe('/login');
    expect(authRedirect('/notshare/abc', false)).toBe('/login');
  });
});
