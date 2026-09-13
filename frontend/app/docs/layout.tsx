import type { Metadata } from 'next';
import type { ReactNode } from 'react';

import { DocsShell } from '@/components/docs/DocsShell';

/**
 * /docs — the TechSara developer documentation (CONTRACT §17).
 *
 * Signed-in people only, and that is decided at the edge, not here: `/docs`
 * is deliberately absent from the public-page list in lib/auth.ts, so a
 * signed-out visitor is redirected to /login at any depth. Reading the
 * documentation needs no capability — unlike the console at /api, which needs
 * `api.console.access`.
 *
 * `noindex` because the site is not public. If it is ever published, that is
 * a decision with its own review (the note in lib/auth.ts says what would
 * have to change), and this line is one of the things it would change.
 */
export const metadata: Metadata = {
  title: {
    default: 'TechSara API documentation',
    template: '%s · TechSara API',
  },
  description:
    'Build against the TechSara developer platform: the Responses API, ' +
    'streaming, background responses, webhooks, errors and rate limits.',
  robots: { index: false, follow: false },
};

export default function DocsLayout({ children }: { children: ReactNode }) {
  return <DocsShell>{children}</DocsShell>;
}
