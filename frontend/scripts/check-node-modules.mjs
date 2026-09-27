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
 * A stale tree is not a small lie either. That one measured 560 packages
 * against the lockfile's 694: it hid 733 tests from the run entirely (2790
 * collected instead of 3523) and broke 17 more that did run. A suite that
 * quietly drops a fifth of itself and still prints a tidy summary is worse
 * than one that refuses to start.
 *
 * So the repository detects the disagreement instead of hoping nobody hits it
 * again. This runs as vitest's `globalSetup`, which means it fires ONCE before
 * a single test file is collected, and it names the missing packages and the
 * command that fixes them. It deliberately does NOT reinstall anything: a
 * guard that silently mutates the tree it was asked to check would hide the
 * very drift it exists to report, and `npm ci` in the wrong directory is how
 * this class of problem gets made in the first place.
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
  // And the installed version may not be older than the floor.
  if (have.minor < wanted.minor) return false;
  if (have.minor === wanted.minor && have.patch < wanted.patch) return false;
  return true;
}

/**
 * Compare `package.json`'s dependencies against what is actually installed.
 *
 * `dependencies` and `devDependencies` are both checked: a test run needs the
 * dev half, and the stale tree that prompted this was short on both.
 *
 * @param {{ root?: string }} options
 * @returns {{ root: string, checked: number, missing: string[], drifted: Array<{name: string, want: string, have: string}>, manifestMissing: boolean }}
 */
export function checkInstalledDeps({ root = packageRoot } = {}) {
  const manifestPath = join(root, 'package.json');
  if (!existsSync(manifestPath)) {
    return { root, checked: 0, missing: [], drifted: [], manifestMissing: true };
  }

  const manifest = JSON.parse(readFileSync(manifestPath, 'utf8'));
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
  };
}

/** True when the report describes a tree that disagrees with the manifest. */
export function isFailure(report) {
  return report.missing.length > 0 || report.drifted.length > 0;
}

/**
 * The message a developer or agent actually reads. It has to answer three
 * questions without a second command: what is wrong, why it is not the
 * branch's fault, and what to type.
 */
export function formatFailure(report) {
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
 * vitest `globalSetup`. Throwing here aborts the run before any test file is
 * collected, which is the whole value: one accurate message instead of 39
 * resolve errors pointing at files that are not the problem.
 */
export function setup() {
  const report = checkInstalledDeps();
  if (report.manifestMissing) return;
  if (isFailure(report)) throw new Error(formatFailure(report));
}

// Also runnable on its own — `npm run check:deps` — so CI and a human can ask
// the question without starting the suite.
if (process.argv[1] && fileURLToPath(import.meta.url) === process.argv[1]) {
  const report = checkInstalledDeps();
  if (report.manifestMissing) {
    console.error('check-node-modules: no package.json at', report.root);
    process.exit(1);
  }
  if (isFailure(report)) {
    console.error(formatFailure(report));
    process.exit(1);
  }
  console.log(
    `check-node-modules: ${report.checked} declared dependencies are installed and in range.`,
  );
}
