'use client';

/**
 * Context meter (Phase C) — a small ring in the composer that fills as the
 * session's context fills, next to the send button.
 *
 * Gray under 60%, amber 60–84%, red at 85%+, with a gentle pulse from 95%.
 * Click for a breakdown and a "Compact now" action.
 *
 * The compact action is only as honest as what it knows: `foldableTurns` is
 * the server's count of what a compaction would fold RIGHT NOW, so the button
 * can say what it will do — or go dead with a reason — instead of looking
 * equally clickable on a conversation with nothing older to fold. Undefined
 * or null means the count is unknown, and unknown keeps the old behaviour.
 *
 * The popover is portalled to <body>: the composer is inside transformed
 * ancestors, which would otherwise become the containing block for
 * position:fixed and mis-place it (the bug that hit the ⋯ menu).
 */

import {
  useCallback,
  useEffect,
  useLayoutEffect,
  useRef,
  useState,
} from 'react';
import { createPortal } from 'react-dom';
import {
  breakdownTotal,
  compactPlan,
  meterColor,
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
  /** Turns the last successful compaction folded, for the lasting line. */
  lastFoldedTurns?: number | null;
  /** Fired when the popover opens or closes, so the host can fetch lazily. */
  onOpenChange?: (open: boolean) => void;
  /** Opens the existing SummaryPanel — this component builds no new surface. */
  onSeeSummary?: () => void;
}

export function ContextMeter({
  view,
  compacting,
  onCompactNow,
  compactDisabled = false,
  foldableTurns = null,
  lastFoldedTurns = null,
  onOpenChange,
  onSeeSummary,
}: ContextMeterProps) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ left: number; bottom: number } | null>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const popoverRef = useRef<HTMLDivElement>(null);
  const color = meterColor(view.state);

  // Every word and every enabled/disabled decision comes from the lib module,
  // so the wording is tested in node rather than asserted against a DOM.
  const plan = compactPlan({
    foldable: foldableTurns ?? null,
    lastFolded: lastFoldedTurns,
    compacting,
    blocked: compactDisabled,
  });

  // Notifying on every open/close keeps the fetch lazy: the host asks the
  // server what is foldable when the popover opens, never while typing. Held
  // in a ref so the identity of `setOpenAnd` is stable and the Escape /
  // outside-click effect below does not re-subscribe on every render.
  const onOpenChangeRef = useRef(onOpenChange);
  onOpenChangeRef.current = onOpenChange;
  const setOpenAnd = useCallback((next: boolean) => {
    setOpen(next);
    onOpenChangeRef.current?.(next);
  }, []);

  useLayoutEffect(() => {
    if (!open) return;
    const rect = buttonRef.current?.getBoundingClientRect();
    if (rect) {
      setPos({
        left: Math.max(12, rect.right - 280),
        bottom: window.innerHeight - rect.top + 8,
      });
    }
  }, [open]);

  useEffect(() => {
    if (!open) return;
    function onKey(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        // Consume it: while streaming, ChatApp maps a bare Escape to "stop
        // generating" — closing this popover must not also kill the answer.
        e.preventDefault();
        e.stopPropagation();
        setOpenAnd(false);
      }
    }
    function onDown(e: PointerEvent) {
      // The popover is PORTALLED, so it is not inside buttonRef — without
      // checking it too, a pointerdown on the popover's own controls closed
      // the popover before the click could land, and "Compact now" could not
      // be pressed at all.
      const target = e.target as Node;
      if (
        !buttonRef.current?.contains(target) &&
        !popoverRef.current?.contains(target)
      ) {
        setOpenAnd(false);
      }
    }
    document.addEventListener('keydown', onKey);
    document.addEventListener('pointerdown', onDown);
    return () => {
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('pointerdown', onDown);
    };
  }, [open, setOpenAnd]);

  const dash = CIRCUMFERENCE * Math.min(1, Math.max(0, view.fraction));
  const rows = view.breakdown;

  return (
    <>
      {compacting && (
        <span
          role="status"
          className="inline-flex shrink-0 items-center gap-1.5 rounded-full border border-border bg-surface-2 px-2 py-1 text-[11px] text-muted"
        >
          <span
            aria-hidden
            className="inline-block h-3 w-3 animate-spin rounded-full border-2 border-muted/40 border-t-accent"
          />
          Compacting conversation…
        </span>
      )}
      <button
        ref={buttonRef}
        type="button"
        onClick={() => setOpenAnd(!open)}
        aria-label={`Context ${view.percent}% used`}
        aria-expanded={open}
        title={`Context ${view.percent}% used`}
        className="inline-flex shrink-0 items-center gap-1.5 rounded-lg px-1.5 py-1 text-muted transition-colors duration-ts hover:bg-surface-2"
      >
        <svg
          width={SIZE}
          height={SIZE}
          viewBox={`0 0 ${SIZE} ${SIZE}`}
          aria-hidden
          className={view.pulsing ? 'ctx-pulse' : undefined}
        >
          <circle
            cx={SIZE / 2}
            cy={SIZE / 2}
            r={RADIUS}
            fill="none"
            stroke="var(--ts-border)"
            strokeWidth={STROKE}
          />
          {/* Starts at 12 o'clock and fills clockwise. */}
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
          />
        </svg>
        <span className="font-mono text-[11px] tabular-nums" style={{ color }}>
          {view.percent}%
        </span>
      </button>

      {open &&
        pos &&
        typeof document !== 'undefined' &&
        createPortal(
          <div
            ref={popoverRef}
            role="dialog"
            aria-label="Context usage"
            className="fixed z-[60] w-[280px] rounded-ts border border-border bg-surface p-3 shadow-2xl"
            style={{ left: pos.left, bottom: pos.bottom }}
            onClick={(e) => e.stopPropagation()}
          >
            <p className="mb-2 text-xs text-muted">
              How much of the model&apos;s context this chat will use on your
              next message.
            </p>
            <ul className="space-y-1.5">
              {rows.map((row) => {
                const total = breakdownTotal(rows) || 1;
                return (
                  <li key={row.label}>
                    <div className="flex items-baseline justify-between gap-2 text-xs">
                      <span className="text-muted">
                        {row.label}
                        {/* Say why this one does not add to the total, rather
                            than leaving the arithmetic looking broken. */}
                        {row.heldBack && (
                          <span className="ml-1 text-faint">(held back)</span>
                        )}
                      </span>
                      <span className="font-mono tabular-nums text-faint">
                        {row.tokens.toLocaleString()}
                      </span>
                    </div>
                    <div className="mt-0.5 h-1 overflow-hidden rounded-full bg-surface-2">
                      <div
                        className="h-full rounded-full"
                        style={{
                          width: `${Math.round((row.tokens / total) * 100)}%`,
                          background: color,
                        }}
                      />
                    </div>
                  </li>
                );
              })}
            </ul>
            <div className="mt-2.5 flex items-baseline justify-between border-t border-border pt-2 text-xs">
              <span className="text-muted">This message uses</span>
              <span className="font-mono tabular-nums text-ink">
                {breakdownTotal(rows).toLocaleString()} /{' '}
                {view.usableBudget.toLocaleString()}
              </span>
            </div>
            {/* The popover deliberately stays OPEN on compact: the result is
                a line in here, and closing would hide the one lasting piece
                of feedback this action produces. */}
            <button
              type="button"
              onClick={onCompactNow}
              disabled={plan.disabled}
              aria-describedby={plan.hint ? 'ctx-compact-hint' : undefined}
              className="mt-3 w-full rounded-lg border border-border px-2.5 py-1.5 text-xs font-medium transition-colors duration-ts hover:bg-surface-2 disabled:cursor-not-allowed disabled:opacity-40 disabled:hover:bg-transparent"
            >
              {plan.label}
            </button>
            {plan.hint && (
              <p
                id="ctx-compact-hint"
                className="mt-1.5 text-[11px] leading-snug text-faint"
              >
                {plan.hint}
              </p>
            )}
            {plan.folded && (
              <div className="mt-2.5 border-t border-border pt-2">
                <p className="text-[11px] text-muted">{plan.folded}</p>
                {plan.showSummaryLink && onSeeSummary && (
                  <button
                    type="button"
                    onClick={() => {
                      setOpenAnd(false);
                      onSeeSummary();
                    }}
                    className="mt-1 text-[11px] font-medium text-accent underline underline-offset-2 transition-colors duration-ts hover:text-ink"
                  >
                    {plan.summaryLabel}
                  </button>
                )}
              </div>
            )}
          </div>,
          document.body,
        )}
    </>
  );
}
