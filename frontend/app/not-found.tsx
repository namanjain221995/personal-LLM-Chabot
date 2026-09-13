import Link from 'next/link';

import { StoredTheme } from '@/components/StoredTheme';
import { TechSaraMark } from '@/components/TechSaraMark';

/**
 * The application's 404.
 *
 * Every URL no route answers, and every `notFound()` thrown outside a segment
 * with its own not-found (the developer console refusing a member, an unknown
 * /admin page), used to land on Next's bare "404 This page could not be
 * found." — unstyled, the same page in both themes, and no way back
 * (responsive audit, 2026-09-13).
 *
 * It deliberately says nothing about WHY. The console's refusal is a 404 on
 * purpose (CONTRACT §6: it must not disclose its own existence to someone who
 * may not use it), so this page reads the same for a member at /api as for a
 * typo, and it does not link to the console.
 *
 * Its tab icon comes from the root layout's metadata like every page's. On a
 * not-found render Chrome sometimes asks for /favicon.ico anyway (measured:
 * 4 of 8 loads of /api as a member, 1 of 8 of /docs/<unknown>), so
 * public/favicon.ico exists: that request is answered instead of adding a
 * second red 404 to the console.
 *
 * <StoredTheme />: on a thrown notFound() Next renders this in the browser,
 * where the root layout's theme script never runs (components/StoredTheme).
 */
export default function NotFound() {
  return (
    <main className="flex min-h-dvh items-center justify-center bg-bg px-4 py-12 text-ink">
      <StoredTheme />
      <div className="w-full max-w-md">
        <TechSaraMark size={40} />
        <p className="mt-6 text-sm font-medium text-muted">404</p>
        <h1 className="mt-2 text-2xl font-semibold tracking-tight [overflow-wrap:anywhere]">
          This page could not be found
        </h1>
        <p className="mt-3 text-sm leading-relaxed text-muted">
          The link may be out of date, or the address mistyped.
        </p>
        <nav aria-label="Ways back" className="mt-8 flex flex-col gap-2 sm:flex-row">
          <Link
            href="/"
            className="inline-flex min-h-10 items-center justify-center rounded-ts bg-accent-strong px-4 text-sm font-semibold text-white no-underline transition-all duration-ts hover:brightness-125 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-bg"
          >
            Back to chat
          </Link>
          <Link
            href="/docs"
            className="inline-flex min-h-10 items-center justify-center rounded-ts border border-border bg-surface px-4 text-sm font-medium text-ink no-underline transition-colors duration-ts hover:bg-surface-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent focus-visible:ring-offset-2 focus-visible:ring-offset-bg"
          >
            API documentation
          </Link>
        </nav>
      </div>
    </main>
  );
}
