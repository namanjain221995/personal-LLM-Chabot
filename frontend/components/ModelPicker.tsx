'use client';

/**
 * Composer effort picker.
 *
 * There is ONE model (Qwen3.6-35B-A3B), so this is no longer a model chooser —
 * it chooses how much work that model is allowed to do. The old separate
 * "Fast" model entry is gone; Fast is now the lowest of four levels:
 *
 *   Fast    answer directly — no reasoning pass, no tools
 *   Low     no reasoning, but may search the web if the question needs it
 *   Medium  thinks first; may plan multi-step work and search the web
 *   High    same tools as Medium, with a longer reasoning pass
 *
 * Each level is a CEILING: the orchestrator may use less than it allows, never
 * more (orchestrator/app/engines/orchestrate.py).
 */

import { useEffect, useRef, useState } from 'react';
import type { ModelChoice, ReasoningEffort } from '@/lib/types';
import { IconCheck, IconChevronDown } from './icons';

const EFFORTS: ReasoningEffort[] = ['fast', 'low', 'medium', 'high'];

const EFFORT_LABEL: Record<ReasoningEffort, string> = {
  fast: 'Fast',
  low: 'Low',
  medium: 'Medium',
  high: 'High',
};

const EFFORT_SHORT: Record<ReasoningEffort, string> = {
  fast: 'Fast',
  low: 'Low',
  medium: 'Med',
  high: 'High',
};

/** What each level actually does — shown so the trade-off is never a guess. */
const EFFORT_HELP: Record<ReasoningEffort, string> = {
  fast: 'Answers straight away · no thinking, no tools',
  low: 'No thinking · searches the web only if the question needs it',
  medium: 'Thinks first · plans multi-step work and searches when useful',
  high: 'Thinks longest · same tools as Medium for the hardest tasks',
};

export function ModelPicker({
  model,
  effort,
  onChange,
}: {
  model: ModelChoice;
  effort: ReasoningEffort;
  onChange: (model: ModelChoice, effort: ReasoningEffort) => void;
}) {
  const [open, setOpen] = useState(false);
  const rootRef = useRef<HTMLDivElement>(null);
  const chipRef = useRef<HTMLButtonElement>(null);

  // Close on outside click / Escape while open.
  useEffect(() => {
    if (!open) return;
    function onPointerDown(e: PointerEvent) {
      if (!rootRef.current?.contains(e.target as Node)) setOpen(false);
    }
    function onKeyDown(e: KeyboardEvent) {
      if (e.key === 'Escape') {
        setOpen(false);
        chipRef.current?.focus();
      }
    }
    document.addEventListener('pointerdown', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    return () => {
      document.removeEventListener('pointerdown', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
    };
  }, [open]);

  function pick(nextEffort: ReasoningEffort) {
    // One model now — `model` stays "smart" and only the level changes.
    onChange('smart', nextEffort);
    setOpen(false);
    chipRef.current?.focus();
  }

  void model; // kept in the props for callers/back-compat

  return (
    <div ref={rootRef} className="relative">
      <button
        ref={chipRef}
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-haspopup="menu"
        aria-expanded={open}
        aria-label={`Effort: ${EFFORT_LABEL[effort]}`}
        title="Choose how much work the model does"
        className="inline-flex items-center gap-1 rounded-full border border-border px-2.5 py-1 text-xs font-medium text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
      >
        {EFFORT_SHORT[effort]}
        <IconChevronDown
          size={12}
          className={`transition-transform duration-ts ${open ? 'rotate-180' : ''}`}
        />
      </button>

      {open && (
        <div
          role="menu"
          aria-label="Effort"
          className="absolute bottom-full left-0 z-30 mb-2 w-[288px] rounded-ts border border-border bg-surface p-1 shadow-xl"
        >
          <p className="px-2.5 pb-1 pt-1.5 text-[11px] font-medium uppercase tracking-wide text-faint">
            How hard should it work?
          </p>
          {EFFORTS.map((e) => {
            const active = effort === e;
            return (
              <button
                key={e}
                type="button"
                role="menuitemradio"
                aria-checked={active}
                onClick={() => pick(e)}
                className="w-full rounded-lg px-2.5 py-2 text-left transition-colors duration-ts hover:bg-surface-2"
              >
                <span className="flex items-center gap-2">
                  <span className="text-sm font-medium">{EFFORT_LABEL[e]}</span>
                  {active && (
                    <IconCheck size={14} className="ml-auto text-accent" />
                  )}
                </span>
                <span className="mt-0.5 block text-xs text-muted">
                  {EFFORT_HELP[e]}
                </span>
              </button>
            );
          })}
        </div>
      )}
    </div>
  );
}
