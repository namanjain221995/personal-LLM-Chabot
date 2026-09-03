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
  /**
   * Which TOOLS this account may use (orchestrator authn/features.py):
   * `{web_search: true, salesforce: false, ...}`. The composer hides what is
   * off. An older orchestrator sends nothing and this stays empty, which
   * `featureAllowed` reads as "allowed" — a half-deployed pair must never
   * hide a tool the server still honours.
   */
  features: Record<string, boolean>;
}

/** One tool's state, defaulting to allowed when the server did not say. */
export function featureAllowed(
  features: Record<string, boolean> | undefined,
  id: string,
): boolean {
  const value = features?.[id];
  return typeof value === 'boolean' ? value : true;
}

/**
 * Why a session ended, as /auth/me reports it on a 401 for a browser whose
 * cookie once opened a real session (2026-09-03). The server only explains
 * when the cookie's secret still matches — proof the browser held that
 * session — so none of this is available to the login form, which stays
 * deliberately generic.
 */
export type SessionEndCode =
  | 'account_removed'
  | 'account_disabled'
  | 'session_revoked'
  | 'session_expired'
  | 'signed_out';

export interface SessionEndContact {
  email: string;
  name: string;
}

export interface MeFailure {
  ok: false;
  status: number;
  code?: SessionEndCode;
  /** The workspace the account was removed from / deactivated in. */
  workspace?: string;
  /** Who can restore access — active admins, super admins first. */
  contact?: SessionEndContact[];
  /** ISO timestamp of the revocation, when the server knows it. */
  endedAt?: string;
}

export type MeResult = ({ ok: true } & MePayload) | MeFailure;

const SESSION_END_CODES: ReadonlySet<string> = new Set([
  'account_removed',
  'account_disabled',
  'session_revoked',
  'session_expired',
  'signed_out',
]);

/** The two codes that mean "this account cannot sign in again by itself". */
export function isAccessEnded(code: SessionEndCode | undefined): boolean {
  return code === 'account_removed' || code === 'account_disabled';
}

function parseFailure(status: number, body: unknown): MeFailure {
  // FastAPI wraps a structured 401 as {detail: {...}}; a plain one is a string.
  const detail = (body as { detail?: unknown } | null)?.detail;
  const info = detail && typeof detail === 'object' ? (detail as Record<string, unknown>) : null;
  const out: MeFailure = { ok: false, status };
  if (!info) return out;
  if (typeof info.code === 'string' && SESSION_END_CODES.has(info.code)) {
    out.code = info.code as SessionEndCode;
  }
  if (typeof info.workspace === 'string' && info.workspace) out.workspace = info.workspace;
  if (typeof info.ended_at === 'string' && info.ended_at) out.endedAt = info.ended_at;
  if (Array.isArray(info.contact)) {
    out.contact = info.contact
      .map((c) => {
        const r = c as Partial<SessionEndContact> | null;
        return r && typeof r.email === 'string' && r.email
          ? { email: r.email, name: typeof r.name === 'string' ? r.name : '' }
          : null;
      })
      .filter((c): c is SessionEndContact => c !== null);
  }
  return out;
}

function parseFeatures(raw: unknown): Record<string, boolean> {
  if (!raw || typeof raw !== 'object') return {};
  const out: Record<string, boolean> = {};
  for (const [key, value] of Object.entries(raw as Record<string, unknown>)) {
    if (typeof value === 'boolean') out[key] = value;
  }
  return out;
}

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
    if (!res.ok) {
      let body: unknown = null;
      try {
        body = await res.json();
      } catch {
        body = null;
      }
      return parseFailure(res.status, body);
    }
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
      features: parseFeatures(body?.features),
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

/** Pages reachable signed out: sign-in, invitation acceptance, and the
    page that explains a removed or deactivated account (2026-09-03). */
const PUBLIC_PAGES = new Set(['/login', '/accept-invite', '/access-removed']);

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

/** A minimal navigator, injectable so the routing rule is testable. */
export interface Navigator {
  assign: (url: string) => void;
}

const browserNavigator: Navigator = {
  assign: (url) => {
    if (typeof window !== 'undefined') window.location.assign(url);
  },
};

/**
 * The page a dead session should land on (2026-09-03), pure so it is
 * unit-testable: a removed or deactivated account goes to /access-removed
 * with what that page needs in the query string (nothing secret — the
 * workspace name and the admins' contact emails); everything else goes to
 * sign-in exactly as before.
 */
export function sessionEndRoute(me: MeFailure): string {
  if (!isAccessEnded(me.code)) return '/login';
  const params = new URLSearchParams();
  params.set('code', me.code as string);
  if (me.workspace) params.set('ws', me.workspace);
  if (me.endedAt) params.set('at', me.endedAt);
  for (const c of me.contact ?? []) {
    params.append('contact', c.name ? `${c.name} <${c.email}>` : c.email);
  }
  return `/access-removed?${params.toString()}`;
}

/**
 * Route a dead session to the right page. A removed/deactivated account
 * gets its local data ERASED first (the person no longer has access, and
 * this may be a shared machine) — the same wipe logout performs — then the
 * explanation page; any other end goes straight to sign-in.
 *
 * `me` may be passed when the caller already probed /auth/me (the boot
 * path); otherwise it is fetched here (a 401 mid-session).
 */
export async function handleSessionEnd(
  me?: MeFailure,
  fetchFn: FetchLike = fetch,
  nav: Navigator = browserNavigator,
): Promise<void> {
  let failure = me;
  if (!failure) {
    const probed = await fetchMe(fetchFn);
    failure = probed.ok ? { ok: false, status: 401 } : probed;
  }
  if (!isAccessEnded(failure.code)) {
    nav.assign('/login');
    return;
  }
  try {
    const { clearActiveUserData } = await import('./history');
    await clearActiveUserData();
  } catch {
    // The wipe is a courtesy to the next person at this keyboard; the
    // explanation page must show either way.
  }
  nav.assign(sessionEndRoute(failure));
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
