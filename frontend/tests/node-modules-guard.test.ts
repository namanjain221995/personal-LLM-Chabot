/**
 * The guard that refuses to run the suite against a stale `node_modules`.
 *
 * Context: `remark-breaks` has been in package.json and package-lock.json
 * since 2026-09-18, but the deploy root's `frontend/node_modules` was last
 * installed on 2026-09-09. Copying that tree — the documented way to seed a
 * working copy on this box — produced 39 files failing with
 * `Failed to resolve import "remark-breaks" from "components/Markdown.tsx"`,
 * and, worse, a run that collected 2790 tests instead of 3523 and still
 * printed a tidy summary.
 *
 * These tests pin four separate things, because any one of them alone leaves
 * the hole open:
 *   1. the checker's judgement (what counts as a disagreement),
 *   2. what the guard DOES with that judgement — the message and the exit,
 *   3. that it is WIRED into vitest, so it actually runs, and
 *   4. that THIS working copy's tree agrees with its manifest right now.
 */

import { mkdtempSync, mkdirSync, writeFileSync, rmSync, readFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
import { afterEach, describe, expect, test } from 'vitest';

import {
  checkInstalledDeps,
  formatFailure,
  isFailure,
  packageRoot,
  runGuard,
  setup,
  versionSatisfies,
} from '../scripts/check-node-modules.mjs';

const temporaryRoots: string[] = [];

afterEach(() => {
  while (temporaryRoots.length > 0) {
    const dir = temporaryRoots.pop();
    if (dir) rmSync(dir, { recursive: true, force: true });
  }
});

/**
 * Build a throwaway package root: a manifest, plus whatever is "installed".
 */
function fixture(
  manifest: Record<string, unknown>,
  installed: Record<string, string | null>,
): string {
  const root = mkdtempSync(join(tmpdir(), 'deps-guard-'));
  temporaryRoots.push(root);
  writeFileSync(join(root, 'package.json'), JSON.stringify(manifest));
  for (const [name, version] of Object.entries(installed)) {
    if (version === null) continue; // "not installed"
    const dir = join(root, 'node_modules', ...name.split('/'));
    mkdirSync(dir, { recursive: true });
    writeFileSync(join(dir, 'package.json'), JSON.stringify({ name, version }));
  }
  return root;
}

describe('versionSatisfies', () => {
  test('caret pins the major and sets a floor', () => {
    expect(versionSatisfies('4.0.0', '^4.0.0')).toBe(true);
    expect(versionSatisfies('4.3.1', '^4.0.0')).toBe(true);
    // The exact drift a copied tree produces: a whole major behind.
    expect(versionSatisfies('3.9.9', '^4.0.0')).toBe(false);
    expect(versionSatisfies('5.0.0', '^4.0.0')).toBe(false);
    // Older than the floor inside the same major is still wrong.
    expect(versionSatisfies('4.0.0', '^4.0.1')).toBe(false);
    expect(versionSatisfies('4.1.0', '^4.2.0')).toBe(false);
  });

  test('caret below 1.0.0 pins the minor, as semver requires', () => {
    expect(versionSatisfies('0.4.2', '^0.4.0')).toBe(true);
    expect(versionSatisfies('0.5.0', '^0.4.0')).toBe(false);
  });

  test('caret below 0.1.0 pins the patch as well — the tightest range there is', () => {
    // `^0.0.3` admits 0.0.3 and nothing else. Checked against the real
    // `semver` (installed transitively) on 2026-09-28: satisfies('0.0.4',
    // '^0.0.3') is false there too. This comparator returned true until the
    // patch guard was added, which is a false PASS — the one direction a
    // guard must never fail in.
    expect(versionSatisfies('0.0.3', '^0.0.3')).toBe(true);
    expect(versionSatisfies('0.0.4', '^0.0.3')).toBe(false);
    expect(versionSatisfies('0.0.2', '^0.0.3')).toBe(false);
    expect(versionSatisfies('0.1.0', '^0.0.3')).toBe(false);
    // Tilde is the looser one here and must stay looser: ~0.0.3 is <0.1.0.
    expect(versionSatisfies('0.0.4', '~0.0.3')).toBe(true);
  });

  test('tilde pins the minor', () => {
    expect(versionSatisfies('1.2.9', '~1.2.0')).toBe(true);
    expect(versionSatisfies('1.3.0', '~1.2.0')).toBe(false);
  });

  test('an exact range means exactly that', () => {
    expect(versionSatisfies('1.2.3', '1.2.3')).toBe(true);
    expect(versionSatisfies('1.2.4', '1.2.3')).toBe(false);
  });

  test('a range it cannot parse is null, never false', () => {
    // Refusing to guess is deliberate: a guard that cries wolf on a range it
    // did not understand gets switched off, and then it guards nothing.
    for (const range of ['*', 'x', '>=1.0.0 <2.0.0', '1 || 2', 'latest',
      'file:../local', 'link:../local', 'workspace:*',
      'npm:other-package@^1.0.0', 'github:owner/repo',
      'https://example.invalid/p.tgz']) {
      expect(versionSatisfies('1.0.0', range), range).toBeNull();
    }
    // Prereleases are not judged without a real semver implementation.
    expect(versionSatisfies('4.0.0-rc.1', '^4.0.0')).toBeNull();
  });
});

describe('checkInstalledDeps', () => {
  test('a tree that matches its manifest is not a failure', () => {
    const root = fixture(
      { dependencies: { 'remark-gfm': '^4.0.1' }, devDependencies: { vitest: '^3.2.0' } },
      { 'remark-gfm': '4.0.1', vitest: '3.2.4' },
    );
    const report = checkInstalledDeps({ root });
    expect(report.missing).toEqual([]);
    expect(report.drifted).toEqual([]);
    expect(report.checked).toBe(2);
    expect(isFailure(report)).toBe(false);
  });

  test('THE REGRESSION: a declared dependency that is not installed is caught', () => {
    // This is the deploy-root tree, reduced to its essence.
    const root = fixture(
      { dependencies: { 'remark-gfm': '^4.0.1', 'remark-breaks': '^4.0.0' } },
      { 'remark-gfm': '4.0.1', 'remark-breaks': null },
    );
    const report = checkInstalledDeps({ root });
    expect(report.missing).toEqual(['remark-breaks']);
    expect(isFailure(report)).toBe(true);
  });

  test('devDependencies are checked too, not just dependencies', () => {
    // Measured, because an earlier draft of this comment claimed 134 missing
    // packages "across both halves" and both halves of that were wrong: the
    // stale tree was short ONE declared package, `remark-breaks`, which lives
    // in `dependencies`, plus its transitive `mdast-util-newline-to-break`.
    // Nothing in `devDependencies` was missing. The dev half is checked
    // because a test run cannot happen without it, not because it broke here.
    const root = fixture(
      { devDependencies: { jsdom: '^26.1.0' } },
      { jsdom: null },
    );
    expect(checkInstalledDeps({ root }).missing).toEqual(['jsdom']);
  });

  test('a scoped package resolves through its own directory', () => {
    const root = fixture(
      { dependencies: { '@fontsource/ibm-plex-sans': '^5.2.5' } },
      { '@fontsource/ibm-plex-sans': '5.2.5' },
    );
    expect(isFailure(checkInstalledDeps({ root }))).toBe(false);

    const bad = fixture(
      { dependencies: { '@fontsource/ibm-plex-sans': '^5.2.5' } },
      { '@fontsource/ibm-plex-sans': null },
    );
    expect(checkInstalledDeps({ root: bad }).missing)
      .toEqual(['@fontsource/ibm-plex-sans']);
  });

  test('an installed version the manifest forbids is reported as drift', () => {
    const root = fixture(
      { dependencies: { next: '^16.3.3' } },
      { next: '15.5.0' },
    );
    const report = checkInstalledDeps({ root });
    expect(report.missing).toEqual([]);
    expect(report.drifted).toEqual([
      { name: 'next', want: '^16.3.3', have: '15.5.0' },
    ]);
    expect(isFailure(report)).toBe(true);
  });

  test('a present package with an unparseable range is left alone', () => {
    const root = fixture(
      { dependencies: { thing: 'github:owner/thing' } },
      { thing: '0.0.0-dev' },
    );
    expect(isFailure(checkInstalledDeps({ root }))).toBe(false);
  });

  test('an unreadable installed manifest counts as missing, not as drift', () => {
    const root = mkdtempSync(join(tmpdir(), 'deps-guard-'));
    temporaryRoots.push(root);
    writeFileSync(join(root, 'package.json'), JSON.stringify({ dependencies: { broken: '^1.0.0' } }));
    mkdirSync(join(root, 'node_modules', 'broken'), { recursive: true });
    writeFileSync(join(root, 'node_modules', 'broken', 'package.json'), '{ not json');
    const report = checkInstalledDeps({ root });
    expect(report.missing).toEqual(['broken']);
    expect(report.drifted).toEqual([]);
  });

  test('a ROOT package.json that will not parse is reported, not thrown', () => {
    // It used to die with a raw SyntaxError out of JSON.parse. In globalSetup
    // that is precisely the unreadable failure this file exists to replace,
    // and it is the one bad input that got no clean message.
    const root = mkdtempSync(join(tmpdir(), 'deps-guard-'));
    temporaryRoots.push(root);
    writeFileSync(join(root, 'package.json'), '{ not json');

    let report!: ReturnType<typeof checkInstalledDeps>;
    expect(() => { report = checkInstalledDeps({ root }); }).not.toThrow();
    expect(report.manifestUnreadable).toBe(true);
    expect(report.manifestMissing).toBe(false);
    expect(isFailure(report)).toBe(true);

    const message = formatFailure(report);
    expect(message).toContain('is not valid JSON');
    expect(message).toContain(root);
    expect(message).not.toContain('SyntaxError');
  });

  test('no manifest at all is not this checker\'s business', () => {
    const root = mkdtempSync(join(tmpdir(), 'deps-guard-'));
    temporaryRoots.push(root);
    const report = checkInstalledDeps({ root });
    expect(report.manifestMissing).toBe(true);
    expect(isFailure(report)).toBe(false);
  });
});

describe('the message', () => {
  test('names every missing package, the fix, and where to run it', () => {
    const root = fixture(
      { dependencies: { 'remark-breaks': '^4.0.0', next: '^16.3.3' } },
      { 'remark-breaks': null, next: '15.5.0' },
    );
    const message = formatFailure(checkInstalledDeps({ root }));

    expect(message).toContain('remark-breaks');
    expect(message).toContain('next');
    expect(message).toContain('package.json wants ^16.3.3, installed 15.5.0');
    // The command, and the directory it belongs in — the whole point is that
    // the reader needs no second command to act.
    expect(message).toContain('npm ci');
    expect(message).toContain(root);
    // And the warning that stops the reader fixing the wrong tree.
    expect(message).toContain('never');
    expect(message.toLowerCase()).toContain('deploy root');
  });
});

describe('the guard is actually wired in', () => {
  test('vitest runs the checker as globalSetup', () => {
    // A checker nobody calls is decoration. If this assertion is ever the only
    // thing standing between a copied node_modules and 39 misleading resolve
    // errors, it should be the thing that fails.
    const config = readFileSync(join(packageRoot, 'vitest.config.mts'), 'utf8');
    expect(config).toContain('globalSetup');
    expect(config).toContain('./scripts/check-node-modules.mjs');
  });

  test('package.json exposes it as a standalone script', () => {
    const manifest = JSON.parse(
      readFileSync(join(packageRoot, 'package.json'), 'utf8'),
    ) as { scripts?: Record<string, string> };
    expect(manifest.scripts?.['check:deps']).toBe('node scripts/check-node-modules.mjs');
  });
});

describe('runGuard', () => {
  /** Collect what the guard would print and what it would exit with. */
  function spy() {
    const written: string[] = [];
    const exited: number[] = [];
    return {
      written,
      exited,
      write: (text: string) => { written.push(text); },
      exit: (code: number) => { exited.push(code); },
    };
  }

  test('a disagreeing tree is written to the stream and exits 1', () => {
    // It reports and exits instead of throwing, because a throw out of
    // globalSetup makes vitest print `No test files found` ABOVE the real
    // message plus a stack that points at the wrong function. Those 21 extra
    // lines are what this asserts against.
    const io = spy();
    const report = checkInstalledDeps({
      root: fixture(
        { dependencies: { 'remark-breaks': '^4.0.0' } },
        { 'remark-breaks': null },
      ),
    });

    runGuard({ report, write: io.write, exit: io.exit });

    expect(io.exited).toEqual([1]);
    expect(io.written.join('')).toContain('remark-breaks');
    expect(io.written.join('')).toContain('npm ci');
  });

  test('a healthy tree is silent and does not exit', () => {
    const io = spy();
    const report = checkInstalledDeps({
      root: fixture(
        { dependencies: { 'remark-gfm': '^4.0.1' } },
        { 'remark-gfm': '4.0.1' },
      ),
    });

    runGuard({ report, write: io.write, exit: io.exit });

    expect(io.exited).toEqual([]);
    expect(io.written).toEqual([]);
  });

  test('a missing manifest neither prints nor exits, and says so in the report', () => {
    const io = spy();
    const root = mkdtempSync(join(tmpdir(), 'deps-guard-'));
    temporaryRoots.push(root);

    const returned = runGuard({
      report: checkInstalledDeps({ root }),
      write: io.write,
      exit: io.exit,
    });

    expect(io.exited).toEqual([]);
    expect(io.written).toEqual([]);
    expect(returned.manifestMissing).toBe(true);
  });
});

describe('this working copy', () => {
  test('its node_modules agrees with its package.json', () => {
    // The invariant that was violated. If this fails, read the message: it is
    // your tree, not this branch.
    const report = checkInstalledDeps();
    expect(report.manifestMissing).toBe(false);
    expect(report.checked).toBeGreaterThan(0);
    expect({ missing: report.missing, drifted: report.drifted })
      .toEqual({ missing: [], drifted: [] });
  });

  test('remark-breaks in particular is installed', () => {
    // Named explicitly because this is the package whose absence started it,
    // and because components/Markdown.tsx imports it at module scope: without
    // it, every test that touches a chat message fails to collect.
    const installed = JSON.parse(
      readFileSync(join(packageRoot, 'node_modules', 'remark-breaks', 'package.json'), 'utf8'),
    ) as { version: string };
    const declared = (
      JSON.parse(readFileSync(join(packageRoot, 'package.json'), 'utf8')) as {
        dependencies: Record<string, string>;
      }
    ).dependencies['remark-breaks'];
    expect(declared).toBeTruthy();
    expect(versionSatisfies(installed.version, declared)).toBe(true);
  });

  test('setup() lets a healthy tree through without throwing', () => {
    expect(() => setup()).not.toThrow();
  });
});
