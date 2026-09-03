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
import type { AgentStep, DocumentActivity, Research, WebSource } from '@/lib/types';
import { documentReadView } from '@/lib/documentActivity';
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

        {documentRead &&
          (() => {
            // Several documents arrive folded into one payload; this splits
            // them back out from the engine's own per-page prefixes — see
            // lib/documentActivity.ts for why that is evidence and not a
            // guess. One document renders exactly as it always did.
            const view = documentReadView(documentRead);
            const shown = view.documents.reduce((n, d) => n + d.pages.length, 0);
            return (
              <section aria-label="Document read">
                <h3 className="flex items-center gap-1.5 text-[11px] font-medium uppercase tracking-wide text-faint">
                  <IconFileText size={12} />
                  {view.multi ? 'Documents read' : 'Document read'}
                </h3>
                {view.multi ? (
                  <p className="mt-1.5 text-[12.5px] text-muted">
                    {view.reported} documents · {view.totalPages} page
                    {view.totalPages !== 1 ? 's' : ''} read in full
                    {view.ocrPages ? ` · ${view.ocrPages} via OCR` : ''}
                  </p>
                ) : (
                  <p className="mt-1.5 text-[12.5px] text-muted">
                    {documentRead.filename} · {view.totalPages} page
                    {view.totalPages !== 1 ? 's' : ''} read in full
                    {view.ocrPages ? ` · ${view.ocrPages} via OCR` : ''}
                  </p>
                )}
                <div className="mt-2 max-h-[45vh] space-y-2 overflow-y-auto">
                  {view.documents.map((d, di) => (
                    <div key={`${d.name}-${di}`} className="space-y-1">
                      {view.multi && (
                        <p className="text-[12px] font-medium text-ink">
                          {d.name}
                          <span className="font-normal text-faint">
                            {' '}· {d.pages.length} page{d.pages.length !== 1 ? 's' : ''}
                          </span>
                        </p>
                      )}
                      {d.pages.map((p) => (
                        <details
                          key={`${di}-${p.page}`}
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
                    </div>
                  ))}
                  {view.totalPages > shown && (
                    <p className="pt-1 text-[11px] text-faint">
                      Showing the first {shown} pages here — the model read all{' '}
                      {view.totalPages}.
                    </p>
                  )}
                  {view.multi && view.reported > view.documents.length && (
                    <p className="pt-1 text-[11px] text-faint">
                      {view.reported - view.documents.length} more document
                      {view.reported - view.documents.length !== 1 ? 's were' : ' was'}{' '}
                      read, beyond the pages shown here.
                    </p>
                  )}
                </div>
              </section>
            );
          })()}

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
