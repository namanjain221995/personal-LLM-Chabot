/**
 * The page Content-Security-Policy (2026-09-13), pure so it is unit-testable
 * without Next. middleware.ts mints one nonce per page request, puts the
 * policy on the REQUEST (Next reads the nonce from there and stamps it on every
 * script it renders) and on the RESPONSE (the browser enforces it).
 *
 * WHY A NONCE AND NOT 'unsafe-inline'. The App Router streams its payload in
 * inline <script> tags whose contents differ per request, so without a nonce
 * the only policy it tolerates allows every inline script — which is the one
 * thing a CSP is for. With a nonce plus 'strict-dynamic', a script runs only if
 * the server rendered it for this response or a script so rendered loaded it
 * (how the bundler fetches chunks). The price is that pages must render per
 * request: a prerendered page has no request to take a nonce from. The root
 * layout reads the request headers, which is what makes every page dynamic.
 * See node_modules/next/dist/docs/01-app/02-guides/content-security-policy.md.
 *
 * default-src 'self' covers fonts, media, workers, manifests and fetches
 * (every call goes through this origin's /api BFF). The overrides below exist
 * because something in this app needs each one:
 *
 * - style-src 'unsafe-inline': React server-renders `style={{…}}` as style
 *   ATTRIBUTES, which no nonce can cover, and mermaid and echarts write
 *   <style> elements and style attributes at runtime. Style injection cannot
 *   run code; scripts stay nonce-only.
 * - img-src data: blob:: composer thumbnails are data URLs; attachment and
 *   artifact page previews are object URLs. Remote images are NOT allowed, so
 *   an image URL inside rendered model output (a classic exfiltration beacon)
 *   does not load.
 * - object-src blob: and frame-src blob:: the PDF preview is an <object> over
 *   an object URL, and Chrome renders a PDF there in a nested frame, which
 *   frame-src governs (verified in Chrome 151: without it the preview is
 *   refused). Nothing else is ever embedded or framed.
 * - frame-ancestors 'none': the header form of X-Frame-Options: DENY.
 * - base-uri / form-action 'self': a stray <base> or <form> cannot retarget
 *   relative URLs or submissions off-origin.
 *
 * DELIBERATELY ABSENT: upgrade-insecure-requests. This frontend is also used
 * over plain http inside the network (docs/AUTH.md, the large-upload path),
 * and the directive rewrites SAME-origin http subresources to https, which
 * would break every script and API call on that path.
 */

/** 18 random bytes → 24 base64 characters, no padding: 144 bits. */
const NONCE_BYTES = 18;

/** A fresh, unpredictable nonce. Web Crypto, so it runs in either runtime. */
export function createNonce(): string {
  const bytes = new Uint8Array(NONCE_BYTES);
  crypto.getRandomValues(bytes);
  let binary = '';
  for (const b of bytes) binary += String.fromCharCode(b);
  return btoa(binary);
}

export interface CspOptions {
  /**
   * `next dev` only: React reconstructs server error stacks with eval, so the
   * dev server needs 'unsafe-eval'. A production build never does.
   */
  dev?: boolean;
}

/** The policy for one page response, as a single header value. */
export function contentSecurityPolicy(nonce: string, options: CspOptions = {}): string {
  if (!/^[A-Za-z0-9+/]+={0,2}$/.test(nonce)) {
    // A nonce is interpolated into a header; anything else is a bug upstream.
    throw new Error('invalid CSP nonce');
  }
  const directives: [string, ...string[]][] = [
    ['default-src', "'self'"],
    [
      'script-src',
      "'self'",
      `'nonce-${nonce}'`,
      "'strict-dynamic'",
      ...(options.dev ? ["'unsafe-eval'"] : []),
    ],
    ['style-src', "'self'", "'unsafe-inline'"],
    ['img-src', "'self'", 'data:', 'blob:'],
    ['object-src', 'blob:'],
    ['frame-src', 'blob:'],
    ['frame-ancestors', "'none'"],
    ['base-uri', "'self'"],
    ['form-action', "'self'"],
  ];
  return directives.map((parts) => parts.join(' ')).join('; ');
}

export type CspHeaderName = 'Content-Security-Policy' | 'Content-Security-Policy-Report-Only';

/**
 * Which response header carries the policy. `CSP_REPORT_ONLY` is a runtime
 * escape hatch, not a rollout stage: if a deployment ever shows a page broken
 * by the policy, setting it makes browsers report instead of block until the
 * cause is fixed, without a rebuild. Parsed like the orchestrator's booleans
 * (1/true/yes/on); anything else, unset included, enforces.
 */
export function cspHeaderName(reportOnly: string | undefined): CspHeaderName {
  const value = (reportOnly ?? '').trim().toLowerCase();
  return ['1', 'true', 'yes', 'on'].includes(value)
    ? 'Content-Security-Policy-Report-Only'
    : 'Content-Security-Policy';
}
