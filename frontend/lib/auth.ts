/**
 * Local identity — what is left after login was removed.
 *
 * There is no sign-in, no sign-up, no session cookie and no route gating: this
 * app runs as a single local user. All that survives is "who am I running
 * as?", which the UI shows and — more importantly — the history store uses to
 * scope its localStorage cache. That name must stay stable, or cached
 * conversations end up orphaned under a key nothing reads again.
 */

export type FetchLike = typeof fetch;

export type MeResult =
  | { ok: true; username: string }
  | { ok: false; status: number };

/** GET /api/auth/me — status 0 means network failure (stay usable offline). */
export async function fetchMe(fetchFn: FetchLike = fetch): Promise<MeResult> {
  try {
    const res = await fetchFn('/api/auth/me', { cache: 'no-store' });
    if (!res.ok) return { ok: false, status: res.status };
    const body = (await res.json()) as { username?: unknown };
    return typeof body.username === 'string'
      ? { ok: true, username: body.username }
      : { ok: false, status: res.status };
  } catch {
    return { ok: false, status: 0 };
  }
}
