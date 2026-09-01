/**
 * Tiny shared bits for the auth forms.
 *
 * The forms only ever touch `ok`, `status` and `json()` on a response, so
 * tests can stub `fetch` with a plain object (the suite-wide idiom, see
 * tests/dataset-upload-feedback.test.tsx).
 */

/** What the forms need from a Response; tests satisfy it with a plain object. */
export interface JsonResponseLike {
  ok: boolean;
  status: number;
  json: () => Promise<unknown>;
}

/** Shown whenever `fetch` itself throws — the orchestrator never answered. */
export const OFFLINE_MESSAGE = 'Cannot reach the server.';

/** Orchestrator errors are `{detail}` (contract §errors). Null when absent. */
export async function readDetail(res: JsonResponseLike): Promise<string | null> {
  try {
    const body = (await res.json()) as { detail?: unknown };
    return typeof body.detail === 'string' && body.detail.length > 0
      ? body.detail
      : null;
  } catch {
    return null;
  }
}
