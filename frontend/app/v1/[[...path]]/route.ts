/**
 * `/v1/*` — the public developer API's same-origin edge (CONTRACT §2, §11).
 *
 * WHY THIS FILE EXISTS AT ALL (2026-09-13). The contract puts the public API
 * at `https://ai.techsarasolutions.com/v1/…` today and at
 * `https://api.techsarasolutions.com/v1/…` "later" — and the later one needs a
 * second hostname through the Cloudflare tunnel, a second certificate and a
 * DNS change nobody has made yet. Until then the API is reached on the app's
 * own origin, which means it arrives at THIS process, and a Next route handler
 * is the only thing in front of the orchestrator that can carry it. CONTRACT
 * §11 allows exactly that, and §18 is equally clear about what this handler is
 * NOT: it is not a security boundary. Every check that matters — the key, the
 * scopes, the model allowlist, the quota, the origin allowlist — happens in the
 * orchestrator, which is separately reachable on the LAN. This file's whole job
 * is to carry a request there faithfully and carry the answer back without
 * spoiling it.
 *
 * "Faithfully" is five specific things, each of which some proxy in this
 * repository was getting wrong before it was written down:
 *
 * 1. THE CREDENTIAL CROSSES. No proxy in this codebase forwarded
 *    `Authorization` at all (confirmed finding, the wave-1 audit) — every one
 *    of them was built for the cookie world, where the browser's `Cookie`
 *    header was the only credential there was. A `/v1` request whose bearer
 *    token is dropped on this hop is a 401 for a key that is perfectly valid,
 *    so `authorization` and `idempotency-key` go upstream byte-for-byte.
 * 2. THE COOKIE DOES NOT. CONTRACT §1: "/v1 reads exactly one credential — the
 *    Authorization header — and ignores Cookie entirely", because a surface
 *    that accepted both would be a confused deputy — any page on the internet
 *    could drive it with a signed-in person's ambient cookie. The orchestrator
 *    ignores it anyway; this edge does not send it, so there is nothing to
 *    ignore, and `Access-Control-Allow-Credentials` is never relayed back
 *    either (§3) so no browser can be told to attach one.
 * 3. THE STREAM IS NOT BUFFERED. `new Response(upstream.body, …)` hands the
 *    upstream stream to the client untouched, the pattern app/api/chat/route.ts
 *    has used since V2 §10, with `X-Accel-Buffering: no` re-asserted here.
 *    `await upstream.arrayBuffer()` would hold every token until the
 *    generation finished and turn a streaming API into a slow blocking one —
 *    and would break the 15-second heartbeat that keeps an idle proxy from
 *    closing the connection (the SSE invariant, 2026-08).
 * 4. THE STATUS AND THE RETRY HEADERS SURVIVE. A 429 that reaches the caller
 *    as a 502, or with its `Retry-After` eaten, leaves a client guessing when
 *    to come back — which is how a throttled integration turns into a retry
 *    storm. The status is relayed exactly and the headers CONTRACT §12 owes
 *    (`RateLimit`, `RateLimit-Policy`, `Retry-After`, `X-Request-Id`) are on
 *    the allowlist below.
 * 5. NOTHING ABOUT THE INSIDE LEAKS. CONTRACT §9 forbids an internal hostname,
 *    a private IP or a path in any response body. The three failures this
 *    handler can produce on its own carry fixed sentences; the orchestrator's
 *    own URL is never in one, never in a header, and a 3xx (which could only
 *    carry an internal `Location`) is refused rather than relayed.
 *
 * WHAT THIS HANDLER DELIBERATELY DOES NOT DO:
 *
 * · it adds no credential of its own. There is no service token, no shared
 *   secret and no "trusted frontend" header here. A request with no
 *   `Authorization` reaches the orchestrator with no `Authorization` and is
 *   told 401 by the thing that is entitled to decide that;
 * · it has no wall-clock ceiling, unlike lib/proxy.ts's 30 seconds. A
 *   generation legitimately runs for minutes, and an edge timeout shorter than
 *   the generation's own wall clock converts finished work into a 504 and
 *   charges for it — the same rule that keeps LLM_REQUEST_TIMEOUT above
 *   GEN_WALL_CLOCK_S everywhere else in this system. The client's signal is
 *   forwarded, so an abandoned request still stops costing something, and the
 *   orchestrator owns the timeout that ends a wedged one (§9 `timeout`);
 * · it does not parse, validate or rewrite the body. Validation is stated once,
 *   server-side (§8), and a second copy here would drift and start refusing
 *   requests the API accepts.
 */

import {
  declaredBodyOverLimit,
  orchestratorUrl,
  readBoundedBody,
  trustedClientIp,
  trustedForwardedProto,
} from '@/lib/proxy';
import { logProxyError } from '@/lib/serverLog';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

/* ------------------------------------------------------------ the cap -- */

/** CONTRACT §12: 1 MiB of body, refused before parsing. */
export const DEFAULT_PUBLIC_API_BODY_BYTES = 1024 * 1024;

/**
 * The body cap, read at call time from the SAME environment variable the
 * orchestrator reads (`PUBLIC_API_MAX_BODY_BYTES`, app/publicapi/models.py
 * `max_body_bytes()`), so a deployment that tightens or loosens the limit
 * moves both halves at once.
 *
 * The two must agree, and the edge is deliberately not tighter: a cap here
 * below the server's would refuse requests the API documents as acceptable,
 * and the caller would have no way to tell that the refusal came from a proxy
 * rather than from the contract. Equal is the only setting that keeps the 413
 * honest wherever it is raised.
 */
export function publicApiBodyBytes(): number {
  const raw = Number(process.env.PUBLIC_API_MAX_BODY_BYTES ?? '');
  return Number.isFinite(raw) && raw > 0
    ? Math.floor(raw)
    : DEFAULT_PUBLIC_API_BODY_BYTES;
}

/* -------------------------------------------------------- the headers -- */

/**
 * What goes UPSTREAM, named rather than filtered.
 *
 * Building the outbound set by naming what may go is the only version of this
 * that cannot be defeated by a spelling nobody thought of — the reasoning
 * lib/proxy.ts's CLIENT_FORWARDING_HEADERS docblock sets out, applied here to
 * a surface where the stakes are a quota and a bill.
 *
 * `cookie` is absent on purpose (CONTRACT §1) and so is every
 * client-supplied forwarding header; `origin` IS forwarded because the
 * orchestrator, not this edge, decides whether it is in the project's
 * `allowed_origins` (§3), and the two `access-control-request-*` headers
 * because a CORS preflight is meaningless without them.
 */
export const REQUEST_HEADER_ALLOWLIST = [
  'authorization',
  'idempotency-key',
  'content-type',
  'accept',
  'accept-language',
  'user-agent',
  'origin',
  'access-control-request-method',
  'access-control-request-headers',
] as const;

/**
 * What comes BACK, also named rather than filtered.
 *
 * The rate-limit family and `Retry-After` are the ones a correct client cannot
 * work without (§12); the CORS answers are the orchestrator's own (§3) and
 * have to survive this hop or every browser call fails preflight.
 *
 * Four names are missing on purpose:
 *   · `set-cookie` — /v1 is cookie-blind in both directions; an API that could
 *     set a cookie on ai.techsarasolutions.com would be handing a bearer-key
 *     caller ambient authority over the chat app;
 *   · `access-control-allow-credentials` — CONTRACT §3 says it is never sent,
 *     and "never" has to include "never relayed";
 *   · `content-length` and `content-encoding` — undici decodes the upstream
 *     body, so both describe bytes that no longer exist by the time the
 *     response leaves here;
 *   · `location` — see `NULL_BODY_STATUSES` below.
 */
export const RESPONSE_HEADER_ALLOWLIST = [
  'content-type',
  'retry-after',
  'ratelimit',
  'ratelimit-policy',
  'x-request-id',
  // RFC 6750 §3: a 401/403 on a bearer-token API says WHY in this header
  // (`Bearer error="insufficient_scope", scope="responses.write"`). The
  // orchestrator sends it and the authentication page documents it; the
  // edge dropped it, so callers through the public URL never saw it
  // (found by executing the documentation, 2026-09-13).
  'www-authenticate',
  'cache-control',
  'vary',
  'access-control-allow-origin',
  'access-control-allow-methods',
  'access-control-allow-headers',
  'access-control-expose-headers',
  'access-control-max-age',
] as const;

/** Allowlisted by prefix: the x-ratelimit-* family has no fixed member list. */
const RESPONSE_HEADER_PREFIXES = ['x-ratelimit-'] as const;

/**
 * CONTRACT §10's streaming headers, re-asserted at the edge rather than
 * trusted to arrive. `X-Accel-Buffering: no` is the one that matters in
 * production: an intermediary that buffers a text/event-stream holds every
 * token until the generation ends, which looks exactly like a hung model.
 */
const SSE_HEADERS: Record<string, string> = {
  'content-type': 'text/event-stream; charset=utf-8',
  'cache-control': 'no-store, no-cache, no-transform',
  connection: 'keep-alive',
  'x-accel-buffering': 'no',
};

/** Statuses whose response may not carry a body — `new Response(bytes, …)`
    throws for these, which would turn a 204 into a 500. */
const NULL_BODY_STATUSES = new Set([101, 103, 204, 205, 304]);

/**
 * The statuses that carry a `Location`. Named one by one rather than tested as
 * "3xx", because 304 is in that range and is not a redirect: it is a cache
 * validator with no Location at all, and refusing it would be this edge
 * inventing a failure out of a perfectly ordinary answer.
 */
const REDIRECT_STATUSES = new Set([301, 302, 303, 307, 308]);

/* --------------------------------------------------------- the errors -- */

/**
 * The only three failures this edge can raise by itself, with the status and
 * wire `type` app/publicapi/errors.py gives them.
 *
 * A CLOSED table, for the same reason the server's `_CODES` is closed: the
 * error vocabulary of CONTRACT §9 is what every generated SDK's retry logic
 * switches on, and a proxy that invented a code (or answered a 502, which is
 * not in the table at all) would produce a failure no client has a branch for.
 */
const EDGE_ERRORS = {
  request_too_large: { status: 413, type: 'invalid_request_error' },
  model_unavailable: { status: 503, type: 'service_unavailable_error' },
  internal_error: { status: 500, type: 'server_error' },
} as const;

type EdgeErrorCode = keyof typeof EDGE_ERRORS;

/**
 * The envelope of CONTRACT §9, five keys, `param` and `request_id` nullable.
 *
 * `request_id` is null and no `X-Request-Id` is set, because this refusal
 * never reached the orchestrator and so has no id in any system. Inventing one
 * is worse than leaving the field empty — lib/serverLog.ts's `requestIdOf`
 * takes the same position, and for the same reason: an id that appears nowhere
 * else sends a support conversation looking for a request that was never
 * recorded.
 */
function edgeError(
  code: EdgeErrorCode,
  message: string,
  opts: { retryAfter?: number } = {},
): Response {
  const spec = EDGE_ERRORS[code];
  const headers: Record<string, string> = {
    'content-type': 'application/json',
    'cache-control': 'no-store',
  };
  // CONTRACT §9: Retry-After on every 429 and 503, integer seconds, minimum 1.
  if (opts.retryAfter !== undefined) {
    headers['retry-after'] = String(Math.max(1, Math.ceil(opts.retryAfter)));
  }
  return new Response(
    JSON.stringify({
      error: { message, type: spec.type, code, param: null, request_id: null },
    }),
    { status: spec.status, headers },
  );
}

/**
 * undici reports the real cause of a transport failure in a nested `cause`
 * chain, and the code (`ECONNREFUSED`, `UND_ERR_HEADERS_TIMEOUT`) is the part
 * an engineer reading the log needs. The MESSAGE is deliberately not taken:
 * it quotes the URL that failed, and this line is exactly the kind of thing
 * that ends up pasted into a ticket. The pattern is app/api/chat/route.ts's.
 */
function describeThrown(err: unknown): string {
  let cur: unknown = err;
  for (let depth = 0; cur && depth < 5; depth += 1) {
    const code = (cur as { code?: unknown }).code;
    if (typeof code === 'string') {
      return `${(err as { name?: string })?.name ?? 'Error'}: ${code}`;
    }
    cur = (cur as { cause?: unknown }).cause;
  }
  return (err as { name?: string })?.name ?? 'fetch failed';
}

/* ---------------------------------------------------------- the relay -- */

/** The upstream path for a `/v1/…` request: the same path, re-encoded. */
export function upstreamPathFor(parts: string[], search: string): string {
  // Next has percent-decoded the segments; re-encode so nothing (a response id
  // with a slash in it, say) can smuggle a separator into the upstream URL.
  const tail = parts.map(encodeURIComponent).join('/');
  return `/v1${tail ? `/${tail}` : ''}${search}`;
}

function forwardedHeaders(req: Request): Record<string, string> {
  const out: Record<string, string> = {};
  for (const name of REQUEST_HEADER_ALLOWLIST) {
    const value = req.headers.get(name);
    if (value) out[name] = value;
  }
  // The caller's address, from the one header this deployment is configured to
  // trust and never from one the caller chose (lib/proxy.ts `trustedClientIp`).
  // It matters more here than on the cookie routes: a project may carry an
  // `ip_allowlist` (SCHEMA-V34), and an allowlist fed a forgeable address is
  // not an allowlist.
  const clientIp = trustedClientIp(req);
  if (clientIp) out['x-forwarded-for'] = clientIp;
  const proto = trustedForwardedProto();
  if (proto) out['x-forwarded-proto'] = proto;
  return out;
}

function relayedHeaders(upstream: Response, sse: boolean): Headers {
  const out = new Headers();
  upstream.headers.forEach((value, name) => {
    const key = name.toLowerCase();
    const allowed =
      (RESPONSE_HEADER_ALLOWLIST as readonly string[]).includes(key) ||
      RESPONSE_HEADER_PREFIXES.some((prefix) => key.startsWith(prefix));
    if (allowed) out.set(key, value);
  });
  if (sse) for (const [key, value] of Object.entries(SSE_HEADERS)) out.set(key, value);
  if (!out.has('content-type')) out.set('content-type', 'application/json');
  // An API answer is one project's data and is never revalidated by anything
  // between here and the caller. Where the orchestrator has an opinion it was
  // relayed above; where it has none, nothing may hold this.
  if (!out.has('cache-control')) out.set('cache-control', 'no-store');
  return out;
}

function isEventStream(upstream: Response): boolean {
  const type = upstream.headers.get('content-type') ?? '';
  return type.toLowerCase().split(';')[0].trim() === 'text/event-stream';
}

async function handle(
  req: Request,
  ctx: { params: Promise<{ path?: string[] }> },
): Promise<Response> {
  const startedAt = Date.now();
  const { path } = await ctx.params;
  const { search } = new URL(req.url);
  const upstreamPath = upstreamPathFor(path ?? [], search);
  const limit = publicApiBodyBytes();

  let body: ArrayBuffer | undefined;
  if (req.method !== 'GET' && req.method !== 'HEAD' && req.method !== 'OPTIONS') {
    // Two refusals, because one is not enough: the caller's own declaration is
    // refused without touching the socket, and a body that declares nothing
    // (or lies about it) is measured chunk by chunk and cancelled the moment it
    // goes over. Either way the orchestrator is never called, so an oversized
    // body costs one quota-free 413 rather than a megabyte of someone else's
    // admission lane.
    const refuse = () =>
      edgeError(
        'request_too_large',
        `The request body is larger than the ${limit} byte limit.`,
      );
    if (declaredBodyOverLimit(req, limit)) return refuse();
    const read = await readBoundedBody(req, limit);
    if (read === null) return refuse();
    // The buffer rather than the view, because TypeScript's BodyInit does not
    // admit a Uint8Array; readBoundedBody allocates it at exactly the body's
    // length, so the two describe the same bytes.
    body = read.byteLength > 0 ? (read.buffer as ArrayBuffer) : undefined;
  }

  let upstream: Response;
  try {
    upstream = await fetch(`${orchestratorUrl()}${upstreamPath}`, {
      method: req.method,
      headers: forwardedHeaders(req),
      body,
      cache: 'no-store',
      // Never follow: a redirect could only point at something inside, and
      // following it would make this handler fetch a URL it did not choose.
      redirect: 'manual',
      // The caller's signal and nothing else — see the docblock on why there
      // is no ceiling of our own here.
      signal: req.signal,
    });
  } catch (err) {
    // The client hung up: there is nobody left to read an answer, and 499 is
    // what this codebase records for it (app/api/chat/route.ts).
    if (req.signal.aborted) return new Response(null, { status: 499 });
    logProxyError({
      route: '/v1',
      status: null,
      category: 'ORCHESTRATOR_UNAVAILABLE',
      message: describeThrown(err),
      durationMs: Date.now() - startedAt,
      retryable: true,
    });
    // 502 is not in CONTRACT §9's table, so it is not an answer this API is
    // allowed to give. `model_unavailable` is: retryable, 503, and it carries
    // the Retry-After a 503 owes.
    return edgeError(
      'model_unavailable',
      'The service is temporarily unavailable. Please retry.',
      { retryAfter: 30 },
    );
  }

  if (REDIRECT_STATUSES.has(upstream.status)) {
    // No /v1 endpoint redirects. If one ever does, its Location names a host
    // on the inside, and relaying it would publish exactly what CONTRACT §9
    // forbids — so this is a refusal with a log line, not a passthrough.
    logProxyError({
      route: '/v1',
      status: upstream.status,
      category: 'APPLICATION_ERROR',
      message: 'the orchestrator answered /v1 with a redirect',
      requestId: upstream.headers.get('x-request-id'),
      durationMs: Date.now() - startedAt,
      retryable: false,
    });
    return edgeError('internal_error', 'Something went wrong on our side.');
  }

  const sse = isEventStream(upstream);
  const headers = relayedHeaders(upstream, sse);
  // The body is PASSED, never read: a streaming response reaches the caller
  // token by token, and a JSON one crosses without being buffered either.
  const passthrough = NULL_BODY_STATUSES.has(upstream.status)
    ? null
    : upstream.body;
  return new Response(passthrough, { status: upstream.status, headers });
}

type Ctx = { params: Promise<{ path?: string[] }> };

export async function GET(req: Request, ctx: Ctx): Promise<Response> {
  return handle(req, ctx);
}

export async function POST(req: Request, ctx: Ctx): Promise<Response> {
  return handle(req, ctx);
}

/**
 * Preflight is forwarded rather than answered here.
 *
 * Next answers OPTIONS itself when a route does not export it — with an
 * `Allow` header and no CORS headers at all, which fails every browser
 * preflight. The orchestrator owns the answer (CONTRACT §3: 204, the echoed
 * origin, `GET, POST, OPTIONS`, `authorization, content-type,
 * idempotency-key`, `Max-Age: 600`), so the request goes there and the answer
 * comes back through the same allowlist as every other response.
 */
export async function OPTIONS(req: Request, ctx: Ctx): Promise<Response> {
  return handle(req, ctx);
}
