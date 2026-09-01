'use client';

/**
 * Who is using the admin area. The layout resolves /api/auth/me once (after
 * gating on members.read) and every page reads the result from here instead
 * of re-probing — one fetch, one redirect rule, one source of capabilities.
 */

import { createContext, useContext, type ReactNode } from 'react';
import type { Me } from './api';

const AdminMeContext = createContext<Me | null>(null);

export function AdminMeProvider({
  me,
  children,
}: {
  me: Me;
  children: ReactNode;
}) {
  return (
    <AdminMeContext.Provider value={me}>{children}</AdminMeContext.Provider>
  );
}

export function useAdminMe(): Me {
  const me = useContext(AdminMeContext);
  if (!me) {
    throw new Error('useAdminMe must be used inside the /admin layout.');
  }
  return me;
}
