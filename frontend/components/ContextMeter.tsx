'use client';

/**
 * Context meter — a small ring in the composer that fills as the session's
 * context fills, and IS the manual compact control.
 *
 * WHAT THIS REPLACED (owner request 2026-09-02). The ring used to sit beside a
 * permanent `50%` label, carry a native `title` tooltip, and open a 280px
 * portalled popover on click: an explanatory paragraph, a "Messages and
 * context" row, a "Reserved for reply (held back)" row, a `5,235 / 991,296`
 * total, three progress bars and a separate "Compact now" button under them.
 * All of that is gone. Raw token counts were never something anyone acted on,
 * and a whole dialog to reach one button made the one action it offered the
 * hardest thing in the composer to press.
 *
 * Now: the ring alone. Hover or focus it for a percentage and what a click
 * will do; click it to compact. No popover, no second button, no token counts,
 * and no `title` — a native tooltip beside a custom one is two tooltips saying
 * the same thing at different moments in different places.
 *
 * The numbers still EXIST — `view.breakdown`, `view.tokensUsed` and
 * `view.usableBudget` arrive on every render and are what `view.fraction` is
 * computed from. They are simply never drawn.
 *
 * Two deliberate choices worth keeping:
 *
 * - The tooltip is `position: absolute` inside this component rather than a
 *   portal. The old popover was portalled because `position: fixed` inside the
 *   composer's transformed ancestors resolved against the wrong containing
 *   block; an absolutely-positioned child of a `relative` parent has no such
 *   problem, and `AttachMenu` already opens upward from this exact row.
 * - When activation would do nothing, the button carries `aria-disabled` and
 *   returns early rather than the `disabled` attribute. A disabled button
 *   leaves the tab order and stops firing hover/focus, so the one state that
 *   most needs explaining would be the one state unable to explain itself.
 */

import { useCallback, useId, useRef, useState } from 'react';
import {
  meterColor,
  meterTooltip,
  type MeterView,
} from '@/lib/contextMeter';

const SIZE = 18;
const STROKE = 2.5;
const RADIUS = (SIZE - STROKE) / 2;
const CIRCUMFERENCE = 2 * Math.PI * RADIUS;

interface ContextMeterProps {
  view: MeterView;
  compacting: boolean;
  onCompactNow: () => void;
  compactDisabled?: boolean;
  /** Turns a compaction would fold now. null/undefined = not known yet. */
  foldableTurns?: number | null;
  /**
   * The tooltip opened or closed, so the host can fetch the foldable count
   * lazily. Previously fired by the popover; hover/focus is its analogue —
   * the count is still never fetched while someone is only typing.
   */
  onOpenChange?: (open: boolean) => void;
}

export function ContextMeter({
  view,
  compacting,
  onCompactNow,
  compactDisabled = false,
  foldableTurns = null,
  onOpenChange,
}: ContextMeterProps) {
  const [shown, setShown] = useState(false);
  const tipId = useId();
  const color = meterColor(view.state);

  // Every word, and the inert/live decision, comes from the lib module so the
  // wording is asserted in node rather than scraped out of a rendered DOM.
  const tip = meterTooltip({
    percent: view.percent,
    foldable: foldableTurns ?? null,
    compacting,
    blocked: compactDisabled,
  });

  // Held in a ref so `setShownAnd` keeps a stable identity across renders.
  const onOpenChangeRef = useRef(onOpenChange);
  onOpenChangeRef.current = onOpenChange;
  const setShownAnd = useCallback((next: boolean) => {
    setShown((prev) => {
      // Only a real transition notifies: sweeping a pointer over the ring
      // must not fire a request per mousemove.
      if (prev !== next) onOpenChangeRef.current?.(next);
      return next;
    });
  }, []);

  function activate() {
    // Three layers guard a double request — this one, ChatApp's `isCompacting`
    // check, and `lib/compact`'s per-conversation in-flight set. This is the
    // cheapest and the only one that can also keep the tooltip honest.
    if (tip.inert) return;
    onCompactNow();
  }

  const dash = CIRCUMFERENCE * Math.min(1, Math.max(0, view.fraction));

  return (
    <span className="relative inline-flex shrink-0 items-center">
      {/* The old design announced this with a visible "Compacting
          conversation…" pill beside the ring, which also shoved the whole
          controls row sideways for the duration. The ring now spins instead —
          but a spinner is `aria-hidden` decoration, so the announcement it
          replaced is kept here rather than dropped. */}
      {compacting && (
        <span role="status" className="sr-only">
          Compacting conversation…
        </span>
      )}
      <button
        type="button"
        onClick={activate}
        onMouseEnter={() => setShownAnd(true)}
        onMouseLeave={() => setShownAnd(false)}
        onFocus={() => setShownAnd(true)}
        onBlur={() => setShownAnd(false)}
        onKeyDown={(e) => {
          // Dismiss the tooltip without letting Escape reach ChatApp's
          // window-level shortcut, which maps a bare Escape to "stop
          // generating" while an answer is streaming.
          if (e.key === 'Escape' && shown) {
            e.preventDefault();
            e.stopPropagation();
            setShownAnd(false);
          }
        }}
        aria-disabled={tip.inert || undefined}
        aria-label={tip.ariaLabel}
        aria-describedby={shown ? tipId : undefined}
        /* No `title`: the browser's own tooltip would duplicate the one below,
           at a different moment and in a different place. */
        className="inline-flex shrink-0 items-center rounded-lg p-1.5 text-muted transition-colors duration-ts hover:bg-surface-2 focus-visible:outline focus-visible:outline-2 focus-visible:outline-offset-2 focus-visible:outline-accent aria-disabled:cursor-default"
      >
        <svg
          width={SIZE}
          height={SIZE}
          viewBox={`0 0 ${SIZE} ${SIZE}`}
          aria-hidden
          className={
            compacting ? 'animate-spin' : view.pulsing ? 'ctx-pulse' : undefined
          }
        >
          <circle
            cx={SIZE / 2}
            cy={SIZE / 2}
            r={RADIUS}
            fill="none"
            stroke="var(--ts-border)"
            strokeWidth={STROKE}
          />
          {/* Starts at 12 o'clock and fills clockwise. `dash` is clamped to
              [0, circumference] above, so a fraction over 1 draws a full ring
              rather than wrapping around it a second time. */}
          <circle
            cx={SIZE / 2}
            cy={SIZE / 2}
            r={RADIUS}
            fill="none"
            stroke={color}
            strokeWidth={STROKE}
            strokeLinecap="round"
            strokeDasharray={`${dash} ${CIRCUMFERENCE}`}
            transform={`rotate(-90 ${SIZE / 2} ${SIZE / 2})`}
            style={{ transition: 'stroke-dasharray 400ms ease, stroke 200ms ease' }}
            data-testid="ctx-ring"
          />
        </svg>
      </button>

      {shown && (
        /* Opens upward and right-aligned, like the "+" menu two controls over.
           `pointer-events-none` so it can never sit between the pointer and
           the button that owns it — a tooltip that swallows its own hover
           flickers forever. */
        <span
          id={tipId}
          role="tooltip"
          className="pointer-events-none absolute bottom-full right-0 z-30 mb-2 w-max max-w-[220px] rounded-ts border border-border bg-surface px-2.5 py-1.5 text-left shadow-xl"
        >
          <span className="block text-xs font-medium text-ink">
            {tip.heading}
          </span>
          {tip.action && (
            <span className="mt-0.5 block text-[11px] leading-snug text-muted">
              {tip.action}
            </span>
          )}
        </span>
      )}
    </span>
  );
}
