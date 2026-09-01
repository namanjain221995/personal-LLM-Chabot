/**
 * Client identity + session plumbing (enterprise auth retrofit).
 *
 * The orchestrator issues an opaque server session in an HttpOnly
 * `ts_session` cookie — the browser can never read it, only carry it, so
 * "am I signed in?" is always the server's answer via GET /api/auth/me.
 * This module is what the rest of the client needs around that fact:
 *
 * - `fetchMe` — the full ME_PAYLOAD (user, workspace, capabilities), with
 *   `{ok:false,status}` for failures and status 0 meaning network/offline
 *   (which must stay usable-offline, unlike a 401 which routes to /login).
 * - `userScopeKey` — the STABLE key (`u<id>`) the history cache is scoped
 *   by. The display name used to be the key; names get renamed, ids do not.
 * - `authRedirect` — the pure page-gating decision middleware.ts applies.
 * - `logout` — revoke server-side, WIPE the local caches (awaited), then
 *   hard-redirect to /login.
 */

export type FetchLike = typeof fetch;

/** The HttpOnly session cookie's name (presence-checked by middleware). */
export const SESSION_COOKIE = 'ts_session';

export type WorkspaceRole = 'super_admin' | 'admin' | 'member';

export interface MeUser {
  id: number;
  name: string;
  email: string;
}

export interface MeWorkspace {
  id: string;
  name: string;
  role: WorkspaceRole;
}

export interface MePayload {
  /** Legacy key — kept as the scoping fallback for pre-retrofit backends. */
  username: string;
  /** Null only when a legacy backend sent a bare {username}. */
  user: MeUser | null;
  workspace: MeWorkspace | null;
  /** Drives UI visibility (e.g. show Audit Log iff "audit.read"). */
  capabilities: string[];
}

export type MeResult =
  | ({ ok: true } & MePayload)
  | { ok: false; status: number };

function parseUser(raw: unknown): MeUser | null {
  const u = raw as Partial<MeUser> | null | undefined;
  if (!u || typeof u.id !== 'number' || !Number.isFinite(u.id)) return null;
  return {
    id: u.id,
    name: typeof u.name === 'string' ? u.name : '',
    email: typeof u.email === 'string' ? u.email : '',
  };
}

function parseWorkspace(raw: unknown): MeWorkspace | null {
  const w = raw as Partial<MeWorkspace> | null | undefined;
  if (!w || typeof w.id !== 'string' || !w.id) return null;
  const role =
    w.role === 'super_admin' || w.role === 'admin' || w.role === 'member'
      ? w.role
      : 'member';
  return {
    id: w.id,
    name: typeof w.name === 'string' ? w.name : '',
    role,
  };
}

/** GET /api/auth/me — status 0 means network failure (stay usable offline). */
export async function fetchMe(fetchFn: FetchLike = fetch): Promise<MeResult> {
  try {
    const res = await fetchFn('/api/auth/me', { cache: 'no-store' });
    if (!res.ok) return { ok: false, status: res.status };
    const body = (await res.json()) as Partial<MePayload> | null;
    const user = parseUser(body?.user);
    const username =
      typeof body?.username === 'string' && body.username
        ? body.username
        : (user?.email ?? '');
    // A 200 that names nobody is not an identity — treat it as a failure.
    if (!user && !username) return { ok: false, status: res.status };
    return {
      ok: true,
      username,
      user,
      workspace: parseWorkspace(body?.workspace),
      capabilities: Array.isArray(body?.capabilities)
        ? body.capabilities.filter((c): c is string => typeof c === 'string')
        : [],
    };
  } catch {
    return { ok: false, status: 0 };
  }
}

/**
 * The STABLE key the local caches are scoped by: `u<id>` from the numeric
 * user id. The username survives only as the fallback for a legacy backend
 * that sends no `user` object — an id cannot be renamed, a name can, and a
 * renamed scoping key orphans (or worse, wipes) the cache.
 */
export function userScopeKey(me: {
  user: MeUser | null;
  username: string;
}): string {
  return me.user ? `u${me.user.id}` : me.username;
}

/* ------------------------------------------------- route gating (pages) */

/** Pages reachable signed out: sign-in itself, and invitation acceptance. */
const PUBLIC_PAGES = new Set(['/login', '/accept-invite']);

/**
 * The middleware's redirect decision, pure so it is unit-testable.
 *
 * Gates PAGES only, on cookie PRESENCE only — validity is the server's job
 * (every /api/* call re-checks it; a stale cookie just means one bounce
 * through a 401). /api/* must answer with statuses rather than redirects,
 * and /_next/* plus dotted static assets have to load on /login itself.
 *
 * Returns where to redirect, or null to let the request through.
 */
export function authRedirect(
  pathname: string,
  hasSessionCookie: boolean,
): '/login' | '/' | null {
  if (pathname.startsWith('/api/') || pathname.startsWith('/_next/')) {
    return null;
  }
  const lastSegment = pathname.slice(pathname.lastIndexOf('/') + 1);
  if (lastSegment.includes('.')) return null; // favicon.ico, *.png, …
  const page =
    pathname.length > 1 && pathname.endsWith('/')
      ? pathname.slice(0, -1)
      : pathname;
  if (hasSessionCookie) {
    // Signed in (as far as a cookie can say): the sign-in page is the one
    // place that makes no sense to show.
    return page === '/login' ? '/' : null;
  }
  return PUBLIC_PAGES.has(page) ? null : '/login';
}

/* --------------------------------------------------------- 401 handling */

/**
 * Hard redirect to sign-in — `window.location.assign`, not client routing,
 * so every in-memory trace of the session (stores, streams, component
 * state) is gone before the next page runs. No-op outside a browser.
 */
export function redirectToLogin(): void {
  if (typeof window !== 'undefined') window.location.assign('/login');
}

/**
 * Sign out: revoke the session server-side (best-effort — revoking an
 * already-dead session fails harmlessly), erase this account's local data
 * AWAITED (conversation cache, sync state, prefs, thumbs — see
 * clearActiveUserData), and only then leave for /login.
 */
export async function logout(fetchFn: FetchLike = fetch): Promise<void> {
  try {
    await fetchFn('/api/auth/logout', { method: 'POST', cache: 'no-store' });
  } catch {
    // Offline logout still signs this browser out locally; the server
    // session dies on its own expiry.
  }
  // Dynamic import keeps middleware.ts (which imports this module for
  // authRedirect) free of the whole history/IndexedDB stack.
  const { clearActiveUserData } = await import('./history');
  await clearActiveUserData();
  redirectToLogin();
}
