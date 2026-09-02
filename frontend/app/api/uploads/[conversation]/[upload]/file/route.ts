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
  const cookie = req.headers.get('cookie');
  let upstream: Response;
  try {
    upstream = await fetch(
      `${orchestratorUrl}/uploads/${encodeURIComponent(conv)}/${encodeURIComponent(up)}/file`,
      {
        signal: req.signal,
        // The OWNER check happens upstream; without the session the
        // orchestrator answers 401 and the ladder falls back correctly.
        headers: cookie ? { cookie } : {},
      },
    );
  } catch {
    return Response.json({ message: 'upload service unreachable' }, { status: 502 });
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

  const headers = new Headers({ 'cache-control': 'no-store' });
  for (const name of ['content-type', 'content-disposition', 'content-length']) {
    const value = upstream.headers.get(name);
    if (value) headers.set(name, value);
  }
  return new Response(upstream.body, { status: 200, headers });
}
