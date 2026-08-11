'use client';

/**
 * Research panel — the searches behind an answer.
 *
 * Collapsed it is one line: how many sources, how long it took, and a live
 * spinner while the work is still running. Expanded it shows which domains the
 * research leaned on, and every search the model ran with the results it got
 * back, so "90 sources" is inspectable rather than a number to be trusted.
 *
 * Driven live by `research` SSE events and re-rendered from meta.research when
 * a stored conversation is reopened — the same component either way.
 */

import { useMemo, useState } from 'react';
import { Loader } from './Loader';
import type { Research } from '@/lib/types';
import {
  IconChevronDown,
  IconExternal,
  IconGlobe,
  IconSearch,
} from './icons';

/** "8m 10s" / "47s" — matches how the elapsed time reads in the reference UIs. */
export function formatElapsed(ms: number): string {
  const total = Math.max(0, Math.round(ms / 1000));
  const m = Math.floor(total / 60);
  const s = total % 60;
  return m > 0 ? `${m}m ${s}s` : `${s}s`;
}

/** Distinct sources found across every search (the same page can repeat). */
export function countSources(research: Research): number {
  const seen = new Set<string>();
  for (const q of research.queries) {
    for (const r of q.results) seen.add(r.url);
  }
  return seen.size;
}

/** Domains ranked by how many results they supplied. */
export function rankDomains(
  research: Research,
): { domain: string; count: number }[] {
  const seen = new Set<string>();
  const tally = new Map<string, number>();
  for (const q of research.queries) {
    for (const r of q.results) {
      if (seen.has(r.url)) continue;
      seen.add(r.url);
      const d = r.domain || r.url;
      tally.set(d, (tally.get(d) ?? 0) + 1);
    }
  }
  return [...tally.entries()]
    .map(([domain, count]) => ({ domain, count }))
    .sort((a, b) => b.count - a.count || a.domain.localeCompare(b.domain));
}

const TOP_DOMAINS = 4;

/** Exported for the ActivityPanel (2026-08-05) — same bars, different home. */
export function DomainBars({ research }: { research: Research }) {
  const ranked = useMemo(() => rankDomains(research), [research]);
  if (ranked.length === 0) return null;
  const top = ranked.slice(0, TOP_DOMAINS);
  const rest = ranked.slice(TOP_DOMAINS);
  const restCount = rest.reduce((n, d) => n + d.count, 0);
  const max = top[0].count || 1;

  return (
    <div className="rounded-ts border border-border bg-surface p-2.5">
      <ul className="flex flex-col gap-1.5">
        {top.map((d) => (
          <li key={d.domain} className="flex items-center gap-2.5">
            <IconGlobe size={12} className="shrink-0 text-faint" />
            <span className="min-w-0 flex-1 truncate text-xs text-ink">
              {d.domain}
            </span>
            <span className="shrink-0 text-xs tabular-nums text-muted">
              {d.count} {d.count === 1 ? 'source' : 'sources'}
            </span>
            <span
              aria-hidden
              className="h-1.5 w-20 shrink-0 overflow-hidden rounded-full bg-surface-2"
            >
              <span
                className="block h-full rounded-full bg-accent/70"
                style={{ width: `${Math.max(8, (d.count / max) * 100)}%` }}
              />
            </span>
          </li>
        ))}
      </ul>
      {rest.length > 0 && (
        <p className="mt-1.5 pl-[22px] text-xs text-faint">
          ··· {restCount} other {restCount === 1 ? 'source' : 'sources'} across{' '}
          {rest.length} {rest.length === 1 ? 'domain' : 'domains'}
        </p>
      )}
    </div>
  );
}

/** Exported for the ActivityPanel (2026-08-05). */
export function QueryGroup({
  query,
  results,
}: {
  query: string;
  results: Research['queries'][number]['results'];
}) {
  const [open, setOpen] = useState(false);
  return (
    <div className="rounded-ts border border-border bg-surface">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-2.5 py-2 text-left transition-colors duration-ts hover:bg-surface-2"
      >
        <IconSearch size={12} className="shrink-0 text-faint" />
        <span className="min-w-0 flex-1 truncate text-xs text-ink">{query}</span>
        <span className="shrink-0 text-xs tabular-nums text-muted">
          {results.length} {results.length === 1 ? 'result' : 'results'}
        </span>
        <IconChevronDown
          size={12}
          className={`shrink-0 text-faint transition-transform duration-ts ${
            open ? 'rotate-180' : ''
          }`}
        />
      </button>
      {open && results.length > 0 && (
        <ul className="flex max-h-72 flex-col gap-0.5 overflow-y-auto border-t border-border p-1.5">
          {results.map((r) => (
            <li key={r.url}>
              <a
                href={r.url}
                target="_blank"
                rel="noopener noreferrer"
                className="flex items-center gap-2 rounded-lg px-2 py-1.5 transition-colors duration-ts hover:bg-surface-2"
              >
                <IconGlobe size={11} className="shrink-0 text-faint" />
                <span className="min-w-0 flex-1 truncate text-xs text-ink">
                  {r.title}
                </span>
                <span className="shrink-0 text-xs text-faint">{r.domain}</span>
                <IconExternal size={11} className="shrink-0 text-faint" />
              </a>
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}

export function ResearchPanel({ research }: { research: Research }) {
  const [open, setOpen] = useState(false);
  const sources = countSources(research);
  if (sources === 0 && !research.active) return null;

  const elapsed =
    research.elapsedMs != null ? formatElapsed(research.elapsedMs) : null;

  return (
    <div className="my-2 overflow-hidden rounded-ts border border-border bg-surface-2/40">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-center gap-2 px-3 py-2.5 text-left transition-colors duration-ts hover:bg-surface-2"
      >
        {research.active ? (
          <Loader size={16} label="Researching" />
        ) : (
          <IconSearch size={14} className="shrink-0 text-accent" />
        )}
        <span className="min-w-0 flex-1">
          <span className="block text-sm font-medium text-ink">
            {research.active ? 'Researching…' : 'Research'}
          </span>
          <span className="block text-xs text-muted">
            {sources} {sources === 1 ? 'source' : 'sources'}
            {research.active ? ' and counting…' : ''}
            {research.read != null && !research.active
              ? ` · ${research.read} read`
              : ''}
            {elapsed ? ` · ${elapsed}` : ''}
          </span>
        </span>
        <IconChevronDown
          size={13}
          className={`shrink-0 text-faint transition-transform duration-ts ${
            open ? 'rotate-180' : ''
          }`}
        />
      </button>

      {open && (
        <div className="flex flex-col gap-2 border-t border-border px-3 py-3">
          <DomainBars research={research} />
          <p className="pt-0.5 text-[11px] font-medium uppercase tracking-wide text-faint">
            {research.queries.length}{' '}
            {research.queries.length === 1 ? 'search' : 'searches'}
          </p>
          {research.queries.map((q) => (
            <QueryGroup key={q.query} query={q.query} results={q.results} />
          ))}
        </div>
      )}
    </div>
  );
}
