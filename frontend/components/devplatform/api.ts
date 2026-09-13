/**
 * The console's fetch helper.
 *
 * It is `adminJson` with one letter changed — the prefix — and everything
 * that matters is IMPORTED rather than retyped: the error type, the offline
 * sentence, and above all `handleSessionEnd`, which is the single place in
 * this codebase that decides what a 401 means (a removed account gets its own
 * page, everything else goes to sign-in). Duplicating that rule is how two
 * surfaces end up disagreeing about what being signed out looks like.
 *
 * WHY NOT JUST CALL adminJson (2026-09-13). `adminJson` hard-codes the
 * `/api/admin/` prefix, and the console's own BFF exists for a reason the
 * admin proxy cannot serve: the playground STREAMS, and proxyToOrchestrator
 * buffers a whole response into an ArrayBuffer before answering. A streamed
 * answer that only arrives when it is finished is not a stream. So the console
 * has its own route handler, and this helper points at it.
 *
 * Status 0 means the network failed and never redirects — a console that signs
 * you out because the office wifi blinked is worse than one that says so.
 */

import { handleSessionEnd } from '@/lib/auth';
import { nav } from '@/components/admin/nav';
import { AdminApiError, OFFLINE_MESSAGE } from '@/components/admin/api';

/** Every console call goes through this one path; the BFF allowlists the rest. */
export const CONSOLE_BASE = '/api/devplatform/';

export async function consoleJson<T>(
  path: string,
  init?: RequestInit,
): Promise<T> {
  let res: Response;
  try {
    res = await fetch(`${CONSOLE_BASE}${path}`, { cache: 'no-store', ...init });
  } catch {
    throw new AdminApiError(0, OFFLINE_MESSAGE);
  }
  if (res.status === 401) {
    void handleSessionEnd(undefined, fetch, nav);
    throw new AdminApiError(401, 'Signed out.');
  }
  if (!res.ok) {
    let detail = 'Something went wrong. Try again.';
    try {
      detail = errorSentence(await res.json()) ?? detail;
    } catch {
      // Non-JSON error body — keep the generic sentence.
    }
    throw new AdminApiError(res.status, detail);
  }
  // 204 No Content is a legitimate answer to a revoke or a delete.
  if (res.status === 204) return undefined as T;
  return (await res.json()) as T;
}

export function consolePost<T>(path: string, body: unknown): Promise<T> {
  return consoleJson<T>(path, {
    method: 'POST',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export function consolePut<T>(path: string, body: unknown): Promise<T> {
  return consoleJson<T>(path, {
    method: 'PUT',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export function consolePatch<T>(path: string, body: unknown): Promise<T> {
  return consoleJson<T>(path, {
    method: 'PATCH',
    headers: { 'content-type': 'application/json' },
    body: JSON.stringify(body),
  });
}

export function consoleDelete<T>(path: string): Promise<T> {
  return consoleJson<T>(path, { method: 'DELETE' });
}

/**
 * The human sentence in an error body, whichever of the three shapes the
 * console can receive: FastAPI's `{detail}` from a console route (a string,
 * or a list of validation errors for a 422), the BFF's own `{message}`, and
 * the CONTRACT §9 envelope `{error: {message}}` the playground shares with
 * /v1. Null when none of them carries a string.
 */
export function errorSentence(body: unknown): string | null {
  if (!body || typeof body !== 'object') return null;
  const b = body as { detail?: unknown; message?: unknown; error?: unknown };
  if (typeof b.detail === 'string') return b.detail;
  if (Array.isArray(b.detail)) {
    const first = b.detail[0] as { msg?: unknown } | undefined;
    if (first && typeof first.msg === 'string') return first.msg;
  }
  if (typeof b.message === 'string') return b.message;
  const envelope = b.error as { message?: unknown } | undefined;
  if (envelope && typeof envelope.message === 'string') return envelope.message;
  return null;
}

/**
 * Turn any thrown value into a sentence a person can act on.
 *
 * Every mutation in the console pairs its failure with a toast, and a toast
 * whose text is "[object Object]" is worse than no toast: it tells the reader
 * the product broke without telling them what to do.
 */
export function messageOf(err: unknown, fallback: string): string {
  return err instanceof AdminApiError && err.message ? err.message : fallback;
}

export { AdminApiError, OFFLINE_MESSAGE };
