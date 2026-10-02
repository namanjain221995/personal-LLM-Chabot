/**
 * The shared half of the chat-media proxies (2026-10-02,
 * docs/chat-media/CONTRACT.md §4 and §10). Server-only: imported by the two
 * route handlers under /api/chat-media.
 *
 * TRANSPORT AND NOTHING ELSE. The orchestrator decides everything that
 * matters — the session, who owns the conversation, whether a photo exists —
 * and answers a photo that is not yours with the same 404 as one that never
 * existed. These routes check the SHAPE of what they are asked for before a
 * request exists, forward the session cookie and the few headers the
 * contract names, and stream bytes both ways without holding them.
 *
 * Never `proxyToOrchestrator` here: it buffers the whole upstream body and
 * caps it at 32 MiB, which is right for a JSON list and wrong for a photo.
 * And never a folder called `data` anywhere under app/: the repository's
 * .gitignore has an unanchored `data/` rule that silently drops it.
 */

/** Conversation ids as the app mints them: UUIDs and conv_* slugs. */
export const SAFE_CONVERSATION = /^[A-Za-z0-9_-]{1,64}$/;
/** Attachment ids as the composer mints them (and `bf-<hex>` for backfills). */
export const SAFE_ATTACHMENT = /^[A-Za-z0-9_-]{8,64}$/;

/**
 * The largest upload body this proxy carries, 64 MiB (CONTRACT §4.1 — the
 * orchestrator's `body_cap_for` entry is the same number). Five photos at the
 * composer's 10 MiB each is 50 MiB of bytes; the rest is multipart framing
 * with room to spare. Counted as the bytes pass, never by holding them.
 */
export const MAX_MEDIA_BODY_BYTES = 64 * 1024 * 1024;

export function orchestratorUrl(): string {
  return process.env.ORCHESTRATOR_URL ?? 'http://localhost:8080';
}

/** A path segment, decoded, or null when it is not even decodable. */
export function decodeSegment(raw: string): string | null {
  try {
    return decodeURIComponent(raw);
  } catch {
    return null;
  }
}

/** The one refusal for a malformed reference: checked before any fetch. */
export function invalidReference(): Response {
  return Response.json(
    { code: 'invalid_reference' },
    { status: 400, headers: { 'cache-control': 'no-store' } },
  );
}

/**
 * CROSS-SITE WRITES ARE REFUSED HERE, for the reason the recording proxy
 * refuses them (app/api/audio/sessions/_forward.ts): this route forwards no
 * Origin, so the orchestrator's own Origin check never sees one, and the
 * SameSite=Lax session cookie does not separate sibling subdomains of the
 * app's site. A multipart POST is a "simple" request a foreign page could
 * send without a preflight. `Sec-Fetch-Site` is written by the browser and
 * cannot be forged by a page; absent (an old client, a script) is allowed,
 * exactly as there.
 */
export function crossSiteRefusal(req: Request): Response | null {
  const site = req.headers.get('sec-fetch-site')?.trim().toLowerCase() ?? null;
  if (site !== null && site !== 'same-origin' && site !== 'none') {
    return Response.json(
      { code: 'cross_site' },
      { status: 403, headers: { 'cache-control': 'no-store' } },
    );
  }
  return null;
}

/**
 * The response headers of a photo, relayed as the orchestrator set them
 * (CONTRACT §4.3). The two that make inline serving safe — nosniff and a
 * sandboxing CSP — are also set here when an upstream ever omits them: the
 * bytes are verified rasters, but this route is the last place that can
 * guarantee a browser never treats them as anything else.
 */
export const RELAYED_MEDIA_HEADERS = [
  'content-type',
  'content-length',
  'content-disposition',
  'cache-control',
  'etag',
  'x-content-type-options',
  'content-security-policy',
] as const;

export function mediaHeaders(upstream: Response): Headers {
  const out = new Headers();
  for (const name of RELAYED_MEDIA_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) out.set(name, value);
  }
  out.set('x-content-type-options', 'nosniff');
  if (!out.has('content-security-policy')) {
    out.set('content-security-policy', "default-src 'none'; sandbox");
  }
  // fetch() hands back a DECODED body; a length measured before decoding
  // would cut it short. The orchestrator does not compress (and is asked not
  // to), so this is a guard, not a path.
  if (upstream.headers.get('content-encoding')) out.delete('content-length');
  return out;
}

/** Was this thrown by the caller going away (a closed tab, a Stop)? */
export function isAbort(err: unknown, req: Request): boolean {
  return req.signal.aborted || (err as { name?: unknown } | null)?.name === 'AbortError';
}
