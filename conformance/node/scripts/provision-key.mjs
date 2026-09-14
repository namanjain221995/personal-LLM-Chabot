#!/usr/bin/env node
// Provision a conformance project and two keys through the developer console API.
//
// WHY a script and not "click it in the console" (2026-09-13): a release manager
// runs this suite against a fresh stack, and the suite needs TWO keys — a default
// key and a key holding only `models.read` — so the 403 insufficient_scope test
// has a real narrow credential to use. Clicking that twice per run is where a
// wrong scope gets ticked.
//
// Usage:
//   node scripts/provision-key.mjs \
//     --console http://localhost:8080 \
//     --email admin@example.test --password-file /path/to/pw \
//     --out /path/outside/every/git/work/tree/conformance.env
//
// It prints the project id, key ids, scopes and last-four only. The secrets are
// written to --out (mode 0600) as TECHSARA_API_KEY and TECHSARA_API_KEY_NARROW,
// with the default key's scopes as TECHSARA_API_KEY_SCOPES, and never to
// stdout: the secret is shown ONCE by the console API (CONTRACT-3 §5) and a CI log
// is not a place to keep it.
import { readFileSync, writeFileSync, chmodSync, existsSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { BASE_SCOPES, SUITE_SCOPES } from '../lib/feature-list.mjs';

function arg(name, fallback) {
  const i = process.argv.indexOf(`--${name}`);
  if (i === -1) return fallback;
  return process.argv[i + 1];
}

const consoleBase = (arg('console') || process.env.TECHSARA_CONSOLE_URL || '').replace(/\/$/, '');
const email = arg('email') || process.env.TECHSARA_CONSOLE_EMAIL;
const passwordFile = arg('password-file') || process.env.TECHSARA_CONSOLE_PASSWORD_FILE;
const out = arg('out');
const projectName = arg('project', `conformance-node-${new Date().toISOString().replace(/[:.]/g, '-')}`);

if (!consoleBase || !email || !passwordFile || !out) {
  console.error('usage: provision-key.mjs --console URL --email EMAIL --password-file FILE --out FILE [--project NAME]');
  process.exit(2);
}
/**
 * The git work tree `path` would land in, or undefined.
 * WHY any work tree and not "the conformance directory" (2026-09-13, review):
 * the first guard refused only paths containing `/conformance/`, so
 * `--out <repo>/conformance.env` passed it — and the root .gitignore covers
 * `.env` and `.env.*`, not `*.env`. The output holds live secrets and this
 * repository is public: no path inside ANY work tree is accepted. Walking up
 * for `.git` (a directory, or a file in a linked worktree) needs no git binary.
 */
function enclosingWorkTree(path) {
  let dir = dirname(resolve(path));
  for (;;) {
    if (existsSync(resolve(dir, '.git'))) return dir;
    const parent = dirname(dir);
    if (parent === dir) return undefined;
    dir = parent;
  }
}
const workTree = enclosingWorkTree(out);
if (workTree) {
  console.error(`refusing to write secrets inside the git work tree ${workTree}; choose a path outside every repository, e.g. $HOME/.config/techsara/conformance.env`);
  process.exit(2);
}

const password = readFileSync(passwordFile, 'utf8').replace(/\r?\n$/, '');

async function call(method, path, body, cookie) {
  const headers = { 'content-type': 'application/json' };
  if (cookie) headers.cookie = cookie;
  let res;
  try {
    res = await fetch(`${consoleBase}${path}`, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
    });
  } catch (err) {
    // One line, not undici's stack: the cause names what actually failed.
    const cause = err?.cause?.code || err?.cause?.message || err?.message || String(err);
    console.error(`cannot reach ${consoleBase}${path}: ${cause}`);
    process.exit(1);
  }
  const text = await res.text();
  let json;
  try {
    json = JSON.parse(text);
  } catch {
    json = { raw: text.slice(0, 300) };
  }
  return { res, json };
}

const login = await call('POST', '/auth/login', { email, password, remember: false });
if (!login.res.ok) {
  console.error(`login failed: HTTP ${login.res.status} ${JSON.stringify(login.json).slice(0, 300)}`);
  process.exit(1);
}
// WHY by hand (2026-09-13): the session cookie is `Secure`, so a cookie jar that
// honours the flag never sends it back to an http:// test stack. The value is
// forwarded verbatim and never printed.
const setCookies = login.res.headers.getSetCookie();
const cookie = setCookies.map((c) => c.split(';')[0]).join('; ');
if (!cookie) {
  console.error('login returned no session cookie');
  process.exit(1);
}

const project = await call('POST', '/admin/api/developers/projects', { name: projectName, environment: 'test' }, cookie);
if (!project.res.ok) {
  console.error(`project creation failed: HTTP ${project.res.status} ${JSON.stringify(project.json).slice(0, 300)}`);
  process.exit(1);
}
const projectId = project.json.project.id;

/**
 * Create a key holding `wanted`, dropping only scopes the stack's console
 * refuses as UNKNOWN (a stack that predates a feature has no word for its
 * scope; that feature's tests then XFAIL on the stack for the real reason).
 * WHY explicit scopes (2026-09-13, review): a key created with no `scopes`
 * gets whatever DEFAULT_SCOPES the stack had that day and keeps them forever
 * (CONTRACT-3 §7), so a later feature's tests would 403 on a reused key.
 */
async function createKey(name, wanted, { required }) {
  let scopes = [...wanted];
  const unknown = [];
  for (let attempt = 0; attempt <= wanted.length; attempt++) {
    const r = await call('POST', `/admin/api/developers/projects/${projectId}/keys`, { name, scopes }, cookie);
    if (r.res.ok) {
      const granted = new Set(r.json.key?.scopes ?? []);
      const notGranted = scopes.filter((sc) => !granted.has(sc));
      if (notGranted.length) {
        console.error(`key ${name} was created without ${notGranted.join(', ')} (granted: ${[...granted].join(' ')}); refusing to write a key the suite cannot use`);
        process.exit(1);
      }
      return { ...r.json, unknown };
    }
    const text = JSON.stringify(r.json);
    const m = r.res.status === 422 ? /'([a-z_]+\.[a-z_]+)' is not a valid scope/.exec(text) : null;
    if (!m || !scopes.includes(m[1]) || required.includes(m[1])) {
      console.error(`key ${name} creation failed: HTTP ${r.res.status} ${text.slice(0, 300)}`);
      process.exit(1);
    }
    unknown.push(m[1]);
    scopes = scopes.filter((sc) => sc !== m[1]);
  }
  console.error(`key ${name} creation did not converge`);
  process.exit(1);
}

const full = await createKey('conformance-default', SUITE_SCOPES, { required: BASE_SCOPES });
const narrow = await createKey('conformance-models-read-only', ['models.read'], { required: ['models.read'] });
if (narrow.key.scopes.length !== 1) {
  console.error(`the narrow key holds ${narrow.key.scopes.join(' ')}, not only models.read`);
  process.exit(1);
}

writeFileSync(
  out,
  [
    `TECHSARA_API_KEY=${full.secret}`,
    `TECHSARA_API_KEY_SCOPES=${full.key.scopes.join(' ')}`,
    `TECHSARA_STACK_UNKNOWN_SCOPES=${full.unknown.join(' ')}`,
    `TECHSARA_API_KEY_NARROW=${narrow.secret}`,
    `TECHSARA_PROJECT_ID=${projectId}`,
    '',
  ].join('\n'),
  { mode: 0o600 },
);
chmodSync(out, 0o600);

console.log(
  JSON.stringify(
    {
      project_id: projectId,
      project_name: projectName,
      default_key: { id: full.key.id, scopes: full.key.scopes, last_four: full.key.last_four },
      scopes_unknown_to_this_stack: full.unknown,
      narrow_key: { id: narrow.key.id, scopes: narrow.key.scopes, last_four: narrow.key.last_four },
      written_to: out,
    },
    null,
    2,
  ),
);
