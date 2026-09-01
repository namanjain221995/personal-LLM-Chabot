import type { Metadata } from 'next';

import { AcceptInviteForm } from '@/components/auth/AcceptInviteForm';
import { AuthLayout } from '@/components/auth/AuthLayout';

const APP_NAME = process.env.NEXT_PUBLIC_APP_NAME ?? 'TechSara AI';

export const metadata: Metadata = {
  title: `Join workspace · ${APP_NAME}`,
};

/**
 * The token is read client-side from ?token=... (AcceptInviteForm), so this
 * page needs no searchParams prop and stays statically renderable.
 */
export default function AcceptInvitePage() {
  return (
    <AuthLayout>
      <AcceptInviteForm />
    </AuthLayout>
  );
}
