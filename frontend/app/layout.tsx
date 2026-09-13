import type { Metadata } from 'next';
import { headers } from 'next/headers';
import type { ReactNode } from 'react';

// Self-hosted fonts via @fontsource — zero runtime CDN requests (§9).
import '@fontsource/ibm-plex-sans/400.css';
import '@fontsource/ibm-plex-sans/500.css';
import '@fontsource/ibm-plex-sans/600.css';
import '@fontsource/ibm-plex-sans/700.css';
import '@fontsource/jetbrains-mono/400.css';
import '@fontsource/jetbrains-mono/500.css';

import './globals.css';
import { Providers } from '@/components/Providers';

const APP_NAME = process.env.NEXT_PUBLIC_APP_NAME ?? 'TechSara AI';

export const metadata: Metadata = {
  title: APP_NAME,
  description:
    'Local AI analysis over synced Salesforce data — nothing leaves this machine.',
  icons: [
    { rel: 'icon', url: '/favicon.png', type: 'image/png' },
    { rel: 'apple-touch-icon', url: '/apple-touch-icon.png' },
  ],
};

/**
 * Applied before hydration so the correct theme paints first.
 * Dark is the primary theme (§9); an explicit user choice wins.
 */
const themeInit = `(function(){var t='dark';try{var s=localStorage.getItem('techsara.theme');if(s==='light'||s==='dark')t=s;}catch(e){}document.documentElement.classList.add(t);document.documentElement.style.colorScheme=t;})();`;

/**
 * THE NONCE, AND WHY EVERY PAGE NOW RENDERS PER REQUEST (2026-09-13).
 *
 * middleware.ts sends each page a Content-Security-Policy that runs only
 * scripts carrying that response's nonce (lib/csp.ts). Next stamps the nonce
 * on its own scripts; the theme script above is ours, so it takes the nonce
 * from `x-nonce` here. Reading the request headers is also what opts every
 * page out of build-time prerendering, which a nonce requires: a page rendered
 * at build time has no request to take one from, and its scripts would all be
 * refused.
 */
export default async function RootLayout({ children }: { children: ReactNode }) {
  const nonce = (await headers()).get('x-nonce') ?? undefined;
  return (
    <html lang="en" suppressHydrationWarning>
      <head>
        <script nonce={nonce} dangerouslySetInnerHTML={{ __html: themeInit }} />
      </head>
      <body>
        <Providers>{children}</Providers>
      </body>
    </html>
  );
}
