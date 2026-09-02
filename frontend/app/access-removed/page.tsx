import type { Metadata } from 'next';

import { AccessRemoved } from '@/components/auth/AccessRemoved';
import { AuthLayout } from '@/components/auth/AuthLayout';

const APP_NAME = process.env.NEXT_PUBLIC_APP_NAME ?? 'TechSara AI';

export const metadata: Metadata = {
  title: `Access removed · ${APP_NAME}`,
};

/**
 * Public page (see PUBLIC_PAGES in lib/auth.ts): the person arriving here
 * has no session any more. Everything it shows rides in the query string,
 * written by handleSessionEnd from /auth/me's explanation.
 */
export default function AccessRemovedPage() {
  return (
    <AuthLayout>
      <AccessRemoved />
    </AuthLayout>
  );
}
