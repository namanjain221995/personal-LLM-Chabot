import type { Metadata } from 'next';
import { Suspense } from 'react';

import { MyFilesPage } from '@/components/myfiles/MyFilesPage';

const APP_NAME = process.env.NEXT_PUBLIC_APP_NAME ?? 'TechSara AI';

export const metadata: Metadata = {
  title: `My files · ${APP_NAME}`,
};

/**
 * Everything the person uploaded, across every chat, with their voice
 * recordings (2026-09-30). Signed-in only: middleware.ts sends a request
 * without the session cookie to /login, and the orchestrator scopes every
 * row, file and delete behind the page to the session's own account.
 *
 * The Suspense boundary is required: the page reads its filters from the
 * URL with useSearchParams, which suspends, and without a boundary Next
 * fails the production build (node_modules/next/dist/docs/01-app/
 * 03-api-reference/04-functions/use-search-params.md, "Prerendering").
 */
export default function Page() {
  return (
    <Suspense fallback={<div className="min-h-dvh bg-bg" aria-busy="true" />}>
      <MyFilesPage />
    </Suspense>
  );
}
