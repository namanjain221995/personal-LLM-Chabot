'use client';

/**
 * Agent step timeline (V2 §4e), restyled 2026-08-29 (owner request).
 *
 * WAS: a bordered card headed "AGENT PLAN" with numbered rows — a box sitting
 * in the middle of the conversation, always open, with no sense of time.
 *
 * NOW: the same information as a quiet vertical pipeline, matching how the
 * reasoning accordion above it already behaves. One summary line —
 * "Working…" while steps run, "Worked for 1m 12s" once they finish — that
 * collapses the whole thing, and a rail connecting each step to the next so
 * the sequence reads as a pipeline rather than a list. No border, no
 * background: the chat body is the surface.
 *
 * The elapsed time is measured on the client, exactly like
 * ReasoningAccordion's "Thought for N s": the step events carry no
 * timestamps, and inventing one server-side would be a lie about when the
 * work actually happened in front of this user.
 */

import { useEffect, useId, useRef, useState } from 'react';
import type { AgentStep } from '@/lib/types';
import { IconCheck, IconChevronDown, IconX } from './icons';
import { Loader } from './Loader';
import { formatElapsed } from './ResearchPanel';

function StatusDot({ status }: { status: AgentStep['status'] }) {
  if (status === 'running') return <Loader size={13} label="Step running" />;
  if (status === 'done') {
    return (
      <IconCheck size={13} className="text-accent" aria-label="Step done" />
    );
  }
  return <IconX size={13} className="text-danger" aria-label="Step failed" />;
}

export function AgentTimeline({ steps }: { steps: AgentStep[] }) {
  const [open, setOpen] = useState(true);
  const [openIds, setOpenIds] = useState<Set<number>>(new Set());
  const idBase = useId();

  const running = steps.some((s) => s.status === 'running');

  // Client-measured elapsed time. `startedAt` is set on the first render that
  // has a running step and never reset, so a re-render after completion keeps
  // the total instead of restarting the clock.
  const startedAt = useRef<number | null>(null);
  const [elapsedMs, setElapsedMs] = useState<number | null>(null);

  useEffect(() => {
    if (running && startedAt.current === null) startedAt.current = Date.now();
    if (startedAt.current === null) return undefined;
    if (!running) {
      setElapsedMs(Date.now() - startedAt.current);
      return undefined;
    }
    const tick = () => setElapsedMs(Date.now() - (startedAt.current ?? Date.now()));
    tick();
    const timer = window.setInterval(tick, 1000);
    return () => window.clearInterval(timer);
  }, [running, steps.length]);

  if (steps.length === 0) return null;

  function toggleStep(id: number) {
    setOpenIds((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  }

  // Reloaded transcripts have no client clock; label with the TOTAL step
  // count — counting only 'done' undercounted whenever a step failed, and
  // the list below visibly disagreed with its own summary.
  const summary = running
    ? elapsedMs != null && elapsedMs >= 1000
      ? `Working… ${formatElapsed(elapsedMs)}`
      : 'Working…'
    : elapsedMs != null
      ? `Worked for ${formatElapsed(elapsedMs)}`
      : `${steps.length} step${steps.length === 1 ? '' : 's'}`;

  return (
    <div className="mb-3">
      <button
        type="button"
        onClick={() => setOpen((o) => !o)}
        aria-expanded={open}
        aria-controls={`${idBase}-steps`}
        className="group flex items-center gap-1 rounded-md py-0.5 text-[13px] text-muted transition-colors duration-ts hover:text-ink"
      >
        <span className={running ? 'animate-pulse' : ''}>{summary}</span>
        <IconChevronDown
          size={13}
          className={`text-faint transition-transform duration-ts ${
            open ? '' : '-rotate-90'
          }`}
        />
      </button>

      {open && (
        <ol
          id={`${idBase}-steps`}
          aria-label="Agent steps"
          className="mt-1.5 flex flex-col"
        >
          {steps.map((step, i) => {
            const isLast = i === steps.length - 1;
            const stepOpen = openIds.has(step.id);
            const expandable = Boolean(step.detail);
            return (
              <li key={step.id} className="relative flex gap-2.5 pb-2 last:pb-0">
                {/* The rail: a hairline from this step's marker to the next. */}
                {!isLast && (
                  <span
                    aria-hidden
                    className="absolute left-[6.5px] top-[18px] bottom-0 w-px bg-border"
                  />
                )}
                <span className="relative z-10 mt-[3px] flex h-[13px] w-[13px] shrink-0 items-center justify-center bg-bg">
                  <StatusDot status={step.status} />
                </span>
                <div className="min-w-0 flex-1">
                  {expandable ? (
                    <button
                      type="button"
                      onClick={() => toggleStep(step.id)}
                      aria-expanded={stepOpen}
                      aria-controls={`${idBase}-step-${step.id}`}
                      className={`flex w-full items-center gap-1.5 text-left text-[13.5px] leading-[19px] transition-colors duration-ts hover:text-ink ${
                        step.status === 'failed'
                          ? 'text-danger'
                          : step.status === 'running'
                            ? 'text-ink'
                            : 'text-muted'
                      }`}
                    >
                      <span className="min-w-0 flex-1 truncate">{step.title}</span>
                      <IconChevronDown
                        size={11}
                        className={`shrink-0 text-faint transition-transform duration-ts ${
                          stepOpen ? 'rotate-180' : ''
                        }`}
                      />
                    </button>
                  ) : (
                    <span
                      className={`block truncate text-[13.5px] leading-[19px] ${
                        step.status === 'failed'
                          ? 'text-danger'
                          : step.status === 'running'
                            ? 'text-ink'
                            : 'text-muted'
                      }`}
                    >
                      {step.title}
                    </span>
                  )}
                  {expandable && stepOpen && (
                    <p
                      id={`${idBase}-step-${step.id}`}
                      className="mt-1.5 whitespace-pre-wrap rounded-md bg-surface-2/60 px-3 py-2 font-mono text-[12.5px] leading-relaxed text-muted"
                    >
                      {step.detail}
                    </p>
                  )}
                </div>
              </li>
            );
          })}
        </ol>
      )}
    </div>
  );
}
