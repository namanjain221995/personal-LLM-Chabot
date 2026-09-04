/**
 * The sharing client: types the server actually sends, and the calls that
 * fetch them.
 *
 * ONE RULE RUNS THROUGH THIS FILE. Nothing here decides whether a
 * conversation may be shared — the server does, every time, and this only
 * renders the answer. The Share button being hidden is a courtesy to the
 * person looking at it, never a control.
 *
 * The token is returned EXACTLY ONCE, by `createShare`, and is not
 * recoverable afterwards: the server stores only a hash of it. A caller that
 * loses it must revoke and create again, which is the same trade the session
 * cookie makes and for the same reason.
 */

export type ShareVisibility = 'public' | 'workspace';

export interface ShareState {
  id: number;
  visibility: ShareVisibility;
  status: 'active' | 'revoked';
  /** Addressable half only — opening it needs the token from `createShare`. */
  url: string;
  created_at: string;
  expires_at: string | null;
  show_owner_name: boolean;
  version: number | null;
  message_count: number;
  last_message_id: number | null;
  view_count: number;
  last_viewed_at: string | null;
}

export interface SharePolicy {
  public_allowed: boolean;
  workspace_allowed: boolean;
  /** Plain sentences for the person, never internals or a matched secret. */
  blocking_reasons: string[];
  warnings: string[];
  shareable_messages: number;
}

export interface ShareStatus {
  enabled: boolean;
  share: ShareState | null;
  policy: SharePolicy;
  /** Completed messages sent since the snapshot the link shows. */
  unshared_messages: number;
  expiry_choices: string[];
  default_expiry: string;
}

export interface CreatedShare {
  share: ShareState;
  /** The full link, with the secret. Shown once; never fetched again. */
  url: string | null;
  token: string | null;
  truncated: boolean;
}

/** One message as the public page receives it — the whole public contract. */
export interface SharedMessage {
  role: 'user' | 'assistant';
  content: string;
  route?: string;
  sources?: { n: number; title: string; url: string; domain: string }[];
}

export interface SharedSnapshot {
  schema: number;
  title: string;
  messages: SharedMessage[];
  shared_at: string;
  truncated: boolean;
  owner_name?: string;
}

export interface PublicShare {
  snapshot: SharedSnapshot;
  visibility: ShareVisibility;
  shared_at: string;
  expires_at: string | null;
}

export class ShareError extends Error {
  constructor(
    readonly status: number,
    message: string,
  ) {
    super(message);
    this.name = 'ShareError';
  }
}

async function call<T>(path: string, init?: RequestInit): Promise<T> {
  let res: Response;
  try {
    res = await fetch(path, { cache: 'no-store', ...init });
  } catch {
    throw new ShareError(0, 'The server could not be reached.');
  }
  if (!res.ok) {
    let detail = 'Something went wrong. Try again.';
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (typeof body.detail === 'string') detail = body.detail;
    } catch {
      /* non-JSON error body — keep the generic sentence */
    }
    throw new ShareError(res.status, detail);
  }
  return (await res.json()) as T;
}

const base = (conversationId: string) =>
  `/api/conversations/${encodeURIComponent(conversationId)}/share`;

export function getShareStatus(conversationId: string): Promise<ShareStatus> {
  return call<ShareStatus>(base(conversationId));
}

export function createShare(
  conversationId: string,
  body: {
    visibility: ShareVisibility;
    expiry: string;
    show_owner_name: boolean;
  },
): Promise<CreatedShare> {
  return call<CreatedShare>(base(conversationId), {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
}

/** Publish the conversation as it stands now, keeping the same link. */
export function refreshShare(conversationId: string): Promise<{ share: ShareState }> {
  return call<{ share: ShareState }>(`${base(conversationId)}?refresh=1`, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: '{}',
  });
}

export function updateShare(
  conversationId: string,
  body: { visibility?: ShareVisibility; expiry?: string; show_owner_name?: boolean },
): Promise<{ share: ShareState }> {
  return call<{ share: ShareState }>(base(conversationId), {
    method: 'PATCH',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export function revokeShare(conversationId: string): Promise<{ revoked: boolean }> {
  return call<{ revoked: boolean }>(base(conversationId), { method: 'DELETE' });
}

export function getPublicShare(token: string): Promise<PublicShare> {
  return call<PublicShare>(`/api/public/shares/${encodeURIComponent(token)}`);
}

/**
 * Copy to the clipboard, reporting whether it worked.
 *
 * `navigator.clipboard` is unavailable on an insecure origin and can be
 * refused by permission policy, and a share link is precisely the thing
 * someone needs to get out of the app — so the caller keeps the URL
 * selectable and says so when this returns false, rather than pretending.
 */
export async function copyToClipboard(text: string): Promise<boolean> {
  try {
    if (typeof navigator !== 'undefined' && navigator.clipboard) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* fall through to the manual path */
  }
  return false;
}

/** "expires in 6 days", or null when it does not. */
export function expiryLabel(expiresAt: string | null): string | null {
  if (!expiresAt) return null;
  const ms = new Date(expiresAt).getTime() - Date.now();
  if (Number.isNaN(ms)) return null;
  if (ms <= 0) return 'expired';
  const days = Math.floor(ms / 86_400_000);
  if (days >= 1) return `expires in ${days} day${days === 1 ? '' : 's'}`;
  const hours = Math.max(1, Math.floor(ms / 3_600_000));
  return `expires in ${hours} hour${hours === 1 ? '' : 's'}`;
}

/**
 * Which expiry option best describes a link that dies at `expiresAt`.
 *
 * The server stores the DATE, not the choice that produced it, so this is a
 * best fit: the smallest offered option that still covers the time left. A
 * 90-day link two months old therefore reads as "30 days", which is why the
 * dialog prints the real date directly beneath the control rather than
 * letting this stand alone.
 *
 * NULL is the exception and is not a best fit at all: it means the link has
 * no deadline, which is exactly "never" and nothing else.
 */
export function deriveExpiryChoice(
  expiresAt: string | null,
  choices: string[],
): string {
  // A link with no expiry reads as "never" WHATEVER the menu currently
  // offers. If the workspace has since withdrawn the option, the honest
  // answer is still "never" and the caller shows it as an unselectable
  // current state — the alternative was a control that displayed "24 hours"
  // for a link that in fact never expires.
  if (!expiresAt) return 'never';
  const hoursLeft = (new Date(expiresAt).getTime() - Date.now()) / 3_600_000;
  if (Number.isNaN(hoursLeft)) return choices[0] ?? '30d';
  const HOURS: Record<string, number> = { '24h': 24, '7d': 168, '30d': 720, '90d': 2160 };
  const fits = choices.filter((c) => c !== 'never' && (HOURS[c] ?? 0) >= hoursLeft);
  return fits[0] ?? choices.filter((c) => c !== 'never').pop() ?? '30d';
}

/** The human name for an expiry option the server offered. */
export const EXPIRY_LABEL: Record<string, string> = {
  '24h': '24 hours',
  '7d': '7 days',
  '30d': '30 days',
  '90d': '90 days',
  never: 'No expiry',
};
