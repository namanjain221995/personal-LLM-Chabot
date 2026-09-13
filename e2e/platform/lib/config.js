'use strict';
/**
 * Everything a run is pointed at, resolved from flags first and environment
 * second. No secret has a default: passwords come from an environment
 * variable or from a file whose PATH is in the environment, so nothing
 * credential-shaped is ever committed next to this code.
 */

const fs = require('fs');
const net = require('net');
const path = require('path');

const DEFAULT_WIDTHS = [360, 768, 1440];

function parseArgs(argv) {
  const out = { _: [] };
  for (let i = 0; i < argv.length; i += 1) {
    const arg = argv[i];
    if (!arg.startsWith('--')) {
      out._.push(arg);
      continue;
    }
    const eq = arg.indexOf('=');
    if (eq !== -1) {
      out[arg.slice(2, eq)] = arg.slice(eq + 1);
    } else {
      const next = argv[i + 1];
      if (next !== undefined && !next.startsWith('--')) {
        out[arg.slice(2)] = next;
        i += 1;
      } else {
        out[arg.slice(2)] = true;
      }
    }
  }
  return out;
}

/**
 * The ports of the platform host's PRODUCTION frontend and orchestrator.
 *
 * WHY (2026-09-13): 3000 and 8080 are the production frontend and
 * orchestrator ports, and on a host that runs production a loopback URL on
 * them IS production. "Loopback" therefore does not mean "a test stack": a stray
 * `--base http://127.0.0.1:3000` would have created conversations, a public
 * share link, a 92 MiB upload and an API project in production. These ports
 * are refused on every host, with no override — a candidate can use any
 * other port.
 */
const PRODUCTION_PORTS = new Set([3000, 8080]);

/** Loopback ports a test stack uses: the e2e stack, the audit stacks, candidates. */
const DEFAULT_ALLOWED_PORTS = '3001,3002,3900-3999';

function parsePortList(spec) {
  const ports = new Set();
  for (const part of splitList(spec)) {
    const m = /^(\d+)(?:-(\d+))?$/.exec(part);
    if (!m) throw new Error(`E2E_ALLOWED_PORTS: "${part}" is not a port or a range`);
    const lo = Number(m[1]);
    const hi = Number(m[2] || m[1]);
    for (let p = lo; p <= hi; p += 1) ports.add(p);
  }
  return ports;
}

/**
 * True only for a real loopback address: 127.0.0.0/8, ::1 (also written as
 * an IPv4-mapped 127.x), or the name `localhost`.
 *
 * WHY net.isIP (2026-09-13): a hostname-prefix test accepted
 * `127.attacker.example` and `127.0.0.1.nip.io`, which are DNS names that can
 * point anywhere.
 */
function isLoopbackUrl(raw) {
  let url;
  try {
    url = new URL(raw);
  } catch {
    return false;
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return false;
  const host = url.hostname.replace(/^\[|\]$/g, '').toLowerCase();
  if (host === 'localhost') return true;
  const kind = net.isIP(host);
  if (kind === 4) return host.split('.')[0] === '127';
  if (kind === 6) {
    if (host === '::1') return true;
    // WHATWG URL normalises [::ffff:127.0.0.1] to [::ffff:7f00:1].
    const mapped = /^::ffff:([0-9a-f]{1,4}):[0-9a-f]{1,4}$/.exec(host);
    return Boolean(mapped) && parseInt(mapped[1], 16) >> 8 === 127;
  }
  return false;
}

function effectivePort(url) {
  if (url.port) return Number(url.port);
  return url.protocol === 'https:' ? 443 : 80;
}

/**
 * May this run write to `base`? Returns null when it may, or the reason it
 * may not.
 *
 * WHY (2026-09-13): this suite WRITES — conversations, share links, API
 * projects and keys, a 92 MiB upload, and one failed sign-in that counts
 * toward a lockout. Three rules, in order:
 *   1. never a production port (3000, 8080), whatever the host and flags;
 *   2. a loopback base must use a test-stack port (E2E_ALLOWED_PORTS,
 *      default 3001, 3002, 3900-3999);
 *   3. anything that is not loopback needs E2E_ALLOW_REMOTE=1 — the operator
 *      saying that deployment is theirs to write to.
 */
function baseRefusal(base, { allowRemote = false, allowedPorts = DEFAULT_ALLOWED_PORTS } = {}) {
  let url;
  try {
    url = new URL(base);
  } catch {
    return `${base} is not a URL`;
  }
  if (url.protocol !== 'http:' && url.protocol !== 'https:') return `${base} is not an http(s) URL`;
  const port = effectivePort(url);
  if (PRODUCTION_PORTS.has(port)) {
    return `port ${port} is a production port on the platform host (frontend 3000, orchestrator 8080); run a candidate on another port`;
  }
  if (isLoopbackUrl(base)) {
    const allowed = parsePortList(allowedPorts);
    if (!allowed.has(port)) {
      return `loopback port ${port} is not a known test-stack port (E2E_ALLOWED_PORTS=${allowedPorts}); add it there if it really is a candidate or e2e stack`;
    }
    return null;
  }
  if (!allowRemote) {
    return `${url.host} is not a loopback address; set E2E_ALLOW_REMOTE=1 only if that deployment is yours to write to`;
  }
  return null;
}

function readSecret(envName, fileEnvName, env) {
  if (env[envName]) return env[envName];
  const file = env[fileEnvName];
  if (!file) return null;
  try {
    return fs.readFileSync(file, 'utf8').trim() || null;
  } catch (err) {
    throw new Error(`${fileEnvName} points at a file that cannot be read (${err.code || err.message})`);
  }
}

function splitList(value) {
  if (value === undefined || value === true || value === '') return [];
  return String(value)
    .split(',')
    .map((s) => s.trim())
    .filter(Boolean);
}

function resolveConfig(argv = process.argv.slice(2), env = process.env) {
  const args = parseArgs(argv);
  const base = String(args.base || env.E2E_BASE_URL || 'http://127.0.0.1:3001').replace(/\/+$/, '');
  const widths = splitList(args.widths || env.E2E_WIDTHS).map(Number).filter((n) => n > 0);
  const chatMode = String(args['chat-mode'] || env.E2E_CHAT_MODE || 'stub');
  if (!['stub', 'live'].includes(chatMode)) {
    throw new Error(`--chat-mode must be stub or live, not ${chatMode}`);
  }
  const stamp = new Date().toISOString().replace(/[:.]/g, '-');
  const outDir = path.resolve(String(args.out || env.E2E_OUT || path.join(__dirname, '..', 'out', stamp)));
  return {
    base,
    allowRemote: env.E2E_ALLOW_REMOTE === '1' || args['allow-remote'] === true,
    allowedPorts: String(env.E2E_ALLOWED_PORTS || DEFAULT_ALLOWED_PORTS),
    widths: widths.length ? widths : DEFAULT_WIDTHS,
    chatMode,
    only: splitList(args.only || env.E2E_ONLY),
    skip: splitList(args.skip || env.E2E_SKIP),
    list: args.list === true,
    headful: args.headful === true || env.E2E_HEADFUL === '1',
    chrome: String(args.chrome || env.CHROME || '/usr/bin/google-chrome'),
    outDir,
    label: String(args.label || env.E2E_LABEL || ''),
    admin: {
      email: env.E2E_ADMIN_EMAIL || 'e2e-devadmin@test.local',
      password: () => readSecret('E2E_ADMIN_PASSWORD', 'E2E_ADMIN_PASSWORD_FILE', env),
    },
    member: {
      email: env.E2E_MEMBER_EMAIL || 'e2e-artifacts@test.local',
      password: () => readSecret('E2E_MEMBER_PASSWORD', 'E2E_MEMBER_PASSWORD_FILE', env),
    },
    // The six public model ids the owner decided on (2026-09-13). A candidate
    // that publishes a different set fails `v1.models-published` by name.
    expectedModels: splitList(env.E2E_EXPECTED_MODELS).length
      ? splitList(env.E2E_EXPECTED_MODELS)
      : ['techsara-35b', 'techsara-8b-vision', 'techsara-ocr', 'techsara-embed', 'techsara-rerank', 'techsara-whisper'],
  };
}

module.exports = { resolveConfig, parseArgs, isLoopbackUrl, baseRefusal, parsePortList, splitList, DEFAULT_WIDTHS, PRODUCTION_PORTS };
