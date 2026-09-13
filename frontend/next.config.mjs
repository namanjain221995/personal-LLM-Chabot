/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'standalone',
  reactStrictMode: true,
  poweredByHeader: false,
  // Security headers (2026-09-01, with login). The Content-Security-Policy is
  // NOT here: it carries a per-request nonce, so middleware.ts sets it on every
  // page (lib/csp.ts, 2026-09-13). A static header cannot hold a nonce, and a
  // static policy Next's inline scripts would tolerate has to allow every
  // inline script. These four are static and safe everywhere:
  // - nosniff: uploaded/report files must never be content-sniffed into HTML
  // - DENY framing: the chat must not be embeddable for click-jacking
  // - referrer: never leak conversation URLs off-origin
  // - permissions: camera and geolocation are denied outright; the
  //   MICROPHONE is allowed for this origin only, because the composer
  //   dictates through it (2026-09-04).
  //
  // `microphone=()` is an empty ALLOWLIST, not a default — it means "no
  // origin may use the microphone", and the browser then refuses
  // getUserMedia before it ever prompts. The refusal arrives as
  // NotAllowedError, indistinguishable from the user clicking Block, so the
  // UI said "access is blocked, allow it in your browser settings" and no
  // amount of allowing it in settings could have helped. `self` is the
  // narrowest value that works: this origin yes, every embed and third party
  // still no.
  async headers() {
    return [
      {
        source: '/:path*',
        headers: [
          { key: 'X-Content-Type-Options', value: 'nosniff' },
          { key: 'X-Frame-Options', value: 'DENY' },
          { key: 'Referrer-Policy', value: 'strict-origin-when-cross-origin' },
          {
            key: 'Permissions-Policy',
            value: 'camera=(), microphone=(self), geolocation=()',
          },
        ],
      },
      {
        // The public developer documentation (2026-09-13). A prerendered page
        // was sent with Next's build-time default, `s-maxage=31536000`, which
        // invites a shared cache to keep a stale copy for a year after the
        // docs change. Pages now render per request (the CSP nonce), and a
        // docs page is the same for every reader — no session is read — so a
        // short public lifetime with revalidation is both safe and cheap. A
        // cached copy replays its nonce to other readers for those minutes;
        // that matters only where a page reflects input, and these pages
        // render repository text and nothing a request supplies.
        // Next leaves a Cache-Control that is already set alone
        // (server/send-payload.js).
        source: '/docs/:path*',
        headers: [{ key: 'Cache-Control', value: 'public, max-age=300, must-revalidate' }],
      },
      {
        // The public share pages. The Next metadata on /share/[token] already
        // emits <meta name="robots" content="noindex">; this is the same
        // instruction as a header, which is what a crawler fetching a
        // non-HTML sub-resource (or one that never parses the head) sees.
        //
        // Note what is deliberately NOT done: robots.txt does not Disallow
        // /share/. A disallowed URL is never fetched, so the noindex is never
        // read — and a link somebody posts can then still be indexed as a
        // bare URL. Letting crawlers in to be told "no" is the only
        // combination that actually keeps these pages out of an index.
        //
        // Referrer-Policy is tightened from the site default: a share URL
        // carries its own secret, so it must not travel in the Referer header
        // to any site the shared conversation happens to cite.
        source: '/share/:path*',
        headers: [
          { key: 'X-Robots-Tag', value: 'noindex, nofollow, noarchive, nosnippet' },
          { key: 'Referrer-Policy', value: 'no-referrer' },
        ],
      },
    ];
  },
};

export default nextConfig;
