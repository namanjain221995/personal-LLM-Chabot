/**
 * GET /api/uploads/[conversation]/[upload]/file — turn a stored upload
 * reference back into bytes.
 *
 * Phase 3's read side: the browser keeps only { conversationId, uploadId }
 * after a reload, and this proxy is what makes that reference durable. The
 * orchestrator owns the truth (ownership via the session cookie, 404 for
 * never-existed, 410 for swept-by-TTL); this route validates the SHAPE of the
 * reference before anything is fetched, forwards the session, and passes the
 * answer through without editorialising the status.
 *
 * 2026-10-02 (chat media, CONTRACT §10): this URL is also the `src` of the
 * chat's <video> and <audio> players, so it speaks byte RANGES. A player
 * never downloads a file to play it: it asks for the first bytes (and, for an
 * MP4 whose index sits at the end, the last ones), then for whatever range a
 * seek lands in, so a 4 GB video is never pulled whole through this proxy.
 * The orchestrator's FileResponse already answers a Range with 206 and the
 * exact slice (Starlette 1.6); what this proxy adds is not losing that on the
 * way through: `Range` and `If-Range` go up, and the 206 or 416 comes back
 * with its `content-range` and `accept-ranges`. The pattern is the recording
 * proxy's (app/api/audio/sessions/_forward.ts), which plays recordings the
 * same way.
 */

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

/** Conversation ids as the app mints them: UUIDs and conv_* slugs. */
const SAFE_CONVERSATION = /^[A-Za-z0-9_-]{1,64}$/;
/** Upload ids exactly as the orchestrator mints them: 32 lowercase hex. */
const SAFE_UPLOAD = /^[0-9a-f]{32}$/;

export function isSafeUploadRef(conversation: string, upload: string): boolean {
  return SAFE_CONVERSATION.test(conversation) && SAFE_UPLOAD.test(upload);
}

/**
 * The request headers that decide WHICH bytes come back; nothing else the
 * browser sends goes up (the cookie is added on its own). `If-Range` rides
 * with `Range` so a player resuming against a file that changed gets the
 * whole new file rather than a slice of it spliced onto the old one.
 */
const FORWARDED_REQUEST_HEADERS = ['range', 'if-range'] as const;

/** The response headers a download, a preview or a player reads. */
const FORWARDED_RESPONSE_HEADERS = [
  'content-type',
  'content-disposition',
  'content-length',
  'content-range',
  'accept-ranges',
] as const;

export async function GET(
  req: Request,
  { params }: { params: Promise<{ conversation: string; upload: string }> },
): Promise<Response> {
  const { conversation, upload } = await params;
  let conv: string;
  let up: string;
  try {
    conv = decodeURIComponent(conversation);
    up = decodeURIComponent(upload);
  } catch {
    return Response.json({ message: 'invalid upload reference' }, { status: 400 });
  }
  // Validation FIRST: a malformed id must never become a request, let alone
  // a path segment (P3-03/04/05).
  if (!isSafeUploadRef(conv, up)) {
    return Response.json({ message: 'invalid upload reference' }, { status: 400 });
  }

  const orchestratorUrl = process.env.ORCHESTRATOR_URL ?? 'http://localhost:8080';
  const headers: Record<string, string> = {
    // A byte range is a range of the STORED bytes. A compressed answer would
    // make content-range and content-length describe bytes the browser never
    // receives, because fetch() hands this proxy a decoded body.
    'accept-encoding': 'identity',
  };
  // The OWNER check happens upstream; without the session the orchestrator
  // answers 401 and the ladder falls back correctly.
  const cookie = req.headers.get('cookie');
  if (cookie) headers.cookie = cookie;
  for (const name of FORWARDED_REQUEST_HEADERS) {
    const value = req.headers.get(name);
    if (value) headers[name] = value;
  }

  let upstream: Response;
  try {
    upstream = await fetch(
      `${orchestratorUrl}/uploads/${encodeURIComponent(conv)}/${encodeURIComponent(up)}/file`,
      { signal: req.signal, headers },
    );
  } catch {
    return Response.json({ message: 'upload service unreachable' }, { status: 502 });
  }

  if (upstream.status === 416) {
    // The player asked past the end of the file. The status and its
    // `bytes */<size>` content-range ARE the answer (RFC 9110 §15.5.17): the
    // player reads the size from it and asks again inside it.
    const out = new Headers({ 'cache-control': 'no-store' });
    const range = upstream.headers.get('content-range');
    if (range) out.set('content-range', range);
    return new Response(null, { status: 416, headers: out });
  }

  if (!upstream.ok) {
    // 410 is a STATEMENT (the TTL swept it), not a shrug — the client shows
    // "expired" instead of the "no longer available" lie this fixes (P3-06).
    if (upstream.status === 410) {
      return Response.json(
        { message: 'this upload has expired and its bytes were removed' },
        { status: 410 },
      );
    }
    // Pass 401/404/5xx through instead of flattening them (P3-08): the
    // resolution ladder distinguishes them from expiry.
    return Response.json({ message: 'upload unavailable' }, { status: upstream.status });
  }

  const out = new Headers({ 'cache-control': 'no-store' });
  for (const name of FORWARDED_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) out.set(name, value);
  }
  // fetch() hands back a DECODED body, and a length measured before decoding
  // would cut it short. `identity` was asked for, so this only guards an
  // upstream that compresses anyway.
  if (upstream.headers.get('content-encoding')) out.delete('content-length');
  // 200 for the whole file, 206 for a slice of it: the status is how a player
  // learns its Range was honoured, so it passes through as it came.
  return new Response(upstream.body, { status: upstream.status, headers: out });
}
