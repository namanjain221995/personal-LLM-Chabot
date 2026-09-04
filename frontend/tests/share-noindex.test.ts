/**
 * A shared conversation must not end up in a search index.
 *
 * Two mechanisms, and they only work together. The page's Next metadata
 * emits <meta name="robots" content="noindex">; next.config.mjs sends the
 * same instruction as a header for whatever never parses the head.
 *
 * The third mechanism — robots.txt Disallow — is deliberately ABSENT, and
 * this file asserts its absence. A disallowed URL is never fetched, so the
 * noindex is never read, and a link somebody posts publicly can still be
 * indexed as a bare URL. Letting crawlers in to be told "no" is the only
 * combination that keeps these pages out.
 *
 * Both are read from the real files: the failure this guards is a config
 * drifting away from the feature, which a mock would hide.
 */
import { existsSync, readFileSync } from 'node:fs';

import { describe, expect, it } from 'vitest';

const root = process.cwd();

function shareHeaderBlock(): string {
  const config = readFileSync(`${root}/next.config.mjs`, 'utf8');
  const at = config.indexOf("source: '/share/:path*'");
  expect(at, 'next.config.mjs has no header rule for /share/').toBeGreaterThan(-1);
  return config.slice(at, config.indexOf('},\n    ];', at));
}

describe('the public share page', () => {
  it('is served noindex by header as well as by meta tag', () => {
    const block = shareHeaderBlock();
    expect(block).toContain('X-Robots-Tag');
    expect(block).toMatch(/noindex/);
    expect(block).toMatch(/nofollow/);
  });

  it('sends no referrer, so the secret link never reaches a cited site', () => {
    expect(shareHeaderBlock()).toMatch(/'Referrer-Policy'[^}]*'no-referrer'/);
  });

  it('declares noindex in the page metadata too', () => {
    const page = readFileSync(`${root}/app/share/[token]/page.tsx`, 'utf8');
    expect(page).toMatch(/robots:\s*\{[^}]*index:\s*false/s);
    expect(page).toContain("referrer: 'no-referrer'");
  });

  it('never derives its title or description from the conversation', () => {
    // Open Graph on a link a person pastes into Slack is the single most
    // likely place a private first message would be published.
    const page = readFileSync(`${root}/app/share/[token]/page.tsx`, 'utf8');
    expect(page).toContain("title: 'Shared conversation'");
    expect(page).not.toMatch(/generateMetadata/);
  });

  it('does not hide /share/ from crawlers in robots.txt', () => {
    for (const candidate of [
      `${root}/app/robots.ts`,
      `${root}/app/robots.txt`,
      `${root}/public/robots.txt`,
    ]) {
      if (!existsSync(candidate)) continue;
      const body = readFileSync(candidate, 'utf8');
      expect(body, `${candidate} disallows /share/ — see this file's header`)
        .not.toMatch(/Disallow:\s*\/share/i);
    }
  });
});
