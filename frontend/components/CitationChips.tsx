/**
 * Proof-drawer Sources section (§9): citation chips `{object} · {record_id}`
 * opening the Salesforce record in a new tab.
 */

import type { Citation } from '@/lib/types';
import { IconExternal } from './icons';

export function CitationChips({ citations }: { citations: Citation[] }) {
  return (
    <ul className="flex flex-wrap gap-2">
      {citations.map((c) => (
        <li key={`${c.object}-${c.record_id}`}>
          <a
            href={c.url}
            target="_blank"
            rel="noopener noreferrer"
            className="group inline-flex items-center gap-2 rounded-full border border-border bg-surface px-3 py-1.5 text-xs no-underline transition-colors duration-ts hover:border-accent/50 hover:bg-surface-2"
            aria-label={`Open ${c.object} ${c.record_id} in Salesforce (new tab)`}
          >
            <span className="font-medium text-ink">{c.object}</span>
            <span aria-hidden className="text-faint">
              ·
            </span>
            <span className="font-mono text-muted">{c.record_id}</span>
            <IconExternal
              size={12}
              className="text-faint transition-colors duration-ts group-hover:text-accent"
            />
          </a>
        </li>
      ))}
    </ul>
  );
}
