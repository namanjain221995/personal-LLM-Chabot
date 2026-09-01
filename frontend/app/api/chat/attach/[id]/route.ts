/**
 * GET /api/chat/attach/[id] — re-join a running detached generation after a
 * page reload: the orchestrator replays every buffered SSE event (instant
 * partial answer) and then streams live. 404 once the generation finished —
 * the answer is waiting in history at that point.
 */

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const SSE_HEADERS = {
  'Content-Type': 'text/event-stream; charset=utf-8',
  'Cache-Control': 'no-cache, no-transform',
  Connection: 'keep-alive',
  'X-Accel-Buffering': 'no',
} as const;

const SAFE_ID = /^[\w-]{1,64}$/;

export async function GET(
  req: Request,
  { params }: { params: Promise<{ id: string }> },
): Promise<Response> {
  const { id } = await params;
  // decodeURIComponent throws URIError on a malformed escape (e.g. "%"),
  // which surfaced as an unhandled 500 rather than a rejected request.
  let decoded: string;
  try {
    decoded = decodeURIComponent(id);
  } catch {
    return Response.json({ message: 'invalid conversation id' }, { status: 400 });
  }
  if (!SAFE_ID.test(decoded)) {
    return Response.json({ message: 'invalid conversation id' }, { status: 400 });
  }
  if (process.env.MOCK_MODE === 'true') {
    return Response.json({ message: 'no active generation' }, { status: 404 });
  }
  const orchestratorUrl =
    process.env.ORCHESTRATOR_URL ?? 'http://localhost:8080';
  let upstream: Response;
  try {
    upstream = await fetch(
      `${orchestratorUrl}/chat/attach/${encodeURIComponent(decoded)}`,
      {
        signal: req.signal,
        // Owner-scoped: without the cookie the orchestrator 404s, so a user
        // could never re-join their OWN generation after a reload.
        headers: req.headers.get('cookie')
          ? { cookie: req.headers.get('cookie') as string }
          : {},
      },
    );
  } catch {
    return Response.json(
      { message: 'The orchestrator is unreachable.' },
      { status: 502 },
    );
  }
  if (!upstream.ok || !upstream.body) {
    // 401 passes through untouched: a session that died mid-generation must
    // reach the client as "sign in", never be disguised as "finished" (404)
    // or "orchestrator down" (502).
    const status =
      upstream.status === 404 || upstream.status === 401 ? upstream.status : 502;
    return Response.json(
      {
        message:
          upstream.status === 401 ? 'Sign in required.' : 'no active generation',
      },
      { status },
    );
  }
  return new Response(upstream.body, { headers: SSE_HEADERS });
}
