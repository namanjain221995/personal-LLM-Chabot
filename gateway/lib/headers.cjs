'use strict';
/**
 * The header rules of the /v1 edge, restated for the v1-gateway
 * (2026-09-13, no-timeout design revision 2, "WHY THE GATEWAY").
 *
 * WHY A COPY AND NOT AN IMPORT. The gateway image carries gateway/ and nothing
 * else (it is pinned by that directory's tree sha, so a frontend deploy must
 * never be able to change what it relays). route.ts is TypeScript inside the
 * Next build. So the lists live here, and test/parity.test.cjs parses
 * frontend/app/v1/[[...path]]/route.ts and next.config.mjs on every run and
 * fails the moment the two edges disagree.
 *
 * WHAT "IDENTICAL" MEANS. The base lists below are byte-for-byte route.ts's
 * REQUEST_HEADER_ALLOWLIST and RESPONSE_HEADER_ALLOWLIST as of 2026-09-13.
 * The PENDING lists are the names the two binding designs add to route.ts in
 * this same wave (no-timeout T5: x-stainless-retry-count and x-should-retry;
 * Files design §12.4/§12.5: the part, range and download headers). The
 * gateway forwards them now because implicit attach and resumable parts do
 * not work without them. The parity test asserts gateway == route.ts ∪
 * pending, and separately lists any pending name route.ts has since absorbed
 * so the integration commit can delete it from here.
 */

const http = require('node:http');

const REQUEST_HEADER_ALLOWLIST = Object.freeze([
  'authorization',
  'idempotency-key',
  'content-type',
  'accept',
  'accept-language',
  'user-agent',
  'origin',
  'access-control-request-method',
  'access-control-request-headers',
  // no-timeout design, deploy_survival §10 IMPLICIT ATTACH (measured, nt-parity).
  'x-stainless-retry-count',
  // Files design §12.4.
  'content-digest',
  'x-part-sha256',
  'range',
  'if-none-match',
]);

const PENDING_REQUEST_HEADERS = Object.freeze([]);

const RESPONSE_HEADER_ALLOWLIST = Object.freeze([
  'content-type',
  'retry-after',
  'ratelimit',
  'ratelimit-policy',
  'x-request-id',
  'www-authenticate',
  'cache-control',
  'vary',
  'access-control-allow-origin',
  'access-control-allow-methods',
  'access-control-allow-headers',
  'access-control-expose-headers',
  'access-control-max-age',
  // no-timeout design T5 (both SDKs read it before their own retry table).
  'x-should-retry',
  // Files design §12.5.
  'content-disposition',
  'content-range',
  'accept-ranges',
  'etag',
  'content-security-policy',
]);

const PENDING_RESPONSE_HEADERS = Object.freeze([]);

const RESPONSE_HEADER_PREFIXES = Object.freeze(['x-ratelimit-']);

/**
 * The internal attach protocol's namespace. Never accepted from a client
 * (a client that could send X-TechSara-Attempt could attach to someone
 * else's run) and never relayed back (X-TechSara-Run names the run).
 */
const INTERNAL_HEADER_PREFIX = 'x-techsara-';

/**
 * next.config.mjs `headers()` applies these four to every path, /v1
 * included, so a /v1 answer through Next carries them. The gateway adds the
 * same four so moving the path rule changes nothing a caller can observe.
 */
const NEXT_STATIC_HEADERS = Object.freeze({
  'x-content-type-options': 'nosniff',
  'x-frame-options': 'DENY',
  'referrer-policy': 'strict-origin-when-cross-origin',
  'permissions-policy': 'camera=(), microphone=(self), geolocation=()',
});

/** CONTRACT §10's streaming headers, re-asserted as route.ts does. */
const SSE_HEADERS = Object.freeze({
  'content-type': 'text/event-stream; charset=utf-8',
  'cache-control': 'no-store, no-cache, no-transform',
  connection: 'keep-alive',
  'x-accel-buffering': 'no',
});

/** keepalive.CommittedJSONResponse's commit headers (edge_100s §2). */
const COMMITTED_JSON_HEADERS = Object.freeze({
  'content-type': 'application/json',
  'cache-control': 'no-store, no-transform',
});

const NULL_BODY_STATUSES = new Set([101, 103, 204, 205, 304]);
const REDIRECT_STATUSES = new Set([301, 302, 303, 307, 308]);

/**
 * route.ts EDGE_ERRORS, plus the 411 the Files design adds for route 12.
 * A closed table: an SDK's retry logic switches on these codes.
 */
const EDGE_ERRORS = Object.freeze({
  request_too_large: { status: 413, type: 'invalid_request_error' },
  model_unavailable: { status: 503, type: 'service_unavailable_error' },
  internal_error: { status: 500, type: 'server_error' },
  invalid_request_error: { status: 411, type: 'invalid_request_error' },
});

function edgeError(code, message, opts = {}) {
  const spec = EDGE_ERRORS[code];
  const headers = {
    'content-type': 'application/json',
    'cache-control': 'no-store',
    ...NEXT_STATIC_HEADERS,
  };
  if (opts.retryAfter !== undefined) {
    headers['retry-after'] = String(Math.max(1, Math.ceil(opts.retryAfter)));
  }
  const body = JSON.stringify({
    error: { message, type: spec.type, code, param: null, request_id: null },
  });
  headers['content-length'] = String(Buffer.byteLength(body));
  return { status: spec.status, headers, body };
}

/* ------------------------------------------------ the caller's address -- */

const IPV4 =
  /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/;

/** lib/proxy.ts `isIpv6`, the same group counting. */
function isIpv6(value) {
  const halves = value.split('::');
  if (halves.length > 2) return false;
  const groups = [];
  for (const half of halves) {
    if (half !== '') groups.push(...half.split(':'));
  }
  let count = 0;
  for (let i = 0; i < groups.length; i += 1) {
    const group = groups[i];
    if (group.includes('.')) {
      if (i !== groups.length - 1 || !IPV4.test(group)) return false;
      count += 2;
      continue;
    }
    if (!/^[0-9a-f]{1,4}$/i.test(group)) return false;
    count += 1;
  }
  return halves.length === 2 ? count <= 7 : count === 8;
}

function isIpLiteral(value) {
  if (typeof value !== 'string' || value.length === 0 || value.length > 45) return false;
  return IPV4.test(value) || isIpv6(value);
}

/** lib/proxy.ts `trustedClientIp`: only the header the deployment NAMED. */
function trustedClientIp(clientHeaders, headerName) {
  if (!headerName) return null;
  const raw = clientHeaders[headerName];
  if (typeof raw !== 'string' || !raw) return null;
  const first = raw.split(',')[0].trim();
  return isIpLiteral(first) ? first : null;
}

/* ------------------------------------------------------ the two sets -- */

const REQUEST_NAMES = Object.freeze([...REQUEST_HEADER_ALLOWLIST, ...PENDING_REQUEST_HEADERS]);
const RESPONSE_NAMES = new Set([...RESPONSE_HEADER_ALLOWLIST, ...PENDING_RESPONSE_HEADERS]);

/**
 * What goes upstream: named, never filtered. `clientHeaders` is node's
 * lower-cased IncomingMessage.headers. No x-techsara-* name can be in the
 * result from the client, because none is on either list; the attach
 * headers are added by relay.cjs after this returns.
 */
function upstreamHeaders(clientHeaders, settings) {
  const out = {};
  for (const name of REQUEST_NAMES) {
    const value = clientHeaders[name];
    if (typeof value === 'string' && value) out[name] = value;
  }
  const ip = trustedClientIp(clientHeaders, settings.trustedClientIpHeader);
  if (ip) out['x-forwarded-for'] = ip;
  if (settings.trustedForwardedProto) out['x-forwarded-proto'] = settings.trustedForwardedProto;
  return out;
}

function isRelayableResponseHeader(name) {
  const key = name.toLowerCase();
  if (key.startsWith(INTERNAL_HEADER_PREFIX)) return false;
  return RESPONSE_NAMES.has(key) || RESPONSE_HEADER_PREFIXES.some((p) => key.startsWith(p));
}

/**
 * What comes back. `upstream` is node's lower-cased IncomingMessage.headers.
 * `content-length` is only ever relayed when relay.cjs asks for it (a byte
 * download relayed verbatim, Files design finding #15); everywhere else the
 * gateway may add bytes (heartbeats) or remove them (ts-seq comments), so the
 * response is chunked, as route.ts's is.
 */
function clientHeaders(upstream, { sse = false, keepLength = false } = {}) {
  const out = { ...NEXT_STATIC_HEADERS };
  for (const [name, value] of Object.entries(upstream)) {
    if (!isRelayableResponseHeader(name)) continue;
    const text = Array.isArray(value) ? value.join(', ') : String(value);
    try {
      // writeHead throws on a value node will not put on the wire, and a
      // throw inside a stream event is a process crash: every relay lost
      // for one bad header. Skip it instead.
      http.validateHeaderValue(name, text);
    } catch {
      continue;
    }
    out[name] = text;
  }
  if (sse) Object.assign(out, SSE_HEADERS);
  if (!out['content-type']) out['content-type'] = 'application/json';
  if (!out['cache-control']) out['cache-control'] = 'no-store';
  if (keepLength && typeof upstream['content-length'] === 'string') {
    out['content-length'] = upstream['content-length'];
  }
  return out;
}

function mediaType(contentType) {
  return String(contentType ?? '').toLowerCase().split(';')[0].trim();
}

function isEventStream(contentType) {
  return mediaType(contentType) === 'text/event-stream';
}

/** A body the gateway may prefix with JSON whitespace without changing it. */
function isJson(contentType) {
  const type = mediaType(contentType);
  return type === 'application/json' || type.endsWith('+json');
}

module.exports = {
  REQUEST_HEADER_ALLOWLIST,
  PENDING_REQUEST_HEADERS,
  RESPONSE_HEADER_ALLOWLIST,
  PENDING_RESPONSE_HEADERS,
  RESPONSE_HEADER_PREFIXES,
  INTERNAL_HEADER_PREFIX,
  NEXT_STATIC_HEADERS,
  SSE_HEADERS,
  COMMITTED_JSON_HEADERS,
  NULL_BODY_STATUSES,
  REDIRECT_STATUSES,
  EDGE_ERRORS,
  edgeError,
  isIpLiteral,
  trustedClientIp,
  upstreamHeaders,
  clientHeaders,
  isRelayableResponseHeader,
  isEventStream,
  isJson,
  mediaType,
};
