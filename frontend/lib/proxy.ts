/**
 * Server-side proxy helper for the /api/auth/*, /api/history/*, /api/admin/*
 * and share route handlers (V2 §4a). Forwards the request to the orchestrator
 * and passes cookies BOTH directions: the browser's Cookie header goes
 * upstream, and every upstream Set-Cookie comes back down (that is how the
 * HttpOnly ts_session cookie reaches the browser through the Next.js proxy).
 *
 * HARDENED 2026-09-12, for the developer platform (docs/developer-platform/
 * CONTRACT.md). The API this proxy is about to sit alongside is quota-bearing
 * and rate-limited, and the orchestrator audits and throttles by source
 * address — so four things that were merely untidy became defects:
 *
 * 1. FORWARDING HEADERS WERE LAUNDERED. The old code read `cf-connecting-ip`
 *    (or `x-forwarded-for`) OFF THE INBOUND REQUEST and re-sent it as
 *    `x-forwarded-for`. Production runs with AUTH_TRUST_PROXY_HEADERS=true
 *    (docs/AUTH.md, "Serving it publicly"), so the orchestrator BELIEVES that
 *    header: it becomes the audit trail's idea of "where from" and the key the
 *    login throttle locks out (authn/sessions.py `client_meta`). Anyone who
 *    can reach this process — and the frontend is reachable on the LAN,
 *    CONTRACT §18 — could therefore choose the address their failed logins
 *    were counted against, or lock out a colleague by typing theirs. Every
 *    client-supplied forwarding header is now stripped and never reaches the
 *    orchestrator; `trustedClientIp` is the one source this process believes.
 * 2. RESPONSE HEADERS WERE DROPPED. Only content-type survived, so a 429's
 *    `Retry-After` and the `RateLimit` headers CONTRACT §12 requires could
 *    never reach a caller through this proxy. There is now an allowlist.
 * 3. THERE WAS NO SIGNAL AND NO TIMEOUT. A closed tab left the upstream
 *    request running to completion; a wedged engine (the 22:15Z outage,
 *    2026-09-12) held this handler open for as long as it liked.
 * 4. BODIES ROUND-TRIPPED THROUGH A UTF-8 STRING. `await req.text()` replaces
 *    every byte that is not valid UTF-8 with U+FFFD, so a binary body sent
 *    through here arrived upstream corrupted. Bodies are bytes now.
 *
 * The bounded readers below are shared with the route handlers that take a
 * body of their own (/api/chat, /api/upload): the pattern is the one the
 * artifacts proxy introduced on 2026-09-11, lifted here so new callers share
 * one copy rather than hand-roll a fifth loop.
 */

export function orchestratorUrl(): string {
  return process.env.ORCHESTRATOR_URL ?? 'http://localhost:8080';
}

/** Read Set-Cookie headers portably (undici exposes getSetCookie()). */
function setCookiesOf(headers: Headers): string[] {
  const h = headers as Headers & { getSetCookie?: () => string[] };
  if (typeof h.getSetCookie === 'function') return h.getSetCookie();
  const single = headers.get('set-cookie');
  return single ? [single] : [];
}

/* --------------------------------------------- who the caller CLAIMS to be */

/**
 * Every header by which a caller can describe its own origin. Not one of them
 * is forwarded: what a client says about where it came from is a string it
 * chose, and this proxy must not be the thing that makes that string look
 * authenticated by putting it on a hop the orchestrator trusts.
 *
 * `x-forwarded-proto` is in the list for the same reason. The orchestrator
 * reads it to decide whether a session cookie gets the `Secure` flag when
 * AUTH_COOKIE_SECURE=auto (authn/sessions.py `_cookie_secure`), so a forged
 * `http` would strip `Secure` from a cookie issued over TLS. Production pins
 * AUTH_COOKIE_SECURE=true and does not consult it at all.
 *
 * The list is documentation as much as code — nothing reads it at runtime,
 * because the outbound header set is built by naming what goes out rather than
 * by deleting what must not, which is the only version of this that cannot be
 * defeated by a spelling nobody thought of.
 */
export const CLIENT_FORWARDING_HEADERS = [
  'cf-connecting-ip',
  'true-client-ip',
  'x-client-ip',
  'x-real-ip',
  'x-forwarded-for',
  'x-forwarded-proto',
  'x-forwarded-host',
  'forwarded',
] as const;

const IPV4 =
  /^(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)(\.(25[0-5]|2[0-4]\d|1\d\d|[1-9]?\d)){3}$/;

/**
 * IPv6 in any of its spellings, including the IPv4-mapped `::ffff:203.0.113.9`.
 *
 * Counting the groups is what earns this its keep, and a regex over the
 * permitted CHARACTERS would not: `203.0.113.9:8080` is nothing but hex digits,
 * dots and a colon, and an address with a port on it is not an address.
 */
function isIpv6(value: string): boolean {
  const halves = value.split('::');
  if (halves.length > 2) return false;
  const groups: string[] = [];
  for (const half of halves) {
    if (half !== '') groups.push(...half.split(':'));
  }
  let count = 0;
  for (let i = 0; i < groups.length; i += 1) {
    const group = groups[i];
    if (group.includes('.')) {
      // An embedded IPv4 tail, and only as the tail, where it fills two groups.
      if (i !== groups.length - 1 || !IPV4.test(group)) return false;
      count += 2;
      continue;
    }
    if (!/^[0-9a-f]{1,4}$/i.test(group)) return false;
    count += 1;
  }
  // `::` stands for one or more zero groups, so a compressed address is short
  // by definition; a written-out one is exactly eight.
  return halves.length === 2 ? count <= 7 : count === 8;
}

/**
 * A shape guard: nothing except a bare IP literal may be written into an
 * outbound header. A hostname, a port, a comma-separated list or a space all
 * fail here, which is what stops a trusted-but-compromised ingress smuggling a
 * second hop — or a second header — into the value. (A CR or LF cannot get
 * this far: undici refuses to build a Headers value containing one.)
 */
export function isIpLiteral(value: string): boolean {
  if (value.length === 0 || value.length > 45) return false;
  return IPV4.test(value) || isIpv6(value);
}

/**
 * The caller's address, from the ONE source this process is configured to
 * trust — or null, which means "say nothing".
 *
 * A Next route handler cannot see the socket's peer address, so the frontend
 * has no way to work out for itself whether a request arrived through the
 * ingress or straight off the LAN. What it can do is refuse to guess: an
 * address goes upstream only when the deployment has NAMED the header its own
 * ingress writes, in `TRUSTED_CLIENT_IP_HEADER`, and only when the value is a
 * single IP literal.
 *
 * In production that setting is `cf-connecting-ip`, and it is trustworthy for
 * exactly one reason: ai.techsarasolutions.com is served through a Cloudflare
 * Tunnel (docs/AUTH.md), cloudflared is the only way in from the internet, and
 * Cloudflare OVERWRITES cf-connecting-ip at its edge — a client cannot set it.
 * Unset, which is the default and what every local and LAN deployment should
 * leave it at, nothing is forwarded and the orchestrator records the address
 * it can actually see: this proxy. That is less informative and entirely
 * honest, which is the same trade docs/AUTH.md already describes for
 * AUTH_TRUST_PROXY_HEADERS.
 */
export function trustedClientIp(req: Request): string | null {
  const name = (process.env.TRUSTED_CLIENT_IP_HEADER ?? '').trim().toLowerCase();
  if (!name) return null;
  const raw = req.headers.get(name);
  if (!raw) return null;
  // A chain ("client, edge1, edge2") names the client first; everything after
  // it is a hop, and a hop is not what the orchestrator is being told about.
  const first = raw.split(',')[0].trim();
  return isIpLiteral(first) ? first : null;
}

/**
 * The scheme the PUBLIC edge terminated, stated by configuration rather than
 * read off the request, for deployments left on AUTH_COOKIE_SECURE=auto.
 * Anything other than http/https is ignored rather than forwarded.
 */
export function trustedForwardedProto(): string | null {
  const value = (process.env.TRUSTED_FORWARDED_PROTO ?? '').trim().toLowerCase();
  return value === 'https' || value === 'http' ? value : null;
}

/* ---------------------------------------------------- bounded body reading */

/**
 * The largest body this proxy will carry, 32 MiB.
 *
 * Everything it fronts is JSON: session and preference calls measured in
 * bytes, and the history sync PUT, which is the only one that can grow — a
 * whole conversation, pasted text included, and a paste has no cap of its own
 * anywhere on the input path (2026-09-05). 32 MiB is about 32 million
 * characters of transcript: past any real conversation, and a ceiling where
 * there was none at all.
 */
export const MAX_PROXY_BODY_BYTES = 32 * 1024 * 1024;

/** How long an orchestrator call may take before this proxy gives up. */
export const PROXY_TIMEOUT_MS = 30_000;

/**
 * Thrown into a request stream when the caller sends more than the route
 * allows. Matched by NAME rather than `instanceof`, because fetch rejects with
 * its own TypeError and hangs the real cause off `cause`.
 */
export class BodyTooLargeError extends Error {
  constructor(limit: number) {
    super(`request body exceeded ${limit} bytes`);
    this.name = 'BodyTooLargeError';
  }
}

/** Walk a thrown error's `cause` chain for a body-too-large refusal. */
export function isBodyTooLarge(err: unknown): boolean {
  let cur: unknown = err;
  for (let depth = 0; cur && depth < 5; depth += 1) {
    if ((cur as { name?: unknown }).name === 'BodyTooLargeError') return true;
    cur = (cur as { cause?: unknown }).cause;
  }
  return false;
}

/**
 * Is the body over the limit by the caller's OWN account? An absent
 * `Content-Length` proves nothing (a chunked body declares no length) and a
 * present one may be a lie, so this is a cheap first refusal and never the
 * only one — the reader and the stream below hold the same limit against the
 * bytes that actually arrive.
 */
export function declaredBodyOverLimit(req: Request, limit: number): boolean {
  const declared = req.headers.get('content-length');
  if (declared === null) return false;
  const length = Number(declared);
  return !Number.isFinite(length) || length > limit;
}

/**
 * Read a request body of at most `limit` bytes, chunk by chunk, so a body with
 * no Content-Length (chunked, or a client that lies) can never become a large
 * buffer in this process. `null` means it was over the limit; the reader is
 * cancelled there, so the rest is never pulled off the socket.
 */
export async function readBoundedBody(
  req: Request,
  limit: number,
): Promise<Uint8Array | null> {
  if (!req.body) return new Uint8Array(0);
  const reader = req.body.getReader();
  const chunks: Uint8Array[] = [];
  let total = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    total += value.byteLength;
    if (total > limit) {
      await reader.cancel().catch(() => undefined);
      return null;
    }
    chunks.push(value);
  }
  const joined = new Uint8Array(total);
  let offset = 0;
  for (const chunk of chunks) {
    joined.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return joined;
}

/**
 * The same limit for a body that must NOT be buffered — an upload of half a
 * gigabyte passes through this process at the size of one chunk, and reading
 * it into an array to measure it would be a worse bug than the one being
 * fixed. The count runs as the bytes go by; going over errors the stream,
 * which fails the in-flight fetch with a `BodyTooLargeError` in its cause
 * chain.
 */
export function boundedBodyStream(
  body: ReadableStream<Uint8Array>,
  limit: number,
): ReadableStream<Uint8Array> {
  let total = 0;
  return body.pipeThrough(
    new TransformStream<Uint8Array, Uint8Array>({
      transform(chunk, controller) {
        total += chunk.byteLength;
        if (total > limit) {
          controller.error(new BodyTooLargeError(limit));
          return;
        }
        controller.enqueue(chunk);
      },
    }),
  );
}

/** The refusal every bounded route gives, in the shape its callers parse. */
export function bodyTooLargeResponse(): Response {
  return Response.json(
    { message: 'The request body is too large.' },
    { status: 413 },
  );
}

/* ------------------------------------------------------- the proxy itself */

/**
 * Response headers that travel back to the browser.
 *
 * The old proxy relayed content-type and nothing else, which is why the admin
 * download endpoints had to bypass it entirely to keep a filename. It matters
 * more now: CONTRACT §12 puts `RateLimit` and `RateLimit-Policy` on every /v1
 * answer and `Retry-After` on every 429 and 503, and a client that cannot see
 * them is left to guess when to retry.
 */
const RESPONSE_HEADER_ALLOWLIST = [
  'retry-after',
  'ratelimit',
  'ratelimit-policy',
  'x-request-id',
  'content-disposition',
  'cache-control',
] as const;

/** Allowlisted by prefix: the x-ratelimit-* family has no fixed member list. */
const RESPONSE_HEADER_PREFIXES = ['x-ratelimit-'] as const;

function relayedResponseHeaders(upstream: Response): Headers {
  const out = new Headers();
  out.set(
    'content-type',
    upstream.headers.get('content-type') ?? 'application/json',
  );
  upstream.headers.forEach((value, name) => {
    const key = name.toLowerCase();
    const allowed =
      (RESPONSE_HEADER_ALLOWLIST as readonly string[]).includes(key) ||
      RESPONSE_HEADER_PREFIXES.some((prefix) => key.startsWith(prefix));
    if (allowed) out.set(key, value);
  });
  // Everything behind this proxy is one person's data. Where the orchestrator
  // has an opinion it is relayed above; where it has none, nothing here may be
  // held by a cache between this process and the browser.
  if (!out.has('cache-control')) out.set('cache-control', 'no-store');
  for (const c of setCookiesOf(upstream.headers)) {
    out.append('set-cookie', c);
  }
  return out;
}

export interface ProxyOptions {
  /** Bound on the request body; 413 over it. Defaults to MAX_PROXY_BODY_BYTES. */
  maxBodyBytes?: number;
  /** Bound on the whole upstream call. Defaults to PROXY_TIMEOUT_MS. */
  timeoutMs?: number;
}

export async function proxyToOrchestrator(
  req: Request,
  upstreamPath: string,
  opts: ProxyOptions = {},
): Promise<Response> {
  const maxBodyBytes = opts.maxBodyBytes ?? MAX_PROXY_BODY_BYTES;
  const timeoutMs = opts.timeoutMs ?? PROXY_TIMEOUT_MS;

  const headers: Record<string, string> = {};
  const cookie = req.headers.get('cookie');
  if (cookie) headers.cookie = cookie;
  const contentType = req.headers.get('content-type');
  if (contentType) headers['content-type'] = contentType;
  const userAgent = req.headers.get('user-agent');
  if (userAgent) headers['user-agent'] = userAgent;
  // Note what is NOT here: anything from CLIENT_FORWARDING_HEADERS. The two
  // forwarding headers that do go out are stated by this deployment, never
  // copied off the caller.
  const clientIp = trustedClientIp(req);
  if (clientIp) headers['x-forwarded-for'] = clientIp;
  const proto = trustedForwardedProto();
  if (proto) headers['x-forwarded-proto'] = proto;

  let body: ArrayBuffer | undefined;
  if (req.method !== 'GET' && req.method !== 'HEAD') {
    if (declaredBodyOverLimit(req, maxBodyBytes)) return bodyTooLargeResponse();
    // Bytes, not text: a UTF-8 round trip corrupts anything that is not text,
    // and this proxy has no business knowing which bodies are which. The
    // buffer is handed over rather than the view because TypeScript's BodyInit
    // does not admit a Uint8Array; `readBoundedBody` allocates it at exactly
    // the body's length, so the two describe the same bytes.
    const read = await readBoundedBody(req, maxBodyBytes);
    if (read === null) return bodyTooLargeResponse();
    body = read.byteLength > 0 ? (read.buffer as ArrayBuffer) : undefined;
  }

  // The caller's own signal AND a ceiling of our own: a closed tab must stop
  // the upstream work nobody is waiting for any more, and a wedged engine must
  // not be able to hold this handler open indefinitely.
  const timeout = AbortSignal.timeout(timeoutMs);
  const signal = AbortSignal.any([req.signal, timeout]);

  let upstream: Response;
  try {
    upstream = await fetch(`${orchestratorUrl()}${upstreamPath}`, {
      method: req.method,
      headers,
      body,
      cache: 'no-store',
      redirect: 'manual',
      signal,
    });
  } catch {
    // The browser went away: there is nobody left to read an answer, and 499
    // is what this codebase records for it (app/api/chat/route.ts).
    if (req.signal.aborted) return new Response(null, { status: 499 });
    if (timeout.aborted) {
      return Response.json(
        { message: 'The orchestrator took too long to answer.' },
        { status: 504 },
      );
    }
    return Response.json(
      { message: 'The orchestrator is unreachable.' },
      { status: 502 },
    );
  }

  return new Response(await upstream.arrayBuffer(), {
    status: upstream.status,
    headers: relayedResponseHeaders(upstream),
  });
}
