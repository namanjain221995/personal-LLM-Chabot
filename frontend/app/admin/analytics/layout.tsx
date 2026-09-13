'use client';

/**
 * The analytics and infrastructure pages' capability courtesy.
 *
 * /admin/shares and /admin/audit already send an admin without their
 * capability back to /admin. The analytics pages did not: a plain admin who
 * typed /admin/analytics got the whole chart layout with "Not found." in
 * every panel beside empty states that read as though the workspace had no
 * traffic, and a console 404 per request. The rail never links here without
 * `analytics.read`; this covers the typed or bookmarked URL. The server
 * stays the authority — every analytics endpoint 404s without it.
 */

import { useEffect, type ReactNode } from 'react';
import { useAdminMe } from '@/components/admin/AdminMeContext';
import { can } from '@/components/admin/api';
import { nav } from '@/components/admin/nav';

export default function AnalyticsLayout({ children }: { children: ReactNode }) {
  const me = useAdminMe();
  const allowed = can(me, 'analytics.read');

  useEffect(() => {
    if (!allowed) nav.assign('/admin');
  }, [allowed]);

  if (!allowed) return null;
  return <>{children}</>;
}
