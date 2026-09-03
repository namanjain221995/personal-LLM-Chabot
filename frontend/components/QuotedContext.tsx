'use client';

import { IconX } from './icons';
import { previewSelectedText } from '@/lib/selectedContext';
import type { SelectedContext } from '@/lib/types';

/**
 * The excerpt a turn is replying to — the same block in both places it
 * appears: pending above the composer (with a remove button), and permanent
 * above the sent user bubble (without one).
 *
 * One component rather than two so the two views cannot drift into looking
 * like different features. It is deliberately small: a rule, a line of quiet
 * text, no card chrome. The reference is context for the question below it,
 * not a thing in its own right.
 */
export function QuotedContext({
  context,
  onRemove,
  align = 'left',
}: {
  context: SelectedContext;
  /** Present only on the pending reference in the composer. */
  onRemove?: () => void;
  /** The sent bubble hugs the right edge with the rest of the user's turn. */
  align?: 'left' | 'right';
}) {
  return (
    <div
      className={`flex items-start gap-2 rounded-md border-l-2 border-accent/60 bg-surface/70 py-1.5 pl-2.5 pr-1.5 ${
        align === 'right' ? 'ml-auto max-w-full' : ''
      }`}
    >
      <span className="min-w-0 flex-1">
        <span className="block text-[10px] font-medium uppercase tracking-wide text-faint">
          Replying to
        </span>
        {/* Display only — the full excerpt still travels with the request. */}
        <span className="mt-0.5 block truncate text-xs text-muted">
          {previewSelectedText(context.text)}
        </span>
        {context.truncated && (
          <span className="mt-0.5 block text-[10px] text-faint">
            Long selection — only the first part is sent.
          </span>
        )}
      </span>
      {onRemove && (
        <button
          type="button"
          onClick={onRemove}
          aria-label="Remove selected context"
          title="Remove selected context"
          className="shrink-0 rounded-md p-1 text-faint transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
        >
          <IconX size={13} />
        </button>
      )}
    </div>
  );
}
