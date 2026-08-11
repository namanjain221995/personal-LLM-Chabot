'use client';

/**
 * The Salesforce clarification card.
 *
 * One question, two to four options, an inline way to type something else, and
 * — only when a safe default exists — a way past it. Answering RESUMES the
 * original request server-side; it does not send a rewritten question as a new
 * message, which is what the legacy `meta.clarify` buttons did.
 *
 * MOST CARDS TAKE SEVERAL ANSWERS (owner request, 2026-08-11). Asked which
 * object holds payment and invoice data, "Invoice__c" AND "Payment__c" is the
 * honest answer; a radio group forced a choice between two things the user
 * needed together. The server pins the handful of slots where two answers are
 * incoherent (see EXCLUSIVE_SLOTS) and sends `multi_select: false` for those.
 *
 * All of the decision logic — keyboard mapping, selection, the idempotency key,
 * the response body — lives in `lib/clarification.ts` and is unit-tested there.
 * This file owns pixels, focus and ARIA.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import {
  answerSummary,
  buildResponse,
  cardKeyAction,
  optionShortcut,
  rowCount,
  wrapIndex,
  type ClarificationRequest,
  type ClarificationResponse,
  type Selection,
} from '@/lib/clarification';
import { IconCheck, IconCloud, IconPencil, IconX } from './icons';

export interface ClarificationCardProps {
  request: ClarificationRequest;
  /** Submit an answer. The parent owns the send and the dedupe. */
  onSubmit: (response: ClarificationResponse, summary: string) => void;
  /**
   * The user asked to answer in the main composer instead. The card has its own
   * text field, so this is now only a hand-off for anyone who prefers the big
   * input — it is not the only way to type an answer.
   */
  onUseComposer?: () => void;
  /** Dismiss (Escape / the × ). Offered only when a skip is safe. */
  onDismiss?: () => void;
  /** True while the continuation run is starting; the card locks. */
  submitting?: boolean;
  /** Rendered instead of the controls once answered. */
  answeredWith?: string;
}

export function ClarificationCard({
  request,
  onSubmit,
  onUseComposer,
  onDismiss,
  submitting = false,
  answeredWith,
}: ClarificationCardProps) {
  const [active, setActive] = useState(0);
  const [chosen, setChosen] = useState<string[]>([]);
  const [customOpen, setCustomOpen] = useState(false);
  const [customText, setCustomText] = useState('');
  const [sent, setSent] = useState(false);
  const optionRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const customRef = useRef<HTMLButtonElement | null>(null);
  const inputRef = useRef<HTMLTextAreaElement | null>(null);

  const dismissible = Boolean(onDismiss);
  const locked = submitting || sent || Boolean(answeredWith);
  const multi = request.multi_select;
  const rows = rowCount(request.options.length, request.allow_custom);

  const submit = useCallback(
    (selection: Selection) => {
      // Guarded here as well as on the server: a double-click must not even
      // open a second stream. The server's first-response-wins UPDATE is the
      // authority; this only removes the flicker.
      if (locked) return;
      const response = buildResponse(request, selection);
      if (!response) return;
      setSent(true);
      onSubmit(response, answerSummary(request, response));
    },
    [locked, onSubmit, request],
  );

  /** Send whatever is currently ticked, plus anything typed. */
  const confirm = useCallback(() => {
    const typed = customText.trim();
    if (chosen.length === 0 && !typed) return;
    submit({ optionIds: chosen, customText: typed });
  }, [chosen, customText, submit]);

  const pick = useCallback(
    (optionId: string) => {
      if (locked) return;
      if (multi) {
        setChosen((prev) =>
          prev.includes(optionId)
            ? prev.filter((id) => id !== optionId)
            : [...prev, optionId],
        );
        return;
      }
      submit({ optionIds: [optionId] });
    },
    [locked, multi, submit],
  );

  const openCustom = useCallback(() => {
    if (locked) return;
    setCustomOpen(true);
    setActive(request.options.length);
    // Focus after paint: the field does not exist until this render commits.
    window.setTimeout(() => inputRef.current?.focus(), 0);
  }, [locked, request.options.length]);

  const focusRow = useCallback(
    (index: number) => {
      setActive(index);
      if (index < request.options.length) optionRefs.current[index]?.focus();
      else customRef.current?.focus();
    },
    [request.options.length],
  );

  const handleKey = useCallback(
    (event: React.KeyboardEvent) => {
      const action = cardKeyAction(event, {
        optionCount: request.options.length,
        allowCustom: request.allow_custom,
        dismissible,
        typingCustom: customOpen && document.activeElement === inputRef.current,
        multiSelect: multi,
        activeIndex: active,
      });
      if (!action) return;
      event.preventDefault();
      switch (action.kind) {
        case 'move':
          focusRow(wrapIndex(active, action.delta, rows));
          return;
        case 'toggle':
        case 'select': {
          const index = Number(action.optionId);
          setActive(index);
          pick(request.options[index].id);
          return;
        }
        case 'custom':
          openCustom();
          return;
        case 'confirm':
          if (multi) confirm();
          else if (active < request.options.length) {
            pick(request.options[active].id);
          }
          return;
        case 'dismiss':
          onDismiss?.();
      }
    },
    [
      active,
      confirm,
      customOpen,
      dismissible,
      focusRow,
      multi,
      onDismiss,
      openCustom,
      pick,
      request.allow_custom,
      request.options,
      rows,
    ],
  );

  // Reset and take focus when a NEW question appears — a keyboard user should
  // not have to hunt for a control that just interrupted them. Keyed on the id
  // so a re-render never steals focus back mid-answer.
  useEffect(() => {
    setActive(0);
    setChosen([]);
    setCustomOpen(false);
    setCustomText('');
    setSent(false);
    optionRefs.current[0]?.focus();
  }, [request.clarification_id]);

  const shortcuts = useMemo(
    () => request.options.map((_, i) => optionShortcut(i)),
    [request.options],
  );
  const canSend = chosen.length > 0 || customText.trim().length > 0;

  if (answeredWith) {
    return (
      <div className="mt-3 inline-flex max-w-full items-center gap-2 rounded-ts border border-border bg-surface px-3 py-1.5 text-xs text-muted">
        <IconCloud size={13} className="shrink-0 text-accent" />
        <span className="truncate">
          <span className="text-faint">{request.question}</span>{' '}
          <span className="text-ink">{answeredWith}</span>
        </span>
      </div>
    );
  }

  return (
    <div
      onKeyDown={handleKey}
      className="mt-3 overflow-hidden rounded-ts border border-border bg-surface"
      data-testid="clarification-card"
    >
      <div className="flex items-start gap-2 px-3.5 pt-3">
        <IconCloud size={14} className="mt-0.5 shrink-0 text-accent" />
        <div className="min-w-0 flex-1">
          <p className="text-[11px] font-medium uppercase tracking-wide text-faint">
            {request.header}
          </p>
          <p
            id={`clr-${request.clarification_id}`}
            className="mt-0.5 text-sm font-medium text-ink"
          >
            {request.question}
          </p>
        </div>
        {/* Done sits top-right and appears only once there is something to
            send — a button that does nothing is worse than no button. */}
        {multi && canSend && (
          <button
            type="button"
            onClick={confirm}
            disabled={locked}
            className="shrink-0 rounded-md bg-accent-strong px-2.5 py-1 text-xs font-medium text-white transition-all duration-ts hover:brightness-110 disabled:opacity-40"
          >
            Done{chosen.length > 1 ? ` (${chosen.length})` : ''}
          </button>
        )}
        {dismissible && (
          <button
            type="button"
            onClick={onDismiss}
            disabled={locked}
            aria-label="Dismiss this question and answer with the safest reading"
            title="Dismiss (Esc)"
            className="shrink-0 rounded-md p-1 text-faint transition-colors duration-ts hover:bg-surface-2 hover:text-ink disabled:opacity-40"
          >
            <IconX size={13} />
          </button>
        )}
      </div>

      <div
        role={multi ? 'group' : 'radiogroup'}
        aria-labelledby={`clr-${request.clarification_id}`}
        className="mt-2.5 flex flex-col px-2 pb-1"
      >
        {request.options.map((option, index) => {
          const ticked = chosen.includes(option.id);
          const focused = active === index;
          return (
            <button
              key={option.id}
              ref={(el) => {
                optionRefs.current[index] = el;
              }}
              type="button"
              role={multi ? 'checkbox' : 'radio'}
              aria-checked={multi ? ticked : focused}
              // Roving tabindex: the whole list is ONE tab stop and the arrows
              // move within it, which is what a listbox is supposed to do.
              tabIndex={focused ? 0 : -1}
              disabled={locked}
              onClick={() => {
                setActive(index);
                pick(option.id);
              }}
              onFocus={() => setActive(index)}
              className={`group/opt flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-left transition-colors duration-ts focus:outline-none disabled:opacity-50 ${
                ticked
                  ? 'bg-accent/12 text-ink'
                  : focused
                    ? 'bg-surface-2'
                    : 'hover:bg-surface-2'
              }`}
            >
              <span
                aria-hidden
                className={`grid h-[18px] w-[18px] shrink-0 place-items-center rounded text-[10px] font-medium ${
                  ticked
                    ? 'bg-accent-strong text-white'
                    : 'border border-border bg-surface text-faint'
                }`}
              >
                {ticked ? <IconCheck size={11} /> : shortcuts[index]}
              </span>
              <span className="min-w-0 flex-1">
                <span className="block text-sm text-ink">{option.label}</span>
                {option.description && (
                  <span className="mt-0.5 block text-xs text-muted">
                    {option.description}
                  </span>
                )}
              </span>
              {/* The ⏎ affordance marks the row Enter would act on. */}
              {focused && !locked && (
                <span aria-hidden className="shrink-0 text-xs text-faint">
                  ↵
                </span>
              )}
            </button>
          );
        })}

        {request.allow_custom && (
          <button
            ref={customRef}
            type="button"
            tabIndex={active === request.options.length ? 0 : -1}
            disabled={locked}
            aria-expanded={customOpen}
            onClick={openCustom}
            onFocus={() => setActive(request.options.length)}
            className={`flex w-full items-center gap-2.5 rounded-md px-2.5 py-2 text-left transition-colors duration-ts focus:outline-none disabled:opacity-50 ${
              active === request.options.length ? 'bg-surface-2' : 'hover:bg-surface-2'
            }`}
          >
            <span
              aria-hidden
              className="grid h-[18px] w-[18px] shrink-0 place-items-center rounded border border-border bg-surface text-faint"
            >
              <IconPencil size={10} />
            </span>
            <span className="flex-1 text-sm text-muted">Something else</span>
            {onUseComposer && (
              <span
                role="button"
                tabIndex={-1}
                onClick={(event) => {
                  event.stopPropagation();
                  onUseComposer();
                }}
                className="shrink-0 rounded border border-border px-1.5 py-0.5 text-[11px] text-faint transition-colors duration-ts hover:bg-surface hover:text-ink"
              >
                Use composer
              </span>
            )}
          </button>
        )}
      </div>

      {/* The text field lives IN the card. Sending someone to the composer for
          "Something else" meant leaving the question to answer it. */}
      {customOpen && (
        <div className="px-3.5 pb-3">
          <textarea
            ref={inputRef}
            rows={2}
            value={customText}
            onChange={(event) => setCustomText(event.target.value)}
            onKeyDown={(event) => {
              // Plain Enter sends; Shift+Enter is a newline, matching the
              // composer. ⌘/Ctrl+Enter is handled by the card's own map.
              if (event.key === 'Enter' && !event.shiftKey && !event.metaKey) {
                event.preventDefault();
                event.stopPropagation();
                confirm();
              }
            }}
            disabled={locked}
            placeholder={request.custom_placeholder}
            aria-label={request.question}
            className="w-full resize-none rounded-md border border-border bg-bg px-3 py-2 text-sm text-ink outline-none transition-colors duration-ts placeholder:text-faint focus:border-accent disabled:opacity-50"
          />
          <div className="mt-1.5 flex items-center gap-2">
            <button
              type="button"
              onClick={confirm}
              disabled={locked || !canSend}
              className="rounded-md bg-accent-strong px-2.5 py-1 text-xs font-medium text-white transition-all duration-ts hover:brightness-110 disabled:opacity-40"
            >
              Send
            </button>
            <button
              type="button"
              onClick={() => {
                setCustomOpen(false);
                setCustomText('');
                focusRow(0);
              }}
              disabled={locked}
              className="rounded-md px-2 py-1 text-xs text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
            >
              Cancel
            </button>
          </div>
        </div>
      )}

      <div className="flex items-center gap-3 border-t border-border px-3.5 py-1.5 text-[11px] text-faint">
        <span>↑↓ to navigate</span>
        <span aria-hidden>·</span>
        <span>{multi ? 'number keys to select' : 'Enter to select'}</span>
        {multi && (
          <>
            <span aria-hidden>·</span>
            <span>⌘↵ to send</span>
          </>
        )}
        {submitting && (
          <span className="ml-auto text-muted">Continuing your request…</span>
        )}
      </div>

      <span aria-live="polite" className="sr-only">
        {submitting ? 'Answer submitted, continuing your request.' : ''}
      </span>
    </div>
  );
}
