/** @type {import('next').NextConfig} */
const nextConfig = {
  output: 'standalone',
  reactStrictMode: true,
  poweredByHeader: false,
  // Security headers (2026-09-01, with login). Deliberately NOT a full
  // Content-Security-Policy: the theme-init inline script and Next's own
  // hydration scripts would need nonce plumbing, and a broken CSP fails
  // silently per-resource — the risk/benefit is wrong for a LAN deployment.
  // These four are safe everywhere:
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
    ];
  },
};

export default nextConfig;
