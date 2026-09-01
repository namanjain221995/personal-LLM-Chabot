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
  // - permissions: this app uses no camera/mic/geolocation
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
            value: 'camera=(), microphone=(), geolocation=()',
          },
        ],
      },
    ];
  },
};

export default nextConfig;
