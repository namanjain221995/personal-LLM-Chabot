/**
 * Client helpers for the /admin area: the ME_PAYLOAD contract types, a fetch
 * wrapper with the signed-out rule baked in, and the small derivations the
 * pages share (invitation status, coarse device names, role tables).
 *
 * Capability strings mirror the orchestrator's rbac.Cap values — visibility
 * is driven from ME_PAYLOAD.capabilities, never from role comparisons
 * scattered through components (contract §Roles).
 */

import { nav } from './nav';

export type Role = 'super_admin' | 'admin' | 'member';

export interface Me {
  user: { id: number; name: string; email: string };
  workspace: { id: string; name: string; role: Role };
  capabilities: string[];
}

export const ROLE_LABEL: Record<Role, string> = {
  super_admin: 'Super admin',
  admin: 'Admin',
  member: 'Member',
};

/** Tolerant ME_PAYLOAD parse — null when the shape is not the contract's. */
export function parseMe(body: unknown): Me | null {
  const b = body as {
    user?: { id?: unknown; name?: unknown; email?: unknown };
    workspace?: { id?: unknown; name?: unknown; role?: unknown };
    capabilities?: unknown;
  } | null;
  if (!b || typeof b !== 'object') return null;
  if (!b.user || typeof b.user.id !== 'number') return null;
  if (!b.workspace || typeof b.workspace.name !== 'string') return null;
  return {
    user: {
      id: b.user.id,
      name: typeof b.user.name === 'string' ? b.user.name : '',
      email: typeof b.user.email === 'string' ? b.user.email : '',
    },
    workspace: {
      id: String(b.workspace.id ?? ''),
      name: b.workspace.name,
      role: (b.workspace.role as Role) ?? 'member',
    },
    capabilities: Array.isArray(b.capabilities)
      ? b.capabilities.filter((c): c is string => typeof c === 'string')
      : [],
  };
}

export function can(me: Me, capability: string): boolean {
  return me.capabilities.includes(capability);
}

/**
 * Roles the signed-in user may hand out. Mirrors the orchestrator's
 * assignable_roles table; invitations additionally accept only admin|member
 * (a super admin is appointed by role change, never by invite link).
 */
export function invitableRoles(me: Me): Role[] {
  if (me.workspace.role === 'super_admin') return ['admin', 'member'];
  if (me.workspace.role === 'admin') return ['member'];
  return [];
}

export function assignableRoles(me: Me): Role[] {
  if (me.workspace.role === 'super_admin')
    return ['super_admin', 'admin', 'member'];
  return [];
}

export class AdminApiError extends Error {
  constructor(
    readonly status: number,
    detail: string,
  ) {
    super(detail);
    this.name = 'AdminApiError';
  }
}

export const OFFLINE_MESSAGE =
  'The server could not be reached. Check the connection and retry.';

/**
 * Fetch an /api/admin/* endpoint and return its JSON. Signed out (401) hard
 * redirects to /login per the contract; every other failure throws
 * AdminApiError carrying the upstream {detail} (status 0 = network failure,
 * which never redirects — usable-offline stays the rule).
 */
export async function adminJson<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`/api/admin/${path}`, { cache: 'no-store', ...init });
  } catch {
    throw new AdminApiError(0, OFFLINE_MESSAGE);
  }
  if (res.status === 401) {
    nav.assign('/login');
    throw new AdminApiError(401, 'Signed out.');
  }
  if (!res.ok) {
    let detail = 'Something went wrong. Try again.';
    try {
      const body = (await res.json()) as {
        detail?: unknown;
        message?: unknown;
      };
      if (typeof body.detail === 'string') detail = body.detail;
      else if (typeof body.message === 'string') detail = body.message;
    } catch {
      // Non-JSON error body — keep the generic sentence.
    }
    throw new AdminApiError(res.status, detail);
  }
  return (await res.json()) as T;
}

export function adminPost<T>(path: string, body: unknown): Promise<T> {
  return adminJson<T>(path, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
}

// ---------------------------------------------------------------------------
// Invitations
// ---------------------------------------------------------------------------

export type InviteStatus = 'pending' | 'accepted' | 'revoked' | 'expired';

export interface Invitation {
  id: string;
  email: string;
  name: string;
  role: string;
  invited_by: string;
  created_at: string | null;
  expires_at: string | null;
  accepted_at: string | null;
  revoked_at: string | null;
}

/** Derived — the API stores timestamps, the UI shows one word. */
export function inviteStatusOf(
  inv: Pick<Invitation, 'accepted_at' | 'revoked_at' | 'expires_at'>,
  now: number = Date.now(),
): InviteStatus {
  if (inv.accepted_at) return 'accepted';
  if (inv.revoked_at) return 'revoked';
  if (inv.expires_at && new Date(inv.expires_at).getTime() < now)
    return 'expired';
  return 'pending';
}

// ---------------------------------------------------------------------------
// Sessions
// ---------------------------------------------------------------------------

/** Coarse device name from a user-agent — "Chrome · Linux", never a parser. */
export function deviceOf(userAgent: string): string {
  const ua = userAgent ?? '';
  if (!ua.trim()) return 'Unknown device';
  const browser = /edg(e|a|ios)?\//i.test(ua)
    ? 'Edge'
    : /firefox|fxios/i.test(ua)
      ? 'Firefox'
      : /opr\/|opera/i.test(ua)
        ? 'Opera'
        : /chrome|crios/i.test(ua)
          ? 'Chrome'
          : /safari/i.test(ua)
            ? 'Safari'
            : '';
  const os = /windows/i.test(ua)
    ? 'Windows'
    : /iphone|ipad|ios/i.test(ua)
      ? 'iOS'
      : /macintosh|mac os/i.test(ua)
        ? 'macOS'
        : /android/i.test(ua)
          ? 'Android'
          : /linux|x11/i.test(ua)
            ? 'Linux'
            : '';
  if (browser && os) return `${browser} · ${os}`;
  return browser || os || 'Unknown device';
}
