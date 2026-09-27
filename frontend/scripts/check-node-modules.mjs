/**
 * Fails loudly when `node_modules` and `package.json` disagree.
 *
 * WHY THIS EXISTS — the failure it replaces
 * ----------------------------------------
 * `remark-breaks` was added to `package.json` and `package-lock.json` on
 * 2026-09-18 (47e52f4, "a single newline in an answer is a line break on
 * screen"). The deploy root's `frontend/node_modules` was last installed on
 * 2026-09-09 and was never refreshed. Both trees are still on disk, so anyone
 * who seeded a working copy by copying `node_modules` from the deploy root —
 * the documented way to get a test tree on this box — inherited a tree nine
 * days older than the manifest and got this, 39 times over:
 *
 *     Failed to resolve import "remark-breaks" from "components/Markdown.tsx"
 *
 * Nothing in the repository was wrong: the lockfile pins remark-breaks 4.0.0,
 * `npm ci` installs it, and the production image has it compiled into the
 * bundle. Only the copied tree was stale. But the cost landed on whoever
 * copied it, as 39 unrelated-looking Vite resolve errors in files that have
 * nothing to do with markdown, and it cost more than one engineer real time
 * before anyone traced it back to a missing directory.
 *
 * A stale tree is not a small lie either, and the cost is in the TESTS, not
 * in a package count. Measured 2026-09-28 against the deploy root's tree with
 * a clean `npm ci` beside it: the stale tree was short exactly THREE
 * directories — `remark-breaks` (the only DECLARED one, and it sits in
 * `dependencies`), its transitive `mdast-util-newline-to-break`, and the
 * platform-optional `@next/swc-linux-arm64-musl`. Those three hid 733 tests
 * from the run entirely (2790 collected instead of dev's 3523) and broke 17
 * more that did run. A suite that quietly drops a fifth of itself and still
 * prints a tidy summary is worse than one that refuses to start.
 *
 * Do not restate that gap as a package count. The counts are real but they
 * are not comparable: `ls node_modules | wc -l` is 560 on the stale tree and
 * 562 on the clean one, because it collapses 122 scoped packages into 34
 * `@scope` entries, while `npm ci` reports `added 694 packages`. Subtracting
 * one from the other invents a gap of 134 packages that does not exist, and
 * an earlier draft of this header did exactly that.
 *
 * So the repository detects the disagreement instead of hoping nobody hits it
 * again. This runs as vitest's `globalSetup`, which means it fires ONCE before
 * a single test file is collected, and it names the missing packages and the
 * command that fixes them. It deliberately does NOT reinstall anything: a
 * guard that silently mutates the tree it was asked to check would hide the
 * very drift it exists to report, and `npm ci` in the wrong directory is how
 * this class of problem gets made in the first place.
 *
 * KNOWN LIMIT, stated so nobody assumes otherwise: it compares the DECLARED
 * dependencies only, so a missing TRANSITIVE package still reaches the run as
 * a raw resolve error. That is not hypothetical — `mdast-util-newline-to-break`
 * was missing from the same tree. It is deliberately not worth a lockfile walk
 * here: that package went missing because its parent did, and naming the
 * parent is already what tells you to run `npm ci`.
 *
 * It imports nothing but Node builtins, on purpose — it has to be able to run
 * and report against a tree that is missing packages, including a tree that is
 * missing everything.
 */

import { readFileSync, existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

/** The frontend package root (this file lives in `<root>/scripts`). */
export const packageRoot = dirname(dirname(fileURLToPath(import.meta.url)));

/**
 * A version range this checker is willing to judge: exact, caret or tilde
 * against a plain `x.y.z`. Everything else — `*`, `x`, `>=1 <2`, `a || b`,
 * `file:`, `link:`, `workspace:`, `npm:` aliases, git and http URLs — parses
 * to null and is presence-checked only.
 *
 * Refusing to guess is the point. A guard that cries wolf on a range it did
 * not understand gets switched off, and then it is not a guard.
 */
function parseSimpleRange(range) {
  const match = /^(\^|~)?(\d+)\.(\d+)\.(\d+)$/.exec(String(range).trim());
  if (!match) return null;
  return {
    operator: match[1] ?? '=',
    major: Number(match[2]),
    minor: Number(match[3]),
    patch: Number(match[4]),
  };
}

/** An installed `version` string, or null when it is not a plain release. */
function parseVersion(version) {
  const match = /^(\d+)\.(\d+)\.(\d+)$/.exec(String(version).trim());
  if (!match) return null;
  return {
    major: Number(match[1]),
    minor: Number(match[2]),
    patch: Number(match[3]),
  };
}

/**
 * Does `installed` satisfy `range`?
 *
 * Returns true, false, or null for "this checker cannot tell" — a prerelease,
 * or a range shape it does not parse. Null never fails the build: semver is
 * not a dependency here, and the drift this catches (a tree copied from an
 * older commit) always shows up as a plain major or minor difference.
 */
export function versionSatisfies(installed, range) {
  const wanted = parseSimpleRange(range);
  const have = parseVersion(installed);
  if (!wanted || !have) return null;

  if (wanted.operator === '=') {
    return have.major === wanted.major
      && have.minor === wanted.minor
      && have.patch === wanted.patch;
  }

  // Caret and tilde both pin the major.
  if (have.major !== wanted.major) return false;
  // Tilde pins the minor; so does caret below 1.0.0, where every minor bump
  // is a breaking change by semver's own rule.
  if (wanted.operator === '~' && have.minor !== wanted.minor) return false;
  if (wanted.operator === '^' && wanted.major === 0 && have.minor !== wanted.minor) {
    return false;
  }
  // And `^0.0.x` is the tightest range semver has: it admits that patch and
  // nothing else, because below 0.1.0 every patch bump may break. Without
  // this line `^0.0.3` accepted 0.0.4, which `semver.satisfies` rejects.
  if (wanted.operator === '^' && wanted.major === 0 && wanted.minor === 0
      && have.patch !== wanted.patch) {
    return false;
  }
  // And the installed version may not be older than the floor.
  if (have.minor < wanted.minor) return false;
  if (have.minor === wanted.minor && have.patch < wanted.patch) return false;
  return true;
}

/**
 * What one inspection of a tree concluded.
 *
 * @typedef {object} DepReport
 * @property {string} root the package root that was inspected
 * @property {number} checked how many declared dependencies were considered
 * @property {string[]} missing declared, but not installed
 * @property {Array<{name: string, want: string, have: string}>} drifted installed at a version the manifest forbids
 * @property {boolean} manifestMissing there is no package.json at `root`
 * @property {boolean} manifestUnreadable there is one, and it is not valid JSON
 */

/**
 * Compare `package.json`'s dependencies against what is actually installed.
 *
 * `dependencies` and `devDependencies` are both checked because a test run
 * needs the dev half to happen at all — not because that half was broken
 * here. The tree that prompted this was short in `dependencies` only.
 *
 * @param {{ root?: string }} options
 * @returns {DepReport}
 */
export function checkInstalledDeps({ root = packageRoot } = {}) {
  const manifestPath = join(root, 'package.json');
  if (!existsSync(manifestPath)) {
    return {
      root,
      checked: 0,
      missing: [],
      drifted: [],
      manifestMissing: true,
      manifestUnreadable: false,
    };
  }

  let manifest;
  try {
    manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
  } catch {
    // A root manifest that will not parse is a real broken state, and it has
    // to be REPORTED, not thrown as a raw SyntaxError: this runs as
    // globalSetup, where a stack trace out of JSON.parse is exactly the
    // unreadable failure this whole file exists to replace.
    return {
      root,
      checked: 0,
      missing: [],
      drifted: [],
      manifestMissing: false,
      manifestUnreadable: true,
    };
  }
  const wanted = {
    ...(manifest.dependencies ?? {}),
    ...(manifest.devDependencies ?? {}),
  };

  const missing = [];
  const drifted = [];

  for (const [name, range] of Object.entries(wanted)) {
    // `npm` lays every package out as its own directory, scoped ones included,
    // so the manifest inside it is the only thing worth looking at.
    const installedManifest = join(root, 'node_modules', name, 'package.json');
    if (!existsSync(installedManifest)) {
      missing.push(name);
      continue;
    }
    let installedVersion;
    try {
      installedVersion = JSON.parse(readFileSync(installedManifest, 'utf8')).version;
    } catch {
      // A package.json that will not parse is a broken install, not a version
      // problem. Report it the same way a missing directory is reported.
      missing.push(name);
      continue;
    }
    if (versionSatisfies(installedVersion, range) === false) {
      drifted.push({ name, want: range, have: String(installedVersion) });
    }
  }

  return {
    root,
    checked: Object.keys(wanted).length,
    missing,
    drifted,
    manifestMissing: false,
    manifestUnreadable: false,
  };
}

/**
 * True when the report describes a tree that disagrees with the manifest.
 *
 * @param {DepReport} report
 */
export function isFailure(report) {
  return report.manifestUnreadable === true
    || report.missing.length > 0
    || report.drifted.length > 0;
}

/**
 * The message a developer or agent actually reads. It has to answer three
 * questions without a second command: what is wrong, why it is not the
 * branch's fault, and what to type.
 */
/** @param {DepReport} report */
export function formatFailure(report) {
  if (report.manifestUnreadable) {
    return [
      '',
      `frontend/package.json is not valid JSON: ${join(report.root, 'package.json')}`,
      '',
      '  The manifest cannot be parsed, so node_modules cannot be checked',
      '  against it. A truncated write or a half-resolved merge conflict is',
      '  the usual cause. Fix the JSON and run this again.',
      '',
    ].join('\n');
  }

  const lines = [
    '',
    'frontend/node_modules does not match frontend/package.json.',
    '',
  ];

  if (report.missing.length > 0) {
    lines.push(`  ${report.missing.length} package(s) declared in package.json are NOT installed:`);
    for (const name of report.missing) lines.push(`    - ${name}`);
    lines.push('');
  }
  if (report.drifted.length > 0) {
    lines.push(`  ${report.drifted.length} package(s) are installed at a version the manifest does not allow:`);
    for (const d of report.drifted) {
      lines.push(`    - ${d.name}: package.json wants ${d.want}, installed ${d.have}`);
    }
    lines.push('');
  }

  lines.push(
    '  Fix it in this working copy:',
    '',
    `      cd ${report.root} && npm ci`,
    '',
    '  This is almost always a COPIED node_modules, not a broken branch. The',
    '  deploy root is a shared production checkout whose node_modules can be',
    '  older than its package.json, so `cp -a` from it yields a tree that is',
    '  missing whatever landed since it was last installed. The repository is',
    '  fine; the copy is stale. Run `npm ci` in YOUR OWN working copy — never',
    '  in the deploy root, whose containers other engineers depend on.',
    '',
  );
  return lines.join('\n');
}

/**
 * The guard itself, shared by vitest and the CLI, with both of its side
 * effects injectable so the failure path is testable without killing the
 * process that is running the tests.
 *
 * It REPORTS AND EXITS rather than throwing, and that is deliberate. Measured
 * in a scratch vitest project on 2026-09-28: both forms exit 1, but a `throw`
 * out of `globalSetup` produces 28 lines of output and puts vitest's own
 * `No test files found, exiting with code 1` ABOVE the real message — because
 * collection never happened, so vitest truthfully reports zero files — then
 * the include/exclude patterns, then a nine-frame stack whose top frame points
 * at this file's standalone CLI block rather than at the throw. A reader
 * scanning that top line goes hunting for a bad test filter. Exiting produces
 * 7 lines: the banner, this message, nothing else. One accurate message up
 * front is the entire point of the change, so the guard must not bury its own.
 *
 * @param {{ report?: DepReport, write?: (text: string) => void, exit?: (code: number) => void }} io
 * @returns {DepReport} the report, when the run is allowed to continue
 */
export function runGuard(io = {}) {
  const report = io.report ?? checkInstalledDeps();
  const write = io.write ?? ((text) => process.stderr.write(text));
  const exit = io.exit ?? ((code) => process.exit(code));

  // No manifest at all is not this checker's business — there is nothing for
  // the tree to disagree with. The CLI still treats it as an error, because
  // being asked the question in the wrong directory IS a mistake.
  if (report.manifestMissing) return report;

  if (isFailure(report)) {
    write(`${formatFailure(report)}\n`);
    exit(1);
  }
  return report;
}

/**
 * vitest `globalSetup`. Runs ONCE before the first test file is collected.
 *
 * vitest calls this with a context argument. This function deliberately
 * declares none, so that context can never be mistaken for `runGuard`'s
 * injectable io and silently replace the real report with a vitest object.
 */
export function setup() {
  runGuard();
}

// Also runnable on its own — `npm run check:deps` — so CI and a human can ask
// the question without starting the suite.
if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const report = runGuard();
  if (report.manifestMissing) {
    console.error('check-node-modules: no package.json at', report.root);
    process.exit(1);
  }
  console.log(
    `check-node-modules: ${report.checked} declared dependencies are installed and in range.`,
  );
}
