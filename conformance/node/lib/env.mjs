// Configuration for the conformance suite, read from the environment only.
//
// WHY no defaults for the base URL or keys (2026-09-13): the suite is pointed at
// staging, the isolated e2e stack and production by the same release manager; a
// default URL is how a run meant for staging quietly spends production engine
// time. Without TECHSARA_BASE_URL every live test is SKIPPED with that reason,
// and the offline tests still run.
import { readFileSync, existsSync } from 'node:fs';

function loadEnvFile(path) {
  // KEY=value lines, no quoting rules beyond stripping one pair of quotes. The
  // file is the output of scripts/provision-key.mjs or a CI secret mount.
  if (!path || !existsSync(path)) return;
  for (const line of readFileSync(path, 'utf8').split(/\r?\n/)) {
    const m = /^\s*([A-Z0-9_]+)\s*=\s*(.*)\s*$/.exec(line);
    if (!m || line.trimStart().startsWith('#')) continue;
    const value = m[2].replace(/^(['"])(.*)\1$/, '$2');
    if (process.env[m[1]] === undefined) process.env[m[1]] = value;
  }
}

loadEnvFile(process.env.TECHSARA_ENV_FILE);

function int(name, fallback) {
  const raw = process.env[name];
  if (raw === undefined || raw === '') return fallback;
  const n = Number(raw);
  if (!Number.isFinite(n)) throw new Error(`${name} must be a number, got ${JSON.stringify(raw)}`);
  return n;
}

function list(name) {
  const raw = process.env[name];
  if (raw === undefined) return null;
  return new Set(
    raw
      .split(/[\s,]+/)
      .map((v) => v.trim())
      .filter(Boolean),
  );
}

export const env = {
  /** e.g. https://api.example.test/v1 — MUST include the /v1 suffix, as the SDK expects. */
  baseURL: (process.env.TECHSARA_BASE_URL || '').replace(/\/$/, ''),
  apiKey: process.env.TECHSARA_API_KEY || '',
  /** A key holding only `models.read`, for the 403 insufficient_scope test. */
  narrowKey: process.env.TECHSARA_API_KEY_NARROW || '',
  /** The chat model every generating test uses. */
  chatModel: process.env.TECHSARA_CHAT_MODEL || 'techsara-35b',
  /**
   * Minimum gap between two requests to the API, across the whole run.
   * WHY 1100 ms by default (2026-09-13): stacks built before the "no usage
   * limits" decision still enforce 60 requests/minute per project; 1.1 s keeps a
   * serial run under it. Set 0 against a stack without limits.
   */
  minIntervalMs: int('CONFORMANCE_MIN_INTERVAL_MS', 1100),
  /**
   * Opt-in: seconds a single synchronous request must stay open in the long
   * no-timeout test. 0 (default) skips it, because the only way to hold a
   * request open that long is to make an engine generate for that long.
   */
  longRequestSeconds: int('CONFORMANCE_LONG_REQUEST_SECONDS', 0),
  /**
   * Opt-in: run the planned 1,000,000-token request.
   * WHY opt-in (2026-09-13, review): a techsara-35b request whose input plus
   * planned output exceeds 131,072 tokens takes the `main.long` capacity gate
   * (CONTRACT-3 §11, §12.3), which admits ONE public request fleet-wide. On a
   * stack that also serves customers the test either steals that slot from a
   * customer's long job or gets a false 503 while a customer holds it.
   */
  allowLongGate: process.env.CONFORMANCE_ALLOW_LONG_GATE === '1',
  /**
   * The deployment's PUBLIC_API_MAX_OUTPUT_TOKENS (owner decision: 1,000,000).
   * techsara-35b advertises min(this, its context window) — CONTRACT-3 §8.3 —
   * so the suite reads the window from GET /v1/models instead of assuming 1M.
   */
  outputCeiling: int('CONFORMANCE_OUTPUT_CEILING', 1_000_000),
  /**
   * The scopes TECHSARA_API_KEY holds, as scripts/provision-key.mjs recorded
   * them, or null when unknown (a hand-made key or a CI secret without it).
   * WHY (2026-09-13, review): stored keys keep their stored scopes (CONTRACT-3
   * §7), so a key minted before a scope existed 403s on that feature's route
   * forever; the suite must say "re-provision", not report a contract defect.
   */
  keyScopes: list('TECHSARA_API_KEY_SCOPES'),
  /** Scopes the stack's console refused as unknown at provisioning time. */
  stackUnknownScopes: list('TECHSARA_STACK_UNKNOWN_SCOPES') ?? new Set(),
  /** Comma-separated feature ids to treat as BUILT (run as normal tests). */
  builtFeatures: new Set(
    (process.env.CONFORMANCE_BUILT_FEATURES || '')
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean),
  ),
};

export const liveSkipReason = !env.baseURL
  ? 'TECHSARA_BASE_URL is not set'
  : !env.apiKey
    ? 'TECHSARA_API_KEY is not set'
    : undefined;
