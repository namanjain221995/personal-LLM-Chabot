'use client';

/**
 * The non-content states of the side panel, in one place so they cannot
 * drift: a loading skeleton, "still rendering" with the truthful stage,
 * failed-with-retry, unavailable, and permission denied. Every one names the
 * situation in a sentence a person can act on; none of them guesses at a
 * cause the browser cannot see (a 404 may be "deleted", "not yours" or
 * "never existed", and the server answers all three identically on purpose).
 */

import { IconAlert, IconRefresh } from '../icons';
import { Loader } from '../Loader';

export type ViewerStateKind =
  | 'loading'
  | 'rendering'
  | 'failed'
  | 'unavailable'
  | 'denied'
  | 'empty';

/** Which state a refused request maps to. */
export function stateForStatus(status: number): ViewerStateKind {
  if (status === 401 || status === 403) return 'denied';
  if (status === 404 || status === 410) return 'unavailable';
  return 'failed';
}

/** A grey page-shaped block, three of them — what a preview looks like before it exists. */
function Skeleton() {
  return (
    <div className="flex flex-col items-center gap-4 p-6" aria-hidden>
      {[0, 1, 2].map((i) => (
        <div
          key={i}
          className="aspect-[1/1.3] w-full max-w-[520px] animate-pulse rounded-md border border-border bg-surface-2"
        />
      ))}
    </div>
  );
}

export function ViewerState({
  kind,
  message,
  detail,
  onRetry,
}: {
  kind: ViewerStateKind;
  /** Overrides the default headline for the state. */
  message?: string;
  /** A second line — the stage title while rendering, the server's sentence on failure. */
  detail?: string | null;
  onRetry?: () => void;
}) {
  // Loading and rendering are NOT live regions here: the panel's own
  // announcer already speaks them, and two regions saying "loading" is the
  // double-announcement screen-reader users complain about. The refusal
  // states below are live, because a viewer nested in the panel (the sheet
  // grid) can fail on its own after the panel has said "ready".
  if (kind === 'loading') {
    return (
      <div aria-busy="true" className="h-full">
        <span className="sr-only">{message ?? 'Loading the preview…'}</span>
        <Skeleton />
      </div>
    );
  }
  if (kind === 'rendering') {
    return (
      <div className="flex h-full flex-col items-center justify-center gap-3 p-8 text-center">
        <Loader size={28} />
        <p className="text-sm font-medium text-ink">{message ?? 'Still being generated'}</p>
        {detail && <p className="text-xs text-muted">{detail}</p>}
        <p className="text-xs text-faint">The preview appears here as soon as the file is ready.</p>
      </div>
    );
  }
  const headline =
    message ??
    (kind === 'denied'
      ? 'You do not have access to this file.'
      : kind === 'unavailable'
        ? 'This file is no longer available.'
        : kind === 'empty'
          ? 'There is nothing to preview for this file.'
          : 'The preview could not be loaded.');
  return (
    <div
      role="status"
      aria-live="polite"
      className="flex h-full flex-col items-center justify-center gap-3 p-8 text-center"
    >
      <IconAlert size={22} className={kind === 'failed' ? 'text-danger' : 'text-muted'} />
      <p className="text-sm font-medium text-ink">{headline}</p>
      {detail && <p className="max-w-[40ch] text-xs text-muted">{detail}</p>}
      {kind === 'failed' && onRetry && (
        <button
          type="button"
          onClick={onRetry}
          className="mt-1 inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-2.5 py-1 text-xs font-medium transition-colors duration-ts hover:bg-surface-2"
        >
          <IconRefresh size={13} />
          Try again
        </button>
      )}
    </div>
  );
}
