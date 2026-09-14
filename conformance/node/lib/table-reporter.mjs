// A node:test reporter that writes the release table: PASS / FAIL / XFAIL / SKIP
// per test, with the reason, and the totals first.
//
// WHY a custom reporter (2026-09-13): node:test has no expected-failure outcome.
// lib/features.mjs marks an expected failure with `t.todo("XFAIL feature=…")`,
// and a todo never fails a run — so the built-in reporters would print it as a
// quiet "# TODO" next to real passes. This reporter makes the four outcomes
// separate columns a release manager can read, and exits nothing itself: the
// process exit code is still node:test's (non-zero on any FAIL, XPASS included).
import { relative } from 'node:path';

function oneLine(s, n = 220) {
  return String(s ?? '')
    .replace(/\s+/g, ' ')
    .replace(/\|/g, '\\|')
    .trim()
    .slice(0, n);
}

function failureText(error) {
  if (!error) return '';
  const inner = error.cause ?? error;
  const message = String(inner.message ?? inner);
  const status =
    typeof inner.status === 'number' && !message.startsWith(String(inner.status)) ? `HTTP ${inner.status} ` : '';
  const code = inner.code && inner.code !== 'ERR_TEST_FAILURE' ? `${inner.code}: ` : '';
  return oneLine(`${status}${code}${message}`);
}

export default async function* tableReporter(source) {
  const rows = [];
  const stack = [];
  // The file of the top-level suite: an `itPlanned` test reports the location of
  // its `it()` call, which is lib/features.mjs, not the test file that owns it.
  const files = [];
  for await (const event of source) {
    const d = event.data;
    if (event.type === 'test:start') {
      stack[d.nesting] = d.name;
      stack.length = d.nesting + 1;
      files[d.nesting] = d.file;
      files.length = d.nesting + 1;
      continue;
    }
    if (event.type !== 'test:pass' && event.type !== 'test:fail') continue;
    // A suite is a row only when it was skipped whole (e.g. no base URL): its
    // tests never ran, so they emit nothing of their own.
    const skippedSuite = d.details?.type === 'suite' && d.skip !== undefined && d.skip !== false;
    if (d.details?.type === 'suite' && !skippedSuite) continue;
    const path = [...stack.slice(0, d.nesting), d.name].join(' › ');
    const owner = files[0] ?? d.file;
    const file = owner ? relative(process.cwd(), owner) : '';
    let outcome;
    let detail = '';
    if (event.type === 'test:fail') {
      outcome = 'FAIL';
      detail = failureText(d.details?.error);
    } else if (d.skip !== undefined && d.skip !== false) {
      outcome = 'SKIP';
      detail = oneLine(typeof d.skip === 'string' ? d.skip : '');
    } else if (typeof d.todo === 'string' && d.todo.startsWith('XFAIL')) {
      outcome = 'XFAIL';
      detail = oneLine(d.todo.replace(/^XFAIL /, ''));
    } else if (d.todo !== undefined && d.todo !== false) {
      outcome = 'TODO';
      detail = oneLine(typeof d.todo === 'string' ? d.todo : '');
    } else {
      outcome = 'PASS';
    }
    rows.push({ file, path, outcome, detail, ms: Math.round(d.details?.duration_ms ?? 0) });
  }

  const count = (o) => rows.filter((r) => r.outcome === o).length;
  yield `# Conformance run (openai-node)\n\n`;
  yield `Generated ${new Date().toISOString()}\n\n`;
  // WHY the base URL (2026-09-13, review): the orchestrator port and the Next
  // /v1 edge are different hops with different behaviour (header forwarding,
  // stream buffering); a table that does not say which one it measured cannot
  // be compared with another. This file is git-ignored.
  yield `Base URL: ${process.env.TECHSARA_BASE_URL || '(unset — live tests skipped)'}\n\n`;
  yield `| PASS | FAIL | XFAIL | SKIP | total |\n|---:|---:|---:|---:|---:|\n`;
  yield `| ${count('PASS')} | ${count('FAIL')} | ${count('XFAIL')} | ${count('SKIP')} | ${rows.length} |\n\n`;
  yield `| outcome | file | test | ms | detail |\n|---|---|---|---:|---|\n`;
  for (const r of rows) {
    yield `| ${r.outcome} | ${oneLine(r.file, 80)} | ${oneLine(r.path, 200)} | ${r.ms} | ${r.detail} |\n`;
  }
}
