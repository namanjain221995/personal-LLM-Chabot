'use client';

/**
 * Web-search sources panel (Phase 1) — the numbered [n] sources behind a
 * search answer. Each row: the citation number, page title, domain, and a
 * link opening in a new tab. No remote favicons are fetched (that would be an
 * extra outbound request per source); a globe glyph stands in.
 */

import type { WebSource } from '@/lib/types';
import { IconGlobe, IconExternal } from './icons';

/** Host for a source stored before the backend sent `domain` (see below). */
function hostOf(url: string): string {
  try {
    return new URL(url).hostname.replace(/^www\./, '');
  } catch {
    return '';
  }
}

export function WebSources({ sources }: { sources: WebSource[] }) {
  return (
    // High-effort research reads dozens of pages. Rendered inline that is a
    // 60-row list shoving the rest of the conversation off the screen, so the
    // list scrolls within itself once it outgrows a comfortable height.
    <ul className="flex max-h-[22rem] flex-col gap-1.5 overflow-y-auto pr-1">
      {/*
        Keyed on the url and the position, not on `s.n`: a message persisted
        before the knowledge layer numbered its sources carries no `n` at
        all, and React then sees a whole list keyed `undefined`. The list is
        fixed metadata on a finished message — never reordered, never
        appended to — so the position is a stable key. `n` and `domain` fall
        back the same way, so those old rows still render a number and a host.
      */}
      {sources.map((s, i) => (
        <li key={`${s.url}#${i}`}>
          <a
            href={s.url}
            target="_blank"
            rel="noopener noreferrer"
            className="flex items-start gap-2.5 rounded-lg border border-border bg-surface px-2.5 py-2 transition-colors duration-ts hover:border-accent/50 hover:bg-surface-2"
          >
            <span className="mt-0.5 grid h-5 w-5 shrink-0 place-items-center rounded bg-surface-2 text-[10px] font-semibold text-muted">
              {s.n || i + 1}
            </span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-sm text-ink">{s.title}</span>
              <span className="flex items-center gap-1 text-xs text-faint">
                <IconGlobe size={11} className="shrink-0" />
                {s.domain || hostOf(s.url)}
              </span>
            </span>
            <IconExternal size={13} className="mt-0.5 shrink-0 text-faint" />
          </a>
        </li>
      ))}
    </ul>
  );
}
