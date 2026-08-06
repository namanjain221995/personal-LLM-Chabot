'use client';

/**
 * Message rendering (§9): quiet right-aligned user bubbles; full-width
 * assistant rows (no bubble) with markdown, streaming caret, pre-first-token
 * shimmer, "Stopped" marker, inline error rows with Retry, and a
 * ChatGPT-style icon action row (2026-08-05): copy · like · dislike · try
 * again, plus a "Sources" book button that opens the right-side
 * ActivityPanel (thinking + web research + time taken). Thinking and
 * research render inline only WHILE streaming; finished answers keep them
 * behind the Sources button instead.
 */

import { useState } from 'react';
import type { ChatMessage } from '@/lib/types';
import {
  loadFeedback,
  saveFeedback,
  toggleFeedback,
  type MessageFeedback,
} from '@/lib/feedback';
import { stripCitations } from '@/lib/citations';
import { AgentTimeline } from './AgentTimeline';
import { ActivityPanel } from './ActivityPanel';
import { countSources, ResearchPanel } from './ResearchPanel';
import { Markdown } from './Markdown';
import { PastedChip } from './PastedChip';
import { ProofDrawer } from './ProofDrawer';
import { CopyButton } from './CopyButton';
import { ReasoningAccordion } from './ReasoningAccordion';
import { friendlyError, trimNotice } from '@/lib/errors';
import {
  IconAlert,
  IconBook,
  IconFileText,
  IconRefresh,
  IconThumbDown,
  IconThumbUp,
} from './icons';

/** "report.pdf" → "PDF", "sales.csv" → "CSV", "data.tar.gz" → "TAR.GZ". */
function fileBadge(name: string): string {
  const m = /\.(tar\.gz|[a-z0-9]{1,5})$/i.exec(name.trim());
  return m ? m[1].toUpperCase() : 'FILE';
}

export function MessageRow({
  message,
  isLast,
  onRegenerate,
  onShowSummary,
  onRetry,
}: {
  message: ChatMessage;
  isLast: boolean;
  onRegenerate: () => void;
  /** Opens the read-only rolling-summary panel (compaction notice). */
  onShowSummary?: () => void;
  onRetry: () => void;
}) {
  // Hooks live above the user-bubble early return (rules of hooks).
  const [activityOpen, setActivityOpen] = useState(false);
  const [feedback, setFeedback] = useState<MessageFeedback | null>(() =>
    typeof window === 'undefined'
      ? null
      : loadFeedback(window.localStorage, message.id),
  );

  function onThumb(kind: MessageFeedback) {
    const next = toggleFeedback(feedback, kind);
    setFeedback(next);
    saveFeedback(window.localStorage, message.id, next);
  }

  if (message.role === 'user') {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] sm:max-w-[70%]">
          {(message.imageDataUrls?.length || message.imageDataUrl) && (
            <div className="mb-1.5 flex flex-wrap justify-end gap-1.5">
              {/* 2026-08-05: up to 5 images per turn — `imageDataUrls` when
                  several, the legacy single `imageDataUrl` otherwise.
                  data: URL previews — next/image can't optimize these. */}
              {(message.imageDataUrls?.length
                ? message.imageDataUrls
                : [message.imageDataUrl as string]
              ).map((url, i) => (
                /* eslint-disable-next-line @next/next/no-img-element */
                <img
                  key={i}
                  src={url}
                  alt={`Attached image ${i + 1}`}
                  className="max-h-40 rounded-ts border border-border object-cover"
                />
              ))}
            </div>
          )}
          {message.pdfName && (
            <div className="mb-1.5 flex justify-end">
              <span className="inline-flex items-center gap-2 rounded-2xl border border-border bg-surface-2 py-1.5 pl-1.5 pr-3">
                <span className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-danger/15 text-danger">
                  <IconFileText size={16} />
                </span>
                <span className="flex flex-col">
                  <span className="max-w-[220px] truncate text-xs text-ink">
                    {message.pdfName}
                  </span>
                  {/* pdfName carries EVERY non-image attachment (datasets
                      included) — a .csv labelled "PDF" was just wrong. */}
                  <span className="text-[10px] uppercase tracking-wide text-faint">
                    {fileBadge(message.pdfName)}
                  </span>
                </span>
              </span>
            </div>
          )}
          {message.meta?.pasted?.map((p) => (
            <div key={p.id} className="mb-1.5 flex justify-end">
              <PastedChip pasted={p} />
            </div>
          ))}
          {message.content && (
            <div className="whitespace-pre-wrap break-words rounded-[20px] bg-bubble px-4 py-2.5 text-[15px] leading-relaxed">
              {message.content}
            </div>
          )}
        </div>
      </div>
    );
  }

  const streaming = message.status === 'streaming';
  // V2: reasoning + steps live on the message while streaming and inside
  // meta once persisted (§4d/§4e) — read whichever is present.
  const reasoningText = message.meta?.reasoning ?? message.reasoning ?? '';
  const reasoningSeconds =
    message.meta?.reasoning_seconds ?? message.reasoningSeconds;
  const steps = message.steps ?? message.meta?.steps ?? [];
  // Live while streaming, from the stored meta once persisted.
  const research = message.research ?? message.meta?.research;
  // The Sources book button opens the ActivityPanel; it appears only when
  // the finished answer actually has thinking, an agent plan, web research,
  // or numbered [n] citations (meta.sources) behind it — none of which show
  // inline once the answer is done.
  const webSources = message.meta?.sources;
  const hasActivity =
    Boolean(reasoningText) ||
    steps.length > 0 ||
    Boolean(research && countSources(research) > 0) ||
    Boolean(webSources?.length);
  const showShimmer =
    streaming &&
    message.content.length === 0 &&
    reasoningText.length === 0 &&
    steps.length === 0 &&
    // The research panel is itself a progress indicator — showing the
    // shimmer underneath it would read as two things loading.
    !research &&
    !message.searchStatus;

  return (
    <div className="group/msg w-full">
      {message.searchStatus && message.content.length === 0 && (
        <div className="mb-2 flex items-center gap-2 text-sm text-muted">
          <span
            className="inline-block h-3.5 w-3.5 animate-spin rounded-full border-2 border-muted/40 border-t-accent"
            aria-hidden
          />
          {message.searchStatus}
        </div>
      )}
      {showShimmer ? (
        <div aria-label="Waiting for the first token" className="space-y-2.5 py-1">
          <div className="shimmer-line w-3/5" />
          <div className="shimmer-line w-4/5" />
          <div className="shimmer-line w-2/5" />
        </div>
      ) : (
        <>
          {/* Inline thinking/research are LIVE progress only (2026-08-05):
              once the answer is done they move behind the Sources button so
              finished messages stay clean, ChatGPT-style. */}
          {reasoningText && streaming && (
            <ReasoningAccordion
              text={reasoningText}
              seconds={reasoningSeconds}
              thinking={streaming && message.content.length === 0}
            />
          )}

          {research && (streaming || research.active) && (
            <ResearchPanel research={research} />
          )}

          {/* The agent plan card is live progress too (owner request
              2026-08-05): once the answer is done it moves into the
              ActivityPanel behind the Sources button. */}
          {steps.length > 0 && streaming && <AgentTimeline steps={steps} />}

          {message.content && (
            <div className="text-[15px]">
              {/* [n] citation markers are stripped for display (2026-08-05)
                  — the numbered sources live in the ActivityPanel instead.
                  The stored content keeps them, so nothing is lost. */}
              <Markdown text={stripCitations(message.content)} />
              {streaming && <span aria-hidden className="stream-caret" />}
            </div>
          )}

          {message.meta?.context?.compacted && (
            <button
              type="button"
              onClick={onShowSummary}
              className="mt-2 inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-2 py-1 text-xs text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
            >
              <IconFileText size={12} className="shrink-0" />
              Conversation compacted — older messages were summarized to free up
              space.
              <span className="text-faint underline">See summary</span>
            </button>
          )}

          {message.meta?.input_trimmed && (
            <p className="mt-2 inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-2 py-1 text-xs text-muted">
              <IconAlert size={12} className="shrink-0 text-warn" />
              {trimNotice(message.meta.input_trimmed)}
            </p>
          )}

          {message.status === 'stopped' && (
            <p className="mt-2 inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-2 py-0.5 text-xs text-muted">
              Stopped
            </p>
          )}

          {message.status === 'error' &&
            (() => {
              // Raw upstream payloads ("Error code: 400 - {'error': …}") are
              // unreadable in a chat thread; show the plain-language cause and
              // keep the original one click away.
              const friendly = friendlyError(message.errorMessage);
              return (
                <div
                  role="alert"
                  className="mt-2 rounded-ts border bg-surface px-3.5 py-2.5"
                  style={{ borderColor: 'color-mix(in srgb, var(--ts-danger) 40%, transparent)' }}
                >
                  <div className="flex flex-wrap items-center gap-3">
                    <IconAlert size={16} className="shrink-0 text-danger" />
                    <span className="min-w-0 flex-1 text-sm">
                      {friendly.message}
                    </span>
                    <button
                      type="button"
                      onClick={onRetry}
                      className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-2.5 py-1 text-xs font-medium transition-colors duration-ts hover:bg-surface-2"
                    >
                      <IconRefresh size={13} />
                      Retry
                    </button>
                  </div>
                  {friendly.detail && (
                    <details className="mt-2">
                      <summary className="cursor-pointer text-xs text-faint hover:text-muted">
                        Technical details
                      </summary>
                      <pre className="mt-1.5 max-h-48 overflow-auto whitespace-pre-wrap break-words rounded-md bg-surface-2 p-2 text-[11px] leading-relaxed text-muted">
                        {friendly.detail}
                      </pre>
                    </details>
                  )}
                </div>
              );
            })()}

          {message.meta && <ProofDrawer meta={message.meta} />}

          {!streaming && message.content && message.status !== 'error' && (
            <div
              className={`mt-1.5 flex items-center gap-0.5 transition-opacity duration-ts ${
                isLast
                  ? 'opacity-100'
                  : 'opacity-0 focus-within:opacity-100 group-hover/msg:opacity-100'
              }`}
            >
              {/* ChatGPT-style icon row (2026-08-05): quiet ghost icons
                  instead of labelled chip buttons. */}
              <CopyButton
                text={stripCitations(message.content)}
                label="Copy message"
                variant="icon"
              />
              <button
                type="button"
                onClick={() => onThumb('up')}
                aria-label="Good response"
                aria-pressed={feedback === 'up'}
                title="Good response"
                className={`rounded-lg p-1.5 transition-colors duration-ts hover:bg-surface-2 ${
                  feedback === 'up'
                    ? 'text-accent'
                    : 'text-muted hover:text-ink'
                }`}
              >
                <IconThumbUp size={15} />
              </button>
              <button
                type="button"
                onClick={() => onThumb('down')}
                aria-label="Bad response"
                aria-pressed={feedback === 'down'}
                title="Bad response"
                className={`rounded-lg p-1.5 transition-colors duration-ts hover:bg-surface-2 ${
                  feedback === 'down'
                    ? 'text-danger'
                    : 'text-muted hover:text-ink'
                }`}
              >
                <IconThumbDown size={15} />
              </button>
              <button
                type="button"
                onClick={onRegenerate}
                aria-label="Try again"
                title="Try again"
                className="rounded-lg p-1.5 text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
              >
                <IconRefresh size={15} />
              </button>
              {hasActivity && (
                <button
                  type="button"
                  onClick={() => setActivityOpen(true)}
                  aria-label="Show sources and thinking"
                  aria-expanded={activityOpen}
                  title="Sources — searches and thinking behind this answer"
                  className="ml-1 inline-flex items-center gap-1.5 rounded-lg px-2 py-1.5 text-xs font-medium text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
                >
                  <IconBook size={14} />
                  Sources
                </button>
              )}
            </div>
          )}

          <ActivityPanel
            open={activityOpen}
            onClose={() => setActivityOpen(false)}
            reasoning={reasoningText || undefined}
            reasoningSeconds={reasoningSeconds}
            steps={steps}
            research={research}
            sources={webSources}
          />
        </>
      )}
    </div>
  );
}
