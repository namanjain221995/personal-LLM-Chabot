/**
 * The edge gate's static-asset allowlist (2026-09-13).
 *
 * The middleware used to skip every path containing a dot, in the matcher and
 * again in authRedirect, so a signed-out visitor received the shell of any
 * gated page whose dynamic segment held a dot. An asset is now exactly what
 * this app serves unauthenticated so /login can render — an image or video file
 * at the root or under /illustrator/, or a well-known root file — and the
 * matcher spells the same rule as lib/auth.ts isStaticAssetPath. These tests
 * pin both halves and hold them together.
 */
import fs from 'node:fs';
import path from 'node:path';

import { describe, expect, it } from 'vitest';

import { authRedirect, isStaticAssetPath } from '../lib/auth';
import { config } from '../middleware';

/** The matcher as Next compiles it: anchored over the whole pathname. */
const matcher = new RegExp(`^${config.matcher[0]}$`);

/** Gated pages that carry a dot somewhere a dynamic segment allows one. */
const DOTTED_PAGES = [
  '/admin/members/1.x',
  '/admin/members/1.x/conversations/2.y',
  '/admin/x.y',
  '/admin/members/1.png',
  '/admin/members/1.json',
  '/admin/audit.csv',
  '/api.json',
  '/api.json/',
  '/docs.json',
  '/admin.',
  '/.well-known/anything',
  '/v1.2/report',
  '/login.php',
  '/index.html',
];

/** Encoded forms that must never look like a file to the gate. */
const ENCODED_TRICKS = [
  '/%2e',
  '/%2e%2e/admin',
  '/admin/%2e%2e/admin',
  '/admin%2Fmembers.png',
  '/admin/members/1%2Epng',
  '/%61dmin.png/x',
  '/illustrator/..%2Fadmin.png',
  '/illustrator%2F..%2Fadmin.webp',
];

/** Files that really exist under public/ or are well-known root files. */
const REAL_ASSETS = [
  '/favicon.ico',
  '/favicon.png',
  '/favicon.svg',
  '/apple-touch-icon.png',
  '/techsara-mark.png',
  '/techsara-logo.webp',
  '/loading.webm',
  '/loading-poster.png',
  '/illustrator/login.webp',
  '/illustrator/company-team.webp',
  '/robots.txt',
  '/sitemap.xml',
  '/manifest.json',
  '/manifest.webmanifest',
];

describe('a gated page with a dot in its path', () => {
  it.each(DOTTED_PAGES)('%s bounces a signed-out visitor to /login', (p) => {
    expect(authRedirect(p, false)).toBe('/login');
  });

  it.each(DOTTED_PAGES)('%s runs the middleware', (p) => {
    expect(matcher.test(p)).toBe(true);
  });

  it.each(DOTTED_PAGES)('%s is not an asset', (p) => {
    expect(isStaticAssetPath(p)).toBe(false);
  });
});

describe('an encoded path', () => {
  it.each(ENCODED_TRICKS)('%s is gated, never waved through as a file', (p) => {
    expect(isStaticAssetPath(p)).toBe(false);
    expect(authRedirect(p, false)).toBe('/login');
    expect(matcher.test(p)).toBe(true);
  });
});

describe('the real static files', () => {
  it.each(REAL_ASSETS)('%s loads without the gate, signed in or out', (p) => {
    expect(isStaticAssetPath(p)).toBe(true);
    expect(authRedirect(p, false)).toBeNull();
    expect(authRedirect(p, true)).toBeNull();
    expect(matcher.test(p)).toBe(false);
  });

  it('covers every file actually shipped in public/ that a signed-out page uses', () => {
    // The sign-in page renders the mark, the favicon and the illustrations; a
    // file added to public/ in a new directory must be added to the allowlist
    // on purpose, and this is where that shows up.
    const root = path.join(process.cwd(), 'public');
    const used = [
      ...fs.readdirSync(root).filter((f) => !fs.statSync(path.join(root, f)).isDirectory()),
      ...fs
        .readdirSync(path.join(root, 'illustrator'))
        .filter((f) => f.endsWith('.webp'))
        .map((f) => `illustrator/${f}`),
    ];
    expect(used.length).toBeGreaterThan(5);
    for (const f of used) expect([f, isStaticAssetPath(`/${f}`)]).toEqual([f, true]);
  });

  it('is case-sensitive, as Next applies the matcher and the files are served', () => {
    expect(isStaticAssetPath('/LOGO.PNG')).toBe(false);
    expect(matcher.test('/LOGO.PNG')).toBe(true);
    expect(authRedirect('/LOGO.PNG', false)).toBe('/login');
  });

  it('does not admit an asset extension below any other directory', () => {
    for (const p of ['/admin/logo.png', '/share/x/y.png', '/docs/x/y.svg', '/illustrator/sub/x.webp']) {
      expect(isStaticAssetPath(p)).toBe(false);
      expect(matcher.test(p)).toBe(true);
    }
  });
});

describe('the matcher and authRedirect agree', () => {
  it('on every path in the corpus: excluded by the matcher ⇔ an asset or an excluded namespace', () => {
    const corpus = [
      ...DOTTED_PAGES,
      ...ENCODED_TRICKS,
      ...REAL_ASSETS,
      '/',
      '/login',
      '/admin',
      '/api',
      '/api/chat',
      '/_next/static/chunks/x.js',
      '/v1',
      '/v1/models',
      '/docs',
      '/docs/quickstart',
      '/share/abc.def',
      '/LOGO.PNG',
    ];
    for (const p of corpus) {
      const namespace = p.startsWith('/api/') || p.startsWith('/_next/') || p === '/v1' || p.startsWith('/v1/');
      expect([p, !matcher.test(p)]).toEqual([p, namespace || isStaticAssetPath(p)]);
    }
  });

  it('keeps the three namespace exclusions exactly as they were', () => {
    expect(config.matcher[0].startsWith('/((?!api/|_next/|v1(?:/|$)|')).toBe(true);
    expect(matcher.test('/api')).toBe(true);
    expect(matcher.test('/apiary')).toBe(true);
    expect(matcher.test('/api/chat')).toBe(false);
    expect(matcher.test('/v1')).toBe(false);
    expect(matcher.test('/v1/')).toBe(false);
    expect(matcher.test('/v1x')).toBe(true);
    expect(matcher.test('/_next/image')).toBe(false);
  });
});
