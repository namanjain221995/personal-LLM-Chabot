'use client';

/**
 * Empty state: TechSara mark + greeting only. The six suggestion chips were
 * removed on 2026-07-23 (owner request) for a ChatGPT-style clean start.
 */

import { TechSaraMark } from './TechSaraMark';

export function EmptyState() {
  return (
    <div className="flex h-full flex-col items-center justify-center px-4 py-10">
      <TechSaraMark size={56} />
      <h1 className="mt-5 text-2xl font-semibold tracking-tight">
        What can I help with?
      </h1>
    </div>
  );
}
