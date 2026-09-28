'use strict';
/**
 * THE CHECK THIS STAGE WAS RED ON, PINNED (2026-09-27).
 *
 * `console.playground` was red about one run in five with "expected one
 * playground execute call, saw 0" after 0.4 s, and the cause was in what it
 * waited for, not in the product:
 *
 *   * frontend/components/devplatform/Playground.tsx renders
 *     `{output || (running ? '' : 'The answer appears here.')}` INSIDE
 *     [data-testid="playground-output"], so a wait for `innerText.trim().length
 *     > 0` is satisfied by the PLACEHOLDER, before the run has started;
 *   * the "and it has stopped running" half of the same predicate was
 *     `button[aria-label="Stop"], button.stop`, and the Stop button carries
 *     neither, so that half matched nothing and was a no-op. Measured today in
 *     headless Chrome against a fixture of that pane: 0 matches for both
 *     selectors while a Stop button was up, 1 for a match on its text.
 *
 * So the wait returned before the POST was issued and fell through to
 * `assert.equal(calls.length, 1)` against 0. These assertions are what stops
 * either half coming back. They are SOURCE assertions on purpose: this file
 * runs in the job's first step, on the standard library alone, before anything
 * is installed or built.
 */

const assert = require('node:assert');
const { test } = require('node:test');
const fs = require('node:fs');
const path = require('node:path');

const REPO = path.resolve(__dirname, '..', '..', '..');
const CONSOLE_SUITE = path.join(REPO, 'e2e', 'platform', 'suites', 'console.js');
const PLAYGROUND = path.join(REPO, 'frontend', 'components', 'devplatform', 'Playground.tsx');

const consoleText = fs.readFileSync(CONSOLE_SUITE, 'utf8');
const playgroundText = fs.readFileSync(PLAYGROUND, 'utf8');

/**
 * The body of the `console.playground` check, from its id to the next t.add,
 * with `//` comments stripped: a comment that QUOTES the broken predicate is
 * the record of the fix, not the fix being undone, and the check below for the
 * dead selectors would otherwise match its own explanation.
 */
function playgroundCheck() {
  const start = consoleText.indexOf("'console.playground'");
  assert.ok(start > -1, 'the console.playground check is gone; this test needs rewriting');
  const end = consoleText.indexOf('t.add(', start);
  return consoleText
    .slice(start, end > -1 ? end : undefined)
    .split('\n')
    .filter((line) => !line.trim().startsWith('//'))
    .join('\n');
}

test('the suite knows the exact placeholder the output pane renders', () => {
  // Read out of the component, not retyped: a copy that drifts turns the wait
  // back into one the placeholder satisfies.
  const rendered = playgroundText.match(/\(running \? '' : '([^']+)'\)/);
  assert.ok(rendered, 'Playground.tsx no longer renders a placeholder in the output pane; re-check the wait');
  const declared = consoleText.match(/const PLAYGROUND_OUTPUT_PLACEHOLDER = '([^']+)';/);
  assert.ok(declared, 'console.js declares no PLAYGROUND_OUTPUT_PLACEHOLDER');
  assert.strictEqual(declared[1], rendered[1]);
});

test('the placeholder is inside the element the wait reads, which is why it matters', () => {
  const pane = playgroundText.indexOf('data-testid="playground-output"');
  assert.ok(pane > -1, 'the output pane lost its test id');
  const window = playgroundText.slice(pane, pane + 500);
  assert.match(window, /\(running \? '' : 'The answer appears here\.'\)/);
});

test('the playground wait excludes the placeholder', () => {
  const body = playgroundCheck();
  assert.ok(
    body.includes('text !== placeholder'),
    'the playground wait no longer excludes the placeholder, so it is satisfied before the run starts',
  );
});

test('the playground wait does not use the two selectors that match no Stop button', () => {
  const body = playgroundCheck();
  assert.ok(
    !body.includes('button[aria-label="Stop"]'),
    'button[aria-label="Stop"] matches nothing in Playground.tsx: the wait would not see a run in flight',
  );
  assert.ok(
    !body.includes('button.stop'),
    'button.stop matches nothing in Playground.tsx: the wait would not see a run in flight',
  );
  // And the shape that does work, on the button's text.
  assert.match(body, /=== 'stop'/);
});

test('neither selector exists in the component, which is the measurement behind the test above', () => {
  assert.ok(!playgroundText.includes('aria-label="Stop"'), 'the Stop button now HAS an aria-label; the wait may use it');
  assert.ok(!/className=\{?['"`][^'"`]*\bstop\b/.test(playgroundText), 'the Stop button now has a `stop` class');
});

test('the playground waits for the intercepted execute call before asserting on it', () => {
  const body = playgroundCheck();
  const wait = body.indexOf('while (calls.length === 0');
  const firstAssert = body.indexOf('assert.equal(calls.length, 1');
  assert.ok(wait > -1, 'the playground no longer waits for the execute call, so the assert races the POST');
  assert.ok(firstAssert > -1, 'the playground no longer asserts on the execute call at all');
  assert.ok(wait < firstAssert, 'the wait for the execute call must come BEFORE the assert on it');
  // A wait with no ceiling would hang the check instead of failing it.
  assert.match(body.slice(wait - 200, wait), /Date\.now\(\) \+ 20_000/);
});

test('the live-mode wait says WHY it expired, because nothing runs that path', () => {
  // WHAT THE REPAIR COSTS IN `--chat-mode live`, measured 2026-09-28 in a
  // fixture of Playground.tsx:505-528 driven in headless Chrome (live mode needs
  // a real model on the GPUs, so the sequence was reproduced; timeout scaled
  // 170 s -> 6 s):
  //
  //   generation still running at the deadline  old RESOLVED after 3 ms (first
  //                                             token), new REJECTED at the
  //                                             deadline
  //   generation finishes inside the deadline   old RESOLVED after 2 ms (first
  //                                             token), new RESOLVED after
  //                                             2504 ms (when it finished)
  //
  // The repair is right -- the old wait asserted nothing in live mode -- but it
  // turns a long live answer into a failure, and pipeline.yml runs
  // `--chat-mode stub` while e2e/platform/README.md lists live as not exercised.
  // So the ONE path this change alters is the one path nobody has run, and its
  // failure has to name the state rather than a millisecond count. This is NOT a
  // licence to widen the timeout or to drop the `!running` half.
  const body = playgroundCheck();
  assert.ok(
    /!running\)/.test(body),
    'the wait no longer requires the generation to have STOPPED; it would pass on the first token again',
  );
  assert.match(
    body,
    /STILL GENERATING/,
    'the live-mode timeout no longer reports that the generation was still running, so the first person '
      + 'to run `--chat-mode live` gets a bare millisecond count',
  );
  assert.match(
    body,
    /never rendered any output/,
    'the live-mode timeout no longer distinguishes "still generating" from "nothing ever arrived"',
  );
});
