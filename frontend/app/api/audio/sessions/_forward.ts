/**
 * The recording-session proxy, shared by every route under
 * /api/audio/sessions (2026-09-29).
 *
 * A dictation is now a SESSION on the orchestrator: the browser opens it, sends
 * the recording in numbered parts WHILE the person talks, finishes it, and
 * long-polls for the transcript. Each of those is one small request, so no
 * request here is ever large or long: a part is about 80 KB (5 s of Opus at
 * the 128.7 kb/s Chrome records at, measured 2026-09-28) and at most 8 MiB, and
 * a long-poll answers within 25 s. That is why these routes need none of the
 * heartbeat machinery the one-blob /api/audio/transcribe route carries, and why
 * Cloudflare's 100 MB upload wall and 125 s first-byte limit stop mattering.
 *
 * What this adds is transport and nothing else. Authorization lives UPSTREAM:
 * the orchestrator requires the session cookie and the voice-input feature and
 * decides ownership of every recording. The proxy forwards the cookie and the
 * few headers the contract names, streams bodies both ways without buffering
 * them, and passes the orchestrator's status and body through unedited.
 *
 * PATH SEGMENTS ARE VALIDATED BEFORE ANY REQUEST EXISTS. A session id is
 * exactly the 32 lowercase hex characters the orchestrator mints and a part
 * number is a plain non-negative integer, so nothing a browser puts in the URL
 * can steer this proxy, cookie attached, at another orchestrator path. A
 * malformed id gets the same 404 an unknown one does, as the contract asks.
 */

export const SESSION_ID = /^[0-9a-f]{32}$/;
export const PART_SEQ = /^(0|[1-9][0-9]{0,8})$/;

/** Headers the browser may send that the orchestrator reads. Nothing else goes up. */
const FORWARDED_REQUEST_HEADERS = [
  'cookie',
  'content-type',
  'x-part-sha256',
  'x-part-end-ms',
  'range',
] as const;

/** Headers the orchestrator answers with that the browser reads. */
const FORWARDED_RESPONSE_HEADERS = [
  'content-type',
  'content-length',
  'content-range',
  'accept-ranges',
  'content-disposition',
  'retry-after',
  'x-recording-complete',
] as const;

export function notFound(): Response {
  return Response.json(
    {
      detail: 'This recording is no longer on the server. It was discarded or has expired.',
      reason: 'not_found',
    },
    { status: 404, headers: { 'cache-control': 'no-store' } },
  );
}

export function badRequest(detail: string): Response {
  return Response.json(
    { detail, reason: 'bad_request' },
    { status: 400, headers: { 'cache-control': 'no-store' } },
  );
}

/**
 * Forward one request to `${ORCHESTRATOR_URL}${path}` and hand the answer back.
 *
 * `path` must already be built from validated segments. The query string is
 * forwarded verbatim (cursor, since_rev, wait_s, limit, before): the
 * orchestrator validates it and answers 400 itself, so there is no second
 * opinion here to disagree with it.
 */
export async function forwardSession(req: Request, path: string): Promise<Response> {
  if (process.env.MOCK_MODE === 'true') {
    return Response.json(
      { detail: 'voice input is disabled in mock mode', reason: 'voice_unavailable' },
      { status: 404 },
    );
  }
  const orchestratorUrl = process.env.ORCHESTRATOR_URL ?? 'http://localhost:8080';
  const { search } = new URL(req.url);
  const headers: Record<string, string> = {};
  for (const name of FORWARDED_REQUEST_HEADERS) {
    const value = req.headers.get(name);
    if (value) headers[name] = value;
  }
  const hasBody = req.method === 'POST' || req.method === 'PUT';
  let upstream: Response;
  try {
    upstream = await fetch(`${orchestratorUrl}${path}${search}`, {
      method: req.method,
      headers,
      ...(hasBody
        ? {
            body: req.body,
            // Required by undici to STREAM a request body rather than buffer
            // it; undici also refuses a GET that declares a body at all.
            duplex: 'half',
          }
        : {}),
      // A tab that goes away takes its request with it: a withdrawn long-poll
      // or part upload is not left running against the orchestrator.
      signal: req.signal,
      cache: 'no-store',
    } as RequestInit & { duplex?: 'half' });
  } catch {
    // The client reads `proxy_unreachable` as "not the server speaking": the
    // part stays in its outbox and is sent again.
    return Response.json(
      { detail: 'The recording service is unreachable.', reason: 'proxy_unreachable' },
      { status: 502, headers: { 'cache-control': 'no-store' } },
    );
  }

  const out = new Headers({
    'cache-control': 'no-store, no-transform',
    'x-accel-buffering': 'no',
  });
  for (const name of FORWARDED_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) out.set(name, value);
  }
  if (!out.has('content-type')) out.set('content-type', 'application/json');
  // fetch() hands back a DECODED body; a length measured before decoding
  // would cut it short. The orchestrator does not compress today.
  if (upstream.headers.get('content-encoding')) out.delete('content-length');
  // 204 (a discarded recording) and 304 may not carry a body; constructing a
  // Response with one throws.
  if (upstream.status === 204 || upstream.status === 304) {
    out.delete('content-type');
    out.delete('content-length');
    return new Response(null, { status: upstream.status, headers: out });
  }
  return new Response(upstream.body, { status: upstream.status, headers: out });
}
