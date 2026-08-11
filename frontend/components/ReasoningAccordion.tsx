'use client';

/**
 * Reasoning accordion (V2 §4d).
 *
 * While `reasoning` events stream: a collapsed row above the answer reading
 * "Thinking…" (shimmer) with a live preview of the LAST reasoning line;
 * click to expand the full text. Once answer tokens start (or the message is
 * done) the label becomes "Thought for N s" (client-measured) and stays
 * collapsed by default. The text itself renders mono-ish and muted.
 */

import { useId, useState } from 'react';
import { IconBulb, IconChevronDown } from './icons';
import { Loader } from './Loader';

function lastLine(text: string): string {
  const lines = text.split('\n');
  for (let i = lines.length - 1; i >= 0; i--) {
    const line = lines[i].trim();
    if (line) return line;
  }
  return '';
}

export function ReasoningAccordion({
  text,
  seconds,
  thinking,
}: {
  text: string;
  seconds?: number;
  /** True while reasoning is still streaming and no answer token arrived. */
  thinking: boolean;
}) {
  const [open, setOpen] = useState(false);
  const panelId = useId();

  const label = thinking
    ? 'Thinking…'
    : seconds != null
      ? `Thought for ${seconds} s`
      : 'Thought process';
  const preview = thinking && !open ? lastLine(text) : '';

  return (
    <div className="mb-2">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        aria-controls={panelId}
        className="group/reason flex max-w-full items-center gap-1.5 rounded-md px-1 py-0.5 text-xs text-muted transition-colors duration-ts hover:text-ink"
      >
        {/* Thinking is work in progress, so it gets the same indicator as
            every other kind; a finished thought keeps the quiet bulb. */}
        {thinking ? (
          <Loader size={16} className="-my-0.5" />
        ) : (
          <IconBulb size={13} className="shrink-0 text-faint" />
        )}
        <span className="shrink-0 font-medium">{label}</span>
        {preview && (
          <span
            aria-hidden
            className="min-w-0 max-w-[340px] truncate text-faint"
          >
            {preview}
          </span>
        )}
        <IconChevronDown
          size={12}
          className={`shrink-0 text-faint transition-transform duration-ts ${
            open ? 'rotate-180' : ''
          }`}
        />
      </button>

      {open && (
        <div
          id={panelId}
          className="mt-1.5 max-h-64 overflow-y-auto whitespace-pre-wrap border-l-2 border-border pl-3 font-mono text-[12.5px] leading-relaxed text-muted"
        >
          {text}
          {thinking && <span aria-hidden className="stream-caret" />}
        </div>
      )}
    </div>
  );
}
