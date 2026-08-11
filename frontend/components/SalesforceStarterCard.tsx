'use client';

/**
 * The Salesforce starter card — shown above the composer when the source is on
 * and the composer is empty.
 *
 * Non-blocking by design: it never covers the composer, never steals focus, and
 * disappears the moment anything is typed. Its options come from the SERVER
 * (`GET /api/chat/salesforce/{id}`), which filters them against what this
 * connection can actually query — so an object the integration user cannot see
 * is never offered.
 *
 * When the conversation has an unfinished or recently completed Salesforce
 * task, "Continue…" is the first option, because resuming is almost always what
 * someone reopening a chat wants.
 */

import type { StarterOption } from '@/lib/salesforceApi';
import { IconCloud } from './icons';

export interface SalesforceStarterCardProps {
  options: StarterOption[];
  /** Run one of the suggestions as a message. */
  onPick: (prompt: string) => void;
  /** "Something else" — focus the normal composer and get out of the way. */
  onUseComposer: () => void;
}

export function SalesforceStarterCard({
  options,
  onPick,
  onUseComposer,
}: SalesforceStarterCardProps) {
  if (options.length === 0) return null;
  return (
    <div
      className="mb-2 rounded-ts border border-border bg-surface px-3 py-2.5"
      data-testid="salesforce-starter-card"
    >
      <p className="flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wide text-faint">
        <IconCloud size={12} className="text-accent" />
        Salesforce
      </p>
      <div className="mt-2 flex flex-wrap gap-1.5">
        {options.map((option) => (
          <button
            key={option.id}
            type="button"
            onClick={() => onPick(option.prompt)}
            title={option.description}
            className="rounded-full border border-border bg-bg px-3 py-1.5 text-xs text-ink transition-colors duration-ts hover:border-accent/60 hover:bg-surface-2 focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            {option.label}
          </button>
        ))}
        <button
          type="button"
          onClick={onUseComposer}
          className="rounded-full border border-transparent px-3 py-1.5 text-xs text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink focus:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          Something else
        </button>
      </div>
    </div>
  );
}
