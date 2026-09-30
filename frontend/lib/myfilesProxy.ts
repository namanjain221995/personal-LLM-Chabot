/**
 * The shared half of the two "My files" proxies (/api/files/mine and
 * /api/files/mine/summary). Server-only: imported by route handlers.
 *
 * AN ALLOWLIST, NOT A PASSTHROUGH. Only the parameters the orchestrator
 * reads go upstream, first value only and bounded in length, so nothing a
 * browser appends (a `user_id`, a second `kind`) reaches it — identity is the
 * session cookie and nothing else (orchestrator/app/myfiles.py).
 *
 * JSON ONLY. proxyToOrchestrator buffers the whole upstream body, which is
 * right for a list and wrong for a file: file bytes stream through
 * /api/uploads/.../file and /api/audio/sessions/.../audio, never through here.
 */

import { handleMockFiles } from './mockApi';
import { proxyToOrchestrator } from './proxy';

export const LIST_PARAMS = [
  'q',
  'kind',
  'since',
  'until',
  'min_bytes',
  'max_bytes',
  'sort',
  'limit',
  'cursor',
] as const;

/** The summary counts per kind across every page: no kind, sort or paging. */
export const SUMMARY_PARAMS = ['q', 'since', 'until', 'min_bytes', 'max_bytes'] as const;

/**
 * The orchestrator's own cursor bound (myfiles._CURSOR_MAX_CHARS). A cursor is
 * ~100 characters under the time sorts, but a name-sort cursor carries the
 * last row's name, and dropping it here would silently hand back page one.
 */
export const MAX_VALUE_CHARS = 16_384;

export function forwardedQuery(params: URLSearchParams, allowed: readonly string[]): string {
  const out = new URLSearchParams();
  for (const name of allowed) {
    const value = params.get(name);
    if (value !== null && value.length <= MAX_VALUE_CHARS) out.set(name, value);
  }
  const query = out.toString();
  return query ? `?${query}` : '';
}

export function filesProxy(req: Request, view: 'list' | 'summary'): Promise<Response> {
  if (process.env.MOCK_MODE === 'true') return Promise.resolve(handleMockFiles(req, view));
  const params = new URL(req.url).searchParams;
  const path = view === 'list' ? '/files/mine' : '/files/mine/summary';
  return proxyToOrchestrator(req, `${path}${forwardedQuery(params, view === 'list' ? LIST_PARAMS : SUMMARY_PARAMS)}`);
}

/** Every method but GET: the resource does not exist for it. */
export function notFound(): Response {
  return Response.json({ message: 'Unknown files endpoint.' }, { status: 404, headers: { 'cache-control': 'no-store' } });
}
