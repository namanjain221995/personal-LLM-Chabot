/**
 * /api/chat-media/[conversation] — a conversation's stored photos
 * (2026-10-02, docs/chat-media/CONTRACT.md §4.1-4.2).
 *
 *   GET  — the viewer's photos in this chat: `{items:[{attachment_id,
 *          media_id, mime, width, height, bytes, created_at}]}`.
 *   POST — store a batch of photos (multipart: `file` + `attachment_id`
 *          parts in the same order, optional `source`; the browser cuts a
 *          batch at MAX_MEDIA_BYTES_PER_REQUEST of file bytes). Used by the backfill
 *          for photos sent before they were stored, and since 2026-10-03 by a
 *          send whose photos would push the /chat body past
 *          INLINE_IMAGE_BUDGET_BYTES (any number of photos: they go first, in
 *          batches of at most that many file bytes, and the turn names them
 *          in `image_refs`). Any other new photo is stored by the orchestrator
 *          straight from the /chat body.
 *
 * The multipart body STREAMS through (duplex 'half'), counted against
 * MAX_MEDIA_BODY_BYTES as it passes and never held: 64 MiB of photos crosses
 * this process one chunk at a time, exactly as a dataset does through
 * /api/upload. Status and body come back as the orchestrator gave them —
 * 400/404/413/415/507 each mean something different to the caller.
 */

import { boundedBodyStream, declaredBodyOverLimit, isBodyTooLarge } from '@/lib/proxy';
import {
  crossSiteRefusal,
  decodeSegment,
  invalidReference,
  isAbort,
  MAX_MEDIA_BODY_BYTES,
  orchestratorUrl,
  SAFE_CONVERSATION,
} from '../_media';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

type Context = { params: Promise<{ conversation: string }> };

/** The conversation segment, validated, or null. Before any fetch exists. */
async function conversationOf(ctx: Context): Promise<string | null> {
  const conv = decodeSegment((await ctx.params).conversation);
  return conv !== null && SAFE_CONVERSATION.test(conv) ? conv : null;
}

/** A JSON answer passed through as a stream, with its status, never cached. */
function relayJson(upstream: Response): Response {
  if (upstream.status === 204 || upstream.status === 304) {
    return new Response(null, { status: upstream.status, headers: { 'cache-control': 'no-store' } });
  }
  return new Response(upstream.body, {
    status: upstream.status,
    headers: {
      'content-type': upstream.headers.get('content-type') ?? 'application/json',
      'cache-control': 'no-store',
    },
  });
}

export async function GET(req: Request, ctx: Context): Promise<Response> {
  const conv = await conversationOf(ctx);
  if (!conv) return invalidReference();
  if (process.env.MOCK_MODE === 'true') return Response.json({ items: [] });
  const cookie = req.headers.get('cookie');
  try {
    const upstream = await fetch(`${orchestratorUrl()}/chat-media/${encodeURIComponent(conv)}`, {
      headers: cookie ? { cookie } : {},
      signal: req.signal,
      cache: 'no-store',
    });
    return relayJson(upstream);
  } catch (err) {
    if (isAbort(err, req)) return new Response(null, { status: 499 });
    return Response.json({ code: 'unreachable' }, { status: 502 });
  }
}

export async function POST(req: Request, ctx: Context): Promise<Response> {
  const conv = await conversationOf(ctx);
  if (!conv) return invalidReference();
  const refusal = crossSiteRefusal(req);
  if (refusal) return refusal;
  if (process.env.MOCK_MODE === 'true') {
    return Response.json({ code: 'disabled_in_mock_mode' }, { status: 404 });
  }
  const type = req.headers.get('content-type') ?? '';
  if (!type.toLowerCase().startsWith('multipart/form-data')) {
    return Response.json({ code: 'bad_request' }, { status: 400 });
  }
  // Refused on the caller's own declaration first, so an oversized upload is
  // told no before a byte is read; the stream below holds the same line
  // against the bytes that actually arrive.
  if (declaredBodyOverLimit(req, MAX_MEDIA_BODY_BYTES)) {
    return Response.json({ code: 'too_large' }, { status: 413 });
  }
  const cookie = req.headers.get('cookie');
  try {
    const upstream = await fetch(`${orchestratorUrl()}/chat-media/${encodeURIComponent(conv)}`, {
      method: 'POST',
      // The multipart body and its boundary pass through untouched.
      headers: { 'content-type': type, ...(cookie ? { cookie } : {}) },
      body: req.body ? boundedBodyStream(req.body, MAX_MEDIA_BODY_BYTES) : null,
      // Required by undici to STREAM a request body rather than buffer it.
      duplex: 'half',
      signal: req.signal,
      cache: 'no-store',
    } as RequestInit & { duplex: 'half' });
    return relayJson(upstream);
  } catch (err) {
    // A body that ran past the cap fails the in-flight fetch rather than
    // returning; the refusal arrives wrapped in undici's own TypeError.
    if (isBodyTooLarge(err)) return Response.json({ code: 'too_large' }, { status: 413 });
    if (isAbort(err, req)) return new Response(null, { status: 499 });
    return Response.json({ code: 'unreachable' }, { status: 502 });
  }
}
