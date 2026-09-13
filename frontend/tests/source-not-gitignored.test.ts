/**
 * No frontend source file may be ignored by git.
 *
 * An unanchored directory rule in the root .gitignore (`reports/`,
 * `uploads/`, and until 2026-09-13 `models/`) matches a folder of that name at
 * ANY depth — including a Next.js route segment. The file then exists on its
 * author's disk, builds and passes locally, and is silently never committed:
 * the admin rail linked to /admin/analytics/models for nine days while every
 * build made from git answered 404. `git check-ignore` applies the real rules
 * (anchors, negations, nested ignore files), so this asks git itself.
 */
import { execFileSync } from 'node:child_process';
import { readdirSync } from 'node:fs';
import { join, relative } from 'node:path';
import { fileURLToPath } from 'node:url';
import { describe, expect, it } from 'vitest';

const FRONTEND = fileURLToPath(new URL('..', import.meta.url));
const SOURCE_DIRS = ['app', 'components', 'lib', 'content', 'public', 'tests'];

function filesUnder(dir: string): string[] {
  return readdirSync(dir, { withFileTypes: true }).flatMap((entry) => {
    const path = join(dir, entry.name);
    return entry.isDirectory() ? filesUnder(path) : [path];
  });
}

/** The paths git would ignore, or [] — check-ignore exits 1 when none match. */
function ignored(paths: string[]): string[] {
  try {
    const out = execFileSync('git', ['check-ignore', '--no-index', '--stdin'], {
      cwd: FRONTEND,
      input: paths.join('\n'),
      encoding: 'utf8',
    });
    return out.split('\n').filter(Boolean);
  } catch (error) {
    const status = (error as { status?: number }).status;
    if (status === 1) return [];
    throw error;
  }
}

describe('the frontend source tree and the root .gitignore', () => {
  it('leaves every source file, route folders included, visible to git', () => {
    const files = SOURCE_DIRS.flatMap((dir) => filesUnder(join(FRONTEND, dir))).map((path) =>
      relative(FRONTEND, path),
    );
    expect(files).toContain(join('app', 'admin', 'analytics', 'models', 'page.tsx'));
    expect(ignored(files)).toEqual([]);
  });

  it('still ignores model weights at the repository root', () => {
    expect(ignored(['../models/Qwen/config.json', '../models/weights.safetensors'])).toEqual([
      '../models/Qwen/config.json',
      '../models/weights.safetensors',
    ]);
  });
});
