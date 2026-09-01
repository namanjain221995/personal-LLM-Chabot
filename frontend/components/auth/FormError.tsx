'use client';

/**
 * Inline form error — the toast error recipe (Providers.tsx) flattened into
 * a block so it can sit inside the form it belongs to. role="alert" makes the
 * appearance announce itself to assistive tech.
 */

import { IconAlert } from '../icons';

export function FormError({ message }: { message: string | null }) {
  if (!message) return null;
  return (
    <div
      role="alert"
      className="flex items-start gap-2 rounded-lg border border-danger/40 bg-danger/10 px-3 py-2 text-sm text-danger"
    >
      <IconAlert size={16} className="mt-0.5 shrink-0" />
      <span className="min-w-0">{message}</span>
    </div>
  );
}
