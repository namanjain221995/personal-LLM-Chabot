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

import { useEffect, useState } from 'react';
import type { ChatMessage } from '@/lib/types';
import { parseClarification } from '@/lib/clarification';
import { ClarificationRecord } from './ClarificationCard';
import { Loader } from './Loader';
import { ReasoningStar } from './ReasoningStar';
import { SalesforceSourceLine } from './SalesforceSourceLine';
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
  onFeedback,
  clarificationPending = false,
  clarificationAnswer = '',
}: {
  message: ChatMessage;
  isLast: boolean;
  onRegenerate: () => void;
  /** Opens the read-only rolling-summary panel (compaction notice). */
  onShowSummary?: () => void;
  onRetry: () => void;
  /**
   * Is THIS message's question the one the thread is waiting on?
   *
   * Computed by the parent from the whole thread (`cardState`), because a
   * message cannot know what came after it. A live question renders NOTHING
   * here: it is a temporary control and it belongs to the composer. Only an
   * answered one leaves a record behind.
   */
  clarificationPending?: boolean;
  /** What was chosen, for the record of an answered question. */
  clarificationAnswer?: string;
  /** Persist a thumb server-side. Omitted in contexts with no store
   *  (previews, tests), where the localStorage fallback still applies. */
  onFeedback?: (feedback: MessageFeedback | null) => void;
}) {
  // Hooks live above the user-bubble early return (rules of hooks).
  const [activityOpen, setActivityOpen] = useState(false);
  // The SERVER's value wins when the message has one: it is the copy that
  // survives a reload. localStorage is the fallback for messages that have
  // not been stored yet (and for anything rendering without a store), which
  // is also the whole reason thumbs used to disappear on refresh — a
  // rehydrated message is keyed differently from the live one it replaced.
  const [feedback, setFeedback] = useState<MessageFeedback | null>(() => {
    if (message.feedback === 'up' || message.feedback === 'down') {
      return message.feedback;
    }
    if (message.feedback === null) return null;
    return typeof window === 'undefined'
      ? null
      : loadFeedback(window.localStorage, message.id);
  });

  // A conversation loaded after this row first rendered (or refreshed by a
  // detached generation) brings the stored thumb with it.
  useEffect(() => {
    if (message.feedback === 'up' || message.feedback === 'down') {
      setFeedback(message.feedback);
    } else if (message.feedback === null) {
      setFeedback(null);
    }
  }, [message.feedback]);

  function onThumb(kind: MessageFeedback) {
    const next = toggleFeedback(feedback, kind);
    setFeedback(next);
    // Keep writing localStorage: it is what covers a message with no server
    // row yet, and it costs nothing for one that has one.
    saveFeedback(window.localStorage, message.id, next);
    onFeedback?.(next);
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
    Boolean(webSources?.length) ||
    Boolean(message.meta?.document);
  // Salesforce Intelligence Mode: the live phase, or the one the finished
  // answer ended on. Only the LIVE one animates — a reopened chat shows the
  // final phase as history, not as work still in progress.
  const phaseStatus = message.phaseStatus ?? null;
  const clarification = parseClarification(message.meta?.clarification);
  // A turn whose ONLY content is a live question renders nothing at all: the
  // question itself is a temporary control and the composer is showing it.
  // Without this the transcript kept an empty assistant row under the request,
  // complete with copy / thumbs / retry buttons for a message with no text —
  // which reads as an answer that failed rather than a question being asked.
  if (clarification && clarificationPending && !hasActivity && !message.meta?.data) {
    return null;
  }
  const showShimmer =
    streaming &&
    message.content.length === 0 &&
    reasoningText.length === 0 &&
    steps.length === 0 &&
    // The research panel is itself a progress indicator — showing the
    // shimmer underneath it would read as two things loading.
    !research &&
    !message.searchStatus &&
    // …and so is the Reasoning Star, which carries a real phase label.
    !phaseStatus;

  return (
    <div className="group/msg w-full">
      {phaseStatus && streaming && (
        <ReasoningStar
          status={phaseStatus}
          size={message.content.length === 0 ? 'lg' : 'sm'}
          className="mb-2"
        />
      )}
      {message.searchStatus && message.content.length === 0 && (
        <div className="mb-2 flex items-center gap-2.5 text-sm text-muted">
          <Loader size={22} />
          {message.searchStatus}
        </div>
      )}
      {showShimmer ? (
        /* The ordinary "waiting for the first token" state. It used to be three
           shimmering bars pretending to be text; it is the same event as every
           other kind of work, so it now says so with the same artwork. */
        <div className="py-1">
          <Loader size={28} label="Waiting for the first token" />
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

          {/* The question is streamed as TEXT as well as carried on the card,
              so that a client with no card renderer — and the stored history a
              future one reads back — still shows a usable question. When the
              card IS rendering, showing both means the user reads the same
              question and the same four options twice, one of them inert. The
              card wins; nothing is lost, because the text is still in
              `message.content` and still goes to the model on the next turn. */}
          {message.content && !clarification && (
            /* No font-size override: the body inherits --ts-fs-base (16px).
               It used to be hardcoded to 15px, which is why assistant prose
               read dimmer than it should — thin stems at 15px on pure black
               lose weight to greyscale antialiasing, and #ffffff stops
               looking white. It also left `.md h3`/`h4` (16px) rendering
               LARGER than the body they head. */
            <div>
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

          {/* A LIVE question is not rendered here at all — the composer owns
              it (see ClarificationCard). What the transcript keeps is the
              record of one already answered, so the user turn after it
              ("Interview") reads as an answer rather than a non sequitur. */}
          {clarification && !clarificationPending && (
            <ClarificationRecord
              question={clarification.question}
              answer={clarificationAnswer || 'Answered'}
            />
          )}

          {message.meta?.salesforce_sources && (
            <SalesforceSourceLine
              sources={message.meta.salesforce_sources}
              scope={message.meta.salesforce_scope}
              assumptions={message.meta.assumptions}
            />
          )}


          {message.meta?.input_trimmed && (
            <p className="mt-2 inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-2 py-1 text-xs text-muted">
              <IconAlert size={12} className="shrink-0 text-warn" />
              {trimNotice(message.meta.input_trimmed)}
            </p>
          )}

          {(message.meta?.memory_updated?.length ?? 0) > 0 && (
            <p
              className="mt-2 inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-2 py-1 text-xs text-muted"
              title={message.meta!.memory_updated!.join('\n')}
            >
              Memory updated
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
              // unreadable in a chat thread AND are not the user's business:
              // they can quote a DSN, a header or a traceback. Only the plain
              // sentence is rendered. The original is written to the server
              // log instead (lib/serverLog.ts), which is where an engineer
              // can actually use it.
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
                className={`rounded-lg p-2 transition-colors duration-ts hover:bg-surface-2 ${
                  feedback === 'up'
                    ? 'text-accent'
                    : 'text-icon hover:text-ink'
                }`}
              >
                <IconThumbUp size={18} />
              </button>
              <button
                type="button"
                onClick={() => onThumb('down')}
                aria-label="Bad response"
                aria-pressed={feedback === 'down'}
                title="Bad response"
                className={`rounded-lg p-2 transition-colors duration-ts hover:bg-surface-2 ${
                  feedback === 'down'
                    ? 'text-danger'
                    : 'text-icon hover:text-ink'
                }`}
              >
                <IconThumbDown size={18} />
              </button>
              <button
                type="button"
                onClick={onRegenerate}
                aria-label="Try again"
                title="Try again"
                className="rounded-lg p-2 text-icon transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
              >
                <IconRefresh size={18} />
              </button>
              {hasActivity && (
                <button
                  type="button"
                  onClick={() => setActivityOpen(true)}
                  aria-label="Show sources and thinking"
                  aria-expanded={activityOpen}
                  title="Sources — searches and thinking behind this answer"
                  className="ml-0.5 inline-flex items-center gap-1.5 rounded-lg px-2.5 py-2 text-xs font-medium text-icon transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
                >
                  <IconBook size={16} />
                  Sources
                </button>
              )}
            </div>
          )}

          <ActivityPanel
            documentRead={message.meta?.document}
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
