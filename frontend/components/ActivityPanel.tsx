'use client';

/**
 * Activity panel (owner request 2026-08-05) — the ChatGPT-style right-side
 * drawer behind the "Sources" book icon in a message's action row. Shows
 * what happened behind a finished answer: the thinking (with how long it
 * took) and the web research (sources, searches, elapsed time). While an
 * answer is still streaming the inline ReasoningAccordion / ResearchPanel
 * show the same data live; once done it lives here instead.
 */

import { useEffect, useRef } from 'react';
import type {
  AgentStep,
  DocumentActivity,
  Research,
  ResearchRun,
  WebSource,
} from '@/lib/types';
import { AgentTimeline } from './AgentTimeline';
import {
  countSources,
  DomainBars,
  formatElapsed,
  QueryGroup,
} from './ResearchPanel';
import { WebSources } from './WebSources';
import { IconBook, IconBulb, IconFileText, IconGlobe, IconX } from './icons';

export function ActivityPanel({
  open,
  onClose,
  reasoning,
  reasoningSeconds,
  steps,
  research,
  researchRun,
  sources,
  documentRead,
}: {
  open: boolean;
  onClose: () => void;
  reasoning?: string;
  reasoningSeconds?: number;
  /** The agent plan steps — inline only while streaming (2026-08-05). */
  steps?: AgentStep[];
  research?: Research;
  /** Deep Research's account of itself (2026-09-03): what it established,
      how, and why it stopped. */
  researchRun?: ResearchRun;
  /** The answer's numbered [n] citations (meta.sources) — shown here, not
      in the proof drawer (owner request 2026-08-05: no box in the chat). */
  sources?: WebSource[];
  /** 2026-08-07: the uploaded document that was read — every page shown. */
  documentRead?: DocumentActivity;
}) {
  const closeRef = useRef<HTMLButtonElement>(null);

  useEffect(() => {
    if (!open) return;
    closeRef.current?.focus();
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        // Consume it: while streaming, ChatApp maps a bare Escape to "stop
        // generating" — closing this panel must not also kill an answer.
        e.preventDefault();
        e.stopPropagation();
        onClose();
      }
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [open, onClose]);

  if (!open) return null;

  const researchSources = research ? countSources(research) : 0;
  // Header total: thinking time plus research wall-clock, whichever exist.
  const totalMs =
    (reasoningSeconds ?? 0) * 1000 + (research?.elapsedMs ?? 0);

  return (
    <aside
      role="dialog"
      aria-label="Activity"
      className="fixed inset-y-0 right-0 z-40 flex w-full max-w-[400px] flex-col border-l border-border bg-bg shadow-2xl"
    >
      <header className="flex items-center gap-2 border-b border-border px-4 py-3">
        <h2 className="text-sm font-semibold text-ink">Activity</h2>
        {totalMs > 0 && (
          <span className="text-xs text-muted">
            · {formatElapsed(totalMs)}
          </span>
        )}
        <button
          ref={closeRef}
          type="button"
          onClick={onClose}
          aria-label="Close activity panel"
          title="Close (Esc)"
          className="ml-auto rounded-lg p-1.5 text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
        >
          <IconX size={15} />
        </button>
      </header>

      <div className="flex flex-1 flex-col gap-5 overflow-y-auto px-4 py-4">
        {reasoning && (
          <section aria-label="Thinking">
            <h3 className="flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wide text-faint">
              <IconBulb size={12} />
              Thinking
              {reasoningSeconds != null && (
                <span className="normal-case tracking-normal">
                  · {reasoningSeconds} s
                </span>
              )}
            </h3>
            <div className="mt-2 max-h-[45vh] overflow-y-auto whitespace-pre-wrap border-l-2 border-border pl-3 font-mono text-[12.5px] leading-relaxed text-muted">
              {reasoning}
            </div>
          </section>
        )}

        {documentRead && (
          <section aria-label="Document read">
            <h3 className="flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wide text-faint">
              <IconFileText size={12} />
              Document read
            </h3>
            <p className="mt-1.5 text-[12.5px] text-muted">
              {documentRead.filename} · {documentRead.total_pages} page
              {documentRead.total_pages !== 1 ? 's' : ''} read in full
              {documentRead.ocr_pages
                ? ` · ${documentRead.ocr_pages} via OCR`
                : ''}
            </p>
            <div className="mt-2 max-h-[45vh] space-y-1 overflow-y-auto">
              {documentRead.pages.map((p) => (
                <details
                  key={p.page}
                  className="rounded-lg border border-border bg-surface px-2.5 py-1.5"
                >
                  <summary className="cursor-pointer select-none text-[12px] font-medium text-muted">
                    Page {p.page}
                  </summary>
                  <div className="mt-1.5 whitespace-pre-wrap font-mono text-[11.5px] leading-relaxed text-muted">
                    {p.text}
                  </div>
                </details>
              ))}
              {documentRead.total_pages > documentRead.pages.length && (
                <p className="pt-1 text-[11px] text-faint">
                  Showing the first {documentRead.pages.length} pages here — the
                  model read all {documentRead.total_pages}.
                </p>
              )}
            </div>
          </section>
        )}

        {steps && steps.length > 0 && (
          // The card carries its own "Agent plan" header — no section
          // heading on top of it.
          <section aria-label="Agent plan">
            <AgentTimeline steps={steps} />
          </section>
        )}

        {sources && sources.length > 0 && (
          <section aria-label="Cited sources">
            <h3 className="flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wide text-faint">
              <IconBook size={12} />
              Cited sources · {sources.length}
            </h3>
            <div className="mt-2">
              <WebSources sources={sources} />
            </div>
          </section>
        )}

        {researchRun && (
          <section aria-label="Research summary">
            <h3 className="mb-2 flex items-center gap-2 text-xs font-medium uppercase tracking-wide text-faint">
              <IconBook size={13} />
              Research summary
            </h3>
            <ResearchSummary run={researchRun} />
          </section>
        )}

        {research && researchSources > 0 && (
          <section aria-label="Web research">
            <h3 className="flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wide text-faint">
              <IconGlobe size={12} />
              Web · {researchSources}
              <span className="normal-case tracking-normal">
                {research.read != null ? ` · ${research.read} read` : ''}
                {research.elapsedMs != null
                  ? ` · ${formatElapsed(research.elapsedMs)}`
                  : ''}
              </span>
            </h3>
            <div className="mt-2 flex flex-col gap-2">
              <DomainBars research={research} />
              <p className="pt-0.5 text-[11px] font-medium uppercase tracking-wide text-faint">
                {research.queries.length}{' '}
                {research.queries.length === 1 ? 'search' : 'searches'}
              </p>
              {research.queries.map((q) => (
                <QueryGroup key={q.query} query={q.query} results={q.results} />
              ))}
            </div>
          </section>
        )}
      </div>
    </aside>
  );
}


/** Humanise a stop reason the server logged: "no_information_gain" → "no new information". */
function stopReasonLabel(reason?: string): string {
  switch (reason) {
    case 'sufficient':
      return 'the evidence was sufficient';
    case 'no_information_gain':
      return 'further searches found nothing new';
    case 'duplicate_rate':
      return 'new results were copies of pages already read';
    case 'no_new_queries':
      return 'there was nowhere left to look';
    case 'iteration_cap':
      return 'the round limit was reached';
    case 'source_cap':
      return 'the source limit was reached';
    case 'timeout':
      return 'the time budget ran out';
    default:
      return reason ? reason.replace(/_/g, ' ') : 'done';
  }
}

const STATUS_CLASS: Record<string, string> = {
  current: 'border-accent/50 text-accent',
  conflicting: 'border-amber-500/60 text-amber-600',
  unknown: 'border-border text-faint',
  superseded: 'border-border text-muted',
  historical: 'border-border text-muted',
};

/** What Deep Research established, per subquestion, and why it stopped. */
export function ResearchSummary({ run }: { run: ResearchRun }) {
  const primaries = run.primary_sources?.length ?? 0;
  return (
    <div className="flex flex-col gap-2 rounded-ts border border-border bg-surface p-2.5 text-xs">
      <p className="text-muted">
        Stopped because {stopReasonLabel(run.stop_reason)}
        {run.confidence != null ? ` · confidence ${Math.round(run.confidence * 100)}%` : ''}
        {run.iterations ? ` · ${run.iterations} ${run.iterations === 1 ? 'round' : 'rounds'}` : ''}
        {run.verification_rounds ? ` (${run.verification_rounds} verification)` : ''}
        {run.links_followed ? ` · ${run.links_followed} ${run.links_followed === 1 ? 'link' : 'links'} followed` : ''}
        {primaries ? ` · ${primaries} primary ${primaries === 1 ? 'source' : 'sources'}` : ''}
        {run.duplicates_dropped ? ` · ${run.duplicates_dropped} duplicate${run.duplicates_dropped === 1 ? '' : 's'} discounted` : ''}
        {run.today ? ` · as of ${run.today}` : ''}
      </p>
      {run.resolutions && run.resolutions.length > 0 && (
        <ul className="flex flex-col gap-1.5">
          {run.resolutions.map((r, i) => (
            <li key={`${i}-${r.subquestion}`} className="flex flex-col gap-0.5">
              <span className="flex items-center gap-2">
                <span
                  className={`shrink-0 rounded-full border px-1.5 py-px text-[10px] font-medium uppercase tracking-wide ${
                    STATUS_CLASS[r.status] ?? STATUS_CLASS.unknown
                  }`}
                >
                  {r.status}
                </span>
                <span className="min-w-0 truncate text-ink">{r.subquestion}</span>
              </span>
              {r.status !== 'unknown' && r.value && (
                <span className="pl-1 text-muted">
                  {r.value}
                  {r.as_of ? ` (as of ${r.as_of})` : ''}
                  {r.support?.length ? ` · ${r.support.map((n) => `[${n}]`).join('')}` : ''}
                  {r.superseded?.length
                    ? ` · previously ${r.superseded.map((s) => `${s.value}${s.as_of ? ` (${s.as_of})` : ''}`).join(', ')}`
                    : ''}
                  {r.conflicts?.length
                    ? ` · disputed by ${r.conflicts.map((c) => `${c.value}${c.as_of ? ` (${c.as_of})` : ''}`).join(', ')}`
                    : ''}
                </span>
              )}
              {r.status === 'unknown' && (
                <span className="pl-1 text-faint">not found in the sources consulted</span>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
