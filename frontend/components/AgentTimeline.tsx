'use client';

/**
 * Agent step timeline (V2 §4e): a live card in the assistant row driven by
 * `step` SSE events — numbered steps with spinner (running), check (done) or
 * cross (failed), each expandable to its detail line. Persisted messages
 * render the same card from meta.steps.
 */

import { useId, useState } from 'react';
import type { AgentStep } from '@/lib/types';
import { IconCheck, IconChevronDown, IconSparkles, IconX } from './icons';

function StatusIcon({ status }: { status: AgentStep['status'] }) {
  if (status === 'running') {
    return (
      <span
        className="ts-spinner shrink-0"
        role="img"
        aria-label="Step running"
      />
    );
  }
  if (status === 'done') {
    return (
      <IconCheck
        size={14}
        className="shrink-0 text-accent"
        aria-label="Step done"
      />
    );
  }
  return (
    <IconX size={14} className="shrink-0 text-danger" aria-label="Step failed" />
  );
}

export function AgentTimeline({ steps }: { steps: AgentStep[] }) {
  const [openIds, setOpenIds] = useState<Set<number>>(new Set());
  const idBase = useId();

  if (steps.length === 0) return null;

  function toggle(id: number) {
    setOpenIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  return (
    <div className="mb-3 rounded-ts border border-border bg-surface/60">
      <p className="flex items-center gap-1.5 border-b border-border px-3 py-1.5 text-[11px] font-medium uppercase tracking-wide text-faint">
        <IconSparkles
          size={12}
          className="text-[color:var(--ts-engine-agent-ink)]"
        />
        Agent plan
      </p>
      <ol aria-label="Agent steps" className="px-1.5 py-1">
        {steps.map((step, i) => {
          const open = openIds.has(step.id);
          const expandable = Boolean(step.detail);
          const row = (
            <span className="flex min-w-0 flex-1 items-center gap-2.5">
              <StatusIcon status={step.status} />
              <span className="w-4 shrink-0 text-right font-mono text-[11px] text-faint">
                {i + 1}
              </span>
              <span
                className={`min-w-0 flex-1 truncate text-left text-sm ${
                  step.status === 'failed' ? 'text-danger' : ''
                } ${step.status === 'running' ? 'text-ink' : ''}`}
              >
                {step.title}
              </span>
            </span>
          );
          return (
            <li key={step.id}>
              {expandable ? (
                <button
                  type="button"
                  onClick={() => toggle(step.id)}
                  aria-expanded={open}
                  aria-controls={`${idBase}-step-${step.id}`}
                  className="flex w-full items-center gap-1.5 rounded-lg px-1.5 py-1.5 text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
                >
                  {row}
                  <IconChevronDown
                    size={12}
                    className={`shrink-0 text-faint transition-transform duration-ts ${
                      open ? 'rotate-180' : ''
                    }`}
                  />
                </button>
              ) : (
                <span className="flex w-full items-center gap-1.5 px-1.5 py-1.5 text-muted">
                  {row}
                </span>
              )}
              {expandable && open && (
                <p
                  id={`${idBase}-step-${step.id}`}
                  className="mx-1.5 mb-1.5 whitespace-pre-wrap rounded-md bg-surface-2/60 px-3 py-2 font-mono text-[12.5px] leading-relaxed text-muted"
                >
                  {step.detail}
                </p>
              )}
            </li>
          );
        })}
      </ol>
    </div>
  );
}
