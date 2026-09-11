/**
 * /api/artifacts and /api/artifacts/* — the proxy for the Artifact Studio
 * listing, files, previews, grids and job status (docs/artifact-studio/API.md,
 * "Frontend proxy").
 *
 * The orchestrator URL is read server-side only and never reaches browser
 * JavaScript — the rule every /api proxy follows. Ownership is decided
 * upstream (`require_user` + user_id in every WHERE clause); this route
 * forwards the session cookie and passes the answer through with its status
 * intact, so 404-for-not-yours reaches the browser as a 404 and not as a
 * proxy failure.
 *
 * Path validation is the part that matters here. The optional catch-all
 * accepts any number of segments — none at all is the listing — so the route
 * matches them against the SMALL closed grammar the API defines: ids are 32
 * lowercase hex, versions and pages are integers, formats are the four the
 * studio writes. It answers 404 for anything else BEFORE an upstream request
 * exists. Traversal, encoded separators, a `..`, an unknown verb: none of
 * them becomes an outbound request, exactly as the /api/reports proxy refuses
 * an unsafe filename. A wrong METHOD on a known path gets the same 404, not a
 * 405: an `Allow` header would map the grammar for whoever is probing it.
 * The upstream re-checks every id too; this is defence in depth, not a
 * replacement.
 *
 * Unlike the reports proxy this one passes `Range` up and `Content-Range` /
 * `Accept-Ranges` / `ETag` down: a page viewer that seeks, or a browser that
 * resumes a download, must not be forced to re-fetch a whole file.
 */

export const runtime = 'nodejs';
export const dynamic = 'force-dynamic';

/** An optional catch-all: `path` is absent for GET /api/artifacts itself. */
type Ctx = { params: Promise<{ path?: string[] }> };

const ID = /^[a-f0-9]{32}$/;
const INT = /^(0|[1-9][0-9]{0,8})$/;
const POSITIVE_INT = /^[1-9][0-9]{0,8}$/;
const FORMATS = new Set(['pdf', 'docx', 'pptx', 'xlsx']);
const PAGE_PNG = /^([1-9][0-9]{0,4})\.png$/;
/** A segment that still holds a separator, a dot-segment or encoding. */
const BAD_SEGMENT = /[\\/%]|\.\./;
/** A sheet name is free text ("Q3 Results"); only control characters are out. */
// eslint-disable-next-line no-control-regex
const CONTROL_CHARS = /[\u0000-\u001f\u007f]/;
/**
 * Excel's own cap on a sheet name, and the upstream's `Query(max_length=31)`
 * (artifacts/api.py get_sheets): one limit for one field, so a long name is
 * dropped here rather than forwarded for FastAPI's 422.
 */
const SHEET_NAME_MAX = 31;
/** A conversation id, in the shape the uploads proxy accepts (SAFE_CONVERSATION). */
const SAFE_CONVERSATION = /^[A-Za-z0-9_-]{1,64}$/;

/** The query parameters each route may carry, and what they may say. */
const QUERY_RULES: Record<string, Record<string, (value: string) => boolean>> = {
  list: { conversation_id: (v) => SAFE_CONVERSATION.test(v) },
  file: { disposition: (v) => v === 'inline' || v === 'attachment' },
  page: { w: (v) => v === '240' || v === '1400' },
  sheets: {
    sheet: (v) => v.length > 0 && v.length <= SHEET_NAME_MAX && !CONTROL_CHARS.test(v),
    rows: (v) => POSITIVE_INT.test(v) && Number(v) <= 10_000,
    cols: (v) => POSITIVE_INT.test(v) && Number(v) <= 200,
  },
};

export interface ResolvedArtifactPath {
  /** The upstream path, rebuilt from validated pieces — never the raw input. */
  upstreamPath: string;
  /** Which query rule set applies (`list` · `file` · `page` · `sheets` · none). */
  query: keyof typeof QUERY_RULES | null;
  /** Methods the route accepts. */
  methods: readonly string[];
}

/**
 * Map the path segments onto the API's grammar. Returns null for anything
 * outside it — including a segment that still contains percent-encoding,
 * which Next has already decoded once and which therefore signals a
 * double-encoded attempt. No segments at all is the listing.
 */
export function resolveArtifactPath(segments: readonly string[]): ResolvedArtifactPath | null {
  if (!Array.isArray(segments) || segments.length > 6) return null;
  for (const s of segments) {
    if (typeof s !== 'string' || s === '' || BAD_SEGMENT.test(s)) return null;
  }

  // /artifacts?conversation_id=
  if (segments.length === 0) {
    return { upstreamPath: '/artifacts', query: 'list', methods: ['GET'] };
  }

  // /artifacts/jobs/{id}[/cancel|/retry]
  if (segments[0] === 'jobs') {
    const [, jobId, verb, ...rest] = segments;
    if (rest.length > 0 || !ID.test(jobId ?? '')) return null;
    if (verb === undefined) {
      return { upstreamPath: `/artifacts/jobs/${jobId}`, query: null, methods: ['GET'] };
    }
    if (verb === 'cancel' || verb === 'retry') {
      return {
        upstreamPath: `/artifacts/jobs/${jobId}/${verb}`,
        query: null,
        methods: ['POST'],
      };
    }
    return null;
  }

  const [artifactId, second, third, fourth, fifth, ...rest] = segments;
  if (rest.length > 0 || !ID.test(artifactId)) return null;

  // /artifacts/{id}
  if (second === undefined) {
    return { upstreamPath: `/artifacts/${artifactId}`, query: null, methods: ['GET'] };
  }
  // /artifacts/{id}/convert
  if (second === 'convert') {
    if (third !== undefined) return null;
    return { upstreamPath: `/artifacts/${artifactId}/convert`, query: null, methods: ['POST'] };
  }
  // /artifacts/{id}/v/{n}...
  if (second !== 'v' || !INT.test(third ?? '')) return null;
  const base = `/artifacts/${artifactId}/v/${Number(third)}`;
  if (fourth === undefined) {
    return { upstreamPath: base, query: null, methods: ['GET'] };
  }
  if (fourth === 'file') {
    if (fifth === undefined || !FORMATS.has(fifth)) return null;
    return { upstreamPath: `${base}/file/${fifth}`, query: 'file', methods: ['GET', 'HEAD'] };
  }
  if (fourth === 'preview') {
    if (fifth === undefined) {
      return { upstreamPath: `${base}/preview`, query: null, methods: ['GET', 'HEAD'] };
    }
    const m = PAGE_PNG.exec(fifth);
    if (!m) return null;
    return { upstreamPath: `${base}/preview/${Number(m[1])}.png`, query: 'page', methods: ['GET'] };
  }
  if (fourth === 'sheets') {
    if (fifth !== undefined) return null;
    return { upstreamPath: `${base}/sheets`, query: 'sheets', methods: ['GET'] };
  }
  return null;
}

/**
 * The query string to forward: only the parameters the route defines, only
 * with values that pass their rule. Anything else is dropped rather than
 * refused — a stray tracking parameter is not an attack, and the upstream
 * has its own defaults.
 */
export function forwardedQuery(
  rule: keyof typeof QUERY_RULES | null,
  search: URLSearchParams,
): string {
  if (!rule) return '';
  const allowed = QUERY_RULES[rule];
  const out = new URLSearchParams();
  for (const [key, check] of Object.entries(allowed)) {
    const value = search.get(key);
    if (value !== null && check(value)) out.set(key, value);
  }
  const s = out.toString();
  return s ? `?${s}` : '';
}

/** Request headers that travel upstream, when the browser sent them. */
const FORWARD_REQUEST_HEADERS = ['cookie', 'range', 'if-none-match', 'content-type'] as const;
/** Response headers that travel back, when the orchestrator sent them. */
const FORWARD_RESPONSE_HEADERS = [
  'content-type',
  'content-disposition',
  'content-length',
  'content-range',
  'accept-ranges',
  'etag',
  'cache-control',
] as const;

const NOT_FOUND = () => Response.json({ detail: 'Not found.' }, { status: 404 });

async function proxy(req: Request, ctx: Ctx): Promise<Response> {
  const { path } = await ctx.params;
  const resolved = resolveArtifactPath(path ?? []);
  // Validation FIRST, and a wrong method on a known path is the same answer
  // as an unknown path: nothing about this route is discoverable by probing.
  if (!resolved || !resolved.methods.includes(req.method)) return NOT_FOUND();

  const orchestratorUrl = process.env.ORCHESTRATOR_URL ?? 'http://localhost:8080';
  const query = forwardedQuery(resolved.query, new URL(req.url).searchParams);

  const headers: Record<string, string> = {};
  for (const name of FORWARD_REQUEST_HEADERS) {
    const value = req.headers.get(name);
    if (value) headers[name] = value;
  }

  let upstream: Response;
  try {
    upstream = await fetch(`${orchestratorUrl}${resolved.upstreamPath}${query}`, {
      method: req.method,
      headers,
      body: req.method === 'POST' ? await req.text() : undefined,
      cache: 'no-store',
      redirect: 'manual',
      signal: req.signal,
    });
  } catch {
    return Response.json({ detail: 'The orchestrator is unreachable.' }, { status: 502 });
  }

  const out = new Headers();
  for (const name of FORWARD_RESPONSE_HEADERS) {
    const value = upstream.headers.get(name);
    if (value) out.set(name, value);
  }
  // Private, always: a generated file belongs to one person, and a shared
  // cache between the edge and the browser must never hold it.
  if (!out.has('cache-control')) out.set('cache-control', 'private, no-store');

  // HEAD and 304 carry no body by definition: the status and the headers ARE
  // the answer, whatever the status is.
  if (req.method === 'HEAD' || upstream.status === 304) {
    return new Response(null, { status: upstream.status, headers: out });
  }

  const satisfied = upstream.ok || upstream.status === 206;
  if (!satisfied) {
    // The API promises `{detail: "<one sentence a person can act on>"}` and
    // never a stack trace, DSN, host or path — so a JSON refusal is passed
    // through for the card to show. Anything else is replaced with a generic
    // sentence: an HTML error page from a proxy in between says too much.
    const type = upstream.headers.get('content-type') ?? '';
    let detail =
      upstream.status === 404
        ? 'This file is no longer available.'
        : 'The request could not be completed.';
    if (type.includes('application/json')) {
      try {
        const body = (await upstream.json()) as { detail?: unknown };
        if (typeof body?.detail === 'string' && body.detail.trim()) detail = body.detail.trim();
      } catch {
        /* keep the generic sentence */
      }
    }
    return Response.json({ detail }, { status: upstream.status });
  }

  // A 200 with no body is a proxy failure, not a success — the reports
  // proxy draws the same line.
  if (!upstream.body) {
    return Response.json({ detail: 'The orchestrator sent an empty answer.' }, { status: 502 });
  }

  // Piped byte for byte — the file, a page image, or JSON — with the
  // upstream's own status (200, or 206 for a satisfied Range).
  return new Response(upstream.body, { status: upstream.status, headers: out });
}

export async function GET(req: Request, ctx: Ctx): Promise<Response> {
  return proxy(req, ctx);
}

export async function HEAD(req: Request, ctx: Ctx): Promise<Response> {
  return proxy(req, ctx);
}

export async function POST(req: Request, ctx: Ctx): Promise<Response> {
  return proxy(req, ctx);
}
