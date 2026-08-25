/**
 * The central error state: a 404-style page inside the existing app shell.
 *
 * It replaces the thread area only. Sidebar, header and composer stay exactly
 * where they were, so the app never looks like it navigated away — the user
 * can still open another chat or type, and the failed message is still in
 * history behind this page.
 *
 * The props are the whole contract: `error` is a ClientError, which HAS no
 * field for an upstream body, a URL, a request id or an exception type. That
 * is deliberate — this component cannot leak internals because it is never
 * given any. Engineers get the real cause from the server log instead
 * (lib/serverLog.ts).
 */
'use client';

import type { ClientError } from '@/lib/errorTypes';
import { IconAlert, IconRefresh } from './icons';

export default function ChatErrorPage({
  error,
  onRetry,
  onReturn,
}: {
  error: ClientError;
  /** Re-send the failed turn. Omit to hide the button. */
  onRetry?: () => void;
  onReturn: () => void;
}) {
  const showRetry = error.retryable && Boolean(onRetry);

  return (
    <div
      role="alert"
      aria-live="polite"
      data-testid="chat-error-page"
      className="flex min-h-full flex-col items-center justify-center px-6 py-16 text-center"
    >
      {/* The existing alert glyph rather than a new illustration set — the
          project has no error artwork, and inventing one here would be a new
          visual system for a single screen. */}
      <div
        className="mb-5 flex h-14 w-14 items-center justify-center rounded-full"
        style={{ background: 'color-mix(in srgb, var(--ts-danger) 12%, transparent)' }}
      >
        <IconAlert size={26} className="text-danger" />
      </div>

      <p
        data-testid="chat-error-status"
        className="text-[64px] font-semibold leading-none tracking-tight text-ink sm:text-[76px]"
      >
        {error.display}
      </p>

      <h2 className="mt-4 text-xl font-semibold text-ink">{error.title}</h2>

      <p className="mt-2.5 max-w-md text-sm leading-relaxed text-muted">
        {error.message}
      </p>

      <div className="mt-7 flex flex-wrap items-center justify-center gap-2.5">
        {showRetry && (
          <button
            type="button"
            onClick={onRetry}
            className="inline-flex items-center gap-1.5 rounded-md bg-accent-strong px-4 py-2 text-sm font-medium text-white transition-all duration-ts hover:brightness-110 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            <IconRefresh size={14} />
            Retry
          </button>
        )}
        <button
          type="button"
          onClick={onReturn}
          className="inline-flex items-center rounded-md border border-border bg-surface px-4 py-2 text-sm font-medium text-ink transition-colors duration-ts hover:bg-surface-2 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
        >
          Return to chat
        </button>
      </div>
    </div>
  );
}
