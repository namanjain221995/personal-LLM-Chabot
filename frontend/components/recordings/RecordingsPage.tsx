'use client';

/**
 * /recordings — every recording the chat microphone stored for this person,
 * newest first, twenty at a time.
 *
 * It lives outside /admin on purpose: the recordings are the person's own,
 * and a member has no admin area. The middleware gates the page on the
 * session cookie; the orchestrator decides ownership of every row, file and
 * delete, so nothing here can reach anyone else's recording.
 *
 * Paging is by time, not by offset (`before` = the last row's created_at), so
 * deleting a row while older pages are still to come shifts nothing.
 */

import { useCallback, useEffect, useRef, useState } from 'react';
import Link from 'next/link';
import { PageHeader, SkeletonLine } from '@/components/admin/ui';
import { IconChevronLeft, IconMic } from '@/components/icons';
import { formatWhen } from '@/lib/format';
import { appendPage, loadRecordings, type Recording } from '@/lib/recordings';
import { RecordingItem, actionClass } from './RecordingItem';

const EMPTY_ID = 'recordings-empty';
const OLDER_ID = 'recordings-older';
/** Focus target after the last loaded row is deleted: whichever of the two exists. */
const AFTER_LAST = 'after-last';

type Phase =
  | { kind: 'loading' }
  | { kind: 'failed'; message: string; signedOut: boolean }
  | { kind: 'ready' };

interface RecordingsPageProps {
  /** Injectable for tests — the AccountMenu idiom. */
  fetchFn?: typeof fetch;
}

function FailurePanel({
  message,
  signedOut,
  onRetry,
}: {
  message: string;
  signedOut: boolean;
  onRetry: () => void;
}) {
  return (
    <div
      role="alert"
      className="flex flex-wrap items-center justify-between gap-3 rounded-ts border border-danger/40 bg-danger/10 px-4 py-3"
    >
      <p className="text-sm text-danger">{message}</p>
      {signedOut ? (
        <a href="/login" className={actionClass}>
          Sign in
        </a>
      ) : (
        <button type="button" onClick={onRetry} className={actionClass}>
          Retry
        </button>
      )}
    </div>
  );
}

export function RecordingsPage({ fetchFn = fetch }: RecordingsPageProps) {
  const [phase, setPhase] = useState<Phase>({ kind: 'loading' });
  const [items, setItems] = useState<Recording[]>([]);
  const [nextBefore, setNextBefore] = useState<string | null>(null);
  const [more, setMore] = useState<{ loading: boolean; error: string | null; signedOut: boolean }>({
    loading: false,
    error: null,
    signedOut: false,
  });
  const [notice, setNotice] = useState('');
  const [focusAfter, setFocusAfter] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const alive = useRef(true);

  useEffect(() => {
    alive.current = true;
    return () => {
      alive.current = false;
    };
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    setPhase({ kind: 'loading' });
    void loadRecordings(fetchFn, null, controller.signal).then((result) => {
      if (controller.signal.aborted) return;
      if (result.kind === 'ok') {
        setItems(result.page.recordings);
        setNextBefore(result.page.nextBefore);
        setPhase({ kind: 'ready' });
      } else {
        setPhase({ kind: 'failed', message: result.message, signedOut: result.signedOut });
      }
    });
    return () => controller.abort();
  }, [fetchFn, attempt]);

  const loadOlder = useCallback(async () => {
    if (!nextBefore) return;
    setMore({ loading: true, error: null, signedOut: false });
    const result = await loadRecordings(fetchFn, nextBefore);
    if (!alive.current) return;
    if (result.kind === 'ok') {
      setItems((current) => appendPage(current, result.page.recordings));
      setNextBefore(result.page.nextBefore);
      setMore({ loading: false, error: null, signedOut: false });
    } else {
      setMore({ loading: false, error: result.message, signedOut: result.signedOut });
    }
  }, [fetchFn, nextBefore]);

  const onDeleted = useCallback((rec: Recording) => {
    const title = `recording-${rec.id}`;
    // Focus moves to the row that takes this one's place (or the one above it
    // at the end of the list), else to "Show older recordings", else to the
    // empty state: never to <body> with the button that vanished. The
    // neighbour is read from the page as it stands and the list is updated
    // from its CURRENT value, because two deletes can be in flight at once:
    // a list captured when the second was confirmed still held the first
    // row, and put it back on screen after the server had deleted it.
    const titles = Array.from(
      document.querySelectorAll<HTMLElement>('ul[aria-label="Your recordings"] h2[id^="recording-"]'),
    ).map((el) => el.id);
    const index = titles.indexOf(title);
    const rest = titles.filter((t) => t !== title);
    setItems((current) => current.filter((r) => r.id !== rec.id));
    setFocusAfter(rest[Math.min(Math.max(index, 0), rest.length - 1)] ?? AFTER_LAST);
    setNotice(`Deleted the recording from ${formatWhen(rec.createdAt)}.`);
  }, []);

  useEffect(() => {
    if (!focusAfter) return;
    const target =
      focusAfter === AFTER_LAST
        ? (document.getElementById(OLDER_ID) ?? document.getElementById(EMPTY_ID))
        : document.getElementById(focusAfter);
    target?.focus();
    setFocusAfter(null);
  }, [focusAfter]);

  return (
    <div className="min-h-dvh bg-bg text-ink">
      <header className="border-b border-border">
        <div className="mx-auto flex h-[52px] w-full max-w-thread items-center px-2 sm:px-4">
          <Link
            href="/"
            className="inline-flex h-10 items-center gap-1.5 rounded-lg px-2 text-sm text-icon transition-colors duration-ts hover:bg-surface-2 hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            <IconChevronLeft size={16} />
            Back to chat
          </Link>
        </div>
      </header>

      <main className="mx-auto w-full max-w-thread px-4 pb-16 pt-6 md:pt-10">
        <PageHeader
          title="Recordings"
          subtitle="Everything you record with the microphone button in a chat is saved to your account and listed here. Play it, copy its transcript, or delete it at any time."
        />

        <p
          role="status"
          className={
            notice
              ? 'mt-4 rounded-ts border border-border bg-surface px-4 py-2.5 text-sm text-muted'
              : 'sr-only'
          }
        >
          {notice}
        </p>

        <div className="mt-6">
          {phase.kind === 'loading' && (
            <div aria-busy="true" className="rounded-ts border border-border bg-surface">
              <span className="sr-only">Loading your recordings…</span>
              {[0, 1, 2].map((n) => (
                <div
                  key={n}
                  className="space-y-2 border-b border-border px-4 py-4 last:border-b-0 sm:px-5"
                >
                  <div>
                    <SkeletonLine className="w-44" />
                  </div>
                  <div>
                    <SkeletonLine className="w-20" />
                  </div>
                  <div className="h-10 w-full animate-pulse rounded bg-surface-2" />
                </div>
              ))}
            </div>
          )}

          {phase.kind === 'failed' && (
            <FailurePanel
              message={phase.message}
              signedOut={phase.signedOut}
              onRetry={() => setAttempt((n) => n + 1)}
            />
          )}

          {phase.kind === 'ready' && items.length === 0 && !nextBefore && (
            <div className="rounded-ts border border-border bg-surface px-5 py-10 text-center">
              <span
                aria-hidden
                className="mx-auto flex h-10 w-10 items-center justify-center rounded-full bg-accent/15 text-accent"
              >
                <IconMic size={18} />
              </span>
              <h2
                id={EMPTY_ID}
                tabIndex={-1}
                className="mt-4 text-base font-medium text-ink focus:outline-none"
              >
                No recordings yet
              </h2>
              <p className="mx-auto mt-1.5 max-w-md text-sm text-muted">
                When you use the microphone button in a chat, the recording is saved to your
                account and kept here with its transcript. You can delete any of them at any time.
              </p>
            </div>
          )}

          {phase.kind === 'ready' && (items.length > 0 || nextBefore) && (
            <>
              {items.length > 0 && (
              <ul
                aria-label="Your recordings"
                className="divide-y divide-border rounded-ts border border-border bg-surface"
              >
                {items.map((rec) => (
                  <RecordingItem
                    key={rec.id}
                    rec={rec}
                    fetchFn={fetchFn}
                    onDeleted={onDeleted}
                    now={Date.now()}
                  />
                ))}
              </ul>
              )}

              {more.error && (
                <div className="mt-4">
                  <FailurePanel
                    message={more.error}
                    signedOut={more.signedOut}
                    onRetry={() => void loadOlder()}
                  />
                </div>
              )}

              {nextBefore && !more.error && (
                <div className="mt-4 flex justify-center">
                  <button
                    id={OLDER_ID}
                    type="button"
                    onClick={() => void loadOlder()}
                    disabled={more.loading}
                    className={actionClass}
                  >
                    {more.loading ? 'Loading…' : 'Show older recordings'}
                  </button>
                </div>
              )}
            </>
          )}
        </div>
      </main>
    </div>
  );
}
