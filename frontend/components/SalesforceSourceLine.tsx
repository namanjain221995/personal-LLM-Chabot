'use client';

/**
 * Provenance for a Salesforce-derived answer.
 *
 * A number without its source is just a number. This line says which objects
 * were read, how many records matched, when the query ran, and whether the data
 * was live or the synced copy — the four things that decide whether an answer
 * can be acted on.
 *
 * Record ids are deliberately absent: they identify people and deals, they are
 * useless to a reader, and putting them in the transcript puts them in every
 * export and every screenshot.
 */

import type { SalesforceSources } from '@/lib/types';
import { IconCloud } from './icons';

function when(timestamp: string | undefined): string {
  if (!timestamp) return '';
  const parsed = new Date(timestamp);
  if (Number.isNaN(parsed.getTime())) return '';
  return parsed.toLocaleString(undefined, {
    dateStyle: 'medium',
    timeStyle: 'short',
  });
}

export function SalesforceSourceLine({
  sources,
  scope,
  assumptions,
}: {
  sources: SalesforceSources;
  scope?: string;
  assumptions?: string[];
}) {
  const parts: string[] = [];
  if (sources.objects?.length) parts.push(sources.objects.join(', '));
  if (typeof sources.record_count === 'number') {
    parts.push(
      `${sources.record_count.toLocaleString()} record${
        sources.record_count === 1 ? '' : 's'
      }`,
    );
  }
  // "Live" and "synced copy" are not interchangeable, and an answer that
  // implies the wrong one is worse than one that says nothing.
  parts.push(sources.source === 'live' ? 'live from Salesforce' : 'synced copy');
  const stamp = when(sources.query_timestamp);
  if (stamp) parts.push(stamp);

  return (
    <div className="mt-2 space-y-1">
      <p className="inline-flex flex-wrap items-center gap-1.5 rounded-md border border-border bg-surface px-2 py-1 text-xs text-muted">
        <IconCloud size={12} className="shrink-0 text-accent" />
        {parts.join(' · ')}
        {sources.truncated && (
          <span className="text-warn">· capped, totals cover what was read</span>
        )}
      </p>
      {scope && (
        <p className="text-xs text-faint">
          <span className="text-muted">Scope:</span> {scope}
        </p>
      )}
      {assumptions && assumptions.length > 0 && (
        <ul className="list-inside list-disc text-xs text-faint">
          {assumptions.map((assumption) => (
            <li key={assumption}>{assumption}</li>
          ))}
        </ul>
      )}
    </div>
  );
}
