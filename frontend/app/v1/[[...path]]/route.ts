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
 * TWO WAYS THERE (2026-09-13, no-timeout design revision 2).
 *
 *  · THROUGH THE v1-GATEWAY, when `V1_GATEWAY_URL` is set. Compose leaves it
 *    blank unless the operator sets it in .env together with the orchestrator's
 *    PUBLIC_API_GATEWAY_PEERS (release review 2026-09-14: a fixed value put all
 *    public /v1 traffic on the gateway before the orchestrator trusted it).
 *    The gateway (gateway/, a separate sha-pinned container) holds the client
 *    connection across orchestrator restarts: it retries a refused connect for
 *    up to 110 s before the first byte, and after the first byte it heartbeats
 *    and re-attaches to the same generation for up to 30 min. A frontend deploy
 *    cannot help with that — the frontend is recreated by the same deploys
 *    (22 of 22 measured) — so this route only relays to it. If the gateway
 *    itself refuses the connection (not deployed yet, or being recreated
 *    because gateway/ changed), the request falls back to the direct path
 *    below, which is what production ran before the gateway existed.
 *  · DIRECT to the orchestrator, when it is unset or the gateway is down.
 *    Before any byte of the answer exists, a refused connect is retried every
 *    2 s for up to 110 s — long enough to cover the ≤97 s orchestrator restart
 *    window, short enough that the final 503 still reaches the caller before
 *    Cloudflare's 125 s first-byte wall. A failure AFTER the request reached
 *    the orchestrator is a 503; on a generation POST without an
 *    Idempotency-Key it carries `x-should-retry: false`, because re-sending it
 *    could start a second, separately billed generation.
 *
 * NO DURATION TIMER, AND ONE SILENCE LIMIT (2026-09-13, revised 2026-09-14).
 * Global fetch runs on undici's default dispatcher, whose headersTimeout and
 * bodyTimeout are both 300 s. /v1 has no wall clock anywhere (CONTRACT §10:
 * the orchestrator sends a byte at least every 15 s, and liveness is judged
 * from engine evidence, never from a clock), so every /v1 fetch uses a
 * dispatcher of its own. On the GATEWAY hop both timers are 0: the gateway
 * pings a client it is re-attaching for, and has its own 300 s upstream
 * watchdog (`v1Dispatcher`). On the DIRECT path the design keeps a 300 s
 * silence limit as a defence (edge_100s, item 6): with the 15 s rule, 300 s
 * with no byte means an orchestrator whose event loop is stuck while its TCP
 * socket stays up, which keepalive probes cannot see — and the documented
 * clients wait forever by design, so without it nothing would end such a
 * request (`v1DirectDispatcher`). Nothing else in the app uses these
 * dispatchers, so the cookie routes keep their own ceilings.
 *
 * "Faithfully" is still these five things:
 *
 * 1. THE CREDENTIAL CROSSES. No proxy in this codebase forwarded
 *    `Authorization` at all (confirmed finding, the wave-1 audit) — every one
 *    of them was built for the cookie world. A `/v1` request whose bearer
 *    token is dropped on this hop is a 401 for a key that is perfectly valid,
 *    so `authorization` and `idempotency-key` go upstream byte-for-byte.
 * 2. THE COOKIE DOES NOT. CONTRACT §1: "/v1 reads exactly one credential — the
 *    Authorization header — and ignores Cookie entirely", because a surface
 *    that accepted both would be a confused deputy. The orchestrator ignores
 *    it anyway; this edge does not send it, and `Access-Control-Allow-
 *    Credentials` is never relayed back either (§3).
 * 3. THE STREAM IS NOT BUFFERED — in either direction. `new Response(
 *    upstream.body, …)` hands the answer to the client as it arrives, with
 *    `X-Accel-Buffering: no` re-asserted. Large request bodies (audio, file
 *    uploads and parts) stream upstream too: buffering costs 310 MiB of RSS
 *    per 64 MiB body, streaming 8 × 64 MiB plateaued at 632 MiB (Files design
 *    §12, measured).
 * 4. THE STATUS AND THE RETRY HEADERS SURVIVE. The status is relayed exactly
 *    and the headers a client retries on (`Retry-After`, `x-should-retry`,
 *    the RateLimit family, `X-Request-Id`) are on the allowlist below.
 * 5. NOTHING ABOUT THE INSIDE LEAKS. CONTRACT §9 forbids an internal hostname,
 *    a private IP or a path in any response body. The failures this handler
 *    produces on its own carry fixed sentences; a 3xx (which could only carry
 *    an internal `Location`) is refused; the internal attach protocol's
 *    `X-TechSara-*` headers and `: ts-seq=N` frames never cross this hop.
 *
 * WHAT THIS HANDLER DELIBERATELY DOES NOT DO:
 *
 * · it adds no credential of its own. A request with no `Authorization`
 *   reaches the orchestrator with no `Authorization` and is told 401 by the
 *   thing that is entitled to decide that;
 * · it has no ceiling on how long an answer takes. The client's signal is
 *   forwarded, so an abandoned request still stops costing something;
 * · it does not parse, validate or rewrite the body. Validation is stated once,
 *   server-side (§8). A `multipart/form-data` upload crosses with its
 *   Content-Type — and so its boundary — untouched.
 */

import {
  declaredBodyOverLimit,
  isBodyTooLarge,
  orchestratorUrl,
  trustedClientIp,
  trustedForwardedProto,
  BodyTooLargeError,
} from '@/lib/proxy';
import { logProxyError } from '@/lib/serverLog';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

/* ----------------------------------------------------------- the caps -- */

/** CONTRACT §12: 1 MiB of body, refused before parsing. */
export const DEFAULT_PUBLIC_API_BODY_BYTES = 1024 * 1024;

/**
 * 20 MiB for the two generating routes, since they accept inline images
 * (2026-09-13, owner request: every model TechSara runs on /v1). The TEXT in
 * such a request still obeys the 1 MiB rule — that is the orchestrator's
 * check, made after parsing — so this number only bounds what images add.
 */
export const DEFAULT_PUBLIC_API_MEDIA_BODY_BYTES = 20 * 1024 * 1024;

/**
 * 90 MiB for a transcription upload: an 89 MiB audio file plus a MiB for the
 * multipart boundaries and form fields (2026-09-13, no-timeout design
 * sidecars_and_audio: audio of any length streams to disk in the
 * orchestrator, so the only reason left for a per-request cap is Cloudflare's
 * 100 MB request-body wall). Longer recordings go through /v1/uploads and a
 * `file_id`. Equal to the orchestrator's PUBLIC_API_MAX_AUDIO_BODY_BYTES.
 */
export const DEFAULT_PUBLIC_API_AUDIO_BODY_BYTES = 90 * 1024 * 1024;

/**
 * 8 MiB for embeddings and rerank (2026-09-13, no-timeout design: 2,048
 * inputs and 1,000 documents per request are request SHAPE, not usage, and
 * 2,048 inputs of real text do not fit in 1 MiB). Read from the same
 * variable as the orchestrator and the gateway, PUBLIC_API_MAX_POOLING_BODY_BYTES.
 */
export const DEFAULT_PUBLIC_API_POOLING_BODY_BYTES = 8 * 1024 * 1024;

/** Files design §8: one 64 MiB file part plus 1 MiB of multipart framing. */
export const DEFAULT_PUBLIC_API_FILES_MAX_BODY_BYTES = 65 * 1024 * 1024;

/** Files design §8: a raw `PUT /v1/uploads/{id}/parts/{n}` body. */
export const DEFAULT_PUBLIC_API_FILES_PART_MAX_BYTES = 64 * 1024 * 1024;

function envBytes(name: string, fallback: number): number {
  const raw = Number(process.env[name] ?? '');
  return Number.isFinite(raw) && raw > 0 ? Math.floor(raw) : fallback;
}

/**
 * The JSON body cap, read at call time from the SAME environment variable the
 * orchestrator reads (`PUBLIC_API_MAX_BODY_BYTES`), so a deployment that
 * tightens or loosens the limit moves both halves at once. The edge is
 * deliberately not tighter: a cap here below the server's would refuse
 * requests the API documents as acceptable.
 */
export function publicApiBodyBytes(): number {
  return envBytes('PUBLIC_API_MAX_BODY_BYTES', DEFAULT_PUBLIC_API_BODY_BYTES);
}

const UPLOAD_PARTS = /^uploads\/[^/]+\/parts$/;
const UPLOAD_PART_PUT = /^uploads\/[^/]+\/parts\/[^/]+$/;

/**
 * The cap for ONE request, by method and path (2026-09-13). Each size is read
 * from the environment variable the orchestrator reads for the same route, so
 * the edge and the server refuse at the same byte:
 *
 *   POST /v1/audio/transcriptions          PUBLIC_API_MAX_AUDIO_BODY_BYTES    90 MiB
 *   POST /v1/responses, /chat/completions  PUBLIC_API_MAX_MEDIA_BODY_BYTES    20 MiB
 *   POST /v1/embeddings, /v1/rerank        PUBLIC_API_MAX_POOLING_BODY_BYTES   8 MiB
 *   POST /v1/files, /uploads/{id}/parts    PUBLIC_API_FILES_MAX_BODY_BYTES    65 MiB
 *   PUT  /v1/uploads/{id}/parts/{n}        PUBLIC_API_FILES_PART_MAX_BYTES    64 MiB
 *   everything else                        PUBLIC_API_MAX_BODY_BYTES           1 MiB
 *
 * Exact paths only. `/v1/responses/{id}/cancel` has no body worth 20 MiB, and
 * a prefix match would hand every future route under a generous path the
 * generous cap without anybody deciding it should have one.
 */
export function publicApiBodyBytesFor(parts: string[], method = 'POST'): number {
  const path = parts.join('/');
  if (path === 'audio/transcriptions') {
    return envBytes('PUBLIC_API_MAX_AUDIO_BODY_BYTES', DEFAULT_PUBLIC_API_AUDIO_BODY_BYTES);
  }
  if (path === 'responses' || path === 'chat/completions') {
    return envBytes('PUBLIC_API_MAX_MEDIA_BODY_BYTES', DEFAULT_PUBLIC_API_MEDIA_BODY_BYTES);
  }
  if (path === 'embeddings' || path === 'rerank') {
    return envBytes('PUBLIC_API_MAX_POOLING_BODY_BYTES', DEFAULT_PUBLIC_API_POOLING_BODY_BYTES);
  }
  if (method === 'POST' && (path === 'files' || UPLOAD_PARTS.test(path))) {
    return envBytes('PUBLIC_API_FILES_MAX_BODY_BYTES', DEFAULT_PUBLIC_API_FILES_MAX_BODY_BYTES);
  }
  if (method === 'PUT' && UPLOAD_PART_PUT.test(path)) {
    return envBytes('PUBLIC_API_FILES_PART_MAX_BYTES', DEFAULT_PUBLIC_API_FILES_PART_MAX_BYTES);
  }
  return publicApiBodyBytes();
}

export type BodyMode = 'none' | 'buffer' | 'stream';

export interface BodyRule {
  cap: number;
  mode: BodyMode;
  /** A raw part PUT must declare its length (Files design §2.12). */
  requireLength: boolean;
}

/**
 * How ONE request's body crosses this hop.
 *
 *  · none — GET, HEAD and OPTIONS carry no body.
 *  · stream — audio, `POST /v1/files` and upload parts: piped upstream as they
 *    arrive, counted against the cap on the way. Buffering them is the RSS
 *    flood the Files design measured, and holding an upload unread while we
 *    retried would trip Cloudflare's 30 s write timeout anyway.
 *  · buffer — every JSON route: read whole (bounded) first, so a refused
 *    connect can be retried with the same bytes for up to 110 s.
 */
export function publicApiBodyRuleFor(method: string, parts: string[]): BodyRule {
  const upper = method.toUpperCase();
  if (upper === 'GET' || upper === 'HEAD' || upper === 'OPTIONS') {
    return { cap: 0, mode: 'none', requireLength: false };
  }
  const path = parts.join('/');
  const cap = publicApiBodyBytesFor(parts, upper);
  if (upper === 'POST' && path === 'audio/transcriptions') {
    return { cap, mode: 'stream', requireLength: false };
  }
  if (upper === 'POST' && (path === 'files' || UPLOAD_PARTS.test(path))) {
    return { cap, mode: 'stream', requireLength: false };
  }
  if (upper === 'PUT' && UPLOAD_PART_PUT.test(path)) {
    return { cap, mode: 'stream', requireLength: true };
  }
  return { cap, mode: 'buffer', requireLength: false };
}

/* -------------------------------------------------------- the headers -- */

/**
 * What goes UPSTREAM, named rather than filtered.
 *
 * `cookie` is absent on purpose (CONTRACT §1) and so is every client-supplied
 * forwarding header and every `x-techsara-*` name: those belong to the internal
 * attach protocol, and a caller who could send `X-TechSara-Attempt` could try
 * to attach to someone else's generation. The orchestrator honours them only
 * from trusted proxies anyway; this edge does not send what it cannot vouch for.
 *
 * gateway/lib/headers.cjs carries the same list, and gateway/test/parity.test.cjs
 * parses this array on every run — the two edges must agree.
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
  // Implicit attach (no-timeout design §B, 2026-09-13): openai-python and
  // openai-node 6.49.0 / 7.15.0 send it on every retry (measured). A retry
  // with count ≥1 and a byte-identical body re-joins the generation the
  // dropped connection left running, instead of starting a second one.
  'x-stainless-retry-count',
  // Files design §12.4: part digests, and the conditional and range reads of
  // `GET /v1/files/{id}/content`.
  'content-digest',
  'x-part-sha256',
  'range',
  'if-none-match',
] as const;

/**
 * What comes BACK, also named rather than filtered.
 *
 * Four names are missing on purpose:
 *   · `set-cookie` — /v1 is cookie-blind in both directions;
 *   · `access-control-allow-credentials` — CONTRACT §3 says it is never sent,
 *     and "never" has to include "never relayed";
 *   · `content-length` and `content-encoding` — undici decodes the upstream
 *     body, so both describe bytes that may no longer exist by the time the
 *     response leaves here. The one exception is a byte download, whose
 *     length is re-set explicitly (`BYTE_DOWNLOAD`, Files finding #15);
 *   · `location` — see `REDIRECT_STATUSES` below.
 */
export const RESPONSE_HEADER_ALLOWLIST = [
  'content-type',
  'retry-after',
  'ratelimit',
  'ratelimit-policy',
  'x-request-id',
  // RFC 6750 §3: a 401/403 on a bearer-token API says WHY in this header.
  'www-authenticate',
  'cache-control',
  'vary',
  'access-control-allow-origin',
  'access-control-allow-methods',
  'access-control-allow-headers',
  'access-control-expose-headers',
  'access-control-max-age',
  // Both SDKs read it BEFORE their own retry table (no-timeout design,
  // sdk_and_docs): `false` on a failure whose retry could double-bill.
  'x-should-retry',
  // Files design §12.5: the download and resume headers.
  'content-disposition',
  'content-range',
  'accept-ranges',
  'etag',
  'content-security-policy',
] as const;

/** Allowlisted by prefix: the x-ratelimit-* family has no fixed member list. */
export const RESPONSE_HEADER_PREFIXES = ['x-ratelimit-'] as const;

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
 * "3xx", because 304 is in that range and is not a redirect.
 */
const REDIRECT_STATUSES = new Set([301, 302, 303, 307, 308]);

/** Byte downloads, whose Content-Length is re-set on the way out (Files §12.5). */
const BYTE_DOWNLOAD = /^files\/[^/]+\/(content|derived\/[^/]+)$/;

/* --------------------------------------------------------- the errors -- */

/**
 * The only failures this edge can raise by itself, with the status and wire
 * `type` app/publicapi/errors.py gives them. A CLOSED table: the error
 * vocabulary of CONTRACT §9 is what every SDK's retry logic switches on.
 * `invalid_request_error` is the 411 a raw part PUT without Content-Length
 * gets — the same code, status and param the orchestrator's
 * `files.wire.length_required()` sends.
 */
const EDGE_ERRORS = {
  request_too_large: { status: 413, type: 'invalid_request_error' },
  model_unavailable: { status: 503, type: 'service_unavailable_error' },
  internal_error: { status: 500, type: 'server_error' },
  invalid_request_error: { status: 411, type: 'invalid_request_error' },
} as const;

type EdgeErrorCode = keyof typeof EDGE_ERRORS;

/**
 * The envelope of CONTRACT §9, five keys, `param` and `request_id` nullable.
 * `request_id` is null and no `X-Request-Id` is set, because this refusal
 * never reached the orchestrator and so has no id in any system.
 */
function edgeError(
  code: EdgeErrorCode,
  message: string,
  opts: { retryAfter?: number; param?: string; shouldRetry?: boolean } = {},
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
  if (opts.shouldRetry !== undefined) headers['x-should-retry'] = String(opts.shouldRetry);
  return new Response(
    JSON.stringify({
      error: { message, type: spec.type, code, param: opts.param ?? null, request_id: null },
    }),
    { status: spec.status, headers },
  );
}

const UNAVAILABLE_SENTENCE = 'The service is temporarily unavailable. Please retry.';

/**
 * undici reports the real cause of a transport failure in a nested `cause`
 * chain. The MESSAGE is deliberately not taken: it quotes the URL that failed,
 * and this line is exactly the kind of thing that ends up pasted into a ticket.
 */
export function transportErrorCode(err: unknown): string | null {
  let cur: unknown = err;
  for (let depth = 0; cur && depth < 6; depth += 1) {
    const code = (cur as { code?: unknown }).code;
    if (typeof code === 'string') return code;
    // An AggregateError (happy eyeballs: IPv4 and IPv6 both refused) keeps
    // its codes on the members.
    const members = (cur as { errors?: unknown }).errors;
    if (Array.isArray(members)) {
      for (const member of members) {
        const inner = (member as { code?: unknown })?.code;
        if (typeof inner === 'string') return inner;
      }
    }
    cur = (cur as { cause?: unknown }).cause;
  }
  return null;
}

function describeThrown(err: unknown): string {
  const code = transportErrorCode(err);
  const name = (err as { name?: string })?.name ?? 'Error';
  return code ? `${name}: ${code}` : name || 'fetch failed';
}

/**
 * Failures that prove the request never reached the server: nothing was
 * listening, the name did not resolve (a container being recreated drops out
 * of Docker's DNS), or the TCP handshake never finished. Only these are
 * retried before the first byte — a reset or "other side closed" may come
 * after the orchestrator read the request, and re-sending THAT could launch a
 * generation twice.
 */
const CONNECT_PHASE_CODES = new Set([
  'ECONNREFUSED',
  'ENOTFOUND',
  'EAI_AGAIN',
  'EHOSTUNREACH',
  'ENETUNREACH',
  'EHOSTDOWN',
  'UND_ERR_CONNECT_TIMEOUT',
]);

export function isConnectPhaseError(err: unknown): boolean {
  const code = transportErrorCode(err);
  return code !== null && CONNECT_PHASE_CODES.has(code);
}

/* ------------------------------------------------------ the transport -- */

/**
 * The /v1 dispatcher's options (2026-09-13).
 *
 *  · headersTimeout 0, bodyTimeout 0 — undici's defaults are 300 s each, and
 *    /v1 has no wall clock (see the docblock). A dead peer is still found: the
 *    orchestrator writes a byte at least every 15 s, and undici turns TCP
 *    keepalive on for every socket it opens (60 s initial delay).
 *  · pipelining 0 — a fresh connection per request. uvicorn closes an idle
 *    keep-alive socket after 5 s; reusing one in the same instant fails the
 *    request with "other side closed" AFTER it may have been read, which is
 *    exactly the failure that cannot be retried safely. A new TCP handshake
 *    on the Docker bridge costs well under a millisecond.
 */
export const V1_DISPATCHER_OPTIONS = Object.freeze({
  headersTimeout: 0,
  bodyTimeout: 0,
  pipelining: 0,
});

/** The options a /v1 dispatcher is built from. */
export interface V1DispatcherOptions {
  headersTimeout: number;
  bodyTimeout: number;
  pipelining: number;
}

/**
 * The direct path's silence limit, `V1_EDGE_UPSTREAM_SILENCE_S` (default 300;
 * 0 turns it off). 2026-09-14, review: the edge first ran the direct path with
 * no timer at all, so a wedged but connected orchestrator hung a documented
 * client (Timeout(None), 2147483647 ms) with nothing to end it.
 */
export const DEFAULT_V1_EDGE_UPSTREAM_SILENCE_S = 300;

/**
 * The direct path's dispatcher options: both undici timers at the silence
 * limit. headersTimeout is the wait for the answer's first byte, due within
 * 15 s; bodyTimeout the longest gap inside the answer, also 15 s.
 *
 * A slow streamed upload (audio, file, parts) is not counted against either:
 * measured 2026-09-14 with a 1 s headersTimeout, a body sent in chunks up to
 * 1.5 s apart over 5.4 s went through on Node 20.20.2 (undici 6.24.1, the
 * image's), Node 22.23.2 (undici 6.28.0) and npm undici 7.29.1 alike, and a
 * test below holds it (a 2.4 s upload under a 1 s limit).
 */
export function v1DirectDispatcherOptions(): V1DispatcherOptions {
  const silenceMs = Math.round(envSeconds('V1_EDGE_UPSTREAM_SILENCE_S', DEFAULT_V1_EDGE_UPSTREAM_SILENCE_S) * 1000);
  return { headersTimeout: silenceMs, bodyTimeout: silenceMs, pipelining: 0 };
}

type DispatcherCtor = new (options: V1DispatcherOptions) => object;
const dispatcherMemo = new Map<string, object | undefined>();
let agentMissingLogged = false;

/**
 * The one dispatcher every /v1 fetch uses.
 *
 * WHY NOT `import { Agent } from 'undici'`: the npm package is not a
 * dependency here, and an Agent from a DIFFERENT undici than the one inside
 * Node's global fetch is not guaranteed to speak its dispatcher protocol.
 * Node's own undici registers its global dispatcher under the cross-version
 * symbol `undici.globalDispatcher.1` the first time fetch is touched; that
 * object's class IS the Agent global fetch speaks to. `new Headers()` forces
 * that lazy load in case nothing in this process has fetched yet.
 *
 * If the symbol is ever missing (a future Node), this returns undefined and
 * logs once: /v1 then runs on the default dispatcher and its 300 s timers,
 * which is today's behaviour rather than a crash.
 */
export function v1Dispatcher(): object | undefined {
  return dispatcherFor(V1_DISPATCHER_OPTIONS);
}

/** The direct path's dispatcher: the gateway hop's, plus the silence limit. */
export function v1DirectDispatcher(): object | undefined {
  return dispatcherFor(v1DirectDispatcherOptions());
}

function dispatcherFor(options: V1DispatcherOptions): object | undefined {
  const key = `${options.headersTimeout}/${options.bodyTimeout}/${options.pipelining}`;
  if (dispatcherMemo.has(key)) return dispatcherMemo.get(key);
  let value: object | undefined;
  try {
    void new Headers();
    const global = (globalThis as Record<symbol, unknown>)[Symbol.for('undici.globalDispatcher.1')];
    let proto = global ? Object.getPrototypeOf(global) : null;
    while (proto && proto.constructor?.name !== 'Agent') proto = Object.getPrototypeOf(proto);
    const Agent = proto?.constructor as DispatcherCtor | undefined;
    if (typeof Agent === 'function') value = new Agent({ ...options });
  } catch {
    value = undefined;
  }
  if (!value && !agentMissingLogged) {
    agentMissingLogged = true;
    logProxyError({
      route: '/v1',
      status: null,
      category: 'ORCHESTRATOR_UNAVAILABLE',
      message: 'no undici Agent found; /v1 runs on the default 300 s fetch timers',
      retryable: true,
    });
  }
  dispatcherMemo.set(key, value);
  return value;
}

/** For tests: forget the memoised dispatchers. */
export function resetV1DispatcherForTests(): void {
  dispatcherMemo.clear();
  agentMissingLogged = false;
}

/**
 * The v1-gateway, when this deployment has one. Anything that is not an
 * absolute http(s) URL is ignored (with the direct path used), because a typo
 * here must not turn every API call into a 500.
 */
export function v1GatewayUrl(): string | null {
  const raw = (process.env.V1_GATEWAY_URL ?? '').trim();
  if (!raw) return null;
  try {
    const url = new URL(raw);
    if (url.protocol !== 'http:' && url.protocol !== 'https:') return null;
    return `${url.origin}${url.pathname.replace(/\/+$/, '')}`;
  } catch {
    return null;
  }
}

function envSeconds(name: string, fallback: number): number {
  const raw = (process.env[name] ?? '').trim();
  if (raw === '') return fallback;
  const value = Number(raw);
  return Number.isFinite(value) && value >= 0 ? value : fallback;
}

/**
 * How long the direct path keeps retrying a refused connect before the first
 * byte: 110 s (no-timeout design, timers "Next /v1 route fetch"). The
 * orchestrator's restart window measured ≤97 s; Cloudflare waits 125 s for a
 * first byte. The variables exist for tests and for an operator who has
 * measured a different restart; production leaves them unset.
 */
export function connectRetryBudgetMs(): number {
  return envSeconds('V1_EDGE_CONNECT_RETRY_S', 110) * 1000;
}

export function connectRetryIntervalMs(): number {
  return Math.max(1, envSeconds('V1_EDGE_CONNECT_RETRY_INTERVAL_S', 2) * 1000);
}

/* ------------------------------------------------------- the body source -- */

/** How many streamed bytes are kept to re-send after a refused connect. */
const REPLAY_BYTES = 1024 * 1024;

/**
 * A client body that can be offered to more than one upstream attempt.
 *
 * WHY (2026-09-13, measured): undici pulls exactly one chunk of a streamed
 * body before it knows whether the connection opened, so a refused connect to
 * the gateway has already taken bytes off the caller's stream. The chunks
 * pulled so far are kept (up to 1 MiB — a connect failure happens after one
 * pull) and replayed at the head of the next attempt. A connect-phase failure
 * proves none of them reached a server, so replaying them is exact.
 *
 * It also owns the two other things a streamed body needs: the byte cap,
 * enforced as bytes pass; and draining (Files design §12.3) — when the
 * upstream answers before the body is finished, the rest is read and thrown
 * away rather than cancelled, because cancelling makes openai-node report
 * "Connection error" and retry the part three times instead of surfacing the
 * 401/404/413 it was sent.
 */
export class BodySource {
  private readonly reader: ReadableStreamDefaultReader<Uint8Array> | null;
  private replay: Uint8Array[] = [];
  private replayBytes = 0;
  private replayable = true;
  private total = 0;
  private finished = false;
  private chain: Promise<unknown> = Promise.resolve();
  private draining = false;

  constructor(body: ReadableStream<Uint8Array> | null, private readonly cap: number) {
    this.reader = body ? body.getReader() : null;
    if (!body) this.finished = true;
  }

  get done(): boolean {
    return this.finished;
  }

  get canReplay(): boolean {
    return this.replayable;
  }

  /** Serialised reads: an attempt's pending pull and a drain never interleave. */
  private read(): Promise<ReadableStreamReadResult<Uint8Array>> {
    const next = this.chain.then(async () => {
      if (this.finished || !this.reader) return { done: true, value: undefined } as const;
      const result = await this.reader.read();
      if (result.done) {
        this.finished = true;
        return result;
      }
      this.total += result.value.byteLength;
      if (this.total > this.cap) {
        this.finished = true;
        await this.reader.cancel().catch(() => undefined);
        throw new BodyTooLargeError(this.cap);
      }
      return result;
    });
    this.chain = next.catch(() => undefined);
    return next;
  }

  /**
   * A fresh stream for one upstream attempt: the replayed chunks first, then
   * the rest of the caller's body. It waits for any read an earlier attempt
   * still has in flight, so a chunk that attempt pulled lands in the replay
   * list before this one looks at it.
   */
  attempt(): ReadableStream<Uint8Array> {
    let index = 0;
    return new ReadableStream<Uint8Array>(
      {
        pull: async (controller) => {
          try {
            await this.chain;
            if (this.replayable && index < this.replay.length) {
              controller.enqueue(this.replay[index]);
              index += 1;
              return;
            }
            if (this.draining) {
              controller.close();
              return;
            }
            const { done, value } = await this.read();
            if (done || !value) {
              controller.close();
              return;
            }
            if (this.replayable) {
              this.replayBytes += value.byteLength;
              if (this.replayBytes > REPLAY_BYTES) {
                this.replayable = false;
                this.replay = [];
              } else {
                this.replay.push(value);
                index += 1;
              }
            }
            controller.enqueue(value);
          } catch (err) {
            // The attempt may already be gone (its fetch failed): the chunk is
            // in the replay list, and there is nobody to tell.
            try {
              controller.error(err);
            } catch {
              /* already closed */
            }
          }
        },
        // Cancelling THIS attempt never cancels the caller's body: the next
        // attempt, or the drain, still needs it.
        cancel: () => undefined,
      },
      { highWaterMark: 0 },
    );
  }

  /** The upstream accepted the connection: nothing will be replayed now. */
  settle(): void {
    this.replayable = false;
    this.replay = [];
  }

  /** Everything still to come, bounded, joined — for the buffered direct path. */
  async readAll(): Promise<Uint8Array | null> {
    const chunks = [...this.replay];
    this.settle();
    try {
      for (;;) {
        const { done, value } = await this.read();
        if (done || !value) break;
        chunks.push(value);
      }
    } catch (err) {
      if (isBodyTooLarge(err)) return null;
      throw err;
    }
    const size = chunks.reduce((n, c) => n + c.byteLength, 0);
    const joined = new Uint8Array(size);
    let offset = 0;
    for (const chunk of chunks) {
      joined.set(chunk, offset);
      offset += chunk.byteLength;
    }
    return joined;
  }

  /** Read and discard the rest of the body, up to the cap, in the background. */
  drain(): void {
    if (this.draining || this.finished) return;
    this.draining = true;
    void (async () => {
      try {
        for (;;) {
          const { done } = await this.read();
          if (done) return;
        }
      } catch {
        // Over the cap (the reader is cancelled there) or the caller left.
      }
    })();
  }
}

/* ---------------------------------------------------------- the relay -- */

/** The upstream path for a `/v1/…` request: the same path, re-encoded. */
export function upstreamPathFor(parts: string[], search: string): string {
  // Next has percent-decoded the segments; re-encode so nothing (a response id
  // with a slash in it, say) can smuggle a separator into the upstream URL.
  const tail = parts.map(encodeURIComponent).join('/');
  return `/v1${tail ? `/${tail}` : ''}${search}`;
}

function forwardedHeaders(req: Request, viaGateway: boolean): Record<string, string> {
  const out: Record<string, string> = {};
  for (const name of REQUEST_HEADER_ALLOWLIST) {
    const value = req.headers.get(name);
    if (value) out[name] = value;
  }
  // The caller's address, from the one header this deployment is configured to
  // trust and never from one the caller chose (lib/proxy.ts `trustedClientIp`).
  // A project may carry an `ip_allowlist`, and an allowlist fed a forgeable
  // address is not an allowlist.
  const clientIp = trustedClientIp(req);
  if (clientIp) {
    out['x-forwarded-for'] = clientIp;
    // The gateway derives the caller's address from the SAME configured header
    // (TRUSTED_CLIENT_IP_HEADER, lib/headers.cjs), so it is re-stated under that
    // name with the value already validated here. Without it every LAN call
    // through the gateway would reach the orchestrator addressed as the gateway.
    const trustedName = (process.env.TRUSTED_CLIENT_IP_HEADER ?? '').trim().toLowerCase();
    if (viaGateway && trustedName && /^[a-z0-9-]+$/.test(trustedName)) out[trustedName] = clientIp;
  }
  const proto = trustedForwardedProto();
  if (proto) out['x-forwarded-proto'] = proto;
  return out;
}

function relayedHeaders(upstream: Response, sse: boolean, keepLength: boolean): Headers {
  const out = new Headers();
  upstream.headers.forEach((value, name) => {
    const key = name.toLowerCase();
    if (key.startsWith('x-techsara-')) return;
    const allowed =
      (RESPONSE_HEADER_ALLOWLIST as readonly string[]).includes(key) ||
      RESPONSE_HEADER_PREFIXES.some((prefix) => key.startsWith(prefix));
    if (allowed) out.set(key, value);
  });
  if (sse) for (const [key, value] of Object.entries(SSE_HEADERS)) out.set(key, value);
  if (!out.has('content-type')) out.set('content-type', 'application/json');
  // An API answer is one project's data and is never revalidated by anything
  // between here and the caller.
  if (!out.has('cache-control')) out.set('cache-control', 'no-store');
  // Files design finding #15: undici does not hand content-length through on a
  // streamed body, and without it SDK progress, If-None-Match and Range size
  // checks break. Byte downloads are identity-encoded by the orchestrator, so
  // the upstream length describes exactly the bytes relayed.
  if (keepLength && !upstream.headers.get('content-encoding')) {
    const length = upstream.headers.get('content-length');
    if (length && /^\d+$/.test(length)) out.set('content-length', length);
  }
  return out;
}

function isEventStream(upstream: Response): boolean {
  const type = upstream.headers.get('content-type') ?? '';
  return type.toLowerCase().split(';')[0].trim() === 'text/event-stream';
}

/**
 * Remove the internal `: ts-seq=N` comment frames from an SSE body.
 *
 * The orchestrator writes one after each data frame, but ONLY for a request
 * the gateway tagged, from a trusted peer — so on this hop there should be
 * none, and the gateway strips its own. This is the defensive copy (no-timeout
 * design T5): a misconfigured trusted-proxy list must not put the internal
 * sequence on a customer's wire, where openai-python's decoder would see a
 * comment it does not need and the attach protocol would be advertised.
 *
 * Line-oriented and streaming: a line is released the moment it cannot be a
 * ts-seq comment, so no event waits for the next one, and memory is bounded by
 * the prefix being examined, not by the frame. A comment that was a frame on
 * its own takes its terminating blank line with it.
 */
export function stripTsSeqComments(): TransformStream<Uint8Array, Uint8Array> {
  const decoder = new TextDecoder();
  const encoder = new TextEncoder();
  const isMarker = (line: string) => /^: ?ts-seq=\d+$/.test(line);
  /** Could `prefix` still grow into a marker line? */
  const couldBeMarker = (prefix: string) =>
    [': ts-seq=', ':ts-seq='].some((head) =>
      prefix.length <= head.length
        ? head.startsWith(prefix)
        : prefix.startsWith(head) && /^\d*$/.test(prefix.slice(head.length)),
    );

  // The start of the current line, held while it could still be a marker.
  let held = '';
  // The rest of the current line is known not to be a marker: copy it.
  let passing = false;
  // Nothing of the current frame has been emitted yet.
  let frameStart = true;
  // The frame so far was only removed markers, so its blank line goes too.
  let swallowBlank = false;
  // The previous character was a \r; `crEmitted` says whether it was written.
  let afterCR = false;
  let crEmitted = false;

  function push(text: string, controller: TransformStreamDefaultController<Uint8Array>) {
    let out = '';
    let i = 0;
    while (i < text.length) {
      const ch = text[i];
      if (afterCR) {
        afterCR = false;
        if (ch === '\n') {
          // The \n of a \r\n pair belongs to the line break already decided.
          if (crEmitted) out += '\n';
          i += 1;
          continue;
        }
      }
      if (ch === '\n' || ch === '\r') {
        let emitBreak = true;
        if (passing) {
          frameStart = false;
          swallowBlank = false;
        } else if (held === '') {
          // A blank line ends the frame.
          emitBreak = !swallowBlank;
          swallowBlank = false;
          frameStart = true;
        } else if (isMarker(held)) {
          emitBreak = false;
          if (frameStart) swallowBlank = true;
        } else {
          out += held;
          frameStart = false;
          swallowBlank = false;
        }
        if (emitBreak) out += ch;
        held = '';
        passing = false;
        afterCR = ch === '\r';
        crEmitted = afterCR && emitBreak;
        i += 1;
        continue;
      }
      if (passing) {
        let j = i;
        while (j < text.length && text[j] !== '\n' && text[j] !== '\r') j += 1;
        out += text.slice(i, j);
        i = j;
        continue;
      }
      held += ch;
      i += 1;
      if (!couldBeMarker(held)) {
        out += held;
        held = '';
        passing = true;
      }
    }
    if (out) controller.enqueue(encoder.encode(out));
  }

  return new TransformStream<Uint8Array, Uint8Array>({
    transform(chunk, controller) {
      push(decoder.decode(chunk, { stream: true }), controller);
    },
    flush(controller) {
      const tail = decoder.decode();
      if (tail) push(tail, controller);
      // A stream that ends mid-line: release what was held unless it is a marker.
      if (!passing && held && !isMarker(held)) controller.enqueue(encoder.encode(held));
    },
  });
}

function isGenerationPost(method: string, parts: string[]): boolean {
  const path = parts.join('/');
  return method === 'POST' && (path === 'responses' || path === 'chat/completions');
}

function sleep(ms: number, signal: AbortSignal): Promise<void> {
  return new Promise((resolve) => {
    if (signal.aborted) {
      resolve();
      return;
    }
    const timer = setTimeout(done, ms);
    function done() {
      clearTimeout(timer);
      signal.removeEventListener('abort', done);
      resolve();
    }
    signal.addEventListener('abort', done, { once: true });
  });
}

type Attempt =
  | { ok: true; upstream: Response }
  | { ok: false; err: unknown };

async function attemptFetch(url: string, init: RequestInit & { dispatcher?: object; duplex?: 'half' }): Promise<Attempt> {
  try {
    return { ok: true, upstream: await fetch(url, init) };
  } catch (err) {
    return { ok: false, err };
  }
}

async function handle(
  req: Request,
  ctx: { params: Promise<{ path?: string[] }> },
): Promise<Response> {
  const startedAt = Date.now();
  const { path } = await ctx.params;
  const parts = path ?? [];
  const { search } = new URL(req.url);
  const upstreamPath = upstreamPathFor(parts, search);
  const method = req.method.toUpperCase();
  const rule = publicApiBodyRuleFor(method, parts);
  const keyed = Boolean(req.headers.get('idempotency-key'));

  const refuseTooLarge = () =>
    edgeError('request_too_large', `The request body is larger than the ${rule.cap} byte limit.`);

  // Two refusals, because one is not enough: the caller's own declaration is
  // refused without touching the socket, and a body that declares nothing (or
  // lies about it) is measured as it arrives. Either way an oversized body
  // costs one quota-free 413 rather than a megabyte of someone else's lane.
  if (rule.mode !== 'none' && declaredBodyOverLimit(req, rule.cap)) return refuseTooLarge();
  if (rule.requireLength && req.headers.get('content-length') === null) {
    return edgeError('invalid_request_error', 'This endpoint requires a Content-Length header.', {
      param: 'Content-Length',
    });
  }

  const source = rule.mode === 'none' ? null : new BodySource(req.body, rule.cap);
  const log = (message: string, retryable: boolean) =>
    logProxyError({
      route: '/v1',
      status: null,
      category: 'ORCHESTRATOR_UNAVAILABLE',
      message,
      durationMs: Date.now() - startedAt,
      retryable,
    });

  let upstream: Response | undefined;

  // ---- 1. the gateway, when there is one --------------------------------
  const gateway = v1GatewayUrl();
  if (gateway) {
    const headers = forwardedHeaders(req, true);
    const declared = req.headers.get('content-length');
    if (source && declared !== null && /^\d+$/.test(declared)) headers['content-length'] = declared;
    const body = source && !source.done ? source.attempt() : undefined;
    const result = await attemptFetch(`${gateway}${upstreamPath}`, {
      method,
      headers,
      body,
      ...(body ? { duplex: 'half' as const } : {}),
      cache: 'no-store',
      redirect: 'manual',
      signal: req.signal,
      dispatcher: v1Dispatcher(),
    });
    if (result.ok) {
      upstream = result.upstream;
      source?.settle();
    } else if (req.signal.aborted) {
      return new Response(null, { status: 499 });
    } else if (isBodyTooLarge(result.err)) {
      return refuseTooLarge();
    } else if (isConnectPhaseError(result.err) && (!source || source.canReplay)) {
      // The gateway is not there. Nothing reached it, so the direct path below
      // gets the same request — the route as it ran before the gateway existed.
      log(`v1-gateway unreachable, relaying direct: ${describeThrown(result.err)}`, true);
    } else {
      // 2026-09-14, review: no `x-should-retry: false` here, keyed or not. A
      // request that reached the gateway was tagged, so its generation is
      // durable and an SDK retry of the identical body attaches to it (design
      // sdk_and_docs: the header is for the edge's post-send 503 only "when the
      // gateway is absent"). Telling the SDK to stop would orphan that run,
      // cancelled after the unkeyed grace with nobody to collect it.
      log(`v1-gateway failed after the request was sent: ${describeThrown(result.err)}`, false);
      return edgeError('model_unavailable', UNAVAILABLE_SENTENCE, { retryAfter: 30 });
    }
  }

  // ---- 2. direct to the orchestrator ------------------------------------
  if (!upstream) {
    let body: ArrayBuffer | ReadableStream<Uint8Array> | undefined;
    let buffered: ArrayBuffer | undefined;
    if (source && rule.mode === 'buffer') {
      let read: Uint8Array | null;
      try {
        read = await source.readAll();
      } catch {
        if (req.signal.aborted) return new Response(null, { status: 499 });
        throw new Error('the request body could not be read');
      }
      if (read === null) return refuseTooLarge();
      // The buffer rather than the view, because TypeScript's BodyInit does not
      // admit a Uint8Array. readAll allocates it at exactly the body's length.
      buffered = read.byteLength === 0 ? undefined : (read.buffer as ArrayBuffer);
    }

    const headers = forwardedHeaders(req, false);
    const dispatcher = v1DirectDispatcher();
    const retryBudget = connectRetryBudgetMs();
    const retryInterval = connectRetryIntervalMs();
    const deadline = Date.now() + retryBudget;
    let tries = 0;
    for (;;) {
      tries += 1;
      if (source && rule.mode === 'stream') {
        const declared = req.headers.get('content-length');
        if (declared !== null && /^\d+$/.test(declared)) headers['content-length'] = declared;
        body = source.done ? undefined : source.attempt();
      } else {
        body = buffered;
      }
      const result = await attemptFetch(`${orchestratorUrl()}${upstreamPath}`, {
        method,
        headers,
        body,
        ...(body instanceof ReadableStream ? { duplex: 'half' as const } : {}),
        cache: 'no-store',
        // Never follow: a redirect could only point at something inside.
        redirect: 'manual',
        // The caller's signal, and the silence limit of the direct dispatcher —
        // no ceiling on duration.
        signal: req.signal,
        dispatcher,
      });
      if (result.ok) {
        upstream = result.upstream;
        source?.settle();
        if (tries > 1) log(`orchestrator reachable again after ${tries} attempts`, true);
        break;
      }
      // The client hung up: nobody is left to read an answer, and 499 is what
      // this codebase records for it (app/api/chat/route.ts).
      if (req.signal.aborted) return new Response(null, { status: 499 });
      if (isBodyTooLarge(result.err)) return refuseTooLarge();
      const connectPhase = isConnectPhaseError(result.err);
      // Buffered bodies are re-sent whole. A streamed body is re-sent only if
      // nothing past the replay window was taken off the caller's stream; in
      // practice that means a refused connect, which is what this loop is for.
      const resendable = rule.mode !== 'stream' || (source?.canReplay ?? true);
      if (connectPhase && resendable && Date.now() + retryInterval <= deadline) {
        if (tries === 1) log(`retrying the connect for up to ${Math.round(retryBudget / 1000)} s: ${describeThrown(result.err)}`, true);
        await sleep(retryInterval, req.signal);
        if (req.signal.aborted) return new Response(null, { status: 499 });
        continue;
      }
      log(describeThrown(result.err), true);
      // 502 is not in CONTRACT §9's table, so it is not an answer this API is
      // allowed to give. `model_unavailable` is: 503 with the Retry-After a 503
      // owes. When the request may already have reached the orchestrator, a
      // generation POST without a key must not be re-sent by the SDK.
      return edgeError('model_unavailable', UNAVAILABLE_SENTENCE, {
        retryAfter: 30,
        ...(!connectPhase && isGenerationPost(method, parts) && !keyed ? { shouldRetry: false } : {}),
      });
    }
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
    await upstream.body?.cancel().catch(() => undefined);
    source?.drain();
    return edgeError('internal_error', 'Something went wrong on our side.');
  }

  // Files design §12.3: an answer before the body finished (a 401 on a part,
  // a 413) — keep reading the caller's body and discard it, so the SDK reads
  // the envelope instead of reporting a broken connection.
  if (source && !source.done && upstream.status >= 400) source.drain();

  const sse = isEventStream(upstream);
  const headers = relayedHeaders(upstream, sse, method === 'GET' && BYTE_DOWNLOAD.test(parts.join('/')));
  if (NULL_BODY_STATUSES.has(upstream.status) || !upstream.body) {
    await upstream.body?.cancel().catch(() => undefined);
    source?.drain();
    return new Response(null, { status: upstream.status, headers });
  }
  // The body is PASSED, never read: a streaming response reaches the caller
  // token by token, and a JSON one crosses without being buffered either.
  let relayed: ReadableStream<Uint8Array> = upstream.body;
  if (sse) relayed = relayed.pipeThrough(stripTsSeqComments());
  if (source && !source.done) {
    // Whatever the upstream did not read by the time its answer ends is drained.
    relayed = relayed.pipeThrough(
      new TransformStream<Uint8Array, Uint8Array>({ flush: () => source.drain() }),
    );
  }
  return new Response(relayed, { status: upstream.status, headers });
}

type Ctx = { params: Promise<{ path?: string[] }> };

export async function GET(req: Request, ctx: Ctx): Promise<Response> {
  return handle(req, ctx);
}

export async function POST(req: Request, ctx: Ctx): Promise<Response> {
  return handle(req, ctx);
}

/** Files design §2.12: raw upload parts. */
export async function PUT(req: Request, ctx: Ctx): Promise<Response> {
  return handle(req, ctx);
}

/** Files design §2.7: `DELETE /v1/files/{id}`. */
export async function DELETE(req: Request, ctx: Ctx): Promise<Response> {
  return handle(req, ctx);
}

/**
 * Preflight is forwarded rather than answered here.
 *
 * Next answers OPTIONS itself when a route does not export it — with an
 * `Allow` header and no CORS headers at all, which fails every browser
 * preflight. The orchestrator owns the answer (CONTRACT §3), so the request
 * goes there and the answer comes back through the same allowlist.
 */
export async function OPTIONS(req: Request, ctx: Ctx): Promise<Response> {
  return handle(req, ctx);
}
