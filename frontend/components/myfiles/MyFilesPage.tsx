'use client';

/**
 * /files — "My files": everything this person uploaded, across every chat,
 * with their voice recordings (2026-09-30).
 *
 * THE FILTERS LIVE IN THE URL (?q=&kind=&from=&to=&size=&sort=), read with
 * useSearchParams and written with router.replace: a reload, Back and a
 * pasted link all show the same view, and twenty filter changes do not bury
 * the page someone arrived from under twenty history entries. The search box
 * is the one control that does not write on every keystroke: it waits 250 ms
 * after the last one.
 *
 * ONE LIST REQUEST IN FLIGHT. Each change of filters aborts the request the
 * previous one started, so a slow answer for "budget" can never land on top
 * of the answer for "budget q3". Paging is by keyset (`cursor`), so a file
 * uploaded or deleted while older pages are still to come shifts nothing.
 *
 * The middleware gates the page on the session cookie; the orchestrator
 * decides whose files these are from that session alone.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import Link from 'next/link';
import { usePathname, useRouter, useSearchParams } from 'next/navigation';
import { PageHeader } from '@/components/admin/ui';
import { IconChevronLeft, IconPaperclip } from '@/components/icons';
import { formatBytes } from '@/lib/format';
import {
  DEFAULT_FILTERS,
  SEARCH_MAX_CHARS,
  appendFiles,
  filtersFromQuery,
  filtersToQuery,
  hasActiveFilters,
  loadFiles,
  loadSummary,
  retentionSentences,
  rowDomId,
  type Filters,
  type MyFile,
  type MyFilesSummary,
  type Retention,
} from '@/lib/myfiles';
import { MyFileRow, describe, myFilesActionClass } from './MyFileRow';
import { MyFilesFilters } from './MyFilesFilters';

/** The pause after the last keystroke before a search is sent. */
export const SEARCH_DEBOUNCE_MS = 250;

const EMPTY_ID = 'files-empty';
const OLDER_ID = 'files-older';
/** Focus target after the last loaded row is deleted: whichever of the two exists. */
const AFTER_LAST = 'after-last';

type Phase =
  | { kind: 'loading' }
  | { kind: 'failed'; message: string; signedOut: boolean }
  | { kind: 'ready' };

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
        <a href="/login" className={myFilesActionClass}>
          Sign in
        </a>
      ) : (
        <button type="button" onClick={onRetry} className={myFilesActionClass}>
          Retry
        </button>
      )}
    </div>
  );
}

function Skeleton() {
  return (
    <div aria-busy="true" className="rounded-ts border border-border bg-surface">
      <span className="sr-only">Loading your files…</span>
      {[0, 1, 2].map((n) => (
        <div key={n} className="flex gap-3 border-b border-border px-4 py-4 last:border-b-0 sm:px-5">
          <span aria-hidden className="h-10 w-10 shrink-0 rounded-lg bg-surface-2 motion-safe:animate-pulse" />
          <div className="flex-1 space-y-2 pt-1">
            <span aria-hidden className="block h-3 w-48 max-w-full rounded bg-surface-2 motion-safe:animate-pulse" />
            <span aria-hidden className="block h-3 w-32 max-w-full rounded bg-surface-2 motion-safe:animate-pulse" />
          </div>
        </div>
      ))}
    </div>
  );
}

function plural(n: number, word: string): string {
  return `${n.toLocaleString()} ${word}${n === 1 ? '' : 's'}`;
}

/** The count line, which is also the page's polite live region. */
function countText(
  filters: Filters,
  summary: MyFilesSummary | null,
  shown: number,
  more: boolean,
): string {
  const narrowed = hasActiveFilters(filters);
  if (summary) {
    const tally = filters.kind ? summary.kinds[filters.kind] : summary.total;
    if (tally.count === 0) return narrowed ? 'No files match these filters.' : 'No files yet.';
    const size = tally.bytes > 0 ? `, ${formatBytes(tally.bytes)}` : '';
    return narrowed
      ? `${plural(tally.count, 'matching file')}${size}.`
      : `${plural(tally.count, 'file')}${size} in all.`;
  }
  if (shown === 0) return narrowed ? 'No files match these filters.' : 'No files yet.';
  return `Showing ${plural(shown, 'file')}${more ? ' so far' : ''}.`;
}

/** The summary after a deletion, so the counts do not wait for a reload. */
function withoutFile(summary: MyFilesSummary, file: MyFile): MyFilesSummary {
  const bytes = file.bytes ?? 0;
  const kind = summary.kinds[file.kind];
  return {
    ...summary,
    kinds: {
      ...summary.kinds,
      [file.kind]: { count: Math.max(0, kind.count - 1), bytes: Math.max(0, kind.bytes - bytes) },
    },
    total: { count: Math.max(0, summary.total.count - 1), bytes: Math.max(0, summary.total.bytes - bytes) },
  };
}

/** What is left of a file once a preview found its bytes gone (a 410). */
function expiredCopy(file: MyFile): MyFile {
  const availability =
    file.can.preview === 'text' ? 'text_only' : file.can.preview === 'summary' ? 'summary_only' : 'expired';
  return { ...file, availability, can: { ...file.can, download: false } };
}

interface MyFilesPageProps {
  /** Injectable for tests — the AccountMenu idiom. */
  fetchFn?: typeof fetch;
}

export function MyFilesPage({ fetchFn = fetch }: MyFilesPageProps) {
  const router = useRouter();
  const pathname = usePathname() ?? '/files';
  const params = useSearchParams();
  const filtersKey = filtersToQuery(filtersFromQuery(params));
  const filters = useMemo(() => filtersFromQuery(new URLSearchParams(filtersKey)), [filtersKey]);
  // The counts are per type, so the type and the sort do not change them.
  const countsKey = filtersToQuery({ ...filters, kind: null, sort: 'newest' });

  const [draft, setDraft] = useState(filters.q);
  const pushedQ = useRef(filters.q);
  const filtersRef = useRef(filters);
  filtersRef.current = filters;

  const [phase, setPhase] = useState<Phase>({ kind: 'loading' });
  const [loadedOnce, setLoadedOnce] = useState(false);
  const [items, setItems] = useState<MyFile[]>([]);
  const [nextCursor, setNextCursor] = useState<string | null>(null);
  const [retention, setRetention] = useState<Retention | null>(null);
  const [summary, setSummary] = useState<MyFilesSummary | null>(null);
  const [more, setMore] = useState<{ loading: boolean; error: string | null; signedOut: boolean }>({
    loading: false,
    error: null,
    signedOut: false,
  });
  const [notice, setNotice] = useState('');
  const [focusAfter, setFocusAfter] = useState<string | null>(null);
  const [attempt, setAttempt] = useState(0);
  const [filtersOpen, setFiltersOpen] = useState(false);
  const moreAbort = useRef<AbortController | null>(null);

  useEffect(() => () => moreAbort.current?.abort(), []);

  const replaceFilters = useCallback(
    (next: Filters) => {
      const query = filtersToQuery(next);
      pushedQ.current = next.q;
      router.replace(query ? `${pathname}?${query}` : pathname, { scroll: false });
    },
    [router, pathname],
  );

  // A change of q that this page did not make (Back, a pasted link) is shown
  // in the box; one it made is not written back over what is being typed.
  useEffect(() => {
    if (filters.q !== pushedQ.current) {
      pushedQ.current = filters.q;
      setDraft(filters.q);
    }
  }, [filters.q]);

  useEffect(() => {
    const wanted = draft.slice(0, SEARCH_MAX_CHARS);
    if (wanted.trim() === filters.q.trim()) return;
    const timer = setTimeout(() => replaceFilters({ ...filtersRef.current, q: wanted }), SEARCH_DEBOUNCE_MS);
    return () => clearTimeout(timer);
  }, [draft, filters.q, replaceFilters]);

  // The list: first page for these filters. The previous request, and any
  // "older" page still loading for the previous filters, are aborted.
  useEffect(() => {
    const controller = new AbortController();
    moreAbort.current?.abort();
    setPhase({ kind: 'loading' });
    setMore({ loading: false, error: null, signedOut: false });
    setNotice('');
    void loadFiles(fetchFn, filtersFromQuery(new URLSearchParams(filtersKey)), null, controller.signal).then(
      (result) => {
        if (controller.signal.aborted || result.kind === 'aborted') return;
        if (result.kind === 'ok') {
          setItems(result.value.items);
          setNextCursor(result.value.nextCursor);
          if (result.value.retention) setRetention(result.value.retention);
          setPhase({ kind: 'ready' });
          setLoadedOnce(true);
        } else {
          setPhase({ kind: 'failed', message: result.message, signedOut: result.signedOut });
        }
      },
    );
    return () => controller.abort();
  }, [fetchFn, filtersKey, attempt]);

  // The counts: independent of the list, and never a reason to fail the page.
  useEffect(() => {
    const controller = new AbortController();
    void loadSummary(fetchFn, filtersFromQuery(new URLSearchParams(countsKey)), controller.signal).then(
      (result) => {
        if (controller.signal.aborted || result.kind === 'aborted') return;
        if (result.kind === 'ok') {
          setSummary(result.value);
          if (result.value.retention) setRetention(result.value.retention);
        } else {
          setSummary(null);
        }
      },
    );
    return () => controller.abort();
  }, [fetchFn, countsKey, attempt]);

  const loadOlder = useCallback(async () => {
    if (!nextCursor) return;
    moreAbort.current?.abort();
    const controller = new AbortController();
    moreAbort.current = controller;
    setMore({ loading: true, error: null, signedOut: false });
    const result = await loadFiles(
      fetchFn,
      filtersFromQuery(new URLSearchParams(filtersKey)),
      nextCursor,
      controller.signal,
    );
    if (controller.signal.aborted || result.kind === 'aborted') return;
    if (result.kind === 'ok') {
      setItems((current) => appendFiles(current, result.value.items));
      setNextCursor(result.value.nextCursor);
      setMore({ loading: false, error: null, signedOut: false });
    } else {
      setMore({ loading: false, error: result.message, signedOut: result.signedOut });
    }
  }, [fetchFn, filtersKey, nextCursor]);

  const onDeleted = useCallback((file: MyFile) => {
    // Focus moves to the row that takes this one's place (or the one above
    // it at the end of the list), else to "Show older files", else to the
    // empty state — never to <body>. The neighbour is read from the page as
    // it stands and the list is filtered from its CURRENT value, because two
    // deletes can be in flight at once (the Recordings page's lesson).
    const title = rowDomId(file);
    const titles = Array.from(
      document.querySelectorAll<HTMLElement>('ul[aria-label="Your files"] h2[id^="file-"]'),
    ).map((el) => el.id);
    const index = titles.indexOf(title);
    const rest = titles.filter((t) => t !== title);
    setItems((current) => current.filter((f) => f.id !== file.id));
    setSummary((current) => (current ? withoutFile(current, file) : current));
    setFocusAfter(rest[Math.min(Math.max(index, 0), rest.length - 1)] ?? AFTER_LAST);
    setNotice(`Deleted ${describe(file)}.`);
  }, []);

  const onExpired = useCallback((file: MyFile) => {
    setItems((current) => current.map((f) => (f.id === file.id ? expiredCopy(f) : f)));
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

  const clearFilters = useCallback(() => {
    setDraft('');
    replaceFilters({ ...DEFAULT_FILTERS, sort: filters.sort });
  }, [filters.sort, replaceFilters]);

  const narrowed = hasActiveFilters(filters);
  const ready = phase.kind === 'ready';
  const refreshing = phase.kind === 'loading' && loadedOnce;
  const status = notice || (ready ? countText(filters, summary, items.length, Boolean(nextCursor)) : '');

  return (
    <div className="min-h-dvh bg-bg text-ink">
      <header className="border-b border-border">
        <div className="mx-auto flex h-[52px] w-full max-w-thread items-center px-2 sm:px-4">
          <Link
            href="/"
            className="inline-flex h-11 items-center gap-1.5 rounded-lg px-2 text-sm text-icon transition-colors duration-ts hover:bg-surface-2 hover:text-ink focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-accent"
          >
            <IconChevronLeft size={16} />
            Back to chat
          </Link>
        </div>
      </header>

      <main className="mx-auto w-full max-w-thread px-4 pb-16 pt-6 md:pt-10">
        <PageHeader
          title="My files"
          subtitle="Everything you have attached to a chat, and your voice recordings, in one place."
        />

        {retention && (
          <p className="mt-4 border-l-2 border-accent/40 pl-3 text-sm leading-relaxed text-muted">
            {retentionSentences(retention).join(' ')}
          </p>
        )}

        <div className="mt-6">
          <MyFilesFilters
            filters={filters}
            draft={draft}
            onDraft={setDraft}
            onSearchNow={() => {
              const wanted = draft.slice(0, SEARCH_MAX_CHARS);
              if (wanted.trim() !== filters.q.trim()) replaceFilters({ ...filters, q: wanted });
            }}
            onChange={(next) => replaceFilters({ ...filters, ...next })}
            onClear={clearFilters}
            summary={summary}
            open={filtersOpen}
            onToggle={() => setFiltersOpen((v) => !v)}
          />
        </div>

        <p role="status" className="mt-4 min-h-5 text-sm text-muted">
          {status}
        </p>

        <div className="mt-2">
          {phase.kind === 'loading' && !loadedOnce && <Skeleton />}

          {phase.kind === 'failed' && (
            <FailurePanel
              message={phase.message}
              signedOut={phase.signedOut}
              onRetry={() => setAttempt((n) => n + 1)}
            />
          )}

          {ready && items.length === 0 && !nextCursor && (
            <div className="rounded-ts border border-border bg-surface px-5 py-10 text-center">
              <span
                aria-hidden
                className="mx-auto flex h-10 w-10 items-center justify-center rounded-full bg-accent/15 text-accent"
              >
                <IconPaperclip size={18} />
              </span>
              <h2 id={EMPTY_ID} tabIndex={-1} className="mt-4 text-base font-medium text-ink focus:outline-none">
                {narrowed ? 'No files match these filters' : 'No files yet'}
              </h2>
              {narrowed ? (
                <div className="mt-4 flex justify-center">
                  <button type="button" onClick={clearFilters} className={myFilesActionClass}>
                    Clear filters
                  </button>
                </div>
              ) : (
                <>
                  <p className="mx-auto mt-1.5 max-w-md text-sm text-muted">
                    Documents, spreadsheets, videos and audio files you attach to a chat appear here, and so do
                    your voice recordings.
                  </p>
                  <p className="mx-auto mt-2 max-w-md text-sm text-muted">
                    Pictures stay only in the browser you sent them from, so they are not listed.
                  </p>
                </>
              )}
            </div>
          )}

          {(ready || refreshing) && items.length > 0 && (
            <ul
              aria-label="Your files"
              aria-busy={refreshing || undefined}
              className={`divide-y divide-border rounded-ts border border-border bg-surface transition-opacity duration-ts ${
                refreshing ? 'opacity-60' : ''
              }`}
            >
              {items.map((file) => (
                <MyFileRow
                  key={file.id}
                  file={file}
                  retention={retention}
                  fetchFn={fetchFn}
                  onDeleted={onDeleted}
                  onExpired={onExpired}
                />
              ))}
            </ul>
          )}

          {ready && more.error && (
            <div className="mt-4">
              <FailurePanel message={more.error} signedOut={more.signedOut} onRetry={() => void loadOlder()} />
            </div>
          )}

          {ready && nextCursor && !more.error && (
            <div className="mt-4 flex justify-center">
              <button
                id={OLDER_ID}
                type="button"
                onClick={() => void loadOlder()}
                disabled={more.loading}
                className={myFilesActionClass}
              >
                {more.loading ? 'Loading…' : filters.sort === 'newest' ? 'Show older files' : 'Show more files'}
              </button>
            </div>
          )}
        </div>
      </main>
    </div>
  );
}
