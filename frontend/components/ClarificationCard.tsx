'use client';

/**
 * The Salesforce clarification panel — a TEMPORARY control attached to the
 * composer, and the quiet record it leaves behind.
 *
 * It used to render as an assistant message in the transcript. That was wrong
 * in two ways at once: an interactive control scrolled away with the history it
 * did not belong to, and the transcript filled up with dead question cards. So
 * there are two exports and they are deliberately different things:
 *
 *   ClarificationCard    the live question. Rendered by the COMPOSER, above the
 *                        input, for exactly as long as the question is
 *                        unanswered. It is not a message and is never stored as
 *                        one.
 *   ClarificationRecord  what the transcript keeps afterwards: one quiet,
 *                        non-interactive line saying what was asked and what
 *                        was chosen, so the user turn that follows it
 *                        ("Interview") reads as the answer it is.
 *
 * Free text is answered in the COMPOSER, not here. A text field in this panel
 * would sit forty pixels above the composer's own — two inputs, one question,
 * and no way to tell which one is listening. "Something else", Escape, and
 * simply starting to type all hand over to the composer instead.
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
   * Answer in the user's own words instead. Focuses the composer and arms it,
   * so the next thing sent resolves THIS question rather than starting a new
   * request. `seed` is the character that triggered the hand-over, when the
   * user simply started typing.
   */
  onUseComposer?: (seed?: string) => void;
  /**
   * Skip (the × ). Submits a "no preference" answer — the question lives on the
   * server and only one may be pending per conversation, so a panel that merely
   * disappeared would block every later question in this chat.
   */
  onSkip?: () => void;
  /** True while the continuation run is starting; the panel locks. */
  submitting?: boolean;
}

export function ClarificationCard({
  request,
  onSubmit,
  onUseComposer,
  onSkip,
  submitting = false,
}: ClarificationCardProps) {
  const [active, setActive] = useState(0);
  const [chosen, setChosen] = useState<string[]>([]);
  const [sent, setSent] = useState(false);
  const optionRefs = useRef<(HTMLButtonElement | null)[]>([]);
  const customRef = useRef<HTMLButtonElement | null>(null);

  const locked = submitting || sent;
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

  /** Send whatever is currently ticked (multi-select only). */
  const confirm = useCallback(() => {
    if (chosen.length === 0) return;
    submit({ optionIds: chosen });
  }, [chosen, submit]);

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
      // Clicking an option IS the submission. Making someone select and then
      // press Send is two actions for one decision.
      submit({ optionIds: [optionId] });
    },
    [locked, multi, submit],
  );

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
        typingCustom: false,
        multiSelect: multi,
        activeIndex: active,
      });
      if (!action) return;
      event.preventDefault();
      // The chat surface maps a bare Escape to "stop streaming". A question is
      // not a stream, and leaving one must not abort anything.
      event.stopPropagation();
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
          onUseComposer?.();
          return;
        case 'confirm':
          if (multi) confirm();
          else if (active < request.options.length) {
            pick(request.options[active].id);
          }
          return;
        case 'leave':
          // Escape, or the user simply started typing their own answer.
          onUseComposer?.(action.text);
      }
    },
    [
      active,
      confirm,
      focusRow,
      multi,
      onUseComposer,
      pick,
      request.allow_custom,
      request.options,
      rows,
    ],
  );

  // Reset when a NEW question replaces this one.
  useEffect(() => {
    setActive(0);
    setChosen([]);
    setSent(false);
  }, [request.clarification_id]);

  // Take focus ONCE, when a question arrives, so the number keys work the
  // moment it appears. Safe only because any other printable key hands
  // straight back to the composer (see CardAction 'leave') — otherwise this
  // would silently swallow the first letter of a typed answer.
  const grabbed = useRef('');
  useEffect(() => {
    if (locked || grabbed.current === request.clarification_id) return;
    grabbed.current = request.clarification_id;
    optionRefs.current[0]?.focus({ preventScroll: true });
  }, [locked, request.clarification_id]);

  const shortcuts = useMemo(
    () => request.options.map((_, i) => optionShortcut(i)),
    [request.options],
  );

  return (
    <div
      onKeyDown={handleKey}
      className="border-b border-border px-3.5 pb-2 pt-2.5"
      data-testid="clarification-card"
    >
      <div className="flex flex-wrap items-start gap-2">
        <IconCloud size={14} className="mt-0.5 shrink-0 text-accent" />
        <div className="min-w-0 flex-1 basis-48">
          {/* A TOPIC ("Mock count"), not a source. As a constant "SALESFORCE"
              it told the reader nothing the pill above had not already said. */}
          <p className="text-[11px] font-medium uppercase tracking-wide text-faint">
            Clarification{request.header ? ` · ${request.header}` : ''}
          </p>
          <p
            id={`clr-${request.clarification_id}`}
            className="mt-0.5 text-sm font-medium text-ink"
          >
            {request.question}
          </p>
        </div>
        {/* Done appears only once there is something to send — a button that
            does nothing is worse than no button. */}
        {multi && chosen.length > 0 && (
          <button
            type="button"
            onClick={confirm}
            disabled={locked}
            className="shrink-0 rounded-md bg-accent-strong px-2.5 py-1 text-xs font-medium text-white transition-all duration-ts hover:brightness-110 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-40"
          >
            Done{chosen.length > 1 ? ` (${chosen.length})` : ''}
          </button>
        )}
        {onSkip && (
          <button
            type="button"
            onClick={onSkip}
            disabled={locked}
            aria-label="Skip this question and answer with the safest reading"
            title="Skip"
            className="shrink-0 rounded-md p-1 text-faint transition-colors duration-ts hover:bg-surface-2 hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-40"
          >
            <IconX size={13} />
          </button>
        )}
      </div>

      <div
        role={multi ? 'group' : 'radiogroup'}
        aria-labelledby={`clr-${request.clarification_id}`}
        className="mt-1.5 flex flex-col"
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
              // Selection, never focus: tracking the focus ring announced each
              // row in turn as "selected" to a screen reader.
              aria-checked={ticked}
              // Roving tabindex: the whole list is ONE tab stop and the arrows
              // move within it.
              tabIndex={focused ? 0 : -1}
              disabled={locked}
              onClick={() => {
                setActive(index);
                pick(option.id);
              }}
              onFocus={() => setActive(index)}
              className={`group/opt flex w-full items-center gap-2.5 rounded-md px-2 py-1.5 text-left transition-colors duration-ts focus:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50 ${
                ticked
                  ? 'bg-accent/10 text-ink'
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
                    : 'border border-border bg-bg text-faint'
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
            onClick={() => onUseComposer?.()}
            onFocus={() => setActive(request.options.length)}
            className={`flex w-full items-center gap-2.5 rounded-md px-2 py-1.5 text-left transition-colors duration-ts focus:outline-none focus-visible:ring-2 focus-visible:ring-accent disabled:opacity-50 ${
              active === request.options.length ? 'bg-surface-2' : 'hover:bg-surface-2'
            }`}
          >
            <span
              aria-hidden
              className="grid h-[18px] w-[18px] shrink-0 place-items-center rounded border border-border bg-bg text-faint"
            >
              <IconPencil size={10} />
            </span>
            <span className="flex-1 text-sm text-muted">Something else</span>
          </button>
        )}
      </div>

      {/* Hints wrap rather than overflow: at 320px the row used to push wider
          than the panel. Hidden from assistive tech — the keyboard map is
          conveyed by the roles, and read aloud these are fragments between the
          question and its answers. */}
      <div
        aria-hidden
        className="mt-1 flex flex-wrap items-center gap-x-3 gap-y-0.5 text-[11px] text-faint"
      >
        <span>↑↓ to navigate</span>
        <span>{multi ? 'number keys to select' : 'Enter to select'}</span>
        {multi && <span>⌘↵ to send</span>}
        <span>or just type your answer</span>
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

/**
 * What the transcript keeps once a question has been answered.
 *
 * Deliberately not a disabled card: a disabled card still reads as something
 * you might be able to use, and a thread full of them is a thread full of dead
 * controls. One line, no controls, no roles — it exists so the user turn after
 * it ("Interview") reads as the answer to something rather than a non sequitur.
 */
export function ClarificationRecord({
  question,
  answer,
}: {
  question: string;
  answer: string;
}) {
  return (
    <div className="mt-3 flex max-w-full items-center gap-2 rounded-ts border border-border bg-surface px-3 py-1.5 text-xs text-muted">
      <IconCheck size={13} className="shrink-0 text-accent" />
      <span className="min-w-0 truncate">
        <span className="text-faint">{question}</span>{' '}
        <span className="text-ink">{answer}</span>
      </span>
    </div>
  );
}
