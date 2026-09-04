import type { Metadata } from 'next';

import { SharedConversation } from '@/components/SharedConversation';

/**
 * /share/<token> — a read-only conversation, for anyone holding the link.
 *
 * The only page in this application that renders without a session. It has no
 * sidebar, no composer, no model picker and no history: not hidden versions of
 * those, none at all, because a page that merely hides its controls is one CSS
 * mistake away from offering them.
 *
 * The token stays on the CLIENT. This is a client-rendered page that fetches
 * /api/public/shares/<token> from the browser, rather than a server component
 * that would put the secret into the server's request path — and therefore
 * into access logs, traces and error reports. The same reasoning is why the
 * orchestrator only ever logs the addressable half.
 */

export const dynamic = 'force-dynamic';

/**
 * Deliberately generic, and deliberately not derived from the conversation.
 * A title or description built from the first message would publish that
 * message to every link preview, chat unfurl and crawler that touches the
 * URL — the one place a shared link is most likely to be pasted.
 */
export const metadata: Metadata = {
  title: 'Shared conversation',
  description: 'A read-only conversation shared from TechSara.',
  robots: {
    index: false,
    follow: false,
    nocache: true,
    googleBot: { index: false, follow: false, noimageindex: true },
  },
  referrer: 'no-referrer',
  openGraph: {
    title: 'Shared conversation',
    description: 'A read-only conversation shared from TechSara.',
  },
};

export default async function SharePage({
  params,
}: {
  params: Promise<{ token: string }>;
}) {
  const { token } = await params;
  return <SharedConversation token={token} />;
}
