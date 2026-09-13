/**
 * The page Content-Security-Policy and the edge headers (2026-09-13).
 *
 * Pinned here:
 *  1. the policy: nonce-only scripts ('strict-dynamic', no 'unsafe-inline',
 *     no 'unsafe-eval' outside `next dev`), and each non-default source the app
 *     was verified to need in Chrome — nothing broader;
 *  2. the nonce: fresh per call, unpredictable-length, header-safe;
 *  3. the middleware: a page that gets through carries the policy on the
 *     response AND on the request Next renders from, with the request's nonce
 *     and policy headers replaced rather than trusted; a redirect needs none;
 *  4. the report-only escape hatch parses like the orchestrator's booleans;
 *  5. next.config: the static headers stay, /docs is cacheable briefly and
 *     publicly instead of for a year, and no static CSP competes with the
 *     nonce policy;
 *  6. the root layout hands the nonce to its own inline script.
 */
import { readFileSync } from 'node:fs';

import { NextRequest } from 'next/server';
import { afterEach, describe, expect, it, vi } from 'vitest';

import { contentSecurityPolicy, createNonce, cspHeaderName } from '../lib/csp';
import { middleware } from '../middleware';
import nextConfig from '../next.config.mjs';

function directives(policy: string): Map<string, string[]> {
  return new Map(
    policy.split(';').map((d) => {
      const [name, ...values] = d.trim().split(/\s+/);
      return [name, values] as [string, string[]];
    }),
  );
}

// ---------------------------------------------------------------------------
// 1. The policy
// ---------------------------------------------------------------------------

describe('the page policy', () => {
  const nonce = 'AAAAAAAAAAAAAAAAAAAAAAAA';
  const policy = directives(contentSecurityPolicy(nonce));

  it('runs only nonced scripts and what they load', () => {
    expect(policy.get('script-src')).toEqual(["'self'", `'nonce-${nonce}'`, "'strict-dynamic'"]);
  });

  it('never allows inline or eval scripts in a production build', () => {
    const script = policy.get('script-src') ?? [];
    expect(script).not.toContain("'unsafe-inline'");
    expect(script).not.toContain("'unsafe-eval'");
    expect(policy.get('default-src')).toEqual(["'self'"]);
  });

  it("adds 'unsafe-eval' for next dev only, which React's dev stacks need", () => {
    const dev = directives(contentSecurityPolicy(nonce, { dev: true }));
    expect(dev.get('script-src')).toContain("'unsafe-eval'");
    expect(dev.get('script-src')).not.toContain("'unsafe-inline'");
  });

  it('forbids framing, <base> retargeting and off-origin form posts', () => {
    expect(policy.get('frame-ancestors')).toEqual(["'none'"]);
    expect(policy.get('base-uri')).toEqual(["'self'"]);
    expect(policy.get('form-action')).toEqual(["'self'"]);
  });

  it('allows exactly the extra sources the app needs, and no remote host anywhere', () => {
    expect(policy.get('img-src')).toEqual(["'self'", 'data:', 'blob:']);
    expect(policy.get('object-src')).toEqual(['blob:']);
    expect(policy.get('frame-src')).toEqual(['blob:']);
    expect(policy.get('style-src')).toEqual(["'self'", "'unsafe-inline'"]);
    for (const [name, values] of policy) {
      for (const v of values) {
        expect([name, v]).not.toEqual([name, expect.stringMatching(/^(https?:|\*|wss?:)/)]);
      }
    }
    // connect-src, font-src, media-src and worker-src fall back to 'self'.
    for (const name of ['connect-src', 'font-src', 'media-src', 'worker-src']) {
      expect(policy.has(name)).toBe(false);
    }
  });

  it('does not upgrade insecure requests: the in-network http path must keep working', () => {
    expect(policy.has('upgrade-insecure-requests')).toBe(false);
  });

  it('refuses a nonce that could break out of the header', () => {
    expect(() => contentSecurityPolicy("abc'; script-src *")).toThrow();
    expect(() => contentSecurityPolicy('')).toThrow();
    expect(() => contentSecurityPolicy('abc def')).toThrow();
  });
});

// ---------------------------------------------------------------------------
// 2. The nonce
// ---------------------------------------------------------------------------

describe('the nonce', () => {
  it('is 24 base64 characters (144 random bits) and different every time', () => {
    const seen = new Set<string>();
    for (let i = 0; i < 200; i += 1) {
      const n = createNonce();
      expect(n).toMatch(/^[A-Za-z0-9+/]{24}$/);
      seen.add(n);
    }
    expect(seen.size).toBe(200);
  });

  it('is accepted by the pattern Next extracts it with', () => {
    // node_modules/next/dist/server/app-render/get-script-nonce-from-header.js
    const NEXT = /^'nonce-([A-Za-z0-9+/_-]+={0,2})'$/;
    const n = createNonce();
    const source = contentSecurityPolicy(n).split(';')[1].trim().split(/\s+/)[2];
    expect(source.match(NEXT)?.[1]).toBe(n);
  });
});

// ---------------------------------------------------------------------------
// 3. The middleware
// ---------------------------------------------------------------------------

function run(path: string, init: { cookie?: boolean; headers?: Record<string, string> } = {}) {
  const headers = new Headers(init.headers);
  if (init.cookie) headers.set('cookie', 'ts_session=abc');
  return middleware(new NextRequest(`http://localhost:3000${path}`, { headers }));
}

describe('the middleware', () => {
  afterEach(() => {
    vi.unstubAllEnvs();
  });

  it('sends a signed-out visitor at a dotted admin path to sign-in, not the page shell', () => {
    for (const path of ['/admin/members/1.x', '/admin/members/1.x/conversations/2.y', '/api.json']) {
      const res = run(path);
      expect([path, res.status]).toEqual([path, 307]);
      expect(res.headers.get('location')).toBe('http://localhost:3000/login');
    }
  });

  it('puts the policy on a page response, with the same nonce on the request Next renders', () => {
    const res = run('/login');
    expect(res.headers.get('x-middleware-next')).toBe('1');
    const policy = res.headers.get('content-security-policy') ?? '';
    const nonce = /'nonce-([^']+)'/.exec(policy)?.[1];
    expect(nonce).toBeTruthy();
    expect(res.headers.get('x-middleware-request-x-nonce')).toBe(nonce);
    expect(res.headers.get('x-middleware-request-content-security-policy')).toBe(policy);
    // The nonce reaches the render as a REQUEST header only; it is not echoed
    // to the browser as a header of its own.
    expect(res.headers.get('x-nonce')).toBeNull();
  });

  it('mints a different nonce for every request', () => {
    const a = run('/', { cookie: true }).headers.get('x-middleware-request-x-nonce');
    const b = run('/', { cookie: true }).headers.get('x-middleware-request-x-nonce');
    expect(a).toBeTruthy();
    expect(a).not.toBe(b);
  });

  it('replaces, never trusts, a nonce or policy the client sent', () => {
    const res = run('/admin', {
      cookie: true,
      headers: {
        'x-nonce': 'attacker',
        'content-security-policy': "script-src 'nonce-attacker'",
        'content-security-policy-report-only': "script-src 'nonce-attacker'",
      },
    });
    expect(res.headers.get('x-middleware-request-x-nonce')).not.toBe('attacker');
    expect(res.headers.get('x-middleware-request-content-security-policy')).not.toContain('attacker');
    // Next deletes every original request header the override list leaves out.
    const kept = (res.headers.get('x-middleware-override-headers') ?? '').split(',');
    expect(kept).toContain('content-security-policy');
    expect(kept).not.toContain('content-security-policy-report-only');
    expect(res.headers.get('x-middleware-request-content-security-policy-report-only')).toBeNull();
  });

  it('covers the public pages too: docs, share and sign-in', () => {
    for (const path of ['/docs', '/docs/guides/webhooks', '/share/abc.def', '/login']) {
      expect([path, run(path).headers.get('content-security-policy')]).toEqual([
        path,
        expect.stringContaining("'strict-dynamic'"),
      ]);
    }
  });

  it('switches to report-only when CSP_REPORT_ONLY is set, and enforces otherwise', () => {
    vi.stubEnv('CSP_REPORT_ONLY', 'true');
    const res = run('/login');
    expect(res.headers.get('content-security-policy')).toBeNull();
    expect(res.headers.get('content-security-policy-report-only')).toContain("'strict-dynamic'");
    // Next still gets a nonce to stamp, so the scripts keep running either way.
    expect(res.headers.get('x-middleware-request-content-security-policy')).toContain("'nonce-");
    vi.unstubAllEnvs();
    expect(run('/login').headers.get('content-security-policy')).toContain("'strict-dynamic'");
  });
});

// ---------------------------------------------------------------------------
// 4. The report-only switch
// ---------------------------------------------------------------------------

describe('cspHeaderName', () => {
  it.each(['1', 'true', 'TRUE', ' yes ', 'on'])('%j means report-only', (v) => {
    expect(cspHeaderName(v)).toBe('Content-Security-Policy-Report-Only');
  });

  it.each([undefined, '', '0', 'false', 'no', 'off', 'report-only', 'enforce'])(
    '%j enforces',
    (v) => {
      expect(cspHeaderName(v)).toBe('Content-Security-Policy');
    },
  );
});

// ---------------------------------------------------------------------------
// 5. next.config headers
// ---------------------------------------------------------------------------

type HeaderRule = { source: string; headers: { key: string; value: string }[] };

async function rules(): Promise<HeaderRule[]> {
  const headers = (nextConfig as { headers: () => Promise<HeaderRule[]> }).headers;
  return headers();
}

describe('next.config headers', () => {
  it('keeps nosniff, framing denial and the referrer policy on every path', async () => {
    const all = (await rules()).find((r) => r.source === '/:path*');
    const get = (k: string) => all?.headers.find((h) => h.key === k)?.value;
    expect(get('X-Content-Type-Options')).toBe('nosniff');
    expect(get('X-Frame-Options')).toBe('DENY');
    expect(get('Referrer-Policy')).toBe('strict-origin-when-cross-origin');
  });

  it('gives /docs a short public lifetime with revalidation, not a year', async () => {
    const docs = (await rules()).find((r) => r.source === '/docs/:path*');
    const cc = docs?.headers.find((h) => h.key === 'Cache-Control')?.value ?? '';
    expect(cc).toMatch(/\bpublic\b/);
    expect(cc).toMatch(/\bmust-revalidate\b/);
    const maxAge = Number(/max-age=(\d+)/.exec(cc)?.[1]);
    expect(maxAge).toBeGreaterThan(0);
    expect(maxAge).toBeLessThanOrEqual(3600);
    expect(cc).not.toMatch(/s-maxage|immutable/);
  });

  it('sets no static Content-Security-Policy that would compete with the nonce policy', async () => {
    for (const rule of await rules()) {
      for (const h of rule.headers) {
        expect([rule.source, h.key.toLowerCase()]).not.toEqual([
          rule.source,
          expect.stringMatching(/^content-security-policy/),
        ]);
      }
    }
  });
});

// ---------------------------------------------------------------------------
// 6. The root layout's own inline script
// ---------------------------------------------------------------------------

describe('the root layout', () => {
  it('stamps the request nonce on the theme script, which the policy would refuse otherwise', () => {
    const layout = readFileSync(`${process.cwd()}/app/layout.tsx`, 'utf8');
    expect(layout).toMatch(/from 'next\/headers'/);
    expect(layout).toMatch(/\.get\('x-nonce'\)/);
    expect(layout).toMatch(/<script nonce=\{nonce\} dangerouslySetInnerHTML=\{\{ __html: themeInit \}\} \/>/);
    // Every inline script in the layout carries it.
    const inline = layout.match(/<script\b[^>]*dangerouslySetInnerHTML/g) ?? [];
    expect(inline.length).toBeGreaterThan(0);
    for (const tag of inline) expect(tag).toContain('nonce={nonce}');
  });
});
