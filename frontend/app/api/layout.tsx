import type { Metadata } from 'next';
import type { ReactNode } from 'react';
import { notFound, redirect } from 'next/navigation';

import { ErrorPanel } from '@/components/admin/ui';
import { consoleSession } from '@/components/devplatform/server';

/**
 * The developer console's segment: the gate, and nothing else.
 *
 * WHY THE GATE IS HERE AND NOT ONLY ON THE PAGE. A layout wraps every page
 * ever added to this segment, so a second console page created later inherits
 * the refusal instead of having to remember it. The page repeats the
 * resolution because it needs the identity to render with — `cache()` makes
 * the two one upstream call (components/devplatform/server.ts).
 *
 * ROUTE HANDLERS ARE UNAFFECTED, and so is every existing /api/* endpoint.
 * Next's own reference is explicit on both halves (node_modules/next/dist/
 * docs/01-app/01-getting-started/15-route-handlers.md, "Route Resolution"):
 * routes "do not participate in layouts", and a conflict exists only between a
 * `route.js` and a `page.js` AT THE SAME ROUTE — the table there lists
 * `app/page.js` beside `app/api/route.js` as valid. There is no
 * app/api/route.ts in this application, so /api/chat, /api/auth/* and the rest
 * keep answering exactly as before; this file governs the PAGE at /api and any
 * page beneath it, and nothing else.
 *
 * The middleware already bounces a signed-out visitor from /api at the edge
 * (the matcher's trailing slash, fixed 2026-09-12), but that check knows only
 * whether a cookie is PRESENT. A forged or expired cookie, and every member
 * with a perfectly valid session and no `api.console.access`, is refused here
 * — server-side, before a byte of console markup exists. CONTRACT §17: never
 * by hiding a menu item.
 */

export const dynamic = 'force-dynamic';

export const metadata: Metadata = {
  title: 'Developer platform · TechSara',
  description: 'API keys, usage and documentation for the TechSara API.',
  // A capability-gated console has no business in a search index, and the
  // refusal below means a crawler could never see it anyway — this states it.
  robots: { index: false, follow: false },
};

export default async function DeveloperConsoleLayout({
  children,
}: {
  children: ReactNode;
}) {
  const session = await consoleSession();

  if (session.state === 'signed-out') {
    redirect('/login');
  }
  if (session.state === 'refused') {
    // 404, not 403 (CONTRACT §6): a member is told this page does not exist,
    // which is the same answer the admin surface gives and the same answer the
    // orchestrator gives every console endpoint.
    notFound();
  }
  if (session.state === 'unavailable') {
    // NOT a refusal. Saying "not found" because a container was restarting
    // would tell an admin their console had been taken away.
    return (
      <div className="flex h-dvh items-center justify-center bg-bg px-4 text-ink">
        <div className="w-full max-w-sm">
          <ErrorPanel message={session.reason} />
        </div>
      </div>
    );
  }

  return <>{children}</>;
}
