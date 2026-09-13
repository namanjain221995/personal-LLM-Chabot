import type { Metadata } from 'next';
import type { ReactNode } from 'react';

import { DocsShell } from '@/components/docs/DocsShell';

/**
 * /docs — the TechSara developer documentation (CONTRACT §17).
 *
 * Public, at any depth (owner decision, 2026-09-13): lib/auth.ts lets a
 * signed-out visitor read /docs and everything under it. Nothing here reads a
 * session. The console at /api is unaffected — signed in, and it needs
 * `api.console.access`. Search engines may index it now that it is public.
 */
export const metadata: Metadata = {
  title: {
    default: 'TechSara API documentation',
    template: '%s · TechSara API',
  },
  description:
    'Build against the TechSara developer platform: the Responses API, ' +
    'streaming, background responses, webhooks, errors and rate limits.',
  robots: { index: true, follow: true },
};

export default function DocsLayout({ children }: { children: ReactNode }) {
  return <DocsShell>{children}</DocsShell>;
}
