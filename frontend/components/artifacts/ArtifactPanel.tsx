'use client';

/**
 * The side panel a generated file opens into (2026-09-11).
 *
 * Desktop: a column beside the conversation, owned by ChatApp (which keeps
 * the thread at ≥45% and gives this the rest). Under 900 px: a full-screen
 * sheet with a Back button — a phone has no room for two columns, and a
 * document at 45% of 390 px is unreadable.
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
 *    page, closes it. Under 900 px the sheet IS modal and every Escape is
 *    its own.
 *  - Focus moves INTO the panel on open (the close control, so the first
 *    Tab lands on the toolbar) and RETURNS to the card that opened it on
 *    close — the card's DOM id travels in as `originId` for exactly this.
 *  - Tab cycles within the panel ONLY in the under-900 px sheet, where it
 *    is genuinely modal (lib/focusTrap, like the palette and the drawer).
 *    Beside the conversation it is a non-modal region — no backdrop, no
 *    aria-modal, the composer stays clickable — and the WAI-ARIA window-
 *    splitter pattern the divider follows assumes exactly that: a keyboard
 *    user must be able to Tab back to the composer with the preview open.
 *
 * The panel fetches the version itself rather than trusting the card's
 * copy: the card's ref may be the meta's snapshot from before the job
 * finished, and the viewer needs the real page count and preview kind. A
 * non-terminal version is followed with the same poll the card uses, and
 * the sentence in the live region is the server's stage title — never a
 * percentage.
 */

import {
  useCallback,
  useEffect,
  useRef,
  useState,
  type KeyboardEvent as ReactKeyboardEvent,
  type ReactNode,
} from 'react';
import {
  ArtifactRequestError,
  fetchArtifact,
  isTerminal,
  kindLabel,
  pollJob,
  statusLine,
} from '@/lib/artifacts';
import { focusableWithin, focusTrapNext } from '@/lib/focusTrap';
import { fileKind } from '@/lib/format';
import type { ArtifactJob, ArtifactRef } from '@/lib/types';
import { IconChevronLeft, IconX } from '../icons';
import { DownloadControl, KindIcon } from './ArtifactCard';
import { PagesViewer } from './PagesViewer';
import { SheetViewer } from './SheetViewer';
import { stateForStatus, ViewerState, type ViewerStateKind } from './ViewerState';

/**
 * Is the panel the full-screen sheet right now? The same breakpoint as the
 * `min-[900px]:` classes below, read at the moment a key is pressed rather
 * than held in state: it only matters on a keystroke, and a resize between
 * two keystrokes must not be answered with a stale value. Without matchMedia
 * (jsdom, a very old browser) the answer is "desktop", the non-modal case.
 */
export function isSheetMode(): boolean {
  return (
    typeof window !== 'undefined' &&
    typeof window.matchMedia === 'function' &&
    window.matchMedia('(max-width: 899px)').matches === true
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

export function ArtifactPanel({
  artifactId,
  version,
  originId,
  onClose,
}: {
  artifactId: string;
  version: number;
  /** DOM id of the card button that opened the panel — focus goes back there. */
  originId: string | null;
  onClose: () => void;
}) {
  const [state, setState] = useState<PanelState>({ kind: 'loading' });
  const [attempt, setAttempt] = useState(0);
  const panelRef = useRef<HTMLElement>(null);
  const closeRef = useRef<HTMLButtonElement>(null);
  const titleId = `artifact-panel-title-${artifactId}`;

  /* ------------------------------------------------------------ data */

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    setState({ kind: 'loading' });
    void (async () => {
      try {
        let artifact: ArtifactRef = await fetchArtifact(artifactId, version, signal);
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
          artifact = done.artifact ?? (await fetchArtifact(artifactId, version, signal));
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
  }, [artifactId, version, attempt]);

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
    const opener = originId ? document.getElementById(originId) : null;
    const previous = document.activeElement as HTMLElement | null;
    closeRef.current?.focus({ preventScroll: true });
    return () => {
      // Read late on purpose — the card may have re-rendered since.
      const target =
        (originId ? document.getElementById(originId) : null) ?? opener ?? previous;
      if (target?.isConnected) target.focus({ preventScroll: true });
    };
    // The origin is fixed for the life of one open; a re-open is a remount.
    // eslint-disable-next-line react-hooks/exhaustive-deps
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

  /* ----------------------------------------------------------- render */

  const artifact = state.kind === 'ready' ? state.artifact : null;
  const job = state.kind === 'ready' ? state.job : null;
  const status = job?.status ?? artifact?.status ?? '';
  const terminal = artifact ? isTerminal(status) : false;
  const failed = status === 'failed' || status === 'cancelled';
  const files = artifact && terminal && !failed ? artifact.files ?? [] : [];
  // What the panel's live region says. Refusals are left to ViewerState's
  // own region so they are spoken once, not twice.
  const announcement =
    state.kind === 'loading'
      ? 'Loading the file…'
      : state.kind === 'ready' && artifact
        ? `${artifact.title}: ${statusLine(artifact, job)}`
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
  } else if (artifact.preview_kind === 'pages' && artifact.preview_pages > 0) {
    body = (
      <PagesViewer
        artifactId={artifact.artifact_id}
        version={artifact.version}
        pages={artifact.preview_pages}
        title={artifact.title}
      />
    );
  } else if (artifact.preview_kind === 'grid') {
    body = (
      <SheetViewer
        artifactId={artifact.artifact_id}
        version={artifact.version}
        title={artifact.title}
      />
    );
  } else {
    body = (
      <ViewerState
        kind="empty"
        detail={files.length ? 'Download the file to open it in its own application.' : undefined}
      />
    );
  }

  const headerTitle = artifact?.title ?? 'Generated file';
  const kind = artifact?.kind ?? 'document';

  return (
    <aside
      ref={panelRef}
      role="dialog"
      aria-labelledby={titleId}
      onKeyDown={onPanelKeyDown}
      data-testid="artifact-panel"
      className="fixed inset-0 z-50 flex h-full w-full flex-col border-border bg-bg min-[900px]:static min-[900px]:z-auto min-[900px]:border-l"
    >
      <header className="flex items-center gap-2 border-b border-border px-3 py-2">
        <button
          type="button"
          onClick={onClose}
          aria-label="Back to the conversation"
          className="inline-flex h-8 w-8 items-center justify-center rounded-lg text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink min-[900px]:hidden"
        >
          <IconChevronLeft size={18} />
        </button>
        <span
          aria-hidden
          className={`hidden h-8 w-8 shrink-0 items-center justify-center rounded-lg min-[900px]:flex ${
            kind === 'presentation'
              ? fileKind('x.pptx').className
              : kind === 'workbook'
                ? fileKind('x.xlsx').className
                : fileKind('x.docx').className
          }`}
        >
          <KindIcon kind={kind} size={16} />
        </span>
        <div className="min-w-0 flex-1">
          <h2 id={titleId} className="truncate text-sm font-semibold text-ink" title={headerTitle}>
            {headerTitle}
          </h2>
          <p className="flex flex-wrap items-center gap-x-1.5 text-[11px] text-muted">
            <span>{kindLabel(kind)}</span>
            {artifact && <span>· v{artifact.version}</span>}
            {files.map((f) => {
              const k = fileKind(f.filename || `x.${f.format}`);
              return (
                <span
                  key={f.format}
                  className={`rounded px-1 font-mono text-[10px] font-semibold ${k.className}`}
                >
                  {k.label}
                </span>
              );
            })}
          </p>
        </div>
        {artifact && files.length > 0 && <DownloadControl artifact={artifact} files={files} />}
        <button
          ref={closeRef}
          type="button"
          onClick={onClose}
          aria-label="Close preview"
          title="Close (Esc)"
          className="inline-flex h-8 w-8 items-center justify-center rounded-lg text-muted transition-colors duration-ts hover:bg-surface-2 hover:text-ink"
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
