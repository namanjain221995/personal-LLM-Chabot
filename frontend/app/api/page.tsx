import { Suspense } from 'react';
import { notFound, redirect } from 'next/navigation';

import { ErrorPanel } from '@/components/admin/ui';
import { Loader } from '@/components/Loader';
import { ConsoleShell } from '@/components/devplatform/ConsoleShell';
import { consoleSession } from '@/components/devplatform/server';

/**
 * /api — the developer console (CONTRACT §17).
 *
 * A SERVER component. It resolves the session before rendering anything and
 * refuses without `api.console.access`; the layout above it made the same
 * decision, and `cache()` means both read one upstream answer. The repetition
 * is on purpose: this page must be safe to reason about on its own, and a page
 * whose only protection is a file one directory up is one refactor away from
 * having none.
 *
 * The identity is handed to the shell as a PROP rather than fetched again in
 * the browser. A client-side re-probe would be a second answer to a settled
 * question, and the moment two gates disagree it is the weaker one that ships.
 *
 * The Suspense boundary is required: the shell reads the section out of
 * `?tab=` with `useSearchParams`, which suspends, and a `useSearchParams`
 * without a boundary is a build error in Next rather than a runtime surprise.
 */

export const dynamic = 'force-dynamic';

export default async function DeveloperConsolePage() {
  const session = await consoleSession();
  if (session.state === 'signed-out') redirect('/login');
  if (session.state === 'refused') notFound();
  if (session.state === 'unavailable') {
    // The same honest answer the layout gives: "could not be checked" is not
    // "does not exist", and only one of those is true when the orchestrator is
    // restarting.
    return (
      <div className="flex h-dvh items-center justify-center bg-bg px-4 text-ink">
        <div className="w-full max-w-sm">
          <ErrorPanel message={session.reason} />
        </div>
      </div>
    );
  }

  return (
    <Suspense
      fallback={
        <div className="flex h-dvh items-center justify-center bg-bg text-muted">
          <Loader size={40} />
        </div>
      }
    >
      <ConsoleShell me={session.me} />
    </Suspense>
  );
}
