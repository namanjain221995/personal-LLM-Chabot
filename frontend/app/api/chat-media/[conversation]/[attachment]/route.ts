/**
 * GET /api/chat-media/[conversation]/[attachment]?size=thumb|full — one stored
 * photo's bytes (2026-10-02, docs/chat-media/CONTRACT.md §4.3 and §10).
 *
 * This is what a chat bubble's `<img>` points at on a device that never held
 * the photo, so it is built to be FAST the second time as much as the first:
 *
 *   · the bytes stream straight through — never read into this process;
 *   · `Cache-Control: private, max-age=31536000, immutable` and the ETag are
 *     relayed as the orchestrator set them. An attachment id never changes
 *     content (first write wins), so the browser may keep a photo for a
 *     year and never ask again; `private` keeps it out of any shared cache;
 *   · `If-None-Match` is forwarded and a 304 relayed with no body, for the
 *     revalidation a forced reload still makes.
 *
 * 404 (no such photo, or not yours — the orchestrator does not say which)
 * and 410 (the row exists, its file does not) are relayed as themselves and
 * never cached, so a photo stored a moment later is found on the next try.
 * Both path segments and `size` are validated before any request exists: a
 * malformed id must never become a request, let alone an upstream path.
 */

import {
  decodeSegment,
  invalidReference,
  isAbort,
  mediaHeaders,
  orchestratorUrl,
  SAFE_ATTACHMENT,
  SAFE_CONVERSATION,
} from '../../_media';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const SIZES = new Set(['thumb', 'full']);

export async function GET(
  req: Request,
  ctx: { params: Promise<{ conversation: string; attachment: string }> },
): Promise<Response> {
  const { conversation, attachment } = await ctx.params;
  const conv = decodeSegment(conversation);
  const att = decodeSegment(attachment);
  const size = new URL(req.url).searchParams.get('size') ?? 'full';
  if (
    conv === null ||
    att === null ||
    !SAFE_CONVERSATION.test(conv) ||
    !SAFE_ATTACHMENT.test(att) ||
    !SIZES.has(size)
  ) {
    return invalidReference();
  }
  if (process.env.MOCK_MODE === 'true') {
    return Response.json({ code: 'not_found' }, { status: 404, headers: { 'cache-control': 'no-store' } });
  }

  const headers: Record<string, string> = {
    // Photos are already compressed; asking for identity keeps the length
    // the orchestrator sends the length the browser receives.
    'accept-encoding': 'identity',
  };
  const cookie = req.headers.get('cookie');
  if (cookie) headers.cookie = cookie;
  const ifNoneMatch = req.headers.get('if-none-match');
  if (ifNoneMatch) headers['if-none-match'] = ifNoneMatch;

  let upstream: Response;
  try {
    upstream = await fetch(
      `${orchestratorUrl()}/chat-media/${encodeURIComponent(conv)}/${encodeURIComponent(att)}?size=${size}`,
      { headers, signal: req.signal, cache: 'no-store' },
    );
  } catch (err) {
    if (isAbort(err, req)) return new Response(null, { status: 499 });
    return Response.json(
      { code: 'unreachable' },
      { status: 502, headers: { 'cache-control': 'no-store' } },
    );
  }

  if (upstream.status === 304) {
    // No body, by definition — constructing a Response with one throws.
    const out = new Headers();
    for (const name of ['etag', 'cache-control'] as const) {
      const value = upstream.headers.get(name);
      if (value) out.set(name, value);
    }
    void upstream.body?.cancel().catch(() => undefined);
    return new Response(null, { status: 304, headers: out });
  }
  if (upstream.status === 200) {
    return new Response(upstream.body, { status: 200, headers: mediaHeaders(upstream) });
  }
  // Every refusal is relayed by status with a body of our own words: 404 and
  // 410 are what a bubble turns into "Image unavailable", 401 is a session
  // that ended. Nothing the orchestrator wrote is passed on.
  void upstream.body?.cancel().catch(() => undefined);
  const code =
    upstream.status === 404
      ? 'not_found'
      : upstream.status === 410
        ? 'media_missing'
        : 'unavailable';
  return Response.json(
    { code },
    { status: upstream.status, headers: { 'cache-control': 'no-store' } },
  );
}
