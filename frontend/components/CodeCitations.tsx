'use client';

/**
 * Code citations panel (Phase 3) — the `path:Lstart-Lend` excerpts behind a
 * repo answer. Each row shows the file + line range; click to expand the code
 * snippet (monospace, scrollable).
 */

import { useState } from 'react';
import type { CodeSource } from '@/lib/types';
import { IconChevronDown, IconFileText } from './icons';

export function CodeCitations({ sources }: { sources: CodeSource[] }) {
  const [open, setOpen] = useState<number | null>(null);
  return (
    <ul className="flex flex-col gap-1.5">
      {sources.map((s, i) => {
        const label = `${s.path}:L${s.start_line}-L${s.end_line}`;
        const isOpen = open === i;
        return (
          <li key={`${s.path}-${s.start_line}-${i}`}>
            <button
              type="button"
              onClick={() => setOpen(isOpen ? null : i)}
              aria-expanded={isOpen}
              className="flex w-full items-center gap-2 rounded-lg border border-border bg-surface px-2.5 py-2 text-left transition-colors duration-ts hover:border-accent/50 hover:bg-surface-2"
            >
              <IconFileText size={14} className="shrink-0 text-muted" />
              <span className="min-w-0 flex-1 truncate font-mono text-xs text-ink">
                {label}
              </span>
              <IconChevronDown
                size={14}
                className={`shrink-0 text-faint transition-transform duration-ts ${
                  isOpen ? 'rotate-180' : ''
                }`}
              />
            </button>
            {isOpen && (
              <pre className="mt-1 max-h-72 w-full overflow-auto whitespace-pre rounded-lg border border-border bg-bg p-3 font-mono text-xs leading-relaxed text-muted">
                {s.snippet}
              </pre>
            )}
          </li>
        );
      })}
    </ul>
  );
}
