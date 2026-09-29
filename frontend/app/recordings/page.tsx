import type { Metadata } from 'next';

import { RecordingsPage } from '@/components/recordings/RecordingsPage';

const APP_NAME = process.env.NEXT_PUBLIC_APP_NAME ?? 'TechSara AI';

export const metadata: Metadata = {
  title: `Recordings · ${APP_NAME}`,
};

/**
 * The person's stored voice recordings (voice security review, item 6: a
 * stored recording its owner cannot find or delete is retention without
 * consent). Signed-in only: middleware.ts sends a request without the session
 * cookie to /login, and every read and delete behind the page is checked for
 * ownership by the orchestrator.
 */
export default function Page() {
  return <RecordingsPage />;
}
