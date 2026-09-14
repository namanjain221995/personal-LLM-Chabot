// @vitest-environment jsdom
/**
 * The Files API documentation: /docs/files, /docs/uploads, /docs/file-inputs,
 * and the no-timeout sections of /docs/long-output (2026-09-13).
 *
 * These pages describe code that exists but is not mounted yet
 * (orchestrator/app/apifiles, orchestrator/app/publicapi/files) and a release
 * that is not live yet (the no-timeout design). Two switches keep them off the
 * site until they are true — FILES_API_PUBLISHED and NO_TIMEOUT_LIVE — and the
 * first tests below tie each switch to the code, so neither can be flipped
 * early or forgotten. FILES_API_PUBLISHED also waits for the public edge in
 * front of the router (exercised as a handler, with the orchestrator stubbed)
 * and for a recorded run through the public URL (FILES_EDGE_PROBE).
 *
 * Everything else holds the prose to the PRIMARY SOURCES, the way
 * tests/docs-site.test.tsx holds the rest of the site: the route table and
 * the scopes in publicapi/files/routes.py, the codes in publicapi/files/wire.py,
 * the ceilings in apifiles/limits.py, the stages in apifiles/sniff.py, the
 * derived names in apifiles/derived.py, the facts in apifiles/jobs.py, the
 * citation fields in apifiles/citations.py. Every page is checked in every
 * state it can be published in, because a page that is only rendered the day
 * a switch flips is a page nobody has read.
 */

import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { createRequire } from 'node:module';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ComponentProps, ReactNode } from 'react';

vi.mock('next/navigation', () => ({
  usePathname: () => '/docs/files',
  notFound: () => {
    throw new Error('notFound');
  },
}));

vi.mock('next/link', () => ({
  __esModule: true,
  default: (props: ComponentProps<'a'> & { children?: ReactNode }) => {
    const { children, ...rest } = props;
    return <a {...rest}>{children}</a>;
  },
}));

import * as nextEdge from '@/app/v1/[[...path]]/route';
import { DocsArticle } from '@/components/docs/DocsArticle';
import { docHeadingsOf } from '@/components/docs/docHeadings';
import type { DocPage } from '@/content/docs';
import {
  DOC_PAGES,
  EXAMPLE_KEYS,
  EXAMPLE_STATUS,
  MODEL_IDS,
  OVERVIEW_SLUG,
  VISION_MODEL_ID,
  WALL_CLOCK_PENDING_NOTE,
  findDocPage,
} from '@/content/docs';
import { fileInputs, fileInputsPage } from '@/content/docs/pages/fileInputs';
import {
  EXAMPLE_FILE_ID,
  EXAMPLE_PART_ID,
  EXAMPLE_UPLOAD_ID,
  FILES_API_PUBLISHED,
  FILES_EDGE_PROBE,
  FILE_LIMITS,
  files,
} from '@/content/docs/pages/files';
import type { FilesEdgeProbe } from '@/content/docs/pages/files';
import { NO_TIMEOUT_LIVE, longOutput, longOutputPage } from '@/content/docs/pages/longOutput';
import { uploads, uploadsPage } from '@/content/docs/pages/uploads';

afterEach(cleanup);

const FRONTEND_DIR = join(dirname(fileURLToPath(import.meta.url)), '..');
const REPO_ROOT = join(FRONTEND_DIR, '..');

function repoFile(...parts: string[]): string {
  return readFileSync(join(REPO_ROOT, ...parts), 'utf8');
}

const APP = ['orchestrator', 'app'];
const CONTRACT = repoFile('docs', 'developer-platform', 'CONTRACT.md');
const ROUTER_PY = repoFile(...APP, 'publicapi', 'router.py');
const MAIN_PY = repoFile(...APP, 'main.py');
const STREAMING_PY = repoFile(...APP, 'publicapi', 'streaming.py');
const ENDPOINTS_PY = repoFile(...APP, 'publicapi', 'endpoints.py');
const CONTENT_PY = repoFile(...APP, 'publicapi', 'files', 'content.py');
const ROUTES_PY = repoFile(...APP, 'publicapi', 'files', 'routes.py');
const WIRE_PY = repoFile(...APP, 'publicapi', 'files', 'wire.py');
const LIMITS_PY = repoFile(...APP, 'apifiles', 'limits.py');
const SNIFF_PY = repoFile(...APP, 'apifiles', 'sniff.py');
const DERIVED_PY = repoFile(...APP, 'apifiles', 'derived.py');
const JOBS_PY = repoFile(...APP, 'apifiles', 'jobs.py');
const EVENTS_PY = repoFile(...APP, 'apifiles', 'events.py');
const CITATIONS_PY = repoFile(...APP, 'apifiles', 'citations.py');
const CONTEXT_PY = repoFile(...APP, 'apifiles', 'context.py');
const SERVICE_PY = repoFile(...APP, 'apifiles', 'service.py');
const INLINE_PY = repoFile(...APP, 'apifiles', 'inline.py');
const EXTRACTORS_PY = repoFile(...APP, 'apifiles', 'extractors', '__init__.py');
const PUBLIC_EVENTS_PY = repoFile(...APP, 'publicapi', 'events.py');
const WEBHOOK_SENDER_PY = repoFile(...APP, 'apiplatform', 'webhooks', 'sender.py');
const OCR_PAGES_PY = repoFile(...APP, 'apifiles', 'ocr_pages.py');
const SCHEMA_PY = repoFile(...APP, 'apifiles', 'schema.py');
const QUEUE_PY = repoFile(...APP, 'apifiles', 'queue.py');
const EXTRACT_WORKER_PY = repoFile(...APP, 'apifiles', 'extract_worker.py');
const ERRORS_PY = repoFile(...APP, 'publicapi', 'errors.py');

// ------------------------------------------------------------- the states --

const STATES = [false, true];

/** The four pages this programme owns, in one no-timeout state. */
function ownPages(noTimeout: boolean): DocPage[] {
  return [files, uploadsPage({ noTimeout }), fileInputsPage({ noTimeout }), longOutputPage({ noTimeout })];
}

/** The pages new to this programme (long-output is only changed). */
function newPages(noTimeout: boolean): DocPage[] {
  return ownPages(noTimeout).slice(0, 3);
}

/**
 * The whole site as it would be published with the Files pages listed and
 * long-output in the given state — the site the links must resolve against.
 */
function publishedSite(noTimeout: boolean): DocPage[] {
  const replaced = new Map(ownPages(noTimeout).map((page) => [page.slug, page]));
  const site = DOC_PAGES.map((page) => replaced.get(page.slug) ?? page);
  for (const page of newPages(noTimeout)) {
    if (!site.some((existing) => existing.slug === page.slug)) site.push(page);
  }
  return site;
}

function fencedBlocks(body: string, language: string): string[] {
  const pattern = new RegExp(`^~~~${language}\\n([\\s\\S]*?)^~~~$`, 'gm');
  return [...body.matchAll(pattern)].map((m) => m[1]);
}

/** One `## heading` section, up to the next `## `. */
function sectionOf(body: string, heading: string): string {
  const start = body.indexOf(`## ${heading}\n`);
  expect(start, `## ${heading}`).toBeGreaterThanOrEqual(0);
  const next = body.indexOf('\n## ', start + 3);
  return body.slice(start, next === -1 ? undefined : next);
}

/** `12,345` in the page's own number style. */
function grouped(n: number): string {
  return n.toLocaleString('en-GB');
}

/** Whitespace collapsed, so a sentence can be matched across line breaks. */
function oneLine(text: string): string {
  return text.replace(/\s+/g, ' ');
}

// -------------------------------------------------------------- the code --

/** The `(method, path)` table the files router registers (routes.py). */
function filesRouteTable(): [string, string][] {
  const block = /^ROUTE_TABLE = \(([\s\S]*?)^\)/m.exec(ROUTES_PY);
  expect(block, 'ROUTE_TABLE in routes.py').not.toBeNull();
  return [...block![1].matchAll(/\("(GET|POST|PUT|DELETE)", "(\/v1\/[^"]+)"\)/g)].map((m) => [m[1], m[2]]);
}

/** handler name → the scope its `_begin` checks. */
function handlerScopes(): Map<string, string> {
  const scopes = new Map<string, string>();
  for (const m of ROUTES_PY.matchAll(
    /async def (\w+)\(self, request: Request[^)]*\) -> Response:\n\s+call = await self\._begin\(request, caller, scope=(SCOPE_READ|SCOPE_WRITE)/g,
  )) {
    scopes.set(m[1], m[2] === 'SCOPE_READ' ? 'files.read' : 'files.write');
  }
  return scopes;
}

/** `(method, /v1 path)` → the handler `_routes` binds it to. */
function routeHandlers(): Map<string, string> {
  const out = new Map<string, string>();
  for (const m of ROUTES_PY.matchAll(/\("(GET|POST|PUT|DELETE)", "(\/[^"]+)", "\w+", (\w+)\)/g)) {
    out.set(`${m[1]} /v1${m[2]}`, m[3]);
  }
  return out;
}

/** The CONTRACT §7 route table, normalised. */
function contractRoutes(): Set<string> {
  const routes = new Set<string>();
  for (const m of CONTRACT.matchAll(/^\|\s*(GET|POST|PUT|PATCH|DELETE)\s*\|\s*`(\/v1\/[^`]+)`/gm)) {
    routes.add(normalisePath(m[2]));
  }
  return routes;
}

/**
 * A documented path reduced to its route: `{param}`, a concrete example id,
 * a shell variable (`$UPLOAD_ID`) and a model id all fill a parameter.
 */
function normalisePath(path: string): string {
  const segments = path.replace(/[.]+$/, '').split('/');
  const normalised = segments.map((segment, i) => {
    if (!segment) return segment;
    if (segments[i - 1] === 'derived') return '{}';
    if (/^\{.*\}$/.test(segment) || /^\$/.test(segment)) return '{}';
    if (/^(?:file-|upload_|part_|resp_|msg_|proj_)[0-9a-f]+$/.test(segment)) return '{}';
    if ((MODEL_IDS as readonly string[]).includes(segment)) return '{}';
    return segment;
  });
  while (normalised.length > 2 && normalised[normalised.length - 1] === '') normalised.pop();
  return normalised.join('/');
}

function routesMentionedIn(body: string): string[] {
  const found = new Set<string>();
  for (const m of body.matchAll(/\/v1\/[A-Za-z0-9_\-{}./$]+/g)) found.add(normalisePath(m[0]));
  return [...found];
}

/** A Python constant expression from limits.py: `64 * _MIB`, `100_000`. */
function pythonBytes(expression: string): number {
  const units: Record<string, number> = { _MIB: 1024 ** 2, _GIB: 1024 ** 3 };
  return expression
    .split('*')
    .map((factor) => factor.trim())
    .reduce((product, factor) => product * (units[factor] ?? Number(factor.replace(/_/g, ''))), 1);
}

/** The default limits.py reads for a setting: `_int("NAME", <expr>)`. */
function limitDefault(name: string): number {
  const m = new RegExp(`_(?:int|float)\\("${name}", ([^)]+)\\)`).exec(LIMITS_PY);
  expect(m, `${name} in limits.py`).not.toBeNull();
  return pythonBytes(m![1]);
}

/** A Python tuple of string literals assigned to `name` in `source`. */
function pythonStrings(source: string, name: string): string[] {
  const m = new RegExp(`^${name} = \\(([^)]*)\\)`, 'm').exec(source);
  expect(m, name).not.toBeNull();
  return [...m![1].matchAll(/"([^"]+)"/g)].map((s) => s[1]);
}

/** The keys of the dict literal a function returns / assigns (`body: … = {`). */
function dictKeys(source: string, anchor: string): string[] {
  const start = source.indexOf(anchor);
  expect(start, anchor).toBeGreaterThanOrEqual(0);
  const open = source.indexOf('{', start);
  let depth = 0;
  let end = open;
  for (; end < source.length; end += 1) {
    if (source[end] === '{') depth += 1;
    if (source[end] === '}') {
      depth -= 1;
      if (depth === 0) break;
    }
  }
  const literal = source.slice(open + 1, end);
  // Top-level keys only: nested dicts are skipped by tracking depth.
  const keys: string[] = [];
  let level = 0;
  for (const m of literal.matchAll(/[{}]|"(\w+)":/g)) {
    if (m[0] === '{') level += 1;
    else if (m[0] === '}') level -= 1;
    else if (level === 0) keys.push(m[1]);
  }
  return keys;
}

/** A parsed JSON sample, walked structurally by the tests below. */
// eslint-disable-next-line @typescript-eslint/no-explicit-any
type JsonObject = Record<string, any>;

function jsonSamples(body: string): JsonObject[] {
  return fencedBlocks(body, 'json').map((block) => JSON.parse(block));
}

// ------------------------------------------------------------ the wiring --

/**
 * Python source with its comments and triple-quoted strings blanked out, and
 * every line break kept: a hook that exists only in a comment, in a docstring
 * or in a proposal pasted into one is not code.
 */
function pythonCode(source: string): string {
  let out = '';
  let i = 0;
  while (i < source.length) {
    const three = source.slice(i, i + 3);
    if (three === '"""' || three === "'''") {
      const end = source.indexOf(three, i + 3);
      const stop = end === -1 ? source.length : end + 3;
      out += source.slice(i, stop).replace(/[^\n]/g, ' ');
      i = stop;
      continue;
    }
    const ch = source[i];
    if (ch === '"' || ch === "'") {
      let j = i + 1;
      while (j < source.length && source[j] !== ch && source[j] !== '\n') j += source[j] === '\\' ? 2 : 1;
      out += source.slice(i, j + 1);
      i = j + 1;
      continue;
    }
    if (ch === '#') {
      const end = source.indexOf('\n', i);
      const stop = end === -1 ? source.length : end;
      out += ' '.repeat(stop - i);
      i = stop;
      continue;
    }
    out += ch;
    i += 1;
  }
  return out;
}

/** The text between the bracket at `open` and the one that closes it. */
function callArguments(code: string, open: number): string {
  let depth = 0;
  for (let i = open; i < code.length; i += 1) {
    const ch = code[i];
    if (ch === '"' || ch === "'") {
      let j = i + 1;
      while (j < code.length && code[j] !== ch && code[j] !== '\n') j += code[j] === '\\' ? 2 : 1;
      i = j;
      continue;
    }
    if ('([{'.includes(ch)) depth += 1;
    if (')]}'.includes(ch)) {
      depth -= 1;
      if (depth === 0) return code.slice(open + 1, i);
    }
  }
  return code.slice(open + 1);
}

/** Top-level comma-separated arguments: `a, b=f(x, y)` gives `a` and `b=f(x, y)`. */
function splitArguments(args: string): string[] {
  const parts: string[] = [];
  let depth = 0;
  let start = 0;
  for (let i = 0; i < args.length; i += 1) {
    const ch = args[i];
    if ('([{'.includes(ch)) depth += 1;
    if (')]}'.includes(ch)) depth -= 1;
    if (ch === ',' && depth === 0) {
      parts.push(args.slice(start, i));
      start = i + 1;
    }
  }
  parts.push(args.slice(start));
  return parts.map((part) => part.trim()).filter(Boolean);
}

/** The names of the functions enclosing `index`, innermost first (a class body runs, so it is transparent). */
function enclosingDefs(code: string, index: number): string[] {
  const lines = code.slice(0, index).split('\n');
  const indentOf = (line: string) => line.length - line.trimStart().length;
  let indent = indentOf(lines[lines.length - 1]);
  const names: string[] = [];
  for (let n = lines.length - 2; n >= 0 && indent > 0; n -= 1) {
    const line = lines[n];
    if (!line.trim() || indentOf(line) >= indent) continue;
    indent = indentOf(line);
    const def = /^\s*(?:async\s+)?def\s+(\w+)/.exec(line);
    if (def) names.push(def[1]);
  }
  return names;
}

function isDefName(code: string, index: number): boolean {
  return /def\s+$/.test(code.slice(Math.max(0, index - 12), index));
}

/**
 * Whether the code at `index` runs: at module level, or inside a function
 * that is itself referenced from code that runs. A `def` nobody calls does not.
 */
function runsAt(code: string, index: number, seen: Set<string> = new Set()): boolean {
  const [innermost] = enclosingDefs(code, index);
  if (innermost === undefined) return true;
  if (seen.has(innermost)) return false;
  seen.add(innermost);
  for (const m of code.matchAll(new RegExp(`\\b${innermost}\\b`, 'g'))) {
    if (isDefName(code, m.index!)) continue;
    if (runsAt(code, m.index!, seen)) return true;
  }
  return false;
}

/** keyword → value, for the top-level `name=value` arguments. */
function keywordArguments(args: string): Map<string, string> {
  const out = new Map<string, string>();
  for (const part of splitArguments(args)) {
    const m = /^(\w+)\s*=(?!=)\s*([\s\S]+)$/.exec(part);
    if (m) out.set(m[1], m[2].trim());
  }
  return out;
}

/**
 * The FilesDependencies fields an expression in router.py sets: a
 * `FilesDependencies(...)` call, `router_dependencies(...)` (read from
 * routes.py), `replace(deps, ...)`, or a variable assigned one of those.
 */
function dependencyFields(expression: string, routerCode: string, routesCode: string, depth = 0): Map<string, string> {
  const expr = expression.trim();
  if (depth > 6) return new Map();
  if (/^\w+$/.test(expr)) {
    const assignments = [...routerCode.matchAll(new RegExp(`^[ \\t]*${expr}[ \\t]*(?::[^=\\n]+)?=(?!=)[ \\t]*`, 'gm'))];
    const last = assignments[assignments.length - 1];
    if (!last) return new Map();
    const rest = routerCode.slice(last.index! + last[0].length);
    const call = /^[\w.]+\s*\(/.exec(rest);
    const rhs = call ? `${call[0]}${callArguments(rest, call[0].length - 1)})` : rest.split('\n')[0];
    return dependencyFields(rhs, routerCode, routesCode, depth + 1);
  }
  const call = /^(?:\w+\.)*(\w+)\s*\(/.exec(expr);
  if (!call) return new Map();
  const args = callArguments(expr, call[0].length - 1);
  if (call[1] === 'FilesDependencies') return keywordArguments(args);
  if (call[1] === 'router_dependencies') {
    const def = routesCode.indexOf('def router_dependencies(');
    const built = def === -1 ? -1 : routesCode.indexOf('FilesDependencies(', def);
    const fields =
      built === -1 ? new Map<string, string>() : keywordArguments(callArguments(routesCode, built + 'FilesDependencies'.length));
    for (const [key, value] of keywordArguments(args)) fields.set(key, value);
    return fields;
  }
  if (call[1] === 'replace') {
    const [base, ...rest] = splitArguments(args);
    const fields =
      base && !/^\w+\s*=(?!=)/.test(base) ? dependencyFields(base, routerCode, routesCode, depth + 1) : new Map<string, string>();
    for (const [key, value] of keywordArguments(rest.join(','))) fields.set(key, value);
    return fields;
  }
  return new Map();
}

/** What a FilesDependencies must set for all 14 routes and the File object these pages print. */
const FULL_WIRING = ['processing_view', 'derived', 'events', 'purge_blob'];

/**
 * Whether a module (publicapi/router.py or main.py), as CODE, registers the
 * files routes with every dependency the pages describe: the processing view (the File object's
 * allowlisted facts and ceiling sentences), derived data and events (routes
 * 6-8 exist only when these are given) and the purge (a DELETE that stops
 * processing). A registration with anything less serves a smaller API than
 * the pages document, so it does not count as mounted.
 */
function filesRoutesWired(routerSource: string, routesSource: string): { registered: boolean; missing: string[] } {
  const code = pythonCode(routerSource);
  const routesCode = pythonCode(routesSource);
  const modules = new Set<string>();
  const functions = new Map<string, string>();
  for (const m of code.matchAll(/^[ \t]*from[ \t]+(?:\.|\.publicapi\.|app\.publicapi\.)files(\.routes)?[ \t]+import[ \t]+(\([^)]*\)|[^\n;]+)/gm)) {
    for (const item of m[2].replace(/[()]/g, '').split(',')) {
      const [name, alias] = item.trim().split(/\s+as\s+/);
      if (!name) continue;
      if (!m[1] && name === 'routes') modules.add(alias ?? name);
      if (m[1]) functions.set(alias ?? name, name);
    }
  }
  const calls: { callee: string; args: string; at: number }[] = [];
  for (const alias of modules) {
    for (const m of code.matchAll(new RegExp(`\\b${alias}\\.(register|create_files_router)\\s*\\(`, 'g'))) {
      calls.push({ callee: m[1], args: callArguments(code, m.index! + m[0].length - 1), at: m.index! });
    }
  }
  for (const [alias, name] of functions) {
    if (name !== 'register' && name !== 'create_files_router') continue;
    for (const m of code.matchAll(new RegExp(`(?<![\\w.])${alias}\\s*\\(`, 'g'))) {
      if (isDefName(code, m.index!)) continue;
      calls.push({ callee: name, args: callArguments(code, m.index! + m[0].length - 1), at: m.index! });
    }
  }
  const running = calls.filter((call) => runsAt(code, call.at));
  if (running.length === 0) return { registered: false, missing: FULL_WIRING };
  const prefix = modules.size ? new RegExp(`\\b(?:${[...modules].join('|')})\\.`, 'g') : null;
  let best = FULL_WIRING;
  for (const call of running) {
    const keywords = keywordArguments(call.args);
    const positional = splitArguments(call.args).filter((arg) => !/^\w+\s*=(?!=)/.test(arg));
    const deps = keywords.get('deps') ?? (call.callee === 'register' ? positional[1] : positional[0]) ?? '';
    const fields = dependencyFields(prefix ? deps.replace(prefix, '') : deps, code, routesCode);
    const missing = FULL_WIRING.filter((field) => !fields.has(field) || fields.get(field) === 'None');
    if (missing.length < best.length) best = missing;
  }
  return { registered: true, missing: best };
}

/** The best registration among the modules that can mount the files router. */
function filesRoutesWiredAnywhere(): { registered: boolean; missing: string[]; where: string } {
  let best = { ...filesRoutesWired(ROUTER_PY, ROUTES_PY), where: 'publicapi/router.py' };
  const main = { ...filesRoutesWired(MAIN_PY, ROUTES_PY), where: 'main.py' };
  if (main.registered && (!best.registered || main.missing.length < best.missing.length)) best = main;
  return best;
}

/**
 * Whether the modules that serve /v1 generations (router.py, streaming.py,
 * endpoints.py, main.py), as code, import keepalive and serve `starting_after`
 * — the import in one and the parameter in another counts, since the
 * integration may split them.
 */
function streamResumeWired(sources: string | string[], keepaliveExists: boolean): boolean {
  const codes = (Array.isArray(sources) ? sources : [sources]).map(pythonCode);
  const importPattern =
    /^[ \t]*(?:from[ \t]+(?:\.|\.publicapi\.|app\.publicapi\.)keepalive[ \t]+import\b|from[ \t]+(?:\.|\.publicapi|app\.publicapi)[ \t]+import[ \t]+[^\n]*\bkeepalive\b)/m;
  const imported = codes.some((code) => importPattern.test(code));
  return keepaliveExists && imported && codes.some((code) => /\bstarting_after\b/.test(code));
}

// -------------------------------------------------------- the public edge --

/** The request headers the Files pages need the edge to forward. */
const FILE_REQUEST_HEADERS: Record<string, string> = {
  range: 'bytes=0-1048575',
  'if-none-match': '"9b1c"',
  'x-part-sha256': 'ab'.repeat(32),
  'content-digest': 'sha-256=:AAAA:',
};

/** The response headers the Files pages promise, with a plausible value each. */
const FILE_RESPONSE_HEADERS: Record<string, string> = {
  'content-disposition': 'attachment; filename="q3-report.pdf"',
  'content-security-policy': "sandbox; default-src 'none'",
  etag: '"9b1c"',
  'accept-ranges': 'bytes',
  'content-range': 'bytes 0-1048575/48213904',
  'x-should-retry': 'false',
};

type EdgeHandler = (req: Request, ctx: { params: Promise<{ path?: string[] }> }) => Promise<Response>;

interface EdgeCall {
  method: string;
  path: string[];
  /** A declared body length the edge must let through (0: no body). */
  declared: number;
  send: string[];
  relay: string[];
}

/** One request per behaviour the pages rely on, at the ceiling the pages state. */
function edgeCalls(): EdgeCall[] {
  const download = Object.keys(FILE_RESPONSE_HEADERS);
  return [
    { method: 'POST', path: ['files'], declared: FILE_LIMITS.singleMaxBodyBytes, send: [], relay: ['x-should-retry'] },
    { method: 'POST', path: ['uploads', EXAMPLE_UPLOAD_ID, 'parts'], declared: FILE_LIMITS.singleMaxBodyBytes, send: [], relay: ['x-should-retry'] },
    {
      method: 'PUT', path: ['uploads', EXAMPLE_UPLOAD_ID, 'parts', '0'], declared: FILE_LIMITS.partMaxBytes,
      send: ['x-part-sha256', 'content-digest'], relay: ['x-should-retry'],
    },
    { method: 'DELETE', path: ['files', EXAMPLE_FILE_ID], declared: 0, send: [], relay: [] },
    { method: 'GET', path: ['files', EXAMPLE_FILE_ID, 'content'], declared: 0, send: ['range', 'if-none-match'], relay: download },
    { method: 'GET', path: ['files', EXAMPLE_FILE_ID, 'derived', 'text.txt'], declared: 0, send: ['range'], relay: download.filter((h) => h !== 'etag') },
  ];
}

/**
 * What a Next-style /v1 edge module fails to carry for the Files pages, found
 * by CALLING its handlers with the orchestrator stubbed — so a refactor of how
 * the caps or the allowlists are spelled cannot fool it. Empty means ready.
 */
async function nextEdgeGaps(edge: Record<string, unknown>): Promise<string[]> {
  const gaps: string[] = [];
  const seen: Set<string>[] = [];
  // This file runs in jsdom, whose Headers global drops `range`; the edge runs
  // on Node, whose Headers keeps it. The runtime's own class is the one a
  // Response is built with, so the edge is called with that one in place.
  vi.stubGlobal('Headers', new Response('').headers.constructor);
  vi.stubGlobal('fetch', async (_url: string | URL, init?: RequestInit) => {
    seen.push(headerNames(init?.headers));
    return new Response('ok', { status: 200, headers: { 'content-type': 'application/octet-stream', ...FILE_RESPONSE_HEADERS } });
  });
  try {
    for (const call of edgeCalls()) {
      const label = `${call.method} /v1/${call.path.join('/')}`;
      const handler = edge[call.method];
      if (typeof handler !== 'function') {
        gaps.push(`${label}: no ${call.method} handler`);
        continue;
      }
      seen.length = 0;
      const headers: Record<string, string> = Object.fromEntries(call.send.map((name) => [name, FILE_REQUEST_HEADERS[name]]));
      if (call.declared) {
        headers['content-length'] = String(call.declared);
        headers['content-type'] = call.method === 'PUT' ? 'application/octet-stream' : 'multipart/form-data; boundary=x';
      }
      const request = new Request(`http://localhost:3000/v1/${call.path.join('/')}`, {
        method: call.method,
        headers,
        body: call.declared ? new Uint8Array(1) : undefined,
      });
      const response = await (handler as EdgeHandler)(request, { params: Promise.resolve({ path: call.path }) });
      await response.body?.cancel().catch(() => undefined);
      if (seen.length === 0) {
        gaps.push(`${label}: answered ${response.status} without reaching the orchestrator`);
        continue;
      }
      for (const name of call.send) if (!seen[0].has(name)) gaps.push(`${label}: dropped ${name}`);
      for (const name of call.relay) if (!response.headers.has(name)) gaps.push(`${label}: did not relay ${name}`);
    }
  } finally {
    vi.unstubAllGlobals();
  }
  return gaps;
}

/** The lower-cased names in any HeadersInit shape, without the environment's Headers class. */
function headerNames(init?: HeadersInit): Set<string> {
  const names = new Set<string>();
  if (!init) return names;
  if (Array.isArray(init)) {
    for (const [name] of init) names.add(name.toLowerCase());
  } else if (typeof (init as Headers).forEach === 'function') {
    (init as Headers).forEach((_value, name) => names.add(name.toLowerCase()));
  } else {
    for (const name of Object.keys(init)) names.add(name.toLowerCase());
  }
  return names;
}

/** Whether a compose file builds the /v1 gateway (gateway/Dockerfile). */
function composeBuildsGateway(sources: string[]): boolean {
  return sources.some((source) => /^[ \t]*context:[ \t]*["']?\.{1,2}\/gateway\/?["']?[ \t]*$/m.test(source));
}

function composeSources(): string[] {
  const out: string[] = [];
  for (const name of ['compose.yaml', 'docker-compose.yml']) {
    if (existsSync(join(REPO_ROOT, name))) out.push(repoFile(name));
  }
  const dir = join(REPO_ROOT, 'compose');
  if (existsSync(dir)) {
    for (const name of readdirSync(dir).filter((n) => /\.ya?ml$/.test(n))) out.push(repoFile('compose', name));
  }
  return out;
}

/** What the gateway fails to carry for the Files pages, read from its own modules. */
function gatewayGaps(): string[] {
  const load = createRequire(import.meta.url);
  const gateway = (...parts: string[]) => load(join(REPO_ROOT, 'gateway', ...parts));
  const headers = gateway('lib', 'headers.cjs');
  const bodies = gateway('lib', 'bodies.cjs');
  const { METHODS } = gateway('server.cjs');
  const gaps: string[] = [];
  for (const method of ['PUT', 'DELETE']) if (!METHODS.has(method)) gaps.push(`no ${method}`);
  const forwarded = headers.upstreamHeaders(FILE_REQUEST_HEADERS, { trustedClientIpHeader: '', trustedForwardedProto: '' });
  for (const name of Object.keys(FILE_REQUEST_HEADERS)) if (!(name in forwarded)) gaps.push(`drops ${name}`);
  for (const name of Object.keys(FILE_RESPONSE_HEADERS)) if (!headers.isRelayableResponseHeader(name)) gaps.push(`does not relay ${name}`);
  for (const call of edgeCalls().filter((c) => c.declared)) {
    const cap = bodies.bodyRuleFor(call.method, call.path.join('/'), {}).cap;
    if (cap < call.declared) gaps.push(`${call.method} /v1/${call.path.join('/')} capped at ${cap}`);
  }
  return gaps;
}

/** Whether the public URL carries the Files pages: route.ts, or a composed gateway. */
async function publicEdgeReadiness(): Promise<{ ready: boolean; detail: string }> {
  const next = await nextEdgeGaps(nextEdge as unknown as Record<string, unknown>);
  if (next.length === 0) return { ready: true, detail: 'route.ts carries the file routes' };
  const composed = composeBuildsGateway(composeSources());
  const gateway = gatewayGaps();
  return {
    ready: composed && gateway.length === 0,
    detail: `route.ts: ${next.join('; ')} | gateway composed: ${composed}; gateway gaps: ${gateway.join('; ') || 'none'}`,
  };
}

/** Whether a recorded run through the public URL passed. */
function edgeProbePassed(probe: FilesEdgeProbe | null): boolean {
  return (
    probe !== null &&
    /^\d{4}-\d{2}-\d{2}$/.test(probe.ranOn) &&
    probe.fullSizePartAccepted &&
    probe.chunkedCreateAccepted &&
    probe.envelopeOnEarlyRefusal &&
    [200, 408, 524].includes(probe.slowPartStatus)
  );
}

// ===========================================================================

describe('the two switches that keep these pages honest', () => {
  it('holds the Files pages back until the contract, the router, the public edge and a measured run through that edge all agree', async () => {
    const inContract = /^\|\s*POST\s*\|\s*`\/v1\/files`\s*\|/m.test(CONTRACT);
    // Routes 6-8 exist only when derived and events are given; the reading below relies on that.
    expect(ROUTES_PY).toContain('    if deps.derived is not None:\n');
    expect(ROUTES_PY).toContain('    if deps.events is not None:\n');
    const wiring = filesRoutesWiredAnywhere();
    const mounted = wiring.registered && wiring.missing.length === 0;
    const edge = await publicEdgeReadiness();
    const probed = edgeProbePassed(FILES_EDGE_PROBE);
    expect(
      FILES_API_PUBLISHED,
      `contract: ${inContract}; registered (${wiring.where}): ${wiring.registered}; missing: ${wiring.missing.join(', ')}; ` +
        `edge: ${edge.ready} (${edge.detail}); probe passed: ${probed}`,
    ).toBe(inContract && mounted && edge.ready && probed);

    const listed = ['files', 'uploads', 'file-inputs'].map((slug) => Boolean(findDocPage(slug)));
    expect(listed).toEqual([FILES_API_PUBLISHED, FILES_API_PUBLISHED, FILES_API_PUBLISHED]);
  });

  it('shows the no-timeout sections only once the committed response and stream resume are on the router', () => {
    const keepalive = existsSync(join(REPO_ROOT, ...APP, 'publicapi', 'keepalive.py'));
    expect(NO_TIMEOUT_LIVE).toBe(streamResumeWired([ROUTER_PY, STREAMING_PY, ENDPOINTS_PY, MAIN_PY], keepalive));

    // The site reads the pages built from the switch, never a stale copy.
    expect(longOutput.body).toBe(longOutputPage({ noTimeout: NO_TIMEOUT_LIVE }).body);
    expect(uploads.body).toBe(uploadsPage({ noTimeout: NO_TIMEOUT_LIVE }).body);
    expect(fileInputs.body).toBe(fileInputsPage({ noTimeout: NO_TIMEOUT_LIVE }).body);
    expect(findDocPage('long-output')).toBe(longOutput);
  });

  it('keeps the long-output page describing the wall clock until the release is live, and drops it after', () => {
    const current = longOutputPage({ noTimeout: false }).body;
    expect(current).toContain('## The wall clock\n');
    expect(current).toContain('## Choose the mode before you choose the size\n');
    expect(current).toContain('min(21600, max(4200, 900 + planned_max_output_tokens / 50))');
    expect(current).not.toContain('There is none.');
    expect(current).not.toContain('starting_after');

    const future = longOutputPage({ noTimeout: true }).body;
    for (const heading of ['Client timeouts', 'Resuming a stream', 'What still ends a generation']) {
      expect(future).toContain(`## ${heading}\n`);
    }
    // The two headings other pages link to survive, with what is now true under them.
    expect(sectionOf(future, 'The wall clock')).toContain('**There is none.**');
    expect(sectionOf(future, 'Choose the mode before you choose the size')).toContain('Every mode suits a long answer.');
    expect(future).not.toContain('min(21600');
    expect(future).not.toContain(WALL_CLOCK_PENDING_NOTE);
    expect(future).not.toMatch(/HTTP `524`|is the wrong tool for a long answer/);
    // The shared sections are the same text in both states.
    for (const heading of ['The ceiling', 'Input and output share one window', 'What the response tells you', 'How long it takes']) {
      expect(sectionOf(future, heading)).toBe(sectionOf(current, heading));
    }
  });

  it('changes the upload and model-input pages only in the sentences the release changes', () => {
    const slowNow = oneLine(uploadsPage({ noTimeout: false }).body);
    const slowLater = oneLine(uploadsPage({ noTimeout: true }).body);
    expect(slowNow).toContain('can be cut off — with a `408`, or with a `524` from the network edge');
    expect(slowNow).not.toContain('No clock on the service ends a part');
    expect(slowLater).toContain('No clock on the service ends a part while its bytes keep flowing');
    expect(slowLater).not.toContain('`408`, or with a `524`');
    expect(slowLater).toContain('sends nothing for 60 seconds');
    expect(slowLater).toContain('cut off a very slow part, with a `524`');

    const inputsNow = fileInputsPage({ noTimeout: false }).body;
    const inputsLater = fileInputsPage({ noTimeout: true }).body;
    expect(inputsNow).toContain('Waits up to 30 seconds');
    expect(inputsNow).toContain('shares about 45 seconds');
    expect(inputsNow).not.toContain('Waits as long as the file takes');
    expect(inputsLater).not.toContain('Waits up to 30 seconds');
    expect(inputsLater).not.toContain('shares about 45 seconds');
    expect(inputsLater).not.toContain('300 seconds of `input_audio` in\ntotal');
    expect(inputsNow).toContain('300 seconds of `input_audio` in\ntotal');
    expect(inputsLater).toContain('Waits as long as the file takes');
  });
});

describe('how the switches read the router', () => {
  /** A routes.py whose router_dependencies() passes all four. */
  const ROUTES_FULL = [
    'def router_dependencies(*, assembler=None):',
    '    """The real dependencies."""',
    '    return FilesDependencies(',
    '        resolve_caller=resolve,',
    '        authorize=authorize,',
    '        processing_view=jobs.file_processing_view,',
    '        purge_blob=retention.purge_blob,',
    '        derived=derived,',
    '        events=events.stream_file_events,',
    '    )',
  ].join('\n');
  /** A routes.py whose router_dependencies() passes none of them, as the storage build left it. */
  const ROUTES_BARE = [
    'def router_dependencies(*, assembler=None):',
    '    return FilesDependencies(',
    '        resolve_caller=resolve,',
    '        authorize=authorize,',
    '        assembler=assembler,',
    '    )',
  ].join('\n');
  const IMPORT = 'from .files import routes as files_routes\n';
  const HOOK = 'files_routes.register(router, files_routes.router_dependencies())\n';

  it('does not count a files hook that is only imported, commented out, quoted in a docstring or defined and never called', () => {
    for (const router of [
      IMPORT,
      `${IMPORT}# ${HOOK}`,
      `${IMPORT}"""\n${HOOK}"""\n`,
      `${IMPORT}\ndef _register_files():\n    ${HOOK}`,
      `${IMPORT}\ndef _register_files():\n    ${HOOK}\n# _register_files()\n`,
    ]) {
      expect(filesRoutesWired(router, ROUTES_FULL).registered, router).toBe(false);
    }
  });

  it('does not count a registration that leaves out the processing view, derived data, events or the purge', () => {
    // The hook the storage build proposed, against the router_dependencies() it built.
    expect(filesRoutesWired(`${IMPORT}${HOOK}`, ROUTES_BARE)).toEqual({ registered: true, missing: FULL_WIRING });
    const oneLine = 'from .files import routes as _files_routes; _files_routes.register(router, _files_routes.router_dependencies())\n';
    expect(filesRoutesWired(oneLine, ROUTES_BARE)).toEqual({ registered: true, missing: FULL_WIRING });
    expect(filesRoutesWired(oneLine, ROUTES_FULL)).toEqual({ registered: true, missing: [] });
    const partial = [
      IMPORT,
      'deps = files_routes.FilesDependencies(',
      '    resolve_caller=resolve, authorize=authorize,',
      '    processing_view=jobs.file_processing_view, derived=derived, purge_blob=retention.purge_blob,',
      ')',
      'files_routes.register(router, deps)',
    ].join('\n');
    expect(filesRoutesWired(partial, ROUTES_BARE)).toEqual({ registered: true, missing: ['events'] });
    expect(filesRoutesWired(partial.replace('derived=derived', 'derived=derived, events=None'), ROUTES_BARE).missing).toEqual(['events']);
  });

  it('counts a registration with all four, whether it runs at import, from a called function or through replace()', () => {
    expect(filesRoutesWired(`${IMPORT}${HOOK}`, ROUTES_FULL)).toEqual({ registered: true, missing: [] });
    expect(filesRoutesWired(`${IMPORT}\ndef _register_files():\n    ${HOOK}\n_register_files()\n`, ROUTES_FULL)).toEqual({
      registered: true,
      missing: [],
    });
    const replaced = [
      'from dataclasses import replace',
      'from .files.routes import create_files_router as build_files, router_dependencies',
      '',
      'def include_files(app):',
      '    app.include_router(build_files(replace(',
      '        router_dependencies(),',
      '        processing_view=jobs.file_processing_view, derived=derived,',
      '        events=events.stream_file_events, purge_blob=retention.purge_blob,',
      '    )))',
      '',
      'include_files(app)',
    ].join('\n');
    expect(filesRoutesWired(replaced, ROUTES_BARE)).toEqual({ registered: true, missing: [] });
  });

  it('counts stream resume only when keepalive exists, is imported as code, and starting_after is code', () => {
    const served = 'from . import errors, keepalive\nafter = request.query_params.get("starting_after")\n';
    expect(streamResumeWired(served, true)).toBe(true);
    expect(streamResumeWired(served, false)).toBe(false);
    expect(streamResumeWired('from . import errors, keepalive\n# starting_after, one day\n', true)).toBe(false);
    expect(streamResumeWired('from . import errors, keepalive\n"""Resume by starting_after."""\n', true)).toBe(false);
    expect(streamResumeWired('# from .keepalive import CommittedJSONResponse\nafter = q.get("starting_after")\n', true)).toBe(false);
  });

  it('counts a registration made in main.py, and stream resume whose import and parameter live in different modules', () => {
    const mainPy = [
      'from .publicapi.files.routes import create_files_router, router_dependencies',
      'app.include_router(create_files_router(router_dependencies()))',
    ].join('\n');
    expect(filesRoutesWired(mainPy, ROUTES_FULL)).toEqual({ registered: true, missing: [] });
    expect(filesRoutesWired(mainPy, ROUTES_BARE)).toEqual({ registered: true, missing: FULL_WIRING });
    expect(filesRoutesWired(`# ${mainPy}`, ROUTES_FULL).registered).toBe(false);

    const router = 'from .keepalive import CommittedJSONResponse\n';
    const streaming = 'def resume(request):\n    return request.query_params.get("starting_after")\n';
    expect(streamResumeWired([router, streaming], true)).toBe(true);
    expect(streamResumeWired([router, streaming.replace(/^/gm, '# ')], true)).toBe(false);
    expect(streamResumeWired(['from .publicapi import keepalive\n', streaming], true)).toBe(true);
  });
});

describe('how the switches read the public edge', () => {
  /** A Next-style edge built from the parts a test chooses. */
  function fakeEdge(opts: { methods: string[]; cap: number; forward: string[]; relay: string[] }): Record<string, EdgeHandler> {
    const handler: EdgeHandler = async (req) => {
      if (Number(req.headers.get('content-length') ?? 0) > opts.cap) return new Response('{}', { status: 413 });
      const headers: Record<string, string> = {};
      for (const name of opts.forward) {
        const value = req.headers.get(name);
        if (value) headers[name] = value;
      }
      const upstream = await fetch('http://orchestrator.invalid/v1', { method: req.method, headers });
      const out = new Headers();
      upstream.headers.forEach((value, name) => {
        if (opts.relay.includes(name)) out.set(name, value);
      });
      return new Response(upstream.body, { status: upstream.status, headers: out });
    };
    return Object.fromEntries(opts.methods.map((method) => [method, handler]));
  }
  const ALL_METHODS = ['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'];
  const FULL = {
    methods: ALL_METHODS,
    cap: FILE_LIMITS.singleMaxBodyBytes,
    forward: Object.keys(FILE_REQUEST_HEADERS),
    relay: Object.keys(FILE_RESPONSE_HEADERS),
  };

  it('counts an edge that takes PUT and DELETE, lets a full part through and carries every file header', async () => {
    expect(await nextEdgeGaps(fakeEdge(FULL))).toEqual([]);
  });

  it('does not count an edge that lacks PUT or DELETE, caps bodies at 1 MiB, or drops a file header either way', async () => {
    const noWrite = await nextEdgeGaps(fakeEdge({ ...FULL, methods: ['GET', 'POST', 'OPTIONS'] }));
    expect(noWrite).toContain(`PUT /v1/uploads/${EXAMPLE_UPLOAD_ID}/parts/0: no PUT handler`);
    expect(noWrite).toContain(`DELETE /v1/files/${EXAMPLE_FILE_ID}: no DELETE handler`);

    const small = await nextEdgeGaps(fakeEdge({ ...FULL, cap: 1024 * 1024 }));
    expect(small).toContain('POST /v1/files: answered 413 without reaching the orchestrator');
    expect(small.filter((gap) => gap.includes('answered 413'))).toHaveLength(3);

    for (const name of Object.keys(FILE_REQUEST_HEADERS)) {
      const gaps = await nextEdgeGaps(fakeEdge({ ...FULL, forward: FULL.forward.filter((h) => h !== name) }));
      expect(gaps.some((gap) => gap.endsWith(`dropped ${name}`)), name).toBe(true);
    }
    for (const name of Object.keys(FILE_RESPONSE_HEADERS)) {
      const gaps = await nextEdgeGaps(fakeEdge({ ...FULL, relay: FULL.relay.filter((h) => h !== name) }));
      expect(gaps.some((gap) => gap.endsWith(`did not relay ${name}`)), name).toBe(true);
    }
  });

  it('reads the real route.ts the same way, with the runtime Headers class, and restores both globals afterwards', async () => {
    const before = [globalThis.fetch, globalThis.Headers];
    const gaps = await nextEdgeGaps(nextEdge as unknown as Record<string, unknown>);
    expect(globalThis.fetch).toBe(before[0]);
    expect(globalThis.Headers).toBe(before[1]);
    // An edge that builds its upstream headers with `new Headers()` still forwards `range`.
    const viaHeaders: EdgeHandler = async (req) => {
      const out = new Headers();
      for (const name of Object.keys(FILE_REQUEST_HEADERS)) {
        const value = req.headers.get(name);
        if (value) out.set(name, value);
      }
      const upstream = await fetch('http://orchestrator.invalid/v1', { method: req.method, headers: out });
      return new Response(upstream.body, { status: upstream.status, headers: upstream.headers });
    };
    const headerGaps = await nextEdgeGaps({ GET: viaHeaders, POST: viaHeaders, PUT: viaHeaders, DELETE: viaHeaders });
    expect(headerGaps).toEqual([]);
    // Whatever today's answer, it is a list of named gaps, never a crash.
    for (const gap of gaps) expect(gap).toMatch(/^(GET|POST|PUT|DELETE) \/v1\/\S+: /);
  });

  it('counts the gateway only when a compose file builds it, and reads its lists and caps from its own modules', () => {
    expect(composeBuildsGateway(['services:\n  v1-gateway:\n    build:\n      context: ./gateway\n'])).toBe(true);
    expect(composeBuildsGateway(['services:\n  v1-gateway:\n    build:\n      context: "../gateway/"\n'])).toBe(true);
    expect(composeBuildsGateway(['# context: ./gateway\n', 'services:\n  frontend:\n    build:\n      context: ./frontend\n'])).toBe(false);
    expect(gatewayGaps()).toEqual([]);
  });

  it('counts the edge probe only when it carries a date and all three checks passed', () => {
    const passing: FilesEdgeProbe = {
      ranOn: '2026-09-20', fullSizePartAccepted: true, chunkedCreateAccepted: true, envelopeOnEarlyRefusal: true, slowPartStatus: 524,
    };
    expect(edgeProbePassed(null)).toBe(false);
    expect(edgeProbePassed(passing)).toBe(true);
    expect(edgeProbePassed({ ...passing, ranOn: 'soon' })).toBe(false);
    for (const key of ['fullSizePartAccepted', 'chunkedCreateAccepted', 'envelopeOnEarlyRefusal'] as const) {
      expect(edgeProbePassed({ ...passing, [key]: false }), key).toBe(false);
    }
  });
});

describe('the routes and scopes', () => {
  it('names every route the files router can register, somewhere on the Files pages', () => {
    const table = filesRouteTable();
    expect(table).toHaveLength(14);
    const text = newPages(false).map((page) => page.body).join('\n');
    for (const [method, path] of table) {
      expect(text, `${method} ${path}`).toContain(`${method} ${path}`);
    }
  });

  it('gives each route the scope routes.py checks for it', () => {
    const scopes = handlerScopes();
    const handlers = routeHandlers();
    expect(scopes.size).toBeGreaterThanOrEqual(14);

    // The files page's route table: `| \`METHOD /v1/…\` | \`scope\` |`.
    const rows = [...files.body.matchAll(/^\| `(GET|POST|PUT|DELETE) (\/v1\/[^`]+)` \| `(files\.(?:read|write))` \|/gm)];
    expect(rows.length).toBe(8);
    for (const [, method, path, scope] of rows) {
      const handler = handlers.get(`${method} ${path}`);
      expect(handler, `${method} ${path}`).toBeDefined();
      expect(scopes.get(handler!), `${method} ${path}`).toBe(scope);
    }

    // "Every upload route needs the files.write scope".
    expect(uploads.body).toContain('Every upload route needs the `files.write` scope');
    for (const [method, path] of filesRouteTable().filter(([, path]) => path.startsWith('/v1/uploads'))) {
      expect(scopes.get(handlers.get(`${method} ${path}`)!), `${method} ${path}`).toBe('files.write');
    }

    // Model input: files.read on top of responses.write, before any lookup.
    expect(SERVICE_PY).toContain('FILES_READ_SCOPE = "files.read"');
    expect(fileInputs.body).toContain('needs the `files.read` scope as well as\n`responses.write`');
  });

  it('names no /v1 route that is in neither the contract nor the files route table', () => {
    const allowed = new Set([...contractRoutes(), ...filesRouteTable().map(([, path]) => normalisePath(path))]);
    expect(allowed).toContain('/v1/responses/{}');
    expect(allowed).toContain('/v1/uploads/{}/parts/{}');
    for (const noTimeout of STATES) {
      const offenders: string[] = [];
      for (const page of ownPages(noTimeout)) {
        for (const route of routesMentionedIn(page.body)) {
          if (!allowed.has(route)) offenders.push(`${page.slug}: ${route}`);
        }
      }
      expect(offenders, `noTimeout=${noTimeout}`).toEqual([]);
    }
  });

  it('says Idempotency-Key is refused on every file route, as routes.py refuses it', () => {
    expect(ROUTES_PY).toContain('"Idempotency-Key is not supported on file routes; parts are idempotent by part_number."');
    expect(ROUTES_PY).toMatch(/def _begin[\s\S]{0,600}_refuse_idempotency_key\(request\)/);
    expect(files.body).toContain('Every file and upload route refuses the header');
    expect(uploads.body).toContain('refuses `Idempotency-Key`');
  });
});

describe('the wire objects', () => {
  it('prints the File object with exactly the keys wire.py and jobs.py put on it', () => {
    const fileKeys = dictKeys(WIRE_PY, 'def file_object(');
    const processingKeys = dictKeys(JOBS_PY, '    view = {');
    const sample = jsonSamples(files.body).find((s) => s.object === 'file' && s.processing)!;
    expect(Object.keys(sample)).toEqual(fileKeys);
    expect(Object.keys(sample.processing)).toEqual(processingKeys);
    expect(sample.id).toMatch(/^file-[0-9a-f]{24}$/);
    expect(sample.sha256).toMatch(/^[0-9a-f]{64}$/);
  });

  it('prints the Upload, part, list and deleted objects in the shapes wire.py builds', () => {
    const uploadKeys = dictKeys(WIRE_PY, 'def upload_object(');
    const partKeys = dictKeys(WIRE_PY, 'def part_object(');
    const listKeys = dictKeys(WIRE_PY, 'def list_object(');
    const deletedKeys = dictKeys(WIRE_PY, 'def deleted_object(');

    const uploadSamples = jsonSamples(uploads.body).filter((s) => s.object === 'upload');
    expect(uploadSamples.length).toBe(3);
    for (const sample of uploadSamples) {
      expect(Object.keys(sample).sort()).toEqual(
        [...uploadKeys, ...('parts' in sample ? ['parts', 'part_mode', 'error'] : [])].sort(),
      );
    }
    const resume = uploadSamples.find((s) => 'parts' in s)!;
    expect(Object.keys(resume.parts[0])).toEqual(partKeys);
    const part = jsonSamples(uploads.body).find((s) => s.object === 'upload.part')!;
    expect(Object.keys(part)).toEqual(partKeys);

    const list = jsonSamples(files.body).find((s) => s.object === 'list' && 'has_more' in s)!;
    expect(Object.keys(list)).toEqual(listKeys);
    const deleted = jsonSamples(files.body).find((s) => s.deleted === true)!;
    expect(Object.keys(deleted)).toEqual(deletedKeys);

    expect(EXAMPLE_UPLOAD_ID).toMatch(/^upload_[0-9a-f]{24}$/);
    expect(EXAMPLE_PART_ID).toMatch(/^part_[0-9a-f]{24}$/);
    expect(EXAMPLE_FILE_ID).toMatch(/^file-[0-9a-f]{24}$/);
  });

  it('keeps every JSON sample parseable, and gives every response object its output ceiling fields', () => {
    for (const noTimeout of STATES) {
      for (const page of ownPages(noTimeout)) {
        for (const block of fencedBlocks(page.body, 'json')) {
          expect(() => JSON.parse(block), `${page.slug}: ${block.slice(0, 40)}`).not.toThrow();
        }
        for (const sample of jsonSamples(page.body).filter((s) => s.object === 'response')) {
          expect(sample, page.slug).toHaveProperty('max_output_tokens');
          expect(sample, page.slug).toHaveProperty('incomplete_details');
        }
      }
    }
  });
});

describe('the error vocabulary', () => {
  /** code → [status, x-should-retry] from wire.FILE_CODES. */
  function fileCodes(): Map<string, [number, boolean]> {
    const block = /^FILE_CODES: Dict\[str, tuple\] = \{([\s\S]*?)^\}/m.exec(WIRE_PY);
    expect(block).not.toBeNull();
    return new Map(
      [...block![1].matchAll(/"([a-z_]+)": \((\d{3}), "[a-z_]+", (True|False)\)/g)].map((m) => [
        m[1],
        [Number(m[2]), m[3] === 'True'] as [number, boolean],
      ]),
    );
  }

  it('documents each of the seven file codes with the status wire.py sends, and no other status', () => {
    const codes = fileCodes();
    expect([...codes.keys()].sort()).toEqual(
      ['checksum_mismatch', 'file_not_found', 'file_not_ready', 'incomplete_body', 'storage_unavailable', 'upload_not_found', 'upload_state_conflict'],
    );
    const tables = [files, uploads].map((page) => sectionOf(page.body, 'Errors')).join('\n');
    const documented = new Map<string, Set<number>>();
    for (const m of tables.matchAll(/^\| `([a-z_]+)` \| (\d{3}) \|/gm)) {
      if (!documented.has(m[1])) documented.set(m[1], new Set());
      documented.get(m[1])!.add(Number(m[2]));
    }
    for (const [code, [status]] of codes) {
      expect([...(documented.get(code) ?? [])], code).toEqual([status]);
    }
    const inputs = sectionOf(fileInputs.body, 'Errors');
    for (const m of inputs.matchAll(/^\| (\d{3}) \| `([a-z_]+)` \|/gm)) {
      if (codes.has(m[2])) expect(Number(m[1]), m[2]).toBe(codes.get(m[2])![0]);
    }
  });

  it('prints x-should-retry the way wire.py sets it for a full disk and for a purge in progress', () => {
    expect(fileCodes().get('storage_unavailable')).toEqual([503, false]);
    expect(WIRE_PY).toMatch(/def storage_busy\(retry_after: float = 2\)[\s\S]*?should_retry=True/);
    expect(WIRE_PY).toMatch(/def storage_unavailable\(retry_after: float = 60\)/);
    const row = /^\| `storage_unavailable` \| 503 \|.*$/m.exec(files.body)![0];
    expect(row).toContain('`Retry-After: 60`, `x-should-retry: false`');
    expect(row).toContain('`Retry-After: 2`, `x-should-retry: true`');
    expect(ROUTES_PY).toContain('retry_after=2, should_retry=True');
    expect(uploads.body).toContain('`Retry-After: 2` and `x-should-retry: true`');
  });

  it('quotes the processing failure sentences verbatim', () => {
    const block = /^ERROR_SENTENCES: Dict\[str, str\] = \{([\s\S]*?)^\}/m.exec(EXTRACTORS_PY)!;
    const sentences = new Map(
      [...block[1].matchAll(/"([a-z_]+)": \(?\s*"([^"]+)"(?:\s*"([^"]+)")?/g)].map((m) => [m[1], m[2] + (m[3] ?? '')]),
    );
    expect(sentences.size).toBe(5);
    const table = sectionOf(files.body, 'Errors');
    for (const [code, sentence] of sentences) {
      const expected = sentence.includes('{ceiling}') ? sentence.replace('{ceiling}.', '') : sentence;
      expect(table, code).toContain(`| \`${code}\` | ${expected}`);
    }
    expect(WIRE_PY).toContain('"checksum_mismatch": "The assembled bytes did not match the checksum you supplied."');
    expect(table).toContain('| `checksum_mismatch` | The assembled bytes did not match the checksum you supplied. |');
  });
});

describe('the status codes and headers the routes really send', () => {
  const flat = oneLine;
  /** A method of the routes.py handler class, from its `async def` to the next method. */
  function methodBody(source: string, name: string): string {
    const start = source.indexOf(`    async def ${name}(`);
    expect(start, name).toBeGreaterThanOrEqual(0);
    const next = source.slice(start + 8).search(/\n    (?:async def|def) /);
    return source.slice(start, next === -1 ? undefined : start + 8 + next);
  }
  const status = (source: string, pattern: RegExp) => Number(pattern.exec(source)![1]);
  const fileCode = (code: string) =>
    Number(new RegExp(`^    "${code}": \\((\\d{3}), "[a-z_]+", (?:True|False)\\),$`, 'm').exec(WIRE_PY)![1]);

  it('names every header content.py puts on a download, with its value', () => {
    const body = /def safe_headers\([\s\S]*?\n    return headers/.exec(CONTENT_PY)![0];
    const set = new Map([...body.matchAll(/"([A-Za-z-]+)": (?:f?"([^"]*)"|content_disposition\(filename\))/g)].map((m) => [m[1], m[2] ?? 'attachment']));
    expect(CONTENT_PY).toContain('return f"attachment; filename=');
    expect(body).toContain('if etag:\n        headers["ETag"] = f\'"{etag}"\'');
    const phrase: Record<string, (value: string) => string> = {
      'Content-Disposition': (value) => `\`Content-Disposition: ${value}\``,
      'Accept-Ranges': (value) => `\`Accept-Ranges: ${value}\``,
      'X-Content-Type-Options': (value) => `\`X-Content-Type-Options: ${value}\``,
      'Content-Security-Policy': (value) => {
        expect(value).toMatch(/^sandbox;/);
        return 'a sandboxing `Content-Security-Policy`';
      },
      'Cache-Control': (value) => `\`Cache-Control: ${value}\``,
    };
    expect([...set.keys()].sort()).toEqual(Object.keys(phrase).sort());
    const download = flat(sectionOf(files.body, 'Download the original'));
    for (const [name, value] of set) expect(download, name).toContain(phrase[name](value));
    expect(download).toContain('with `ETag` (the sha256)');
    expect(ROUTES_PY).toMatch(/content\.file_response, path, filename=row\["filename"\], etag=row\["blob_sha256"\],/);
  });

  it('says a derived download has the same safe headers and its own type but no ETag and never a 304', () => {
    const derived = methodBody(ROUTES_PY, 'download_derived');
    expect(derived).toContain('content.file_response, path, filename=name, etag=None, media_type=media_type,');
    expect(derived).not.toContain('if_none_match');
    expect(derived).toContain('range_header=request.headers.get("range")');
    const types = new Map([...(/^CONTENT_TYPES = \{([\s\S]*?)^\}/m.exec(DERIVED_PY)![1]).matchAll(/"(\.[a-z]+)": "([^"]+)"/g)].map((m) => [m[1], m[2]]));
    expect([...types.keys()].sort()).toEqual(['.csv', '.jpg', '.json', '.md', '.png', '.srt', '.txt', '.vtt']);
    const paragraph = flat(sectionOf(files.body, 'Download the original'));
    expect(paragraph).toContain(
      'A [derived](#derived-data) download carries the same `Content-Disposition`, `Accept-Ranges`, `X-Content-Type-Options`, `Content-Security-Policy` and `Cache-Control`',
    );
    expect(paragraph).toContain('with its own type — text, Markdown, JSON, CSV, subtitles or PNG — and answers a `Range` the same way');
    expect(paragraph).toContain('It has **no `ETag`**, so `If-None-Match` never gets a `304` there.');
    expect(paragraph).not.toMatch(/Derived downloads carry the same headers/);
  });

  it('answers each kind of download request with the status content.py and wire.py send', () => {
    const whole = status(CONTENT_PY, /start, length, status = 0, size, (\d{3})/);
    const partial = status(CONTENT_PY, /length, status = end - start \+ 1, (\d{3})/);
    const notModified = status(CONTENT_PY, /if _etag_matches\(if_none_match, etag\):\n\s+return Response\(status_code=(\d{3})/);
    const unsatisfiable = status(WIRE_PY, /def range_not_satisfiable[\s\S]*?status=(\d{3}),/);
    // Several ranges are ignored (the whole body); a start past the end is unsatisfiable.
    expect(CONTENT_PY).toContain('if "," in value:\n        return None');
    expect(CONTENT_PY).toContain('if start >= size:\n        return UNSATISFIABLE');
    expect(CONTENT_PY).toContain('if wanted == UNSATISFIABLE:\n            raise wire.range_not_satisfiable(size)');
    expect(CONTENT_PY).toContain('headers["Content-Range"] = f"bytes {start}-{end}/{size}"');
    expect(WIRE_PY).toContain('extra_headers={"Content-Range": f"bytes */{int(size)}"},');

    const table = sectionOf(files.body, 'Download the original');
    const rows = new Map([...table.matchAll(/^\| (.+?) \| `(\d{3})`(.*)\|$/gm)].map((m) => [m[1], [Number(m[2]), m[3]] as [number, string]]));
    expect([...rows.keys()]).toEqual([
      'No `Range`', '`Range: bytes=0-1048575` (one range)', 'Several ranges', 'A range past the end', '`If-None-Match: "<sha256>"` matching the file',
    ]);
    expect(rows.get('No `Range`')![0]).toBe(whole);
    expect(rows.get('`Range: bytes=0-1048575` (one range)')).toEqual([partial, ' with `Content-Range`. ']);
    expect(rows.get('Several ranges')![0]).toBe(whole);
    expect(rows.get('A range past the end')).toEqual([unsatisfiable, ' with `Content-Range: bytes */<size>`. ']);
    expect(rows.get('`If-None-Match: "<sha256>"` matching the file')).toEqual([notModified, ', no body. ']);
    expect(sectionOf(files.body, 'Errors')).toContain(`| \`invalid_request_error\` | ${unsatisfiable} | A \`Range\` past the end of the file. |`);
  });

  it('requires Content-Length on a raw part with the status length_required sends', () => {
    const required = status(WIRE_PY, /def length_required\(\)[\s\S]*?status=(\d{3})/);
    expect(methodBody(ROUTES_PY, 'put_part')).toContain('if declared is None:\n                raise wire.length_required()');
    expect(methodBody(ROUTES_PY, 'add_part')).not.toContain('length_required');
    expect(flat(uploads.body)).toContain(`\`Content-Length\` is required (a \`${required}\` without it)`);
    expect(sectionOf(uploads.body, 'Errors')).toContain(`| \`invalid_request_error\` | ${required} | A raw \`PUT\` part without \`Content-Length\`. | No |`);
  });

  it('refuses a late part with upload_state_conflict and x-should-retry false, and lets a cancel be repeated', () => {
    const conflict = fileCode('upload_state_conflict');
    expect(WIRE_PY).toContain('def upload_state_conflict(message: str, *, retry_after: Optional[float] = None, should_retry: bool = False)');
    for (const handler of ['add_part', 'put_part']) {
      expect(methodBody(ROUTES_PY, handler), handler).toMatch(
        /if row\["status"\] != "pending" or row\.get\("lapsed"\):\n\s+raise wire\.upload_state_conflict\(f"This upload is \{wire\.upload_status\(row\)\}; it no longer accepts parts\."\)/,
      );
    }
    expect(flat(uploads.body)).toContain(
      `A part that arrives after \`complete\` or \`cancel\` is \`${conflict} upload_state_conflict\`, with \`x-should-retry: false\`.`,
    );

    // A second cancel: schema.py answers "already" with the row, and the route only refuses "gone" and "conflict".
    expect(SCHEMA_PY).toContain('if row["status"] == "cancelled":\n            return "already", row');
    const cancel = methodBody(ROUTES_PY, 'cancel_upload');
    expect(cancel).not.toContain('"already"');
    expect([...cancel.matchAll(/if state == "(\w+)":/g)].map((m) => m[1])).toEqual(['gone', 'conflict', 'cancelled']);
    expect(cancel).toContain('body = wire.upload_object(row or {})');
    expect(cancel).toContain('"This upload is being completed; it cannot be cancelled.", retry_after=2\n');
    const section = flat(sectionOf(uploads.body, 'Cancel an upload'));
    expect(section).toContain('returns the upload with `status: "cancelled"`. Cancelling again returns the same.');
    expect(section).toContain(`A completed upload cannot be cancelled (\`${conflict}\`)`);
    expect(section).toContain(`one being completed at that moment answers \`${conflict}\` with \`Retry-After: 2\``);
  });

  it('checks the scope before any file id is looked up, with the statuses insufficient_scope and file_not_found have', () => {
    const scope = status(ERRORS_PY, /"insufficient_scope": _CodeSpec\((\d{3}),/);
    const missing = fileCode('file_not_found');
    // File routes: _begin authorizes first, and every handler begins with _begin.
    expect(ROUTES_PY).toMatch(/async def _begin\(self[^\n]*\n\s+await self\.deps\.authorize\(request, caller, scope\)\n/);
    expect(handlerScopes().size).toBe(14);
    // Model input: the facade's contract is scope before resolution.
    expect(flat(SERVICE_PY)).toContain(
      'The router checks it BEFORE resolution, so a key that may not read files learns nothing about which ids exist (403 before 404).',
    );
    expect(flat(files.body)).toContain(`answers \`${scope} insufficient_scope\` on these routes`);
    expect(fileInputs.body).toContain('The scope is checked before any id is looked up, so a key\nwithout it learns nothing about which files exist.');
    const errorsTable = sectionOf(fileInputs.body, 'Errors');
    expect(errorsTable).toContain(`| ${scope} | \`insufficient_scope\` | A \`file_id\` in a request from a key without \`files.read\`. |`);
    expect(errorsTable).toContain(`| ${missing} | \`file_not_found\` |`);
    expect(errorsTable).not.toMatch(/\| (?!403 )\d{3} \| `insufficient_scope`/);
  });

  it('gives every id that is not a live file of the project the one 404 wire.py builds', () => {
    expect(WIRE_PY).toContain("ONE sentence for absent, malformed, deleted, expired and another\n    project's");
    const missing = fileCode('file_not_found');
    const isolation = flat(sectionOf(files.body, 'Retention and isolation'));
    expect(isolation).toContain(
      `A file id from another project, a deleted or expired file's id, a malformed id and an id that never existed all get the **same** \`${missing} file_not_found\`, byte for byte apart from the request id — the API does not confirm that someone else's file exists.`,
    );
    expect(isolation).not.toMatch(/\b403\b|\bor a 4\d\d\b/);
  });

  it('answers a background request that names a processing file with the 202 the router sends', () => {
    expect(ROUTER_PY.split('JSONResponse(status_code=202, content=_row_to_wire(row))').length - 1).toBeGreaterThanOrEqual(1);
    expect(fileInputs.body).toContain('| `"background": true` | Answers `202` at once; the response stays `queued` until its files are ready.');
  });

  it('gives every file and upload error row a status its factory can send', () => {
    const invalidStatuses = new Set([
      status(ERRORS_PY, /"invalid_request_error": _CodeSpec\((\d{3}),/),
      status(WIRE_PY, /def length_required\(\)[\s\S]*?status=(\d{3})/),
      status(WIRE_PY, /def range_not_satisfiable[\s\S]*?status=(\d{3}),/),
    ]);
    const tooLarge = status(ERRORS_PY, /"request_too_large": _CodeSpec\((\d{3}),/);
    for (const page of [files, uploads]) {
      for (const m of sectionOf(page.body, 'Errors').matchAll(/^\| `([a-z_]+)` \| (\d{3}) \|/gm)) {
        const [code, documented] = [m[1], Number(m[2])];
        if (code === 'invalid_request_error') expect(invalidStatuses.has(documented), `${page.slug}: ${code} ${documented}`).toBe(true);
        else if (code === 'request_too_large') expect(documented, page.slug).toBe(tooLarge);
        else expect(documented, `${page.slug}: ${code}`).toBe(fileCode(code));
      }
    }
  });
});

describe('what the edge in front of the API does to a slow part', () => {
  it('publishes no part time limit or upload speed until a run through the public URL has measured them', () => {
    for (const noTimeout of STATES) {
      for (const page of [uploadsPage({ noTimeout }), files]) {
        expect(page.body, `${page.slug} noTimeout=${noTimeout}`).not.toMatch(/Mbit|\b300 seconds\b|within about \d+ seconds/);
      }
      const slow = oneLine(/\*\*Slow links\.\*\*[\s\S]*?(?=\n\n)/.exec(uploadsPage({ noTimeout }).body)![0]);
      expect(slow, `noTimeout=${noTimeout}`).toContain('`524`');
      expect(slow).toContain('use 16 MiB parts');
      expect(slow).toContain('must start a new upload to change it');
    }
    expect(oneLine(uploadsPage({ noTimeout: false }).body)).toContain('with a `408`, or with a `524`');
  });
});

describe('the ceilings', () => {
  it('states every upload and file ceiling at the default limits.py reads, in bytes', () => {
    const pairs: [keyof typeof FILE_LIMITS, string][] = [
      ['singleMaxBytes', 'PUBLIC_API_FILES_SINGLE_MAX_BYTES'],
      ['singleMaxBodyBytes', 'PUBLIC_API_FILES_MAX_BODY_BYTES'],
      ['partMaxBytes', 'PUBLIC_API_FILES_PART_MAX_BYTES'],
      ['maxParts', 'PUBLIC_API_FILES_MAX_PARTS'],
      ['uploadMaxBytes', 'PUBLIC_API_FILES_UPLOAD_MAX_BYTES'],
      ['listMaxLimit', 'PUBLIC_API_FILES_LIST_MAX_LIMIT'],
      ['jsonMaxBytes', 'PUBLIC_API_FILES_JSON_MAX_BYTES'],
      ['pdfMaxBytes', 'PUBLIC_API_FILES_PDF_MAX_BYTES'],
      ['pdfMaxPages', 'PUBLIC_API_FILES_PDF_MAX_PAGES'],
      ['ocrPageBudget', 'PUBLIC_API_FILES_OCR_PAGE_BUDGET'],
      ['officeMaxBytes', 'PUBLIC_API_FILES_OFFICE_MAX_BYTES'],
      ['xlsxMaxBytes', 'PUBLIC_API_FILES_XLSX_MAX_BYTES'],
      ['textMaxBytes', 'PUBLIC_API_FILES_TEXT_MAX_BYTES'],
      ['imageMaxBytes', 'PUBLIC_API_FILES_IMAGE_MAX_BYTES'],
      ['imageMaxPixels', 'PUBLIC_API_FILES_IMAGE_MAX_PIXELS'],
      ['mediaMaxSeconds', 'PUBLIC_API_FILES_MEDIA_MAX_SECONDS'],
      ['indexMaxChunks', 'PUBLIC_API_FILES_INDEX_MAX_CHUNKS'],
      ['tombstoneDays', 'PUBLIC_API_FILES_TOMBSTONE_DAYS'],
    ];
    for (const [key, setting] of pairs) {
      expect(FILE_LIMITS[key], setting).toBe(limitDefault(setting));
    }
    expect(WIRE_PY).toContain(`EXPIRES_MIN_S = ${FILE_LIMITS.expiresMinSeconds}`);
    expect(WIRE_PY).toContain(`EXPIRES_MAX_S = ${String(FILE_LIMITS.expiresMaxSeconds).replace(/\B(?=(\d{3})+$)/g, '_')}`);

    const filesLimits = sectionOf(files.body, 'Limits and why');
    for (const value of [
      FILE_LIMITS.singleMaxBytes, FILE_LIMITS.singleMaxBodyBytes, FILE_LIMITS.expiresMinSeconds,
      FILE_LIMITS.expiresMaxSeconds, FILE_LIMITS.listMaxLimit, FILE_LIMITS.pdfMaxPages,
      FILE_LIMITS.ocrPageBudget, FILE_LIMITS.imageMaxPixels, FILE_LIMITS.mediaMaxSeconds,
      FILE_LIMITS.indexMaxChunks,
    ]) {
      expect(filesLimits, String(value)).toContain(grouped(value));
    }
    const uploadLimits = sectionOf(uploads.body, 'Limits and why');
    for (const value of [FILE_LIMITS.partMaxBytes, FILE_LIMITS.singleMaxBodyBytes, FILE_LIMITS.maxParts, FILE_LIMITS.uploadMaxBytes]) {
      expect(uploadLimits, String(value)).toContain(grouped(value));
    }
    // The Upload object advertises the same two ceilings.
    const created = jsonSamples(uploads.body).find((s) => s.object === 'upload')!;
    expect(created.part_max_bytes).toBe(FILE_LIMITS.partMaxBytes);
    expect(created.max_parts).toBe(FILE_LIMITS.maxParts);
  });

  it('states the pending-upload expiry and the record retention limits.py applies', () => {
    expect(limitDefault('PUBLIC_API_UPLOAD_IDLE_TTL_HOURS')).toBe(24);
    expect(limitDefault('PUBLIC_API_UPLOAD_MAX_TTL_HOURS')).toBe(168);
    expect(limitDefault('PUBLIC_API_UPLOAD_RECORD_TTL_DAYS')).toBe(30);
    expect(limitDefault('PUBLIC_API_FILES_SWEEP_INTERVAL_S')).toBe(600);
    const states = sectionOf(uploads.body, 'States and expiry');
    expect(states).toContain('**24 hours after the last part that arrived**');
    expect(states).toContain('**7 days**');
    expect(states).toContain('read back for 30 days');
    expect(files.body).toContain('within about\nten minutes of that time');
    expect(files.body).toContain(`What stays, for ${FILE_LIMITS.tombstoneDays} days`);
  });

  it('states the model-input ceilings at the defaults limits.py, context.py and inline.py read', () => {
    const inputs = fileInputs.body;
    expect(limitDefault('PUBLIC_API_FILES_INLINE_MAX_TOKENS')).toBe(100_000);
    expect(limitDefault('PUBLIC_API_FILES_RETRIEVAL_TOKENS')).toBe(32_000);
    expect(limitDefault('PUBLIC_API_FILES_RETRIEVAL_MAX_TOKENS')).toBe(200_000);
    expect(limitDefault('PUBLIC_API_FILES_MAX_PER_REQUEST')).toBe(20);
    expect(limitDefault('PUBLIC_API_FILES_VIDEOS_PER_REQUEST')).toBe(3);
    expect(limitDefault('PUBLIC_API_FILES_VIDEO_FRAMES')).toBe(3);
    expect(limitDefault('PUBLIC_API_FILES_VIDEO_FRAMES_HIGH')).toBe(8);
    expect(limitDefault('PUBLIC_API_FILES_PDF_VISION_PAGES')).toBe(2);
    expect(limitDefault('PUBLIC_API_FILES_PDF_VISION_PAGES_HIGH')).toBe(6);
    expect(limitDefault('PUBLIC_API_INLINE_SYNC_MAX_OCR_PAGES')).toBe(8);
    expect(INLINE_PY).toContain('_setting_int("PUBLIC_API_FILES_INLINE_OCR_MAX_PAGES", 40)');
    expect(INLINE_PY).toContain('_setting_int("PUBLIC_API_FILES_INLINE_AUDIO_MAX_SECONDS", 300)');
    expect(CONTEXT_PY).toContain('AROUND_RADIUS_S = 45.0');
    expect(CONTEXT_PY).toContain('AROUND_MAX_TIMES = 3');
    expect(CONTEXT_PY).toContain('u.chapters[:30]');
    expect(SERVICE_PY).toContain('return 30.0 if any(r.media for r in records) else 5.0');
    expect(SERVICE_PY).toContain('setting_float("PUBLIC_API_FILES_SYNC_PREPARE_BUDGET_S", 45.0)');
    expect(limitDefault('PUBLIC_API_FILES_SYNC_READY_WAIT_S')).toBe(30);

    const table = sectionOf(inputs, 'Limits and why');
    for (const fragment of [
      '| File parts per request | 20 |', '| Audio and video per request | 3 |',
      '| Automatic inlining | 100,000 estimated tokens', '| Retrieval budget | 32,000 tokens by default, 200,000 at most |',
      '| Page images per PDF | 2 (`auto`), 6 (`high`) |', '| Frames per video | 3 (`auto`), 8 (`high`) |',
      '| OCR for inline PDFs | 8 pages synchronous, 40 streamed or background |', '| `input_audio` | 300 seconds',
    ]) {
      expect(table).toContain(fragment);
    }
    expect(inputs).toContain('45 seconds either side of up\n   to three times');
    expect(inputs).toContain('up to 30\n   chapters');
    expect(fileInputsPage({ noTimeout: false }).body).toContain('`Retry-After: 5` (documents, tables, images) or `Retry-After: 30` (audio, video)');
  });

  it('derives the techsara-8b-vision file budgets from the context builder arithmetic and the published window', () => {
    const reserve = Number(/^WINDOW_RESERVE_TOKENS = (\d+)$/m.exec(CONTEXT_PY)![1]);
    expect(CONTEXT_PY).toContain('if window <= 131_072:\n            budget = min(budget, window // 2)');
    const catalogue = findDocPage('models')!.body;
    const vision = JSON.parse(fencedBlocks(catalogue, 'json').find((b) => b.includes('"object": "list"'))!).data.find(
      (model: { id: string }) => model.id === VISION_MODEL_ID,
    );
    const window = vision.context_window as number;
    const output = vision.default_max_output_tokens as number;
    const inline = Math.min(100_000, window - output - reserve);
    const retrieval = Math.min(32_000, Math.floor(window / 2), window - output - reserve);
    const section = sectionOf(fileInputs.body, 'Documents: inline or retrieved');
    expect(section).toContain(`files inline up to ${grouped(inline)} tokens`);
    expect(section).toContain(`retrieval\npacks at most ${grouped(retrieval)}`);
    expect(fileInputs.body).toContain(`within a ${grouped(window)}-token window`);
    expect(section).toContain(`a ${grouped(reserve)}-token reserve`);
  });
});

describe('processing, derived data and events', () => {
  it('lists the stages of each kind exactly as sniff.py names them', () => {
    const block = /^STAGES_BY_KIND = \{([\s\S]*?)^\}/m.exec(SNIFF_PY)!;
    const stages = new Map(
      [...block[1].matchAll(/"([a-z]+)": \(([^)]*)\)/g)].map((m) => [m[1], [...m[2].matchAll(/"([a-z]+)"/g)].map((s) => s[1])]),
    );
    const table = sectionOf(files.body, 'Kinds and what processing does');
    const rows = [...table.matchAll(/^\| `([a-z]+)` \| [^|]+ \| ((?:`[a-z]+`(?:, )?)+) \|$/gm)];
    expect(rows.map((m) => m[1]).sort()).toEqual([...stages.keys()].filter((kind) => kind !== 'unknown').sort());
    for (const [, kind, listed] of rows) {
      expect([...listed.matchAll(/`([a-z]+)`/g)].map((m) => m[1]), kind).toEqual(stages.get(kind));
    }
  });

  it('lists exactly the facts jobs.py lets onto the wire, per kind', () => {
    const block = /^_FACT_KEYS: Dict\[str, Tuple\[str, \.\.\.\]\] = \{([\s\S]*?)^\}/m.exec(JOBS_PY)!;
    const facts = new Map(
      [...block[1].matchAll(/"([a-z]+)": \(([^)]*)\)/g)].map((m) => [m[1], [...m[2].matchAll(/"([a-z_]+)"/g)].map((s) => s[1])]),
    );
    const indexed = pythonStrings(JOBS_PY, '_INDEXED_KEYS');
    const section = sectionOf(files.body, 'Kinds and what processing does');
    const documented = new Map<string, string[]>();
    for (const m of section.matchAll(/^\| ((?:`[a-z]+`(?:, )?)+) \| ((?:`[a-z_]+`(?:, )?)+) \|$/gm)) {
      const keys = [...m[2].matchAll(/`([a-z_]+)`/g)].map((k) => k[1]);
      for (const kind of [...m[1].matchAll(/`([a-z]+)`/g)].map((k) => k[1])) documented.set(kind, keys);
    }
    expect([...documented.keys()].sort()).toEqual([...facts.keys()].sort());
    for (const [kind, keys] of facts) expect(documented.get(kind), kind).toEqual(keys);
    for (const key of indexed) expect(section).toContain(`\`${key}\``);
  });

  it('lists the derived names derived.py serves, and no other', () => {
    const audio = pythonStrings(DERIVED_PY, '_AUDIO_NAMES');
    const video = pythonStrings(DERIVED_PY, '_VIDEO_EXTRA');
    expect(DERIVED_PY).toContain('TEXT_NAME = "text.txt"');
    expect(DERIVED_PY).toContain('PAGES_JSON_NAME = "pages.json"');
    expect(DERIVED_PY).toContain('IMAGE_NAME = "image.png"');
    expect(DERIVED_PY).toContain('_SHEET_RE = re.compile(r"^sheet-([1-9][0-9]{0,3})\\.csv$")');
    const table = sectionOf(files.body, 'Derived data');
    for (const name of [...audio, ...video, 'text.txt', 'pages.json', 'image.png', 'profile.json', 'sheet-1.csv']) {
      expect(table, name).toContain(`\`${name}\``);
    }
    const named = new Set([...table.matchAll(/`([a-z0-9_-]+\.(?:txt|json|png|csv|srt|vtt|md))`/g)].map((m) => m[1]));
    const allowed = new Set([...audio, ...video, 'text.txt', 'pages.json', 'image.png', 'profile.json']);
    for (const name of named) {
      expect(allowed.has(name) || /^sheet-[1-9]\d*\.csv$/.test(name), name).toBe(true);
    }
    // text.txt's unit markers are derived.py's own words.
    expect(DERIVED_PY).toContain('_UNIT_WORDS = {"page": "Page", "section": "Section", "slide": "Slide", "rows": "Rows"}');
  });

  it('frames file events the way events.py writes them: named, numbered from 1, one terminal', () => {
    const names = [...EVENTS_PY.matchAll(/^FILE_[A-Z]+ = "(file\.[a-z]+)"$/gm)].map((m) => m[1]);
    expect(names.sort()).toEqual(['file.failed', 'file.processed', 'file.processing']);
    expect(EVENTS_PY).toContain('HEARTBEAT_S = 15.0');
    expect(EVENTS_PY).toContain('return ": ping\\n\\n"');

    const sample = fencedBlocks(sectionOf(files.body, 'Progress events'), 'text')[0];
    const frames = [...sample.matchAll(/^event: (\S+)\ndata: (\{.*\})$/gm)].map((m) => ({ event: m[1], data: JSON.parse(m[2]) }));
    expect(frames.length).toBe(2);
    frames.forEach((frame, i) => {
      expect(names).toContain(frame.event);
      expect(Object.keys(frame.data)).toEqual(['type', 'sequence_number', 'data']);
      expect(frame.data.type).toBe(frame.event);
      expect(frame.data.sequence_number).toBe(i + 1);
      expect(frame.data.data.object).toBe('file');
    });
    expect(frames.filter((frame) => frame.event !== 'file.processing')).toHaveLength(1);
    expect(sample).toContain(': ping');
    expect(files.body).toContain('its `status` is `"deleted"`');
  });

  it('shows the progress comment in the shape service.py writes it', () => {
    expect(SERVICE_PY).toContain('text = f"file {record.file_id} {stage}"');
    expect(SERVICE_PY).toContain('text += f" {max(0, min(100, int(record.percent)))}%"');
    expect(SERVICE_PY).toContain('"transcript"');
    expect(fileInputs.body).toContain(`\`: file ${EXAMPLE_FILE_ID} transcript 40%\``);
  });
});

describe('what processing, retries and refusals really do', () => {
  /** Whitespace collapsed, so a sentence can be matched across the page's line breaks. */
  const flat = (text: string) => text.replace(/\s+/g, ' ');

  /** A top-level Python function, from its `def` to the next top-level statement. */
  function functionBody(source: string, anchor: string): string {
    const start = source.indexOf(anchor);
    expect(start, anchor).toBeGreaterThanOrEqual(0);
    const rest = source.slice(start + anchor.length);
    const end = rest.search(/\n(?:def |async def |class |@|[A-Za-z_]+\s*(?::[^\n=]+)?=)/);
    return source.slice(start, end === -1 ? undefined : start + anchor.length + end);
  }

  it('says a scanned PDF whose OCR could not finish still ends processed, as ocr_pages.py makes it', () => {
    expect(JOBS_PY).toContain('final_attempt = int(ctx.blob.get("attempt") or 0) + 1 >= ctx.runner.max_attempts');
    expect(OCR_PAGES_PY).toMatch(/if not final_attempt:\n\s+raise\n\s+gate_refused = True/);
    expect(OCR_PAGES_PY).toContain('"ocr_failed_pages": len(failed_pages),');
    expect(OCR_PAGES_PY).toContain('facts["ocr_disabled"] = True');
    expect(OCR_PAGES_PY).toContain('record["source"] = "ocr"');
    // Neither count reaches the wire, so the pages must not promise them.
    const pdfFacts = /^\s+"pdf": \(([^)]*)\),$/m.exec(JOBS_PY)![1];
    for (const hidden of ['ocr_failed_pages', 'ocr_disabled']) {
      expect(pdfFacts).not.toContain(hidden);
      for (const page of newPages(false)) expect(page.body, page.slug).not.toContain(hidden);
    }
    const facts = flat(sectionOf(files.body, 'Kinds and what processing does'));
    expect(facts).toContain('**OCR is best effort.**');
    expect(facts).toContain('or every scanned page when OCR is not available on the service');
    expect(facts).toContain('a scanned PDF can end `processed` with some pages holding only their (nearly empty) text layer');
    expect(facts).toContain('a page read by OCR has `"source": "ocr"`');
    expect(flat(files.body)).toContain('OCR is the exception: on the last attempt a PDF ends `processed` with the pages OCR managed to read');
    const scanned = flat(sectionOf(fileInputs.body, 'Scanned documents'));
    expect(scanned).toContain('a page it did not read does not fail the file');
    expect(scanned).toContain('the file still ends `processed`');
    expect(scanned).not.toContain('there is nothing to ask for. In `pages.json`, each page');
  });

  it('tells a reader to delete before uploading failed bytes again, because a re-upload joins the failed copy', () => {
    const lock = functionBody(SCHEMA_PY, 'def _lock_or_create_blob(');
    expect(lock).toContain('ON CONFLICT (project_id, sha256) DO NOTHING');
    expect(lock).toContain('if row["status"] == "deleting":');
    expect(lock, 'a join now resets a failed copy: rewrite "Retrying a failed file"').not.toMatch(/'queued'|'failed'|"failed"/);
    expect(QUEUE_PY.split('if created:\n        _notify_enqueued(blob)').length - 1).toBe(2);
    expect(SERVICE_PY).toContain('"Upload the file again later."');

    const retry = files.body.slice(files.body.indexOf('### Retrying a failed file\n'));
    expect(retry.length).toBeLessThan(files.body.length);
    expect(flat(retry)).toContain('"upload the file again" means **delete first**');
    expect(flat(retry)).toContain('gives a new file id that is already `status: "error"` with the same code');
    expect(flat(retry)).toContain('`DELETE` every one whose `sha256` is the failed file\'s');
    expect(flat(sectionOf(files.body, 'Retention and isolation'))).toContain(
      'a re-upload of bytes whose processing failed is `error` immediately',
    );
  });

  it('warns that derived data of a failed file answers a retryable 409 for good, as routes.py and wire.py do', () => {
    const refusal = 'if row.get("blob_status") != "processed":\n                raise wire.file_not_ready(5)';
    expect(ROUTES_PY.split(refusal).length - 1).toBe(2);
    expect(WIRE_PY).toContain('"file_not_ready": (409, "invalid_request_error", True)');
    const derived = flat(sectionOf(files.body, 'Derived data'));
    expect(derived).toContain('and so is asking for a file whose `status` is `error`, which will never have any');
    expect(derived).toContain("**read the file's `status` first**");
    const row = /^\| `file_not_ready` \| 409 \|.*$/m.exec(files.body)![0];
    expect(row).toContain('never for derived data of a file in `error`');

  });

  it('quotes the two sentences a download of a failed assembly really sends, which differ from the file object', () => {
    // routes.py builds the 400 from wire.py's default view, not the configured
    // processing view: its sentence for a non-checksum failure is wire.py's
    // `assembly_failed`, while the File object says jobs.py's internal_error.
    expect(ROUTES_PY).toContain('"This file has no content: " + wire.default_processing_view(row)["status_details"],\n                    param="file_id",');
    expect(WIRE_PY).toContain(
      'sentence = STATUS_DETAILS["checksum_mismatch"] if file_error == "checksum_mismatch" else STATUS_DETAILS["assembly_failed"]',
    );
    const detail = (key: string) => new RegExp(`^    "${key}": "([^"]+)",$`, 'm').exec(WIRE_PY)![1];
    const download = flat(sectionOf(files.body, 'Download the original'));
    expect(download).toContain('`400 invalid_request_error` with `param: "file_id"` and one of two sentences');
    expect(download).toContain(`"This file has no content: ${detail('checksum_mismatch')}" after a checksum mismatch`);
    expect(download).toContain(`"This file has no content: ${detail('assembly_failed')}" after any other assembly failure`);
    expect(download).not.toContain('followed by its `status_details`');
  });

  it('describes the name and the declared type as tie-breakers for text only, as sniff.py uses them', () => {
    expect(SNIFF_PY).toContain('if suffix in (".csv", ".tsv") or hint in ("text/csv", "text/tab-separated-values"):');
    expect(SNIFF_PY).toContain('if suffix in (".jsonl", ".ndjson") or hint in ("application/x-ndjson", "application/jsonl"):');
    expect(SNIFF_PY).toContain('if stripped.startswith("{") and (suffix == ".json" or hint == "application/json"):');
    expect(SNIFF_PY).toContain('if suffix in (".md", ".markdown") or hint == "text/markdown":');
    expect(SNIFF_PY).toContain('if delimiter is not None and suffix not in (".md", ".txt"');
    expect(SNIFF_PY).toMatch(/^_MACRO_SUFFIXES = \([^)]*"\.docm"/m);
    for (const page of [files, uploads]) {
      expect(page.body, page.slug).not.toMatch(/never used to decide what the file is|Advisory only|not the name, and not the type/);
    }
    const kinds = flat(sectionOf(files.body, 'Kinds and what processing does'));
    expect(kinds).toContain('The name and the type your client sent only break ties between text formats');
    expect(kinds).toContain('a `.txt` or `.md` name keeps comma-separated text from being read as a table');
    expect(uploads.body).toContain('this type and the filename only break ties between text formats');
  });

  it('states the reading time and memory ceilings kind_caps and the extraction child apply', () => {
    const wall = (kind: string) => {
      const anchor = `"${kind}": KindCaps`;
      const at = LIMITS_PY.indexOf(anchor);
      expect(at, kind).toBeGreaterThanOrEqual(0);
      return Number(/wall_s=([\d.]+)/.exec(callArguments(LIMITS_PY, at + anchor.length))![1]);
    };
    const words = (seconds: number) =>
      seconds % 3600 === 0 ? (seconds === 3600 ? '1 hour' : `${seconds / 3600} hours`) : `${seconds / 60} minutes`;
    const limits = sectionOf(files.body, 'Limits and why');
    const row = /^\| Reading one file \|.*$/m.exec(limits)![0];
    expect(wall('presentation')).toBe(wall('document'));
    for (const kind of ['spreadsheet', 'text', 'html']) expect(wall(kind), kind).toBe(wall('pdf'));
    expect(row).toContain(`${words(wall('image'))} for an image`);
    expect(row).toContain(`${words(wall('document'))} for DOCX and PPTX`);
    expect(row).toContain(`${words(wall('pdf'))} for PDF, XLSX, text and HTML`);
    expect(row).toContain(`${words(wall('tabular'))} for CSV and the other tables`);
    expect(LIMITS_PY).toContain('_float("PUBLIC_API_FILES_EXTRACT_RLIMIT_AS_GIB", 8.0)');
    expect(row).toContain('8 GiB of memory for any of them');
    expect(LIMITS_PY).toContain('"tabular": KindCaps(bytes=_int("PUBLIC_API_FILES_TABULAR_MAX_BYTES", upload_max)');
    expect(limits).toContain('| CSV, TSV, Parquet, JSON Lines | 100 GiB, the upload ceiling |');
    expect(EXTRACT_WORKER_PY).toContain('FileTooComplex("it needs more time or memory than the processing ceiling")');
    expect(flat(limits)).toContain(
      '"The file exceeds a processing ceiling: it needs more time or memory than the processing ceiling."',
    );
  });

  it('gives a request naming a failed file the fixed sentence of its code, which leaves out the ceiling', () => {
    expect(SERVICE_PY).toContain('"file_too_complex": "The file exceeds a processing ceiling.",');
    expect(SERVICE_PY).toContain('sentence = FAILURE_SENTENCES.get(record.error_code or "", FAILURE_SENTENCES["internal_error"])');
    expect(flat(fileInputs.body)).toContain('except for `file_too_complex`, whose `400` leaves out which ceiling it was');
    expect(flat(files.body)).toContain('for `file_too_complex`, "The file exceeds a processing ceiling." without the ceiling itself');
    for (const page of newPages(false)) expect(flat(page.body), page.slug).not.toMatch(/with that same sentence|the file's own sentence/);
  });

  it('lists the synchronous budget refusals while the budget exists, and counts input_audio among the recordings', () => {
    expect(SERVICE_PY).toContain('raise errors.model_at_capacity(TRANSCRIBE_RETRY_AFTER_S) from None');
    expect(SERVICE_PY).toContain('TRANSCRIBE_RETRY_AFTER_S = 30.0');
    expect(ERRORS_PY).toMatch(/def model_at_capacity\(retry_after: float\) -> ApiError:[\s\S]{0,900}?"model_unavailable",\n\s+"The model is at capacity right now\./);
    expect(ERRORS_PY).toContain('"model_unavailable": _CodeSpec(503,');
    expect(INLINE_PY).toContain('"This inline file takes too long to read in a synchronous request; "');
    const now = sectionOf(fileInputsPage({ noTimeout: false }).body, 'Errors');
    const later = sectionOf(fileInputsPage({ noTimeout: true }).body, 'Errors');
    for (const fragment of ['| 503 | `model_unavailable` |', '`Retry-After: 30`', '| 400 | `invalid_request_error` | An inline `file_data` that could not be read']) {
      expect(now).toContain(fragment);
      expect(later).not.toContain(fragment);
    }
    expect(SERVICE_PY).toContain('audio_parts = [r for r in lifted.refs if r.part_type in ("input_video", "input_audio")]');
    expect(flat(fileInputs.body)).toContain('An `input_audio` clip counts as a file part and as one of the three.');
  });

  it('says what a long prompt costs without describing how it is scheduled against other requests', () => {
    const scheduling =
      /one at a time|one-at-a-time|dedicated lane|\blanes?\b|long-context|scheduled separately|goes first|admission|prioriti[sz]|\b13[01],\d{3}\b|\b131k\b|steps? aside|pause[sd]? a/i;
    for (const noTimeout of STATES) {
      for (const page of ownPages(noTimeout)) {
        expect(flat(`${page.summary} ${page.body}`), `${page.slug} noTimeout=${noTimeout}`).not.toMatch(scheduling);
      }
    }
    expect(flat(fileInputs.body)).toContain('measured at 878 seconds for about 950,000 tokens');
  });

  it('says a streamed or background inline PDF past 40 scanned pages is sent with only its text layer and no error, as only a synchronous one is refused', () => {
    const code = pythonCode(INLINE_PY);
    expect(code).toContain('strict = delivery == "sync"');
    expect(code).toContain('limit = sync_max_ocr_pages() if strict else max_ocr_pages()');
    const refusal = code.search(/if thin and strict and len\(thin\) > int\(max_ocr\):\n\s+raise NeedsMoreOcr\(len\(thin\)\)/);
    expect(refusal).toBeGreaterThan(0);
    // The only refusal, and it comes before any page is read.
    expect(code.split('raise NeedsMoreOcr').length - 1).toBe(1);
    expect(refusal).toBeLessThan(code.indexOf('ocr_pages.run_stage('));
    expect(code).toContain('budget=int(max_ocr)');
    // The budget takes thin pages in page order; the rest are skipped, not refused.
    expect(OCR_PAGES_PY).toContain('ordered = sorted({int(p) for p in thin_pages if int(p) >= 1})');
    expect(OCR_PAGES_PY).toContain('within, beyond = ordered[:budget], ordered[budget:]');

    for (const noTimeout of STATES) {
      const inline = flat(sectionOf(fileInputsPage({ noTimeout }).body, 'Inline file_data'));
      expect(inline).toContain(
        'A **synchronous** request with more than 8 scanned pages is refused with a `400` pointing to `/v1/files`, before any page is read.',
      );
      expect(inline).toContain(
        'A **streamed or background** request is never refused for it: OCR reads the first 40 scanned pages, in page order, and **every scanned page after those reaches the model as its text layer — usually nearly empty — with no error**.',
      );
      expect(inline).not.toMatch(/40 for streaming or background\. More is a `400`/);
      const row = /^\| OCR for inline PDFs \|.*$/m.exec(fileInputsPage({ noTimeout }).body)![0];
      expect(row).toContain('past 40, a streamed or background request sends the remaining scanned pages with only their text layer, and no error');
    }
  });

  it('promises a file webhook only when processing of its bytes ends, since a file that joins processed bytes enqueues nothing', () => {
    const jobs = pythonCode(JOBS_PY);
    const emitters = [...jobs.matchAll(/await _emit_file_events\(/g)].map((m) => enclosingDefs(jobs, m.index!)[0]).sort();
    expect(emitters).toEqual(['_after_terminal', '_run_assembly']);
    expect(JOBS_PY).toContain('files = await db.run_in_thread(schema.live_files_for_blob, str(row["id"]))');
    expect(JOBS_PY).toContain('if result.outcome in ("checksum_mismatch", "failed") and result.file and self.emit_webhooks:');
    // A create or an assembly that joins an existing blob does not enqueue it, and nothing else emits.
    expect(QUEUE_PY.split('if created:\n        _notify_enqueued(blob)').length - 1).toBe(2);
    for (const source of [QUEUE_PY, ROUTES_PY, SCHEMA_PY]) expect(pythonCode(source)).not.toMatch(/emit_file_event/);

    const wait = flat(sectionOf(files.body, 'Wait for processing'));
    expect(wait).not.toMatch(/once per file|fire once/);
    expect(wait).toContain('**A file whose bytes this project had already processed gets no webhook**');
    expect(wait).toContain('or, from an upload, the moment its parts are assembled');
    expect(wait).toContain(
      'wait for a webhook only while its `status` is `uploaded` and its `processing.stage` is past `assemble`; otherwise act on the `status` you read.',
    );
  });

  it('names the SDK versions the raw part samples need, and starts over when a saved upload has lost its parts', () => {
    expect(ROUTES_PY).toContain('raise wire.upload_state_conflict(f"This upload is {wire.upload_status(row)}; it no longer accepts parts.")');
    expect(flat(uploads.body)).toContain('it needs version 2.16.0 or later of the `openai` Python package');
    expect(flat(uploads.body)).toContain('needs version 5 or later of the `openai` Node package');
    const resumable = sectionOf(uploads.body, 'A resumable upload');
    const python = fencedBlocks(resumable, 'python')[0];
    const lost = python.indexOf('if state["status"] in ("expired", "cancelled"):');
    expect(lost).toBeGreaterThan(0);
    expect(python.indexOf('upload_id = new_upload()', lost)).toBeGreaterThan(lost);
    expect(lost).toBeLessThan(python.indexOf('client.put('));
    const shell = fencedBlocks(resumable, 'bash')[1];
    expect(shell).toContain('expired|cancelled) rm upload.id;');
    expect(shell.indexOf('expired|cancelled')).toBeLessThan(shell.indexOf('-T "$part"'));
  });

  it('tells raw clients that a committed synchronous call can also end without a complete JSON object', () => {
    const sentence = 'Your own client should do the same with a body that holds no complete JSON object';
    expect(flat(longOutputPage({ noTimeout: true }).body)).toContain(sentence);
    expect(flat(longOutputPage({ noTimeout: false }).body)).not.toContain(sentence);
  });
});

describe('citations', () => {
  it('prints file_citation annotations with only the fields citations.py writes, at a UTF-16 index that is right', () => {
    expect(CITATIONS_PY).toMatch(/"type": "file_citation",\n\s+"file_id": fields\["file_id"\],\n\s+"filename": fields\["filename"\],\n\s+"index": offsets\[found\.start\],/);
    expect(CITATIONS_PY).toContain('annotation["page"] = fields["page"]');
    expect(CITATIONS_PY).toContain('annotation["timestamp_s"] = fields["timestamp_s"]');
    const allowed = ['type', 'file_id', 'filename', 'index', 'page', 'timestamp_s'];

    const annotations: JsonObject[] = [];
    for (const sample of jsonSamples(fileInputs.body)) {
      if (sample.type === 'file_citation') annotations.push(sample);
      for (const item of sample.output ?? []) {
        for (const part of item.content ?? []) {
          for (const annotation of part.annotations ?? []) {
            annotations.push(annotation);
            // index counts UTF-16 code units up to the marker's bracket: a JS index.
            expect(part.text.indexOf('[')).toBe(annotation.index);
          }
        }
      }
    }
    expect(annotations.length).toBeGreaterThanOrEqual(2);
    for (const annotation of annotations) {
      for (const key of Object.keys(annotation)) expect(allowed).toContain(key);
      expect('page' in annotation !== 'timestamp_s' in annotation).toBe(true);
    }
  });

  it('prints only labels the citation parser accepts, one per unit it knows', () => {
    // A port of citations._UNIT_RE: the label, a space, then the unit.
    const unit = /\s(?:p\.\s?\d{1,6}|§\s?\d{1,6}|slide\s\d{1,6}|rows\s\d{1,9}\s?[-–]\s?\d{1,9}|\d{1,2}:\d{2}(?::\d{2})?)\s*$/;
    expect(CITATIONS_PY).toContain('r"|(?P<slide>slide\\s(?P<slide_n>\\d{1,6}))"');
    const table = sectionOf(fileInputs.body, 'Citations');
    const labels = [...table.matchAll(/^\| [^|]+ \| `\[([^\]]+)\]` \|$/gm)].map((m) => m[1]);
    expect(labels).toHaveLength(5);
    for (const label of labels) expect(label, label).toMatch(unit);
    // And the model is told the same labels.
    for (const example of ['[report.pdf p.12]', '[notes.docx §3]', '[deck.pptx slide 4]', '[data.xlsx rows 201-400]', '[call.mp4 12:05]']) {
      expect(CONTEXT_PY).toContain(example);
    }
  });

  it('names the streamed annotation event the citations module emits', () => {
    expect(CITATIONS_PY).toContain('"type": "response.output_text.annotation.added"');
    expect(fileInputs.body).toContain('`response.output_text.annotation.added`');
    expect(CITATIONS_PY).toContain('def chat_message_annotations(');
    expect(fileInputs.body).toContain('`choices[0].message.annotations`');
  });
});

describe('what the pages say about models', () => {
  it('uses only the six public model ids, in samples and in SDK calls', () => {
    for (const noTimeout of STATES) {
      for (const page of ownPages(noTimeout)) {
        for (const m of page.body.matchAll(/"model":\s*"([^"]+)"|\bmodel="([^"]+)"/g)) {
          expect(MODEL_IDS as readonly string[], `${page.slug}`).toContain(m[1] ?? m[2]);
        }
      }
    }
  });

  it('gives techsara-ocr exactly one image, as the context builder refuses anything else', () => {
    expect(CONTEXT_PY).toContain('if len(files) != 1 or files[0].kind != KIND_IMAGE:');
    expect(fileInputs.body).toMatch(/`techsara-ocr` \| \*\*Exactly one image\*\*/);
  });

  it('sends page images for PDFs only, as the production page renderer does', () => {
    expect(CONTEXT_PY).toContain('if file.kind != "pdf" or not file.derived_dir:');
    expect(fileInputs.body).toContain('A presentation is sent as its slide text and speaker notes,\nwithout images.');
  });

  it('refuses file_url and never fetches it, as the lift refuses it', () => {
    expect(SERVICE_PY).toContain('"Files are not fetched from URLs; upload the file with /v1/files and pass its file_id."');
    expect(fileInputs.body).toContain('`file_url` is\n  refused: files are never fetched from a URL');
  });
});

describe('what a reader copies', () => {
  it('sets TECHSARA_API_KEY before any shell sample uses it', () => {
    for (const noTimeout of STATES) {
      for (const page of ownPages(noTimeout)) {
        let exported = false;
        for (const block of fencedBlocks(page.body, 'bash')) {
          const exportAt = block.search(/^export TECHSARA_API_KEY=/m);
          const useAt = block.search(/\$TECHSARA_API_KEY/);
          if (useAt >= 0) expect(exported || (exportAt >= 0 && exportAt < useAt), page.slug).toBe(true);
          if (exportAt >= 0) exported = true;
        }
      }
    }
  });

  it('prints no credential but the example keys the validator refuses', () => {
    for (const noTimeout of STATES) {
      for (const page of ownPages(noTimeout)) {
        for (const m of page.body.matchAll(/tsk_(?:live|test)_[A-Za-z0-9_-]+/g)) {
          expect(EXAMPLE_KEYS as readonly string[], page.slug).toContain(m[0]);
        }
        expect(page.body).not.toMatch(/whsec_[A-Za-z0-9]{8,}/);
      }
    }
  });

  it('recommends SDK timeouts that wait, never the zero that fails at once', () => {
    const future = longOutputPage({ noTimeout: true }).body;
    expect(future).toContain('timeout=Timeout(None, connect=10.0)');
    expect(future).toContain('timeout: 2_147_483_647');
    expect(future).toContain('responses.retrieve(response_id, stream=True, starting_after=last_seq)');
    expect(future).toContain('GET /v1/responses/{id}?stream=true&starting_after=412');
    for (const noTimeout of STATES) {
      for (const page of ownPages(noTimeout)) {
        expect(page.body, page.slug).not.toMatch(/timeout\s*[=:]\s*0\b(?!\.)/);
        for (const block of fencedBlocks(page.body, 'bash')) expect(block, page.slug).not.toContain('--max-time');
      }
    }
  });

  it('keeps the numbered-part rule the router enforces, and sends numbered parts in every resumable sample', () => {
    expect(ROUTES_PY).toContain('f"This upload is {mode}; do not mix numbered and sequential parts."');
    expect(ROUTES_PY).toContain('"This upload\'s parts were sent without part_number, so their order is only known "');
    const body = uploads.body;
    expect(body).toContain('**The first part decides the upload\'s numbering, for good.**');
    expect(body).toContain('**Part numbers start at 0**');
    expect(ROUTES_PY).toContain('ceiling = limits.max_parts() - 1');
    // The resumable samples use the raw numbered PUT.
    const resumable = sectionOf(body, 'A resumable upload');
    expect(resumable).toContain('client.put(\n                f"/uploads/{upload_id}/parts/{n}"');
    expect(resumable).toContain('curl -s --fail-with-body -T "$part"');
  });
});

describe('each new page, rendered', () => {
  it('marks every page with the one shared example status, visibly', () => {
    for (const noTimeout of STATES) {
      for (const page of ownPages(noTimeout)) {
        cleanup();
        expect(page.examples, page.slug).toBe(EXAMPLE_STATUS);
        render(<DocsArticle page={page} />);
        const notice = screen.getByTestId('docs-example-status');
        expect(notice.getAttribute('data-executed')).toBe(EXAMPLE_STATUS.executed ? 'true' : 'false');
        expect(notice.textContent).toContain(EXAMPLE_STATUS.note);
      }
    }
  });

  it('renders its title and summary, and every heading with an id its contents rail can reach', () => {
    for (const noTimeout of STATES) {
      for (const page of ownPages(noTimeout)) {
        cleanup();
        const { container } = render(<DocsArticle page={page} />);
        expect(screen.getByRole('heading', { level: 1, name: page.title })).toBeTruthy();
        expect(screen.getByText(page.summary)).toBeTruthy();
        const rendered = [...container.querySelectorAll('article h2[id], article h3[id]')].map((node) => node.id);
        const listed = docHeadingsOf(page.body).map((heading) => heading.id);
        expect([...rendered].sort(), `${page.slug} noTimeout=${noTimeout}`).toEqual([...listed].sort());
        expect(new Set(listed).size, `${page.slug} repeats a heading`).toBe(listed.length);
        expect(listed.every((id) => id.length > 0)).toBe(true);
        expect(listed.length).toBeGreaterThanOrEqual(6);
      }
    }
  });

  it('wraps every table so a wide one scrolls, and labels every code sample', () => {
    for (const page of newPages(false)) {
      cleanup();
      const { container } = render(<DocsArticle page={page} />);
      const tables = container.querySelectorAll('table');
      expect(tables.length).toBeGreaterThan(0);
      for (const table of tables) expect(table.closest('.md-table-wrap'), page.slug).toBeTruthy();
      const blocks = container.querySelectorAll('.code-block');
      expect(blocks.length).toBeGreaterThan(2);
      for (const block of blocks) expect(block.querySelector('button')).toBeTruthy();
    }
  });

  it('resolves every link on these pages, in every state the site can be published in', () => {
    for (const noTimeout of STATES) {
      const site = publishedSite(noTimeout);
      const find = (slug: string) => site.find((page) => page.slug === slug);
      const offenders: string[] = [];
      for (const page of ownPages(noTimeout)) {
        for (const m of page.body.matchAll(/\]\((\/docs[^)\s]*|#[^)\s]+)\)/g)) {
          const [path, fragment] = m[1].split('#');
          const slug = path === '' ? page.slug : path === '/docs' ? OVERVIEW_SLUG : path.replace('/docs/', '');
          const target = find(slug);
          if (!target) {
            offenders.push(`${page.slug} -> ${m[1]} (no such page)`);
            continue;
          }
          if (fragment && !docHeadingsOf(target.body).some((heading) => heading.id === fragment)) {
            offenders.push(`${page.slug} -> ${m[1]} (no such heading)`);
          }
        }
      }
      expect(offenders, `noTimeout=${noTimeout}`).toEqual([]);
    }
  });

  it('lists the Files pages beside the other API reference pages, in reading order, once published', () => {
    for (const page of newPages(false)) expect(page.section).toBe('API reference');
    if (FILES_API_PUBLISHED) {
      const slugs = DOC_PAGES.map((page) => page.slug);
      expect(slugs.slice(slugs.indexOf('images'), slugs.indexOf('images') + 4)).toEqual(['images', 'files', 'uploads', 'file-inputs']);
    }
  });
});

describe('what the pages never say', () => {
  it('names no internal checkpoint, engine, library, host, port, path, setting or internal header', () => {
    const configPy = repoFile(...APP, 'config.py');
    const checkpoints = [
      ...new Set([...configPy.matchAll(/"((?:Qwen|nvidia|baidu|openai)\/[A-Za-z0-9._-]+)"/g)].map((m) => m[1])),
    ];
    expect(checkpoints.length).toBeGreaterThanOrEqual(4);
    const tails = checkpoints.map((name) => name.split('/')[1].toLowerCase());
    const machinery = [
      /qwen/i, /unlimited-ocr/i, /whisper-large/i, /\bvllm\b/i, /nvfp4/i, /\bbaidu\b/i, /sf-local-ai/i,
      /\b192\.168\.\d+\.\d+/, /\b172\.\d+\.\d+\.\d+/, /:30\d{3}\b/, /:80[89]0\b/,
      /\/data\//, /api-files/, /PUBLIC_API_/, /X-TechSara/i, /ts-seq/, /v1-gateway/i, /cloudflare/i,
      /\bffmpeg\b/i, /\bffprobe\b/i, /pdfium/i, /duckdb/i, /openpyxl/i, /trafilatura/i, /\bpillow\b/i,
      /lancedb/i, /landlock/i, /seccomp/i, /\bhls\b/i, /m3u8/i, /\bnumpy\b/i, /postgres/i, /\blease\b/i,
      /\bblob_[0-9a-f]/, /\bproj_[0-9a-f]{24}\b/,
    ];
    for (const noTimeout of STATES) {
      const offenders: string[] = [];
      for (const page of ownPages(noTimeout)) {
        const text = `${page.title}\n${page.summary}\n${page.body}`;
        for (const tail of tails) if (text.toLowerCase().includes(tail)) offenders.push(`${page.slug}: ${tail}`);
        for (const pattern of machinery) if (pattern.test(text)) offenders.push(`${page.slug}: ${pattern}`);
      }
      expect(offenders, `noTimeout=${noTimeout}`).toEqual([]);
    }
  });

  it('names no other vendor, beyond the openai package a reader already has', () => {
    const banned = /\b(anthropic|claude|gemini|mistral|cohere|azure openai|chatgpt)\b/i;
    for (const noTimeout of STATES) {
      for (const page of ownPages(noTimeout)) {
        expect(page.body, page.slug).not.toMatch(banned);
        // "openai" appears only as the package: imports, installs and `openai` in code spans.
        for (const m of page.body.matchAll(/openai/gi)) {
          const line = page.body.slice(page.body.lastIndexOf('\n', m.index!) + 1, page.body.indexOf('\n', m.index!));
          expect(line, page.slug).toMatch(/from openai import|import OpenAI|from "openai"|`openai`|new OpenAI|OpenAI\(/);
        }
      }
    }
  });

  it('names only streaming events the grammar, the webhooks or the citation splice emit', () => {
    const emitted = new Set([
      ...[...PUBLIC_EVENTS_PY.matchAll(/^RESPONSE_[A-Z_]+ = "([a-z._]+)"$/gm)].map((m) => m[1]),
      ...[...WEBHOOK_SENDER_PY.matchAll(/^RESPONSE_[A-Z]+ = "([a-z.]+)"$/gm)].map((m) => m[1]),
      ...[...CITATIONS_PY.matchAll(/"type": "(response\.[a-z_.]+)"/g)].map((m) => m[1]),
    ]);
    expect(emitted.size).toBeGreaterThanOrEqual(8);
    for (const noTimeout of STATES) {
      for (const page of ownPages(noTimeout)) {
        for (const pattern of [/`(response\.[a-z_.]+)`/g, /event: (response\.[a-z_.]+)/g, /"type":\s?"(response\.[a-z_.]+)"/g, /"(response\.[a-z_.]+)"/g]) {
          for (const m of page.body.matchAll(pattern)) {
            expect(emitted, `${page.slug}: ${m[1]}`).toContain(m[1]);
          }
        }
      }
    }
  });

  it('never presents a limit on how much a project stores or uploads as a usage limit', () => {
    for (const page of newPages(false)) {
      expect(page.body, page.slug).not.toMatch(/\b429\b|rate limit|quota exceeded|per minute/i);
    }
    expect(sectionOf(files.body, 'Limits and why')).toContain('None of these is a usage limit');
    expect(sectionOf(uploads.body, 'Limits and why')).toContain('Technical ceilings, not usage limits');
  });
});
