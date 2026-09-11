'use client';

/**
 * The side panel a generated file opens into (2026-09-11; per-file since
 * 2026-09-12, CONTRACT-2 §9).
 *
 * Desktop: a column beside the conversation, owned by ChatApp (which clamps
 * it to 45–55 % of the workspace, never narrower than 520 px nor wider than
 * 960). Under 768 px: a full-screen sheet with a Back button — a phone has
 * no room for two columns, and a document at half of 390 px is unreadable.
 *
 * The panel is opened on ONE FILE of one version and can step to the
 * others: prev/next walk the version's files in order and then on into the
 * message's other versions, disabled at the ends. Stepping within a version
 * changes no fetch — the page images of a PDF and its Word twin are the
 * same set, keyed by version, and stay mounted; stepping to another version
 * fetches it like a fresh open. The host is told of every step
 * (`onNavigate`) so the card of the file on show is the one marked active.
 *
 * Keyboard contract, in the order it matters:
 *  - Escape closes the panel and STOPS there. ChatApp's window-level map
 *    reads a bare Escape as "stop generating" while a stream is live
 *    (lib/searchPalette.ts); a person closing a preview must not also kill
 *    the answer they are waiting on. Same recipe as ActivityPanel: a
 *    document-level listener that stopPropagation()s before the window
 *    handler runs — with one difference. ActivityPanel is a transient
 *    overlay; on desktop this panel is a PERSISTENT column and the person
 *    keeps chatting beside it, so an Escape that already means something
 *    else must not also tear the file down: one the palette, the model
 *    picker or the mermaid viewer preventDefault()ed, one typed into the
 *    composer (L-17: an editable element owns Escape), one aimed at another
 *    dialog. Only an Escape from inside the panel, or a bare one from the
 *    page, closes it. Under 768 px the sheet IS modal and every Escape is
 *    its own.
 *  - Focus moves INTO the panel on open (the close control, so the first
 *    Tab lands on the toolbar) and RETURNS to the card that opened it on
 *    close — the card's DOM id travels in as `originId` for exactly this,
 *    and the LATEST origin wins when another card re-targets an open panel.
 *  - Tab cycles within the panel ONLY in the under-768 px sheet, where it
 *    is genuinely modal (lib/focusTrap, like the palette and the drawer).
 *    Beside the conversation it is a non-modal region — no backdrop, no
 *    aria-modal, the composer stays clickable — and the WAI-ARIA window-
 *    splitter pattern the divider follows assumes exactly that: a keyboard
 *    user must be able to Tab back to the composer with the preview open.
 *
 * The panel fetches the version itself rather than trusting the card's
 * copy: the card's ref may be the meta's snapshot from before the job
 * finished, and the viewer needs the real file list, ids and preview. A
 * non-terminal version is followed with the same poll the card uses, and
 * the sentence in the live region is the server's stage title — never a
 * percentage.
 */

import {
  useCallback,
  useEffect,
  useMemo,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from 'react';
import {
  ArtifactRequestError,
  fetchArtifact,
  fileDownloadUrl,
  fileKey,
  fileMatchesKey,
  formatLabel,
  isFileId,
  isPreviewable,
  isTerminal,
  pollJob,
  previewKindFor,
  statusLine,
} from '@/lib/artifacts';
import { focusableWithin, focusTrapNext } from '@/lib/focusTrap';
import { fileKind } from '@/lib/format';
import type { ArtifactFile, ArtifactJob, ArtifactRef } from '@/lib/types';
import { IconChevronLeft, IconChevronRight, IconDownload, IconForFormat, IconX } from '../icons';
import { groupArtifacts } from './ArtifactCards';
import { PagesViewer } from './PagesViewer';
import { SheetViewer } from './SheetViewer';
import { stateForStatus, ViewerState, type ViewerStateKind } from './ViewerState';

/** The width below which the panel is the full-screen sheet (Tailwind's `md`). */
export const SHEET_MAX_WIDTH = 767;

/**
 * Is the panel the full-screen sheet right now? The same breakpoint as the
 * `md:` classes below, read at the moment a key is pressed rather than
 * held in state: it only matters on a keystroke, and a resize between two
 * keystrokes must not be answered with a stale value. Without matchMedia
 * (jsdom, a very old browser) the answer is "desktop", the non-modal case.
 */
export function isSheetMode(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia(`(max-width: ${SHEET_MAX_WIDTH}px)`).matches === true
  );
}

/** The elements that own their own Escape (ChatApp's shortcut map draws the same line). */
function isEditable(el: Element | null): boolean {
  if (!el) return false;
  const tag = el.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || (el as HTMLElement).isContentEditable === true;
}

type PanelState =
  | { kind: 'loading' }
  | { kind: 'ready'; artifact: ArtifactRef; job: ArtifactJob | null }
  | { kind: Exclude<ViewerStateKind, 'loading' | 'rendering' | 'empty'>; detail?: string };

/** Where the panel is: one file of one version. */
export interface PanelCursor {
  artifactId: string;
  version: number;
  fileKey: string | null;
}

/** One stop of prev/next. */
export interface NavEntry {
  artifactId: string;
  version: number;
  key: string;
  file: ArtifactFile;
  ref: ArtifactRef;
}

/**
 * The order prev/next walk: the message's versions as the cards show them
 * (by artifact, versions ascending), each version's files in the server's
 * order. The version on show contributes the files the panel FETCHED —
 * fresher than the meta's snapshot, and the only copy that has ids for a
 * ref persisted before them. Versions that are not finished have no files
 * and are skipped.
 */
export function navigationEntries(
  refs: readonly ArtifactRef[],
  shown: ArtifactRef | null,
): NavEntry[] {
  const seen = new Set<string>();
  const out: NavEntry[] = [];
  const push = (ref: ArtifactRef) => {
    const id = `${ref.artifact_id}:${ref.version}`;
    if (seen.has(id)) return;
    seen.add(id);
    const status = ref.status;
    if (!isTerminal(status) || status === 'failed' || status === 'cancelled') return;
    for (const file of ref.files ?? []) {
      out.push({ artifactId: ref.artifact_id, version: ref.version, key: fileKey(ref, file), file, ref });
    }
  };
  const groups = groupArtifacts([...refs]);
  let placed = false;
  for (const group of groups) {
    for (const ref of group) {
      if (shown && ref.artifact_id === shown.artifact_id && ref.version === shown.version) {
        push(shown);
        placed = true;
      } else {
        push(ref);
      }
    }
  }
  if (shown && !placed) push(shown);
  return out;
}

export function ArtifactPanel({
  refs = [],
  artifactId,
  version,
  fileKey: initialFileKey = null,
  originId,
  onClose,
  onNavigate,
}: {
  /** Every ref of the message the opening card sits in, for prev/next. */
  refs?: readonly ArtifactRef[];
  artifactId: string;
  version: number;
  /** The file to show first (lib/artifacts.ts fileKey); the version's first file when null. */
  fileKey?: string | null;
  /** DOM id of the card control that opened the panel — focus goes back there. */
  originId: string | null;
  onClose: () => void;
  /** Prev/next moved the panel; the host mirrors it so the right card is marked. */
  onNavigate?: (artifactId: string, version: number, fileKey: string) => void;
}) {
  const [cursor, setCursor] = useState<PanelCursor>({ artifactId, version, fileKey: initialFileKey });
  const [state, setState] = useState<PanelState>({ kind: 'loading' });
  const [attempt, setAttempt] = useState(0);
  const panelRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const originRef = useRef<string | null>(originId);
  originRef.current = originId;
  const titleId = 'artifact-panel-title';

  // The host re-targets an open panel (another card clicked): follow it.
  useEffect(() => {
    setCursor({ artifactId, version, fileKey: initialFileKey });
  }, [artifactId, version, initialFileKey]);

  /* ------------------------------------------------------------ data */

  const shownId = cursor.artifactId;
  const shownVersion = cursor.version;

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    setState({ kind: 'loading' });
    void (async () => {
      try {
        let artifact: ArtifactRef = await fetchArtifact(shownId, shownVersion, signal);
        if (signal.aborted) return;
        setState({ kind: 'ready', artifact, job: null });
        if (isTerminal(artifact.status)) return;
        // Not finished: follow the job, then read the version again for the
        // real file list and preview.
        const done = await pollJob(artifact.job_id, {
          signal,
          onUpdate: (job) => {
            if (!signal.aborted) setState({ kind: 'ready', artifact, job });
          },
        });
        if (!done || signal.aborted) return;
        try {
          artifact = done.artifact ?? (await fetchArtifact(shownId, shownVersion, signal));
        } catch {
          artifact = { ...artifact, status: done.status };
        }
        if (!signal.aborted) setState({ kind: 'ready', artifact, job: done });
      } catch (err) {
        if (signal.aborted) return;
        if (err instanceof ArtifactRequestError) {
          const kind = stateForStatus(err.status);
          setState({ kind: kind === 'denied' || kind === 'unavailable' ? kind : 'failed', detail: err.detail });
        } else {
          setState({ kind: 'failed', detail: 'The server could not be reached.' });
        }
      }
    })();
    return () => controller.abort();
  }, [shownId, shownVersion, attempt]);

  /* -------------------------------------------------------- keyboard */

  // Escape: consumed here, before the window-level map can read it as
  // "stop generating" — but only when it is OURS (see the module comment).
  useEffect(() => {
    function onKey(e: KeyboardEvent) {
      if (e.key !== 'Escape') return;
      // Something closer to the key already answered it (the palette, a
      // menu, the mermaid viewer): React's root handlers run before this
      // document listener, and every one of them preventDefaults first.
      if (e.defaultPrevented) return;
      const target = e.target instanceof Element ? e.target : null;
      const inside = Boolean(target && panelRef.current?.contains(target));
      if (!inside && !isSheetMode()) {
        // Beside the conversation: a field owns its own Escape, and so does
        // any other dialog that is open.
        if (isEditable(target) || target?.closest('[role="dialog"]')) return;
      }
      e.preventDefault();
      e.stopPropagation();
      onClose();
    }
    document.addEventListener('keydown', onKey);
    return () => document.removeEventListener('keydown', onKey);
  }, [onClose]);

  // Focus in on open; back to the originating card on close/unmount.
  useEffect(() => {
    const previous = document.activeElement as HTMLElement | null;
    closeRef.current?.focus({ preventScroll: true });
    return () => {
      // Read late on purpose — the card may have re-rendered since, and the
      // origin may have moved to another card while the panel stayed open.
      const id = originRef.current;
      const target = (id ? document.getElementById(id) : null) ?? previous;
      if (target?.isConnected) target.focus({ preventScroll: true });
    };
    // Runs once per mount: a re-target keeps the panel and only moves the origin.
  }, []);

  function onPanelKeyDown(e: ReactKeyboardEvent<HTMLElement>) {
    if (e.key !== 'Tab') return;
    // Trapped only while the panel is the full-screen sheet. Beside the
    // thread Tab leaves naturally — the composer is one Tab away on purpose.
    if (!isSheetMode()) return;
    const nodes = focusableWithin(panelRef.current);
    if (nodes.length === 0) return;
    e.preventDefault();
    focusTrapNext(nodes, document.activeElement, e.shiftKey)?.focus({ preventScroll: true });
  }

  const retry = useCallback(() => setAttempt((n) => n + 1), []);

  /* ------------------------------------------------------- navigation */

  const artifact = state.kind === 'ready' ? state.artifact : null;
  const job = state.kind === 'ready' ? state.job : null;
  const status = job?.status ?? artifact?.status ?? '';
  const terminal = artifact ? isTerminal(status) : false;
  const failed = status === 'failed' || status === 'cancelled';
  const files = artifact && terminal && !failed ? (artifact.files ?? []) : [];
  const current: ArtifactFile | null = artifact
    ? (files.find((f) => cursor.fileKey !== null && fileMatchesKey(artifact, f, cursor.fileKey)) ??
      files[0] ??
      null)
    : null;
  const currentKey = artifact && current ? fileKey(artifact, current) : cursor.fileKey;

  const entries = useMemo(
    () => navigationEntries(refs, artifact && terminal && !failed ? artifact : null),
    [refs, artifact, terminal, failed],
  );
  const index = entries.findIndex(
    (e) =>
      e.artifactId === cursor.artifactId &&
      e.version === cursor.version &&
      currentKey !== null &&
      fileMatchesKey(e.ref, e.file, currentKey),
  );
  const previousEntry = index > 0 ? entries[index - 1] : null;
  const nextEntry = index >= 0 && index < entries.length - 1 ? entries[index + 1] : null;

  function goTo(entry: NavEntry | null) {
    if (!entry) return;
    setCursor({ artifactId: entry.artifactId, version: entry.version, fileKey: entry.key });
    onNavigate?.(entry.artifactId, entry.version, entry.key);
  }

  /* ----------------------------------------------------------- render */

  const fileTitle = current?.title?.trim() || artifact?.title || 'Generated file';
  const downloadHref = artifact && current ? fileDownloadUrl(artifact, current, 'attachment') : '';
  // What the panel's live region says. Refusals are left to ViewerState's
  // own region so they are spoken once, not twice.
  const announcement =
    state.kind === 'loading'
      ? 'Loading the file…'
      : state.kind === 'ready' && artifact
        ? `${fileTitle}: ${statusLine(artifact, job)}`
        : '';

  let body: ReactNode;
  if (state.kind === 'loading') {
    body = <ViewerState kind="loading" />;
  } else if (state.kind !== 'ready' || !artifact) {
    body = (
      <ViewerState
        kind={state.kind === 'ready' ? 'failed' : state.kind}
        detail={state.kind === 'ready' ? undefined : state.detail}
        onRetry={retry}
      />
    );
  } else if (!terminal) {
    body = (
      <ViewerState
        kind="rendering"
        message={status === 'queued' ? 'Waiting for a worker' : 'Still being generated'}
        detail={statusLine(artifact, job)}
      />
    );
  } else if (failed) {
    body = (
      <ViewerState
        kind="failed"
        message={status === 'cancelled' ? 'This generation was cancelled.' : 'This file could not be generated.'}
        detail={job?.error ?? undefined}
      />
    );
  } else if (!current) {
    body = <ViewerState kind="empty" />;
  } else {
    const kind = previewKindFor(current);
    const previewable = isPreviewable(current, artifact);
    const pages =
      artifact.preview_pages > 0
        ? artifact.preview_pages
        : typeof current.pages === 'number'
          ? current.pages
          : 0;
    if (kind === 'pages' && previewable && pages > 0) {
      body = (
        <PagesViewer
          // Keyed by VERSION: the page set is the version's, shared by its
          // PDF and its Word/PowerPoint twin, so stepping between them keeps
          // the images that are already on screen.
          key={`${artifact.artifact_id}:${artifact.version}`}
          artifactId={artifact.artifact_id}
          version={artifact.version}
          pages={pages}
          title={fileTitle}
        />
      );
    } else if (kind === 'grid' && previewable) {
      body = (
        <SheetViewer
          key={currentKey ?? `${artifact.artifact_id}:${artifact.version}`}
          artifactId={artifact.artifact_id}
          version={artifact.version}
          fileId={isFileId(current.file_id) ? current.file_id : null}
          title={fileTitle}
        />
      );
    } else {
      // The file exists — Download works — but there is nothing to show
      // for it: the server rendered no preview, or the format has none.
      body = (
        <ViewerState
          kind="empty"
          message={kind === 'none' ? 'There is nothing to preview for this file.' : 'Preview unavailable — download file'}
          detail={
            downloadHref ? (
              <a
                href={downloadHref}
                download={current.filename}
                // A different accessible name from the header's Download:
                // two controls announced identically are one control to a
                // screen-reader user, and this one says what the header's
                // cannot — why it is the only way to see the file.
                aria-label={`Download ${current.filename} to open it in its own application`}
                className="inline-flex items-center gap-1.5 rounded-md border border-border bg-surface px-2.5 py-1 text-xs font-medium text-ink no-underline transition-colors duration-ts hover:bg-surface-2"
                data-testid="artifact-panel-download-fallback"
              >
                <IconDownload size={13} />
                Download the file
              </a>
            ) : (
              'Download the file to open it in its own application.'
            )
          }
        />
      );
    }
  }

  const format = current?.format ?? '';
  const toolButton =
    'inline-flex h-8 w-8 items-center justify-center rounded-lg text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink disabled:opacity-40 disabled:hover:bg-transparent disabled:hover:text-muted';

  return (
    <aside
      ref={panelRef}
      role="dialog"
      aria-labelledby={titleId}
      onKeyDown={onPanelKeyDown}
      data-testid="artifact-panel"
      className="fixed inset-0 z-50 flex h-full w-full flex-col border-border bg-bg md:static md:z-auto md:border-l"
    >
      <header className="flex items-center gap-1.5 border-b border-border px-3 py-2">
        <button
          type="button"
          onClick={onClose}
          aria-label="Back to the conversation"
          className={`${toolButton} md:hidden`}
        >
          <IconChevronLeft size={18} />
        </button>
        <span
          aria-hidden
          className={`hidden h-8 w-8 shrink-0 items-center justify-center rounded-lg md:flex ${
            format ? fileKind(`x.${format}`).className : 'file-icon-other'
          }`}
        >
          <IconForFormat format={format} size={16} />
        </span>
        <div className="min-w-0 flex-1">
          <h2 id={titleId} className="truncate text-sm font-semibold text-ink" title={current?.filename ?? fileTitle}>
            {fileTitle}
          </h2>
          <p className="truncate text-[11px] text-muted" data-testid="artifact-panel-subtitle">
            {current ? formatLabel(current.format) : artifact ? 'Generated file' : ''}
            {artifact ? ` · v${artifact.version}` : ''}
            {entries.length > 1 && index >= 0 ? ` · ${index + 1} of ${entries.length}` : ''}
          </p>
        </div>
        {downloadHref && current && (
          <a
            href={downloadHref}
            download={current.filename}
            aria-label={`Download ${current.filename}`}
            title={`Download ${current.filename}`}
            className="inline-flex items-center gap-1 rounded-lg border border-border bg-surface px-2 py-1 text-xs font-medium text-muted transition-colors duration-ts hover:border-accent/50 hover:bg-surface-2 hover:text-ink"
            data-testid="artifact-panel-download"
          >
            <IconDownload size={14} />
            <span className="hidden sm:inline">Download</span>
          </a>
        )}
        <span className="ml-1 flex items-center" role="group" aria-label="Other files">
          <button
            type="button"
            className={toolButton}
            onClick={() => goTo(previousEntry)}
            disabled={!previousEntry}
            aria-label="Previous file"
            title={previousEntry ? `Previous: ${previousEntry.file.filename}` : 'No previous file'}
          >
            <IconChevronLeft size={16} />
          </button>
          <button
            type="button"
            className={toolButton}
            onClick={() => goTo(nextEntry)}
            disabled={!nextEntry}
            aria-label="Next file"
            title={nextEntry ? `Next: ${nextEntry.file.filename}` : 'No next file'}
          >
            <IconChevronRight size={16} />
          </button>
        </span>
        <button
          ref={closeRef}
          type="button"
          onClick={onClose}
          aria-label="Close preview"
          title="Close (Esc)"
          className={toolButton}
        >
          <IconX size={16} />
        </button>
      </header>

      <p
        className="sr-only"
        role="status"
        aria-live="polite"
        aria-atomic="true"
        data-testid="artifact-panel-status"
      >
        {announcement}
      </p>

      <div className="min-h-0 flex-1">{body}</div>
    </aside>
  );
}
