// @vitest-environment jsdom
/**
 * The developer documentation at /docs (CONTRACT §17).
 *
 * Documentation rots differently from code: nothing fails when a page
 * describes a route that was renamed, a key format that changed, or an
 * anchor that moved. These tests exist to make that failure loud, so they
 * read the PRIMARY SOURCES rather than a copy — CONTRACT.md for the routes,
 * apiplatform/keys.py's own rules for the example keys, publicapi/registry.py
 * for what the model can do — and hold the prose to them.
 *
 * Four of them are the ones the brief names, and they are the four that
 * matter most:
 *
 *   1. every /v1 route named in the documentation exists in the contract;
 *   2. every example key FAILS the real validator, so nobody can paste one
 *      out of a page and have it look valid;
 *   3. the sidebar renders every section and every page;
 *   4. a page renders, and its anchors — its own and its cross-page ones —
 *      resolve to headings that exist.
 */

import { existsSync, readFileSync, readdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ComponentProps, ReactNode } from 'react';

vi.mock('next/navigation', () => ({
  usePathname: () => '/docs/errors',
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

import { createRequire } from 'node:module';

import { DocsArticle } from '@/components/docs/DocsArticle';
import { DocsMarkdown } from '@/components/docs/DocsMarkdown';
import { DocsNav } from '@/components/docs/DocsNav';
import { docHeadingsOf } from '@/components/docs/docHeadings';
import { DocsShell } from '@/components/docs/DocsShell';
import {
  DOC_PAGES,
  DOC_SECTIONS,
  EMBED_MODEL_ID,
  EXAMPLE_KEYS,
  EXAMPLE_STATUS,
  EXAMPLES_EXECUTED,
  EXECUTED_NOTE,
  NOT_EXECUTED_NOTE,
  EXAMPLE_LIVE_KEY,
  LONG_OUTPUT_WALL_CLOCK_LIVE,
  MODEL_ID,
  MODEL_IDS,
  OCR_MODEL_ID,
  OVERVIEW_SLUG,
  RERANK_MODEL_ID,
  VISION_MODEL_ID,
  WALL_CLOCK_PENDING_NOTE,
  WHISPER_MODEL_ID,
  docHref,
  findDocPage,
  neighboursOf,
} from '@/content/docs';

afterEach(cleanup);

const FRONTEND_DIR = join(dirname(fileURLToPath(import.meta.url)), '..');
const REPO_ROOT = join(FRONTEND_DIR, '..');

function repoFile(...parts: string[]): string {
  return readFileSync(join(REPO_ROOT, ...parts), 'utf8');
}

/** One `## heading` section of a page body, up to the next `## ` heading. */
function sectionOf(body: string, heading: string): string {
  const start = body.indexOf(`## ${heading}\n`);
  expect(start, `## ${heading}`).toBeGreaterThanOrEqual(0);
  const next = body.indexOf('\n## ', start + 3);
  return body.slice(start, next === -1 ? undefined : next);
}

/** A body with its fenced samples removed, cut into prose units: a blank-line
 * paragraph, or one table row on its own (a whole table is one paragraph,
 * and one honest row must not excuse its neighbours). */
function proseUnitsOf(body: string): string[] {
  const prose = body.replace(/^~~~[^\n]*\n[\s\S]*?^~~~$/gm, '');
  return prose
    .split(/\n\s*\n/)
    .flatMap((block) => (block.trimStart().startsWith('|') ? block.split('\n') : [block]))
    .filter((unit) => unit.trim() !== '');
}

const CONTRACT = repoFile('docs', 'developer-platform', 'CONTRACT.md');
const REGISTRY_PY = repoFile('orchestrator', 'app', 'publicapi', 'registry.py');
const KEYS_PY = repoFile('orchestrator', 'app', 'apiplatform', 'keys.py');

/** The changelog heading of the all-models / 1M-output change (2026-09-13). */
const ALL_MODELS_ENTRY = '2026-09-13 — every model on the API, and answers up to 1,000,000 tokens';

// -------------------------------------------------------------- tailwind --

/**
 * Compile a list of classes with the REAL tailwind.config.ts and return
 * class -> declarations for every class that produced a rule. jsdom applies
 * no stylesheet, so this is the only way a test can see that a utility class
 * is dead.
 */
async function compileTailwind(classes: string[]): Promise<Map<string, string>> {
  const require = createRequire(join(FRONTEND_DIR, 'package.json'));
  const postcss = require('postcss');
  const tailwind = require('tailwindcss');
  const loadConfig = require('tailwindcss/loadConfig');
  const config = loadConfig(join(FRONTEND_DIR, 'tailwind.config.ts'));
  const result = await postcss([
    tailwind({
      ...config,
      content: [{ raw: classes.join(' '), extension: 'html' }],
      corePlugins: { ...(config.corePlugins ?? {}), preflight: false },
    }),
  ]).process('@tailwind utilities;', { from: undefined });

  const rules = new Map<string, string>();
  const unescape = (selector: string) =>
    selector
      .replace(/\\([0-9a-fA-F]{1,6})\s?/g, (_, hex) => String.fromCodePoint(parseInt(hex, 16)))
      .replace(/\\(.)/g, '$1');
  result.root.walkRules((rule: { selector: string; nodes: { toString(): string }[] }) => {
    for (const part of rule.selector.split(',')) {
      // A class selector with CSS escapes: `\[`, `\/`, and the hex form
      // `\2c ` (with its terminating space) that Tailwind uses for commas.
      const match = /^\s*\.((?:\\[0-9a-fA-F]{1,6}\s?|\\[^0-9a-fA-F]|[^\s:>+~.[\]\\])+)/.exec(part);
      if (!match) continue;
      const name = unescape(match[1]);
      const body = rule.nodes.map((node) => node.toString()).join(';\n');
      rules.set(name, `${rules.get(name) ?? ''}${body};\n`);
    }
  });
  return rules;
}

// ---------------------------------------------------------------- routes --

/**
 * The endpoint table of CONTRACT §7, as the contract writes it:
 *
 *     | GET | `/v1/models` | `models.read` | only models this key may use |
 */
function contractRoutes(): Set<string> {
  const routes = new Set<string>();
  const row = /^\|\s*(GET|POST|PUT|PATCH|DELETE)\s*\|\s*`(\/v1\/[^`]+)`/gm;
  for (const match of CONTRACT.matchAll(row)) {
    routes.add(normalisePath(match[2]));
  }
  return routes;
}

/**
 * `/v1/responses/resp_4f2b…/cancel` and `/v1/responses/{id}/cancel` are the
 * same route. A path parameter in the contract is `{id}`; in a cURL example
 * it is a concrete value, so both collapse to `{}` before comparison.
 */
function normalisePath(path: string): string {
  return path
    .split('/')
    .map((segment) => {
      if (!segment) return segment;
      if (/^\{.*\}$/.test(segment)) return '{}';
      if (/^(resp|msg|proj|svc|key|whe|whd)_/.test(segment)) return '{}';
      // Any of the six public ids fills `/v1/models/{model}` (2026-09-13).
      if ((MODEL_IDS as readonly string[]).includes(segment)) return '{}';
      return segment;
    })
    .join('/');
}

/** Every `/v1/...` path mentioned anywhere in a page's prose or samples. */
function routesMentionedIn(body: string): string[] {
  const found = new Set<string>();
  for (const match of body.matchAll(/\/v1\/[A-Za-z0-9_\-{}./]+/g)) {
    const cleaned = match[0].replace(/[.]+$/, '');
    found.add(normalisePath(cleaned));
  }
  return [...found];
}

/** The six public ids, in the order CONTRACT §15's registry table lists them. */
function contractModelIds(): string[] {
  const section = contractSection('## 15. Model registry', '## 16. Recording');
  return [...section.matchAll(/^\|\s*`(techsara-[a-z0-9-]+)`\s*\|\s*`[a-z]+`\s*\|/gm)].map((m) => m[1]);
}

/** One `## ` section of the contract, from its heading to the next one named. */
function contractSection(start: string, end: string): string {
  const from = CONTRACT.indexOf(start);
  const to = CONTRACT.indexOf(end, from + 1);
  expect(from, start).toBeGreaterThanOrEqual(0);
  expect(to, end).toBeGreaterThan(from);
  return CONTRACT.slice(from, to);
}

/** The scope table of CONTRACT §7: scope -> the sentence the server shows. */
function contractScopes(): Map<string, string> {
  const section = contractSection('## 7. Public endpoints', '## 8. Request contract');
  return new Map(
    [...section.matchAll(/^\|\s*`([a-z]+\.[a-z]+)`\s*\|\s*([^|]+?)\s*\|\s*(?:yes|no)/gm)].map(
      (m) => [m[1], m[2]],
    ),
  );
}

/** `12,345` -> 12345, `none` -> null. */
function numberOrNull(cell: string): number | null {
  const match = /^([\d,]+)/.exec(cell.trim());
  return match ? Number(match[1].replace(/,/g, '')) : null;
}

// ------------------------------------------------------------------ keys --

/**
 * A faithful mirror of `split_key()` in orchestrator/app/apiplatform/keys.py,
 * checksum included, so this test can prove an example key is refused
 * WITHOUT running Python.
 *
 * A mirror is only trustworthy if it can also say yes, which is why
 * `parsesWithTheRightChecksum` below repairs an example key and expects it to
 * parse. Without that control, a mirror that returned null for everything
 * would make every assertion here pass while checking nothing.
 *
 * Verified against the real implementation on 2026-09-13:
 *   keys.checksum_for("0123456789abcdef", "<the example secret>") == "1wPKxD"
 * which is what this crc32/base62 pair computes for the same input.
 */
const ACCEPTED_SECRET_LENGTHS = new Set([43]);
const CHECKSUM_CHARS = 6;
const PUBLIC_ID_CHARS = 16;
const BASE62 = '0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz';

function crc32(input: string): number {
  const bytes = new TextEncoder().encode(input);
  let crc = 0xffffffff;
  for (const byte of bytes) {
    crc ^= byte;
    for (let bit = 0; bit < 8; bit += 1) {
      crc = (crc >>> 1) ^ (0xedb88320 & -(crc & 1));
    }
  }
  return (crc ^ 0xffffffff) >>> 0;
}

function base62(value: number, width: number): string {
  let out = '';
  let rest = value;
  while (rest > 0) {
    out = BASE62[rest % 62] + out;
    rest = Math.floor(rest / 62);
  }
  return out.padStart(width, BASE62[0]);
}

function checksumFor(publicId: string, secret: string): string {
  return base62(crc32(publicId + secret), CHECKSUM_CHARS);
}

interface ParsedKey {
  environment: string;
  publicId: string;
  secret: string;
  checksum: string;
}

function splitKey(token: string): ParsedKey | null {
  const environment = ['live', 'test'].find((env) => token.startsWith(`tsk_${env}_`));
  if (!environment) return null;

  const rest = token.slice(`tsk_${environment}_`.length);
  const separator = rest.indexOf('_');
  if (separator < 0) return null;

  const publicId = rest.slice(0, separator);
  const tail = rest.slice(separator + 1);
  if (publicId.length !== PUBLIC_ID_CHARS || !/^[0-9a-f]+$/.test(publicId)) return null;
  if (tail.length <= CHECKSUM_CHARS) return null;

  const secret = tail.slice(0, -CHECKSUM_CHARS);
  const checksum = tail.slice(-CHECKSUM_CHARS);
  if (!ACCEPTED_SECRET_LENGTHS.has(secret.length)) return null;
  if (!/^[A-Za-z0-9_-]+$/.test(secret)) return null;
  if (!/^[0-9A-Za-z]+$/.test(checksum)) return null;
  if (checksumFor(publicId, secret) !== checksum) return null;

  return { environment, publicId, secret, checksum };
}

/** Every key-shaped string anywhere in the documentation. */
function keyLikeStrings(): string[] {
  const found: string[] = [];
  for (const page of DOC_PAGES) {
    for (const match of page.body.matchAll(/tsk_(?:live|test)_[A-Za-z0-9_-]+/g)) {
      found.push(match[0]);
    }
  }
  return found;
}

// ----------------------------------------------------------------- links --

interface DocLink {
  from: string;
  href: string;
}

function internalLinks(): DocLink[] {
  const links: DocLink[] = [];
  for (const page of DOC_PAGES) {
    for (const match of page.body.matchAll(/\]\((\/docs[^)\s]*|#[^)\s]+)\)/g)) {
      links.push({ from: page.slug, href: match[1] });
    }
  }
  return links;
}

function anchorsOf(slug: string): Set<string> {
  const page = findDocPage(slug);
  if (!page) return new Set();
  return new Set(docHeadingsOf(page.body).map((heading) => heading.id));
}

// ===========================================================================

describe('the documented API surface', () => {
  it('names only /v1 routes that exist in the developer-platform contract', () => {
    const allowed = contractRoutes();
    // The contract table itself must have parsed, or this whole test would
    // pass by comparing against an empty set.
    expect(allowed.size).toBeGreaterThanOrEqual(8);
    expect(allowed).toContain('/v1/responses');

    const offenders: string[] = [];
    for (const page of DOC_PAGES) {
      for (const route of routesMentionedIn(page.body)) {
        if (!allowed.has(route)) offenders.push(`${page.slug}: ${route}`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it('documents every route the contract publishes, somewhere', () => {
    const documented = new Set(DOC_PAGES.flatMap((page) => routesMentionedIn(page.body)));
    const missing = [...contractRoutes()].filter((route) => !documented.has(route));
    expect(missing).toEqual([]);
  });

  it('uses only the model ids the contract registry declares, and every one of them', () => {
    // 2026-09-13, owner request: six public ids, not one. The contract's §15
    // table is the list the code builds against; samples.ts must equal it,
    // and no sample may send an id outside it.
    expect(REGISTRY_PY).toContain('TECHSARA_35B = "techsara-35b"');
    expect(MODEL_ID).toBe('techsara-35b');
    const contractIds = contractModelIds();
    expect(contractIds).toHaveLength(6);
    expect([...MODEL_IDS]).toEqual(contractIds);

    const invented: string[] = [];
    for (const page of DOC_PAGES) {
      for (const match of page.body.matchAll(/"model":\s*"([^"]+)"/g)) {
        if (!(MODEL_IDS as readonly string[]).includes(match[1])) {
          invented.push(`${page.slug}: ${match[1]}`);
        }
      }
    }
    expect(invented).toEqual([]);

    // The registry may declare fewer ids than the contract while its wave is
    // landing, never an id the contract and these pages do not know.
    const registryIds = [...REGISTRY_PY.matchAll(/"(techsara-[a-z0-9-]+)"/g)].map((m) => m[1]);
    expect(registryIds).toContain(MODEL_ID);
    for (const id of registryIds) expect(MODEL_IDS as readonly string[]).toContain(id);
  });

  it('states plainly that tool calling is not offered, because the registry says tools=False', () => {
    // The registry is the authority (CONTRACT §15). If it ever declares
    // tools=True this assertion fails first, which is the moment to rewrite
    // the page rather than the moment a customer discovers the gap. Matched
    // as `tools=False` or a dataclass default `tools: bool = False`, because
    // the registry is being reshaped for six models (2026-09-13).
    expect(REGISTRY_PY).toMatch(/\btools\s*(?::\s*bool\s*)?=\s*False\b/);
    expect(REGISTRY_PY).not.toMatch(/\btools\s*(?::\s*bool\s*)?=\s*True\b/);

    const tools = findDocPage('tools');
    expect(tools).toBeDefined();
    expect(tools!.body).toContain('Tool calling is not available');
    expect(tools!.body).toContain('"tools": false');

    // And nothing anywhere may advertise the opposite.
    for (const page of DOC_PAGES) {
      expect(page.body).not.toMatch(/"tools":\s*true/);
    }
  });
});

describe('the example credentials', () => {
  it('are refused by the real key validator, every one of them', () => {
    const tokens = keyLikeStrings();
    // Both example keys appear in the prose; if this ever reads zero, the
    // loop below is checking nothing.
    expect(tokens.length).toBeGreaterThanOrEqual(2);

    for (const token of tokens) {
      expect(splitKey(token), `${token} must not validate`).toBeNull();
    }
    for (const token of EXAMPLE_KEYS) {
      expect(splitKey(token)).toBeNull();
    }
  });

  it('fail on the checksum alone — the shape is perfect, which is the point', () => {
    // Proves the mirror is a validator and not a rubber stamp: repair the
    // checksum and the very same token parses. It also proves the example
    // key is realistic enough to teach a reader what a key looks like.
    const environment = 'live';
    const rest = EXAMPLE_LIVE_KEY.slice(`tsk_${environment}_`.length);
    const publicId = rest.slice(0, PUBLIC_ID_CHARS);
    const secret = rest.slice(PUBLIC_ID_CHARS + 1, -CHECKSUM_CHARS);

    expect(publicId).toMatch(/^[0-9a-f]{16}$/);
    expect(secret).toHaveLength(43);
    expect(EXAMPLE_LIVE_KEY.slice(-CHECKSUM_CHARS)).toBe('EXAMPL');

    const repaired = `tsk_${environment}_${publicId}_${secret}${checksumFor(publicId, secret)}`;
    expect(splitKey(repaired)).not.toBeNull();
    expect(repaired).not.toBe(EXAMPLE_LIVE_KEY);
  });

  it('keeps the documented key anatomy in step with keys.py', () => {
    // The security page teaches the format. These are the numbers it teaches.
    expect(KEYS_PY).toContain('KEY_PREFIX = "tsk"');
    expect(KEYS_PY).toContain('CHECKSUM_CHARS = 6');
    expect(KEYS_PY).toContain('_PUBLIC_ID_BYTES = 8');
    expect(KEYS_PY).toContain('DEFAULT_ROTATION_OVERLAP = timedelta(days=7)');
    expect(KEYS_PY).toContain('MAX_ROTATION_OVERLAP = timedelta(days=30)');
    expect(KEYS_PY).toContain('DEFAULT_KEY_LIFETIME = timedelta(days=90)');

    const security = findDocPage('key-security')!;
    expect(security.body).toContain('16 hex characters');
    expect(security.body).toContain('43 characters');
    expect(security.body).toContain('90 days');
  });

  it('describes key rotation as the console actually performs it', () => {
    // keys.py CAN date an old key out days ahead; the console's rotate route
    // decides whether it does. Earlier on 2026-09-13 it used
    // `plan_revocation` (the old key died at once) while the page promised a
    // 7-day grace window; the console wave then shipped the overlap. The page
    // is bound to the ROUTE, so whichever way it goes next this fails until
    // the page says so.
    //
    // The route now delegates to the ONE rotation implementation,
    // `projects.rotate_key` (wave 4, 2026-09-13), whose default overlap is
    // `settings.public_api_key_rotation_overlap_hours` (168 h) and whose
    // compromise path is an explicit overlap of 0. The detector follows that
    // structure rather than the helper names the route used to call inline —
    // sniffing the old names read a correct page as wrong the moment the
    // implementation moved.
    const consoleApi = repoFile('orchestrator', 'app', 'apiplatform', 'console_api.py');
    const projectsPy = repoFile('orchestrator', 'app', 'apiplatform', 'projects.py');
    const configPy = repoFile('orchestrator', 'app', 'config.py');
    const rotate = consoleApi.slice(
      consoleApi.indexOf('async def rotate_key('),
      consoleApi.indexOf('async def revoke_key('),
    );
    expect(rotate.length).toBeGreaterThan(0);
    const page = findDocPage('key-security')!.body;
    const withOverlap =
      rotate.includes('key_projects.rotate_key(') &&
      /"PUBLIC_API_KEY_ROTATION_OVERLAP_HOURS", 168\.0/.test(configPy) &&
      projectsPy.includes('settings.public_api_key_rotation_overlap_hours') &&
      /immediate = overlap <= timedelta\(0\)/.test(projectsPy) &&
      consoleApi.includes('le=int(key_tools.MAX_ROTATION_OVERLAP.total_seconds() // 3600)');
    if (withOverlap) {
      expect(page).toContain('7 days by default, 30 days at the very most');
      expect(page).toContain('an overlap of **zero** is the compromise case');
      expect(page).not.toContain('no overlap window today');
    } else {
      expect(page).toContain('no overlap window today');
      expect(page).not.toContain('7 days by default');
    }
  });

  it('sets TECHSARA_API_KEY before any shell sample on the page uses it', () => {
    // A reader pastes ONE block. The live probe on /docs/status used the
    // variable without ever setting it and sent `Authorization: Bearer ` — a
    // 401 on the page about diagnosing outages (verifier finding, 2026-09-13).
    const offenders: string[] = [];
    for (const page of DOC_PAGES) {
      const blocks = [...page.body.matchAll(/^~~~bash\n([\s\S]*?)^~~~$/gm)].map((m) => m[1]);
      let exported = false;
      for (const block of blocks) {
        const exportAt = block.search(/^export TECHSARA_API_KEY=/m);
        const useAt = block.search(/\$TECHSARA_API_KEY/);
        if (useAt >= 0 && !exported && (exportAt < 0 || exportAt > useAt)) {
          offenders.push(page.slug);
          break;
        }
        if (exportAt >= 0) exported = true;
      }
    }
    expect(offenders).toEqual([]);
  });

  it('never prints a secret-shaped value for anything but a placeholder', () => {
    for (const page of DOC_PAGES) {
      // A webhook signing secret is never documented as a literal: the
      // console shows it once and no API returns it.
      expect(page.body).not.toMatch(/whsec_[A-Za-z0-9]{8,}/);
      expect(page.body).not.toMatch(/API_KEY_PEPPER=\S+/);
    }
  });
});

describe('the error and scope vocabularies', () => {
  it('documents every error code with the status errors.py actually sends for it', () => {
    // 2026-09-13, verifier finding: this used to check that the code appeared
    // on the page and, SEPARATELY, that the status appeared somewhere on the
    // page — so `insufficient_scope | 404` passed because `| 403 |` survived
    // on the next row. The pairs are compared now, as pairs, against the
    // table the server raises from and against the contract.
    const tableRow = /^\|\s*`([a-z_]+)`\s*\|\s*(\d{3})\s*\|/gm;
    const pageMap = new Map(
      [...findDocPage('errors')!.body.matchAll(tableRow)].map((m) => [m[1], m[2]]),
    );
    const contractMap = new Map([...CONTRACT.matchAll(tableRow)].map((m) => [m[1], m[2]]));
    const errorsPy = repoFile('orchestrator', 'app', 'publicapi', 'errors.py');
    const serverMap = new Map(
      [...errorsPy.matchAll(/^\s{4}"([a-z_]+)": _CodeSpec\((\d{3}), "[a-z_]+"\),$/gm)].map(
        (m) => [m[1], m[2]],
      ),
    );
    expect(contractMap.size).toBeGreaterThanOrEqual(16);
    expect(serverMap.size).toBeGreaterThanOrEqual(16);

    expect(Object.fromEntries(pageMap)).toEqual(Object.fromEntries(serverMap));
    for (const [code, status] of contractMap) {
      expect(pageMap.get(code), `${code} on the errors page`).toBe(status);
    }
  });

  it('says Retry-After exactly where errors.py requires it, and nowhere it does not', () => {
    // `timeout` is a 504 and errors.py constructs it without Retry-After; the
    // page used to promise the header on every retryable code.
    const errorsPy = repoFile('orchestrator', 'app', 'publicapi', 'errors.py');
    expect(errorsPy).toContain('_RETRY_AFTER_REQUIRED = frozenset({429, 503})');
    expect(errorsPy).toMatch(/"timeout": _CodeSpec\(504, /);
    const page = findDocPage('errors')!.body;
    expect(page).not.toMatch(/Every one of those carries \\?`Retry-After/);
    expect(page).toContain('carries no `Retry-After`');
    expect(page).not.toMatch(/monthly/i);
  });

  it('prints the insufficient_scope message and challenge the server builds', () => {
    const errorsPy = repoFile('orchestrator', 'app', 'publicapi', 'errors.py');
    const scopesPy = repoFile('orchestrator', 'app', 'apiplatform', 'scopes.py');
    expect(errorsPy).toContain('f"The API key does not have the `{name}` scope."');
    expect(scopesPy).toContain('f\'Bearer error="insufficient_scope", scope="{wanted}"\'');
    const page = findDocPage('authentication')!.body;
    expect(page).toContain('"message": "The API key does not have the `responses.write` scope."');
    expect(page).toContain('WWW-Authenticate: Bearer error="insufficient_scope", scope="responses.write"');
  });

  it('refuses a disabled workspace with the same 401 the resolver sends, not a 403', () => {
    const resolverPy = repoFile('orchestrator', 'app', 'apiplatform', 'resolver.py');
    expect(resolverPy).toMatch(/workspace_status\(workspace_id\) != "active":\n\s+raise _refuse\(\)/);
    const page = findDocPage('authentication')!.body;
    expect(page).not.toMatch(/workspace\*\* — disabled is `403`/);
    expect(page).toContain('Rungs 1 to 5 all answer the **same** `401 invalid_api_key`');
  });

  it('documents every scope the platform defines, and invents none', () => {
    // 2026-09-13: the vocabulary grows from four to seven, and the CONTRACT
    // moves first (§7's scope table) while scopes.py follows in the same
    // wave. So a scope is legitimate when the contract names it; scopes.py
    // may never define one the contract does not; and the authentication
    // page must carry each one with the contract's sentence, verbatim — the
    // sentence the console and the OpenAPI document show.
    const scopesPy = repoFile('orchestrator', 'app', 'apiplatform', 'scopes.py');
    const defined = new Set(
      [...scopesPy.matchAll(/^\s{4}[A-Z_]+ = "([a-z.]+)"$/gm)].map((m) => m[1]),
    );
    expect(defined.size).toBeGreaterThanOrEqual(4);
    const contract = contractScopes();
    expect([...contract.keys()].sort()).toEqual(
      ['audio.write', 'embeddings.write', 'models.read', 'rerank.write', 'responses.read', 'responses.write', 'usage.read'],
    );
    for (const scope of defined) expect(contract.has(scope), `${scope} is not in CONTRACT §7`).toBe(true);

    const authentication = findDocPage('authentication')!.body;
    for (const scope of new Set([...defined, ...contract.keys()])) {
      expect(authentication, `${scope} must be documented`).toContain(`\`${scope}\``);
    }
    for (const [scope, sentence] of contract) {
      expect(authentication).toContain(`| \`${scope}\` | ${sentence} |`);
      // Where scopes.py already describes the scope, it says the same thing.
      const described = new RegExp(`Scope\\.${scope.toUpperCase().replace('.', '_')}: "([^"]+)"`).exec(scopesPy);
      if (described) expect(described[1]).toBe(sentence);
    }

    const invented: string[] = [];
    for (const page of DOC_PAGES) {
      for (const match of page.body.matchAll(
        /`((?:models|responses|usage|webhooks|embeddings|rerank|audio)\.[a-z]+)`/g,
      )) {
        if (!contract.has(match[1]) && !defined.has(match[1])) invented.push(`${page.slug}: ${match[1]}`);
      }
    }
    expect(invented).toEqual([]);
  });

  it('shows mid-stream failures in the shapes events.py and streaming.py frame', () => {
    // The page used to show `event: error` wrapping the HTTP envelope. The
    // grammar's `error` payload is flat (type, code, message, param,
    // sequence_number) and a failure after the stream opens on /v1/responses
    // is `response.failed` carrying the response with its `error`.
    const errorsPy = repoFile('orchestrator', 'app', 'publicapi', 'errors.py');
    const streamingPy = repoFile('orchestrator', 'app', 'publicapi', 'streaming.py');
    expect(errorsPy).toMatch(/"type": "error",\n\s+"code": self\.code,/);
    expect(streamingPy).toContain('yield emitter.failed(');
    expect(streamingPy).toContain('yield chunks.error_chunk(failure)');

    const page = findDocPage('streaming')!.body;
    const frames = [...page.matchAll(/^data: (\{.*\})$/gm)].map((m) => JSON.parse(m[1]));
    const errorFrame = frames.find((frame) => frame.type === 'error');
    expect(errorFrame).toBeDefined();
    expect(Object.keys(errorFrame).sort()).toEqual(
      ['code', 'message', 'param', 'sequence_number', 'type'].sort(),
    );
    const failed = frames.find((frame) => frame.type === 'response.failed');
    expect(failed?.response?.status).toBe('failed');
    expect(Object.keys(failed.response.error).sort()).toEqual(['code', 'message']);

    // And the client loops read the fields from where they actually are.
    for (const slug of ['streaming', 'python', 'javascript']) {
      const body = findDocPage(slug)!.body;
      expect(body, slug).not.toContain('data.get("error", data)');
      expect(body, slug).not.toContain('payload.error?.code');
    }

    const chat = findDocPage('chat-completions')!.body;
    const chatError = [...chat.matchAll(/^data: (\{.*\})$/gm)]
      .map((m) => JSON.parse(m[1]))
      .find((frame) => frame.error);
    expect(chatError?.choices).toEqual([]);
    expect(Object.keys(chatError.error).sort()).toEqual(['code', 'message', 'param', 'type']);
  });

  it('uses only the streaming event names the SSE grammar emits', () => {
    const eventsPy = repoFile('orchestrator', 'app', 'publicapi', 'events.py');
    const emitted = new Set(
      [...eventsPy.matchAll(/^RESPONSE_[A-Z_]+ = "([a-z._]+)"$/gm)].map((m) => m[1]),
    );
    expect(emitted.size).toBeGreaterThanOrEqual(7);

    // Only where an event name is actually being NAMED: inline code, an SSE
    // `event:` line, or a `"type"` field. A bare `response.json()` in a
    // Python sample is a variable, not an event, and matching it would make
    // this test about nothing.
    const sources = [
      /`(response\.[a-z_.]+)`/g,
      /event: (response\.[a-z_.]+)/g,
      /"type":\s?"(response\.[a-z_.]+)"/g,
    ];
    // The webhook events (CONTRACT §14) are a separate vocabulary from the
    // SSE lifecycle, and `response.cancelled` exists only there.
    const webhookPy = repoFile('orchestrator', 'app', 'apiplatform', 'webhooks', 'sender.py');
    const subscribable = new Set(
      [...webhookPy.matchAll(/^RESPONSE_[A-Z]+ = "([a-z.]+)"$/gm)].map((m) => m[1]),
    );
    expect(subscribable.size).toBe(3);

    const invented: string[] = [];
    for (const page of DOC_PAGES) {
      for (const pattern of sources) {
        for (const match of page.body.matchAll(pattern)) {
          const name = match[1];
          if (!emitted.has(name) && !subscribable.has(name)) {
            invented.push(`${page.slug}: ${name}`);
          }
        }
      }
    }
    expect(invented).toEqual([]);
  });
});

/**
 * The `/v1` router landed while this documentation was being written
 * (2026-09-13). These tests hold the pages to what it ACTUALLY does, rather
 * than to what the contract permits it to do — the two agree today, and the
 * day they stop agreeing the documentation is the thing that will be wrong.
 */
describe('the shipped router', () => {
  const ROUTER_PY = repoFile('orchestrator', 'app', 'publicapi', 'router.py');

  it('maps each endpoint to the scope the docs promise', () => {
    // SCOPES in router.py is the enforcement. The authentication page's table
    // is the promise. They are checked against each other here rather than by
    // a reader discovering a 403.
    const scopeMap = new Map<string, string>();
    for (const match of ROUTER_PY.matchAll(
      /^\s{4}"([a-z_]+)": requires\(Scope\.([A-Z_]+)\),$/gm,
    )) {
      scopeMap.set(match[1], match[2].toLowerCase().replace('_', '.'));
    }
    expect(scopeMap.get('create_response')).toBe('responses.write');
    expect(scopeMap.get('get_usage')).toBe('usage.read');

    const authentication = findDocPage('authentication')!.body;
    for (const scope of new Set(scopeMap.values())) {
      expect(authentication).toContain(`\`${scope}\``);
    }
  });

  it('accepts exactly the Chat Completions fields the compatibility page lists', () => {
    const declared = /_CHAT_FIELDS = \(([^)]*)\)/.exec(ROUTER_PY);
    expect(declared).not.toBeNull();
    const fields = [...declared![1].matchAll(/"([a-z_]+)"/g)].map((m) => m[1]);
    expect(fields).toContain('messages');

    // 2026-09-13, verifier finding: the "must not promise a refused field"
    // half asserted against the ROUTER's tuple, never the page, so a page
    // that advertised `top_p` stayed green. The page's own accepted-fields
    // table is now extracted and must equal the router's list exactly — a
    // field in either and not the other fails.
    const page = findDocPage('chat-completions')!.body;
    // Both markers asserted found (wave-3 re-verify): a missing end marker is
    // -1, and slice(start, -1) quietly runs to the end of the page.
    const start = page.indexOf('## The fields this endpoint accepts');
    const end = page.indexOf('That is the complete list.');
    expect(start).toBeGreaterThanOrEqual(0);
    expect(end).toBeGreaterThan(start);
    const section = page.slice(start, end);
    const documented = [...section.matchAll(/^\|\s*`([a-z_]+)`\s*\|/gm)].map((m) => m[1]);
    // 2026-09-13: `max_completion_tokens` is in CONTRACT §8.2 and lands in
    // router.py's _CHAT_FIELDS in the same wave as this page. It is allowed
    // here ONLY while the contract names it and the router has not caught
    // up; once the router lists it, this line adds nothing.
    const contractChat = contractSection('### 8.2 `POST /v1/chat/completions`', '### 8.3');
    const pending = ['max_completion_tokens'].filter(
      (field) => contractChat.includes(`\`${field}\``) && !fields.includes(field),
    );
    expect([...documented].sort()).toEqual([...fields, ...pending].sort());

    // Anywhere else on the page, a sampling knob the router refuses may only
    // be named inside the refusal the router sends — never in a request body,
    // a field name or plain prose ("we also accept top_p"), which the older
    // backtick/JSON-key check let through (wave-3 re-verify, 2026-09-13).
    const outsideRefusals = page.replace(/Unsupported field: [a-z_]+\./g, '');
    for (const refused of ['top_p', 'presence_penalty', 'logit_bias', 'seed']) {
      expect(fields).not.toContain(refused);
      expect(outsideRefusals, `${refused} named outside the refusal`).not.toMatch(
        new RegExp(`(?<![A-Za-z0-9_])${refused}(?![A-Za-z0-9_])`),
      );
    }
    // `n` too, which the original assertion carried. A one-letter word cannot
    // take a boundary match (`\n` in a sample is a boundary), so it is matched
    // as a field name or a JSON key.
    expect(fields).not.toContain('n');
    expect(outsideRefusals).not.toMatch(/"n"\s*:/);
    expect(outsideRefusals).not.toContain('`n`');
  });

  it('names the same generation defaults the router applies', () => {
    // A default is the most quietly wrong thing a document can carry: nobody
    // sends the field, so nobody discovers the number moved.
    //
    // 2026-09-13: the planning of a generation moves into
    // publicapi/planning.py in the all-models wave; the constant is read from
    // whichever of the two files defines it, so the move is not a docs bug.
    const planningPath = join(REPO_ROOT, 'orchestrator', 'app', 'publicapi', 'planning.py');
    const planningPy = existsSync(planningPath) ? readFileSync(planningPath, 'utf8') : '';
    const maxOutput =
      /^DEFAULT_MAX_OUTPUT_TOKENS = (\d+)$/m.exec(ROUTER_PY)?.[1] ??
      /^DEFAULT_MAX_OUTPUT_TOKENS = (\d+)$/m.exec(planningPy)?.[1];
    const temperature =
      /^DEFAULT_TEMPERATURE = ([\d.]+)$/m.exec(ROUTER_PY)?.[1] ??
      /^DEFAULT_TEMPERATURE = ([\d.]+)$/m.exec(planningPy)?.[1];
    expect(maxOutput).toBe('8192');
    expect(temperature).toBe('0.2');

    const page = findDocPage('responses')!.body;
    expect(page).toContain('**8,192**');
    expect(page).toContain('Defaults to \`0.2\`');
    expect(findDocPage('rate-limits')!.body).toContain('8,192');
  });

  it('publishes the project limit defaults the schema and the body cap actually have', () => {
    // The number a customer sizes their client against. Matched by luck until
    // 2026-09-13 (verifier finding: 600 rpm / 40 concurrent stayed green).
    const dbPy = repoFile('orchestrator', 'app', 'db.py');
    const modelsPy = repoFile('orchestrator', 'app', 'publicapi', 'models.py');
    const ddl = (column: string): number => {
      const match = new RegExp(`^\\s+${column}\\s+(?:integer|bigint)\\s+NOT NULL DEFAULT (\\d+)$`, 'm').exec(dbPy);
      expect(match, `${column} default in api_projects`).not.toBeNull();
      return Number(match![1]);
    };
    const body = /^_DEFAULT_MAX_BODY_BYTES = (\d+) \* (\d+)$/m.exec(modelsPy);
    expect(body).not.toBeNull();
    expect(Number(body![1]) * Number(body![2])).toBe(1024 * 1024);

    const page = findDocPage('rate-limits')!.body;
    const row = (label: string): string => {
      const match = new RegExp(`^\\| ${label} \\| ([^|]+) \\|`, 'm').exec(page);
      expect(match, `${label} row`).not.toBeNull();
      return match![1].trim();
    };
    const formatted = (n: number) => n.toLocaleString('en-GB');
    expect(row('Requests per minute')).toBe(formatted(ddl('rpm')));
    expect(row('Input tokens per minute')).toBe(formatted(ddl('input_tpm')));
    expect(row('Output tokens per minute')).toBe(formatted(ddl('output_tpm')));
    expect(row('Concurrent requests')).toBe(formatted(ddl('max_concurrency')));
    expect(row('Tokens per day')).toBe(formatted(ddl('daily_token_quota')));
    expect(row('Body bytes')).toBe('1 MiB');
  });

  it('describes the enforced limits, under the operator switch only, the way the shared quota interface enforces them', () => {
    // The platform-services wave's interface, as the integration lead fixed
    // it on 2026-09-13: per PROJECT, reserved atomically, every authenticated
    // route metered, one concurrency count across sync/stream/background,
    // and 0 meaning zero. If those names are not in quotas.py the page is
    // describing a platform that does not exist, and this fails.
    // 2026-09-13, owner decision: those limits are enforced only with
    // PUBLIC_API_ENFORCE_LIMITS on, so every one of these sentences must sit
    // inside the operator section, never in the part a default reader reads.
    const quotasPy = repoFile('orchestrator', 'app', 'apiplatform', 'quotas.py');
    expect(quotasPy).toMatch(/^def reserve\(/m);
    expect(quotasPy).toContain('pg_advisory_xact_lock');
    expect(quotasPy).toMatch(/def concurrency_slot\(\s*caller[^)]*kind/);

    const page = findDocPage('rate-limits')!.body;
    const enforced = sectionOf(page, 'If an operator enables limits');
    const unlimited = page.replace(enforced, '');
    for (const sentence of [
      'limits belong to the **project**',
      'summed across all of the project\'s keys',
      '*tighten*',
      'A limit set to `0` allows nothing.',
      'holds one from its `202`',
      'A `401` carries no quota headers at all',
    ]) {
      expect(enforced, sentence).toContain(sentence);
      expect(unlimited, sentence).not.toContain(sentence);
    }
    expect(page).toContain('## Usage is still recorded');
    expect(sectionOf(page, 'Usage is still recorded')).toContain('reads included');
    // The old per-key wording and the promise of a header on a 401 are gone.
    expect(page).not.toMatch(/every `\/v1` response carries/i);
    expect(page).not.toContain('## Every request counts');
    expect(page).not.toContain('## Zero means zero');
  });

  it('meters every route that takes a key, as the limits page says', () => {
    // "Every request counts, reads included" is a claim about the router.
    // Each handler that resolves a caller must pass through `_admit`; the
    // two that take no key (the schema, the preflight) must not resolve one.
    const handlers = ROUTER_PY.split(/^@router\./m).slice(1);
    expect(handlers.length).toBeGreaterThanOrEqual(9);
    const keyed = handlers.filter((h) => h.includes('Depends(resolve_caller)'));
    expect(keyed.length).toBe(7);
    for (const handler of keyed) {
      const name = /async def ([a-z_]+)\(/.exec(handler)?.[1];
      expect(handler, `${name} must count against RPM`).toContain('_admit(');
    }
    const openapi = handlers.find((h) => h.includes('"/openapi.json"'))!;
    expect(openapi).not.toContain('resolve_caller');
    const page = findDocPage('rate-limits')!.body;
    expect(page).toContain('`GET /v1/openapi.json` and the browser\'s `OPTIONS` preflight');
  });

  it('says webhook secrets rotate in place only if the console can do it', () => {
    const consoleApi = repoFile('orchestrator', 'app', 'apiplatform', 'console_api.py');
    const canRotate = /@router\.(?:post|put|patch)\("\/projects\/\{project_id\}\/webhooks\/\{endpoint_id\}\/rotate/.test(
      consoleApi,
    );
    const page = findDocPage('webhooks')!.body;
    if (canRotate) {
      expect(page).not.toContain('does not yet offer an in-place rotation');
    } else {
      expect(page).toContain('does not yet offer an in-place rotation');
    }
  });

  it('bounds the usage range exactly as the usage page says', () => {
    const maxDays = /^MAX_USAGE_DAYS = (\d+)$/m.exec(ROUTER_PY)?.[1];
    const defaultDays = /^DEFAULT_USAGE_DAYS = (\d+)$/m.exec(ROUTER_PY)?.[1];
    expect(maxDays).toBeDefined();
    expect(defaultDays).toBeDefined();

    // The router's window is INCLUSIVE: `end - (DEFAULT_USAGE_DAYS - 1)` and
    // `(end - start).days + 1 > MAX_USAGE_DAYS`. The page once said "30 days
    // before end_date", one day off (verifier finding, 2026-09-13).
    expect(ROUTER_PY).toContain('default_start = end - timedelta(days=DEFAULT_USAGE_DAYS - 1)');
    expect(ROUTER_PY).toContain('(end - start).days + 1 > MAX_USAGE_DAYS');
    const page = findDocPage('usage')!.body;
    expect(page).toContain(`${maxDays} days`);
    expect(page).toContain(`${defaultDays}-day window ending at`);
    expect(page).toContain(`${Number(defaultDays) - 1} days before it`);
    expect(page).not.toContain(`Defaults to ${defaultDays} days before`);
    expect(page).toContain('start_date');
    expect(page).toContain('end_date');
  });

  it('prints the enforced-mode rate-limit headers in the shape quotas.py sends, inside the operator section only', () => {
    const quotas = repoFile('orchestrator', 'app', 'apiplatform', 'quotas.py');
    // The two draft-11 field names, and the item and parameter spellings
    // the sample on the page uses. Not the f-string byte for byte: the quota
    // module is being rewritten per project, and what the reader depends on
    // is the field syntax, which this pins.
    expect(quotas).toContain('"RateLimit-Policy"');
    expect(quotas).toMatch(/"RateLimit": f'"requests";r=\{[a-z_]+\};t=\{[a-z_]+\}'/);
    expect(quotas).toContain('"concurrency";q=');
    expect(quotas).toContain('qu="concurrent-requests"');
    // 2026-09-13: sent only with PUBLIC_API_ENFORCE_LIMITS on, so the page
    // shows the shape only under the operator heading, and as prose — there
    // is no header sample left for docs_examples_run.py to hold to a server
    // that, by default, sends none.
    const page = sectionOf(findDocPage('rate-limits')!.body, 'If an operator enables limits');
    expect(page).toContain('RateLimit: "requests";r=');
    expect(page).toContain('"concurrency";q=');
    expect(page).toContain('qu="concurrent-requests"');
    expect(page).not.toContain('~~~');
  });

  it('signs webhooks with the header the webhook page tells you to verify', () => {
    const signer = repoFile('orchestrator', 'app', 'apiplatform', 'webhooks', 'signer.py');
    expect(signer).toContain('SIGNATURE_HEADER = "TechSara-Signature"');
    expect(signer).toContain('SCHEME = "v1"');
    expect(signer).toContain('DEFAULT_TOLERANCE_SECONDS = 300');

    const page = findDocPage('webhooks')!.body;
    expect(page).toContain('TechSara-Signature: t=');
    expect(page).toContain('v1=');
    // The rotation rule the signer's docstring insists on: a consumer must
    // accept ANY matching v1, because two are sent during an overlap.
    expect(page).toContain('at least one');
    expect(page).toContain('TOLERANCE_S = 300');
  });

  it('names no webhook scope, because there is none', () => {
    // The scope vocabulary lost `webhooks.read` / `webhooks.manage` on
    // 2026-09-13: webhook management is a console capability with a person
    // behind it, not something a machine credential may do. A page that
    // still advertised the scope would be teaching an error.
    const scopesPy = repoFile('orchestrator', 'app', 'apiplatform', 'scopes.py');
    expect(scopesPy).not.toContain('"webhooks.manage"');
    for (const page of DOC_PAGES) {
      expect(page.body, `${page.slug}`).not.toMatch(/`webhooks\.(read|manage)`/);
    }
    expect(findDocPage('webhooks')!.body).toContain('api.webhooks.manage');
  });

  it('retries a delivery on the schedule the webhook page publishes', () => {
    const sender = repoFile('orchestrator', 'app', 'apiplatform', 'webhooks', 'sender.py');
    const attempts = /^MAX_ATTEMPTS = (\d+)$/m.exec(sender)?.[1];
    const base = /^BASE_DELAY_SECONDS = ([\d.]+)$/m.exec(sender)?.[1];
    expect(attempts).toBe('6');
    expect(base).toBe('10.0');

    const page = findDocPage('webhooks')!.body;
    expect(page).toContain('**6 attempts**');
    expect(page).toContain('10 seconds');
  });
});

describe('the unlimited API (owner decision, 2026-09-13)', () => {
  // The owner removed every usage limit from the public API: no requests or
  // tokens per minute, no daily or monthly quota, no per-project concurrency
  // cap, and no RateLimit headers. The server keeps the enforced mode behind
  // PUBLIC_API_ENFORCE_LIMITS (default false). A documentation site that
  // still promised 429s and headers would have a reader build throttling for
  // a limit that does not exist — these tests hold every page to the default.

  it('reads the switch that makes the API unlimited, defaulting to false, in config.py', () => {
    const configPy = repoFile('orchestrator', 'app', 'config.py');
    expect(configPy).toMatch(/_bool\("PUBLIC_API_ENFORCE_LIMITS", False\)/);
  });

  it('says first, before any table, that there are no usage limits and no RateLimit headers', () => {
    const page = findDocPage('rate-limits')!;
    const intro = page.body.slice(0, page.body.indexOf('\n## '));
    expect(intro).toContain('**The API currently enforces no usage limits.**');
    expect(intro).toContain('no limit on tokens per minute');
    expect(intro).toContain('no daily or monthly token quota');
    expect(intro).toContain('no cap on how many requests a project runs at once');
    expect(intro).toMatch(/no response carries a\s+`RateLimit` or `RateLimit-Policy` header/);
    expect(intro).not.toContain('|');
    expect(page.summary).toMatch(/no usage limits/);
  });

  it('keeps the technical limits and the 503 backoff that did not go away', () => {
    const errorsPy = repoFile('orchestrator', 'app', 'publicapi', 'errors.py');
    expect(errorsPy).toMatch(/"model_recovering": _CodeSpec\(503, /);
    expect(errorsPy).toMatch(/"request_too_large": _CodeSpec\(413, /);
    expect(errorsPy).toMatch(/"context_length_exceeded": _CodeSpec\(400, /);

    const page = findDocPage('rate-limits')!.body;
    const kept = sectionOf(page, 'What still applies');
    expect(kept).toContain('| Body bytes | 1 MiB |');
    expect(kept).toContain('`413 request_too_large`');
    expect(kept).toContain('`400 context_length_exceeded`');
    expect(kept).toContain('8,192');
    expect(kept).toContain("The engine's queue");

    const backoff = sectionOf(page, 'Backing off on 503');
    expect(backoff).toContain('`503 model_recovering`');
    expect(backoff).toContain('`Retry-After`');
    expect(backoff).toMatch(/jitter/i);
    expect(backoff).toMatch(/Cap your attempts/);
  });

  it('says usage is still recorded, because the ledgers are still written once per request', () => {
    const section = sectionOf(findDocPage('rate-limits')!.body, 'Usage is still recorded');
    expect(section).toContain('still counted, once');
    expect(section).toContain('`GET /v1/usage`');
  });

  it('names a usage-limit refusal or a RateLimit header on no page, except as the operator-enabled mode', () => {
    // A unit that names one of these must also say why it is not a default
    // behaviour: the operator switch, the engine's own queue (a technical
    // limit the owner kept), or that the API enforces none.
    const excuse = /operator|engine|enforces no|no longer enforces|\bno\b[^.]*`RateLimit`|neither/i;
    const named = /`quota_exceeded`|`concurrency_limit_exceeded`|`RateLimit(?:-Policy)?`/;
    const offenders: string[] = [];
    for (const page of DOC_PAGES) {
      const body =
        page.slug === 'rate-limits'
          ? page.body.replace(sectionOf(page.body, 'If an operator enables limits'), '')
          : page.body;
      for (const unit of proseUnitsOf(body)) {
        if (named.test(unit) && !excuse.test(unit)) offenders.push(`${page.slug}: ${unit.slice(0, 120)}`);
      }
    }
    expect(offenders).toEqual([]);

    // The exact claims the pages made before the decision, each of which is
    // now false on a default deployment.
    const retired = [
      /counts against (?:your|the project's)\s+\[rate limits\]/,
      /Read the `RateLimit` headers/,
      /Your project reached a limit/,
      /Rate-limit headers\*\* on every response/,
      /`RateLimit` headers, which are live/,
      /\*\*rate, quota and concurrency\*\* — over any of them is `429`/,
      /Rate and token limits \| A key whose abuse has a ceiling/,
      /prints `X-Request-Id` \(log it\), `RateLimit`/,
      /refused with `429 concurrency_limit_exceeded` and a/,
    ];
    for (const page of DOC_PAGES) {
      for (const claim of retired) expect(page.body, `${page.slug} ${claim}`).not.toMatch(claim);
    }
  });

  it('marks the three 429 codes in every retry sample as arriving only when an operator enables limits', () => {
    for (const slug of ['errors', 'python', 'javascript']) {
      const body = findDocPage(slug)!.body;
      expect(body, slug).toContain('"quota_exceeded", "concurrency_limit_exceeded"');
      expect(body, slug).toMatch(/quota_exceeded and concurrency_limit_exceeded arrive only if an operator\s+(?:#|\/\/) enables limits/);
    }
  });

  it('records the removal in the changelog, dated 2026-09-13, as a decision, newest first', () => {
    const changelog = findDocPage('changelog')!.body;
    const headings = docHeadingsOf(changelog).map((heading) => heading.text);
    // Newest first: the all-models entry of the same day came after it.
    expect(headings[0]).toBe(ALL_MODELS_ENTRY);
    expect(headings[1]).toBe('2026-09-13 — usage limits removed');
    const entry = sectionOf(changelog, '2026-09-13 — usage limits removed');
    expect(entry).toContain('by decision');
    expect(entry).toContain('`PUBLIC_API_ENFORCE_LIMITS`, off by default');
    expect(entry).toContain('`503 model_recovering` with `Retry-After`');
  });

  it('shows no RateLimit header in the curl sample, and the example runner fails a server that sends one', () => {
    const curl = findDocPage('curl')!.body;
    expect(curl).toContain('There is no `RateLimit` header to read');
    const runner = repoFile('scripts', 'docs_examples_run.py');
    expect(runner).toContain('absent_headers=("ratelimit", "ratelimit-policy")');
    expect(runner).not.toMatch(/^def chk_ratelimit\(/m);
    expect(runner).not.toMatch(/^\s+S\["rate-limits", \d+\] = Spec\(/m);
    expect(findDocPage('rate-limits')!.body).not.toContain('~~~');
  });
});

/**
 * Every model TechSara runs, on /v1, and output up to 1,000,000 tokens
 * (owner request and owner decision, 2026-09-13).
 *
 * The pages were written while the code for the same wave was being built in
 * parallel, so they are held to CONTRACT.md — the document that wave builds
 * against — and to the primary sources that already exist (audio_api.py,
 * llm.py, config.py). Where a code file is still catching up, the test says
 * so in a comment and tightens itself the day it lands.
 */
describe('every model on the API (owner request, 2026-09-13)', () => {
  const NEW_ROUTES: [string, string, string, string, string][] = [
    ['POST', '/v1/embeddings', 'embeddings.write', 'embeddings', EMBED_MODEL_ID],
    ['POST', '/v1/rerank', 'rerank.write', 'rerank', RERANK_MODEL_ID],
    ['POST', '/v1/audio/transcriptions', 'audio.write', 'audio-transcriptions', WHISPER_MODEL_ID],
  ];
  const escapeRe = (text: string) => text.replace(/[.*+?^${}()|[\]\\/]/g, '\\$&');

  /** CONTRACT §12.2's per-model ceilings table. */
  function contractCeilings() {
    const section = contractSection('### 12.2 Technical ceilings', '### 12.3');
    const rows = [
      ...section.matchAll(
        /^\| `(techsara-[a-z0-9-]+)` \| ([a-z]+) \| ([^|]+) \| ([^|]+) \| ([^|]+) \| ([^|]+) \| ([^|]+) \|$/gm,
      ),
    ];
    return new Map(
      rows.map((m) => [
        m[1],
        {
          kind: m[2],
          context: numberOrNull(m[3]),
          input: numberOrNull(m[4]),
          output: numberOrNull(m[5]),
          defaultOutput: numberOrNull(m[6]),
          other: m[7],
        },
      ]),
    );
  }

  /** Every fenced json block on a page that parses. */
  function jsonSamples(body: string): unknown[] {
    const out: unknown[] = [];
    for (const match of body.matchAll(/^~~~json\n([\s\S]*?)^~~~$/gm)) {
      try {
        out.push(JSON.parse(match[1]));
      } catch {
        // A request fragment with an elided value is prose, not a sample.
      }
    }
    return out;
  }

  it('publishes the three new routes in CONTRACT §7 with exactly one scope each, eleven routes in all', () => {
    expect(contractRoutes().size).toBe(11);
    for (const [method, path, scope] of NEW_ROUTES) {
      expect(CONTRACT).toMatch(
        new RegExp(`^\\| ${method} \\| \`${escapeRe(path)}\` \\| \`${escapeRe(scope)}\` \\|`, 'm'),
      );
    }
  });

  it('gives each new endpoint its own reference page, naming its scope, its model and the refusal of Idempotency-Key', () => {
    const reference = DOC_SECTIONS.find((section) => section.title === 'API reference')!;
    for (const [method, path, scope, slug, model] of NEW_ROUTES) {
      const page = findDocPage(slug);
      expect(page, slug).toBeDefined();
      expect(reference.pages).toContain(page);
      expect(page!.body).toContain(`${method} ${path}`);
      expect(page!.body).toContain(`\`${scope}\``);
      expect(page!.body).toContain(model);
      expect(page!.body).toContain('Keys created before 2026-09-13');
      expect(page!.body).toMatch(/`Idempotency-Key` (?:header|is refused)/);
    }
    for (const slug of ['images', 'long-output']) {
      expect(reference.pages).toContain(findDocPage(slug));
    }
  });

  it('lists all six models on the models page, with the ceilings CONTRACT §12.2 publishes', () => {
    const ceilings = contractCeilings();
    expect([...ceilings.keys()]).toEqual([...MODEL_IDS]);

    const page = findDocPage('models')!.body;
    const catalogue = jsonSamples(page).find(
      (sample) => (sample as { object?: string }).object === 'list',
    ) as {
      data: {
        id: string;
        kind: unknown;
        context_window: unknown;
        max_input_tokens: unknown;
        max_output_tokens: unknown;
        default_max_output_tokens: unknown;
        capabilities: { tools: unknown };
        endpoints: string[];
        limits: Record<string, number | undefined>;
      }[];
    };
    expect(catalogue.data.map((model) => model.id)).toEqual([...MODEL_IDS]);

    const routes = contractRoutes();
    for (const model of catalogue.data) {
      const row = ceilings.get(model.id)!;
      expect(model.kind, model.id).toBe(row.kind);
      expect(model.context_window, model.id).toBe(row.context);
      expect(model.max_input_tokens, model.id).toBe(row.input);
      expect(model.max_output_tokens, model.id).toBe(row.output);
      expect(model.default_max_output_tokens, model.id).toBe(row.defaultOutput);
      expect(model.capabilities.tools).toBe(false);
      for (const endpoint of model.endpoints) expect(routes).toContain(endpoint);

      const count = (pattern: RegExp) => Number((pattern.exec(row.other)?.[1] ?? 'NaN').replace(/,/g, ''));
      if (model.limits.max_images_per_request !== undefined) {
        expect(model.limits.max_images_per_request).toBe(count(/(\d+) images? per request/));
      }
      if (model.limits.max_inputs_per_request !== undefined) {
        expect(model.limits.max_inputs_per_request).toBe(count(/(\d+) inputs/));
        expect(model.limits.embedding_dimensions).toBe(count(/([\d,]+) dimensions/));
      }
      if (model.limits.max_documents_per_request !== undefined) {
        expect(model.limits.max_documents_per_request).toBe(count(/(\d+) documents/));
      }
      if (model.limits.max_audio_seconds !== undefined) {
        expect(model.limits.max_audio_seconds).toBe(count(/(\d+) s\b/));
        expect(model.limits.max_audio_bytes).toBe(count(/(\d+) MiB/) * 1024 * 1024);
      }
      // A heading of its own, so a link can point at the model.
      expect(page).toContain(`\n### ${model.id}\n`);
    }
  });

  it('names no internal checkpoint, engine, host or port on any page', () => {
    // CONTRACT §15: internal checkpoint names and engine URLs never leave the
    // server — and a documentation site is the widest-read place they could.
    const configPy = repoFile('orchestrator', 'app', 'config.py');
    const checkpoints = [
      ...new Set(
        [...configPy.matchAll(/"((?:Qwen|nvidia|baidu|openai)\/[A-Za-z0-9._-]+)"/g)].map((m) => m[1]),
      ),
    ];
    expect(checkpoints.length).toBeGreaterThanOrEqual(4);
    const tails = checkpoints.map((name) => name.split('/')[1].toLowerCase());
    const machinery = [
      /qwen/i, /unlimited-ocr/i, /whisper-large/i, /\bvllm\b/i, /nvfp4/i, /\bbaidu\b/i,
      /sf-local-ai/i, /\b192\.168\.\d+\.\d+/, /:30\d{3}\b/,
    ];
    const offenders: string[] = [];
    for (const page of DOC_PAGES) {
      const text = `${page.title}\n${page.summary}\n${page.body}`;
      for (const tail of tails) if (text.toLowerCase().includes(tail)) offenders.push(`${page.slug}: ${tail}`);
      for (const pattern of machinery) if (pattern.test(text)) offenders.push(`${page.slug}: ${pattern}`);
    }
    expect(offenders).toEqual([]);
  });

  it('lists exactly the audio types audio_api.py accepts', () => {
    const audioPy = repoFile('orchestrator', 'app', 'audio_api.py');
    const block = /^ALLOWED_TYPES = \{([\s\S]*?)^\}/m.exec(audioPy);
    expect(block).not.toBeNull();
    const allowed = [...block![1].matchAll(/"([a-z0-9.+-]+\/[a-z0-9.+-]+)"/g)].map((m) => m[1]).sort();
    expect(allowed.length).toBeGreaterThan(10);

    const fields = sectionOf(findDocPage('audio-transcriptions')!.body, 'The fields');
    const listing = fields.slice(fields.indexOf('Accepted audio types'));
    const listed = [...listing.matchAll(/`([a-z0-9.+-]+\/[a-z0-9.+-]+)`/g)].map((m) => m[1]).sort();
    expect(listed).toEqual(allowed);
    expect(CONTRACT).toContain('`audio_api.ALLOWED_TYPES`');
  });

  it('states the image rules the contract fixes: data URLs only, four types, the three byte caps and the per-model counts', () => {
    const rules = contractSection('### 8.1 `POST /v1/responses`', '### 8.2');
    const page = findDocPage('images')!.body;
    for (const type of ['image/png', 'image/jpeg', 'image/webp', 'image/gif']) {
      expect(rules).toContain(`\`${type}\``);
      expect(page).toContain(`\`${type}\``);
    }
    for (const cap of ['20 MiB', '10 MiB', '1 MiB']) {
      expect(rules).toContain(cap);
      expect(page).toContain(`**${cap}**`);
    }
    expect(page).toContain('`https://`, `http://`, `file:` and every other scheme are refused');
    expect(rules).toContain('a test proves an `http://` URL never reaches the stub engine');

    const ceilings = contractCeilings();
    const counts: [string, RegExp][] = [
      [MODEL_ID, /\| `techsara-35b` \| up to (\d+) \|/],
      [VISION_MODEL_ID, /\| `techsara-8b-vision` \| up to (\d+) \|/],
      [OCR_MODEL_ID, /\| `techsara-ocr` \| exactly (\d+) \|/],
    ];
    for (const [id, row] of counts) {
      const documented = row.exec(page)?.[1];
      const contracted = /(\d+) images? per request/.exec(ceilings.get(id)!.other)?.[1];
      expect(documented, id).toBeDefined();
      expect(documented, id).toBe(contracted);
    }
  });

  it('computes every wall clock and duration on the long-output page from the contract formula', () => {
    const contract = contractSection('### 8.3 The output ceiling', '### 8.4');
    expect(contract).toContain('`PUBLIC_API_GEN_WALL_CLOCK_S` defaults to 21,600 s');
    expect(contract).toContain('`PUBLIC_API_MAIN_PREFILL_ALLOWANCE_S` 900 s');
    expect(contract).toContain('`PUBLIC_API_MAIN_MIN_DECODE_TOKENS_PER_S` 50');
    expect(contract).toContain('`GEN_WALL_CLOCK_S` (4,200 s in `.env`)');
    expect(contract).toContain('reserve 512 (`CONTEXT_SAFETY_MARGIN`)');

    const page = findDocPage('long-output')!.body;
    expect(page).toContain('min(21600, max(4200, 900 + planned_max_output_tokens / 50))');

    const clock = sectionOf(page, 'The wall clock');
    const rows = [...clock.matchAll(/^\| ([\d,]+)(?: \(the default\))? \| ([\d,]+) s — /gm)];
    expect(rows).toHaveLength(4);
    for (const [, tokens, seconds] of rows) {
      const n = Number(tokens.replace(/,/g, ''));
      expect(Number(seconds.replace(/,/g, '')), tokens).toBe(
        Math.min(21600, Math.max(4200, Math.round(900 + n / 50))),
      );
    }

    // The clamp example: 1,000,000 window − 300,000 prompt − 512 reserve.
    expect(page).toContain(`about ${(1_000_000 - 300_000 - 512).toLocaleString('en-GB')}`);

    const phrase = (seconds: number): string => {
      if (seconds < 100) return `${Math.round(seconds)} seconds`;
      const minutes = Math.round(seconds / 60);
      if (minutes < 60) return `${minutes} minutes`;
      return `${Math.floor(minutes / 60)} hours ${minutes % 60} minutes`;
    };
    const durations = sectionOf(page, 'How long it takes');
    const durationRows = [...durations.matchAll(/^\| ([\d,]+) \| ([^|]+) \| ([^|]+) \|$/gm)];
    expect(durationRows).toHaveLength(4);
    for (const [, tokens, at100, at70] of durationRows) {
      const n = Number(tokens.replace(/,/g, ''));
      expect(at100.trim(), tokens).toBe(phrase(n / 100));
      expect(at70.trim(), tokens).toBe(phrase(n / 70));
    }
  });

  it('says a synchronous request is unsuitable above about 5,000 output tokens, with the two timeouts behind it', () => {
    const contract = contractSection('### 8.3 The output ceiling', '### 8.4');
    expect(contract).toContain('response timeout is 100 s (HTTP 524)');
    expect(contract).toContain('default 300 s header and body timeouts');

    const page = sectionOf(findDocPage('long-output')!.body, 'Choose the mode before you choose the size');
    expect(page).toContain('**100 seconds**');
    expect(page).toContain('HTTP `524`');
    expect(page).toContain('300 seconds');
    expect(page).toContain('about **5,000 tokens**');
    expect(page).toContain('`"stream": true`');
    expect(page).toContain('`"background": true`');
    expect(findDocPage('responses')!.body).toContain('Above about 5,000 output tokens');
  });

  it('ties the wall-clock caveat to llm.py itself, so it goes the day the per-request clock ships', () => {
    // The 1,000,000 ceiling is only deliverable once llm.stream_chat_events
    // accepts a per-call wall clock (CONTRACT §8.3, a separate integration).
    // Until then the pages say so; the day the parameter appears this fails
    // until LONG_OUTPUT_WALL_CLOCK_LIVE is flipped, and the caveat goes.
    const llmPy = repoFile('orchestrator', 'app', 'llm.py');
    const start = llmPy.indexOf('async def stream_chat_events(');
    expect(start).toBeGreaterThanOrEqual(0);
    const signature = llmPy.slice(start, llmPy.indexOf('->', start));
    expect(LONG_OUTPUT_WALL_CLOCK_LIVE).toBe(/\bwall_clock_s\b/.test(signature));

    for (const slug of ['long-output', 'changelog']) {
      const body = findDocPage(slug)!.body;
      expect(body.includes(WALL_CLOCK_PENDING_NOTE), slug).toBe(!LONG_OUTPUT_WALL_CLOCK_LIVE);
    }
    expect(WALL_CLOCK_PENDING_NOTE).toContain('4,200 seconds');
  });

  it('describes capacity refusals as a 503 per engine, shared by every caller, with the Retry-After the contract sets', () => {
    const gates = contractSection('### 12.3 Capacity gates', '### 12.4');
    const gateRows = new Map(
      [...gates.matchAll(/^\| `([a-z.]+)` \| (\d+)[^|]* \| [^|]+ \| [^|]+ \| [^|]+ \| (\d+) s \|$/gm)].map(
        (m) => [m[1], { concurrency: m[2], retryAfter: m[3] }],
      ),
    );
    // Seven since 2026-09-13's adversarial review added `main.extended`.
    expect(gateRows.size).toBe(7);
    expect(gateRows.get('main.extended')?.retryAfter).toBe(gateRows.get('main.long')?.retryAfter);

    const section = sectionOf(findDocPage('rate-limits')!.body, 'Capacity queues, per engine');
    expect(section).toContain('never a `429`');
    expect(section).toContain('**They belong to the engine, not to you.**');
    const byModel: [string, string, RegExp | null][] = [
      [MODEL_ID, 'main.long', null],
      [VISION_MODEL_ID, 'router', /\| (\d+), and a bounded share/],
      [OCR_MODEL_ID, 'ocr', /\| (\d+), stepping aside/],
      [EMBED_MODEL_ID, 'embed', /\| (\d+), and a bounded share/],
      [RERANK_MODEL_ID, 'rerank', /\| (\d+), and a bounded share/],
      [WHISPER_MODEL_ID, 'asr', /\| (\d+) across the whole deployment/],
    ];
    for (const [id, gate, concurrency] of byModel) {
      const row = new RegExp(`^\\| \`${escapeRe(id)}\` \\|[^\\n]*$`, 'm').exec(section)?.[0];
      expect(row, id).toBeDefined();
      expect(row, id).toContain(`\`Retry-After\` ${gateRows.get(gate)!.retryAfter} s`);
      if (concurrency) expect(concurrency.exec(row!)?.[1], id).toBe(gateRows.get(gate)!.concurrency);
    }
    // The pre-decision sentence that called a full queue a 429 is gone.
    for (const page of DOC_PAGES) {
      expect(page.body, page.slug).not.toContain('Treat a `429 concurrency_limit_exceeded` from a full');
    }
  });

  it('refuses Idempotency-Key on exactly the three new endpoints, and leases a running claim for 13 hours', () => {
    const contract = contractSection('## 13. Idempotency', '## 14.');
    expect(contract).toContain('`POST /v1/responses` and `POST /v1/chat/completions` **only**');
    for (const [, path] of NEW_ROUTES) expect(contract).toContain(`\`${path}\``);
    expect(contract).toContain('`param: Idempotency-Key`');
    // 2 × PUBLIC_API_GEN_WALL_CLOCK_S (21,600) + PUBLIC_API_BACKGROUND_GATE_WAIT_S (3,600).
    expect(contract).toContain(`${(2 * 21600 + 3600).toLocaleString('en-GB')} s`);
    expect(2 * 21600 + 3600).toBe(13 * 3600);

    const page = findDocPage('idempotency')!.body;
    expect(page).toContain('**Not accepted on**');
    expect(page).toContain('`400 invalid_request_error` with `param` `Idempotency-Key`');
    for (const slug of ['embeddings', 'rerank', 'audio-transcriptions']) {
      expect(page).toContain(`](/docs/${slug})`);
    }
    expect(page).toContain('13 hours');
    // The still-running refusal is a 409 since the limits were removed.
    expect(page).not.toContain('This `429` is not a usage limit');
  });

  it('tells holders of older keys the three new scopes are not theirs, and lists the defaults scopes.py grants', () => {
    const authentication = findDocPage('authentication')!.body;
    expect(authentication).toContain('**Keys created before 2026-09-13 do not have the three newest scopes.**');
    expect(contractSection('## 7. Public endpoints', '## 8.')).toContain('A key created before 2026-09-13');
    expect(sectionOf(findDocPage('changelog')!.body, ALL_MODELS_ENTRY)).toContain(
      '**Keys created before\n  this change do not have them**',
    );

    const defaults = [...contractScopes().keys()].filter((scope) =>
      new RegExp(`^\\| \`${escapeRe(scope)}\` \\| [^|]+ \\| yes \\|$`, 'm').test(CONTRACT),
    );
    expect(defaults).toHaveLength(6);
    const paragraph = authentication.slice(authentication.indexOf('A key created without a choice gets'));
    const sentence = paragraph.slice(0, paragraph.indexOf('\n\n'));
    for (const scope of defaults) expect(sentence).toContain(`\`${scope}\``);
    expect(sentence).not.toContain('`usage.read`');

    // Whatever scopes.py grants by default today is a subset of that sentence.
    const scopesPy = repoFile('orchestrator', 'app', 'apiplatform', 'scopes.py');
    const block = /^DEFAULT_SCOPES[^=]*= frozenset\(\s*\{([^}]*)\}/m.exec(scopesPy);
    expect(block).not.toBeNull();
    for (const member of block![1].matchAll(/Scope\.([A-Z_]+)/g)) {
      expect(sentence).toContain(`\`${member[1].toLowerCase().replace('_', '.')}\``);
    }
  });

  it('prints max_output_tokens and incomplete_details on every response object, and the applied ceiling on a chat completion', () => {
    const offenders: string[] = [];
    for (const page of DOC_PAGES) {
      const objects: Record<string, unknown>[] = [];
      for (const sample of jsonSamples(page.body)) {
        if ((sample as { object?: string }).object === 'response') objects.push(sample as Record<string, unknown>);
      }
      for (const match of page.body.matchAll(/^data: (\{.*\})$/gm)) {
        const frame = JSON.parse(match[1]);
        if (frame.response) objects.push(frame.response);
      }
      for (const object of objects) {
        if (!('max_output_tokens' in object) || !('incomplete_details' in object)) {
          offenders.push(`${page.slug}: ${JSON.stringify(object).slice(0, 60)}`);
        }
      }
    }
    expect(offenders).toEqual([]);
    expect(contractSection('## 9. Response and error envelope', '## 10.')).toContain(
      '"max_output_tokens": 8192,\n  "incomplete_details": null,',
    );

    const chat = findDocPage('chat-completions')!.body;
    const completion = jsonSamples(chat).find(
      (sample) => (sample as { object?: string }).object === 'chat.completion',
    ) as Record<string, unknown>;
    expect(completion.max_output_tokens).toBe(8192);
    const finishChunk = [...chat.matchAll(/^data: (\{.*\})$/gm)]
      .map((m) => JSON.parse(m[1]))
      .find((frame) => frame.choices?.[0]?.finish_reason);
    expect(finishChunk.max_output_tokens).toBe(8192);
  });

  it('warns that a forced language translates, and that the OCR model reads best with no prompt at all', () => {
    // The contract wraps its prose at 80 columns; compare it as running text.
    const prose = (text: string) => text.replace(/\s+/g, ' ');
    expect(prose(contractSection('### 8.6', '## 9.'))).toContain('translates rather than transcribes');
    expect(prose(contractSection('### 8.1', '### 8.2'))).toContain('the server appends the text part `OCR`');
    for (const slug of ['models', 'audio-transcriptions']) {
      const body = findDocPage(slug)!.body;
      expect(body, slug).toMatch(/`en`[^.]*translation/);
    }
    for (const slug of ['models', 'images']) {
      const body = findDocPage(slug)!.body;
      expect(body, slug).toMatch(/the server adds the (?:plain|one-word) instruction `OCR`/);
    }
  });

  it('keeps audio seconds out of GET /v1/usage, as the contract records them', () => {
    expect(contractSection('## 16. Recording', '## 17.')).toContain('Audio seconds are **not** in');
    expect(findDocPage('usage')!.body).toContain('**audio seconds\nare not in it**');
  });

  it('records the change in the changelog, dated 2026-09-13, as the newest entry', () => {
    const changelog = findDocPage('changelog')!.body;
    expect(docHeadingsOf(changelog)[0].text).toBe(ALL_MODELS_ENTRY);
    const entry = sectionOf(changelog, ALL_MODELS_ENTRY);
    for (const id of MODEL_IDS) expect(entry).toContain(`\`${id}\``);
    for (const [method, path] of NEW_ROUTES) expect(entry).toContain(`\`${method} ${path}\``);
    expect(entry).toContain('`max_output_tokens` up to 1,000,000');
    expect(entry).toContain('**clamped** instead of refused');
    expect(entry).toContain('`incomplete_details`');
    expect(entry).toContain('never a `429`');
    expect(entry).toContain('**Examples are still marked as not executed.**');
  });
});

describe('the navigation', () => {
  it('renders every section and every page in the registry', () => {
    render(<DocsNav sections={DOC_SECTIONS} currentHref="/docs/errors" />);

    const nav = screen.getByRole('navigation', { name: 'Documentation' });
    for (const section of DOC_SECTIONS) {
      expect(within(nav).getByRole('heading', { name: section.title })).toBeTruthy();
    }
    for (const page of DOC_PAGES) {
      const link = within(nav).getByRole('link', { name: page.title });
      expect(link.getAttribute('href')).toBe(docHref(page.slug));
    }
    expect(within(nav).getAllByRole('link')).toHaveLength(DOC_PAGES.length);
  });

  it('marks the page being read with aria-current, not colour alone', () => {
    render(<DocsNav sections={DOC_SECTIONS} currentHref="/docs/errors" />);
    const current = screen.getByRole('link', { name: 'Errors' });
    expect(current.getAttribute('aria-current')).toBe('page');
    expect(
      screen.getByRole('link', { name: 'Quickstart' }).getAttribute('aria-current'),
    ).toBeNull();
  });

  it('puts the overview at /docs rather than /docs/overview', () => {
    expect(docHref(OVERVIEW_SLUG)).toBe('/docs');
    expect(docHref('errors')).toBe('/docs/errors');
  });

  it('lists every page in exactly one section, under the title it claims', () => {
    const seen = new Set<string>();
    for (const section of DOC_SECTIONS) {
      for (const page of section.pages) {
        expect(page.section).toBe(section.title);
        expect(seen.has(page.slug), `${page.slug} is listed twice`).toBe(false);
        seen.add(page.slug);
      }
    }
    expect(seen.size).toBe(DOC_PAGES.length);
  });

  it('chains the pages in reading order', () => {
    expect(neighboursOf(DOC_PAGES[0].slug).previous).toBeUndefined();
    expect(neighboursOf(DOC_PAGES[0].slug).next?.slug).toBe(DOC_PAGES[1].slug);
    const last = DOC_PAGES[DOC_PAGES.length - 1];
    expect(neighboursOf(last.slug).next).toBeUndefined();
  });
});

describe('a documentation page', () => {
  it('renders its title, its summary and its prose', () => {
    const page = findDocPage('errors')!;
    render(<DocsArticle page={page} />);

    expect(screen.getByRole('heading', { level: 1, name: page.title })).toBeTruthy();
    expect(screen.getByText(page.summary)).toBeTruthy();
    expect(screen.getByRole('heading', { level: 2, name: /The envelope/ })).toBeTruthy();
  });

  it('gives every heading on every page an id its own contents rail can reach', () => {
    // Every page, not one (verifier finding, 2026-09-13: a heading with a link
    // in it slugged differently on the two sides and only `errors` was
    // checked). The "On this page" ids come from the source; the DOM ids from
    // the renderer; they must be the same set.
    for (const page of DOC_PAGES) {
      cleanup();
      const { container } = render(<DocsArticle page={page} />);
      const rendered = new Set(
        [...container.querySelectorAll('article h2[id], article h3[id]')].map((node) => node.id),
      );
      const listed = docHeadingsOf(page.body).map((heading) => heading.id);
      expect([...rendered].sort(), `${page.slug}`).toEqual([...listed].sort());

      for (const anchor of container.querySelectorAll('h2 a[href^="#"], h3 a[href^="#"]')) {
        const id = anchor.getAttribute('href')!.slice(1);
        expect(rendered.has(id)).toBe(true);
        // Named for a screen reader, because "#" is not a link text.
        expect(anchor.getAttribute('aria-label')).toMatch(/^Link to this section: /);
      }
    }
  });

  it('slugs a heading that contains a link the same way in the rail and in the DOM', () => {
    const body = '## Limits attached to a [model](/docs/rate-limits)\n\ntext';
    const { container } = render(<DocsMarkdown body={body} />);
    const id = container.querySelector('h2')?.id;
    expect(id).toBe('limits-attached-to-a-model');
    expect(docHeadingsOf(body).map((heading) => heading.id)).toEqual([id]);
  });

  // 2026-09-13, wave-3 re-verify: the rail was a regex over the source and
  // disagreed with the DOM on `&amp;` (a-amp-b vs a-b) and on `[text][ref]`
  // (see-the-ref-r vs see-the-ref). Every probe below is rendered and listed,
  // and the two sides must agree — on the id AND on a non-empty, expected one.
  it.each([
    ['an HTML entity', '## A &amp; B', 'a-b'],
    ['a numeric entity', '## Caf&#233; &#x26; bar', 'caf-bar'],
    ['a resolved reference-style link', '## See [the ref][r]\n\n[r]: /docs/errors', 'see-the-ref'],
    ['a collapsed reference link', '## See [errors][]\n\n[errors]: /docs/errors', 'see-errors'],
    ['an unresolved reference, which stays literal text', '## See [the ref][nowhere]', 'see-the-ref-nowhere'],
    ['an image', '## Logo ![the mark](/logo.png) here', 'logo-here'],
    ['inline HTML, which the renderer shows as literal text', '## Before <br> after', 'before-br-after'],
    ['an escaped marker', '## Five \\* two', 'five-two'],
    ['strikethrough and emphasis', '## ~~Old~~ _new_ **bold** `code`', 'old-new-bold-code'],
    ['an autolink literal', '## Call https://example.com now', 'call-https-example-com-now'],
    ['a closing-hash sequence', '## Trailing hashes ##', 'trailing-hashes'],
    ['a setext heading', 'Setext style\n------------', 'setext-style'],
  ])('slugs a heading with %s the same way in the rail and in the DOM', (_label, body, expected) => {
    const { container } = render(<DocsMarkdown body={body} />);
    const rendered = [...container.querySelectorAll('h2[id], h3[id]')].map((node) => node.id);
    expect(rendered).toEqual([expected]);
    expect(docHeadingsOf(body).map((heading) => heading.id)).toEqual(rendered);
  });

  it('lists no heading from inside a fenced code sample', () => {
    const body = '## Real\n\n```python\n## not a heading\n```\n\n~~~\n### nor this\n~~~\n';
    expect(docHeadingsOf(body).map((heading) => heading.id)).toEqual(['real']);
  });

  it('never leaks the markdown node object onto a DOM element', () => {
    const { container } = render(<DocsArticle page={findDocPage('errors')!} />);
    expect(container.querySelectorAll('[node]')).toHaveLength(0);
  });

  it('treats a protocol-relative link as external, with noopener', () => {
    const { container } = render(
      <DocsMarkdown body={'[a](//evil.example.com/x) and [b](/docs/errors)'} />,
    );
    const external = container.querySelector('a[href="//evil.example.com/x"]');
    expect(external?.getAttribute('rel')).toBe('noopener noreferrer');
    expect(external?.getAttribute('target')).toBe('_blank');
    const internal = container.querySelector('a[href="/docs/errors"]');
    expect(internal?.getAttribute('target')).toBeNull();
  });

  it('resolves every in-page and cross-page anchor in the whole site', () => {
    const offenders: string[] = [];
    for (const { from, href } of internalLinks()) {
      const [path, fragment] = href.split('#');
      const slug =
        path === '' ? from : path === '/docs' ? OVERVIEW_SLUG : path.replace('/docs/', '');

      if (!findDocPage(slug)) {
        offenders.push(`${from} -> ${href} (no such page)`);
        continue;
      }
      if (fragment && !anchorsOf(slug).has(fragment)) {
        offenders.push(`${from} -> ${href} (no such heading)`);
      }
    }
    expect(offenders).toEqual([]);
  });

  it('repeats no heading, so no two anchors collide', () => {
    for (const page of DOC_PAGES) {
      const ids = docHeadingsOf(page.body).map((heading) => heading.id);
      expect(new Set(ids).size, `${page.slug} repeats a heading`).toBe(ids.length);
      // An empty id would be a link to the top of the page pretending to be
      // a link to a section.
      expect(ids.every((id) => id.length > 0)).toBe(true);
    }
  });

  it('gives every code sample a copy button and a language label', () => {
    const page = findDocPage('quickstart')!;
    const { container } = render(<DocsArticle page={page} />);

    const blocks = container.querySelectorAll('.code-block');
    expect(blocks.length).toBeGreaterThan(3);
    for (const block of blocks) {
      expect(within(block as HTMLElement).getByRole('button', { name: 'Copy code' })).toBeTruthy();
      expect(block.querySelector('pre')?.getAttribute('tabindex')).toBe('0');
      // The label is the header's text, and it names the language the
      // highlighter was given — not merely "some span exists".
      const language =
        /language-([\w-]+)/.exec(block.querySelector('pre code')?.className ?? '')?.[1] ?? 'text';
      const label = [...block.querySelectorAll('span')].find(
        (span) => span.textContent?.trim().toLowerCase() === language.toLowerCase(),
      );
      expect(label, `a "${language}" label on the block`).toBeTruthy();
    }
    // rehype-highlight tags the tokens, which is what makes the colours the
    // same as a code block in the chat.
    expect(container.querySelector('pre code.language-bash')).toBeTruthy();
  });

  it('wraps tables so a wide one scrolls instead of widening the page', () => {
    // The error table has four columns and cannot fit 400px. The body must
    // never scroll sideways; the table may.
    const { container } = render(<DocsArticle page={findDocPage('errors')!} />);
    const wraps = container.querySelectorAll('.md-table-wrap');
    expect(wraps.length).toBeGreaterThan(0);
    for (const wrap of wraps) {
      expect(wrap.querySelector('table')).toBeTruthy();
    }
  });

  it('offers the next page at the foot', () => {
    render(<DocsArticle page={findDocPage('quickstart')!} />);
    const nav = screen.getByRole('navigation', { name: 'Page navigation' });
    const next = neighboursOf('quickstart').next!;
    expect(within(nav).getByRole('link', { name: new RegExp(next.title) })).toBeTruthy();
  });
});

describe('the honesty of the examples (CONTRACT §17)', () => {
  it('marks every page with the one shared example status, visibly', () => {
    for (const page of DOC_PAGES) {
      cleanup();
      // One value for the whole site, so no page can claim a status the
      // others do not and the integration lead flips exactly one switch.
      expect(page.examples, page.slug).toBe(EXAMPLE_STATUS);
      render(<DocsArticle page={page} />);
      const notice = screen.getByTestId('docs-example-status');
      expect(notice.getAttribute('role')).toBe('note');
      expect(notice.getAttribute('data-executed')).toBe(page.examples.executed ? 'true' : 'false');
      expect(notice.textContent).toContain(page.examples.note);
      expect(notice.textContent).toContain(
        page.examples.executed ? 'Examples verified' : 'Examples not yet executed',
      );
    }
  });

  it('tells the truth about the routes: they are mounted, the examples are what is unrun', () => {
    // 2026-09-13, verifier finding: every page said "the /v1 routes are still
    // being built" while router.py declared them and main.py mounted them.
    // The premise is checked against the code, and the notice against it.
    const routerPy = repoFile('orchestrator', 'app', 'publicapi', 'router.py');
    const mainPy = repoFile('orchestrator', 'app', 'main.py');
    expect(routerPy).toContain('router = APIRouter(prefix="/v1"');
    expect(mainPy).toMatch(/from \.publicapi\.router import router as public_api_router/);
    expect(mainPy).toContain('app.include_router(public_api_router)');

    const allNotes = `${NOT_EXECUTED_NOTE} ${EXECUTED_NOTE}`;
    expect(allNotes).not.toMatch(/being built|do(?:es)? not exist|no routes|not yet mounted/i);
    expect(NOT_EXECUTED_NOTE).toContain('routes these examples call are live');
    expect(NOT_EXECUTED_NOTE).toContain('not yet been executed against a running deployment');
    expect(EXAMPLE_STATUS.note).toBe(EXAMPLES_EXECUTED ? EXECUTED_NOTE : NOT_EXECUTED_NOTE);

    for (const page of DOC_PAGES) {
      expect(page.body, page.slug).not.toMatch(/routes are still being built/i);
    }
  });

  it('allows "executed" only with a changelog entry for the run behind it', () => {
    // Flipping EXAMPLES_EXECUTED is a claim about a run that happened. The
    // run is recorded under a changelog heading containing "examples
    // executed"; without one the flag is refused. While the flag is false,
    // no such heading may exist — a changelog cannot record a run the pages
    // still call unexecuted.
    const changelog = findDocPage('changelog')!.body;
    const recorded = docHeadingsOf(changelog).some((heading) =>
      /examples executed/i.test(heading.text),
    );
    expect(recorded).toBe(EXAMPLES_EXECUTED);
  });

  it('gives the notice a tint and a border that really compile, in both states', async () => {
    // jsdom loads no CSS, which is how `bg-warn/10` — a class Tailwind emits
    // NO rule for, because `warn` is a bare var() in the config — shipped on
    // all 22 pages as an unstyled paragraph. So the rendered className is
    // compiled here with the real tailwind.config.ts, and every class on the
    // notice must produce CSS, including a background, a border colour and a
    // left bar that each name a theme token.
    for (const executed of [false, true]) {
      cleanup();
      const page = { ...findDocPage('errors')!, examples: { executed, note: 'n' } };
      render(<DocsArticle page={page} />);
      const notice = screen.getByTestId('docs-example-status');
      const classes = notice.className.split(/\s+/).filter(Boolean);
      const css = await compileTailwind(classes);

      for (const cls of classes) {
        expect(css.has(cls), `"${cls}" must compile to a rule (executed=${executed})`).toBe(true);
      }
      const declarations = classes.map((cls) => css.get(cls) ?? '').join('\n');
      expect(declarations).toMatch(/background-color:[^;]*var\(--ts-(?:warn|ok)/);
      expect(declarations).toMatch(/(?:^|\s)border-color:[^;]*var\(--ts-(?:warn|ok)/);
      expect(declarations).toMatch(/border-left-color:[^;]*var\(--ts-(?:warn|ok)/);
      expect(declarations).toContain('border-left-width: 4px');
      // The words stay full-contrast ink in both themes; the colour is the
      // bar, the tint and the icon.
      expect(classes).toContain('text-ink');
    }
  });
});

/** Answer the docs shell's console-access question, and nothing else. */
function serveConsoleAccess(body: { allowed: boolean }) {
  const fetchMock = vi.fn(async (_url: string) => ({ ok: true, status: 200, json: async () => body }));
  vi.stubGlobal('fetch', fetchMock);
  return fetchMock;
}

describe('the shell', () => {
  it('offers a skip link, a menu toggle and the API status page', () => {
    render(
      <DocsShell>
        <p>page body</p>
      </DocsShell>,
    );

    const skip = screen.getByRole('link', { name: 'Skip to content' });
    expect(skip.getAttribute('href')).toBe('#docs-content');
    expect(document.querySelector('#docs-content')).toBeTruthy();

    const toggle = screen.getByRole('button', { name: 'Menu' });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    expect(toggle.getAttribute('aria-controls')).toBe('docs-nav-panel');
    expect(document.querySelector('#docs-nav-panel')).toBeTruthy();

    // The link exists twice on purpose — in the header and in the sidebar —
    // so this names the header one rather than asserting there is only one.
    const header = screen.getByRole('banner');
    expect(within(header).getByRole('link', { name: 'API status' }).getAttribute('href')).toBe(
      '/docs/status',
    );
  });

  it('opens the drawer from the keyboard and lets Escape out of it', () => {
    render(
      <DocsShell>
        <p>page body</p>
      </DocsShell>,
    );

    const toggle = screen.getByRole('button', { name: 'Menu' });
    toggle.focus();
    fireEvent.click(toggle);

    const opened = screen.getByRole('button', { name: 'Close' });
    expect(opened.getAttribute('aria-expanded')).toBe('true');

    // A drawer you can open with the keyboard and not close with it is a
    // trap; focus going back to the toggle is what stops a reader having to
    // start again at the top of the document.
    fireEvent.keyDown(document, { key: 'Escape' });
    expect(screen.getByRole('button', { name: 'Menu' }).getAttribute('aria-expanded')).toBe(
      'false',
    );
    expect(document.activeElement).toBe(screen.getByRole('button', { name: 'Menu' }));
  });

  it('keeps the console and chat links reachable when the header drops them', () => {
    // At 400px the header holds the brand, the menu button and the status
    // link and nothing else. The other two move into the drawer rather than
    // disappearing — `sm:hidden` here against `hidden sm:block` there, so
    // exactly one copy is ever visible.
    const { container } = render(
      <DocsShell>
        <p>page body</p>
      </DocsShell>,
    );
    const drawerExtras = container.querySelector('#docs-nav-panel .sm\\:hidden');
    expect(drawerExtras).toBeTruthy();
    expect(within(drawerExtras as HTMLElement).getByRole('link', { name: 'Console' })).toBeTruthy();
    expect(
      within(drawerExtras as HTMLElement).getByRole('link', { name: 'Back to chat' }),
    ).toBeTruthy();

    const header = screen.getByRole('banner');
    expect(within(header).getByRole('link', { name: 'Console' }).className).toContain(
      'sm:block',
    );
  });

  it('renders the table of contents exactly once, whatever the viewport', () => {
    // jsdom loads no CSS, so a desktop column PLUS a mobile drawer would both
    // be in the document here — duplicating every section heading id and
    // sending every aria-labelledby to whichever came first (M-12, the chat
    // sidebar). One node, moved by CSS, is why this passes.
    render(
      <DocsShell>
        <p>page body</p>
      </DocsShell>,
    );
    expect(screen.getAllByRole('navigation', { name: 'Documentation' })).toHaveLength(1);
    expect(document.querySelectorAll('#docs-nav-getting-started')).toHaveLength(1);
  });
});

describe('the design system', () => {
  // Every file under the two docs trees, found rather than listed (wave-3
  // re-verify): a hand-kept list let a new component ship unchecked.
  const SOURCES = ['components/docs', 'app/docs'].flatMap((root) =>
    (readdirSync(join(FRONTEND_DIR, root), { recursive: true }) as string[])
      .filter((file) => /\.(tsx?|css)$/.test(file))
      .map((file) => `${root}/${file.split('\\').join('/')}`),
  );

  it('checks every source file in the docs trees, including the ones added later', () => {
    expect(SOURCES).toContain('components/docs/DocsMarkdown.tsx');
    expect(SOURCES).toContain('components/docs/docHeadings.ts');
    expect(SOURCES).toContain('app/docs/[slug]/page.tsx');
  });

  it('hard-codes no colour, so both themes and the brand tokens hold', () => {
    // Every colour comes from the Tailwind semantic names, which resolve to
    // the --ts-* variables app/globals.css redefines under html.light. A hex
    // literal is one way to break that; a Tailwind PALETTE class
    // (`text-slate-400`, `hover:bg-zinc-800`, `text-white`) is the far more
    // common one in a Tailwind file, and the hex check alone let both through
    // (verifier finding, 2026-09-13).
    //
    // ONE allowlisted literal: the drawer scrim, `bg-black/50`, in DocsShell.
    // A scrim dims whatever is behind it in both themes — every dialog in the
    // product does the same — and a theme token would turn it white on paper.
    const palette =
      /\b(?:bg|text|border|ring|outline|fill|stroke|from|via|to|divide|placeholder|decoration|shadow|accent|caret)-(?:slate|gray|zinc|neutral|stone|red|orange|amber|yellow|lime|green|emerald|teal|cyan|sky|blue|indigo|violet|purple|fuchsia|pink|rose|white|black)\b(?:-\d{2,3})?(?:\/\d+)?/g;
    const allowed = new Map([['components/docs/DocsShell.tsx', ['bg-black/50']]]);

    for (const relative of SOURCES) {
      const source = readFileSync(join(FRONTEND_DIR, relative), 'utf8');
      expect(source, `${relative} must not hard-code a hex colour`).not.toMatch(
        /#[0-9a-fA-F]{3,8}\b/,
      );
      const found = [...source.matchAll(palette)].map((m) => m[0]);
      const permitted = allowed.get(relative) ?? [];
      expect(
        found.filter((cls) => !permitted.includes(cls)),
        `${relative} uses a palette colour`,
      ).toEqual([]);
    }
  });

  it('uses no opacity modifier that Tailwind silently compiles to nothing', async () => {
    // `bg-bg/95`, `bg-surface-2/60`, `bg-warn/10`: on a colour the config
    // declares as a bare var(), the `/NN` form emits no rule at all, and the
    // element renders as though the class were not there. Every such class in
    // these files is compiled with the real config and must produce CSS.
    const modified = new Set<string>();
    for (const relative of SOURCES) {
      const source = readFileSync(join(FRONTEND_DIR, relative), 'utf8');
      for (const m of source.matchAll(/(?<![\w/-])((?:[a-z-]+:)*(?:bg|text|border(?:-[trblxy])?|ring|divide|from|via|to)-[a-z][a-z0-9-]*\/\d{1,3})(?![\w/])/g)) {
        modified.add(m[1]);
      }
    }
    expect(modified.size).toBeGreaterThan(0);
    const css = await compileTailwind([...modified]);
    const dead = [...modified].filter((cls) => !css.has(cls));
    expect(dead).toEqual([]);
  });

  it('names no other vendor (CONTRACT §17: TechSara branding only)', () => {
    // Compatibility is described in plain words; no other vendor's product
    // name, logo or prose appears on the site. "OpenAI-shaped" is the one
    // permitted mention, and only as the name of a request shape a reader
    // arrives with — it names what they have, it does not borrow anything.
    const banned = /\b(anthropic|claude|gemini|mistral|cohere|azure openai|chatgpt)\b/i;
    for (const page of DOC_PAGES) {
      expect(page.body, `${page.slug} mentions another vendor`).not.toMatch(banned);
    }
  });
});
