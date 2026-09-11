/**
 * GET /api/chat/requests/[intent_id] — what the SERVER knows about one send
 * intent (docs/upload-reliability/API.md).
 *
 * The reconciliation a reloaded tab runs before it claims anything about the
 * last turn: `{status, generation_id, attempt, resumable, answer_persisted,
 * live}`. Until this existed the browser's only question was "is a stream
 * running in this tab?", and the answer "no" was rendered as "this message
 * was never sent" over generations that were running perfectly well.
 *
 * The upstream body is passed through UNCHANGED, 404 included, for one
 * reason: the client has to tell a 404 that means "no such intent" (the
 * turn really never reached the server) from a 404 that means "this backend
 * has no such route" (a new frontend against an old orchestrator — see the
 * Compatibility section of CONTRACT.md). The orchestrator says
 * `{"detail": "unknown intent"}`; FastAPI's own missing-route 404 says
 * `{"detail": "Not Found"}`. Rewriting either into a house sentence would
 * destroy the distinction, so nothing here rewrites anything: only the
 * transport failure this proxy suffers itself becomes copy of its own.
 */

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

/** The contract's intent_id: 1-64 chars of [A-Za-z0-9_-]. */
const SAFE_ID = /^[\w-]{1,64}$/;

export async function GET(
  req: Request,
  { params }: { params: Promise<{ id: string }> },
): Promise<Response> {
  const { id } = await params;
  // decodeURIComponent throws URIError on a malformed escape ("%"), which
  // would surface as an unhandled 500 rather than a rejected request.
  let decoded: string;
  try {
    decoded = decodeURIComponent(id);
  } catch {
    return Response.json({ message: 'invalid intent id' }, { status: 400 });
  }
  if (!SAFE_ID.test(decoded)) {
    return Response.json({ message: 'invalid intent id' }, { status: 400 });
  }
  if (process.env.MOCK_MODE === 'true') {
    return Response.json({ detail: 'unknown intent' }, { status: 404 });
  }
  const orchestratorUrl =
    process.env.ORCHESTRATOR_URL ?? 'http://localhost:8080';
  let upstream: Response;
  try {
    upstream = await fetch(
      `${orchestratorUrl}/chat/requests/${encodeURIComponent(decoded)}`,
      {
        cache: 'no-store',
        signal: req.signal,
        // Owner-scoped: without the cookie the orchestrator 404s, and the
        // browser would read that as "your turn was never sent".
        headers: req.headers.get('cookie')
          ? { cookie: req.headers.get('cookie') as string }
          : {},
      },
    );
  } catch {
    return Response.json(
      { message: 'The orchestrator is unreachable.', code: 'NETWORK_ERROR' },
      { status: 502 },
    );
  }
  let body: unknown;
  try {
    body = await upstream.json();
  } catch {
    // Not JSON — an intermediary's error page. Never let that read as a
    // definitive answer about the intent.
    return Response.json(
      { message: 'The orchestrator answered with no body.', code: 'UNKNOWN_ERROR' },
      { status: upstream.ok ? 502 : upstream.status },
    );
  }
  return Response.json(body, { status: upstream.status });
}
