'use client';

/**
 * Context meter (Phase C) — a small ring in the composer that fills as the
 * session's context fills, next to the send button.
 *
 * Gray under 60%, amber 60–84%, red at 85%+, with a gentle pulse from 95%.
 * Click for a breakdown and a "Compact now" action.
 *
 * The popover is portalled to <body>: the composer is inside transformed
 * ancestors, which would otherwise become the containing block for
 * position:fixed and mis-place it (the bug that hit the ⋯ menu).
 */

import { useEffect, useLayoutEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import {
  breakdownTotal,
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
}

export function ContextMeter({
  view,
  compacting,
  onCompactNow,
  compactDisabled = false,
}: ContextMeterProps) {
  const [open, setOpen] = useState(false);
  const [pos, setPos] = useState<{ left: number; bottom: number } | null>(null);
  const buttonRef = useRef<HTMLButtonElement>(null);
  const color = meterColor(view.state);

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
        setOpen(false);
      }
    }
    function onDown(e: PointerEvent) {
      if (!buttonRef.current?.contains(e.target as Node)) setOpen(false);
    }
    document.addEventListener('keydown', onKey);
    document.addEventListener('pointerdown', onDown);
    return () => {
      document.removeEventListener('keydown', onKey);
      document.removeEventListener('pointerdown', onDown);
    };
  }, [open]);

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
        onClick={() => setOpen((v) => !v)}
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
            <button
              type="button"
              onClick={() => {
                setOpen(false);
                onCompactNow();
              }}
              disabled={compactDisabled || compacting}
              className="mt-3 w-full rounded-lg border border-border px-2.5 py-1.5 text-xs font-medium transition-colors duration-ts hover:bg-surface-2 disabled:cursor-not-allowed disabled:opacity-40"
            >
              Compact now
            </button>
          </div>,
          document.body,
        )}
    </>
  );
}
