import type { Metadata } from 'next';

import {
  AccessRemoved,
  parseContact,
  type Contact,
} from '@/components/auth/AccessRemoved';
import { AuthLayout } from '@/components/auth/AuthLayout';

const APP_NAME = process.env.NEXT_PUBLIC_APP_NAME ?? 'TechSara AI';

export const metadata: Metadata = {
  title: `Access removed · ${APP_NAME}`,
};

type Search = { [key: string]: string | string[] | undefined };

function one(v: string | string[] | undefined): string {
  return Array.isArray(v) ? (v[0] ?? '') : (v ?? '');
}

function many(v: string | string[] | undefined): string[] {
  return Array.isArray(v) ? v : v ? [v] : [];
}

/**
 * Public page (see PUBLIC_PAGES in lib/auth.ts): the person arriving here
 * has no session any more. Everything it shows rides in the query string,
 * written by handleSessionEnd from /auth/me's explanation, and is rendered
 * on the server so the explanation is in the HTML itself.
 */
export default async function AccessRemovedPage({
  searchParams,
}: {
  searchParams: Promise<Search>;
}) {
  const q = await searchParams;
  const contacts = many(q.contact)
    .map(parseContact)
    .filter((c): c is Contact => c !== null);
  return (
    <AuthLayout>
      <AccessRemoved
        code={one(q.code) || null}
        workspace={one(q.ws)}
        endedAt={one(q.at) || null}
        contacts={contacts}
      />
    </AuthLayout>
  );
}
