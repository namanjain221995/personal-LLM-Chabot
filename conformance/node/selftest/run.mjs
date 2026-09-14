// Self-test of the harness and of the suite's own test bodies, against
// selftest/design-stub.mjs. No TechSara stack is contacted.
//
// WHY every planned test and not only planned-endpoints.test.mjs (2026-09-13,
// review): the first selftest ran 11 of the 27 planned bodies; the 16 in
// models, responses and chat had never executed past their first failing call,
// and two of them held latent defects (a hard-coded 1,000,000 context window, a
// transcription `usage` that may not be null). Now:
//
//   1. planned, promoted  — every `[feature: …]` test in test/live must PASS;
//   2. planned, planned   — every one must FAIL as XPASS(strict);
//   3. streams            — the §10 order test, the item-frame test and the
//                           responses.stream() helper test against three stream
//                           shapes, proving one server CAN pass all three and a
//                           contract-legal response.queued does not fail the order;
//   4. scopes             — a stale key SKIPs with "re-provision", a scope the
//                           stack does not know yet still runs.
//
// Exit 0 only when every expectation holds.
import { spawn } from 'node:child_process';
import { readdirSync } from 'node:fs';
import { startDesignStub } from './design-stub.mjs';
import { FEATURES } from '../lib/feature-list.mjs';

const LIVE = readdirSync(new URL('../test/live/', import.meta.url))
  .filter((f) => f.endsWith('.test.mjs'))
  .map((f) => `test/live/${f}`);
const ALL_FEATURES = Object.keys(FEATURES).join(',');

const clean = { ...process.env };
for (const k of Object.keys(clean)) if (/^(TECHSARA_|CONFORMANCE_)/.test(k)) delete clean[k];

async function withStub(streamMode, fn) {
  const server = await startDesignStub({ streamMode });
  try {
    return await fn(`http://127.0.0.1:${server.address().port}/v1`);
  } finally {
    server.close();
  }
}

// WHY async spawn (2026-09-13): the stub lives in this process, and spawnSync
// blocks the event loop that would answer the child's requests — a deadlock.
function run(baseURL, files, pattern, extraEnv = {}) {
  return new Promise((resolve) => {
    const child = spawn(
      process.execPath,
      ['--test', '--test-concurrency=1', '--test-reporter=tap', `--test-name-pattern=${pattern}`, ...files],
      {
        env: {
          ...clean,
          TECHSARA_BASE_URL: baseURL,
          TECHSARA_API_KEY: 'tsk_test_selftest',
          CONFORMANCE_MIN_INTERVAL_MS: '0',
          ...extraEnv,
        },
      },
    );
    let out = '';
    child.stdout.on('data', (d) => (out += d));
    child.stderr.on('data', (d) => (out += d));
    child.on('close', () => {
      // Leaf results only: a `# Subtest` suite line is followed by its own ok line
      // after its children; suites carry "type: 'suite'" in their YAML block.
      const results = new Map();
      const lines = out.split('\n');
      for (let i = 0; i < lines.length; i++) {
        const m = /^\s*(not ok|ok) \d+ - (.+?)(?: # (SKIP|TODO)(.*))?$/.exec(lines[i]);
        if (!m) continue;
        const isSuite = lines.slice(i + 1, i + 6).some((l) => /type: 'suite'/.test(l));
        // A file with no test matching the pattern reports itself as one passing test.
        if (isSuite || /^test\/live\/[\w-]+\.test\.mjs$/.test(m[2])) continue;
        const outcome = m[1] === 'not ok' ? 'fail' : m[3] === 'SKIP' ? 'skip' : m[3] === 'TODO' ? 'todo' : 'pass';
        results.set(m[2], { outcome, note: (m[4] || '').trim() });
      }
      resolve({ results, out });
    });
  });
}

const problems = [];
const expect = (label, ok, detail) => {
  console.log(`${ok ? 'ok  ' : 'FAIL'} ${label}${detail ? ` — ${detail}` : ''}`);
  if (!ok) problems.push(label);
};
const count = (results, outcome) => [...results.values()].filter((r) => r.outcome === outcome).length;
const PLANNED = '\\[feature: ';

// 1 and 2 — every planned body, both ways.
await withStub('helper', async (base) => {
  const promoted = await run(base, LIVE, PLANNED, { CONFORMANCE_BUILT_FEATURES: ALL_FEATURES, CONFORMANCE_ALLOW_LONG_GATE: '1' });
  const planned = await run(base, LIVE, PLANNED, { CONFORMANCE_ALLOW_LONG_GATE: '1' });
  const n = promoted.results.size;
  const failing = [...promoted.results].filter(([, r]) => r.outcome !== 'pass').map(([name, r]) => `${r.outcome}: ${name}`);
  console.log(`promoted: tests=${n} pass=${count(promoted.results, 'pass')} fail=${count(promoted.results, 'fail')} skip=${count(promoted.results, 'skip')}`);
  expect('promoted: every planned body passes against the design stub', n > 0 && failing.length === 0, failing.join('; '));
  if (failing.length) console.log(promoted.out.split('\n').filter((l) => /not ok|error:|expected|actual|message/.test(l)).slice(0, 60).join('\n'));
  const xpass = (planned.out.match(/XPASS\(strict\)/g) || []).length;
  console.log(`planned:  tests=${planned.results.size} pass=${count(planned.results, 'pass')} fail=${count(planned.results, 'fail')} xpass-messages=${xpass}`);
  expect('planned: every planned body fails as XPASS(strict)', planned.results.size === n && count(planned.results, 'fail') === n && xpass >= n);
});

// 3 — stream shapes.
const ORDER = 'a stream follows the §10 order';
const FRAMES = 'the text is framed by output_item.added';
const HELPER = 'the responses.stream() helper resolves finalResponse()';
const streamPattern = `^(${[ORDER, FRAMES, HELPER].map((s) => s.replace(/[.*+?^${}()|[\]\\]/g, '\\$&')).join('|')})`;
const outcomeOf = (results, prefix) => [...results].find(([name]) => name.startsWith(prefix))?.[1]?.outcome;
for (const [mode, want] of [
  ['contract', { [ORDER]: 'pass', [FRAMES]: 'fail', [HELPER]: 'fail' }],
  ['helper', { [ORDER]: 'pass', [FRAMES]: 'pass', [HELPER]: 'pass' }],
  ['queued', { [ORDER]: 'pass', [FRAMES]: 'pass', [HELPER]: 'pass' }],
]) {
  await withStub(mode, async (base) => {
    const { results } = await run(base, ['test/live/responses.test.mjs'], streamPattern);
    for (const [prefix, outcome] of Object.entries(want)) {
      const got = outcomeOf(results, prefix);
      expect(`stream mode ${mode}: "${prefix}…" is ${outcome}`, got === outcome, `got ${got}`);
    }
  });
}

// 4 — scopes.
const EMBED = 'embeddings.create returns one 1,024-dimension float vector';
await withStub('helper', async (base) => {
  const stale = await run(base, ['test/live/planned-endpoints.test.mjs'], EMBED, {
    CONFORMANCE_BUILT_FEATURES: 'embeddings',
    TECHSARA_API_KEY_SCOPES: 'models.read responses.read responses.write',
    TECHSARA_STACK_UNKNOWN_SCOPES: '',
  });
  const s = outcomeOf(stale.results, EMBED);
  const note = [...stale.results.values()][0]?.note ?? '';
  expect('a key the stack could have given embeddings.write, but did not, SKIPs with a re-provision reason', s === 'skip' && /re-provision/.test(note), `got ${s} ${note}`);
  const young = await run(base, ['test/live/planned-endpoints.test.mjs'], EMBED, {
    CONFORMANCE_BUILT_FEATURES: 'embeddings',
    TECHSARA_API_KEY_SCOPES: 'models.read responses.read responses.write',
    TECHSARA_STACK_UNKNOWN_SCOPES: 'embeddings.write',
  });
  expect('a scope the stack did not know at provisioning still runs the test', outcomeOf(young.results, EMBED) === 'pass', `got ${outcomeOf(young.results, EMBED)}`);
});

if (problems.length) {
  console.log(`selftest FAILED: ${problems.length} expectation(s)`);
  process.exit(1);
}
console.log('selftest ok');
