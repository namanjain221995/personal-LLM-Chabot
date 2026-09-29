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
 * CROSS-SITE REQUESTS ARE REFUSED HERE (2026-09-29, security review item 13).
 *
 * This proxy forwards no Origin, so the orchestrator's own Origin check
 * (main.py) never sees one, and the ts_session cookie is SameSite=Lax, which
 * does not separate sibling subdomains of the app's site. The check is made
 * where the browser's own headers still are, exactly as the Files console
 * proxy does (app/api/devplatform/[...path]/route.ts `crossSiteWriteRefusal`):
 *
 *  · `Sec-Fetch-Site` is set by the browser and cannot be written by a page.
 *    Present and neither `same-origin` nor `none` (typed into the address
 *    bar, e.g. opening a recording's download link) is refused, on every
 *    method: a sibling subdomain says `same-site`.
 *  · The JSON POSTs (create, finish, retranscribe) must say
 *    `application/json`. A cross-site page cannot send that without a CORS
 *    preflight this route never answers, and the orchestrator parses the body
 *    as JSON whatever the header says, so without this a `text/plain` form
 *    post would reach it. PUT and DELETE always preflight.
 *
 * Origin is not compared with Host: the public tunnel may rewrite Host, and
 * every current browser sends Sec-Fetch-Site (Chrome 76, Firefox 90, Safari
 * 16.4), so the comparison would only add false refusals.
 */
export function crossSiteRefusal(req: Request, opts: { json?: boolean } = {}): Response | null {
  const site = req.headers.get('sec-fetch-site')?.trim().toLowerCase() ?? null;
  if (site !== null && site !== 'same-origin' && site !== 'none') {
    return Response.json(
      { detail: 'Cross-site request refused.', reason: 'cross_site' },
      { status: 403, headers: { 'cache-control': 'no-store' } },
    );
  }
  if (opts.json && req.method === 'POST') {
    const type = (req.headers.get('content-type') ?? '').split(';')[0]!.trim().toLowerCase();
    if (type !== 'application/json') {
      return Response.json(
        { detail: 'This request must send JSON.', reason: 'bad_request' },
        { status: 415, headers: { 'cache-control': 'no-store' } },
      );
    }
  }
  return null;
}

/**
 * How long the orchestrator may take to START answering (2026-09-29, security
 * review item 13; there was no deadline at all). Only the wait for response
 * headers is bounded: a recording's download then streams for as long as it
 * takes, and the browser's own abort still ends it.
 *
 *  · A long-poll GET answers by its `wait_s` (the contract caps it at 25 s):
 *    that plus 15 s.
 *  · A part's body streams through before the answer, so its deadline grows
 *    with it: 30 s plus one second per 16 KiB (128 kb/s, the recording's own
 *    rate — a slower client cannot keep up with a recording anyway).
 *  · Everything else: 30 s, lib/proxy.ts's PROXY_TIMEOUT_MS.
 */
export const SESSION_PROXY_TIMEOUT_MS = 30_000;
export const LONG_POLL_CAP_S = 25;

export function upstreamTimeoutMs(req: Request, kind: 'poll' | 'part' | 'plain'): number {
  if (kind === 'poll') {
    const asked = Number(new URL(req.url).searchParams.get('wait_s') ?? 0);
    const wait = Number.isFinite(asked) ? Math.min(LONG_POLL_CAP_S, Math.max(0, asked)) : 0;
    return (wait + 15) * 1000;
  }
  if (kind === 'part') {
    const length = Number(req.headers.get('content-length') ?? 0);
    const bytes = Number.isFinite(length) && length > 0 ? length : 8 * 1024 * 1024;
    return SESSION_PROXY_TIMEOUT_MS + Math.ceil(bytes / 16_384) * 1000;
  }
  return SESSION_PROXY_TIMEOUT_MS;
}

/**
 * Forward one request to `${ORCHESTRATOR_URL}${path}` and hand the answer back.
 *
 * `path` must already be built from validated segments. The query string is
 * forwarded verbatim (cursor, since_rev, wait_s, limit, before): the
 * orchestrator validates it and answers 400 itself, so there is no second
 * opinion here to disagree with it.
 */
export async function forwardSession(
  req: Request,
  path: string,
  opts: { json?: boolean; kind?: 'poll' | 'part' | 'plain' } = {},
): Promise<Response> {
  if (process.env.MOCK_MODE === 'true') {
    return Response.json(
      { detail: 'voice input is disabled in mock mode', reason: 'voice_unavailable' },
      { status: 404 },
    );
  }
  const refusal = crossSiteRefusal(req, opts);
  if (refusal) return refusal;
  const orchestratorUrl = process.env.ORCHESTRATOR_URL ?? 'http://localhost:8080';
  const { search } = new URL(req.url);
  const headers: Record<string, string> = {};
  for (const name of FORWARDED_REQUEST_HEADERS) {
    const value = req.headers.get(name);
    if (value) headers[name] = value;
  }
  const hasBody = req.method === 'POST' || req.method === 'PUT';
  const deadline = new AbortController();
  const timer = setTimeout(() => deadline.abort(), upstreamTimeoutMs(req, opts.kind ?? 'plain'));
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
      // or part upload is not left running against the orchestrator. And an
      // orchestrator that never starts answering does not hold this handler.
      signal: AbortSignal.any([req.signal, deadline.signal]),
      cache: 'no-store',
    } as RequestInit & { duplex?: 'half' });
  } catch {
    clearTimeout(timer);
    if (deadline.signal.aborted && !req.signal.aborted) {
      // Read by the client as "not the server speaking", like the 502.
      return Response.json(
        { detail: 'The recording service took too long to answer.', reason: 'proxy_timeout' },
        { status: 504, headers: { 'cache-control': 'no-store' } },
      );
    }
    // The client reads `proxy_unreachable` as "not the server speaking": the
    // part stays in its outbox and is sent again.
    return Response.json(
      { detail: 'The recording service is unreachable.', reason: 'proxy_unreachable' },
      { status: 502, headers: { 'cache-control': 'no-store' } },
    );
  }
  // Headers are in: the body streams on under the browser's own abort only.
  clearTimeout(timer);

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
