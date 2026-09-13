'use strict';
/**
 * The runner: an ordered list of named checks, each one PASS, FAIL or SKIP,
 * with the failure kept VERBATIM (message and the top of the stack) and a
 * screenshot of every page the check still had open when it failed.
 *
 * Checks run strictly in order and one at a time. Several of them build on an
 * earlier one (the rename needs the conversation the send created), and a
 * serial run against a shared e2e stack is also the polite one.
 */

const fs = require('fs');
const path = require('path');

class SkipError extends Error {
  constructor(reason) {
    super(reason);
    this.name = 'SkipError';
  }
}

class Registry {
  constructor() {
    this.tests = [];
  }

  /**
   * @param {string} suite
   * @param {string} id        stable, dotted: `chat.rename`
   * @param {string} title     a sentence saying what must be true
   * @param {(ctx: object) => Promise<void>} fn
   * @param {{timeoutMs?: number, needs?: string[]}} [opts]
   */
  add(suite, id, title, fn, opts = {}) {
    if (this.tests.some((t) => t.id === id)) throw new Error(`duplicate test id ${id}`);
    this.tests.push({ suite, id, title, fn, timeoutMs: opts.timeoutMs ?? 180_000, needs: opts.needs ?? [] });
  }

  /** Suite names and id prefixes both select: `--only chat,gating.docs`. */
  select(only, skip) {
    const matches = (t, sel) => sel.some((s) => t.suite === s || t.id === s || t.id.startsWith(`${s}.`) || t.id.startsWith(s));
    return this.tests.filter((t) => (only.length ? matches(t, only) : true) && !(skip.length && matches(t, skip)));
  }
}

function withTimeout(promise, ms, id) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${id} did not finish within ${Math.round(ms / 1000)} s`)), ms);
  });
  return Promise.race([promise, timeout]).finally(() => clearTimeout(timer));
}

/** The message plus the first frames of the stack that are OURS, not node's. */
function describeError(err) {
  if (!err) return 'unknown error';
  const message = err.message || String(err);
  const frames = String(err.stack || '')
    .split('\n')
    // A real frame ends in file:line:col; a message line that merely starts
    // with "at 360px:" must not be mistaken for one.
    .filter((l) => /^\s+at .+:\d+:\d+\)?$/.test(l))
    .filter((l) => !l.includes('node:internal') && !l.includes('node_modules'))
    .slice(0, 4)
    .map((l) => l.trim());
  return frames.length ? `${message}\n    ${frames.join('\n    ')}` : message;
}

async function runAll(tests, makeContext, { outDir, log = console.log } = {}) {
  const screensDir = path.join(outDir, 'screenshots');
  fs.mkdirSync(screensDir, { recursive: true });
  const results = [];
  const passed = new Set();
  for (const test of tests) {
    const started = Date.now();
    const result = {
      id: test.id,
      suite: test.suite,
      title: test.title,
      status: 'pass',
      ms: 0,
      detail: '',
      error: null,
      notes: [],
      screenshots: [],
      consoleErrors: [],
    };
    const missing = test.needs.filter((dep) => !passed.has(dep));
    if (missing.length) {
      result.status = 'skip';
      result.detail = `depends on ${missing.join(', ')}, which did not pass in this run`;
      results.push(result);
      log(`SKIP ${test.id} — ${result.detail}`);
      continue;
    }
    const ctx = makeContext(test, result);
    try {
      await withTimeout(Promise.resolve().then(() => test.fn(ctx)), test.timeoutMs, test.id);
      passed.add(test.id);
    } catch (err) {
      if (err instanceof SkipError) {
        result.status = 'skip';
        result.detail = err.message;
      } else {
        result.status = 'fail';
        result.error = describeError(err);
        result.detail = (err && err.message ? err.message : String(err)).split('\n')[0];
        await ctx._screenshotOpenPages(screensDir).catch((e) => result.notes.push(`screenshot failed: ${e.message}`));
      }
    } finally {
      await ctx._dispose().catch(() => {});
      result.ms = Date.now() - started;
    }
    if (!result.detail && result.notes.length) result.detail = result.notes[result.notes.length - 1];
    results.push(result);
    log(`${result.status.toUpperCase().padEnd(4)} ${test.id} (${(result.ms / 1000).toFixed(1)} s)${result.detail ? ` — ${result.detail}` : ''}`);
    if (result.status === 'fail') log(`     ${result.error.replace(/\n/g, '\n     ')}`);
  }
  return results;
}

function escapeCell(text) {
  return String(text ?? '')
    .replace(/\|/g, '\\|')
    .replace(/\r?\n/g, ' ');
}

function renderMarkdown(results, meta) {
  const count = (s) => results.filter((r) => r.status === s).length;
  const lines = [];
  lines.push(`# Platform regression run${meta.label ? ` — ${meta.label}` : ''}`);
  lines.push('');
  lines.push(`- Base URL: \`${meta.base}\``);
  lines.push(`- Started: ${meta.startedAt}; finished: ${meta.finishedAt}`);
  lines.push(`- Chat mode: \`${meta.chatMode}\`; widths: ${meta.widths.join(', ')}`);
  lines.push(`- Browser: ${meta.browserVersion || 'not launched'}`);
  if (meta.target && (meta.target.frontendImage || meta.target.orchestratorImage || meta.target.git)) {
    lines.push(
      `- Target: frontend image \`${meta.target.frontendImage || 'not stated'}\`; orchestrator image \`${meta.target.orchestratorImage || 'not stated'}\`; git \`${meta.target.git || 'not stated'}\``,
    );
  } else {
    lines.push('- Target: not stated (set E2E_TARGET_FRONTEND_IMAGE, E2E_TARGET_ORCHESTRATOR_IMAGE, E2E_TARGET_GIT)');
  }
  lines.push(`- Totals: **${count('pass')} passed, ${count('fail')} failed, ${count('skip')} skipped** of ${results.length}`);
  lines.push('');
  lines.push('| # | Suite | Check | Result | Time | Detail |');
  lines.push('|---|---|---|---|---|---|');
  results.forEach((r, i) => {
    lines.push(
      `| ${i + 1} | ${escapeCell(r.suite)} | \`${escapeCell(r.id)}\` ${escapeCell(r.title)} | ${r.status.toUpperCase()} | ${(r.ms / 1000).toFixed(1)} s | ${escapeCell(r.detail)} |`,
    );
  });
  const failures = results.filter((r) => r.status === 'fail');
  if (failures.length) {
    lines.push('');
    lines.push('## Failures, verbatim');
    for (const r of failures) {
      lines.push('');
      lines.push(`### \`${r.id}\``);
      lines.push('');
      lines.push(r.title);
      lines.push('');
      lines.push('```text');
      lines.push(r.error);
      lines.push('```');
      if (r.notes.length) {
        lines.push('');
        lines.push('Notes:');
        for (const n of r.notes) lines.push(`- ${n.replace(/\n/g, ' ')}`);
      }
      if (r.consoleErrors.length) {
        lines.push('');
        lines.push('Browser console errors seen during the check:');
        lines.push('```text');
        for (const c of r.consoleErrors) lines.push(c);
        lines.push('```');
      }
      if (r.screenshots.length) {
        lines.push('');
        lines.push(`Screenshots: ${r.screenshots.map((s) => `\`${s}\``).join(', ')}`);
      }
    }
  }
  const noted = results.filter((r) => r.status !== 'fail' && r.notes.length);
  if (noted.length) {
    lines.push('');
    lines.push('## Notes from passing and skipped checks');
    for (const r of noted) {
      lines.push('');
      lines.push(`- \`${r.id}\`: ${r.notes.map((n) => n.replace(/\n/g, ' ')).join(' · ')}`);
    }
  }
  if (meta.cleanups && meta.cleanups.length) {
    const bad = meta.cleanups.filter((c) => c.status === 'failed');
    lines.push('');
    lines.push(`## Cleanup: ${meta.cleanups.length - bad.length} done, ${bad.length} failed`);
    lines.push('');
    for (const c of meta.cleanups) {
      lines.push(`- ${c.status === 'failed' ? '**FAILED**' : 'done'}: ${c.label} (registered by \`${c.from}\`)${c.error ? ` — ${c.error.replace(/\n/g, ' ')}` : ''}`);
    }
  }
  lines.push('');
  return lines.join('\n');
}

module.exports = { Registry, SkipError, runAll, renderMarkdown, describeError, escapeCell, withTimeout };
