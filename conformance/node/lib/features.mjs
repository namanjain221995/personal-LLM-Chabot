// Planned features, and the strict expected-failure wrapper that tests them.
//
// WHY strict XFAIL rather than skip (2026-09-13): a skipped test for an endpoint
// that is "not built yet" says nothing on the day it ships, and a test that
// passes against a 404 because its assertions were too loose is worse. Every
// planned feature's tests RUN on every stack. While the feature is listed as
// planned here, a failing body is reported XFAIL (expected, with the reason),
// and a PASSING body is a hard FAIL ("XPASS(strict)") — the signal to move the
// feature to `built` and let its tests guard it from then on.
//
// A release manager can promote a feature for one run without editing this file:
//   CONFORMANCE_BUILT_FEATURES=embeddings,rerank npm test
import { it } from 'node:test';
import { env } from './env.mjs';
import { FEATURES } from './feature-list.mjs';

export { FEATURES };

/**
 * Thrown by an `itPlanned` body to end as SKIP with a reason (a plain `it`
 * calls `t.skip()` and returns instead). WHY a class (2026-09-13): inside
 * `itPlanned` a body that returns normally is XPASS(strict), so a skip must be
 * told apart from "the feature works".
 */
export class SkipTest extends Error {
  constructor(reason) {
    super(reason);
    this.name = 'SkipTest';
  }
}

/**
 * Why this run's key cannot exercise `featureId`, or undefined.
 * A scope the stack refused as UNKNOWN when the key was provisioned means the
 * feature is not on that stack yet: the test runs and XFAILs with the real
 * reason. A scope the stack knows but the key lacks is a stale key: SKIP.
 */
export function missingScopeReason(featureId) {
  const f = FEATURES[featureId];
  if (!f?.scopes || !env.keyScopes) return undefined;
  const missing = f.scopes.filter((s) => !env.keyScopes.has(s) && !env.stackUnknownScopes.has(s));
  if (missing.length === 0) return undefined;
  return (
    `the API key lacks ${missing.join(', ')} (it holds ${[...env.keyScopes].join(' ') || 'nothing'}); ` +
    'stored keys keep their stored scopes (CONTRACT-3 §7) — re-provision with scripts/provision-key.mjs'
  );
}

function insufficientScope(err) {
  return err && err.status === 403 && (err.code === 'insufficient_scope' || err.error?.code === 'insufficient_scope');
}

export function isBuilt(id) {
  const f = FEATURES[id];
  if (!f) throw new Error(`unknown feature id ${JSON.stringify(id)} — add it to lib/features.mjs`);
  return f.status === 'built' || env.builtFeatures.has(id);
}

function summarise(err) {
  const msg = String((err && err.message) || err).split('\n')[0];
  const status =
    err && typeof err.status === 'number' && !msg.startsWith(String(err.status)) ? `HTTP ${err.status} ` : '';
  const code = err && err.code && typeof err.code === 'string' ? `${err.code}: ` : '';
  return `${status}${code}${msg}`.slice(0, 240);
}

/**
 * `it` for a planned feature. `options.skip` is honoured as usual (for tests
 * that also need opt-in configuration).
 */
export function itPlanned(featureId, name, options, fn) {
  if (typeof options === 'function') {
    fn = options;
    options = {};
  }
  const built = isBuilt(featureId);
  return it(`${name} [feature: ${featureId}]`, options, async (t) => {
    const scopeReason = missingScopeReason(featureId);
    if (scopeReason) {
      t.skip(scopeReason);
      return;
    }
    try {
      await fn(t);
    } catch (err) {
      if (err instanceof SkipTest) {
        t.skip(err.message);
        return;
      }
      if (insufficientScope(err) && !env.keyScopes) {
        // The key's scopes were not recorded, so a 403 here cannot be told
        // apart from a key minted before the scope existed.
        t.skip(`HTTP 403 insufficient_scope with a key whose scopes are unknown; re-provision with scripts/provision-key.mjs (${summarise(err)})`);
        return;
      }
      if (built) throw err;
      // The reporter reads this marker; keep the prefix stable.
      t.todo(`XFAIL feature=${featureId}: ${summarise(err)}`);
      return;
    }
    if (!built) {
      throw new Error(
        `XPASS(strict): feature "${featureId}" is marked planned but this test passed. ` +
          'Set its status to "built" in lib/features.mjs so the test guards it.',
      );
    }
  });
}
