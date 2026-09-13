/**
 * /api/devplatform/* — the developer console's BFF.
 *
 * The console is a BROWSER surface: session cookie, capabilities, 404 on a
 * missing one. This handler carries its calls to the orchestrator's console
 * API and does nothing else. Authorization is entirely upstream — CONTRACT
 * §18 is explicit that the BFF is not a security boundary, because both this
 * process and the orchestrator are reachable on the LAN, so every check that
 * matters lives in the orchestrator and this file must not be mistaken for
 * one. What it CAN do is refuse to be a wider door than the console needs,
 * and that is what the three allowlists below are for.
 *
 * WHY THIS EXISTS AT ALL, GIVEN /api/admin/* ALREADY PROXIES (2026-09-13).
 * Two reasons, and neither is style:
 *
 *  1. THE PLAYGROUND STREAMS. `proxyToOrchestrator` ends with
 *     `new Response(await upstream.arrayBuffer(), …)` — it buffers the whole
 *     answer before replying. Piped through it, a streamed generation would
 *     arrive in one piece at the end, which is not a stream; the playground's
 *     event inspector would have nothing to inspect and a 60-second answer
 *     would look like a 60-second hang. So `playground/execute` gets
 *     `upstream.body` piped through untouched, exactly as /api/chat does.
 *  2. A NAMED SURFACE CAN BE ALLOWLISTED. The admin proxy forwards whatever
 *     path it is handed, which is right for a surface whose whole upstream is
 *     one capability-gated router. This one forwards a FIXED list of
 *     operations (below) and 404s everything else, so a future orchestrator
 *     route cannot become reachable from the console by accident.
 *
 * WHAT NEVER GOES UPSTREAM FROM HERE. The `Authorization` header. The public
 * API takes a key and ONLY a key (CONTRACT §1); this surface takes the session
 * and only the session. A browser that sent both would be asking the two
 * credentials to meet, which is the confused deputy the contract exists to
 * prevent — and it is also why the playground never asks for a key: there is
 * no path by which one could be used here. Both paths below build their
 * outbound header set by NAMING what goes out, so the header is dropped.
 */

import {
  MAX_PROXY_BODY_BYTES,
  declaredBodyOverLimit,
  orchestratorUrl,
  proxyToOrchestrator,
  readBoundedBody,
  trustedClientIp,
  trustedForwardedProto,
} from '@/lib/proxy';

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

/**
 * Where the orchestrator mounts the console API:
 * `orchestrator/app/apiplatform/console_api.py`,
 * `APIRouter(prefix="/admin/api/developers")`.
 *
 * FIXED 2026-09-13 (wave-2 verification). This constant said
 * `/admin/api/devplatform`, and the operation list below was written against
 * an imagined router — `keys?project=…`, `limits/:id`, `PATCH models/:id`,
 * `playground` — while the real one nests every project resource under
 * `projects/{project_id}/…`, toggles a model with PUT and runs the playground
 * at `playground/execute`. Every console call would have 404'd upstream. The
 * BFF path is now the orchestrator path, segment for segment, so the two can
 * be compared by eye and the proxy suite pins the table against the router.
 */
export const CONSOLE_UPSTREAM_BASE = '/admin/api/developers';

/**
 * What an identifier segment may look like.
 *
 * V34 ids are `proj_<24 hex>`, `key_<24 hex>`, `whe_<24 hex>` and the public
 * model ids are lower-case words with hyphens; none of them needs a dot, a
 * slash or a percent. Refusing everything else here means a segment cannot
 * carry path traversal or a second query string upstream even before the
 * re-encoding below — belt, and separately, braces.
 */
const ID_SEGMENT = /^[A-Za-z0-9_-]{1,64}$/;

/**
 * A query parameter's rule: the canonical value to send upstream, or null to
 * refuse the whole request.
 */
type QueryRule = (raw: string) => string | null;

/** A whole number inside [min, max], written in plain digits and nothing else. */
function boundedInt(min: number, max: number): QueryRule {
  return (raw) => {
    if (!/^\d{1,12}$/.test(raw)) return null;
    const n = Number(raw);
    return n >= min && n <= max ? String(n) : null;
  };
}

function oneOf(values: readonly string[]): QueryRule {
  return (raw) => (values.includes(raw) ? raw : null);
}

const identifier: QueryRule = (raw) => (ID_SEGMENT.test(raw) ? raw : null);

interface Operation {
  pattern: readonly string[];
  methods: readonly string[];
  /**
   * The query parameters this operation accepts, with a bound on each. Absent
   * means NONE: a query string on an operation that takes no parameters is
   * refused rather than forwarded.
   */
  query?: Readonly<Record<string, QueryRule>>;
  /** Piped through unbuffered (the playground). */
  stream?: boolean;
}

/**
 * The console's whole vocabulary — every entry has a caller in
 * components/devplatform, and every entry is a route in console_api.py with
 * the same path and method. `:id` matches one opaque identifier.
 *
 * NO DEAD SURFACE (2026-09-13). The previous list relayed four operations no
 * console code called (`keys/:id/rotate`, `logs/:id`,
 * `webhooks/:id/deliveries`, `settings`), "so the panel that calls them does
 * not have to reopen this file". The adversarial review measured the cost: a
 * rotate POST reached the orchestrator from any signed-in session with no
 * console code or frontend test anywhere near it, so its safety rested on an
 * upstream gate nothing in this repository exercised through this door. An
 * operation is added here IN THE SAME COMMIT as the panel that calls it and a
 * test that drives it — a console call with no line is a 404 from this
 * process and never reaches the network, which is loud in development and
 * closed in production.
 *
 * QUERY STRINGS ARE ALLOWLISTED TOO (same review). The old handler appended
 * `new URL(req.url).search` verbatim, so `logs?limit=999999999&offset=-1`
 * reached the orchestrator unbounded. Now each operation names its
 * parameters, each parameter carries the SAME bound the orchestrator's
 * `Query(...)` declares, and the upstream query is rebuilt from the validated
 * values — nothing the browser typed is concatenated onto the URL.
 */
const OPERATIONS: readonly Operation[] = [
  { pattern: ['overview'], methods: ['GET'] },
  { pattern: ['projects'], methods: ['GET', 'POST'] },
  // PATCH only for enable/disable; the list carries every field the panel shows.
  { pattern: ['projects', ':id'], methods: ['PATCH'] },
  { pattern: ['projects', ':id', 'keys'], methods: ['GET', 'POST'] },
  { pattern: ['projects', ':id', 'keys', ':id', 'revoke'], methods: ['POST'] },
  { pattern: ['projects', ':id', 'limits'], methods: ['GET', 'PUT'] },
  {
    pattern: ['projects', ':id', 'logs'],
    methods: ['GET'],
    // console_api.request_logs: status pattern and `Query(50, ge=1, le=200)`.
    query: {
      status: oneOf(['queued', 'in_progress', 'completed', 'failed', 'cancelled']),
      limit: boundedInt(1, 200),
    },
  },
  { pattern: ['projects', ':id', 'webhooks'], methods: ['GET', 'POST'] },
  { pattern: ['projects', ':id', 'webhooks', ':id'], methods: ['PATCH', 'DELETE'] },
  { pattern: ['projects', ':id', 'webhooks', ':id', 'test'], methods: ['POST'] },
  {
    pattern: ['usage'],
    methods: ['GET'],
    // console_api.usage_series: `max_length=64` and `ge=1, le=MAX_USAGE_DAYS`.
    query: { project_id: identifier, days: boundedInt(1, 93) },
  },
  { pattern: ['models'], methods: ['GET'] },
  { pattern: ['models', ':id'], methods: ['PUT'] },
  // The one streamed operation. POST only: a generation is not a GET. The
  // console sends no project_id, so none is accepted.
  { pattern: ['playground', 'execute'], methods: ['POST'], stream: true },
];

function matchOperation(parts: string[], method: string): Operation | null {
  const upper = method.toUpperCase();
  for (const op of OPERATIONS) {
    if (op.pattern.length !== parts.length) continue;
    if (!op.methods.includes(upper)) continue;
    const same = op.pattern.every((segment, i) => {
      const actual = parts[i] as string;
      return segment === ':id' ? ID_SEGMENT.test(actual) : segment === actual;
    });
    if (same) return op;
  }
  return null;
}

/**
 * The table, flattened for the proxy suite — which compares it, line by line,
 * with the `@router.<method>("…")` decorators in console_api.py so a rename on
 * either side fails a test instead of 404ing a console panel in production.
 */
export function consoleOperations(): { path: string; methods: string[] }[] {
  return OPERATIONS.map((op) => ({ path: op.pattern.join('/'), methods: [...op.methods] }));
}

/** True when this exact (path, method) pair is one of the console's operations. */
export function consoleRouteAllowed(parts: string[], method: string): boolean {
  return matchOperation(parts, method) !== null;
}

/**
 * The upstream query string for an operation, or null when the request must
 * be refused: an unknown parameter, a repeated one, or a value out of bounds.
 *
 * A repeated parameter is refused rather than first-or-last-wins, because the
 * two ends of this hop would not have to agree on which one wins.
 */
export function consoleQuery(parts: string[], method: string, search: string): string | null {
  const op = matchOperation(parts, method);
  if (!op) return null;
  const incoming = new URLSearchParams(search);
  const rules = op.query ?? {};
  const out = new URLSearchParams();
  const seen = new Set<string>();
  for (const [name, raw] of incoming) {
    const rule = Object.prototype.hasOwnProperty.call(rules, name) ? rules[name] : undefined;
    if (!rule || seen.has(name)) return null;
    seen.add(name);
    // An empty value is "not set" — the orchestrator's own default applies.
    if (raw === '') continue;
    const value = rule(raw);
    if (value === null) return null;
    out.set(name, value);
  }
  const text = out.toString();
  return text ? `?${text}` : '';
}

/**
 * The console body cap: 1 MiB, the same number CONTRACT §8 puts on a public
 * request. Nothing that rides this surface is an upload — a project name, a
 * scope list, a prompt — so anything larger is a mistake or an attack, and
 * either way it is refused here rather than carried across the network.
 */
export const MAX_CONSOLE_BODY_BYTES = 1024 * 1024;

/** Not found, in the console's own words, with nothing about what does exist. */
function unknownEndpoint(): Response {
  return Response.json({ message: 'Unknown console endpoint.' }, { status: 404 });
}

function badQuery(): Response {
  return Response.json(
    { message: 'The console request carried a query parameter it does not accept.' },
    { status: 400 },
  );
}

const SSE_HEADERS = {
  'Content-Type': 'text/event-stream; charset=utf-8',
  // no-transform is the load-bearing half: a compressing proxy that buffers to
  // find something to compress turns a token stream into one late block.
  'Cache-Control': 'no-store, no-cache, no-transform',
  Connection: 'keep-alive',
  'X-Accel-Buffering': 'no',
} as const;

/**
 * Response headers the streaming path carries down — the same list
 * `lib/proxy.ts` relays for every buffered call.
 *
 * FIXED 2026-09-13. This path used to relay content-type and x-request-id
 * alone, so a 429 from the playground reached the browser WITHOUT
 * `Retry-After` and a refusal reached it without `X-Request-Id` — the id
 * Settings and the request log tell a developer to quote. proxy.ts states the
 * rule ("a client that cannot see them is left to guess when to retry"); its
 * list is module-private and that file has another owner, so it is repeated
 * here and pinned by the proxy suite on both branches.
 */
const STREAM_RESPONSE_HEADERS = [
  'retry-after',
  'ratelimit',
  'ratelimit-policy',
  'x-request-id',
] as const;

function copyRelayedHeaders(upstream: Response, out: Headers): void {
  upstream.headers.forEach((value, name) => {
    const key = name.toLowerCase();
    if (
      (STREAM_RESPONSE_HEADERS as readonly string[]).includes(key) ||
      key.startsWith('x-ratelimit-')
    ) {
      out.set(key, value);
    }
  });
}

/**
 * Pipe a streamed generation through, byte for byte.
 *
 * NO WALL CLOCK OF OUR OWN, deliberately. `proxyToOrchestrator` puts a 30 s
 * ceiling on a call because a JSON endpoint that has not answered in 30 s is
 * wedged; a generation legitimately runs for minutes, and a ceiling here
 * shorter than the orchestrator's GEN_WALL_CLOCK_S would cut off good answers
 * at a fixed length — the exact invariant the SSE heartbeat rule was written
 * to protect (2026-09-01: the stream heartbeats so an idle proxy cannot close
 * it, and no timeout on this side may be shorter than the generation's own).
 * The caller's signal IS forwarded, so a closed tab still stops the work.
 */
async function proxyStream(req: Request, upstreamPath: string): Promise<Response> {
  if (declaredBodyOverLimit(req, MAX_CONSOLE_BODY_BYTES)) {
    return Response.json(
      { message: 'The request body is too large.' },
      { status: 413 },
    );
  }
  const raw = await readBoundedBody(req, MAX_CONSOLE_BODY_BYTES);
  if (raw === null) {
    return Response.json(
      { message: 'The request body is too large.' },
      { status: 413 },
    );
  }

  const headers: Record<string, string> = {
    'content-type': req.headers.get('content-type') ?? 'application/json',
    accept: 'text/event-stream',
  };
  const cookie = req.headers.get('cookie');
  if (cookie) headers.cookie = cookie;
  // FIXED 2026-09-13: the one quota-bearing, engine-touching console call was
  // the one call the orchestrator audited as coming from this container. The
  // same two deployment-stated headers every buffered call sends — never a
  // forwarding header copied off the caller (lib/proxy.ts, point 1).
  const clientIp = trustedClientIp(req);
  if (clientIp) headers['x-forwarded-for'] = clientIp;
  const proto = trustedForwardedProto();
  if (proto) headers['x-forwarded-proto'] = proto;

  let upstream: Response;
  try {
    upstream = await fetch(`${orchestratorUrl()}${upstreamPath}`, {
      method: 'POST',
      headers,
      body: raw.byteLength > 0 ? (raw.buffer as ArrayBuffer) : undefined,
      cache: 'no-store',
      redirect: 'manual',
      signal: req.signal,
    });
  } catch {
    // The reader went away: there is nobody left to hand an answer to, and 499
    // is what this codebase records for that (app/api/chat/route.ts).
    if (req.signal.aborted) return new Response(null, { status: 499 });
    return Response.json(
      { message: 'The orchestrator is unreachable.' },
      { status: 502 },
    );
  }

  const isStream = (upstream.headers.get('content-type') ?? '').includes(
    'text/event-stream',
  );

  // A refusal — or a non-streamed answer — arrives as JSON with a status, not
  // as a stream. Relay it as it is: the playground renders the §9 envelope's
  // own sentence.
  if (!upstream.ok || !upstream.body || !isStream) {
    const body = await upstream.text();
    const out = new Headers({
      'content-type': upstream.headers.get('content-type') ?? 'application/json',
      'cache-control': 'no-store',
    });
    copyRelayedHeaders(upstream, out);
    return new Response(body, { status: upstream.status, headers: out });
  }

  const headersOut = new Headers();
  copyRelayedHeaders(upstream, headersOut);
  // The SSE set is applied LAST so it wins on content-type and cache-control.
  for (const [name, value] of Object.entries(SSE_HEADERS)) headersOut.set(name, value);
  return new Response(upstream.body, { status: 200, headers: headersOut });
}

async function handle(req: Request, ctx: Ctx): Promise<Response> {
  const { path } = await ctx.params;
  const parts = path ?? [];
  const op = matchOperation(parts, req.method);
  if (!op) return unknownEndpoint();

  const query = consoleQuery(parts, req.method, new URL(req.url).search);
  if (query === null) return badQuery();

  // Next has already percent-decoded the segments; re-encode so nothing a
  // segment happens to contain can add a path separator or a query string of
  // its own upstream. (The same rule the admin proxy follows.)
  const upstreamPath = `${CONSOLE_UPSTREAM_BASE}/${parts
    .map(encodeURIComponent)
    .join('/')}${query}`;

  if (op.stream) return proxyStream(req, upstreamPath);
  return proxyToOrchestrator(req, upstreamPath, {
    maxBodyBytes: Math.min(MAX_CONSOLE_BODY_BYTES, MAX_PROXY_BODY_BYTES),
  });
}

type Ctx = { params: Promise<{ path: string[] }> };

export async function GET(req: Request, ctx: Ctx): Promise<Response> {
  return handle(req, ctx);
}

export async function POST(req: Request, ctx: Ctx): Promise<Response> {
  return handle(req, ctx);
}

export async function PATCH(req: Request, ctx: Ctx): Promise<Response> {
  return handle(req, ctx);
}

export async function PUT(req: Request, ctx: Ctx): Promise<Response> {
  return handle(req, ctx);
}

export async function DELETE(req: Request, ctx: Ctx): Promise<Response> {
  return handle(req, ctx);
}
