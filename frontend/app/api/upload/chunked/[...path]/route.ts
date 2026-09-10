/**
 * /api/upload/chunked/* — the chunked-upload rail's proxy.
 *
 * Same shape as /api/upload one directory up: forward the body untouched
 * (init and complete are small; a part is up to 64 MB and STREAMS — never
 * buffered here), ride the session cookie along so ownership is decided
 * upstream, and pass the orchestrator's status through unedited. Path
 * segments are validated to the exact shapes the rail mints, so this proxy
 * can never be steered at an arbitrary orchestrator path.
 *
 * 2026-09-10: GET and DELETE ride the same road. GET is the resume call —
 * "which parts do you already have?" — and DELETE hands a session back when
 * the person removes the chip, so its parts (and, for a video, the analysis
 * they would have started) are not left to a 24-hour TTL. Both are
 * body-less: `body`/`duplex` are set only for the methods that carry one,
 * because undici rejects a GET that declares a stream body at all.
 */

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

const SAFE_SEGMENT = /^[A-Za-z0-9_-]{1,64}$/;

async function forward(
  req: Request,
  { params }: { params: Promise<{ path: string[] }> },
): Promise<Response> {
  if (process.env.MOCK_MODE === 'true') {
    return Response.json({ message: 'uploads are disabled in mock mode' }, { status: 404 });
  }
  const { path } = await params;
  if (
    !Array.isArray(path) ||
    path.length === 0 ||
    path.length > 4 ||
    !path.every((seg) => SAFE_SEGMENT.test(seg))
  ) {
    return Response.json({ message: 'invalid upload path' }, { status: 400 });
  }
  const orchestratorUrl = process.env.ORCHESTRATOR_URL ?? 'http://localhost:8080';
  const cookie = req.headers.get('cookie');
  const contentType = req.headers.get('content-type');
  // The part hash the client computed. It rides through untouched: the
  // orchestrator is the only thing that can check it, because only it sees
  // the bytes land.
  const partHash = req.headers.get('x-part-sha256');
  const hasBody = req.method === 'POST' || req.method === 'PUT';
  try {
    const upstream = await fetch(
      `${orchestratorUrl}/uploads/chunked/${path.map(encodeURIComponent).join('/')}`,
      {
        method: req.method,
        headers: {
          ...(cookie ? { cookie } : {}),
          ...(contentType ? { 'content-type': contentType } : {}),
          ...(partHash ? { 'x-part-sha256': partHash } : {}),
        },
        ...(hasBody
          ? {
              body: req.body,
              // Node's fetch requires this to stream a request body instead
              // of buffering 64 MB parts in the proxy's memory.
              duplex: 'half',
            }
          : {}),
        signal: req.signal,
      } as RequestInit & { duplex?: 'half' },
    );
    const status = upstream.status;
    // 204 (a cancelled session) and 304 may not carry a body at all —
    // constructing a Response with one throws.
    if (status === 204 || status === 304) {
      return new Response(null, { status, headers: { 'cache-control': 'no-store' } });
    }
    const text = await upstream.text();
    return new Response(text, {
      status,
      headers: {
        'content-type': upstream.headers.get('content-type') ?? 'application/json',
        'cache-control': 'no-store',
      },
    });
  } catch {
    return Response.json({ message: 'upload service unreachable' }, { status: 502 });
  }
}

export { forward as POST, forward as PUT, forward as GET, forward as DELETE };
