'use client';

/**
 * "PASTED" attachment chip (V5) — a compact card standing in for a long block
 * of pasted text/code, like ChatGPT's. Click to expand a read-only preview;
 * the composer variant adds a remove button. Purely presentational.
 */

import { useState } from 'react';
import type { PastedText } from '@/lib/types';
import { IconFileText, IconX } from './icons';

const PREVIEW_CAP = 5000;

export function PastedChip({
  pasted,
  onRemove,
}: {
  pasted: PastedText;
  onRemove?: () => void;
}) {
  const [open, setOpen] = useState(false);
  const preview =
    pasted.content.length > PREVIEW_CAP
      ? `${pasted.content.slice(0, PREVIEW_CAP)}\n…`
      : pasted.content;

  return (
    <div className="inline-flex max-w-full flex-col items-start">
      <div className="inline-flex max-w-full items-center gap-2 rounded-2xl border border-border bg-surface-2 py-1.5 pl-1.5 pr-2">
        <span className="grid h-9 w-9 shrink-0 place-items-center rounded-lg bg-bg text-muted">
          <IconFileText size={16} />
        </span>
        <button
          type="button"
          onClick={() => setOpen((o) => !o)}
          aria-expanded={open}
          className="min-w-0 py-0.5 pr-1 text-left"
          title="Show pasted text"
        >
          <span className="block text-xs font-semibold uppercase tracking-wide text-ink">
            Pasted
          </span>
          <span className="block text-[11px] text-faint">
            {pasted.lines.toLocaleString()} line
            {pasted.lines === 1 ? '' : 's'} · {pasted.chars.toLocaleString()}{' '}
            chars
          </span>
        </button>
        {onRemove && (
          <button
            type="button"
            onClick={onRemove}
            aria-label="Remove pasted text"
            className="ml-1 shrink-0 rounded-md p-1 text-faint transition-colors duration-ts hover:bg-border hover:text-ink"
          >
            <IconX size={13} />
          </button>
        )}
      </div>
      {open && (
        <pre className="mt-1 max-h-52 w-full max-w-full overflow-auto whitespace-pre-wrap break-words rounded-xl border border-border bg-bg p-3 text-left font-mono text-xs leading-relaxed text-muted">
          {preview}
        </pre>
      )}
    </div>
  );
}
