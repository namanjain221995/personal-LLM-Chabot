'use client';

/**
 * Message rendering (§9): quiet right-aligned user bubbles; full-width
 * assistant rows (no bubble) with markdown, streaming caret, pre-first-token
 * shimmer, "Stopped" marker, inline error rows with Retry, and hover
 * actions (Copy, Regenerate).
 */

import type { ChatMessage } from '@/lib/types';
import { AgentTimeline } from './AgentTimeline';
import { ResearchPanel } from './ResearchPanel';
import { Markdown } from './Markdown';
import { PastedChip } from './PastedChip';
import { ProofDrawer } from './ProofDrawer';
import { CopyButton } from './CopyButton';
import { ReasoningAccordion } from './ReasoningAccordion';
import { friendlyError, trimNotice } from '@/lib/errors';
import { IconAlert, IconFileText, IconRefresh } from './icons';

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
  if (message.role === 'user') {
    return (
      <div className="flex justify-end">
        <div className="max-w-[85%] sm:max-w-[70%]">
          {message.imageDataUrl && (
            <div className="mb-1.5 flex justify-end">
              {/* data: URL preview of the user's upload — next/image can't optimize these */}
              {/* eslint-disable-next-line @next/next/no-img-element */}
              <img
                src={message.imageDataUrl}
                alt="Attached image"
                className="max-h-40 rounded-ts border border-border object-cover"
              />
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
                  <span className="text-[10px] uppercase tracking-wide text-faint">
                    PDF
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
          {reasoningText && (
            <ReasoningAccordion
              text={reasoningText}
              seconds={reasoningSeconds}
              thinking={streaming && message.content.length === 0}
            />
          )}

          {research && <ResearchPanel research={research} />}

          {steps.length > 0 && <AgentTimeline steps={steps} />}

          {message.content && (
            <div className="text-[15px]">
              <Markdown text={message.content} />
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
              className={`mt-2 flex items-center gap-1.5 transition-opacity duration-ts ${
                isLast
                  ? 'opacity-100'
                  : 'opacity-0 focus-within:opacity-100 group-hover/msg:opacity-100'
              }`}
            >
              <CopyButton text={message.content} label="Copy message" />
              <button
                type="button"
                onClick={onRegenerate}
                aria-label="Regenerate response"
                title="Regenerate"
                className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-2 py-1 text-xs text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
              >
                <IconRefresh size={13} />
                Regenerate
              </button>
            </div>
          )}
        </>
      )}
    </div>
  );
}
